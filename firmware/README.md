# STM32F103C8T6 电压表 / 示波器采集端固件

> Codex 复核更新（2026-11-09）：`main.c` 现在只在上一帧处理完后解析命令，避免 SINGLE 收尾覆盖新运行命令，以及旧帧落在 SINGLE 确认之后。以下历史构建数字保留作过程记录；最新全量构建为 Code=14258、RO-data=1790、RW-data=112、ZI-data=14504，Flash=16160 B、RAM=14616 B，0 错 0 警，HEX=45501 B。完整说明见 `../outputs/Codex复核报告.md`。尚未上板验证。

第一版定位为**低压、低频、单通道教学仪器**的采集端：TIM3 硬件触发 + ADC + DMA 等间隔采样，
USART1 二进制协议上报，OLED 独立显示直流电压。协议**唯一依据**是 `docs/protocol.md v1`，
本固件不自行扩展任何字段。代码使用 UTF-8、中文注释解释原理，驱动独立封装，`main.c` 只做调度。

> 本固件**未上板验证**，不声称上板通过、不编造实测数据。100 kS/s 是设计目标，不是实测指标。

---

## 1. 目录结构

```
firmware/
├── Project.uvprojx         Keil 工程（设备/内存/分组/编译选项，见第 8 节）
├── Project.uvoptx          Keil IDE 会话配置（已清除旧舵机文件引用）
├── build.ps1               命令行构建脚本（调用本机 UV4.exe）
├── README.md               本文件
├── 引脚资源核对.md          引脚/复用功能冲突核对结论
├── Start/                  启动文件、core_cm3、stm32f10x.h、system_stm32f10x.*
│                           （含 md/ld/hd 等全部 startup_*.s，工程只编译 md）
├── Library/                STM32F10x 标准外设库整套 .c/.h（conf.h 会 include 全部头文件）
├── System/                 Delay.c/.h（SysTick 轮询延时，工程保留但本版未调用）
├── User/
│   ├── main.c               总调度（采集状态机、OLED 刷新时机）
│   ├── stm32f10x_conf.h     标准库配置（复制自参考工程）
│   └── stm32f10x_it.c/.h    异常向量 + SysTick_Handler -> TimeBase_Tick()
├── Hardware/
│   ├── OLED.c/.h/OLED_Font.h    软件 I2C OLED 文本显示（PB8/PB9，复制自参考工程）
│   ├── ADC_Scope.c/.h           ADC1_IN0 + TIM3 TRGO + DMA1_Channel1 采集
│   ├── Serial_Protocol.c/.h     USART1 协议：命令/响应/采样帧/EVENT + 重同步状态机
│   ├── OLED_Voltmeter.c/.h      本地电压表显示（平均/最小/最大 + 状态）
│   └── SelfTest_PWM.c/.h        PA8 = TIM1_CH1，100 Hz 自检 PWM
├── Common/
│   ├── protocol_codec.c/.h      协议常量、CRC16、小端读写、通用帧封装
│   └── TimeBase.c/.h            SysTick 1 ms 时基（OLED 刷新 + 串口 1s 截断超时）
├── Tests/
│   └── protocol_selftest.c      编译期 CRC 断言 + golden 帧常量（参与 Keil 编译）
├── Objects/                 构建输出（build.log、Project.axf、Project.hex 等）
└── Listings/                链接 map / 列表文件
```

---

## 2. 各驱动职责

