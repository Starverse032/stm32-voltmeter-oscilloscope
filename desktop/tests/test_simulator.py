# -*- coding: utf-8 -*-
"""模拟数据源单元测试：各场景必须走与真实数据完全一致的链路。

链路：Simulator.next_frame_bytes() → 字节流 → FrameParser → Frame →
parse_sample_payload → codes_to_volts / statistics / find_rising_trigger。
与串口真实数据共用同一套函数，不给 GUI 开小灶。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import processing as proc  # noqa: E402
import protocol as p  # noqa: E402
from simulator import Simulator, SCENE_NAMES  # noqa: E402


class TestSimulatorChain(unittest.TestCase):
    def _through_chain(self, sim):
        """把模拟帧字节喂给 FrameParser，再走完整处理链，返回结果。"""
        raw = sim.next_frame_bytes()
        if raw is None:
            return None
        frames, errs = p.FrameParser().feed(raw)
        self.assertEqual(errs, [])
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual(f.ftype, p.TYPE_SAMPLE)
        sd = p.parse_sample_payload(f.payload)
        self.assertTrue(sd.ok, sd.reason)
        self.assertGreater(sd.sample_rate_hz, 0)
        self.assertGreater(sd.vref_mv, 0)
        volts = proc.codes_to_volts(sd.samples, sd.vref_mv)
        stats = proc.statistics(volts)
        trig = proc.find_rising_trigger(volts, 1.65, sd.sample_rate_hz)
        return f, sd, volts, stats, trig

    def test_sine(self):
        r = self._through_chain(Simulator("sine", points=512))
        self.assertIsNotNone(r)
        _, sd, volts, stats, trig = r
        self.assertEqual(sd.point_count, 512)
        self.assertGreater(stats.vpp, 2.0)
        self.assertIsNotNone(trig)   # 正弦穿越 1.65V 阈值

    def test_square(self):
        r = self._through_chain(Simulator("square", points=512))
        self.assertIsNotNone(r)
        _, _, _, stats, _ = r
        self.assertGreater(stats.vpp, 2.0)

    def test_dc_no_trigger(self):
        r = self._through_chain(Simulator("dc", points=256))
        _, _, volts, stats, trig = r
        self.assertAlmostEqual(stats.vpp, 0.0)
        self.assertAlmostEqual(stats.mean, 2048 / 4095.0 * 3.3)
        self.assertIsNone(trig)      # 直流无触发

    def test_near_rails(self):
        r = self._through_chain(Simulator("near_rails", points=1024))
        _, _, volts, stats, _ = r
        self.assertGreater(stats.vmax, 3.1)   # 接近上限
        self.assertLess(stats.vmin, 0.2)      # 接近下限

    def test_no_trigger_scene(self):
        r = self._through_chain(Simulator("no_trigger", points=256))
        _, _, volts, stats, trig = r
        self.assertLess(stats.vmax, 1.0)
        self.assertIsNone(trig)

    def test_disconnect_scene(self):
        sim = Simulator("disconnect")
        self.assertIsNone(sim.next_frame_bytes())  # 断线不产帧，GUI 转"已断开"

    def test_short_frames_via_chain(self):
        """模拟源也能产生 1..1024 点合法短帧并走通同一链路。"""
        for n in (1, 2, 64, 256, 1024):
            r = self._through_chain(Simulator("sine", points=n))
            self.assertEqual(r[1].point_count, n)

    def test_scene_names_complete(self):
        for key in ("sine", "square", "dc", "near_rails", "no_trigger", "disconnect"):
            self.assertIn(key, SCENE_NAMES)


class TestSimulatedParameterResponse(unittest.TestCase):
    """模拟设备对 SET_RATE / SET_VREF 的应答也走响应解析 + 确认链路。"""

    def _parse(self, raw):
        frames, errs = p.FrameParser().feed(raw)
        self.assertEqual(errs, [])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].ftype, p.TYPE_RESPONSE)
        return frames[0], p.parse_response_payload(frames[0].payload)

    def test_rate_ok_response(self):
        sim = Simulator()
        f, rd = self._parse(sim.build_rate_response(0x07, 100000))
        sw = proc.ParameterSwitch()
        sw.arm(0x07, p.CMD_SET_RATE, 100000)
        ok, msg = sw.check(f.seq, rd)
        self.assertTrue(ok, msg)
        self.assertEqual(rd.actual_rate_hz, 100000)
        sim.set_rate(100000)
        self.assertEqual(sim.sample_rate_hz, 100000)

    def test_rate_busy_response_not_applied(self):
        """设备忙：响应回读旧值，ParameterSwitch 判定不生效（回滚）。"""
        sim = Simulator()
        sim.rate_status = p.STATUS_BUSY
        f, rd = self._parse(sim.build_rate_response(0x07, 100000))
        sw = proc.ParameterSwitch()
        sw.arm(0x07, p.CMD_SET_RATE, 100000)
        ok, msg = sw.check(f.seq, rd)
        self.assertFalse(ok)
        self.assertIn("忙", msg)
        self.assertEqual(rd.actual_rate_hz, 10000)  # 设备真实旧值，不是请求值

    def test_vref_response(self):
        sim = Simulator()
        f, rd = self._parse(sim.build_vref_response(0x08, 3000))
        sw = proc.ParameterSwitch()
        sw.arm(0x08, p.CMD_SET_VREF, 3000)
        ok, msg = sw.check(f.seq, rd)
        self.assertTrue(ok, msg)
        self.assertEqual(rd.vref_mv, 3000)
        sim.set_vref(3000)
        self.assertEqual(sim.vref_mv, 3000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
