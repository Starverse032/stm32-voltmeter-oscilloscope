"""GUI and worker coverage with injected controllers and no device discovery."""
from dataclasses import replace
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from swd_instruments.contracts import (
    ActivationReport, ContinuityResult, PreflightReport, PulseResult, RecoveryReport,
)
from swd_instruments.gui import InstrumentWindow


class FakeController:
    def __init__(self):
        self.resources = dict(adc=True, pwm=True, meter=True, continuity=True)
        self.pulse = PulseResult(True, 'measured', .02, .0015, 50., 7.5, 1e-6, 'fake')
        self.continuity = ContinuityResult(True, True, 'fake')
        self.calls = 0
        self.entered = threading.Event()
        self.block = False
        self.cancelled = False
        self.closed = False
        self.thread_ids = []

    def preflight(self, cancel):
        return PreflightReport(True, (), {}, {}, self.resources, None, 'fake')

    def activate(self, cancel):
        return ActivationReport(True, (), self.resources['adc'], self.resources['pwm'],
                                False, {}, meter_ready=self.resources['meter'],
                                continuity_ready=self.resources['continuity'])

    def measure_pulse(self, timeout_s, cancel):
        self.thread_ids.append(threading.get_ident())
        self.calls += 1
        self.entered.set()
        if self.block:
            while not cancel.cancelled:
                time.sleep(.005)
            self.cancelled = True
        return self.pulse

    def check_continuity(self):
        self.thread_ids.append(threading.get_ident())
        return self.continuity

    def close(self):
        self.closed = True
        return RecoveryReport(True, (), (), 'not-owned')


class MeterGUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fake = FakeController()
        self.opener = patch('swd_instruments.gui.open_real_controller', return_value=self.fake)
        self.opener.start()
        self.port_guard = patch('swd_instruments.serial_terminal._default_port_lister',
                                side_effect=AssertionError('port discovery forbidden'))
        self.lister = self.port_guard.start()
        self.window = InstrumentWindow()
        self.window.serial.setText('FAKE-ONLY')
        self.window.show()

    def tearDown(self):
        self.window.close()
        self.wait_for(lambda: self.window.worker is None)
        self.window.close()
        self.app.processEvents()
        self.lister.assert_not_called()
        self.port_guard.stop()
        self.opener.stop()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertTrue(predicate(), self.window.real_text.toPlainText())

    def activate(self):
        self.assertFalse(self.window.meter_button.isEnabled())
        self.window.run_preflight()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertFalse(self.window.meter_button.isEnabled())
        self.window.activate_real()
        self.wait_for(lambda: self.window._pending_commands == 0)

    def test_profile_pin_titles_hints_and_warning(self):
        for filename, meter, adc in (('servo_reference.json', 'PA6', 'PA4'),
                                     ('wind-motor.json', 'PA6', 'PA7'),
                                     ('air-harp.json', '不启用', 'PA4'),
                                     ('lazy-rescue.json', '不启用', 'PA4'),
                                     ('running-led.json', '不启用', 'PB0')):
            for index in range(self.window.profile_combo.count()):
                if Path(self.window.profile_combo.itemData(index)).name == filename:
                    self.window.profile_combo.setCurrentIndex(index)
                    break
            self.assertIn(meter, self.window.meter_title.text())
            self.assertIn('PB10→GND', self.window.continuity_title.text())
            self.assertIn('PB10', self.window.continuity_pin_hint.text())
            self.assertIn(meter, self.window.info_label.text())
            if meter == 'PA6':
                self.assertIn('0–3.3 V', self.window.meter_pin_hint.text())
                self.assertIn('不耐受 5 V', self.window.meter_pin_hint.text())
                self.assertIn('HC-SR04 Echo', self.window.meter_pin_hint.text())
                self.assertFalse(self.window.meter_pin_hint.isHidden())
            if adc:
                self.assertIn(adc, self.window.adc_pin_hint.text())

    def test_measured_units_and_no_signal_clears_numbers(self):
        self.activate()
        self.assertTrue(self.window.meter_button.isEnabled())
        self.window.measure_pulse()
        self.wait_for(lambda: self.window._pending_commands == 0)
        text = self.window.meter_result.text()
        for value in ('50 Hz', '20 ms', '1.5 ms', '7.5%', '1 µs'):
            self.assertIn(value, text)
        self.assertIn('1.5 ms', self.window.meter_high.text())
        self.fake.pulse = replace(self.fake.pulse, ok=False, state='no-signal')
        self.window.measure_pulse()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertEqual(self.window.meter_result.text(), '无信号')
        self.assertNotRegex(self.window.meter_high.text(), r'\d')
        self.assertIn('无信号', self.window.real_text.toPlainText())
        self.assertNotIn(threading.get_ident(), self.fake.thread_ids)

    def test_continuity_results(self):
        self.activate()
        for closed, ok, expected in [(True, True, '导通'), (False, True, '开路'),
                                     (None, False, '不确定')]:
            self.fake.continuity = ContinuityResult(ok, closed, 'fake')
            self.window.check_continuity()
            self.wait_for(lambda: self.window._pending_commands == 0)
            self.assertEqual(self.window.continuity_result.text(), expected)

    def test_continuity_log_has_one_state_and_one_line(self):
        self.activate()
        for result, expected in (
            (ContinuityResult(True, False, '开路（PB10高电平）'), '开路（PB10高电平）'),
            (ContinuityResult(True, True, '导通至GND（PB10低电平）'), '导通至GND（PB10低电平）'),
            (ContinuityResult(True, None, '电平不稳定'), '不确定（电平不稳定）'),
            (ContinuityResult(False, None, ''), '不确定'),
        ):
            self.window.real_text.clear()
            self.fake.continuity = result
            self.window.check_continuity()
            self.wait_for(lambda: self.window._pending_commands == 0)
            self.assertEqual(self.window.real_text.toPlainText(), f'{self.window.continuity_title.text()}：{expected}')

    def test_meter_log_does_not_repeat_driver_state(self):
        self.activate()
        for state, caption, message in (
            ('measured', '已测得', '已测得（硬件捕获）'),
            ('no-signal', '无信号', '无信号（期限内无完整周期）'),
            ('out-of-range', '超出范围', '超出范围（计数溢出）'),
            ('not-ready', '不可用', '未授权'),
            ('error', '错误', '错误（计数器停止未确认）'),
        ):
            self.window.real_text.clear()
            self.fake.pulse = replace(self.fake.pulse, state=state, ok=state == 'measured', message=message)
            self.window.measure_pulse()
            self.wait_for(lambda: self.window._pending_commands == 0)
            log = self.window.real_text.toPlainText()
            self.assertTrue(log.startswith(self.window.meter_title.text() + '：'), log)
            self.assertEqual(log.count(caption), 1, log)
            self.assertIn(message, log)
            if state == 'measured':
                self.assertIn('50 Hz', log)
                self.assertIn('1.5 ms', log)

    def test_active_real_page_wrapped_labels_have_room(self):
        self.window.mode_tabs.setCurrentIndex(1)
        self.activate()
        page = self.window.mode_tabs.widget(1)
        for width, height in ((1360, 920), (1120, 700), (1600, 1000)):
            self.window.resize(width, height)
            self.window.profile_notes.setText('较长的接线提示，检查换行和动态布局。' * 20)
            self.window.real_pwm_status.setText('真实输出状态未知，请检查外部电路。' * 10)
            self.window.measure_pulse()
            self.wait_for(lambda: self.window._pending_commands == 0)
            QTest.qWait(20)
            for label in page.findChildren(QLabel):
                if label.isVisible() and label.wordWrap():
                    self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()), label.text())

    def test_profile_selection_locked_until_recovery(self):
        self.assertTrue(self.window.profile_combo.isEnabled())
        self.window.run_preflight()
        self.assertFalse(self.window.profile_combo.isEnabled())
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertFalse(self.window.profile_combo.isEnabled())
        self.assertEqual(self.window.adc_pin_hint.text(), 'ADC 输入：PA4（仅 0–3.3 V）')
        self.window.activate_real()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertFalse(self.window.profile_combo.isEnabled())
        self.window.recover_real()
        self.wait_for(lambda: self.window.worker is None)
        self.assertTrue(self.window.profile_combo.isEnabled())

    def test_reason_labels_only_reserve_space_when_present(self):
        self.fake.resources.update(adc=False, pwm=False, meter=False, continuity=False)
        self.activate()
        labels = (self.window.sample_reason, self.window.pwm_reason,
                  self.window.meter_reason, self.window.continuity_reason)
        for label in labels:
            self.assertTrue(label.text())
            self.assertFalse(label.isHidden())
        self.fake.resources.update(adc=True, pwm=True, meter=True, continuity=True)
        self.window.run_preflight()
        self.wait_for(lambda: self.window._pending_commands == 0)
        for label in labels:
            self.assertEqual(label.text(), '')
            self.assertTrue(label.isHidden())

    def test_readiness_required_even_when_resource_available(self):
        original = self.fake.activate

        def activate(cancel):
            return replace(original(cancel), meter_ready=False, continuity_ready=False)

        self.fake.activate = activate
        self.activate()
        self.assertFalse(self.window.meter_button.isEnabled())
        self.assertFalse(self.window.continuity_button.isEnabled())
        self.assertTrue(self.window.sample_button.isEnabled())
        self.assertTrue(self.window.real_pwm_button.isEnabled())
        self.window.measure_pulse()
        self.assertEqual(self.fake.calls, 0)

    def test_profile_labels_preserve_paths_and_exclude_metadata(self):
        choices = [self.window.profile_combo.itemText(i) for i in range(self.window.profile_combo.count())]
        self.assertTrue(any('servo_reference.json' in label for label in choices))
        self.assertFalse(any('reference_artifact.json' in label for label in choices))
        for index, label in enumerate(choices):
            if self.window.profile_combo.itemData(index) == '__general__':
                self.assertEqual(label, '通用模式（未登记固件）')
                continue
            self.assertIn(Path(self.window.profile_combo.itemData(index)).name, label)

    def test_each_function_unavailable_is_independent(self):
        self.fake.resources.update(adc=False, pwm=False, meter=False,
                                   reasons={'adc': 'ADC被占用', 'meter': 'PB7被占用'})
        self.activate()
        self.assertFalse(self.window.sample_card.isEnabled())
        self.assertFalse(self.window.pwm_card.isEnabled())
        self.assertFalse(self.window.meter_card.isEnabled())
        self.assertIn('ADC被占用', self.window.sample_reason.text())
        self.assertIn('PB7被占用', self.window.meter_reason.text())
        self.assertIn('未授权', self.window.pwm_reason.text())
        self.assertTrue(self.window.continuity_button.isEnabled())
        self.assertIn('ADC 不可用', self.window.statistics.text())
        self.window.check_continuity()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertEqual(self.window.continuity_result.text(), '导通')

    def test_continuous_recover_cancels_long_measurement(self):
        self.activate()
        self.fake.block = True
        self.window.toggle_meter()
        self.wait_for(self.fake.entered.is_set)
        self.window.recover_real()
        self.wait_for(lambda: self.window.worker is None)
        self.assertTrue(self.fake.cancelled)
        self.assertTrue(self.fake.closed)
        QTest.qWait(550)
        self.assertEqual(self.fake.calls, 1)
        self.assertFalse(self.window._meter_timer.isActive())
        self.assertFalse(self.window._meter_continuous)

    def test_continuous_rate_pause_and_error(self):
        self.activate()
        self.window.toggle_meter()
        self.wait_for(lambda: self.fake.calls == 1 and self.window._pending_commands == 0)
        QTest.qWait(200)
        self.assertEqual(self.fake.calls, 1)
        self.window.pause()
        QTest.qWait(550)
        self.assertEqual(self.fake.calls, 1)
        self.fake.pulse = replace(self.fake.pulse, ok=False, state='error')
        self.window.toggle_meter()
        self.wait_for(lambda: self.window._pending_commands == 0)
        self.assertFalse(self.window._meter_continuous)
        self.assertEqual(self.window.meter_result.text(), '错误')

    def test_close_cancels_meter_then_shuts_down_serial(self):
        self.activate()
        self.fake.block = True
        self.window.measure_pulse()
        self.wait_for(self.fake.entered.is_set)
        with patch.object(self.window.serial_terminal, 'shutdown') as shutdown:
            self.window.close()
            self.wait_for(lambda: self.window.worker is None and not self.window.isVisible())
            shutdown.assert_called_once()
        self.assertTrue(self.fake.cancelled)

    def test_serial_tab_and_minimum_size(self):
        self.assertIn('串口调试', [self.window.tabs.tabText(i) for i in range(self.window.tabs.count())])
        self.window.resize(1120, 700)
        self.app.processEvents()
        self.assertEqual(self.window.width(), 1120)
        self.assertEqual(self.window.height(), 700)
        with patch.object(self.window.serial_terminal, 'shutdown') as shutdown:
            self.window.close()
            shutdown.assert_called_once()


if __name__ == '__main__':
    unittest.main()