| 文件 | 职责 |
| --- | --- |
| `User/main.c` | 初始化各模块；主循环每轮 `Serial_Process()` -> `App_ScopeTask()`；在帧间刷新 OLED。**不等待电脑握手**，上电即作为电压表运行。 |
| `Hardware/ADC_Scope.c/.h` | ADC1_IN0(PA0) + TIM3 更新事件 TRGO 硬件触发 + DMA1_Channel1 搬 1024 个 uint16 到 `g_adc_buf[1024]`；非循环 DMA、传输完成中断 `DMA1_Channel1_IRQHandler`；`Scope_SetRateHz/StartFrame/Stop/IsFrameReady/...`。 |
| `Hardware/Serial_Protocol.c/.h` | USART1 115200 8N1；接收字节流重同步状态机（MAGIC 搜索、LEN 上限 2060、RX_BUF_MAX 8192、CRC 错/非法长度丢弃 MAGIC 首字节后重搜、1s 截断超时）；命令 0x01~0x08 解析与 14 字节响应；0x10 采样帧、0x22 事件帧构造；SEQ 回显与忙判定。 |
| `Common/protocol_codec.c/.h` | 协议常量、CRC-16/CCITT-FALSE（`CRC_BIT1` 单步宏 / `CRC_STEP` 8 层嵌套宏 + `Protocol_Crc16`）、小端读写、通用帧封装 `Begin/Finish/BuildFrame`。**运行时 `Protocol_Crc16` 用循环调用 8 次 `CRC_BIT1` 单步宏**（不直接展开 `CRC_STEP`，否则 3^8 份表达式会生成约 110 KB 机器码撑爆 64 KB Flash；详见第 8 节）。 |
| `Common/TimeBase.c/.h` | SysTick 1 ms 中断维护 `volatile` 毫秒计数，供 OLED 200 ms 刷新与串口 1 s 截断超时。 |
| `Hardware/OLED_Voltmeter.c/.h` | 把一帧 ADC 码换算成电压（`mV = code*vref_mv/4095`，整数等价协议浮点公式），显示平均/最小/最大电压、运行状态、参考电压。 |
| `Hardware/SelfTest_PWM.c/.h` | PA8 = TIM1_CH1，100 Hz、0~3.3 V 方波，默认关闭，由命令 0x07 开关；**仅自检信号**。 |
| `Tests/protocol_selftest.c` | 用 `CRC_STEP`（8 层 `CRC_BIT1` 嵌套）做 64 条分段独立 `#if` 编译期 CRC 断言（4 组 golden），并放一份人工写死的样例 B 完整帧常量（第 255 行），供跨实现核对；与运行时函数由**同一套 `CRC_BIT1` 宏定义**保证语义一致（详见第 6/8 节）。 |

---

## 3. 采样率推导（实际配置值）

时钟来自 `Start/system_stm32f10x.c`：`SYSCLK_FREQ_72MHz` 已启用，SYSCLK = 72 MHz，
`stm32f10x.h` 中 MD 分支 `HSE_VALUE = 8000000`（8 MHz，晶振实际值仍需现场确认）。

- **TIM3 时钟**：APB1 预分频 = 2 -> PCLK1 = 36 MHz；因 APB1 预分频不为 1，定时器时钟
  = 2 x PCLK1 = **72 MHz**。TIM3 工作在"更新事件 = TRGO"，每次更新触发一次 ADC 转换。
- `PSC = 0`：
  - **10 kS/s（默认）**：72 MHz / 10000 = 7200 -> `ARR = 7200 - 1 = 7199`（触发周期 100 us）。
  - **100 kS/s（SET_RATE 可切）**：72 MHz / 100000 = 720 -> `ARR = 720 - 1 = 719`（触发周期 10 us）。
- **ADC 时钟**：PCLK2 = 72 MHz，`RCC_PCLK2_Div6` -> ADCCLK = **12 MHz**（不得超过 14 MHz）。
- **转换时间相容性**：采样时间 `ADC_SampleTime_55Cycles5`，一次转换
  = 55.5 + 12.5 = 68 cycles = 68 / 12 MHz = **5.667 us**。
  100 kS/s 触发间隔 10 us > 5.667 us，转换能在下次触发前完成，相容；10 kS/s 的 100 us 更宽松。
- **信号源阻抗**：55.5 cycles 采样窗口对高阻信号源可能充不满采样电容，电位器滑端建议
  并联 0.1 uF 或加电压跟随器缓冲；源阻抗过大时即便 55.5 cycles 也不够，属后续扩展。

