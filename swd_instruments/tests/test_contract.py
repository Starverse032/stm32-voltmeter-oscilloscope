import csv
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from swd_instruments import core as c
from swd_instruments.transport import PyOCDDiagnostic, SESSION_OPTIONS


class SafetyContract(unittest.TestCase):
    def test_real_writes_unconditionally_rejected(self):
        transport = PyOCDDiagnostic('specific-serial')
        for address in (c.ADC2_CR2, c.TIM1_CR1, 0x08000000, 0x20000000):
            with self.subTest(address=address), self.assertRaises(c.SafetyError):
                transport.write32(address, 1)
        # 受限字段写也必须先有真实会话；未连接时不能写。
        with self.assertRaises(c.SafetyError):
            transport.write_masked(c.ADC2_CR2, 1, 1)
        # 即使有会话，raw写仍永远拒绝；没有任意地址控制台。
        with self.assertRaises(c.SafetyError):
            transport.write32(c.ADC2_CR2, 1)

    def test_data_register_reads_rejected_before_transport_access(self):
        class NeverRead:
            def read32(self, _):
                self.fail('transport must not be called')
        for address in (c.ADC1_DR, c.ADC2_DR, 0x20000000):
            with self.subTest(address=address), self.assertRaises(c.SafetyError):
                c.safe_read(NeverRead(), address)
            with self.assertRaises(c.SafetyError):
                PyOCDDiagnostic('serial').read32(address)

    def test_diagnostic_never_grants_write_authority(self):
        for busy in (0, (1 << 10), (1 << 11)):
            bank = c.SimRegisterBank()
            bank.registers[c.RCC_APB2ENR] |= busy
            before = dict(bank.registers)
            report = c.diagnose(bank)
            self.assertFalse(report['writes_allowed'])
            self.assertEqual(bank.write_log, [])
            self.assertEqual(bank.registers, before)
            if busy:
                self.assertTrue(any('已启用' in issue for issue in report['issues']))

    def test_wrong_device_and_stopped_target_reported(self):
        bank = c.SimRegisterBank()
        bank.registers[c.IDCODE] = 0
        bank.running = False
        report = c.diagnose(bank)
        self.assertFalse(report['running'])
        self.assertGreaterEqual(len(report['issues']), 3)
        self.assertEqual(bank.write_log, [])

    def test_occupied_gpio_reported(self):
        bank = c.SimRegisterBank()
        bank.registers[c.GPIOA_CRL] |= 0xF << 16
        bank.registers[c.GPIOA_CRH] |= 0xF
        report = c.diagnose(bank)
        self.assertTrue(any('PA4' in item for item in report['issues']))
        self.assertTrue(any('PA8' in item for item in report['issues']))

    def test_all_transaction_edits_validated_before_any_write(self):
        bank = c.SimRegisterBank()
        transaction = c.SimTransaction(bank)
        with self.assertRaises(c.SafetyError):
            transaction.apply([c.Edit(c.GPIOA_CRL, 0xF0000, 0), c.Edit(0x08000000, 1, 1)])
        self.assertEqual(bank.write_log, [])

    def test_shared_bits_preserved_on_apply_and_restore(self):
        bank = c.SimRegisterBank()
        before = bank.registers[c.GPIOA_CRL]
        transaction = c.SimTransaction(bank)
        transaction.apply([c.Edit(c.GPIOA_CRL, 0xF0000, 0)])
        self.assertEqual(bank.registers[c.GPIOA_CRL] & ~0xF0000, before & ~0xF0000)
        # 原程序改变其他引脚；回滚不能覆盖这一运行期变化。
        bank.registers[c.GPIOA_CRL] ^= 1 << 28
        transaction.restore()
        self.assertEqual(bank.registers[c.GPIOA_CRL], before ^ (1 << 28))

    def test_ownership_drift_is_not_overwritten(self):
        bank = c.SimRegisterBank()
        transaction = c.SimTransaction(bank)
        transaction.apply([c.Edit(c.GPIOA_CRL, 0xF0000, 0)])
        bank.registers[c.GPIOA_CRL] |= 2 << 16
        expected = bank.registers[c.GPIOA_CRL]
        with self.assertRaisesRegex(c.SafetyError, '所有权'):
            transaction.restore()
        self.assertEqual(bank.registers[c.GPIOA_CRL], expected)

    def test_partial_failure_rolls_back_prior_edits(self):
        bank = c.SimRegisterBank()
        before = dict(bank.registers)
        real_apply = bank.apply
        count = 0
        def fail_second(edit):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('simulated bus failure')
            return real_apply(edit)
        with patch.object(bank, 'apply', side_effect=fail_second):
            with self.assertRaises(OSError):
                c.SimTransaction(bank).apply([c.Edit(c.GPIOA_CRL, 0xF0000, 0),
                                              c.Edit(c.GPIOA_CRH, 0xF, 0xB)])
        self.assertEqual(bank.registers, before)

    def test_original_resources_cannot_be_written(self):
        for address, mask in ((c.GPIOA_CRH, 0xF00000), (c.GPIOB_CRL, 0xF000),
                              (c.RCC_CFGR, 1), (c.AFIO_MAPR, 1),
                              (c.ADC1_DR, 0xFFFF), (0x40012408, 1)):
            with self.subTest(address=address, mask=mask), self.assertRaises(c.SafetyError):
                c.Edit(address, mask, 0).validate()

    def test_sim_transactions_cannot_use_real_transport(self):
        with self.assertRaises(c.SafetyError):
            c.SimTransaction(PyOCDDiagnostic('serial'))

    def test_exact_serial_required(self):
        with self.assertRaises(ValueError):
            PyOCDDiagnostic(' ')


