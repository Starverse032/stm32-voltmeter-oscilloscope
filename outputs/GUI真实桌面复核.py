#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主 agent 独立复核：在真实 Windows 桌面启动仪器窗口、跑模拟源并截图。

不依赖电脑端 agent 的冒烟脚本，独立验证「真实桌面 + 中文可读 + 波形可见」。
用法（项目根）：.venv\\Scripts\\python.exe outputs\\GUI真实桌面复核.py
输出：outputs/screenshots/主agent复核_电压表.png、主agent复核_示波器.png
      outputs/GUI真实桌面复核.txt
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "desktop"))

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402

OUT_DIR = ROOT / "outputs" / "screenshots"
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG = ROOT / "outputs" / "GUI真实桌面复核.txt"


def main() -> int:
    lines: list[str] = [
        "主 agent 独立 GUI 真实桌面复核",
        f"时间: {datetime.now().isoformat(timespec='seconds')}",
        f"解释器: {sys.executable}",
        "",
    ]
    # 明确不设置 QT_QPA_PLATFORM，使用真实 windows 平台
    os.environ.pop("QT_QPA_PLATFORM", None)
    app = QApplication([])
    from main_window import MainWindow  # noqa: E402

    lines.append(f"Qt platformName: {app.platformName()}")
    lines.append(f"主屏尺寸: {app.primaryScreen().size() if app.primaryScreen() else 'None'}")
    lines.append(f"是否 offscreen: {app.platformName().lower() == 'offscreen'}")

    w = MainWindow()
    w.resize(1280, 860)
    w.show()

    shots: dict[str, bool] = {}

    def shot_voltmeter() -> None:
        try:
            w.tabs.setCurrentIndex(0) if hasattr(w, "tabs") else None
            shots["主agent复核_电压表.png"] = w.grab().save(str(OUT_DIR / "主agent复核_电压表.png"))
        except Exception:
            lines.append("电压表截图异常: " + traceback.format_exc())

    def shot_scope() -> None:
        try:
            if hasattr(w, "tabs"):
                w.tabs.setCurrentIndex(1)
            shots["主agent复核_示波器.png"] = w.grab().save(str(OUT_DIR / "主agent复核_示波器.png"))
        except Exception:
            lines.append("示波器截图异常: " + traceback.format_exc())

    def start_sim() -> None:
        try:
            w.start_simulation()
            lines.append("start_simulation() 调用成功")
        except Exception:
            lines.append("start_simulation 异常: " + traceback.format_exc())

    QTimer.singleShot(100, start_sim)
    QTimer.singleShot(900, shot_voltmeter)
    QTimer.singleShot(1500, shot_scope)
    QTimer.singleShot(1900, app.quit)
    rc = app.exec()
    lines.append(f"app.exec() 退出码: {rc}")
    for name, ok in shots.items():
        p = OUT_DIR / name
        lines.append(f"截图 {name}: save={ok}, 大小={p.stat().st_size if p.exists() else 0} B")

    # 关闭窗口，验证线程释放
    try:
        w.close()
        lines.append("窗口 close() 已调用，无异常")
    except Exception:
        lines.append("关闭异常: " + traceback.format_exc())

    text = "\n".join(lines) + "\n"
    LOG.write_text(text, encoding="utf-8")
    print(text)
    return 0 if rc == 0 and all(shots.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
