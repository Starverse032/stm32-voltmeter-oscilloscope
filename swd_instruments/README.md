# Task H：可选引脚与通用模式（现行规则）

本节取代后文历史说明中的固定引脚/固定授权集合；后文的 PA4、PA8、PA6、PB10 是默认接线示例。
只读预检不会写外设。已登记 profile 仍以完整 64 KiB 镜像 SHA-256 自动解锁；摘要不符时可重新选择第一项
“通用模式（未登记固件）”。通用模式先读设备 ID（0x410）、容量（64 KiB）和完整镜像，显示摘要供以后登记；
点击激活后必须在本会话对风险对话框选择“我已了解，继续”。确认不持久化，恢复/关闭后撤销。
原程序以后可能启用同一外设或引脚而相互干扰；工具能检测大部分变化并停止，但不能保证零影响。
通用模式绝不修改共享 ADCPRE；ADC 时钟超过 14 MHz 时 ADC 不可用，其他空闲功能仍可使用。
通用模式 HSE 按 8 MHz 配置假设推导，Vref 按 3300 mV，均不是现场测量。

允许集合 = 硬件候选 ∩ 未物理占用 ∩ 本次预检空闲 ∩ 登记 profile 限制（通用模式没有这一项）。
同一引脚或同一定时器不能同时承担两个功能。每个功能可选“不启用”。卡片①的“引脚分配”可展开；
冲突或忙碌候选变灰并显示原因，会话期间锁定分配。物理占用引脚由用户勾选并保存到 QSettings，
默认 PB6–PB9，两种模式均排除。每种 profile/模式分别保存选择，禁止占用的引脚不显示为可选项。
忙碌候选清单由只读预检返回；需要更改分配时先断开并恢复。

| 功能 | 默认映射候选 |
| --- | --- |
| ADC2 | PA0–PA7→CH0–7；PB0→CH8；PB1→CH9 |
| PWM输入频率计 | TIM1 CH1/PA8、CH2/PA9；TIM2 CH1/PA0、CH2/PA1；TIM3 CH1/PA6、CH2/PA7；TIM4 CH1/PB6、CH2/PB7 |
| PWM模式1输出 | TIM1 CH1–4→PA8–PA11；TIM2 CH1–4→PA0–PA3；TIM3 CH1–4→PA6/PA7/PB0/PB1；TIM4 CH1–4→PB6–PB9 |
| 通断输入上拉 | PA0–PA12；PB0–PB2；PB5–PB15；只通过原子 BSRR/BRR 修改上拉位 |