**外部触发源选择（核对 F103，不照搬其他系列）**：核对 `Library/stm32f10x_adc.h`
（第 123~131 行）与 STM32F103 中容量参考手册，ADC1/ADC2 规则组外部触发只有
`T1_CC1 / T1_CC2 / T2_CC2 / T3_TRGO / T4_CC4 / Ext_IT11_TIM8_TRGO / T1_CC3 / None`，
**没有 TIM2_TRGO、没有 TIM8_TRGO**（TIM8_TRGO 属 ADC3 或别的系列）。故选择
`ADC_ExternalTrigConv_T3_TRGO` + `TIM_SelectOutputTrigger(TIM3, TIM_TRGOSource_Update)`
（`TIM_TRGOSource_Update = 0x0020`，已在 `stm32f10x_tim.h` 第 913 行确认存在）。

采样帧里 `sample_rate_hz` 填 `Scope_GetRateHz()` 的**真实生效值**，不由电脑端猜测。
100 kS/s 是设计目标，不是已上板实测指标；**采样率不等于带宽**。

---

## 4. TIM / DMA / ADC 时序与缓冲区生命周期

```
Scope_StartFrame()
  -> 清就绪标志、重新 DMA_Init(DMA1_Channel1)（重置 CNDTR/CMAR/CCR）、
     开 DMA TC 中断、使能 DMA、使能 ADC 外部触发、TIM3 清零并启动
  -> TIM3 每 ARR+1 个时钟发一次 TRGO -> 触发 ADC1 规则组转换
  -> 转换完成产生 DMA 请求 -> DMA 把 ADC1->DR 搬到 g_adc_buf[]（全硬件，CPU 不逐点参与）

采满 1024 点 -> DMA1_Channel1_IRQHandler：
  -> 先 Scope_Stop()：停 TIM3 / 关 ADC 外部触发 / 关 DMA（确保不再写缓冲）
  -> 再置 g_frame_ready = 1
主循环看到就绪：
  -> 刷新 OLED（本地电压表）
  -> 构造并发送整帧 0x10（USART1 轮询发送，约 180 ms）
  -> 发送完成后 Scope_ClearFrameReady() + 才允许启动下一帧
```

**单缓冲，为什么不需要双缓冲**：发送期间 TIM3/ADC/DMA 全部停止，`g_adc_buf` 不会被覆盖，
因此单缓冲即安全；"发送期间绝不覆盖正在读取的缓冲区"由上述严格时序保证。

**为什么 OLED 刷新不破坏一帧内的等间隔采样**：采样是 TIM3+DMA 全硬件完成的，主循环里的
OLED 软件 I2C bit-bang 和串口发送只占 CPU，不改变 TIM3 的 TRGO 周期。唯一硬约束是
"一帧 DMA 正在采集时不得重新配置 TIM3/ADC/DMA"，本工程把所有这类配置集中在
`Scope_StartFrame()/Scope_Stop()`（只在帧边界调用），并把 OLED 刷新进一步安排在
"帧就绪、DMA 已停"之后（`Scope_IsAcquiring()==0` 的帧间/空闲期），双重稳妥。

> 115200 8N1 理论上限约 11520 B/s，一帧 1024 点 = 2070 字节 ≈ 180 ms。本固件只做
> "采集一帧 -> 停止采样 -> 发送一帧"，**帧间存在空隙**，不声称能连续无损传 100 kS/s 流。

---

## 5. volatile 与"为什么不需要关中断/内存屏障"

