# -*- coding: utf-8 -*-
"""协议层单元测试。

重要：golden 三帧直接硬编码 docs/protocol.md 第 7 节的十六进制字符串，
**不调用** 自己的 encode_frame 生成这些输入（避免"自己编码自己解码"）。
"""
import os
import re
import struct
import sys
import unittest

DESKTOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(DESKTOP)
C_SELFTEST = os.path.join(PROJECT_ROOT, "firmware", "Tests", "protocol_selftest.c")
sys.path.insert(0, DESKTOP)
import protocol as p  # noqa: E402

# ---- docs/protocol.md 第 7 节 golden vector（逐字硬编码） ----
GOLDEN_A = "A55AC33C01200500070004A08601007882"          # SET_RATE=100000, SEQ=0x07
GOLDEN_B = "A55AC33C011012002A00102700000400E40C000000000008FF0FD204A47E"  # 4点采样, SEQ=0x2A
GOLDEN_C = "A55AC33C01210E0007000400A0860100E40C0000000000003AE7"          # ACK, SEQ=0x07


class TestCRC(unittest.TestCase):
    def test_self_check_vector(self):
        self.assertEqual(p.crc16_ccitt_false(b"123456789"), 0x29B1)

    def test_golden_crc_coverage(self):
        """CRC 覆盖段 = raw[4 : 4+6+LEN]，三帧分别 0x8278 / 0x7EA4 / 0xE73A。"""
        for hx, want in ((GOLDEN_A, 0x8278), (GOLDEN_B, 0x7EA4), (GOLDEN_C, 0xE73A)):
            raw = bytes.fromhex(hx)
            ln = raw[6] | (raw[7] << 8)
            self.assertEqual(len(raw), ln + 12)
            self.assertEqual(len(raw[4:4 + 6 + ln]), 6 + ln)
            self.assertEqual(p.crc16_ccitt_false(raw[4:4 + 6 + ln]), want)
            # 帧内 CRC 低字节在前
            self.assertEqual(raw[-2] | (raw[-1] << 8), want)


class TestGoldenFrames(unittest.TestCase):
    def test_A_command_set_rate(self):
        frames, errs = p.FrameParser().feed(bytes.fromhex(GOLDEN_A))
        self.assertEqual(errs, [])
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f.ver, 0x01)
        self.assertEqual(f.ftype, p.TYPE_COMMAND)
        self.assertEqual(f.seq, 0x07)
        self.assertEqual(f.flags, 0x00)
        self.assertEqual(f.length, 5)
        self.assertEqual(f.crc, 0x8278)
        cmd, params = p.parse_command_payload(f.payload)
        self.assertEqual(cmd, p.CMD_SET_RATE)
        self.assertEqual(int.from_bytes(params, "little"), 100000)

    def test_B_sample_fields(self):
        frames, errs = p.FrameParser().feed(bytes.fromhex(GOLDEN_B))
        self.assertEqual(errs, [])
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f.ftype, p.TYPE_SAMPLE)
        self.assertEqual(f.seq, 0x2A)
        self.assertEqual(f.length, 18)
        self.assertEqual(f.crc, 0x7EA4)
        sd = p.parse_sample_payload(f.payload)
        self.assertTrue(sd.ok, sd.reason)
        self.assertEqual(sd.sample_rate_hz, 10000)
        self.assertEqual(sd.point_count, 4)
        self.assertEqual(sd.vref_mv, 3300)
        self.assertEqual(sd.samples, [0, 2048, 4095, 1234])

    def test_B_voltage_conversion(self):
        """按协议公式 V=code/4095.0*(vref_mv/1000.0) 独立核对换算。

        golden 字节串直接硬编码自 docs/protocol.md 第 7 节，不调用被测编码器。
        期望值与文档一致：0.0000 / 1.6504 / 3.3000 / 0.9944 V（文档保留
        4 位小数）；换算由公式独立计算，避免"自己编码自己解码"。
        CRC、字段、golden 字节串本身也由其它用例与文档逐项核对。
        """
        f = p.FrameParser().feed(bytes.fromhex(GOLDEN_B))[0][0]
        sd = p.parse_sample_payload(f.payload)
        from processing import codes_to_volts
        v = codes_to_volts(sd.samples, sd.vref_mv)
        for code, got in zip(sd.samples, v):
            self.assertAlmostEqual(got, code / 4095.0 * (sd.vref_mv / 1000.0),
                                   places=9)
        # 公式精确值（等价于文档保留 4 位的 0.0000/1.6504/3.3000/0.9944 V）
        for got, want in zip(v, (0.0, 1.6504029304029304, 3.3,
                                 0.9944322344322343)):
            self.assertAlmostEqual(got, want, places=9)

    def test_C_response_fields(self):
        frames, errs = p.FrameParser().feed(bytes.fromhex(GOLDEN_C))
        self.assertEqual(errs, [])
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f.ftype, p.TYPE_RESPONSE)
        self.assertEqual(f.seq, 0x07)
        self.assertEqual(f.length, 14)
        self.assertEqual(f.crc, 0xE73A)
        rd = p.parse_response_payload(f.payload)
        self.assertTrue(rd.ok, rd.reason)
        self.assertEqual(rd.ack_cmd, p.CMD_SET_RATE)
        self.assertEqual(rd.status, p.STATUS_OK)
        self.assertEqual(rd.actual_rate_hz, 100000)
        self.assertEqual(rd.vref_mv, 3300)
        self.assertEqual(rd.run_state, p.RUN_IDLE)
        self.assertEqual(rd.frames_sent, 0)


