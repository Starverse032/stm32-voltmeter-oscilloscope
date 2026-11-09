# 第三方组件与许可说明

本项目为教学用途的第一版最小实现。以下列出随交付物一起提供或由使用者自行安装的第三方组件，**请以各组件官方发布的 LICENSE 文本为准**；本文件只做来源索引，不替代许可条款，也不构成法律意见。

## 1. 随源码一起提供的组件

| 组件 | 位置 | 来源与说明 |
| --- | --- | --- |
| STM32F10x_StdPeriph_Lib V3.5.0 | `firmware/Start/`、`firmware/Library/`、`firmware/User/stm32f10x_conf.h`、`stm32f10x_it.*` | STMicroelectronics 标准外设库。每个 `.c`/`.h` 文件头都保留了原始版权与免责声明，**未删改、请继续保留**。工程只把实际用到的 7 个 `.c` 加入 Keil 分组，其余 `.c/.h` 仍随源码附带供查阅。 |
| OLED 软件 I2C 驱动 | `firmware/Hardware/OLED.c/.h/OLED_Font.h` | 来自用户「电协笔试」练习工程（`C:\Users\29319\Desktop\电协笔试\来个舵机摇一摇！`），按江协科技 STM32 入门教程风格编写；本交付只做只读复制，原工程未被修改。 |
| 启动文件 / CMSIS core_cm3 | `firmware/Start/startup_stm32f10x_md.s`、`core_cm3.*` | 随 ST 标准外设库与 Keil 设备包提供，保留原文件头声明。 |
| Keil 生成的分散加载文件 | `firmware/Objects/Project.sct` | uVision 自动生成，仅作构建证据。 |

## 2. 由使用者通过 `desktop/requirements.txt` 安装（**不打进交付包**）

| 组件 | 用途 | 许可（以官方为准） |
| --- | --- | --- |
| PySide6-Essentials（Qt for Python） | GUI 框架 | LGPLv3 / GPLv3（Qt 模块各有差异，商用前请核对） |
| pyserial | 串口枚举与读写 | BSD-3-Clause |
| pyqtgraph | 波形绘制 | MIT |
| numpy | pyqtgraph 依赖、数值计算 | BSD-3-Clause |
| Python 3.10+（建议 3.13） | 运行环境 | PSF License |

依赖装在项目内 `.venv/`，**不要求全局安装**；`.venv/`、`.git/`、原项目视频、Keil 个人界面配置均已从交付 ZIP 中排除。

## 3. 本项目自写部分

`firmware/Common/`、`firmware/Hardware/ADC_Scope.*`、`Serial_Protocol.*`、`OLED_Voltmeter.*`、`SelfTest_PWM.*`、`firmware/Tests/`、`firmware/User/main.c`、`desktop/*.py`、`docs/`、`outputs/` 中的脚本与文档为本项目自写，按教学用途提供。

## 4. 注意

- 交付 ZIP 不包含任何原笔试工程的视频（`.mp4`）、旧构建中间产物或私有配置。
- 若要把本项目用于教学之外的分发，请先逐一核对上表各组件的官方许可义务（尤其是 Qt/PySide6 的动态链接与署名要求）。