| 变量 | 位置 | 共享方 | 为什么 |
| --- | --- | --- | --- |
| `g_frame_ready` / `g_acquiring` | `ADC_Scope.c` | DMA ISR 写、主循环读 | `volatile` 强制每次从内存读；否则编译器可能把主循环的读优化成"只读一次寄存器"而永远等不到帧就绪。 |
| `s_ms` | `TimeBase.c` | SysTick ISR 写、主循环读 | 同上，毫秒时基必须可见。 |
| `s_rx_fifo` / `head` / `tail` / `overflow` | `Serial_Protocol.c` | USART1 RXNE ISR 写、主循环读 | 经典"单生产者单消费者"免锁环形 FIFO：ISR 只写 head、只读 tail，主循环只写 tail、只读 head；Cortex-M3 上对齐访问是原子的，F103 无 D-Cache，因此**不需要关中断、不需要内存屏障**。 |

不需要关中断/内存屏障的进一步理由：

1. 两个标志都是整体读/整体写，没有"读-改-写"竞争（主循环只读标志、只在帧边界改状态；
   ISR 只置/清标志）。
2. 写 `g_adc_buf` 的是 **DMA 硬件**而非 CPU；DMA 与 CPU 走同一总线、F103 无 D-Cache，
   不存在缓存一致性问题。`g_adc_buf` 是数据缓冲而非"状态变量"，靠"先停 DMA 后置就绪标志"
   的顺序保证 CPU 读时 DMA 已不再写，因此**不需要 volatile**（也避免把 1024 个元素的遍历
   都强制成 volatile 访问）。
3. 串口接收缓冲 `s_rx_buf` 与解析状态机**只被主循环访问**（ISR 只往 FIFO 放字节），
   与 ISR 没有共享，更不需要临界区。
4. USART 发送用主循环轮询；发送期间 RXNE 中断仍把命令字节存入免锁 FIFO，SysTick 时基仍
   运行，命令在帧间空隙被处理，符合协议"帧间空隙"设计。

---

## 6. 协议实现位置

- **帧常量 / CRC / 小端 / 帧封装**：`Common/protocol_codec.h/.c`
  - `CRC-16/CCITT-FALSE`，覆盖 `VER..PAYLOAD` 共 `6+LEN` 字节，帧内低字节在前。
  - `CRC_BIT1` 照抄 `docs/protocol.md` 第 5 节（已核对为括号平衡版本、8 层嵌套）；
    `CRC_STEP` 保留其 8 层嵌套、括号平衡结构，仅把 `(uint16_t)` 强制类型转换换成数值
    等价的 `& 0xFFFFu`——因为 `#if` 预处理表达式不允许 C 类型转换（详见头文件注释）。
  - **运行时与编译期的区别（关键，见第 8 节 P3）**：编译期断言用 `CRC_STEP`（预处理期
    求值、不生成代码，64 条分段独立 `#if` 避免 ARMCC `out of memory`）；运行时
    `Protocol_Crc16` **不展开** `CRC_STEP`，而是循环调用**同一个 `CRC_BIT1` 单步宏** 8 次。
    两者由同一套 `CRC_BIT1` 宏定义保证语义一致。若运行时直接展开 8 层嵌套，`(c)` 每层
    出现 3 次 → 3^8 份表达式会被编译成约 110 KB 机器码，撑爆 64 KB Flash。
- **命令/响应/采样帧/EVENT/重同步状态机**：`Hardware/Serial_Protocol.c/.h`
  - 命令 0x01 START / 0x02 STOP / 0x03 SINGLE / 0x04 SET_RATE / 0x05 SET_VREF /
    0x06 PING / 0x07 SELFTEST_PWM / 0x08 QUERY_STATUS。
  - RESPONSE（LEN=14）SEQ 原样回显，`status != 0` 时 `actual_rate_hz`/`vref_mv` 仍填真实值。
  - SET_RATE 忙时回 `status=0x02` 且不改参数；参数变更需先 STOP 再 SET_RATE 再 START。
  - 采样帧固定 1024 点，payload 逐字段按协议第 4.1 节，样本为 12 位右对齐原始码。
  - 重同步按协议第 6 节逐条实现：MAGIC 搜索、未找到只留末尾 3 字节、VER/LEN/TYPE 自洽、
    CRC 校验、错帧/非法长度/超时统一"丢弃 MAGIC 首字节"后重搜、合并帧、1 s 截断恢复、分类计数。
  - **USART1 采样流中禁止任何 printf/文本调试输出**，全部为二进制帧。
