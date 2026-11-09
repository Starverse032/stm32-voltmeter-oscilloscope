#include "Serial_Protocol.h"
#include "protocol_codec.h"
#include "ADC_Scope.h"
#include "SelfTest_PWM.h"

/* ============================================================================
 * Serial_Protocol.c —— USART1 二进制协议（设备侧），依据 docs/protocol.md v1
 *
 * ---- 物理层 ----
 *   USART1：PA9=TX（设备->电脑）、PA10=RX（电脑->设备），115200 baud，8N1，无流控。
 *   **采样流里禁止任何 printf / 文本调试输出**：本文件发出的全是二进制帧。
 *
 * ---- 帧类型 ----
 *   收：0x20 COMMAND（LEN 1..8）。
 *   发：0x10 SAMPLE（固定 1024 点，LEN = 2058，整帧 2070 字节）、
 *       0x21 RESPONSE（LEN 固定 14）、0x22 EVENT（LEN 1..64）。
 *   解析器对四种 TYPE 都做 LEN/TYPE 自洽校验（支持 1..1024 点短采样帧），
 *   但设备作为接收端只会真正处理 0x20；方向不对的合法帧整帧丢弃并计数。
 *
 * ---- 传输能力（如实说明） ----
 *   115200 8N1 理论上限约 11520 B/s；一帧 1024 点 = 2070 字节 ≈ 180ms。
 *   本固件只做"采集一帧 -> 停止采样 -> 发送一帧"，帧间有空隙，
 *   绝不声称 115200 能连续无损传 100 kS/s 的 uint16 流。
 *   发送用轮询 TXE（主循环里完成，约 180ms）；期间 RXNE 中断仍把命令字节
 *   存进免锁 FIFO，SysTick 时基仍运行，命令在帧间空隙被处理，符合协议设计。
 *
 * ---- 接收架构：为什么不需要关中断 / 内存屏障 ----
 *   USART1_RXNE 中断**只做一件事**：把收到的字节放进一个软件 FIFO，ISR 极短。
 *   所有"解析、命令处理、发响应/EVENT"都在主循环 Serial_Process() 里做。
 *   FIFO 是经典的"单生产者单消费者"环形缓冲：
 *     - ISR 只写 head、只读 tail；主循环只写 tail、只读 head；
 *     - head/tail 声明为 volatile，保证两端都能看到对方的最新值；
 *     - Cortex-M3 上对齐的 16 位访问是原子的，且 F103 无 D-Cache，
 *       因此这个免锁 FIFO 不需要关中断，也不需要内存屏障。
 *   协议接收缓冲 s_rx_buf / 状态机变量**只被主循环访问**，与 ISR 没有共享，
 *   更不需要临界区。
 *   （对比：ADC_Scope.c 里的 g_frame_ready 是 ISR 与主循环共享的标志，
 *     那里用 volatile + "先停 DMA 后置标志"的顺序保证可见性，见该文件注释。）
 *
 * ---- 重同步状态机（协议第 6 节，逐条对应） ----
 *   1) 缓冲追加字节，s_rx_len > RX_BUF_MAX 时整体清空、上报一次错误、重新同步。
 *   2) 搜索 4 字节 MAGIC；未找到只保留末尾 3 字节（防 MAGIC 被分片切断）。
 *   3) 找到 MAGIC 后不足 6 字节头字段则等待更多数据。
 *   4) VER != 0x01 -> 丢弃 MAGIC 首字节、回 2。
 *   5) LEN > 2060 或 LEN 与 TYPE 不自洽 -> 判定非法长度，丢弃 MAGIC 首字节、回 2
 *      （不丢整个缓冲，保住后面紧跟的合法帧）。
 *   6) 等 6+LEN+2 齐备后算 CRC（覆盖 VER..PAYLOAD 末 = buf[4..4+6+LEN-1]）
 *      与帧尾比对：不匹配丢弃 MAGIC 首字节、回 2；匹配则处理并移除整帧，
 *      继续解析剩余字节（合并帧）。
 *   7) 截断包：某已定位 MAGIC 的帧 1s 内没齐备 -> 丢弃 MAGIC 首字节、提示一次。
 *   8) 计数：有效帧 / CRC 错 / 非法长度 / 超时 / 溢出分别统计（调试用）。
 *
 *   "丢弃 MAGIC 首字节"用 Rx_DropFirstByte() 实现：把 buf 整体左移 1 字节。
 *   这样无论 CRC 错、非法长度、错 VER，后面紧跟的合法帧都能被重新找回。
 * ==========================================================================*/

