#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主 agent 整合阶段自检：只读扫描交付物，输出 outputs/交付自检结果.txt。

用法：python outputs/交付自检.py
退出码 0 = 必需交付物齐全；1 = 有缺失（不阻塞，报告里如实记录）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs" / "交付自检结果.txt"

REQUIRED = [
    "README.md",
    "docs/protocol.md", "docs/接线与硬件.md", "docs/上板验收.md", "docs/审查记录.md",
    "firmware/Project.uvprojx", "firmware/README.md", "firmware/引脚资源核对.md",
    "desktop/main.py", "desktop/requirements.txt",
    "outputs/安装依赖.bat", "outputs/启动仪器.bat", "outputs/运行验证.bat",
    "outputs/协议交叉核对.py", "outputs/协议核对结果.txt", "outputs/验证报告.md",
]
REQUIRED_GLOBS = [
    "firmware/Hardware/*.c", "firmware/Hardware/*.h",
    "firmware/Start/startup_stm32f10x_md.s", "firmware/Library/stm32f10x_adc.c",
    "firmware/Tests/*.c", "firmware/Objects/build.log",
    "desktop/tests/*.py", "desktop/screenshots/*.png", "outputs/screenshots/*.png",
    "firmware/Objects/*.hex",
]


def main() -> int:
    lines: list[str] = ["交付物自检报告", f"项目根目录: {ROOT}", ""]
    missing = 0

    lines.append("== 必需文件 ==")
    for rel in REQUIRED:
        p = ROOT / rel
        ok = p.is_file()
        if not ok:
            missing += 1
        lines.append(f"[{'OK  ' if ok else 'MISS'}] {rel}" + (f"  ({p.stat().st_size} B)" if ok else ""))
    lines.append("")

    lines.append("== 必需文件（glob） ==")
    for pat in REQUIRED_GLOBS:
        hits = sorted(ROOT.glob(pat))
        ok = len(hits) > 0
        if not ok:
            missing += 1
        lines.append(f"[{'OK  ' if ok else 'MISS'}] {pat} -> {len(hits)} 个"
                     + (": " + ", ".join(h.relative_to(ROOT).as_posix() for h in hits[:6]) if hits else ""))
    lines.append("")

    lines.append("== Keil 工程配置 ==")
    up = ROOT / "firmware" / "Project.uvprojx"
    if up.is_file():
        t = up.read_text(encoding="utf-8", errors="replace")
        checks = [
            ("Device STM32F103C8", r"<Device>STM32F103C8</Device>" in t),
            ("PackID 2.2.0", "Keil.STM32F1xx_DFP.2.2.0" in t),
            ("CreateHexFile=1", re.search(r"<CreateHexFile>\s*1\s*</CreateHexFile>", t) is not None),
            ("USE_STDPERIPH_DRIVER", "USE_STDPERIPH_DRIVER" in t),
            ("md 启动文件", "startup_stm32f10x_md.s" in t),
            ("IRAM 0x5000 / IROM 0x10000", "0x5000" in t and "0x10000" in t),
            ("IncludePath 含 Common/Hardware", (".\\Common" in t or "./Common" in t) and "Hardware" in t),
            ("协议自检文件在工程里", "protocol_selftest" in t.lower()),
        ]
        for name, ok in checks:
            lines.append(f"[{'OK  ' if ok else 'MISS'}] {name}")
            if not ok:
                missing += 1
    else:
        lines.append("[MISS] firmware/Project.uvprojx 不存在")
        missing += 1
    lines.append("")

    lines.append("== 构建日志与产物判定（只读，不代替实际构建） ==")
    log = ROOT / "firmware" / "Objects" / "build.log"
    hexes = sorted((ROOT / "firmware" / "Objects").glob("*.hex")) if (ROOT / "firmware" / "Objects").is_dir() else []
    if log.is_file():
        t = log.read_text(encoding="utf-8", errors="replace")
        for kw in ["Error", "Warning", "Program Size", "Code", "RO-data", "RW-data", "ZI-data", "hex"]:
            lines.append(f"  {kw}: {len(re.findall(kw, t, re.IGNORECASE))} 次")
        zero_err = re.search(r"\b0\s+Error\(s\)", t, re.IGNORECASE) is not None
        lines.append(f"[{'OK  ' if zero_err else 'MISS'}] 日志含 '0 Error(s)'（构建无错误）")
        if not zero_err:
            missing += 1
        lines.append(f"[{'OK  ' if hexes else 'MISS'}] 生成 HEX: "
                     + (", ".join(h.name for h in hexes) if hexes else "无"))
        if not hexes:
            missing += 1
        tail = "\n".join(t.strip().splitlines()[-12:])
        lines.append("  --- 日志末尾 12 行 ---")
        lines.extend("  " + ln for ln in tail.splitlines())
    else:
        lines.append("[MISS] firmware/Objects/build.log 不存在")
        missing += 1
        lines.append("[MISS] 未生成 HEX")
        missing += 1
    lines.append("  说明: 本脚本只读取既有日志与产物，不执行 Keil 构建；"
                 "『构建成功』以 UV4 原始日志 + 退出码为准，不得由文件存在推断。")
    lines.append("")

    lines.append("== 交付包排除项检查 ==")
    for name in [".venv", ".git"]:
        p = ROOT / name
        lines.append(f"  {name}: {'存在（打包脚本会排除）' if p.exists() else '不存在'}")
    vids = list(ROOT.rglob("*.mp4"))
    lines.append(f"  *.mp4 视频: {len(vids)} 个 {'（不应有）' if vids else 'OK'}")
    lines.append("")

    lines.append("=" * 60)
    lines.append(f"结论: {'必需交付物齐全' if missing == 0 else f'有 {missing} 项缺失/待补（见上）'}")
    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