- **跨实现核对证据**：`Tests/protocol_selftest.c`
  - 64 个逐字节 `#if CRC_STEP(...) != 期望值 #error` 断言，覆盖
    `"123456789" -> 0x29B1`、样例 A `0x8278`、样例 B `0x7EA4`、样例 C `0xE73A`
    （四个文档期望值**未改**）。
  - `const uint8_t kGoldenSampleFrame[]`（第 255 行）逐字节等于协议样例 B 完整帧
    `A5 5A C3 3C 01 10 12 00 2A 00 10 27 00 00 04 00 E4 0C 00 00 00 00 00 08 FF 0F D2 04 A4 7E`，
    **人工写死、不调用编码器生成**，供 Python 侧解析本文件文本做跨实现核对。
  - 该文件已加入 Keil 工程参与编译，**"ARMCC 编译通过"即证明 C 侧 CRC 与文档/Python 一致**。

> 说明：最初按文档第 8 节把整条 CRC_STEP 嵌套链放进单个 `#if`，但本机 ARMCC V5.06 update 5
> 实测仅 9 字节链就报 `#4: out of memory`（预处理器对超长/超深 `#if` 常量表达式的内存限制）。
> 故改为逐字节短 `#if`（每个仍展开同一个 `CRC_STEP`，深度恒定），期望值一个未改；
> 逐字节断言还能定位到具体哪一字节不一致。原始失败输出见第 8 节构建记录。

---

## 7. 电压换算与参考电压口径

- `V = code / 4095.0 * (vref_mv / 1000.0)`；OLED 用整数等价形式 `mV = code * vref_mv / 4095`
  （避免引入浮点库），与电脑端**同一个 `vref_mv`**。
- `vref_mv` 默认 **3300**，可由 SET_VREF 配为 **2500..3600**；**只改 RAM、掉电不保存**。
- **不宣称板上 3.3 V 恰好等于 3.300 V**：实际供电电压需现场测量后再校准。
- 悬空 ADC 不代表零伏；采集与 OLED 刷新率不同；采样率不等于带宽。

---

## 8. 构建命令与结果（本机实际执行）

工具：`C:\Keil_v5\UV4\UV4.exe`，编译器 `V5.06 update 5 (build 528)` / ARMCC，
设备包 `Keil.STM32F1xx_DFP.2.2.0`。

```powershell
# 在 firmware/ 目录下
powershell -ExecutionPolicy Bypass -File .\build.ps1
# 或直接：
$p = Start-Process C:\Keil_v5\UV4\UV4.exe -ArgumentList '-r','Project.uvprojx','-j0','-o','Objects\build.log' -WorkingDirectory . -Wait -PassThru
Get-Content Objects\build.log; $p.ExitCode
```

**最终结果（`-r` 全量重建，退出码 0）**：

```
Program Size: Code=14250 RO-data=1790 RW-data=112 ZI-data=14504
FromELF: creating hex file...
".\Objects\Project.axf" - 0 Error(s), 0 Warning(s).
```

- Flash（ROM）= Code + RO Data + RW Data = **16152 字节 (15.77 KB) / 64 KB**。
- RAM = RW Data + ZI Data = **14616 字节 (14.27 KB) / 20 KB**。
- 构建日志：`firmware/Objects/build.log`（完整留存）。
- **HEX 路径：`firmware/Objects/Project.hex`**（45485 字节，1013 行，Intel HEX；
  首记录 `:020000040800F2` 起始地址 0x08000000，末记录 `:00000001FF`）。
- CRC 编译期断言：`Tests/protocol_selftest.c` 编译通过 = 四组 golden 断言全部成立。

**构建过程中的失败与修正（如实记录，根因以 `fromelf -z` 实测为准）**：

