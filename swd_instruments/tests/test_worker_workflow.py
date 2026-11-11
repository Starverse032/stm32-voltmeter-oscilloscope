"""Run the GUI -> QThread -> actual controller flow with an emulated F1 only."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from swd_instruments import core as c
from swd_instruments.gui import InstrumentWindow
from swd_instruments.hardware import RealDeviceController
from swd_instruments.profiles import load_profile
from test_hardware import FakeF1Transport, build_image, write_profile


class WorkerWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.image = build_image()
        profile = load_profile(write_profile(Path(self.tmp.name), self.image))
        self.fake = FakeF1Transport(self.image)
        self.initial = dict(self.fake.registers)
        self.controller = RealDeviceController(self.fake, profile)
        self.opener = patch('swd_instruments.gui.open_real_controller', return_value=self.controller)
        self.open_mock = self.opener.start()
        self.window = InstrumentWindow()
        self.window.show()
        self.window.serial.setText('TEST-EMULATED-NO-USB')

    def tearDown(self):
        self.window.close()
        self.wait_for(lambda: self.window.worker is None)
        self.window.close()
        self.app.processEvents()
        self.opener.stop()
        self.tmp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertTrue(predicate(), self.window.real_text.toPlainText())

    def activate(self):
        self.window.run_preflight()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertTrue(self.window._preflight_ok)
        self.assertEqual(self.fake.write_log, [])
        self.window.activate_real()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertTrue(self.window._real_activated)
        self.assertTrue(self.window.sample_button.isEnabled())
        self.assertFalse(self.window.activate_button.isEnabled())

    def assert_restored(self):
        for address in (c.GPIOA_CRL, c.GPIOA_CRH, c.RCC_APB2ENR, c.ADC2_CR2,
                        c.TIM1_CR1, c.TIM1_CCER, c.TIM1_BDTR):
            self.assertEqual(self.fake.registers[address], self.initial[address])
        self.assertEqual(bytes(self.fake.flash), self.image)
        self.assertNotIn(c.ADC1_DR, self.fake.read_log)
        self.assertTrue(all(entry[0] in c.REAL_WRITE_MASKS for entry in self.fake.write_log))
        self.assertEqual(self.fake.closed, 1)

    def test_preflight_pwm_sample_csv_output_off_and_recovery(self):
        self.activate()
        self.window.set_real_pwm()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertIn('开启', self.window.real_pwm_status.text())
        self.window.sample_points.setValue(4)
        self.window.sample_interval.setValue(0)
        self.window.single_real_sampling()
        self.wait_for(lambda: self.window._pending_commands == 0)
        frame = self.window.current_frame
        self.assertIsNotNone(frame, self.window.real_text.toPlainText())
        self.assertTrue(frame.is_real)
        self.assertEqual(len(frame.points), 4)
        self.assertEqual(tuple(self.window.curve.getData()[0]), frame.relative_times)
        output = Path(self.tmp.name) / 'frame.csv'
        with patch('swd_instruments.gui.QFileDialog.getSaveFileName', return_value=(str(output), 'CSV')):
            self.window.save_csv()
        csv = output.read_text(encoding='utf-8-sig')
        self.assertIn(c.REAL_SOURCE, csv)
        self.assertIn('time_uncertainty_s', csv)
        self.window.real_output_off()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertIn('关闭已读回确认', self.window.real_pwm_status.text())
        self.window.recover_real()
        self.wait_for(lambda: self.window.worker is None)
        self.assert_restored()
        self.assertIs(self.window.current_frame, frame)
        self.open_mock.assert_called_once()
        from swd_instruments.transport import ALLOWED_SWD_HZ, DEFAULT_SWD_HZ
        self.assertEqual(self.open_mock.call_args.kwargs['swd_hz'], DEFAULT_SWD_HZ)
        offered = [self.window.swd_clock.itemData(i) for i in range(self.window.swd_clock.count())]
        self.assertEqual(offered[0], DEFAULT_SWD_HZ)
        self.assertEqual(sorted(offered), sorted(ALLOWED_SWD_HZ))

    def test_window_close_cancels_inflight_sample_then_restores(self):
        self.activate()
        entered = threading.Event()
        actual_sample = self.controller.sample

        def sample(request, cancel):
            entered.set()
            return actual_sample(request, cancel)

        self.controller.sample = sample
        self.window.sample_points.setValue(128)
        self.window.sample_interval.setValue(1)
        self.window.single_real_sampling()
        self.wait_for(entered.is_set)
        self.window.close()
        self.wait_for(lambda: self.window.worker is None and not self.window.isVisible())
        self.assertIsNone(self.window.current_frame)
        self.assert_restored()


if __name__ == '__main__':
    unittest.main()
