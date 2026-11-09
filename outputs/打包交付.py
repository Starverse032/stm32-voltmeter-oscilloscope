#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成交付 ZIP：保留目录结构，排除虚拟环境 / .git / 缓存 / 视频 / 旧构建垃圾。

用法：python outputs/打包交付.py
输出：outputs/STM32电压表与示波器_交付包.zip
"""
from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs" / "STM32电压表与示波器_交付包.zip"

EXCLUDE_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "node_modules", ".vs", ".vscode", "work"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".mp4", ".avi", ".mov", ".zip", ".uvguix"}
EXCLUDE_NAMES = {"Project.uvguix.29319", "keilkill.bat", "Desktop.ini", "Thumbs.db"}

# 固件构建产物：保留 .hex / .log / .map / .axf（作为构建证据），排除中间 .o/.d/.crf 等
FIRMWARE_BUILD_KEEP = {".hex", ".log", ".map", ".axf", ".htm", ".sct"}
FIRMWARE_BUILD_DROP = {".o", ".d", ".crf", ".dep", ".lnp", ".lst", ".sbr", ".bak", ".tra", ".iex", ".i"}


def keep(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = set(rel.parts)
    if parts & EXCLUDE_DIRS:
        return False
    if path.name in EXCLUDE_NAMES:
        return False
    if any(path.name.endswith(s) for s in EXCLUDE_SUFFIXES):
        return False
    if len(rel.parts) >= 2 and rel.parts[0] == "firmware" and rel.parts[1] in ("Objects", "Listings"):
        ext = path.suffix.lower()
        if ext in FIRMWARE_BUILD_DROP:
            return False
        if ext and ext not in FIRMWARE_BUILD_KEEP and ext not in {".txt", ".md"}:
            return False
    return path.is_file()


def main() -> None:
    files = sorted((p for p in ROOT.rglob("*") if keep(p)), key=lambda p: str(p.relative_to(ROOT)))
    if OUT.exists():
        OUT.unlink()
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            z.write(p, p.relative_to(ROOT).as_posix())
        z.writestr("打包清单.txt", "\n".join(
            [f"打包时间: {datetime.now().isoformat(timespec='seconds')}",
             f"文件数: {len(files)}",
             "已排除: .venv / .git / __pycache__ / *.pyc / 视频 / Keil 个人界面配置 / 旧构建中间产物",
             ""] + [p.relative_to(ROOT).as_posix() for p in files]
        ))
    print(f"已生成: {OUT}")
    print(f"文件数: {len(files)}, 大小: {OUT.stat().st_size / 1024:.1f} KiB")
    for p in files:
        print("  ", p.relative_to(ROOT).as_posix())


if __name__ == "__main__":
    main()