1. 首轮：`Tests/protocol_selftest.c(49): error: #4: out of memory`（把 9~24 个
   `CRC_STEP` 用宏链串成单个 `#if` 表达式，ARMCC 5 预处理器表达式膨胀溢出）。
   修正：改为 **64 条分段独立 `#if`**，输入 crc 用字面常量、文档期望值不变（见第 6 节）。
   主 agent 用 `armcc` 单独编译验证过：错误版退出码 1，分段版退出码 0，四组末值
   `0x29B1 / 0x8278 / 0x7EA4 / 0xE73A` 与 `docs/protocol.md` 一致。
   证据：`outputs/keil_probe/CRC编译期断言探测.txt`。
2. 随后多轮链接失败：`L6406E: No space in execution regions with .ANY selector ...`，
   `L6407E: Sections of aggregate size 0x1dce0 / 0x1540c bytes could not fit`（129 → 112
   → 31 个错误）。把工程从全部 23 个标准库 `.c` 精简到 7 个必要驱动后**仍失败**。
   主 agent 用 `fromelf -z` 逐个统计 `Project.lnp` 里链接的 21 个 `.o`，定位**真正根因**：
   **`protocol_codec.o` 单独 Code = 112,640 字节**（其余 20 个合计约 14.5 KB）。原因是
   `Protocol_Crc16` 运行时直接展开 8 层嵌套的 `CRC_STEP`；`CRC_BIT1(c)` 是三元表达式，
   `(c)` 每层出现 3 次 → 3^8 份表达式被编译成约 110 KB 机器码，撑爆 64 KB Flash。
   **决定性修复**：运行时改为循环调用**同一个 `CRC_BIT1` 单步宏** 8 次（语义由同一宏
   保证与编译期 `CRC_STEP` 等价），该 `.o` 降到 **264 字节**；`docs/protocol.md` 第 5 节
   参考实现已同步修正并注明该坑。证据：`outputs/keil_probe/P3链接溢出根因.txt`。
   辅助优化（保留但非根因）：关闭调试/浏览信息（`DebugInformation=0` /
   `BrowseInformation=0`）、`OneElfS=0`、只编译实际用到的 7 个外设驱动 `.c`（全套
   `.c/.h` 仍保留在 `Library/`，`conf.h` 仍 include 全部头文件）。这些设置减小目标文件
   体积、不影响 HEX 内容，也不影响 CRC 编译期断言（`#if` 在预处理阶段求值）。
3. 曾尝试 `-O3`（速度优化，反而使库代码膨胀、构建变慢 2 分 35 秒，已撤销）；曾引入
   `string.h` 的 `memmove`（会链入大块标准 C 库），已改为手写 `Rx_Shift()` 字节移动；
   曾临时放大 IROM/IRAM 区做诊断，**最终工程已恢复为 `IROM 0x10000 (64 KB)` /
   `IRAM 0x5000 (20 KB)`**，可用 `firmware/Objects/Project.sct` 核对。
4. 最终用 `UV4 -r` 全量重建：`0 Error(s), 0 Warning(s)`，HEX 正常生成；
   HEX 实际占用 16,152 / 65,536 字节（24.6% Flash），RAM 14,616 / 20,480 字节（71.4%）。

**关于 AGENTS.md 的取舍**：风格参考 `C:\Users\29319\Desktop\STM32Project\AGENTS.md`（另一练习工程）
建议"不要直接编辑 .uvprojx，请在 Keil GUI 中改工程设置"。但本项目为了能在本机命令行
实际构建并产出 HEX，**必须**编辑 `.uvprojx`（设备、启动文件、分组、IncludePath、
`CreateHexFile`、宏定义、编译选项）。这一取舍已在交付报告中说明；`build.ps1` 与最终
`Project.uvprojx` 是本工程的一部分。

**最终工程关键配置**：
- Device `STM32F103C8` / PackID `Keil.STM32F1xx_DFP.2.2.0`；
  `<Cpu> IRAM(0x20000000,0x5000) IROM(0x08000000,0x10000)`（64 KB Flash / 20 KB RAM）。