依据 [ST DS5319 Rev 20 表5](https://www.st.com/resource/en/datasheet/stm32f103t8.pdf)、
[RM0008 定时器/PWM输入与 GPIO 章节](https://www.st.com/resource/en/reference_manual/rm0008-stm32f103xx-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)，
寄存器布局和地址另由测试对照 `tests/data/stm32f10x.h`（ST 标准外设库头文件）。不进行任何 AFIO 重映射。
不提供 SWD PA13/PA14 或 JTAG PA15/PB3/PB4。PB2 标注 BOOT1，PA11/PA12 标注 USB D-/D+。
候选中的 FT 数字引脚为 PA8–PA12、PB2、PB6–PB15；非 FT 为 PA0–PA7、PB0/PB1/PB5。
ADC 一律仅限 0–3.3 V。FT 仅表示数据手册规定条件下数字输入耐 5 V，不是输出 5 V；
非 FT 频率计/通断输入禁止直接连接 5 V，HC-SR04 Echo 等信号需要分压/电平转换。
FT 耐压条件要求关闭内部上下拉（数据手册表9/表36注释）。通断会启用内置上拉，因此即使 FT 引脚也
只接干接点/GND，禁止外加5V；FT 标签不是上拉模式的5V耐压承诺。

登记限制来自已有 `resources.original_uses`、原 free 标志语义及既有 `candidate.json` 的 `uses`，本任务没有重新审查工程源码：

| profile | 已知占用定时器 | 已知占用引脚（所有行另加 PB8/PB9 OLED） |
| --- | --- | --- |
| servo-reference | TIM2 | PA0/PA1 |
| air-harp | TIM1/TIM3 | PA6/PA8/PB12 |
| lazy-rescue | TIM2/TIM3 | PA0/PA1 |
| light-master | TIM2 | PA0/PA1/PA2/PB1/PB11 |
| oled-awakening | 无 | 无 |
| road-blocked | TIM2 | PA0/PA1/PB0/PB1 |
| running-led | TIM2 | PA0–PA7/PB12/PB15 |
| wind-motor | TIM2 | PA2/PA4/PA5/PB1/PB11 |

这些集合固化在 `TRUSTED_PROFILES[*]['restrictions']`，JSON 不能放宽；旧 PB6/PB7/TIM4 的保守 free=false
不等于固件实际使用，板上接线由物理占用列表控制。共享 ADCPRE 仍受原登记 adcpre_free 限制。

传输层字段白名单是所有候选的窄掩码并集，不能单独授予写权限。控制器激活时另外提交实际可用选择的
`(address, mask)` 集合给 `activate_selection()`；`write_masked()` 同时检查固件租约、选择字段和运行状态。
通用模式 `authorize_general_mode(hash, confirmed=True)` 仅接受当前传输会话亲自完整读取并计算的摘要，
不接受调用者任意提供的摘要；不授权 ADCPRE。恢复/关闭撤销选择与租约。事务保留原值、检查所有权并精确恢复；
命令寄存器不读，状态标志按 rc_w0 语义只清本会话之前为0的位；不开中断/DMA，不写 ADC1、AFIO 或系统时钟。

目前只有 servo profile 的 PA4 ADC、PA8 PWM 路径已有实物验证；本次新增候选只经过假目标测试，未接板验证。


---

# SWD 仪器实验工具

独立于原工程的电脑端实验版本，包含**模拟三功能**、**只读 SWD 诊断**，以及本轮补齐的**受限真实 ADC2/TIM1 控制器**。

> 2026-11-10已完成实物接地采样和0.1Hz PWM回环的基础功能检查，结果摘要见仓库根目录 README 的“实测验证”一节。用户授权后已备份并覆盖板上未知程序，部署参考工程；随后仪器测试没有再次烧录或复位，测试前后完整Flash摘要一致。源工程未改。**绝对精度、带宽及原程序时序无干扰仍未验收。**

## 功能状态

| 功能 | 状态 |
| --- | --- |
| 模拟电压表/曲线/信号发生器 | 可用，逐点模拟时间戳，明确标注非真实测量 |
| ST-Link 只读诊断 | 可用代码路径；指定完整序列号，attach 不 halt/reset |
| 真实 ADC2/PA4 低压采样 | 接地与慢速回环初验通过；需已审查 profile + 预检通过；默认SWD时钟950 kHz下实测约14点/秒（100 kHz时约3点/秒） |
| 真实 TIM1_CH1/PA8 方波/PWM | 0.1Hz/50%回环初验通过；板上逻辑电平，电平和频率精度未独立核验 |
| 电流、电阻 | 没有相应前端电路，**不支持**，界面不提供假数据 |
| 任意幅值正弦/三角输出 | 没有 DAC/滤波电路，**不支持**；PWM 不冒充模拟波形 |
| 任意地址读写控制台、`--force` | **不提供** |

## 脉冲与通断输入

`measure_pulse(timeout_s=1.5, cancel=None)` 使用 profile 选定的 PA6 / TIM3_CH1，范围 0.5 Hz～100 kHz，返回周期、高电平时间、频率、占空比与 tick。自动选择预分频，比较两个新鲜捕获，无中断和 DMA。1 Hz 可选 8 秒，0.5 Hz 可选 15 秒超时。

**PA6 不耐受 5 V，仅允许 0–3.3 V 逻辑信号。HC-SR04 Echo 等 5 V 信号禁止直接连接。** ADC 的 PA4/PA7 同样只允许 0–3.3 V。测量信号与开发板共地。

`check_continuity()` 使用 PB10 内部上拉，五次去抖读取。全部低电平表示接地，全部高电平表示开路，混合返回 `closed=None`。PB10 耐受 5 V，通断功能仅检测与 GND 的连接，接入无外部供电的无源待测点。

主机已验证面包板 OLED 四针落在 PB6/PB7/PB8/PB9 行，分别为 GND、3.3 V VCC、SCL、SDA。TIM4 的四通道恰好使用 PB6–PB9。所有登记配置禁止 PB6/PB7 仪器输入，并将 `tim4_free/pb6_free/pb7_free` 设为 false。跳线进入供电行可能与 OLED 电源短接。

资源选择由 `meter_input`（`PA6`、`PB7`、null）与 `continuity_input`（`PB10`、`PB6`、null）决定。PA6 要求 `tim3_free` 和 `pa6_free`，PB7 要求 `tim4_free` 和 `pb7_free`，通断要求对应的 `pb10_free` 或 `pb6_free`。JSON 必须与可信登记逐字段一致。TIM4/PB7、PB6 分支保留并有离线测试，当前八个登记配置均选择新引脚或禁用。

## 已登记项目与引脚

运行时预检会进一步检查资源占用，镜像摘要维持原值。PA6/PB10 新路径通过离线替身验证，实物验收待主机执行。

| 原工程 | profile | ADC 输入 | PWM | 脉冲计 | 通断 |
| --- | --- | --- | --- | --- | --- |
| 来个舵机摇一摇！ | servo_reference.json | PA4 | PA8 | PA6 | PB10 |
| 懒人救星！ | lazy-rescue.json | PA4 | PA8 | 不可用，TIM3 被计时器占用 | PB10 |
| 此路不通！ | road-blocked.json | PA4 | PA8 | PA6 | PB10 |
| 点灯大师！ | light-master.json | PA4 | PA8 | PA6 | PB10 |
| 空气竖琴！ | air-harp.json | PA4 | 不可用，PA8 被 Echo 占用 | 不可用，TIM3/PA6 被蜂鸣器占用 | PB10 |
| 觉醒吧，oled！ | oled-awakening.json | PA4 | PA8 | PA6 | PB10 |
| 进化：流水灯！ | running-led.json | 不可用，PA0–PA7 被 LED 宏覆盖 | PA8 | 不可用，PA6 被 LED 宏覆盖 | PB10 |
| 面对疾风吧！ | wind-motor.json | PA7 | PA8 | PA6 | PB10 |

逐工程复查证据（文件范围、SHA-256、宏和 GPIO/定时器引用行号）保存在作者本地，未随仓库发布。懒人救星 `System/Timer.c:12-26` 使用 TIM3，空气竖琴 `Hardware/Tone.c:22-66` 使用 TIM3/PA6，流水灯 `Hardware/LED.h:4` 与 `Hardware/LED.c:10-31` 定义并使用 PA0–PA7。八个工程均无 PB10 使用。

### 寄存器与白名单

TIM3 基址 `0x40000400`，CR1/SMCR/DIER/SR/EGR/CCMR1/CCMR2/CCER/PSC/ARR/CCR1/CCR2 偏移依次为 `00/08/0C/10/14/18/1C/20/28/2C/34/38`。配置 CCMR1=`0x201`、SMCR=`0x54`、CCER=`0x31`，CCR1 为周期、CCR2 为高电平时间。TIM3 时钟按 APB1 分频倍频规则推导。配置已对照 `tests/data/stm32f10x.h`（ST 标准外设库头文件） 的 TIM_TypeDef、TIM3_BASE、TIM3EN、TIM3_REMAP 和 TIM 位定义，以及 RM0008 的 PWM input mode 与寄存器章节。[RM0008](https://www.st.com/resource/en/reference_manual/rm0008-stm32f101xx-stm32f102xx-stm32f103xx-advanced-arm-based-32-bit-mcus-stmicroelectronics.pdf)

- TIM3 写掩码为 CR1 `0x5`、SMCR `0x77`、CCMR1 `0x303`、CCER `0x33`、PSC/ARR `0xFFFF`、EGR `0x1`、SR `0x65F`。DIER、CCMR2、CCR1/2 仅读取，DIER 始终为 0。
- RCC_APB1ENR 新增 TIM3EN bit1，GPIOA_CRL 新增 PA6 nibble `0x0F000000`。原 TIM4 与 ADC 掩码保留。
- GPIOB_CRH 仅允许 PB10 nibble `0x00000F00`，配置为 `0x8`。BSRR/BRR 新增 bit10 `0x400`，保留旧 PB6 bit6。GPIOB_ODR 仅读取，上拉和恢复使用原子命令。
- 预检要求 TIM3 时钟关闭，CR1/SMCR/DIER/CCMR1/CCMR2/CCER 为零，AFIO_MAPR[11:10]=00，PA6 nibble 为 0 或 4。运行期保护共享环境，计时器量测结束停止并在事务恢复时还原配置。
- SR 按 rc_w0 处理，仅清除会话前为零的标志。命令寄存器禁止读取和恢复重放，CCR 捕获使用 CNT × tick。

`PreflightReport.ok` 要求设备 ID、Flash 容量、完整 64 KiB 镜像 SHA-256、向量表、目标运行状态、时钟推导和总线范围检查通过，并且至少有一个功能可用。本地产物校验失败也阻止激活。`issues` 保存这些阻断问题。外设占用、引脚占用、未授权及 ADC 超频等原因写入 `resources.reasons`，同时以中文写入 `notes`。`activate()` 只激活可用功能，全部可用功能完成激活才返回成功。

### 报告与 profile 字段

`PreflightReport.resources` 的四个必有布尔键为 `adc`、`pwm`、`meter`、`continuity`。`reasons` 始终是 `dict[str, str]`，只包含不可用功能，键取上述四项，值是中文原因。`adcpre_divider` 始终为 `int | None`，计划调整时为 2、4、6、8 中的一项，其余情况为 null。`clocks` 保留预检实际读到的时钟，激活报告的 `clocks` 为调整后的时钟。

完成寄存器读取时，`resources` 还包含以下整数快照键。提前异常时这些快照可能缺失，四项可用性均为 false。

- `rcc_apb2enr`、`adc1_cr1`、`adc1_cr2`、`adc2_cr1`、`adc2_cr2`、`adc2_sr`
- `pa4_nibble`、`pa8_nibble`、`gpioa_crl`、`gpioa_crh`、`rcc_cfgr`
- `tim1_cr1`、`tim1_dier`、`tim1_ccmr1`、`tim1_ccmr2`、`tim1_ccer`、`tim1_bdtr`、`afio_mapr`
- `rcc_apb1enr`、`gpiob_crl`、`gpiob_crh`
- 所选 TIM3/TIM4 的整数地址键（含 SR），例如 `TIM3_CR1`、`TIM4_SMCR`、`TIM4_DIER`、`TIM4_CCMR1`、`TIM4_CCMR2`、`TIM4_CCER`，值均为整数寄存器快照

profile 顶层可选 `display_name`，要求去除首尾空白后非空，原字符串不超过 64 字符，加载后暴露为 `ReviewedProfile.display_name` 并纳入 `summary()`，缺省回退到 `profile_id`。`ReviewedProfile.adc_input` 为 `str | None`。资源可选 `adcpre_free`，缺省 false，所有授权字段必须与 `TRUSTED_PROFILES` 完全一致。

### 共享 ADC 预分频所有权

仅源码审查确认 ADC1 和 ADC2 均未使用的工程可登记 `adcpre_free: true`。当 ADC 已授权且时钟高于 14 MHz 时，预检规划满足上限的最小分频，72 MHz PCLK2 对应 /6、12 MHz ADC。若运行期 ADC1 时钟或 ADON 已开启，ADC 功能禁用。流水灯没有 ADC 引脚，因此即使有分频授权也不调整 ADCPRE。

激活在事务中仅写 `RCC_CFGR[15:14]`，白名单新增掩码严格为 `0x0000C000`，传输层也要求当前 profile 有分频授权。其余 CFGR 位受保护且每次读改写保留。事务记录原值，恢复时先确认环境及所有权，再恢复分频。工具拥有的 ADCPRE 由事务检查，CFGR 其余位继续由环境检查保护。原程序接管字段后恢复会报告失败并跳过写入。

舵机、懒人救星、此路不通使用 ADC1，始终禁止修改共享分频。它们若读到 ADC 超频，仅禁用 ADC，PWM、测频、通断按各自检查结果继续。采样时间计算与 SMPR 配置继续使用原算法。

## 启动

当前电脑的项目虚拟环境已安装依赖。双击本目录 `启动SWD仪器.bat` 即可启动；启动器实际位置为 `swd_instruments/启动SWD仪器.bat`。

换电脑时，先安装 Python 3.13，双击同目录 `安装SWD依赖.bat`（包含 pyOCD 0.46.1），再启动。也可在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m swd_instruments
```

启动时不枚举 USB、不自动连接设备。默认停留在模拟模式。

## 界面布局（2026-11-10 改版）

- **顶栏**：右上角的徽标显示当前数据来源，蓝色为模拟、绿色为真实测量、黄色为历史数据、红色为输出状态未知；旁边是“导出当前帧 CSV”。
- **左侧面板**：两个标签页。“模拟（离线）”放模拟输入和模拟信号发生器；“真实 SWD（接板）”按 ① 连接·预检·激活 → ② 采样 → ③ PWM 输出 的顺序排列，每一步右上角有状态标签，“④ 断开并恢复”固定在面板底部。
- **右侧**：电压读数与最小/最大/峰峰值/帧跨度、采样曲线、底部的“实验日志 / ST-Link 只读诊断 / 说明与限制”同屏显示，不需要来回切换页面。真实帧在曲线上画出每个实际采样点。
- 采样卡片会按所选 SWD 时钟档在 2026-11-10 实测的秒/点（默认档约 0.072 秒/点）估算每帧等待时间，采样进行中显示已用时间；这只是估算，不是性能保证。
- “① 连接·预检·激活”里可以选择 SWD 时钟档位，会话打开后锁定。详见下文“SWD 时钟”。
- 用启动器或 `python -m swd_instruments` 启动时，会回填上次**手动输入**的 ST-Link 序列号（保存在 `%APPDATA%\swd_instruments\gui.ini`）。仍然不枚举 USB、不自动连接，必须点击“只读预检”才会打开会话。
- 真实 PWM 频率的界面默认值为 1 Hz（原为 100 Hz），与“回环先用 1 Hz 或更慢”的建议一致。

界面截图见仓库 `docs/screenshots/`（测试替身数据，不是硬件验收）。

## 不接板先试用

1. 在左侧“模拟（离线）”页点击“单次”，右侧立即显示读数、最小/最大/峰峰值和曲线。
2. 在曲线上滚轮缩放、拖动平移，点击“复位视图”恢复。
3. 点击“运行模拟”连续更新；“暂停”冻结当前帧，“单次”只替换一帧。
4. 在“模拟信号发生器”设置频率和占空比，点击“开启 / 更新模拟PWM”；信号场景会切为“PWM回环”。
5. 点击顶栏“导出当前帧 CSV”，保存时间戳、ADC码、电压、来源和参考电压。断开后仍能导出历史帧；模拟数据始终标注模拟来源。

## 将来接板后的操作顺序

这是待实物验证的操作步骤。先保留原程序和原接线，不运行下载、擦除或调试启动命令。

1. 连接 ST-Link 的 SWDIO→PA13、SWCLK→PA14、GND→GND；参考电压连接按探头型号要求。开发板按原方式供电，避免多路电源互相回灌；关闭 Keil/VS Code 等其他调试会话。
2. 测电压：被测低压信号→PA4，被测地→板上GND，输入限定在板上GND～VDDA范围（本配置约0～3.3V）。PA0、PA1、PB8、PB9维持原用途。PA4不能接负电压、高压或市电。
3. 左侧“真实 SWD（接板）”页填写完整 ST-Link 序列号，选择 `servo_reference.json`，点击“只读预检”。序列号可从 ST-Link 外壳或现有官方工具的设备列表读取，不要点击连接/下载来强行试探。预检结果写在右下“实验日志”。
4. **只有身份与全局时钟检查通过且至少一项功能可用**（状态标签显示“预检通过”），才点击“激活ADC2+TIM1”。未知原程序会被拒绝；不要改 JSON 或烧录附带 HEX 来绕过检查。若不是本工具审查的原工程，需要另做源码审查和镜像登记。
5. 在“② 采样”点击“单次采样”或“连续采样”；右侧读数和曲线直接更新。默认64点、间隔0.01秒，实际速率受SWD通信影响（默认时钟下实测约14点/秒，一帧约5秒）。“暂停”/“取消”停止更新，不能用它代替关闭PWM。
6. 需要方波时，将 PA8 作为3.3V逻辑PWM输出、GND作为参考，在“③ PWM 输出”设置频率和占空比，点击“开启 / 更新输出”。它只能驱动合适的高阻输入，不能直接带电机/大电流负载。需要回环可在确认PA8没有其他连接后将PA8接PA4，先用1Hz或更慢的信号观察变化。
7. 结束时先“关闭输出”，再点面板底部的“④ 断开并恢复”。查看恢复结果；遇到“输出状态未知”时，PA8可能仍在输出，需要检查外部连接和实际引脚状态。

没有连接开发板时不要执行上述实物步骤；模拟功能可独立使用。

## SWD 时钟

采样慢的原因不是 ADC，而是每个采样点前后各做一轮保护配置复查：控制器层每点约 72 次传输访问，其中约 66 次是寄存器读取。提高 SWD 时钟**不减少任何检查**，只缩短每次访问的线上时间。

2026-11-10 在实物上（PA4 接 GND、PA8 悬空、PWM 全程关闭）按档位从低到高测得：

| SWD 时钟 | 采样速率 | 只读预检耗时（含完整 Flash 摘要） |
| --- | --- | --- |
| 100 kHz | 约 2.9 点/秒 | 8.6 秒 |
| 240 kHz | 约 6.0 点/秒 | 3.5 秒 |
| 480 kHz | 约 9.2 点/秒 | 1.9 秒 |
| 950 kHz（默认） | 约 13.9 点/秒 | 1.1 秒 |
| 1.8 MHz | 约 17.5 点/秒 | 0.7 秒 |

- 950 kHz 和 1.8 MHz 各重复 10 轮，全部干净完成：每轮两次完整 64KiB Flash 摘要都与基线一致，恢复无错误，诊断寄存器前后无差异，目标保持运行。
- 默认取 950 kHz，比通过的最高档低一档，为面包板接线留余量。时钟只能取 `transport.ALLOWED_SWD_HZ` 登记的档位，不接受任意数值；4.6 MHz 未登记、未测试。
- 这是这一套接线、这一次上电的结果。换线、换探头或加长杜邦线后如果预检或采样出现通信错误，先“断开并恢复”，在界面上改选更低的档位再试。
- 速率是主机时间戳统计，不是独立仪表测量；点间隔仍然非等间隔。未验证更高时钟对 ADC 读数噪声或原程序时序的影响：接地读数的最大 ADC 码在 100 kHz～950 kHz 为 2～6，在 1.8 MHz 为 9，样本太少，不能下结论。

原始测速数据与脚本保存在作者本地，未随仓库发布；测速脚本会连接实物并激活外设。

## 真实 SWD 实验的硬性前置条件

1. 完整 ST-Link 序列号（拒绝自动选第一块板）。
2. `profiles/` 内八份运行 profile 见下表；`servo_reference.json` 是舵机参考配置；`reference_artifact.json` 是独立重建的来源记录，不是运行profile；`reference_original.hex` 只用于校验交付物，不用于烧录。
3. 板子按原方式供电，SWDIO/SWCLK/GND（及必要参考电压）连接正确；退出其他调试器。
4. 预检先通过身份与全局时钟检查，再独立计算各功能可用性。检查包括：
   - IDCODE `DEV_ID=0x410` 与 profile 一致，Flash 容量一致；
   - 按 profile 的 `flash_start/image_size` **只读**原固件区间，SHA-256 与 `image_sha256` 一致，向量表合理；
   - RCC 推导成功，系统及总线时钟在审查范围；ADC 分频规则见下文；
   - ADC2 时钟/ADON/SCAN 空闲，ADC1/ADC2 独立模式（只读 `ADC1_CR1`，**不读 `ADC1_DR`**）；
   - TIM1 时钟/CEN/其他通道/互补输出/DIER/BDTR 非 MOE 字段空闲，AFIO 未重映射 TIM1；
   - PA4、PA8 nibble 为 0 或 4（空闲候选），不是已配置输出。

身份或全局时钟检查失败会在外设写入之前拒绝所有功能。外设或引脚忙会禁用对应功能，其他已授权且空闲的功能可以激活。设备 ID 和“ADC2 当前没开”不能证明固件身份或将来不会使用资源；身份由只读镜像摘要绑定。

### profile 规范（`servo_reference.json`）

Codex 独占生成；本工具只读。加载器要求：

```json
{
  "schema_version": 1,
  "profile_id": "servo-reference-armcc5-v1",
  "device": {"dev_id": 1040, "flash_size_kb": 64},
  "firmware": {
    "flash_start": 134217728,
    "image_size": 65536,
    "image_sha256": "2f5f8ad3e43078cc53b1e356c37324bdf7cc59e03416529e13896a38a64bc988",
    "artifact_file": "reference_original.hex",
    "artifact_sha256": "a674b4cf911e8e90e61d4f654936373d826895a36b40459ec5342106eba985c1"
  },
  "clock": {"hse_hz": 8000000, "hse_source": "reviewed-assumption"},
  "vref_mv": 3300,
  "resources": {
    "adc2_free": true, "tim1_ch1_free": true,
    "pa4_free": true, "pa8_free": true,
    "tim4_free": false, "pb6_free": false, "pb7_free": false,
    "pa7_free": true, "adcpre_free": false, "adc_input": "PA4",
    "tim3_free": true, "pa6_free": true, "pb10_free": true,
    "meter_input": "PA6", "continuity_input": "PB10",
    "original_uses": ["ADC1/PA0", "TIM2_CH2/PA1", "OLED PB8/PB9"]
  },
  "review": {"status": "reviewed", "reviewed_by": "Codex", "reviewed_at": "2026-11-10"},
  "notes": []
}
```

还必须与 `profiles.py` 内登记的已审查固件一致；JSON自称 `reviewed` 或 `approved=true` 不能新增信任。空摘要、部分镜像、资源与可信登记不符、缺审查人均拒绝。完整64KiB镜像按HEX未覆盖字节为0xFF重建，板上未使用区域不是0xFF也会导致拒绝。

`hse_hz` 是**配置假设**，不是现场晶振测量。这里绑定的是 “来个舵机摇一摇！”练习工程 的原工程副本重建结果，**不是板上已烧录证明**；其他工程需要独立审查，不能靠烧录/擦除让摘要匹配。

## 真实路径的行为

### 激活

通过预检后，事务按字段白名单写入：

- `RCC_APB2ENR` 仅 GPIOA/ADC2/TIM1 时钟位；
- `GPIOA_CRL` 的 PA4/PA7 模拟输入与 PA6 脉冲输入分别按授权配置；
- `GPIOA_CRH` 仅 PA8 nibble → AF 推挽；
- ADC2：`SCAN=0`、`所选通道 SMP=239.5cyc`、`L=0`、`SQ1=4或7`、单次软件触发、`ADON`、`RSTCAL`、`CAL`；
- TIM1_CH1：PWM1、CCR 预装载、PSC/ARR/CCR1，**CC1E/MOE/CEN 保持 0**；不启用互补输出、CH2/3/4、中断。

每步有截止时间（校准 50ms、EOC 10ms、轮询 0.2ms），并检查目标是否停止。失联/超时不会无限等待。

### 采样

`sample()` 每次写 `SWSTART`，轮询 `ADC2_SR.EOC`，只在取得 ADC2 数据所有权后读 `ADC2_DR`；普通诊断/扫描仍禁止读 ADC2_DR。每个点记录：

- 触发前后主机 `monotonic` 时间；
- 原始 12 位码、按 `vref_mv` 换算的电压；
- 时间戳取前后中点，`time_uncertainty_s` 为半宽（不小于轮询量子的一半）。

这是**主机轮询**，不是硬件触发/等间隔示波器；帧内时间戳是实际主机时间，不生成假的 10k/100kS/s 轴。默认低速、64 点、可取消；高频输入有混叠风险。

### TIM1 输出

频率/占空比按RCC寄存器和HSE配置假设推导的 TIM1 时钟量化，显示**配置量化值，不是实测频率**。0% 时 CCR=0；100% 时 CCR=ARR+1（≤65535，绝不写 65536）。输出使能顺序为 CC1E → MOE → CEN，并读回确认。

断线、目标停止或写失败后，输出可能继续。只有读回确认 CC1E/MOE/CEN 都为 0 才显示“已关闭”；否则显示“输出状态未知，可能仍在输出”。关闭工具**不会**用 MCU 复位来恢复。

### 事务与恢复

- 首次写入保存本工具拥有的字段原值；恢复时只写这些掩码。
- 恢复前读回检查字段是否仍等于本工具写入值；若原程序已改动，报告“所有权已变化”，不覆盖。
- 若受保护的时钟、GPIO、ADC1或其他TIM1通道变化，恢复跳过写入，避免关闭已被其他用途接管的时钟/计数器；此时输出保持未知。
- TIM1 EGR是命令寄存器，不读取它，也不会在恢复时再次触发UG事件。
- 其他位/其他寄存器的运行期变化保留。
- 初始错误与恢复错误分别列出；恢复失败不隐藏。
- 恢复失败时输出状态可能未知，需人工断电/重新确认，不能假定安全。

## 明确未验证项

- 真实 ADC2 上电/校准/转换、PA4 输入阻抗与负载影响；
- 真实 TIM1/PA8 波形、电平、频率精度、占空比精度；
- attach 对原程序的时序影响、总线争用、读改写竞争窗口；
- 原固件在长时间运行中是否动态启用 ADC2/TIM1/PA4/PA8；
- 无干扰运行、测量精度、带宽、采样率验收；
- Windows 上 pyOCD 0.46.1 与具体 ST-Link 固件的实际兼容性。

“不能完全保证零影响”是事实，不是拒绝实现的理由；实现采取保守、可检查、可恢复的路径，并在未知/忙/时钟不兼容时拒绝。

## 软件验证

在项目根目录：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s swd_instruments\tests -v
```

测试使用 fake transport 模拟 F1 自清位、EOC、超时、断线、部分初始化失败和回滚；不枚举 USB、不连接实物。GUI 测试会创建 Qt 窗口。测试通过不等于硬件验收。

## 文件

- `contracts.py`：驱动/GUI 冻结接口与取消令牌；
- `core.py`：寄存器白名单、时钟推导、Frame/CSV、PWM 规划、模拟器、只读诊断；
- `profiles.py`：profile 严格加载与本地产物摘要；
- `transport.py`：pyOCD attach、只读读取、受限 `write_masked`；
- `hardware.py`：真实 ADC2/TIM1 控制器、事务/恢复/超时；
- `gui.py`：模拟、真实实验页、后台 worker、CSV；
- `theme.py`：界面调色板与样式表，只影响外观；
- `tests/`：契约、驱动 mock、GUI 测试。

## 协作说明

本轮已实际调用WorkBuddy并取得代码文件，但任务达到900秒上限，未取得最终JSON报告，因此不能确认本轮最终模型元数据或内部子agent数量。此前连通性重试轮的主模型元数据match不替代本轮证明。后续整合、修复、测试、教程由Codex完成，没有继续消耗WorkBuddy额度。软件测试和截图不等于WorkBuddy验收或硬件验收。

## 原理依据

- [ST RM0008](https://www.st.com/resource/en/reference_manual/rm0008-stm32f103xx-advanced-armbased-32bit-mcus-stmicroelectronics.pdf)
- [pyOCD 连接选项](https://pyocd.io/docs/options.html)
- 原参考工程 `Hardware/AD.c`、`PWM.c`、`User/main.c`
- 当前工程 `tests/data/stm32f10x.h`（ST 标准外设库头文件） 的 ADC/TIM/RCC 位定义