class TestStreamResync(unittest.TestCase):
    """第 6 节状态机：分片 / 合并 / 噪声 / CRC / LEN / 截断恢复。"""

    def test_byte_by_byte_fragmentation(self):
        """任意分片，含逐字节。"""
        raw = bytes.fromhex(GOLDEN_B)
        ps = p.FrameParser()
        frames = []
        for i, b in enumerate(raw):
            f, _ = ps.feed(bytes([b]), now=100.0 + i * 0.001)
            frames.extend(f)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 0x2A)

    def test_random_chunk_fragmentation(self):
        """多种分片大小（含跨帧边界）都能解出全部 3 帧。"""
        raw = bytes.fromhex(GOLDEN_A + GOLDEN_B + GOLDEN_C)
        for size in (1, 2, 3, 5, 7, 11, 13, 29, 64):
            ps = p.FrameParser()
            got = []
            for i in range(0, len(raw), size):
                f, _ = ps.feed(raw[i:i + size], now=200.0 + i * 0.0001)
                got.extend(f)
            self.assertEqual(len(got), 3, "分片大小=%d" % size)
            self.assertEqual([x.ftype for x in got],
                             [p.TYPE_COMMAND, p.TYPE_SAMPLE, p.TYPE_RESPONSE])

    def test_multiple_frames_in_one_read(self):
        raw = bytes.fromhex(GOLDEN_A + GOLDEN_B + GOLDEN_C)
        frames, errs = p.FrameParser().feed(raw)
        self.assertEqual(errs, [])
        self.assertEqual([f.ftype for f in frames],
                         [p.TYPE_COMMAND, p.TYPE_SAMPLE, p.TYPE_RESPONSE])
        self.assertEqual([f.seq for f in frames], [0x07, 0x2A, 0x07])

    def test_leading_noise_resync(self):
        noise = b"\x00\x11\x22\x7F\xFE"
        frames, _ = p.FrameParser().feed(noise + bytes.fromhex(GOLDEN_B))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 0x2A)

    def test_middle_noise_resync(self):
        """两帧之间插入不含 MAGIC 的噪声，应被丢弃且两帧都解出。"""
        raw = bytes.fromhex(GOLDEN_A) + b"\x01\x02\x03" + bytes.fromhex(GOLDEN_B)
        ps = p.FrameParser()
        frames, _ = ps.feed(raw)
        self.assertEqual(len(frames), 2)
        self.assertEqual([f.seq for f in frames], [0x07, 0x2A])
        self.assertGreaterEqual(ps.counts.noise_bytes, 3)

    def test_crc_one_bit_error_then_valid_frame(self):
        bad = bytearray(bytes.fromhex(GOLDEN_B))
        bad[12] ^= 0x01  # payload 里翻 1 位
        ps = p.FrameParser()
        frames, _ = ps.feed(bytes(bad) + bytes.fromhex(GOLDEN_B))
        self.assertEqual(ps.counts.crc_errors, 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 0x2A)  # 后面合法帧被找回

    def test_len_too_large_illegal_then_recovery(self):
        """LEN 改大到 >2060 → 非法长度，丢弃 MAGIC 首字节后恢复。"""
        bad = bytearray(bytes.fromhex(GOLDEN_B))
        bad[6], bad[7] = 0xFF, 0x0F  # LEN = 4095
        ps = p.FrameParser()
        frames, _ = ps.feed(bytes(bad) + bytes.fromhex(GOLDEN_B))
        self.assertEqual(ps.counts.illegal_length, 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 0x2A)

    def test_len_too_small_illegal_then_recovery(self):
        """LEN 改小到与 TYPE 不自洽（SAMPLE LEN=8）→ 非法长度后恢复。"""
        bad = bytearray(bytes.fromhex(GOLDEN_B))
        bad[6], bad[7] = 0x08, 0x00
        ps = p.FrameParser()
        frames, _ = ps.feed(bytes(bad) + bytes.fromhex(GOLDEN_B))
        self.assertEqual(ps.counts.illegal_length, 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 0x2A)

    def test_len_modified_but_self_consistent_is_caught_by_crc(self):
        """LEN 改成仍自洽的 20（N=5）→ 覆盖段变化，CRC 不通过 → 恢复后续合法帧。"""
        bad = bytearray(bytes.fromhex(GOLDEN_B))
        bad[6], bad[7] = 0x14, 0x00  # LEN = 20
        ps = p.FrameParser()
        frames, _ = ps.feed(bytes(bad) + bytes.fromhex(GOLDEN_B))
        self.assertEqual(ps.counts.crc_errors, 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 0x2A)

    def test_truncated_then_valid_frame_recovers(self):
        """截断包后紧接一个合法帧，能重同步恢复。"""
        trunc = bytes.fromhex(GOLDEN_B)[:15]
        frames, _ = p.FrameParser().feed(trunc + bytes.fromhex(GOLDEN_B))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 0x2A)

    def test_timeout_recovery(self):
        """已定位 MAGIC 的帧 1s 未收齐 → 超时丢弃首字节；再喂合法帧可恢复。"""
        trunc = bytes.fromhex(GOLDEN_B)[:15]
        ps = p.FrameParser()
        f0, _ = ps.feed(trunc, now=100.0)
        self.assertEqual(len(f0), 0)
        f1, e1 = ps.tick(now=101.2)
        self.assertEqual(len(f1), 0)
        self.assertEqual(ps.counts.timeouts, 1)
        self.assertTrue(any("超时" in e for e in e1))
        f2, _ = ps.feed(bytes.fromhex(GOLDEN_B), now=101.2)
        self.assertEqual(len(f2), 1)
        self.assertEqual(f2[0].seq, 0x2A)

    def test_version_mismatch_then_recovery(self):
        bad = bytearray(bytes.fromhex(GOLDEN_B))
        bad[4] = 0x02  # VER != 0x01
        ps = p.FrameParser()
        frames, _ = ps.feed(bytes(bad) + bytes.fromhex(GOLDEN_B))
        self.assertEqual(ps.counts.version_mismatch, 1)
        self.assertEqual(len(frames), 1)

    def test_unknown_type_whole_frame_dropped(self):
        unknown = p.encode_frame(0x99, 1, b"\x01\x02")
        ps = p.FrameParser()
        frames, errs = ps.feed(unknown)
        self.assertEqual(len(frames), 0)
        self.assertEqual(ps.counts.unknown_type, 1)
        self.assertTrue(any("未知 TYPE" in e for e in errs))

    def test_rx_buffer_overflow(self):
        ps = p.FrameParser()
        frames, errs = ps.feed(b"\x00" * 9000, now=1.0)
        self.assertEqual(len(frames), 0)
        self.assertEqual(ps.counts.buffer_overflows, 1)
        self.assertTrue(any("8192" in e for e in errs))

    def test_counts_summary(self):
        ps = p.FrameParser()
        ps.feed(bytes.fromhex(GOLDEN_B))
        d = ps.counts.as_dict()
        self.assertEqual(d["有效帧"], 1)
        self.assertEqual(d["CRC 错误"], 0)


