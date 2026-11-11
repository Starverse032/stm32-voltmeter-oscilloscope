"""Task H: fake target only. No probe discovery or real transport open."""
import hashlib
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import tempfile

from swd_instruments import core as c
from swd_instruments.pins import CANDIDATES, ADC, PWM, METER, CONTINUITY, TIMER_BASES, OFFSETS, timer_registers
from swd_instruments.selectable import SelectableController
from swd_instruments.transport import PyOCDSWD
from swd_instruments.profiles import load_profile
from swd_instruments.contracts import SampleRequest
from test_hardware import FakeF1Transport, build_image


class Target:
    def __init__(self):
        self.fake = FakeF1Transport(build_image())
        self.fake.read_latency_s = .00001
        self.fake.registers[c.GPIOA_CRL] = 0x44444444
        self.fake.registers[c.GPIOA_CRH] = 0x44444444
        self.fake.registers[c.RCC_APB2ENR] = 0
        self.fake.registers[c.ADC1_CR2] = 0
        for timer in TIMER_BASES:
            for name in OFFSETS:
                if name != 'EGR':
                    self.fake.registers.setdefault(getattr(timer_registers(timer), name), 0)
        self.fake.registers.setdefault(0x4001080C, 0)

    def is_running(self):
        return self.fake.running

    def read_memory_block8(self, address, size):
        return self.fake.read_flash_block(address, size)

    def read32(self, address):
        signal = getattr(self, 'signal', None)
        if signal:
            timer, channel, period, high = signal
            t = timer_registers(timer)
            r = self.fake.registers
            if r[t.CR1] & 1 and r[t.CCER] & 0x11 == 0x11:
                clocks = c.derive_clocks(r[c.RCC_CFGR], 8_000_000)
                hz = clocks.tim1_hz if timer == 'TIM1' else clocks.tim3_hz
                tick = (r[t.PSC] + 1) / hz
                r[getattr(t, f'CCR{channel}')] = max(0, round(period / tick) - 1)
                r[getattr(t, f'CCR{3 - channel}')] = max(0, round(high / tick) - 1)
                if period / tick > 65536:
                    r[t.SR] |= 1
                else:
                    r[t.SR] |= 6
                    # This fixture injects completed captures. Keep the underlying
                    # edge model's reset epoch in sync with those injected cycles.
                    if timer == 'TIM3':
                        self.fake._reset3_at = self.fake._timer3_start = self.fake.now
                    elif timer == 'TIM4':
                        self.fake._reset_at = self.fake._timer_start = self.fake.now
            if address in (t.CCR1, t.CCR2):
                value = r[address]
                r[t.SR] &= ~(2 if address == t.CCR1 else 4)
                self.fake.advance(self.fake.read_latency_s)
                return value
        if address == c.ADC2_DR:
            self.fake.registers[c.ADC2_SR] &= ~c.ADC_SR_EOC
            return self.fake.adc_code
        return self.fake.read32(address)

    def write32(self, address, value):
        # Model raw command semantics and rc_w0 independently of transport RMW.
        for base in (0x40010800, 0x40010C00):
            if address in (base + 16, base + 20):
                if address == base + 16:
                    self.fake.registers[base + 12] |= value
                else:
                    self.fake.registers[base + 12] &= ~value
                return
        for timer in TIMER_BASES:
            t = timer_registers(timer)
            if address == t.SR:
                self.fake.registers[address] &= value
                return
            if address == t.EGR:
                if not self.fake.registers[t.CR1] & 4:
                    self.fake.registers[t.SR] |= 1
                return
        self.fake.write_masked(address, c.REAL_WRITE_MASKS[address], value & c.REAL_WRITE_MASKS[address])