class DataContract(unittest.TestCase):
    def test_adc_endpoints_and_statistics(self):
        frame = c.Frame((c.Point(10, 0), c.Point(10.3, 4095)), 3300,
                        '模拟数据（非真实测量）', '模拟时钟（非等间隔）', 1)
        self.assertEqual(frame.volts, (0, 3.3))
        self.assertEqual(frame.stats, (1.65, 0, 3.3, 3.3))
        self.assertAlmostEqual(frame.relative_times[1], 0.3)

    def test_immutable_frames_own_their_points(self):
        points = [c.Point(0, 12)]
        frame = c.Frame(points, 3300, '模拟数据（非真实测量）', '模拟时钟（非等间隔）', 1)
        points.append(c.Point(1, 13))
        self.assertEqual(len(frame.points), 1)
        with self.assertRaises(FrozenInstanceError):
            frame.vref_mv = 3000

    def test_bad_frames_fail(self):
        cases = [lambda: ([], 3300), lambda: ([c.Point(0, -1)], 3300),
                 lambda: ([c.Point(0, 1.5)], 3300), lambda: ([c.Point(float('nan'), 0)], 3300),
                 lambda: ([c.Point(0, 0), c.Point(0, 1)], 3300), lambda: ([c.Point(0, 0)], 0)]
        for make in cases:
            with self.subTest(make=make), self.assertRaises(ValueError):
                points, reference = make()
                c.Frame(points, reference, c.SIM_SOURCE, c.SIM_CLOCK, 1)

    def test_no_fake_real_measurement_source(self):
        with self.assertRaises(c.SafetyError):
            c.Frame((c.Point(0, 0),), 3300, '真实测量', 'host', 1)

    def test_simulation_has_irregular_strict_timestamps(self):
        first = c.Simulator().capture()
        differences = [b.time_s - a.time_s for a, b in zip(first.points, first.points[1:])]
        self.assertTrue(all(value > 0 for value in differences))
        self.assertGreater(max(differences) - min(differences), 0.0001)

    def test_csv_uses_stored_source_and_timestamps(self):
        frame = c.Simulator().capture(count=20)
        root = Path(__file__).resolve().parents[2] / 'work'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            name = Path(tmp) / 'frame.csv'
            c.export_csv(frame, name)
            text = name.read_text(encoding='utf-8-sig')
            self.assertIn('模拟数据', text)
            self.assertIn('非等间隔', text)
            data = list(csv.DictReader(line for line in text.splitlines() if not line.startswith('#')))
            self.assertEqual(len(data), 20)
            for original, row in zip(frame.points, data):
                self.assertAlmostEqual(original.time_s, float(row['time_s']), places=8)

    def test_invalid_reference_rejected_before_sampling(self):
        sim = c.Simulator()
        with self.assertRaises(ValueError):
            sim.capture(vref_mv=0)
        self.assertEqual(sim.sequence, 0)
        self.assertEqual(sim.time, 0)

    def test_pwm_quantization_and_limits(self):
        for frequency in (0.1, 1, 100, 123.456, 100_000):
            for duty in (0, 0.1, 50, 99.9, 100):
                plan = c.plan_pwm(frequency, duty)
                self.assertTrue(0 <= plan.psc <= 65535)
                self.assertTrue(1 <= plan.arr <= 65534)
                self.assertTrue(0 <= plan.ccr <= plan.arr + 1 <= 65535)
                self.assertLess(abs(plan.actual_hz - frequency) / frequency, 0.001)
                if duty in (0, 100):
                    self.assertEqual(plan.actual_duty, duty)

    def test_bad_pwm_requests_rejected(self):
        for frequency, duty in ((0, 50), (1e6, 50), (float('inf'), 50),
                                (100, -1), (100, 101), (100, float('nan'))):
            with self.assertRaises(ValueError):
                c.plan_pwm(frequency, duty)

    def test_simulated_pwm_off_and_endpoints(self):
        sim = c.Simulator()
        sim.scene = 'PWM回环'
        self.assertEqual(set(p.adc_code for p in sim.capture().points), {0})
        sim.output = c.plan_pwm(100, 100)
        self.assertEqual(set(p.adc_code for p in sim.capture().points), {4095})
        sim.output = c.plan_pwm(100, 0)
        self.assertEqual(set(p.adc_code for p in sim.capture().points), {0})


