#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主 agent 的「协议跨实现核对」脚本（只用 Python 标准库）。

目的：避免"Python 自己编码自己解码"的循环验证。核对链路由三条互不依赖的来源组成：

  1. docs/protocol.md 第 7 节人工写死的三个完整十六进制 golden 帧
     -> 本脚本独立实现 CRC-16/CCITT-FALSE 与帧解析，逐字段核对。
  2. firmware/Tests/protocol_selftest.c 里人工写死的 const uint8_t kGoldenSampleFrame[]
     -> 本脚本用正则从 C 源码文本中提取字节，与文档样例 B 逐字节比对，
        并解析这份「来自 C 侧的数据」验证字段与 CRC。
  3. 同一 C 文件里 #if 编译期常量表达式断言的期望 CRC 值
     -> 检查它与文档/Python 三方一致；该断言由 Keil ARMCC 实际编译验证，
        编译通过即证明 C 编译器算出的 CRC 与 Python 一致。

另可选检查 desktop/protocol.py 是否也引用了这些 golden 常量。

用法：python outputs/协议交叉核对.py
输出：控制台 + outputs/协议核对结果.txt；退出码 0 = 全部通过，1 = 有失败项。
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "protocol.md"
C_SELFTEST = ROOT / "firmware" / "Tests" / "protocol_selftest.c"
DESKTOP_PROTOCOL = ROOT / "desktop" / "protocol.py"
OUT = ROOT / "outputs" / "协议核对结果.txt"

MAGIC = bytes([0xA5, 0x5A, 0xC3, 0x3C])
LEN_MAX = 2060
RX_BUF_MAX = 8192

EXPECTED_HEX = {
    "A_SET_RATE": "A55AC33C01200500070004A08601007882",
    "B_SAMPLE_4PT": "A55AC33C011012002A00102700000400E40C000000000008FF0FD204A47E",
    "C_ACK": "A55AC33C01210E0007000400A0860100E40C0000000000003AE7",
}
EXPECTED_CRC = {
    "A_SET_RATE": 0x8278,
    "B_SAMPLE_4PT": 0x7EA4,
    "C_ACK": 0xE73A,
}