class SelectionTests(unittest.TestCase):
    def make(self, selection=None, profile=None):
        self.target = Target()
        self.transport = PyOCDSWD('FAKE-ONLY')
        self.transport.session = SimpleNamespace(target=self.target, close=lambda: None)
        return SelectableController(self.transport, profile, selection, profile is None,
            monotonic=lambda: self.target.fake.now, sleep=self.target.fake.advance)

    def test_tables_and_header(self):
        header = (Path(__file__).resolve().parent / 'data' / 'stm32f10x.h').read_text(encoding='latin-1')
        timer = header[:header.index('} TIM_TypeDef;')].rsplit('typedef struct', 1)[1]
        members = re.findall(r'(?:__IO )?uint16_t\s+(\w+);', timer)
        for name, offset in OFFSETS.items():
            self.assertEqual(offset, members.index(name) * 2)
        for timer, offset in (('TIM1', '2C00'), ('TIM2', '0000'), ('TIM3', '0400'), ('TIM4', '0800')):
            self.assertRegex(header, timer + r'_BASE\s+\(APB[12]PERIPH_BASE \+ 0x' + offset + r'\)')
        self.assertEqual([p.channel for p in ADC], list(range(10)))
        self.assertEqual((len(PWM), len(METER), len(CONTINUITY)), (16, 8, 27))
        for entries in CANDIDATES.values():
            self.assertFalse({'PA13', 'PA14', 'PA15', 'PB3', 'PB4'} & {p.pin for p in entries})
        self.assertFalse(next(p for p in CONTINUITY if p.pin == 'PB5').ft)
        self.assertTrue(next(p for p in CONTINUITY if p.pin == 'PB2').ft)

    def test_general_confirmation_hash_and_selection_lease(self):
        ctrl = self.make(dict(adc='PA4', pwm=None, meter=None, continuity=None))
        pre = ctrl.preflight()
        self.assertTrue(pre.ok, pre)
        self.assertEqual(pre.firmware_sha256, hashlib.sha256(build_image()).hexdigest())
        self.assertEqual(self.transport.write_log, [])
        self.assertFalse(ctrl.activate().ok)
        with self.assertRaises(c.SafetyError):
            self.transport.authorize_general_mode(pre.firmware_sha256, confirmed=False)
        with self.assertRaises(c.SafetyError):
            self.transport.write_masked(c.ADC2_CR2, 1, 1)
        ctrl.confirm_general_mode(True)
        self.assertTrue(ctrl.activate().ok)
        self.assertTrue(ctrl.sample(SampleRequest(2, .001)).is_real)
        # TIM3_CCMR2 is globally whitelisted, but does not belong to this selection.
        with self.assertRaises(c.SafetyError):
            self.transport.write_masked(c.TIM3_CCMR2, 255, 0)
        with self.assertRaises(c.SafetyError):
            self.transport.write_masked(c.RCC_CFGR, 0xC000, 0)
        self.assertTrue(ctrl.recover().ok)
        with self.assertRaises(c.SafetyError):
            self.transport.write_masked(c.ADC2_CR2, 1, 1)
        self.assertFalse(any(a == c.RCC_CFGR for a, _, _ in self.transport.write_log))

    def test_no_bare_hash_authorization(self):
        self.make()
        with self.assertRaises(c.SafetyError):
            self.transport.authorize_general_mode(hashlib.sha256(build_image()).hexdigest(), confirmed=True)

    def test_registered_lease_needs_no_general_confirmation(self):
        from test_hardware import write_profile
        with tempfile.TemporaryDirectory() as tmp:
            profile = load_profile(write_profile(Path(tmp), build_image()))
            ctrl = self.make(dict(adc='PA4', pwm=None, meter=None, continuity=None), profile)
            self.assertTrue(ctrl.activate().ok)
            self.assertFalse(ctrl.general_confirmed)
            self.assertTrue(ctrl.recover().ok)

    def test_functions_restore_on_two_choices(self):
        for function, choices in dict(adc=('PA4', 'PB1'), pwm=('TIM1_CH1/PA8', 'TIM3_CH3/PB0'),
                meter=('TIM3_CH1/PA6', 'TIM2_CH2/PA1'), continuity=('PB10', 'PA5')).items():
            for key in choices:
                with self.subTest(function=function, key=key):
                    ctrl = self.make(dict.fromkeys(CANDIDATES) | {function: key})
                    before = dict(self.target.fake.registers)
                    ctrl.confirm_general_mode(True)
                    result = ctrl.activate()
                    self.assertTrue(result.ok, result)
                    if function == 'pwm':
                        self.assertTrue(ctrl.set_pwm(1000, 30).ok)
                    elif function == 'continuity':
                        self.assertTrue(ctrl.check_continuity().ok)
                    elif function == 'meter':
                        ctrl.measure_pulse(.01)
                    self.assertTrue(ctrl.recover().ok)
                    for address, value in before.items():
                        if address != c.ADC2_SR:
                            self.assertEqual(self.target.fake.registers[address], value, hex(address))

    def test_conflicts_busy_and_adc_clock(self):
        for selection in (dict(pwm='TIM3_CH3/PB0', meter='TIM3_CH1/PA6'),
                          dict(adc='PA6', meter='TIM3_CH1/PA6')):
            ctrl = self.make(selection)
            self.assertFalse(ctrl.preflight().ok)
            self.assertEqual(self.transport.write_log, [])
        ctrl = self.make()
        self.target.fake.registers[c.TIM3_CR1] = 1
        pre = ctrl.preflight()
        rows = pre.resources['candidates']['meter']
        self.assertTrue(next(row for row in rows if row['key'] == 'TIM3_CH1/PA6')['reason'])
        self.assertFalse(next(row for row in rows if row['key'] == 'TIM2_CH2/PA1')['reason'])
        self.target.fake.registers[c.RCC_CFGR] &= ~0xC000
        pre = ctrl.preflight()
        self.assertFalse(pre.resources['adc'])
        self.assertIn('14MHz', pre.resources['reasons']['adc'])

    def test_pulse_measurement_on_ti1_and_ti2(self):
        for key, timer, channel in (('TIM3_CH1/PA6', 'TIM3', 1), ('TIM2_CH2/PA1', 'TIM2', 2)):
            with self.subTest(key=key):
                ctrl = self.make(dict.fromkeys(CANDIDATES) | {'meter': key})
                ctrl.confirm_general_mode(True)
                self.assertTrue(ctrl.activate().ok)
                self.target.signal = timer, channel, .02, .0015
                result = ctrl.measure_pulse(.1)
                self.assertTrue(result.ok, result)
                self.assertAlmostEqual(result.frequency_hz, 50)
                self.assertAlmostEqual(result.duty_percent, 7.5)
                self.assertTrue(ctrl.recover().ok)

    def test_profile_restrictions_ignore_json_grants(self):
        directory = Path(__file__).resolve().parents[1] / 'profiles'
        from swd_instruments.selectable import restricted
        for file, pins, timers in (('servo_reference.json', {'PA0', 'PA1'}, {'TIM2'}),
                                  ('air-harp.json', {'PA6', 'PA8'}, {'TIM1', 'TIM3'})):
            profile = load_profile(directory / file)
            profile.resources['restrictions'] = {}
            for entries in CANDIDATES.values():
                for p in entries:
                    if p.pin in pins or p.timer in timers:
                        self.assertTrue(restricted(profile, p))


class SelectionGUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_occupied_persistence_and_general_entry(self):
        from PySide6.QtCore import QSettings, Qt
        from swd_instruments.gui import InstrumentWindow
        with tempfile.TemporaryDirectory() as tmp, patch('swd_instruments.serial_terminal._default_port_lister') as ports:
            settings = QSettings(str(Path(tmp) / 'settings.ini'), QSettings.IniFormat)
            with patch.object(InstrumentWindow, '_settings', return_value=settings):
                w = InstrumentWindow()
                w.enable_persistence()
                self.assertEqual(w.profile_combo.itemText(0), '通用模式（未登记固件）')
                w.profile_combo.setCurrentIndex(0)
                self.assertEqual(w.pin_combos['continuity'].findData('PB6'), -1)
                w.pin_combos['adc'].setCurrentIndex(w.pin_combos['adc'].findData('PA5'))
                for i in range(w.occupied_list.count()):
                    item = w.occupied_list.item(i)
                    if item.data(Qt.UserRole) == 'PB6':
                        item.setCheckState(Qt.Unchecked)
                w.close()
                w2 = InstrumentWindow()
                w2.enable_persistence()
                self.assertEqual(w2.profile_combo.currentIndex(), 0)
                self.assertEqual(w2.pin_combos['adc'].currentData(), 'PA5')
                self.assertGreaterEqual(w2.pin_combos['continuity'].findData('PB6'), 0)
                w2.close()
            ports.assert_not_called()

    def test_dialog_gates_activation_and_locks_selection(self):
        from swd_instruments.gui import InstrumentWindow
        from PySide6.QtTest import QTest
        helper = SelectionTests()
        ctrl = helper.make()
        def wait(predicate):
            for _ in range(400):
                if predicate():
                    return
                QTest.qWait(5)
            self.fail('fake worker timed out')
        with patch('swd_instruments.gui.open_real_controller', return_value=ctrl), patch(
                'swd_instruments.serial_terminal._default_port_lister') as ports:
            w = InstrumentWindow()
            try:
                w.profile_combo.setCurrentIndex(0)
                w.serial.setText('FAKE-ONLY')
                w.run_preflight()
                wait(lambda: w._pending_commands == 0)
                self.assertTrue(w._preflight_ok)
                self.assertFalse(w.pin_group.isEnabled())
                with patch.object(w, '_confirm_general_dialog', return_value=False) as dialog:
                    w.activate_real()
                    dialog.assert_called_once()
                self.assertEqual(helper.transport.write_log, [])
                self.assertFalse(ctrl.general_confirmed)
                with patch.object(w, '_confirm_general_dialog', return_value=True):
                    w.activate_real()
                wait(lambda: w._pending_commands == 0)
                self.assertTrue(w._real_activated, w.real_text.toPlainText())
                self.assertTrue(helper.transport.write_log)
            finally:
                w.close()
                wait(lambda: w.worker is None)
                w.close()
            ports.assert_not_called()