class TestShortAndCorruptSamples(unittest.TestCase):
    """解析器必须支持 1..1024 点合法短帧；损坏样本整帧丢弃。"""

    def test_short_frames_1_to_1024(self):
        for n in (1, 2, 7, 16, 1024):
            raw = p.build_sample(10000, 3300, [i % 4096 for i in range(n)], 5)
            frames, errs = p.FrameParser().feed(raw)
            self.assertEqual(errs, [], "n=%d" % n)
            self.assertEqual(len(frames), 1, "n=%d" % n)
            sd = p.parse_sample_payload(frames[0].payload)
            self.assertTrue(sd.ok, "n=%d %s" % (n, sd.reason))
            self.assertEqual(sd.point_count, n)

    def test_sample_above_4095_discarded(self):
        raw = p.build_sample(10000, 3300, [0, 5000, 4095], 1)
        frames = p.FrameParser().feed(raw)[0]
        self.assertEqual(len(frames), 1)  # CRC 层是合法帧
        sd = p.parse_sample_payload(frames[0].payload)
        self.assertFalse(sd.ok)           # 语义层判定数据损坏，UI 必须丢弃
        self.assertIn("4095", sd.reason)

    def test_sample_point_count_mismatch_discarded(self):
        # 手工构造：LEN=14(N=2) 但 payload 里 point_count 写成 3
        payload = struct.pack("<IHHH", 10000, 3, 3300, 0) + struct.pack("<2H", 1, 2)
        raw = p.encode_frame(p.TYPE_SAMPLE, 1, payload)
        frames = p.FrameParser().feed(raw)[0]
        self.assertEqual(len(frames), 1)
        sd = p.parse_sample_payload(frames[0].payload)
        self.assertFalse(sd.ok)