def crc16_ccitt_false(data: bytes) -> int:
    """独立实现，不调用任何项目内编码器。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def parse_frame(raw: bytes) -> dict:
    """按 docs/protocol.md 第 2 节独立解析，返回全部字段。"""
    if len(raw) < 12:
        raise ValueError(f"帧过短: {len(raw)}")
    if raw[:4] != MAGIC:
        raise ValueError("MAGIC 不匹配")
    ver, typ = raw[4], raw[5]
    length = struct.unpack_from("<H", raw, 6)[0]
    seq, flags = raw[8], raw[9]
    if length > LEN_MAX:
        raise ValueError(f"LEN={length} 超过上限 {LEN_MAX}")
    if len(raw) != length + 12:
        raise ValueError(f"整帧长度 {len(raw)} != LEN+12 ({length + 12})")
    payload = raw[10:10 + length]
    covered = raw[4:10 + length]
    crc_calc = crc16_ccitt_false(covered)
    crc_wire = struct.unpack_from("<H", raw, 10 + length)[0]
    return {
        "ver": ver, "type": typ, "len": length, "seq": seq, "flags": flags,
        "payload": payload, "crc_calc": crc_calc, "crc_wire": crc_wire,
        "crc_ok": crc_calc == crc_wire,
    }


class Checks:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.failed = 0
        self.skipped = 0
        self.skip_names: list[str] = []

    def ok(self, name: str, detail: str = "") -> None:
        self.lines.append(f"[PASS] {name}" + (f" — {detail}" if detail else ""))

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        self.lines.append(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))

    def skip(self, name: str, detail: str) -> None:
        # SKIP 必须计入结论：docs/protocol.md 第 8 节要求三方证据一致，
        # 缺任何一方都不能写成「全部通过」（避免虚假验证结论）。
        self.skipped += 1
        self.skip_names.append(name)
        self.lines.append(f"[SKIP] {name} — {detail}")

    def check(self, cond: bool, name: str, detail: str = "") -> None:
        (self.ok if cond else self.fail)(name, detail)


def main() -> int:
    c = Checks()
    c.lines.append("协议跨实现核对报告")
    c.lines.append(f"项目根目录: {ROOT}")
    c.lines.append("核对依据: docs/protocol.md v1（第 5/7/8 节）")
    c.lines.append("")

    # ---------- 0. CRC 标准自检向量 ----------
    c.check(crc16_ccitt_false(b"123456789") == 0x29B1,
            'CRC 标准向量 "123456789"', f"实得 0x{crc16_ccitt_false(b'123456789'):04X}，期望 0x29B1")
    c.check(LEN_MAX == 2060 and RX_BUF_MAX == 8192, "长度常量", f"LEN_MAX={LEN_MAX}, RX_BUF_MAX={RX_BUF_MAX}")
    c.lines.append("")

    # ---------- 1. 从文档提取 golden 十六进制串（不是自己编码生成） ----------
    doc_text = DOC.read_text(encoding="utf-8") if DOC.exists() else ""
    doc_hex: dict[str, str] = {}
    for key, val in EXPECTED_HEX.items():
        # 直接在整篇文档中找该串（大小写不敏感），不调用任何编码器生成
        m = re.search(r"`(" + re.escape(val) + r")`", doc_text, re.IGNORECASE)
        if m:
            doc_hex[key] = m.group(1).upper()
            c.ok(f"文档含 golden 串 {key}", m.group(1).upper())
        else:
            c.fail(f"文档含 golden 串 {key}", "docs/protocol.md 第 7 节未找到该十六进制串")
    c.lines.append("")

    # ---------- 2. 独立解析三帧并逐字段核对 ----------
    frames: dict[str, bytes] = {}
    for key, hexstr in EXPECTED_HEX.items():
        raw = bytes.fromhex(hexstr)
        frames[key] = raw
        try:
            f = parse_frame(raw)
        except ValueError as e:
            c.fail(f"解析 {key}", str(e))
            continue
        c.check(f["ver"] == 1, f"{key} VER", str(f["ver"]))
        c.check(f["flags"] == 0, f"{key} FLAGS", str(f["flags"]))
        c.check(f["crc_ok"], f"{key} CRC 自洽", f"帧内 0x{f['crc_wire']:04X} / 重算 0x{f['crc_calc']:04X}")
        c.check(f["crc_calc"] == EXPECTED_CRC[key], f"{key} CRC 期望值",
                f"0x{f['crc_calc']:04X} vs 0x{EXPECTED_CRC[key]:04X}")
        p = f["payload"]

        if key == "A_SET_RATE":
            c.check(f["type"] == 0x20, "A TYPE=COMMAND", hex(f["type"]))
            c.check(f["len"] == 5 and f["seq"] == 0x07, "A LEN/SEQ", f"LEN={f['len']}, SEQ={f['seq']}")
            cmd = p[0]
            rate = struct.unpack_from("<I", p, 1)[0]
            c.check(cmd == 0x04 and rate == 100000, "A 命令字段", f"cmd=0x{cmd:02X}, rate={rate} Hz")
        elif key == "B_SAMPLE_4PT":
            c.check(f["type"] == 0x10, "B TYPE=SAMPLE", hex(f["type"]))
            c.check(f["len"] == 18 and f["seq"] == 0x2A, "B LEN/SEQ", f"LEN={f['len']}, SEQ=0x{f['seq']:02X}")
            rate, n, vref, resv = struct.unpack_from("<IHHH", p, 0)
            samples = list(struct.unpack_from("<4H", p, 10))
            c.check(rate == 10000 and n == 4 and vref == 3300 and resv == 0,
                    "B 采样元信息", f"rate={rate}, n={n}, vref_mv={vref}, reserved={resv}")
            c.check(samples == [0, 2048, 4095, 1234], "B 原始 ADC 码", str(samples))
            c.check(f["len"] == 10 + 2 * n, "B LEN 与点数自洽", f"{f['len']} == 10+2*{n}")
            volts = [round(code / 4095.0 * (vref / 1000.0), 4) for code in samples]
            c.check(volts == [0.0, 1.6504, 3.3, 0.9944], "B 电压换算", str(volts))
        elif key == "C_ACK":
            c.check(f["type"] == 0x21, "C TYPE=RESPONSE", hex(f["type"]))
            c.check(f["len"] == 14 and f["seq"] == 0x07, "C LEN/SEQ", f"LEN={f['len']}, SEQ={f['seq']}")
            ack, status = p[0], p[1]
            rate, vref = struct.unpack_from("<IH", p, 2)
            run_state, dev_flags = p[8], p[9]
            frames_sent = struct.unpack_from("<I", p, 10)[0]
            c.check(ack == 0x04 and status == 0x00 and rate == 100000 and vref == 3300
                    and run_state == 0 and dev_flags == 0 and frames_sent == 0,
                    "C 响应字段", f"ack=0x{ack:02X}, status={status}, rate={rate}, vref={vref}, "
                                  f"run={run_state}, flags={dev_flags}, frames_sent={frames_sent}")
    c.lines.append("")

    # ---------- 3. C 侧 golden 数组（跨实现：解析来自 C 源码的数据） ----------
    if C_SELFTEST.exists():
        ctext = C_SELFTEST.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"kGoldenSampleFrame\s*\[\s*\]\s*=\s*\{(.*?)\}\s*;", ctext, re.S)
        if m:
            cbytes = bytes(int(x, 16) for x in re.findall(r"0[xX]([0-9A-Fa-f]{1,2})(?![0-9A-Fa-f])", m.group(1)))
            c.check(len(cbytes) == len(frames["B_SAMPLE_4PT"]),
                    "C golden 数组长度", f"{len(cbytes)} vs {len(frames['B_SAMPLE_4PT'])}")
            c.check(cbytes == frames["B_SAMPLE_4PT"],
                    "C golden 数组逐字节 == 文档样例 B",
                    "完全一致" if cbytes == frames["B_SAMPLE_4PT"] else
                    f"C={cbytes.hex().upper()} / DOC={frames['B_SAMPLE_4PT'].hex().upper()}")
            try:
                cf = parse_frame(cbytes)
                c.check(cf["crc_ok"] and cf["crc_calc"] == 0x7EA4,
                        "解析 C 侧 golden 数据", f"CRC=0x{cf['crc_calc']:04X}, n={struct.unpack_from('<H', cf['payload'], 4)[0]}")
            except ValueError as e:
                c.fail("解析 C 侧 golden 数据", str(e))
        else:
            c.skip("C golden 数组", "firmware/Tests/protocol_selftest.c 中未找到 kGoldenSampleFrame[]（固件 agent 可能尚未完成）")

        # ---------- 4. C 编译期断言的期望值 ----------
        for key, val in EXPECTED_CRC.items():
            pat = re.compile(r"0x" + format(val, "04X"), re.IGNORECASE)
            hits = len(pat.findall(ctext))
            c.check(hits >= 1, f"C 断言含期望 CRC {key} 0x{val:04X}", f"出现 {hits} 次")
        c.check(re.search(r"0x29B1", ctext, re.IGNORECASE) is not None,
                'C 断言含标准向量 0x29B1')
        c.check("#if" in ctext and "#error" in ctext,
                "C 使用 #if 常量表达式 + #error 断言")
        # 防回归：ARMCC 5 预处理器对「CRC_STEP 宏链嵌套 9~24 层」会报
        # Error: #4 out of memory（见 outputs/keil_probe/CRC编译期断言探测.txt）。
        # 正确写法是每步一个独立 #if，输入 crc 为 0x 字面常量。
        chained = re.findall(r"#define\s+CRC_[A-Za-z0-9_]*_\d+\s+CRC_STEP\(\s*CRC_", ctext)
        literal_steps = re.findall(r"#if\s+CRC_STEP\(\s*0[xX][0-9A-Fa-f]+u\s*,", ctext)
        c.check(not chained and len(literal_steps) >= 60,
                "C 断言为分段独立写法（无宏链，避免 ARMCC 预处理期溢出）",
                f"宏链 {len(chained)} 处，字面量起始的独立断言 {len(literal_steps)} 条")
        c.lines.append("       提示: 编译期断言必须由 ARMCC 实际编译通过才算证据，"
                       "见 outputs/keil_probe/CRC编译期断言探测.txt 与固件构建日志。")
    else:
        c.skip("C 侧 golden/断言", f"{C_SELFTEST} 尚不存在（固件 agent 可能尚未完成）")
    c.lines.append("")

    # ---------- 5. 电脑端实现是否引用了同样的常量（可选，非循环验证） ----------
    if DESKTOP_PROTOCOL.exists():
        ptext = DESKTOP_PROTOCOL.read_text(encoding="utf-8", errors="replace")
        c.check("0xFFFF" in ptext and "0x1021" in ptext,
                "电脑端 CRC 实现含标准多项式/初值")
        c.check(re.search(r"A5\s*5A\s*C3\s*3C|0xA5,?\s*0x5A,?\s*0xC3,?\s*0x3C|MAGIC", ptext) is not None,
                "电脑端含 MAGIC 定义")
    else:
        c.skip("电脑端实现", f"{DESKTOP_PROTOCOL} 尚不存在（电脑端 agent 可能尚未完成）")
    c.lines.append("")

    c.lines.append("=" * 60)
    if c.failed:
        verdict = f"有 {c.failed} 项失败；另有 {c.skipped} 项未核对"
    elif c.skipped:
        verdict = (f"已核对项全部通过，但有 {c.skipped} 项未核对 —— "
                   "不构成完整的跨实现核对，不能写成『全部通过』")
    else:
        verdict = "三方证据齐全且全部通过"
    c.lines.append(f"结论: {verdict}")
    if c.skip_names:
        c.lines.append("未完成项: " + "; ".join(c.skip_names))
    c.lines.append("说明: 本机没有可在 PC 上执行的 x86 C 编译器，因此 C 运行时函数不能编译成 PC 程序执行；")
    c.lines.append("      跨实现证据由「ARMCC 编译期 #if 常量断言（见固件构建日志）」+「本脚本解析 C 源码中的人工 golden 字节」构成。")
    c.lines.append("      「C 编码→Python 解码 / Python 编码→C 解码」的运行级双向核对列为待补项。")

    text = "\n".join(c.lines) + "\n"
    OUT.write_text(text, encoding="utf-8")
    print(text)
    # 0=完整通过；1=有失败；2=有未核对项（中间态，不允许当成完整验证）
    if c.failed:
        return 1
    return 2 if c.skipped else 0


if __name__ == "__main__":
    sys.exit(main())
