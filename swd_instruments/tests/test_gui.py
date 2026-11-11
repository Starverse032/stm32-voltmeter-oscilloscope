from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from swd_instruments.gui import InstrumentWindow
from swd_instruments.profiles import ProfileError, load_profile


class WindowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = InstrumentWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def test_initial_state_does_not_connect_or_create_data(self):
        self.assertIsNone(self.window.current_frame)
        self.assertIsNone(self.window.worker)
        self.assertFalse(self.window.timer.isActive())
        self.assertIn('未连接', self.window.source.text())

    def select_profile(self, filename):
        for index in range(self.window.profile_combo.count()):
            if Path(self.window.profile_combo.itemData(index)).name == filename:
                self.window.profile_combo.setCurrentIndex(index)
                return
        self.fail(f'Profile missing: {filename}')

    def test_profile_pin_hints_without_connection(self):
        with patch('swd_instruments.gui.open_real_controller') as opener:
            for filename, text in [('servo_reference.json', 'ADC 输入：PA4（仅 0–3.3 V）'),
                                   ('wind-motor.json', 'ADC 输入：PA7（仅 0–3.3 V）'),
                                   ('running-led.json', 'ADC 输入：PB0（仅 0–3.3 V）')]:
                self.select_profile(filename)
                self.assertEqual(self.window.adc_pin_hint.text(), text)
                self.assertFalse(self.window.sample_button.isEnabled())
                self.assertIsNone(self.window.worker)
            opener.assert_not_called()

    def test_profile_grants_and_notes_are_shown(self):
        self.select_profile('servo_reference.json')
        for text in ('电压采样 PA4', 'PWM 输出 PA8', '频率计 PA6', '通断 PB10', '预检'):
            self.assertIn(text, self.window.profile_hint.text())
        self.select_profile('air-harp.json')
        profile = load_profile(Path(self.window.profile_combo.currentData()))
        self.assertNotIn('PWM 输出 PA8', self.window.profile_hint.text())
        self.assertTrue(profile.notes)
        self.assertEqual(self.window.profile_notes.text(), '\n'.join(profile.notes))
        self.assertFalse(self.window.profile_notes.isHidden())

    def test_profile_load_errors_are_displayed(self):
        for error in (ProfileError('invalid review'), OSError('unreadable profile')):
            with patch('swd_instruments.gui.load_profile', side_effect=error):
                self.window._on_profile_changed()
            self.assertIn(str(error), self.window.profile_hint.text())
            self.assertIn('待确认', self.window.adc_pin_hint.text())
            self.assertTrue(self.window.profile_notes.isHidden())

    def test_default_profile_and_opt_in_persistence(self):
        self.assertEqual(Path(self.window.profile_combo.currentData()).name, 'servo_reference.json')
        with patch.object(self.window, '_settings') as settings:
            self.select_profile('wind-motor.json')
            settings.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            settings = QSettings(str(Path(folder) / 'gui.ini'), QSettings.IniFormat)
            settings.setValue('profile', 'wind-motor.json')
            settings.setValue('serial', 'FAKE-SAVED-SERIAL')
            with patch('swd_instruments.gui.InstrumentWindow._settings', return_value=settings):
                restored = InstrumentWindow()
                try:
                    self.assertEqual(Path(restored.profile_combo.currentData()).name, 'servo_reference.json')
                    restored.enable_persistence()
                    self.assertEqual(Path(restored.profile_combo.currentData()).name, 'wind-motor.json')
                    self.assertEqual(restored.serial.text(), 'FAKE-SAVED-SERIAL')
                    self.assertIn('PA7', restored.adc_pin_hint.text())
                    next_path = Path(restored.profile_combo.currentData()).with_name('running-led.json')
                    restored.profile_combo.setCurrentIndex(restored.profile_combo.findData(str(next_path)))
                    self.assertEqual(settings.value('profile'), 'running-led.json')
                    self.assertIsNone(restored.worker)
                finally:
                    restored.close()

    def test_stale_saved_profile_falls_back_to_servo(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = QSettings(str(Path(folder) / 'gui.ini'), QSettings.IniFormat)
            settings.setValue('profile', 'missing.json')
            with patch.object(self.window, '_settings', return_value=settings):
                self.window.enable_persistence()
                self.assertEqual(Path(self.window.profile_combo.currentData()).name, 'servo_reference.json')

    def test_empty_reason_labels_are_hidden(self):
        for label in (self.window.sample_reason, self.window.pwm_reason,
                      self.window.meter_reason, self.window.continuity_reason):
            self.assertEqual(label.text(), '')
            self.assertTrue(label.isHidden())

    def test_real_page_wrapped_labels_have_room_before_connection(self):
        self.window.mode_tabs.setCurrentIndex(1)
        page = self.window.mode_tabs.widget(1)
        for width, height in ((1360, 920), (1120, 700), (1600, 1000)):
            self.window.resize(width, height)
            for filename in ('servo_reference.json', 'air-harp.json', 'wind-motor.json'):
                self.select_profile(filename)
                QTest.qWait(20)
                for label in page.findChildren(QLabel):
                    if label.isVisible() and label.wordWrap():
                        self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()),
                                                label.text())

    def test_pause_and_single_freeze_entire_frame(self):
        self.window.start()
        self.window.pause()
        frame = self.window.current_frame
        QTest.qWait(260)
        self.assertIs(self.window.current_frame, frame)
        self.window.single()
        single = self.window.current_frame
        self.assertGreater(single.sequence, frame.sequence)
        QTest.qWait(260)
        self.assertIs(self.window.current_frame, single)
        self.assertFalse(self.window.timer.isActive())

    def test_plot_uses_frame_timestamps(self):
        self.window.single()
        x, y = self.window.curve.getData()
        self.assertEqual(tuple(x), self.window.current_frame.relative_times)
        self.assertEqual(tuple(y), self.window.current_frame.volts)

    def test_export_keeps_frozen_reference_and_source(self):
        self.window.single()
        frame = self.window.current_frame
        self.window.disconnect()
        self.window.vref.setValue(3000)
        root = Path(__file__).resolve().parents[2] / 'work'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            name = str(Path(tmp) / 'history.csv')
            with patch('swd_instruments.gui.QFileDialog.getSaveFileName', return_value=(name, 'CSV')):
                self.window.save_csv()
            data = Path(name).read_text(encoding='utf-8-sig')
            self.assertIn('# vref_mv,3300', data)
            self.assertIn('模拟数据', data)
            self.assertIs(self.window.current_frame, frame)
            self.assertIn('历史', self.window.source.text())

    def test_generator_only_changes_simulation(self):
        self.window.frequency.setValue(100)
        self.window.duty.setValue(100)
        self.window.enable_sim_output()
        self.window.single()
        self.assertEqual(set(point.adc_code for point in self.window.current_frame.points), {4095})
        self.assertIn('仅更新模拟器', self.window.output_status.text())
        self.assertIsNone(self.window.worker)
        self.window.disable_sim_output()
        self.window.single()
        self.assertEqual(set(point.adc_code for point in self.window.current_frame.points), {0})

    def test_diagnostic_requires_explicit_serial(self):
        self.window.start_diagnostic()
        self.assertIsNone(self.window.worker)
        self.assertIn('没有连接', self.window.diagnostic_text.toPlainText())

    def test_diagnostic_failure_does_not_become_measurement(self):
        self.window.single()
        frame = self.window.current_frame
        self.window.serial.setText('mock-only')
        with patch('swd_instruments.gui.PyOCDDiagnostic', side_effect=RuntimeError('mock: no pyocd')):
            self.window.start_diagnostic()
            for _ in range(50):
                QTest.qWait(10)
                if self.window.worker is None:
                    break
        self.assertIsNone(self.window.worker)
        self.assertIn('no pyocd', self.window.diagnostic_text.toPlainText())
        self.assertIs(self.window.current_frame, frame)
        self.assertIn('模拟数据', self.window.source.text())

    def test_close_is_refused_while_diagnostic_is_running(self):
        self.window.worker = types.SimpleNamespace(isRunning=lambda: True)
        self.assertFalse(self.window.close())
        self.assertIn('不会强杀', self.window.statusBar().currentMessage())
        self.window.worker = None
        self.assertTrue(self.window.close())


if __name__ == '__main__':
    unittest.main()