class TestParameterResponse(unittest.TestCase):
    """参数变更响应：status=0x02 忙时回读的是设备真实旧值，不得当作生效。"""

    def test_busy_response_fields(self):
        raw = p.build_response(p.CMD_SET_RATE, p.STATUS_BUSY,
                               10000, 3300, p.RUN_IDLE, 0, 0, 0x07)
        frames = p.FrameParser().feed(raw)[0]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].seq, 0x07)
        rd = p.parse_response_payload(frames[0].payload)
        self.assertTrue(rd.ok)
        self.assertEqual(rd.status, p.STATUS_BUSY)
        # status != OK 时仍填设备当前真实值（10000，而非请求的 100000）
        self.assertEqual(rd.actual_rate_hz, 10000)

    def test_event_frame(self):
        raw = p.build_event(p.EVENT_FRAME_DROPPED, 3, "frame dropped")
        frames = p.FrameParser().feed(raw)[0]
        self.assertEqual(frames[0].ftype, p.TYPE_EVENT)
        ev = p.parse_event_payload(frames[0].payload)
        self.assertEqual(ev.code, p.EVENT_FRAME_DROPPED)
        self.assertEqual(ev.text, "frame dropped")


@unittest.skipUnless(os.path.exists(C_SELFTEST),
                     "固件自检文件不存在，跳过 C 侧常量跨实现核对")
