#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主 agent 整合复核：用 docs/protocol.md 的 golden 字节串去扰动、检验电脑端 FrameParser。

输入不是电脑端自己编码出来的（避免循环验证）：三帧十六进制直接从协议文档读取，
再人为制造分片/合并/噪声/CRC 错/非法长度/截断等情形，喂给 desktop/protocol.py。

用法（项目根）：.venv\\Scripts\\python.exe outputs\\协议状态机复核.py
输出：outputs/协议状态机复核.txt；退出码 0=全部通过，1=有失败项。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "desktop"))

from protocol import FrameParser, crc16_ccitt_false  # noqa: E402
import protocol as _p  # noqa: E402

DOC = ROOT / "docs" / "protocol.md"
OUT = ROOT / "outputs" / "协议状态机复核.txt"

GOLDEN = {
    "A_SET_RATE": "A55AC33C01200500070004A08601007882",
    "B_SAMPLE_4PT": "A55AC33C011012002A00102700000400E40C000000000008FF0FD204A47E",
    "C_ACK": "A55AC33C01210E0007000400A0860100E40C0000000000003AE7",
}


def main() -> int:
    lines: list[str] = ["协议状态机整合复核（输入来自 docs/protocol.md 的 golden 字节串）", ""]
    failed = 0
    doc = DOC.read_text(encoding="utf-8")
    raws: dict[str, bytes] = {}
    for k, h in GOLDEN.items():
        m = re.search(r"`(" + re.escape(h) + r")`", doc, re.IGNORECASE)
        if not m:
            lines.append(f"[FAIL] 文档中找不到 {k} 的 golden 串")
            failed += 1
            continue
        raws[k] = bytes.fromhex(m.group(1))
        lines.append(f"[OK] 从文档读取 {k}: {len(raws[k])} 字节")

    def check(cond: bool, name: str, detail: str = "") -> None:
        nonlocal failed
        if not cond:
            failed += 1
        lines.append(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    b = raws.get("B_SAMPLE_4PT", b"")
    a = raws.get("A_SET_RATE", b"")
    c = raws.get("C_ACK", b"")

    # 1. 逐字节分片（串口一次 read != 一个完整包）
    p = FrameParser(clock=lambda: 0.0)
    got = []
    for byte in b:
        fr, _ = p.feed(bytes([byte]), now=0.0)
        got.extend(fr)
    check(len(got) == 1 and got[0].raw == b, "逐字节分片能重组出完整采样帧",
          f"解出 {len(got)} 帧")

    # 2. 任意两字节分片
    p = FrameParser(clock=lambda: 0.0)
    got = []
    for i in range(0, len(b), 2):
        fr, _ = p.feed(b[i:i + 2], now=0.0)
        got.extend(fr)
    check(len(got) == 1 and got[0].raw == b, "任意两字节分片能重组")

    # 3. 多帧合并（一次 read 含三帧）
    p = FrameParser(clock=lambda: 0.0)
    fr, _ = p.feed(a + b + c, now=0.0)
    check(len(fr) == 3 and [f.ftype for f in fr] == [0x20, 0x10, 0x21],
          "一次 read 含 3 帧全部解出且类型正确", str([hex(f.ftype) for f in fr]))

    # 4. 前置噪声 + 后续合法帧重同步
    p = FrameParser(clock=lambda: 0.0)
    fr, _ = p.feed(b"\x00\xFF\x11\x22" + b, now=0.0)
    check(len(fr) == 1 and fr[0].raw == b, "前置随机噪声后能重同步")

    # 5. 帧中间插入噪声（等价于错帧残骸）后仍能找回下一帧
    p = FrameParser(clock=lambda: 0.0)
    fr, _ = p.feed(a[:8] + b"\xDE\xAD\xBE\xEF" + a[8:] + b, now=0.0)
    check(any(f.raw == b for f in fr), "噪声残骸后能找回后续合法帧",
          f"解出 {len(fr)} 帧")

    # 6. CRC 错 1 位：该帧被丢弃，后面的合法帧仍可恢复
    bad = bytearray(b)
    bad[-1] ^= 0x01
    p = FrameParser(clock=lambda: 0.0)
    fr, _ = p.feed(bytes(bad) + c, now=0.0)
    check(all(f.raw != bytes(bad) for f in fr) and any(f.raw == c for f in fr),
          "CRC 错帧被丢弃且后续帧可恢复", f"解出 {len(fr)} 帧")

    # 7. LEN 被改大（超过上限）→ 判非法长度并重同步
    bad = bytearray(b)
    bad[6], bad[7] = 0xFF, 0xFF
    p = FrameParser(clock=lambda: 0.0)
    fr, _ = p.feed(bytes(bad) + c, now=0.0)
    check(any(f.raw == c for f in fr), "LEN=65535 非法长度后能恢复后续帧")

    # 8. LEN 被改小（与 TYPE 不自洽）→ 不能当成有效帧
    bad = bytearray(b)
    bad[6], bad[7] = 0x03, 0x00
    p = FrameParser(clock=lambda: 0.0)
    fr, _ = p.feed(bytes(bad) + c, now=0.0)
    check(all(f.ftype != 0x10 for f in fr), "LEN=3 与 SAMPLE 不自洽，不当成有效采样帧")

    # 9. 截断包 + 超时恢复（注入时间）
    p = FrameParser(clock=lambda: 0.0)
    p.feed(b[:15], now=0.0)
    fr, errs = p.tick(now=1.2)
    check(not fr and p.counts.timeouts >= 1, "截断帧 1s 超时被丢弃并上报",
          f"timeouts={p.counts.timeouts}, errors={len(errs)}")
    fr, _ = p.feed(c, now=1.3)
    check(any(f.raw == c for f in fr), "截断超时后接一个合法帧能恢复")

    # 10. 缓冲区上限（超过 8192 会整体清空）
    p = FrameParser(clock=lambda: 0.0)
    p.feed(b"\x55" * 9000, now=0.0)
    check(p.counts.buffer_overflows >= 1, "接收缓冲超过 8192 会清空重同步",
          f"buffer_overflows={p.counts.buffer_overflows}")

    # 11. 参数变更响应：SEQ 回显 + 实际值回读（检查帧内字段）
    p = FrameParser(clock=lambda: 0.0)
    fr, _ = p.feed(a + c, now=0.0)
    check(len(fr) == 2 and fr[0].seq == fr[1].seq == 0x07,
          "命令与响应 SEQ 回显一致", f"seq={[f.seq for f in fr]}")
    payload = fr[1].payload if len(fr) == 2 else b""
    check(len(payload) == 14 and payload[0] == 0x04 and payload[1] == 0x00
          and int.from_bytes(payload[2:6], "little") == 100000,
          "响应回读实际采样率 100000 / status OK / ack_cmd=SET_RATE")

    # 12. CRC 标准向量（与固件 C 断言、文档三方共用）
    check(crc16_ccitt_false(b"123456789") == 0x29B1, "CRC 标准向量 0x29B1")

    # 13. 编码器输出与「文档人工常量」逐字节比对（不是自己编自己解的循环验证：
    #     期望值来自 docs/protocol.md 第 7 节手工写死的十六进制）
    if len(raws) == 3:
        made = {
            "A_SET_RATE": _p.build_set_rate(100000, 0x07),
            "B_SAMPLE_4PT": _p.build_sample(10000, 3300, [0, 2048, 4095, 1234], 0x2A),
            "C_ACK": _p.build_response(0x04, 0x00, 100000, 3300, 0, 0, 0, 0x07),
        }
        for k, v in made.items():
            check(v == raws[k], f"编码器 {k} == 文档 golden 常量",
                  "逐字节一致" if v == raws[k] else v.hex().upper())

    lines.append("")
    lines.append("=" * 60)
    lines.append(f"结论: {'全部通过' if failed == 0 else f'有 {failed} 项失败'}")
    text = "\n".join(lines) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
