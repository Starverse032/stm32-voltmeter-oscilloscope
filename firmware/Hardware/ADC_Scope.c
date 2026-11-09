#include "ADC_Scope.h"
#include "protocol_codec.h"   /* 复用 PROTO_RATE_10K / PROTO_RATE_100K，保证与协议同源 */

/* ============================================================================
 * ADC_Scope.c —— 等间隔采样的硬件链路
 *
 * 链路（全程硬件，CPU 不逐点参与）：
 *   TIM3 更新事件(TRGO) -> 触发 ADC1 规则组转换 -> ADC 转换完成产生 DMA 请求
 *   -> DMA1_Channel1 把 ADC1->DR 搬到 g_adc_buf[1024] -> 满 1024 点触发 TC 中断。
 *
 * 这正是不做"主循环 printf 一串数然后连线"的假示波器的关键：
 * 每个采样点由 TIM3 的更新事件等间隔触发，时间轴 t_i = i / sample_rate_hz
 * 是确定的（每帧从 0 重新开始，帧间空隙不隐藏）。
 *
 * ---- 引脚 ----
 *   PA0 = ADC1_IN0（模拟输入，GPIO_Mode_AIN）。电位器滑端接这里。
 *   TIM3 仅用内部 TRGO，不引出任何引脚；DMA1_Channel1 是 ADC1 专用通道，无引脚。
 *   因此与 USART1(PA9/PA10)、TIM1_CH1(PA8)、OLED(PB8/PB9) 都不冲突，
 *   详见 firmware/引脚资源核对.md。
 *
 * ---- 采样率推导（必须在代码与报告写清） ----
 *   system_stm32f10x.c 已启用 SYSCLK_FREQ_72MHz，HSE_VALUE=8MHz，SYSCLK=72MHz。
 *   APB1 预分频 = 2 -> PCLK1 = 36MHz；因 APB1 预分频不为 1，定时器时钟 = 2*PCLK1
 *   => TIM3 时钟 = 72MHz。TIM3 工作在"更新事件 = TRGO"，每 (PSC+1)*(ARR+1)
 *   个时钟发一次触发。
 *     PSC = 0。
 *     10 kS/s：触发周期 100us -> 72MHz / 10000 = 7200 -> ARR = 7200-1 = 7199。
 *     100 kS/s：触发周期 10us -> 72MHz / 100000 = 720  -> ARR = 720-1  = 719。
 *   设计默认 = 10 kS/s；SET_RATE 命令可切到 100 kS/s。
 *   100 kS/s 是设计目标，不是已上板实测指标。
 *
 * ---- ADC 转换时间相容性 ----
 *   PCLK2 = 72MHz，RCC_PCLK2_Div6 -> ADCCLK = 12MHz（不得超过 14MHz）。
 *   采样时间取 ADC_SampleTime_55Cycles5：一次转换 = 55.5 + 12.5 = 68 cycles
 *   = 68 / 12MHz = 5.667us。
 *   100 kS/s 触发间隔 10us > 5.667us -> 转换能在下一次触发前完成，相容；
 *   10 kS/s 间隔 100us 更宽松。
 *   信号源阻抗：55.5 cycles 的采样窗口对高阻信号源可能来不及把采样电容充满，
 *   导致读数偏低/失真。电位器滑端建议并联 0.1uF 或加电压跟随器缓冲；
 *   源阻抗过大时即便 55.5 cycles 也不够，属于后续扩展，不在第一版验证范围。
 *
 * ---- 外部触发源（核对 F103，不照搬其他系列） ----
 *   核对 Library/stm32f10x_adc.h（第 123~131 行）与 STM32F103 中容量参考手册：
 *   ADC1/ADC2 规则组外部触发只有 T1_CC1 / T1_CC2 / T2_CC2 / T3_TRGO /
 *   T4_CC4 / Ext_IT11_TIM8_TRGO / T1_CC3 / None。
 *   **没有 TIM2_TRGO、没有 TIM8_TRGO**（TIM8_TRGO 属 ADC3，或属别的系列）。
 *   因此选 ADC_ExternalTrigConv_T3_TRGO +
 *   TIM_SelectOutputTrigger(TIM3, TIM_TRGOSource_Update)（该宏=0x0020，
 *   已在 stm32f10x_tim.h 第 913 行确认存在）。
 *   这是核对 F103 参考手册/库头文件后的选择，不是照搬其他系列。
 *
 * ---- volatile 与"为什么不需要关中断/内存屏障" ----
 *   g_frame_ready / g_acquiring 被 DMA ISR 写、主循环读，必须 volatile：
 *   否则编译器看不到 ISR 会改它们，可能把主循环里的读优化成"只读一次寄存器"，
 *   导致主循环永远等不到帧就绪（死等）。volatile 强制每次都从内存重新读。
 *
 *   这里不需要关中断、也不需要内存屏障，理由：
 *   1) Cortex-M3 上对齐的 8/16/32 位访问是原子的；这两个标志都是整体读/整体写，
 *      没有"读-改-写"竞争（主循环只读标志、只在帧边界改状态；ISR 只置/清标志）。
 *   2) 写 g_adc_buf 的是 DMA 硬件，不是 CPU；DMA 与 CPU 走同一条总线，且
 *      F103 没有 D-Cache，不存在缓存一致性问题。
 *   3) 关键在"顺序"：ISR 里先 Scope_Stop()（停 TIM3/ADC 触发/DMA，确保 DMA 不再
 *      写缓冲），再置 g_frame_ready=1。主循环看到 1 时 DMA 一定已停，读缓冲不会
 *      与 DMA 写重叠。反过来，主循环只在"发送整帧完成后"才 Scope_StartFrame()
 *      启动下一帧，StartFrame 内部先把 g_frame_ready 清 0 再使能硬件，
 *      保证下一帧不会误判。整个握手靠这个顺序，而非靠关中断。
 *
 * ---- 单缓冲，为什么不需要双缓冲 ----
 *   采用严格时序：采满一帧 -> DMA ISR 立即停 TIM3/ADC 触发/DMA -> 置就绪标志
 *   -> 主循环把整帧发走 -> 发送全部完成后才重新配置并启动下一帧。
 *   发送期间 TIM3/ADC/DMA 全部停止，g_adc_buf 不会被覆盖，因此单缓冲就安全，
 *   不需要双缓冲，也就不存在"两块缓冲谁在读谁在写"的重叠问题。
 *
 * ---- OLED 刷新为什么不会破坏等间隔采样 ----
 *   采样是 TIM3+DMA 全硬件完成的，主循环里的 OLED 刷新（GPIO 软件 I2C bit-bang）
 *   和串口发送只占用 CPU，不会改变 TIM3 的 TRGO 周期，所以从原理上就不会破坏
 *   一帧内的等间隔采样。真正的硬约束是：一帧 DMA 正在采集时，主循环不得重新
 *   配置 TIM3(ARR/PSC)、ADC 或 DMA（那会改坏当帧）。本工程把所有这类配置变更
 *   都集中在 Scope_StartFrame()/Scope_Stop()（只在帧边界调用），并把 OLED 刷新
 *   进一步安排在 Scope_IsAcquiring()==0 的帧间/空闲期（见 main.c），双重稳妥。
 * ==========================================================================*/