#define RX_FIFO_SIZE   512u

/* ---- ISR <-> 主循环共享：volatile + 单生产者单消费者免锁 FIFO ---- */
static volatile uint8_t  s_rx_fifo[RX_FIFO_SIZE];
static volatile uint16_t s_rx_fifo_head = 0;          /* ISR 写 */
static volatile uint16_t s_rx_fifo_tail = 0;          /* 主循环写 */
static volatile uint8_t  s_rx_fifo_overflow = 0;

/* ---- 只被主循环访问的协议接收缓冲与状态（不需 volatile / 不需临界区） ---- */
static uint8_t  s_rx_buf[PROTO_RX_BUF_MAX];           /* 8192 字节硬上限 */
static uint16_t s_rx_len = 0;
static uint32_t s_rx_last_ms = 0;

/* 发送缓冲：整帧上限 2072 字节；采样帧就地在此构造，无需额外 payload 副本 */
static uint8_t  s_tx_buf[PROTO_FRAME_MAX_LEN];

/* ---- 设备状态（命令处理在本文件内改；main 读/改少量） ---- */
static uint8_t  s_run_state   = RUN_STATE_IDLE;
static uint16_t s_vref_mv     = PROTO_VREF_DEFAULT_MV;  /* 默认 3300，掉电不保存 */
static uint32_t s_frames_sent = 0;
static uint8_t  s_frame_seq   = 0;   /* 采样帧 SEQ，设备自增 */
static uint8_t  s_event_seq   = 0;   /* 事件帧 SEQ，设备自增 */
static uint8_t  s_hw_busy     = 0;   /* main 置位：正在采集/发送一帧；仅主循环访问，无需 volatile */

/* ---- 统计计数（调试用，不进协议） ---- */
static uint32_t s_cnt_valid = 0, s_cnt_crc = 0, s_cnt_badlen = 0;
static uint32_t s_cnt_timeout = 0, s_cnt_overflow = 0, s_cnt_proto = 0;

/* ---------- 底层：USART 轮询发送（纯二进制，绝不 printf） ---------- */
static void Serial_SendRaw(const uint8_t *p, uint16_t len)
{
    uint16_t i;
    for (i = 0; i < len; i++)
    {
        while (USART_GetFlagStatus(USART1, USART_FLAG_TXE) == RESET) { }
        USART_SendData(USART1, (uint16_t)p[i]);
    }
    /* 等最后一字节真正移出移位寄存器，确保整帧发完才返回，
       然后采集状态机才会启动下一帧（帧间空隙语义）。 */
    while (USART_GetFlagStatus(USART1, USART_FLAG_TC) == RESET) { }
}

/* ---------- 0x21 RESPONSE（LEN 固定 14，SEQ 必须原样回显命令 SEQ） ---------- */
static void Serial_SendResponse(uint8_t seq, uint8_t ack_cmd, uint8_t status)
{
    uint8_t *p;
    uint16_t total;

    p = Protocol_BeginFrame(s_tx_buf, PROTO_TYPE_RESPONSE, seq, 14u);
    p[0] = ack_cmd;
    p[1] = status;
    /* status != 0 时仍填设备"当前真实值"，不是请求值，供电脑端回滚显示。 */
    Protocol_WriteU32LE(&p[2], Scope_GetRateHz());
    Protocol_WriteU16LE(&p[6], s_vref_mv);
    p[8] = s_run_state;
    p[9] = SelfTest_PWM_IsOn() ? DEV_FLAG_PWM_ON : 0x00u;
    Protocol_WriteU32LE(&p[10], s_frames_sent);
    total = Protocol_FinishFrame(s_tx_buf, 14u);
    Serial_SendRaw(s_tx_buf, total);
}

/* ---------- 0x22 EVENT ---------- */
static void Serial_SendEvent(uint8_t code, const char *msg)
{
    uint8_t payload[64];
    uint16_t len = 0;
    uint16_t total;

    payload[len++] = code;                    /* 偏移0：事件码 */
    while (msg && *msg && len < 64u)         /* 偏移1..：可选 ASCII 说明，不含中文 */
    {
        payload[len++] = (uint8_t)(*msg++);
    }
    total = Protocol_BuildFrame(s_tx_buf, PROTO_TYPE_EVENT, s_event_seq++, payload, len);
    Serial_SendRaw(s_tx_buf, total);
}

