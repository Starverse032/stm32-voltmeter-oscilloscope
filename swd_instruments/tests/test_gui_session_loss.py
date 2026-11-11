"""GUI 对“会话被驱动撤销”“测量被取消”“无信号换档提示”“USB 断开”的处理。只用假控制器。"""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gui_meter import FakeController, MeterGUI  # noqa: E402

from swd_instruments.contracts import PreflightReport  # noqa: E402


def pulse(state, ok=False, session_active=True, message=''):
    # 用简单对象模拟驱动结果，不依赖 PulseResult 是否已含 session_active 字段。
    return types.SimpleNamespace(ok=ok, state=state, period_s=None, high_s=None,
                                 frequency_hz=None, duty_percent=None, tick_s=None,
                                 message=message, session_active=session_active)


class SessionLossGUI(unittest.TestCase):
    # 复用 MeterGUI 的准备、收尾和等待方法，但不继承它的测试用例。
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    setUp = MeterGUI.setUp
    tearDown = MeterGUI.tearDown
    wait_for = MeterGUI.wait_for
    activate = MeterGUI.activate

    def test_meter_error_that_released_session_deactivates_gui(self):
        self.activate()
        self.assertTrue(self.window.sample_button.isEnabled())
        self.fake.pulse = pulse('error', session_active=False, message='脉冲测量失败')
        self.window.measure_pulse()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertFalse(self.window._real_activated)
        self.assertFalse(self.window.sample_button.isEnabled())
        self.assertFalse(self.window.real_pwm_button.isEnabled())
        self.assertFalse(self.window.meter_button.isEnabled())
        self.assertTrue(self.window.recover_button.isEnabled())
        self.assertIn('会话已失效', self.window.real_text.toPlainText())
        self.assertIn('断开并恢复', self.window.statusBar().currentMessage())

    def test_session_loss_is_noticed_even_when_result_is_ignored(self):
        self.activate()
        self.window._accept_pulse = False
        self.window._pending_commands += 1
        self.window._on_pulse(pulse('error', session_active=False))
        self.assertFalse(self.window._real_activated)
        self.assertIn('会话已失效', self.window.real_text.toPlainText())

    def test_controller_reporting_inactive_is_detected_after_any_command(self):
        self.activate()
        self.fake.is_active = False
        self.window.check_continuity()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertFalse(self.window._real_activated)
        self.assertFalse(self.window.continuity_button.isEnabled())

    def test_cancelled_measurement_keeps_session_and_shows_no_error(self):
        self.activate()
        before = self.window.meter_result.text()
        self.window.real_text.clear()
        self.fake.pulse = pulse('cancelled', message='测量已取消')
        self.window.measure_pulse()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertTrue(self.window._real_activated)
        self.assertTrue(self.window.sample_button.isEnabled())
        self.assertTrue(self.window.meter_button.isEnabled())
        self.assertEqual(self.window.meter_result.text(), before)
        self.assertNotIn('错误', self.window.real_text.toPlainText())

    def test_no_signal_on_fast_timeout_suggests_slow_range(self):
        self.activate()
        self.window.meter_timeout.setCurrentIndex(0)
        self.fake.pulse = pulse('no-signal', message='期限内无有效边沿')
        self.window.measure_pulse()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertIn('慢信号 10 s', self.window.strip_meter_state.text())
        self.assertEqual(self.window.real_text.toPlainText().count('无信号'), 1)
        self.window.meter_timeout.setCurrentIndex(2)
        self.window.measure_pulse()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertNotIn('慢信号 10 s', self.window.strip_meter_state.text())

    def test_usb_error_in_preflight_explains_replug(self):
        issue = 'USB Error: [Errno 5] Input/Output Error'
        with patch.object(FakeController, 'preflight',
                          lambda self, cancel: PreflightReport(False, (issue,), {}, {}, {}, None, 'fake')):
            self.window.run_preflight()
            self.wait_for(lambda: self.window._pending_commands == 0)
        log = self.window.real_text.toPlainText()
        self.assertIn(issue, log)
        self.assertIn('重新插拔 ST-Link', log)
        self.assertIn('重新插拔 ST-Link', self.window.statusBar().currentMessage())

    def test_usb_hint_ignores_ordinary_errors(self):
        self.assertEqual(self.window._usb_hint('SafetyError: ADC2未激活'), '')
        self.assertTrue(self.window._usb_hint('LIBUSB_ERROR_IO'))


del MeterGUI  # 避免 unittest 在本模块里再收集一遍原测试类。

if __name__ == '__main__':
    unittest.main()