class MockPyOCDContract(unittest.TestCase):
    def test_attach_options_no_scripts_no_unprotected_reads(self):
        events = []
        class Probe:
            unique_id = 'exact'
        class FakeSession:
            def __init__(self, probe, options, auto_open):
                self.options = options
                self.target = types.SimpleNamespace(is_running=lambda: True,
                    read32=lambda address: events.append(('read', address)) or 0x410)
                self._load_user_script()
            def _load_user_script(self):
                raise AssertionError('user scripts must be disabled')
            def open(self):
                events.append(('open',))
            def close(self):
                events.append(('close',))
        modules = {
            'pyocd': types.ModuleType('pyocd'),
            'pyocd.core': types.ModuleType('pyocd.core'),
            'pyocd.core.helpers': types.SimpleNamespace(ConnectHelper=types.SimpleNamespace(
                get_all_connected_probes=lambda blocking: [Probe()])),
            'pyocd.core.session': types.SimpleNamespace(Session=FakeSession),
            'pyocd.probe': types.ModuleType('pyocd.probe'),
            'pyocd.probe.stlink_probe': types.SimpleNamespace(StlinkProbe=Probe),
        }
        with patch.dict(sys.modules, modules):
            with PyOCDDiagnostic('exact') as transport:
                self.assertEqual(transport.session.options['connect_mode'], 'attach')
                for key in ('auto_unlock', 'resume_on_disconnect'):
                    self.assertFalse(transport.session.options[key])
                self.assertTrue(transport.session.options['no_config'])
                self.assertEqual(transport.read32(c.IDCODE), 0x410)
                with self.assertRaises(c.SafetyError):
                    transport.read32(c.ADC1_DR)
            self.assertEqual(events, [('open',), ('read', c.IDCODE), ('close',)])
            with self.assertRaises(c.SafetyError):
                PyOCDDiagnostic('substring').open()


if __name__ == '__main__':
    unittest.main()
