#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主 agent 最终整合验证：把全部可在本机执行的检查跑一遍，汇总到 outputs/验证日志.txt。

用法（项目根）：.venv\\Scripts\\python.exe outputs\\最终验证.py
只做只读检查与运行既有测试，不修改任何被测源码。
退出码 0 = 所有检查通过；非 0 = 有失败/未核对项（逐项列出）。
"""
from __future__ import annotations

import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
LOG = ROOT / "outputs" / "验证日志.txt"

STEPS = [
    ("协议跨实现核对", [PY, "outputs/协议交叉核对.py"]),
    ("协议状态机整合复核", [PY, "outputs/协议状态机复核.py"]),
    ("电脑端单元测试", [PY, "-m", "unittest", "discover", "-s", "desktop/tests", "-p", "test_*.py", "-v"]),
    ("交付物自检", [PY, "outputs/交付自检.py"]),
]


def main() -> int:
    # 管道下 Windows Python 可能默认 GBK；子进程和日志统一 UTF-8。
    # 同时修正本进程 stdout，防止最终打印日志时再次编码失败。
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    child_env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    lines: list[str] = [
        "STM32 电压表与示波器 —— 最终整合验证日志",
        f"时间: {datetime.now().isoformat(timespec='seconds')}",
        f"解释器: {PY}",
        f"工作目录: {ROOT}",
        "=" * 70,
        "",
    ]
    overall = 0
    results: list[tuple[str, int]] = []

    for name, cmd in STEPS:
        lines.append(f"### {name}")
        lines.append("命令: " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
        try:
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="strict", env=child_env, timeout=180)
            out = (r.stdout or "") + (r.stderr or "")
            rc = r.returncode
        except Exception as e:  # noqa: BLE001
            out = f"执行异常: {type(e).__name__}: {e}"
            rc = 99
        lines.append(f"退出码: {rc}")
        lines.extend("  " + ln for ln in out.strip().splitlines())
        lines.append("")
        results.append((name, rc))
        # 协议交叉核对：2=有未核对项（中间态），不能算完整通过
        if rc != 0:
            overall = overall or rc

    # 额外只读事实
    lines.append("### 构建 / 截图 / 文档 只读快照")
    for rel in [
        "firmware/Project.uvprojx", "firmware/Objects/build.log", "firmware/Objects/Project.hex",
        "firmware/Tests/protocol_selftest.c", "desktop/main.py", "desktop/main_window.py",
        "docs/protocol.md", "docs/接线与硬件.md", "docs/上板验收.md", "docs/审查记录.md",
        "outputs/验证报告.md", "outputs/协议核对结果.txt", "outputs/协议状态机复核.txt",
    ]:
        p = ROOT / rel
        lines.append(f"  [{'OK  ' if p.is_file() else 'MISS'}] {rel}" +
                     (f"  ({p.stat().st_size} B)" if p.is_file() else ""))
    for d in ["desktop/screenshots", "outputs/screenshots"]:
        p = ROOT / d
        pngs = sorted(p.glob("*.png")) if p.is_dir() else []
        lines.append(f"  {d}: {len(pngs)} 张 PNG" + (": " + ", ".join(x.name for x in pngs) if pngs else ""))
    lines.append("")

    lines.append("=" * 70)
    lines.append("汇总: " + " | ".join(f"{n}={rc}" for n, rc in results))
    lines.append("退出码含义: 0=全部通过 / 1=有失败 / 2=有未核对项 / 99=执行异常")
    lines.append(f"最终退出码: {overall}")
    text = "\n".join(lines) + "\n"
    LOG.write_text(text, encoding="utf-8")
    print(text[-4000:])
    return overall


if __name__ == "__main__":
    sys.exit(main())