/* DMA 硬件写、CPU 在 DMA 停止后读；靠上面说的顺序保证不重叠，故不加 volatile，
 * 也避免把 1024 个元素的遍历都强制成 volatile 访问（数据缓冲不是"状态变量"）。 */
static uint16_t g_adc_buf[SCOPE_POINT_COUNT];

static volatile uint8_t g_frame_ready = 0;  /* ISR 写、主循环读 */
static volatile uint8_t g_acquiring  = 0;  /* ISR/主循环写、主循环读 */

static uint32_t        g_rate_hz = PROTO_RATE_10K;  /* 当前真实生效采样率，只主循环访问 */
static DMA_InitTypeDef  s_dma;                       /* 每帧重新 DMA_Init 用的模板 */

void ADC_Scope_Init(void)
{
    GPIO_InitTypeDef        gpio;
    TIM_TimeBaseInitTypeDef tim;
    ADC_InitTypeDef         adc;
    NVIC_InitTypeDef        nvic;

    /* ---- 时钟 ---- */
    RCC_APB2PeriphClockCmd(RCC_APB2Periph_GPIOA | RCC_APB2Periph_ADC1 |
                           RCC_APB2Periph_AFIO, ENABLE);
    RCC_APB1PeriphClockCmd(RCC_APB1Periph_TIM3, ENABLE);
    RCC_AHBPeriphClockCmd(RCC_AHBPeriph_DMA1, ENABLE);
    RCC_ADCCLKConfig(RCC_PCLK2_Div6);   /* 72MHz/6 = 12MHz ADCCLK（<=14MHz） */

    /* ---- PA0 模拟输入 ---- */
    gpio.GPIO_Pin   = GPIO_Pin_0;
    gpio.GPIO_Mode  = GPIO_Mode_AIN;    /* 断开数字输入，电压直接进 ADC */
    gpio.GPIO_Speed = GPIO_Speed_50MHz;
    GPIO_Init(GPIOA, &gpio);

    /* ---- TIM3：只作触发源，不输出引脚 ----
     * PSC=0, ARR=7199 -> 默认 10 kS/s（见文件头推导）。
     * TIM_RepetitionCounter 对通用定时器无效，填 0 即可。 */
    tim.TIM_Period            = 7199u;
    tim.TIM_Prescaler         = 0u;
    tim.TIM_ClockDivision     = TIM_CKD_DIV1;
    tim.TIM_CounterMode       = TIM_CounterMode_Up;
    tim.TIM_RepetitionCounter = 0u;
    TIM_TimeBaseInit(TIM3, &tim);
    TIM_SelectOutputTrigger(TIM3, TIM_TRGOSource_Update);  /* TRGO = 更新事件 */
    /* 注意：这里不 TIM_Cmd，等 Scope_StartFrame 才启动。 */

    /* ---- ADC1：规则组、外部触发、右对齐、非连续、非扫描 ---- */
    ADC_RegularChannelConfig(ADC1, ADC_Channel_0, 1, ADC_SampleTime_55Cycles5);
    adc.ADC_Mode              = ADC_Mode_Independent;
    adc.ADC_ScanConvMode      = DISABLE;
    adc.ADC_ContinuousConvMode= DISABLE;                 /* 每次触发只转一次 */
    adc.ADC_ExternalTrigConv  = ADC_ExternalTrigConv_T3_TRGO;  /* 核对 F103 后的选择 */
    adc.ADC_DataAlign         = ADC_DataAlign_Right;   /* 读出即 0..4095 */
    adc.ADC_NbrOfChannel      = 1u;
    ADC_Init(ADC1, &adc);
    ADC_DMACmd(ADC1, ENABLE);                          /* 允许 ADC 产生 DMA 请求 */
    ADC_Cmd(ADC1, ENABLE);

    /* 上电校准，减小内部电容误差 */
    ADC_ResetCalibration(ADC1);
    while (ADC_GetResetCalibrationStatus(ADC1) == SET) { }
    ADC_StartCalibration(ADC1);
    while (ADC_GetCalibrationStatus(ADC1) == SET) { }

    ADC_ExternalTrigConvCmd(ADC1, DISABLE);            /* 先关，StartFrame 再开 */

    /* ---- DMA1_Channel1：ADC1->DR 到 g_adc_buf，半字，非循环，TC 中断 ---- */
    s_dma.DMA_PeripheralBaseAddr = (uint32_t)(&(ADC1->DR));
    s_dma.DMA_MemoryBaseAddr     = (uint32_t)g_adc_buf;
    s_dma.DMA_DIR                = DMA_DIR_PeripheralSRC;
    s_dma.DMA_BufferSize         = SCOPE_POINT_COUNT;
    s_dma.DMA_PeripheralInc      = DMA_PeripheralInc_Disable;
    s_dma.DMA_MemoryInc          = DMA_MemoryInc_Enable;
    s_dma.DMA_PeripheralDataSize = DMA_PeripheralDataSize_HalfWord;
    s_dma.DMA_MemoryDataSize     = DMA_MemoryDataSize_HalfWord;
    s_dma.DMA_Mode               = DMA_Mode_Normal;   /* 非循环：采满 1024 点即停 */
    s_dma.DMA_Priority           = DMA_Priority_High;
    s_dma.DMA_M2M                = DMA_M2M_Disable;
    DMA_DeInit(DMA1_Channel1);
    DMA_Init(DMA1_Channel1, &s_dma);
    DMA_ITConfig(DMA1_Channel1, DMA_IT_TC, ENABLE);
    DMA_Cmd(DMA1_Channel1, DISABLE);                   /* 先不使能 */

    /* ---- NVIC：DMA TC 抢占优先级最高，保证"采满立即停 TIM/ADC" ---- */
    nvic.NVIC_IRQChannel                   = DMA1_Channel1_IRQn;
    nvic.NVIC_IRQChannelPreemptionPriority = 0u;
    nvic.NVIC_IRQChannelSubPriority        = 0u;
    nvic.NVIC_IRQChannelCmd                = ENABLE;
    NVIC_Init(&nvic);

    g_rate_hz      = PROTO_RATE_10K;
    g_frame_ready  = 0;
    g_acquiring    = 0;
}