/* ---------- 0x10 SAMPLE（每帧 1024 点） ---------- */
void Serial_SendSampleFrame(const uint16_t *buf, uint16_t n)
{
    uint8_t *p;
    uint16_t len;
    uint16_t total;
    uint16_t i;

    len = (uint16_t)(10u + 2u * n);          /* n=1024 -> 2058 */
    p = Protocol_BeginFrame(s_tx_buf, PROTO_TYPE_SAMPLE, s_frame_seq, len);
    Protocol_WriteU32LE(&p[0], Scope_GetRateHz());  /* 真实生效采样率，不由电脑猜测 */
    Protocol_WriteU16LE(&p[4], n);                  /* point_count */
    Protocol_WriteU16LE(&p[6], s_vref_mv);          /* 设备当前参考电压 */
    Protocol_WriteU16LE(&p[8], 0u);                 /* reserved = 0 */
    for (i = 0; i < n; i++)
    {
        uint16_t code = buf[i] & 0x0FFFu;           /* 12 位右对齐，高位必须为 0 */
        Protocol_WriteU16LE(&p[10u + 2u * i], code);
    }
    total = Protocol_FinishFrame(s_tx_buf, len);
    Serial_SendRaw(s_tx_buf, total);

    s_frame_seq++;                                   /* 采样帧 SEQ 自增，低 8 位回绕 */
    s_frames_sent++;                                 /* 累计已发送采样帧数 */
}

/* ---------- 命令处理（协议第 4.2 节） ---------- */
static void Serial_HandleCommand(const uint8_t *payload, uint16_t len, uint8_t seq)
{
    uint8_t  cmd = payload[0];              /* len>=1 已由 LEN/TYPE 自洽校验保证 */
    uint8_t  status = STATUS_OK;
    uint32_t rate;
    uint16_t vref;

    switch (cmd)
    {
        case CMD_START:                      /* 进入连续采集 */
            if (len != 1u) { status = STATUS_BAD_PARAM; }
            else           { s_run_state = RUN_STATE_RUN; }
            break;

        case CMD_STOP:                       /* 停止；当前帧发完后不再启动新帧 */
            if (len != 1u) { status = STATUS_BAD_PARAM; }
            else           { s_run_state = RUN_STATE_IDLE; }
            break;

        case CMD_SINGLE:                     /* 只采发 1 帧，完成后 main 置回 IDLE */
            if (len != 1u) { status = STATUS_BAD_PARAM; }
            else           { s_run_state = RUN_STATE_SINGLE_BUSY; }
            break;

        case CMD_SET_RATE:                   /* 4 字节 uint32 LE */
            if (len != 5u) { status = STATUS_BAD_PARAM; break; }
            rate = Protocol_ReadU32LE(&payload[1]);
            if (rate != PROTO_RATE_10K && rate != PROTO_RATE_100K)
            {
                status = STATUS_BAD_PARAM;   /* 只允许 10000 / 100000 */
                break;
            }
            if (Serial_IsBusy())
            {
                status = STATUS_BUSY;        /* 忙：回 0x02 且不改参数 */
                break;
            }
            if (!Scope_SetRateHz(rate))
            {
                status = STATUS_HW_ERROR;
            }
            break;

        case CMD_SET_VREF:                   /* 2 字节 uint16 LE，2500..3600 */
            if (len != 3u) { status = STATUS_BAD_PARAM; break; }
            vref = Protocol_ReadU16LE(&payload[1]);
            if (vref < PROTO_VREF_MIN_MV || vref > PROTO_VREF_MAX_MV)
            {
                status = STATUS_BAD_PARAM;
                break;
            }
            s_vref_mv = vref;                /* 只改 RAM，掉电不保存；OLED 与电脑端同口径 */
            break;

        case CMD_PING:                       /* 立即回响应，不改状态 */
            if (len != 1u) { status = STATUS_BAD_PARAM; }
            break;

        case CMD_SELFTEST_PWM:               /* 1 字节 0/1，开关 PA8 100Hz 自检 PWM */
            if (len != 2u)          { status = STATUS_BAD_PARAM; break; }
            if (payload[1] > 1u)    { status = STATUS_BAD_PARAM; break; }
            SelfTest_PWM_Set(payload[1]);
            break;

        case CMD_QUERY_STATUS:               /* 只回响应，不改运行状态 */
            if (len != 1u) { status = STATUS_BAD_PARAM; }
            break;

        default:
            status = STATUS_UNSUPPORTED;     /* 不支持的命令 */
            break;
    }

    /* 关键约束：SEQ 必须原样回显对应命令的 SEQ。 */
    Serial_SendResponse(seq, cmd, status);
}

