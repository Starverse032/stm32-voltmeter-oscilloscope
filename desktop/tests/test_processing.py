# -*- coding: utf-8 -*-
"""数据处理层单元测试：换算 / 校准 / 时间轴 / 统计 / 触发 / 冻结 / 频率 / 参数确认。"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import processing as proc  # noqa: E402
import protocol as p  # noqa: E402


class TestConversion(unittest.TestCase):
    def test_zero_and_fullscale(self):
        v = proc.codes_to_volts([0, 4095], 3300)
        self.assertAlmostEqual(v[0], 0.0)
        self.assertAlmostEqual(v[1], 3.3)

    def test_vref_calibration(self):
        """不同 vref 校准：满量程码必须得到 vref/1000，电脑端不得假定 3.300V。"""
        for mv in (2500, 3000, 3300, 3600):
            v = proc.codes_to_volts([4095], mv)[0]
            self.assertAlmostEqual(v, mv / 1000.0)

    def test_mid_code(self):
        self.assertAlmostEqual(proc.codes_to_volts([2048], 3300)[0],
                               2048 / 4095.0 * 3.3)

    def test_resolution_note(self):
        """1 LSB @3.3V ≈ 0.806 mV，显示 3 位小数不冒充精度。"""
        self.assertAlmostEqual(proc.LSB_MV_AT_3300, 0.8059, places=3)
        self.assertEqual(proc.VOLT_DECIMALS, 3)


class TestTimeAxis(unittest.TestCase):
    def test_starts_at_zero_per_frame(self):
        t = proc.time_axis(4, 10000)
        self.assertEqual(t[0], 0.0)
        self.assertAlmostEqual(t[3], 3 / 10000.0)

    def test_rate_affects_axis(self):
        self.assertAlmostEqual(proc.time_axis(2, 100000)[1], 1e-5)

    def test_invalid_rate(self):
        self.assertEqual(proc.time_axis(4, 0), [])
        self.assertEqual(proc.time_axis(0, 10000), [])


class TestStatistics(unittest.TestCase):
    def test_mean_min_max_pp(self):
        s = proc.statistics([0.0, 1.0, 2.0, 3.0])
        self.assertEqual(s.count, 4)
        self.assertAlmostEqual(s.mean, 1.5)
        self.assertAlmostEqual(s.vmin, 0.0)
        self.assertAlmostEqual(s.vmax, 3.0)
        self.assertAlmostEqual(s.vpp, 3.0)

    def test_empty(self):
        self.assertIsNone(proc.statistics([]))


class TestRisingTrigger(unittest.TestCase):
    def test_dc_never_triggers(self):
        """直流（含恒定等于阈值）必须正确返回 None。"""
        dc = [1.65] * 100
        for th in (0.5, 1.65, 2.0):
            self.assertIsNone(proc.find_rising_trigger(dc, th, 10000))
        self.assertIsNone(proc.find_rising_trigger([3.0] * 50, 1.65, 10000))
        self.assertIsNone(proc.find_rising_trigger([0.1] * 50, 1.65, 10000))

    def test_known_signal_trigger_position(self):
        """已知上升沿信号的触发位置必须精确，且不伪造预触发数据。"""
        volts = [0.0] * 10 + [2.0] * 10
        r = proc.find_rising_trigger(volts, 1.0, 10000)
        self.assertIsNotNone(r)
        self.assertEqual(r.trigger_index, 10)
        self.assertAlmostEqual(r.times[10], 0.0)          # 触发点对齐到 t=0
        self.assertAlmostEqual(r.times[9], -1 / 10000.0)  # 之前的真实样本为负时间
        self.assertEqual(len(r.volts), 20)                # 保留本帧全部真实数据

    def test_threshold_above_range_no_trigger(self):
        self.assertIsNone(proc.find_rising_trigger([0.0, 1.0, 0.0, 1.0], 2.0, 10000))

    def test_too_few_points(self):
        self.assertIsNone(proc.find_rising_trigger([1.0], 1.0, 10000))


class TestFrequency(unittest.TestCase):
    def test_dc_unavailable(self):
        r = proc.estimate_frequency([1.65] * 100, 10000)
        self.assertFalse(r.ok)
        self.assertTrue(r.reason)

    def test_short_frame_unavailable(self):
        self.assertFalse(proc.estimate_frequency([0, 3, 0, 3], 10000).ok)

    def test_noise_unavailable(self):
        r = proc.estimate_frequency([0.02] * 100, 10000)
        self.assertFalse(r.ok)

    def test_known_sine(self):
        volts = [1.65 + 1.4 * math.sin(2 * math.pi * 50 * i / 10000)
                 for i in range(1000)]
        r = proc.estimate_frequency(volts, 10000)
        self.assertTrue(r.ok, r.reason)
        self.assertAlmostEqual(r.hz, 50.0, delta=2.0)

    def test_out_of_range(self):
        # 每点交替（约 Nyquist），应判越界或周期不足，不能给出虚假频率
        volts = [0.0 if i % 2 == 0 else 3.3 for i in range(1000)]
        self.assertFalse(proc.estimate_frequency(volts, 10000).ok)


class TestFrameHold(unittest.TestCase):
    """暂停 / 单次冻结后，后台新帧不得覆盖当前显示帧。"""

    def test_hold_keeps_last_frame(self):
        h = proc.FrameHold()
        self.assertTrue(h.submit("a"))
        h.set_mode(proc.FrameHold.MODE_HOLD)
        self.assertFalse(h.submit("b"))
        self.assertEqual(h.frame, "a")
        h.set_mode(proc.FrameHold.MODE_RUN)
        self.assertTrue(h.submit("c"))
        self.assertEqual(h.frame, "c")

    def test_single_freezes_after_first_frame(self):
        h = proc.FrameHold()
        h.submit("old")
        h.arm_single()
        self.assertIsNone(h.frame)          # 清空，等待下一帧
        self.assertTrue(h.submit("frame2"))
        self.assertFalse(h.submit("frame3"))  # 冻结，新帧不覆盖
        self.assertFalse(h.submit("frame4"))
        self.assertEqual(h.frame, "frame2")

    def test_run_overwrites(self):
        h = proc.FrameHold()
        h.submit(1)
        h.submit(2)
        self.assertEqual(h.frame, 2)


class TestParameterSwitch(unittest.TestCase):
    """只有 SEQ 匹配 + status OK + 回读正确才生效；busy 必须回滚。"""

    def _rd(self, cmd, status, rate, vref):
        return p.ResponseData(True, "", cmd, status, rate, vref, p.RUN_IDLE, 0, 0)

    def test_ok_applied(self):
        sw = proc.ParameterSwitch()
        sw.arm(0x07, p.CMD_SET_RATE, 100000, "采样率")
        self.assertTrue(sw.is_pending())
        ok, msg = sw.check(0x07, self._rd(p.CMD_SET_RATE, p.STATUS_OK, 100000, 3300))
        self.assertTrue(ok, msg)
        self.assertFalse(sw.is_pending())

    def test_busy_not_applied(self):
        """status=0x02 忙时不生效，并提示'设备忙，未切换'。"""
        sw = proc.ParameterSwitch()
        sw.arm(0x07, p.CMD_SET_RATE, 100000)
        ok, msg = sw.check(0x07, self._rd(p.CMD_SET_RATE, p.STATUS_BUSY, 10000, 3300))
        self.assertFalse(ok)
        self.assertIn("忙", msg)

    def test_seq_mismatch_not_applied(self):
        sw = proc.ParameterSwitch()
        sw.arm(0x07, p.CMD_SET_RATE, 100000)
        ok, msg = sw.check(0x08, self._rd(p.CMD_SET_RATE, p.STATUS_OK, 100000, 3300))
        self.assertFalse(ok)
        self.assertIn("SEQ", msg)

    def test_readback_mismatch_not_applied(self):
        sw = proc.ParameterSwitch()
        sw.arm(0x07, p.CMD_SET_RATE, 100000)
        ok, msg = sw.check(0x07, self._rd(p.CMD_SET_RATE, p.STATUS_OK, 10000, 3300))
        self.assertFalse(ok)
        self.assertIn("回滚", msg)

    def test_bad_param_status(self):
        sw = proc.ParameterSwitch()
        sw.arm(0x09, p.CMD_SET_VREF, 5000)
        ok, msg = sw.check(0x09, self._rd(p.CMD_SET_VREF, p.STATUS_BAD_PARAM, 10000, 3300))
        self.assertFalse(ok)

    def test_vref_ok_applied(self):
        sw = proc.ParameterSwitch()
        sw.arm(0x0A, p.CMD_SET_VREF, 3000)
        ok, _ = sw.check(0x0A, self._rd(p.CMD_SET_VREF, p.STATUS_OK, 10000, 3000))
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