uint8_t Scope_SetRateHz(uint32_t hz)
{
    uint16_t arr;
    if (hz == PROTO_RATE_10K)       { arr = 7199u; }   /* 72MHz/7200 = 10 kS/s */
    else if (hz == PROTO_RATE_100K) { arr = 719u;  }   /* 72MHz/720  = 100 kS/s */
    else                            { return 0u;   }   /* 其它值拒绝 */

    /* 调用方(Serial_Protocol)用"忙判定"保证此刻 TIM3 已停、没有正在采集的一帧。
     * 帧间直接写 ARR，下次 TIM_Cmd 启动即以新周期触发（PSC 恒为 0）。 */
    TIM3->ARR = arr;
    g_rate_hz = hz;
    return 1u;
}

uint32_t Scope_GetRateHz(void)
{
    return g_rate_hz;
}

void Scope_StartFrame(void)
{
    /* 先清就绪标志，再使能硬件：保证下一帧不会因旧标志被误判。 */
    g_frame_ready = 0;
    g_acquiring   = 1;

    DMA_Cmd(DMA1_Channel1, DISABLE);
    DMA_Init(DMA1_Channel1, &s_dma);                  /* 重置 CNDTR/CMAR/CCR 到初始状态 */
    DMA_ITConfig(DMA1_Channel1, DMA_IT_TC, ENABLE);  /* DMA_Init 会重写 CCR，需重开 TC */
    DMA_ClearITPendingBit(DMA1_IT_GL1);
    DMA_Cmd(DMA1_Channel1, ENABLE);                   /* 等待 ADC 的 DMA 请求 */

    ADC_ExternalTrigConvCmd(ADC1, ENABLE);            /* 开外部触发 */
    TIM_SetCounter(TIM3, 0u);
    TIM_Cmd(TIM3, ENABLE);                            /* 启动 TRGO，采样开始 */
}