/* ---------- 接收状态机（协议第 6 节） ---------- */

/* LEN 与 TYPE 自洽校验（第 4.1 / 4.2 / 4.3 / 4.4 节）。
 * 采样帧支持 1..1024 点短帧：LEN = 10 + 2*N。 */
static uint8_t Rx_LenTypeConsistent(uint8_t type, uint16_t len)
{
    uint16_t n;
    switch (type)
    {
        case PROTO_TYPE_SAMPLE:
            if (len < 12u || len > (10u + 2u * PROTO_POINT_MAX)) { return 0u; }
            if (((len - 10u) & 1u) != 0u) { return 0u; }
            n = (uint16_t)((len - 10u) / 2u);
            return (uint8_t)(n >= 1u && n <= PROTO_POINT_MAX);
        case PROTO_TYPE_COMMAND:  return (uint8_t)(len >= 1u && len <= 8u);
        case PROTO_TYPE_RESPONSE: return (uint8_t)(len == 14u);
        case PROTO_TYPE_EVENT:    return (uint8_t)(len >= 1u && len <= 64u);
        default:                  return 0u;   /* 未知 TYPE */
    }
}

/* 缓冲区整体左移 count 字节，源区间位于目标右侧，正向复制可处理重叠。
 * 这里使用简单循环保持实现独立；不据此推断 memmove 的链接体积。
 * 早期 Flash 溢出的实际根因是运行时嵌套 CRC 宏展开，见验证报告。 */
static void Rx_Shift(uint16_t count)
{
    uint16_t i;
    if (count == 0u || count > s_rx_len) { return; }
    for (i = 0u; (uint32_t)i + count < s_rx_len; i++)
    {
        s_rx_buf[i] = s_rx_buf[i + count];
    }
    s_rx_len -= count;
}

/* 丢弃 MAGIC 首字节（整体左移 1 字节），回到"重新搜索 MAGIC"。
 * 这是 CRC 错 / 非法长度 / 错 VER / 超时后的统一恢复动作。 */
static void Rx_DropFirstByte(void)
{
    Rx_Shift(1u);
}

/* 在缓冲区里找 MAGIC，返回其索引；未找到返回 -1。 */
static int16_t Rx_FindMagic(void)
{
    uint16_t i;
    if (s_rx_len < 4u) { return -1; }
    for (i = 0u; (uint32_t)i + 4u <= s_rx_len; i++)
    {
        if (s_rx_buf[i]   == PROTO_MAGIC0 && s_rx_buf[i+1] == PROTO_MAGIC1 &&
            s_rx_buf[i+2] == PROTO_MAGIC2 && s_rx_buf[i+3] == PROTO_MAGIC3)
        {
            return (int16_t)i;
        }
    }
    return -1;
}

