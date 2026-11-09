# -*- coding: utf-8 -*-
"""STM32 电压表 / 示波器 电脑端入口。

启动（项目根目录）：
    .venv\\Scripts\\python.exe desktop\\main.py
无桌面会话时：
    .venv\\Scripts\\python.exe desktop\\main.py --offscreen
    （等价于设置环境变量 QT_QPA_PLATFORM=offscreen）

只使用项目内 .venv，不要求全局安装任何依赖。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 显式指定 PyQtGraph 使用 PySide6 绑定，避免自动探测时选错 Qt 绑定
# （必须在 import pyqtgraph / main_window 之前设置）
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")

# 必须在创建 QApplication（import PySide6 后创建）之前设置平台插件
if "--offscreen" in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.argv.remove("--offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from main_window import MainWindow, ensure_offscreen_fonts  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    ensure_offscreen_fonts()   # 仅 offscreen 生效；真实桌面不受影响
    app.setApplicationName("STM32 电压表与示波器")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