void Scope_Stop(void)
{
    TIM_Cmd(TIM3, DISABLE);                           /* 先掐断触发源 */
    ADC_ExternalTrigConvCmd(ADC1, DISABLE);           /* 关 ADC 外部触发 */
    DMA_Cmd(DMA1_Channel1, DISABLE);                  /* 关 DMA 通道（保险） */
    g_acquiring = 0;
}

uint8_t Scope_IsFrameReady(void)      { return g_frame_ready; }
void     Scope_ClearFrameReady(void)  { g_frame_ready = 0; }
uint8_t  Scope_IsAcquiring(void)      { return g_acquiring; }
const uint16_t *Scope_GetBuffer(void) { return g_adc_buf; }
uint16_t Scope_GetPointCount(void)    { return (uint16_t)SCOPE_POINT_COUNT; }

/* ---- DMA1_Channel1 传输完成中断 ----
 * 采满一帧 -> 立即停止 TIM3/ADC 外部触发/DMA -> 置就绪标志。
 * 之后主循环才会读缓冲并发送；发送期间硬件全停，缓冲区不会被覆盖。 */
void DMA1_Channel1_IRQHandler(void)
{
    if (DMA_GetITStatus(DMA1_IT_TC1) != RESET)
    {
        DMA_ClearITPendingBit(DMA1_IT_GL1);
        Scope_Stop();
        g_frame_ready = 1;
    }
}