- 启动文件 `.\Start\startup_stm32f10x_md.s`（中容量 MD）。
- `<Define>USE_STDPERIPH_DRIVER,STM32F10X_MD</Define>`（`STM32F10X_MD` 为中容量器件宏，
  构建必需；`USE_STDPERIPH_DRIVER` 为任务要求）。
- `<IncludePath>.\Start;.\Library;.\User;.\System;.\Hardware;.\Common</IncludePath>`。
- 分组：`Start / Library / System / User / Hardware / Common / Tests`，所有新增 .c 均已加入。
- `<CreateHexFile>1</CreateHexFile>`，输出 `.\Objects\`，Listing `.\Listings\`。

**模板遗留项澄清（审查记录 R9，避免把模板值误读为硬件事实）**：

- **`<Cpu> ... CLOCK(12000000) ... </Cpu>` 里的 `CLOCK(12000000)` 是 Keil 调试/仿真的核心
  时钟参数，不是外部晶振频率，不能当作 HSE 证据。** 工程里真正决定时钟的是
  `Start/system_stm32f10x.c` 的 `SYSCLK_FREQ_72MHz` 与 `Start/stm32f10x.h` MD 分支的
  `HSE_VALUE = 8000000`（8 MHz）。**开发板实际晶振与实际电源电压仍需用户现场确认**，
  本固件不把 8 MHz / 3.300 V 写成已实测值。
- **`<FlashDriverDll> ... STM32F10x_128.FLM ... </FlashDriverDll>` 与 `<FlashUtilSpec>` 中的
  "128" 是从参考工程模板继承的 Keil 烧录算法名**（Flash 编程算法文件名，带 128 字样），
  与 STM32F103C8 的 64 KB Flash / `IROM 0x08000000,0x10000` 配置**不矛盾**：它只影响
  Keil 点击下载时选用哪个 Flash 编程算法，**不影响编译、链接与 HEX 内容**。本机命令行
  构建（生成 HEX）不涉及该算法；**实际烧录时请用户在 Keil 的
  Options for Target -> Utilities -> Flash Download 中确认/选择正确的 64 KB 器件算法**。

---

## 9. 已知限制与未验证项

- **未上板**：未做接线、烧录、串口、采样时序、精度与带宽实测；不编造任何测量结果。
- **晶振 / 电源电压待确认**：`HSE_VALUE=8000000` 来自库头文件（MD 分支），板上实际晶振与
  实际 3.3 V 电压需现场确认；`vref_mv` 默认 3300 只是标称值，掉电不保存。
- **采样率 != 带宽**：100 kS/s 是设计目标，不证明模拟前端带宽或高频响应。
- **自检 PWM 的局限**：PA8 100 Hz、0~3.3 V 方波只用于验证接线与采集链路，**不能**证明
  高速带宽或绝对精度；使用时须经合理限流/滤波接入 PA0，且**不可与外部信号同时驱动 PA0**。
- **串口时序未实测**：发送一帧约 180 ms 期间主循环被占用，命令在帧间空隙处理；实际
  端到端时序、丢帧、重同步在真实串口上尚未验证。
- **跨实现核对的待补项**：本机无可在 PC 上执行的 x86 C 编译器，"C 编码 -> Python 解码"
  与 "Python 编码 -> C 解码" 的双向运行级核对未做；本版证据是 Python 独立实现 +
  ARMCC 编译期 `#if` 断言 + 文档 golden 三方一致。
- **IDLE / STOP 语义**：IDLE（含上电默认、STOP 之后）仍以约 200 ms 周期本地采集一帧
  **仅供 OLED 电压表、不向串口发帧**，以满足"无电脑连接独立电压表"；STOP 停止的是
  "连续向电脑发采样帧"。若需彻底冻结采样可作为后续扩展。
- 超过 3.3 V 量程、交流负半周、高压测量、分压前端、模拟带宽均不在第一版实现内。