class TestCrossImplementationFromCSource(unittest.TestCase):
    """docs/protocol.md 第 8 节第 3 条：跨实现核对。

    解析 firmware/Tests/protocol_selftest.c 中人工写死的
    kGoldenSampleFrame[]（样例 B 完整 30 字节），用**电脑端自己的
    FrameParser** 解出全部字段与 CRC，不调用固件/本端任何编码器生成输入。
    C 侧常量文本 ←→ Python 解析器，构成"文档期望值 / Python / C 常量"
    三方独立的核对证据之一。
    """

    def _source(self):
        with open(C_SELFTEST, "r", encoding="utf-8") as f:
            return f.read()

    def _c_golden_frame(self):
        src = self._source()
        m = re.search(
            r"const\s+uint8_t\s+kGoldenSampleFrame\[\]\s*=\s*\{(.*?)\}\s*;",
            src, re.S)
        self.assertIsNotNone(m, "未找到 kGoldenSampleFrame[] 常量定义")
        body = m.group(1)
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)   # 去块注释
        body = re.sub(r"//[^\n]*", "", body)                  # 去行注释
        hexes = re.findall(r"0x([0-9A-Fa-f]{2})\b", body)
        return bytes(int(h, 16) for h in hexes)

    def test_c_constant_is_30_bytes_and_matches_document_bytes(self):
        raw = self._c_golden_frame()
        self.assertEqual(len(raw), 30, "整帧应为 LEN(18)+12=30 字节")
        self.assertEqual(raw.hex().upper(), GOLDEN_B,
                         "C 侧常量应与 docs/protocol.md 样例 B 逐字节一致")

    def test_c_constant_parsed_by_python_parser(self):
        raw = self._c_golden_frame()
        frames, errs = p.FrameParser().feed(raw)
        self.assertEqual(errs, [])
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f.ver, 0x01)
        self.assertEqual(f.ftype, p.TYPE_SAMPLE)
        self.assertEqual(f.seq, 0x2A)
        self.assertEqual(f.length, 18)
        self.assertEqual(f.crc, 0x7EA4)
        sd = p.parse_sample_payload(f.payload)
        self.assertTrue(sd.ok, sd.reason)
        self.assertEqual(sd.sample_rate_hz, 10000)
        self.assertEqual(sd.point_count, 4)
        self.assertEqual(sd.vref_mv, 3300)
        self.assertEqual(sd.samples, [0, 2048, 4095, 1234])

    def test_c_source_expects_0x7EA4_and_python_agrees(self):
        src = self._source()
        self.assertIn("0x7EA4", src, "C 侧应声明 CRC 期望 0x7EA4")
        raw = self._c_golden_frame()
        ln = raw[6] | (raw[7] << 8)
        self.assertEqual(ln, 18)
        self.assertEqual(p.crc16_ccitt_false(raw[4:4 + 6 + ln]), 0x7EA4)

    def test_four_crc_goldens(self):
        """四组 CRC golden：0x29B1 / 0x8278 / 0x7EA4 / 0xE73A。"""
        self.assertEqual(p.crc16_ccitt_false(b"123456789"), 0x29B1)
        for hx, want in ((GOLDEN_A, 0x8278), (GOLDEN_C, 0xE73A)):
            raw = bytes.fromhex(hx)
            ln = raw[6] | (raw[7] << 8)
            self.assertEqual(p.crc16_ccitt_false(raw[4:4 + 6 + ln]), want)
        c_raw = self._c_golden_frame()
        self.assertEqual(p.crc16_ccitt_false(c_raw[4:4 + 6 + 18]), 0x7EA4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