/* 尝试从当前缓冲解析出尽可能多的完整帧（合并帧）。 */
static void Rx_TryParse(uint32_t now_ms)
{
    (void)now_ms;   /* 时间戳在 Serial_Process 里统一维护，这里只做解析 */
    for (;;)
    {
        int16_t  mi;
        uint8_t  type;
        uint16_t len, total;
        uint16_t crc, crc_frame;

        mi = Rx_FindMagic();
        if (mi < 0)
        {
            /* 规则 2：未找到 MAGIC，只保留末尾 3 字节，丢弃其余噪声 */
            if (s_rx_len > 3u)
            {
                Rx_Shift((uint16_t)(s_rx_len - 3u));   /* 只保留末尾 3 字节 */
            }
            return;
        }
        if (mi > 0)
        {
            /* 丢弃 MAGIC 之前的噪声字节 */
            Rx_Shift((uint16_t)mi);
        }
        /* 现在 buf[0..3] = MAGIC */

        /* 规则 3：不足 6 字节头字段，等待更多数据 */
        if (s_rx_len < PROTO_HEADER_LEN) { return; }

        /* 规则 4：VER != 0x01 -> 丢 MAGIC 首字节、重新搜索 */
        if (s_rx_buf[4] != PROTO_VER)
        {
            s_cnt_proto++;
            Serial_SendEvent(0x01u, "bad VER");
            Rx_DropFirstByte();
            continue;
        }

        type = s_rx_buf[5];
        len  = (uint16_t)((uint16_t)s_rx_buf[6] | ((uint16_t)s_rx_buf[7] << 8));

        /* 规则 5：LEN > 2060 或 LEN 与 TYPE 不自洽 -> 非法长度，丢 MAGIC 首字节。
         * 未知 TYPE 也归到这一类（无法自洽），统一重搜，利于紧随其后的合法帧恢复。 */
        if (len > PROTO_LEN_MAX || !Rx_LenTypeConsistent(type, len))
        {
            s_cnt_badlen++;
            Serial_SendEvent(0x01u, "bad LEN/TYPE");
            Rx_DropFirstByte();
            continue;
        }

        total = (uint16_t)(len + PROTO_HEADER_LEN + PROTO_CRC_LEN);  /* = LEN + 12 */

        /* 规则 6：等 6+LEN+2 齐备（截断包交给 Serial_Process 的 1s 超时处理） */
        if (s_rx_len < total) { return; }

        /* CRC 覆盖 buf[4 .. 4+6+LEN-1]，共 6+LEN 字节；帧尾低字节在前。 */
        crc = Protocol_Crc16(s_rx_buf + 4, (uint32_t)(6u + len));
        crc_frame = (uint16_t)((uint16_t)s_rx_buf[PROTO_HEADER_LEN + len] |
                     ((uint16_t)s_rx_buf[PROTO_HEADER_LEN + len + 1u] << 8));
        if (crc != crc_frame)
        {
            s_cnt_crc++;
            Serial_SendEvent(0x01u, "CRC error");
            Rx_DropFirstByte();
            continue;
        }

        /* CRC 通过，交给上层 */
        if (type == PROTO_TYPE_COMMAND)
        {
            s_cnt_valid++;
            /* payload 从偏移 10 开始；SEQ 在偏移 8 */
            Serial_HandleCommand(s_rx_buf + PROTO_HEADER_LEN, len, s_rx_buf[8]);
        }
        else
        {
            /* 方向不对的合法帧（设备不该收到 0x10/0x21/0x22）：整帧丢弃并计数一次协议错误 */
            s_cnt_proto++;
            Serial_SendEvent(0x01u, "unexpected TYPE");
        }

        /* 移除整帧，继续解析剩余字节（一次 read 可能含多帧 = 合并帧） */
        Rx_Shift(total);
    }
}

/* 把一个收到的字节追加到缓冲并尝试解析。仅主循环调用。 */
static void Rx_FeedByte(uint8_t b, uint32_t now_ms)
{
    s_rx_last_ms = now_ms;

    /* 规则 1：超过 RX_BUF_MAX 硬上限 -> 整体清空、重新寻找 MAGIC、上报一次错误 */
    if (s_rx_len >= PROTO_RX_BUF_MAX)
    {
        s_rx_len = 0;
        s_cnt_overflow++;
        Serial_SendEvent(0x01u, "RX buf overflow");
    }
    s_rx_buf[s_rx_len++] = b;
    Rx_TryParse(now_ms);
}

/* ---------- 对外：主循环每轮调用 ---------- */
void Serial_Process(uint32_t now_ms)
{
    /* 1) 把免锁 FIFO 里的字节全部取出喂状态机（单消费者） */
    while (s_rx_fifo_tail != s_rx_fifo_head)
    {
        uint8_t b = s_rx_fifo[s_rx_fifo_tail];
        s_rx_fifo_tail = (uint16_t)((s_rx_fifo_tail + 1u) % RX_FIFO_SIZE);
        Rx_FeedByte(b, now_ms);
    }

    /* 2) FIFO 满导致丢字节：上报一次 */
    if (s_rx_fifo_overflow)
    {
        s_rx_fifo_overflow = 0;
        s_cnt_overflow++;
        Serial_SendEvent(0x01u, "RX fifo full");
    }

    /* 3) 规则 7：截断包 1s 超时恢复。丢弃 MAGIC 首字节、提示一次，
     *    更新 s_rx_last_ms 避免同一残帧反复上报，然后继续尝试解析。 */
    if (s_rx_len > 0u && (uint32_t)(now_ms - s_rx_last_ms) > PROTO_FRAME_TIMEOUT_MS)
    {
        s_rx_last_ms = now_ms;
        s_cnt_timeout++;
        Serial_SendEvent(0x03u, "RX timeout");
        Rx_DropFirstByte();
        Rx_TryParse(now_ms);
    }
}

