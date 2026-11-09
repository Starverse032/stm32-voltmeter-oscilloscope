"""Codex integration regressions: feed real protocol frames into the actual window.

No physical serial/ADC measurements are performed by these tests.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
from PySide6.QtWidgets import QApplication
from main_window import MainWindow
import protocol as p


class TestWindowRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = MainWindow()
        self.w.mode = "sim"

    def tearDown(self):
        self.w.close()

    def sample(self, values, seq=1, rate=10000, vref=3300):
        self.w._feed_bytes(p.build_sample(rate, vref, values, seq))

    def test_single_waits_for_a_triggered_frame(self):
        self.w.trigger_combo.setCurrentIndex(1)
        self.w.single_capture()
        self.sample([1000] * 16)
        self.assertIsNone(self.w._display, "untriggered DC must not consume single capture")
        self.sample([0, 0, 4095, 4095] * 4, seq=2)
        self.assertEqual(self.w._display["seq"], 2)
        self.sample([0, 4095] * 8, seq=3)
        self.assertEqual(self.w._display["seq"], 2)

    def test_waiting_trigger_keeps_curve_stats_and_export_on_same_frame(self):
        self.w.trigger_combo.setCurrentIndex(1)
        self.sample([0, 4095] * 8, seq=11)
        self.sample([1000] * 16, seq=12)
        self.assertEqual(self.w._display["seq"], 11)

    def test_disconnected_sim_csv_retains_sim_provenance(self):
        self.sample([1000] * 16)
        self.w._on_disconnect("test")
        temp_root = Path(__file__).resolve().parents[2] / "work"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            path = Path(tmp) / "frame.csv"
            self.assertTrue(self.w.export_csv(str(path))[0])
            text = path.read_text(encoding="utf-8-sig")
        self.assertIn("模拟数据", text)
        self.assertNotIn("data_source,真实设备", text)

    def test_delayed_serial_frame_cannot_contaminate_simulation(self):
        frame = p.FrameParser().feed(p.build_sample(10000, 3300, [0]*8, 9))[0][0]
        self.w._on_serial_frame(frame)
        self.assertIsNone(self.w._display)

    def test_delayed_disconnect_does_not_stop_simulation(self):
        self.w._on_disconnected("old worker")
        self.assertEqual(self.w.mode, "sim")

    def test_real_single_ignores_inflight_frame_before_ack(self):
        self.w.mode = "real"
        self.w.single_capture()
        cmd = p.FrameParser().feed(self.w.worker._write_queue.get_nowait())[0][0]
        self.sample([0, 4095]*8, seq=20)
        self.assertIsNone(self.w._display)
        self.w._feed_bytes(p.build_response(p.CMD_SINGLE, p.STATUS_OK, 10000, 3300,
                                           p.RUN_SINGLE_BUSY, 0, 0, cmd.seq))
        self.sample([0, 4095]*8, seq=21)
        self.assertEqual(self.w._display["seq"], 21)

    def test_unrelated_rate_ack_does_not_cancel_pending_request(self):
        self.w.mode = "real"
        self.w._request_rate(100000)
        seq = self.w.rate_switch.pending.seq
        self.w._feed_bytes(p.build_response(p.CMD_SET_RATE, p.STATUS_OK, 100000,
                                           3300, p.RUN_IDLE, 0, 0, (seq+1) % 256))
        self.assertTrue(self.w.rate_switch.is_pending())
        self.assertEqual(self.w._current_rate, 10000)

    def test_invalid_sample_metadata_is_rejected_before_drawing(self):
        self.sample([1000]*16, rate=0)
        self.assertIsNone(self.w._display)
        self.sample([1000]*16, vref=0)
        self.assertIsNone(self.w._display)

    def test_switch_timeout_rolls_back_reference_editor(self):
        self.w.mode = "real"
        self.w.set_vref(3200)
        self.w._on_switch_timeout()
        self.assertEqual(self.w.vref_spin.value(), 3300)

    def pop_command(self):
        return p.FrameParser().feed(self.w.worker._write_queue.get_nowait())[0][0]

    def test_real_triggered_single_rearms_after_nontriggering_frame(self):
        self.w.mode = "real"
        self.w.trigger_combo.setCurrentIndex(1)
        self.w.single_capture()
        first = self.pop_command()
        self.w._feed_bytes(p.build_response(p.CMD_SINGLE, 0, 10000, 3300,
                                           p.RUN_SINGLE_BUSY, 0, 0, first.seq))
        self.sample([1000]*16)
        self.assertIsNone(self.w._display)
        second = self.pop_command()
        self.assertNotEqual(first.seq, second.seq)
        self.assertEqual(second.payload, bytes([p.CMD_SINGLE]))

    def test_rejected_single_does_not_consume_next_frame(self):
        self.w.mode = "real"
        self.w.single_capture()
        cmd = self.pop_command()
        self.w._feed_bytes(p.build_response(p.CMD_SINGLE, p.STATUS_BUSY, 10000,
                                           3300, p.RUN_IDLE, 0, 0, cmd.seq))
        self.sample([1000]*16)
        self.assertIsNone(self.w._display)

    def test_start_waits_for_matching_ack(self):
        self.w.mode = "real"
        self.w.resume()
        cmd = self.pop_command()
        self.sample([1000]*16)
        self.assertIsNone(self.w._display)
        self.w._feed_bytes(p.build_response(p.CMD_START, 0, 10000, 3300,
                                           p.RUN_RUN, 0, 0, cmd.seq))
        self.sample([1000]*16, seq=2)
        self.assertEqual(self.w._display["seq"], 2)

    def test_late_start_ack_cannot_override_a_newer_stop(self):
        self.w.mode = "real"
        self.w.resume()
        start = self.pop_command()
        self.w.pause()
        stop = self.pop_command()
        self.w._feed_bytes(p.build_response(p.CMD_START, 0, 10000, 3300,
                                           p.RUN_RUN, 0, 0, start.seq))
        self.assertEqual(self.w._control_pending, (stop.seq, p.CMD_STOP))
        self.sample([1000]*16)
        self.assertIsNone(self.w._display)

    def test_stop_timeout_reports_unknown_device_state(self):
        self.w.mode = "real"
        self.w.pause()
        self.w._on_control_timeout()
        self.assertIn("未知", self.w._run_state_text())

    def test_invalid_response_does_not_change_calibration(self):
        self.w._feed_bytes(p.build_response(p.CMD_QUERY_STATUS, 0, 0, 0,
                                           p.RUN_IDLE, 0, 0, 0))
        self.assertEqual(self.w._current_rate, 10000)
        self.assertEqual(self.w._current_vref, 3300)


if __name__ == "__main__":
    unittest.main()
