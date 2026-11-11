# 第三方组件与许可说明

以下列出本仓库随附或由使用者自行安装的第三方组件。**请以各组件官方发布的许可文本为准**；本文件只做来源索引，不替代许可条款，也不构成法律意见。

## 随仓库提供

| 组件 | 位置 | 说明 |
| --- | --- | --- |
| STM32F10x 标准外设库 V3.5.0 头文件 `stm32f10x.h` | `swd_instruments/tests/data/` | STMicroelectronics 提供，仅供测试核对寄存器地址和位定义。文件头保留原始版权与免责声明，未删改。 |
| 课程练习工程的编译产物（`.hex`） | `swd_instruments/profiles/` | 由作者自己的练习工程编译得到，用于核对板上固件摘要。这些固件链接了 ST 标准外设库与 CMSIS 启动代码，相关部分的版权归 STMicroelectronics / Arm 所有。 |

## 由使用者通过 `swd_instruments/requirements.txt` 安装

| 组件 | 用途 | 许可（以官方为准） |
| --- | --- | --- |
| pyOCD | 通过 ST-Link 访问 SWD | Apache-2.0 |
| PySide6-Essentials（Qt for Python） | 图形界面 | LGPLv3 / GPLv3（各 Qt 模块不同，商用前请核对） |
| pyqtgraph | 曲线绘制 | MIT |
| NumPy | pyqtgraph 依赖 | BSD-3-Clause |
| pyserial | 串口调试页 | BSD-3-Clause |
| Python 3.13 | 运行环境 | PSF License |

依赖安装在仓库内的 `.venv/`，不要求全局安装，`.venv/` 不进入版本库。