/* ---------- 对外：初始化 USART1 115200 8N1 + RXNE 中断 ---------- */
void Serial_Protocol_Init(void)
{
    GPIO_InitTypeDef  gpio;
    USART_InitTypeDef usart;
    NVIC_InitTypeDef  nvic;

    RCC_APB2PeriphClockCmd(RCC_APB2Periph_USART1 | RCC_APB2Periph_GPIOA |
                           RCC_APB2Periph_AFIO, ENABLE);

    /* PA9 = USART1_TX（复用推挽）；PA10 = USART1_RX（上拉输入）。
     * USART1 默认就在 PA9/PA10，不需要 AFIO 重映射。 */
    gpio.GPIO_Pin   = GPIO_Pin_9;
    gpio.GPIO_Mode  = GPIO_Mode_AF_PP;
    gpio.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOA, &gpio);
    gpio.GPIO_Pin   = GPIO_Pin_10;
    gpio.GPIO_Mode  = GPIO_Mode_IPU;
    GPIO_Init(GPIOA, &gpio);

    usart.USART_BaudRate            = 115200u;
    usart.USART_WordLength          = USART_WordLength_8b;
    usart.USART_StopBits            = USART_StopBits_1;
    usart.USART_Parity              = USART_Parity_No;
    usart.USART_HardwareFlowControl = USART_HardwareFlowControl_None;
    usart.USART_Mode                = USART_Mode_Rx | USART_Mode_Tx;
    USART_Init(USART1, &usart);

    /* 只开 RXNE 中断；TX 用主循环轮询，避免中断里做长帧发送 */
    USART_ITConfig(USART1, USART_IT_RXNE, ENABLE);
    nvic.NVIC_IRQChannel                   = USART1_IRQn;
    nvic.NVIC_IRQChannelPreemptionPriority = 1u;   /* 低于 DMA(0)，保证采满立即停 */
    nvic.NVIC_IRQChannelSubPriority        = 0u;
    nvic.NVIC_IRQChannelCmd                = ENABLE;
    NVIC_Init(&nvic);
    USART_Cmd(USART1, ENABLE);

    s_rx_fifo_head     = 0;
    s_rx_fifo_tail     = 0;
    s_rx_fifo_overflow = 0;
    s_rx_len           = 0;
    s_rx_last_ms       = 0;
    s_run_state        = RUN_STATE_IDLE;
    s_vref_mv          = PROTO_VREF_DEFAULT_MV;
    s_frames_sent      = 0;
    s_frame_seq        = 0;
    s_event_seq        = 0;
    s_hw_busy          = 0;
}

/* ---------- 对外：设备状态 getter/setter ---------- */
uint8_t  Serial_GetRunState(void)             { return s_run_state; }
void     Serial_SetRunState(uint8_t s)        { s_run_state = s; }
uint16_t Serial_GetVrefMv(void)               { return s_vref_mv; }
uint32_t Serial_GetRateHz(void)               { return Scope_GetRateHz(); }
void     Serial_SetHwBusy(uint8_t busy)       { s_hw_busy = busy; }
uint8_t  Serial_IsBusy(void)
{
    /* 忙 = 连续运行 / 单次忙 / 正在采集或发送一帧（由 main 用 SetHwBusy 通知）。
     * 协议要求：忙时 SET_RATE 回 0x02 且不改参数。 */
    return (uint8_t)((s_run_state != RUN_STATE_IDLE) || s_hw_busy);
}

/* ---------- USART1 接收中断：只入 FIFO，ISR 极短 ---------- */
void USART1_IRQHandler(void)
{
    if (USART_GetITStatus(USART1, USART_IT_RXNE) != RESET)
    {
        uint8_t  b = (uint8_t)USART_ReceiveData(USART1);  /* 读 DR 自动清 RXNE */
        uint16_t next = (uint16_t)((s_rx_fifo_head + 1u) % RX_FIFO_SIZE);
        if (next != s_rx_fifo_tail)                       /* 单生产者：只写 head */
        {
            s_rx_fifo[s_rx_fifo_head] = b;
            s_rx_fifo_head = next;
        }
        else
        {
            s_rx_fifo_overflow = 1;                       /* 满：丢字节，主循环上报 */
        }
    }
}
