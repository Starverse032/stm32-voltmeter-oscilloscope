"""Independent integration regressions: no probe discovery and no real target I/O."""
import hashlib
import json
from pathlib import Path
import re
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication

from swd_instruments import core as c
from swd_instruments.contracts import CancelToken, SampleRequest
from swd_instruments.hardware import RealDeviceController, RealRegisterTransaction
from swd_instruments.gui import InstrumentWindow
from swd_instruments.profiles import load_profile, ProfileError
from swd_instruments.transport import PyOCDSWD
from test_hardware import FakeF1Transport, build_image, write_profile


class IndependentDefinitions(unittest.TestCase):
    def test_f1_constants_match_existing_st_header(self):
        root = Path(__file__).resolve().parents[2]
        header = (Path(__file__).resolve().parent / 'data' / 'stm32f10x.h').read_text(encoding='utf-8', errors='replace')
        for name in ('ADC_SR_EOC', 'ADC_CR2_ADON', 'ADC_CR2_CAL', 'ADC_CR2_RSTCAL',
                     'ADC_CR2_SWSTART', 'ADC_CR2_EXTTRIG', 'RCC_APB2ENR_ADC2EN',
                     'RCC_APB2ENR_TIM1EN', 'TIM_CCER_CC1NE'):
            match = re.search(r'#define\s+'+name+r'\s+\(\(uint\d+_t\)(0x[0-9A-Fa-f]+)\)', header)
            self.assertIsNotNone(match, name)
            self.assertEqual(getattr(c, name), int(match.group(1), 16), name)

    def test_all_f103_ahb_dividers(self):
        for code, divisor in enumerate((2, 4, 8, 16, 64, 128, 256, 512), start=8):
            self.assertEqual(c.derive_clocks(code << 4, 8_000_000).hclk_hz, 8_000_000 // divisor)

    def test_f103_multiplier_13_is_15_not_connectivity_6_point_5(self):
        cfgr = (13 << 18) | (1 << 16) | 8
        self.assertEqual(c.derive_clocks(cfgr, 4_000_000).sysclk_hz, 60_000_000)

    def test_bundled_profile_matches_independent_rebuilt_artifact(self):
        folder = Path(__file__).resolve().parents[1] / 'profiles'
        profile = load_profile(folder/'servo_reference.json')
        artifact = json.loads((folder/'reference_artifact.json').read_text(encoding='utf-8'))
        self.assertEqual(profile.firmware.image_size, 65536)
        self.assertEqual(profile.firmware.image_sha256, artifact['flash_sha256'])
        self.assertEqual(hashlib.sha256((folder/'reference_original.hex').read_bytes()).hexdigest(),
                         profile.firmware.artifact_sha256)

    def test_self_declared_review_or_partial_image_does_not_grant_access(self):
        folder = Path(__file__).resolve().parents[1] / 'profiles'
        raw = json.loads((folder/'servo_reference.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as tmp:
            for key, value in (('profile_id', 'unregistered-but-reviewed'), ('image_size', 1024)):
                data = json.loads(json.dumps(raw))
                if key == 'image_size':
                    data['firmware'][key] = value
                else:
                    data[key] = value
                p = Path(tmp)/'unsafe.json'
                p.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(ProfileError):
                    load_profile(p)


class DriverRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.image = build_image()
        self.profile = load_profile(write_profile(Path(self.tmp.name), self.image))
        self.fake = FakeF1Transport(self.image)
        self.device = RealDeviceController(self.fake, self.profile, sleep=lambda _: None)

    def tearDown(self):
        self.device.close()
        self.tmp.cleanup()

    def test_shared_clock_drift_blocks_sampling_and_recovery_writes(self):
        self.assertTrue(self.device.activate().ok)
        before = len(self.fake.write_log)
        self.fake.registers[c.RCC_CFGR] ^= 1 << 14
        with self.assertRaises(c.SafetyError):
            self.device.sample(SampleRequest(1, 0))
        result = self.device.recover()
        self.assertFalse(result.ok)
        self.assertEqual(len(self.fake.write_log), before)

    def test_other_channel_takeover_does_not_blindly_clear_moe_or_counter(self):
        self.assertTrue(self.device.activate().ok)
        self.assertTrue(self.device.set_pwm(100, 50).ok)
        self.fake.registers[c.TIM1_CCER] |= 1 << 4
        before = len(self.fake.write_log)
        self.assertEqual(self.device.output_off().output_state, 'unknown-may-still-output')
        result = self.device.recover()
        self.assertFalse(result.ok)
        self.assertEqual(len(self.fake.write_log), before)
        self.assertTrue(self.fake.registers[c.TIM1_BDTR] & c.TIM_BDTR_MOE)

    def test_update_event_not_replayed_during_restore(self):
        self.assertTrue(self.device.activate().ok)
        count = sum(entry[0] == c.TIM1_EGR for entry in self.fake.write_log)
        self.device.close()
        self.assertEqual(sum(entry[0] == c.TIM1_EGR for entry in self.fake.write_log), count)

    def test_write_applied_then_readback_failed_is_still_journaled(self):
        before = dict(self.fake.registers)
        write = self.fake.write_masked
        failed = False
        def once(address, mask, value):
            nonlocal failed
            result = write(address, mask, value)
            if address == c.GPIOA_CRL and not failed:
                failed = True
                raise OSError('write applied, readback lost')
            return result
        self.fake.write_masked = once
        result = self.device.activate()
        self.assertFalse(result.ok)
        for address in (c.GPIOA_CRL, c.GPIOA_CRH, c.RCC_APB2ENR):
            self.assertEqual(self.fake.registers[address], before[address])

    def test_stale_eoc_is_drained_before_new_conversion(self):
        self.assertTrue(self.device.activate().ok)
        self.fake.adc_code = 100
        self.fake.registers[c.ADC2_SR] = c.ADC_SR_EOC
        write, read = self.fake.write_masked, self.fake.read32
        pending = False
        polls = 0
        def delayed_write(address, mask, value):
            nonlocal pending, polls
            old_eoc = self.fake.registers[c.ADC2_SR]
            result = write(address, mask, value)
            if address == c.ADC2_CR2 and value & c.ADC_CR2_SWSTART:
                self.fake.registers[c.ADC2_SR] = old_eoc
                pending, polls = True, 0
            return result
        def delayed_read(address):
            nonlocal polls, pending
            if address == c.ADC2_SR and pending:
                polls += 1
                if polls >= 2:
                    self.fake.adc_code = 200
                    self.fake.registers[c.ADC2_SR] |= c.ADC_SR_EOC
                    pending = False
            return read(address)
        self.fake.write_masked, self.fake.read32 = delayed_write, delayed_read
        frame = self.device.sample(SampleRequest(1, 0))
        self.assertEqual(frame.points[0].adc_code, 200)

    def test_repeated_host_clock_is_rejected_instead_of_invented(self):
        self.assertTrue(self.device.activate().ok)
        self.device._monotonic = lambda: 5.0
        with self.assertRaises(c.SafetyError):
            self.device.sample(SampleRequest(2, 0))
        self.assertEqual(self.device._sequence, 0)

    def test_second_activation_leaves_existing_resources_owned(self):
        self.assertTrue(self.device.activate().ok)
        before = len(self.fake.write_log)
        second = self.device.activate()
        self.assertFalse(second.ok)
        self.assertTrue(second.adc2_ready and second.tim1_ready)
        self.assertEqual(len(self.fake.write_log), before)
        self.assertTrue(self.device.set_pwm(100, 50).ok)

    def test_cancellation_during_activation_rolls_back(self):
        token = CancelToken()
        self.device._sleep = lambda _: token.cancel()
        result = self.device.activate(token)
        self.assertFalse(result.ok)
        self.assertTrue(any('取消' in issue for issue in result.issues))
        self.assertFalse(self.fake.registers[c.RCC_APB2ENR] & ((1 << 10) | (1 << 11)))


class ActualAdapterContract(unittest.TestCase):
    def test_connected_but_unverified_session_cannot_write(self):
        calls = []
        target = types.SimpleNamespace(is_running=lambda: True,
            read32=lambda address: calls.append(('read', address)) or 0,
            write32=lambda address, value: calls.append(('write', address, value)))
        transport = PyOCDSWD('mock')
        transport.session = types.SimpleNamespace(target=target)
        with self.assertRaises(c.SafetyError):
            transport.write_masked(c.GPIOA_CRL, 0xF0000, 0)
        self.assertEqual(calls, [])

    def test_egr_command_never_reads_write_only_event_register(self):
        calls = []
        def read(address):
            if address == c.TIM1_EGR:
                raise AssertionError('EGR must not be read')
            return 0
        transport = PyOCDSWD('mock')
        transport.session = types.SimpleNamespace(target=types.SimpleNamespace(
            is_running=lambda: True, read32=read,
            write32=lambda address, value: calls.append((address, value))))
        folder = Path(__file__).resolve().parents[1] / 'profiles'
        profile = load_profile(folder/'servo_reference.json')
        transport.authorize_profile(profile, profile.firmware.image_sha256)
        transport.activate_selection({c.TIM1_EGR: 1})
        transaction = RealRegisterTransaction(transport)
        transaction.write(c.TIM1_EGR, 1, 1, self_clearing=1)
        transaction.restore()
        self.assertEqual(calls, [(c.TIM1_EGR, 1)])

    def test_flash_bounds_are_c8_sized_before_reading_device(self):
        transport = PyOCDSWD('mock')
        for address, count in ((c.FLASH_BASE + 65536, 1), (c.FLASH_BASE + 65535, 2)):
            with self.assertRaises(c.SafetyError):
                transport.read_flash_block(address, count)

    def test_flash_missing_block_api_cannot_fall_back_to_wider_words(self):
        reads = []
        transport = PyOCDSWD('mock')
        transport.session = types.SimpleNamespace(target=types.SimpleNamespace(
            is_running=lambda: True, read32=lambda address: reads.append(address) or 0))
        transport._flash_capacity = 65536
        with self.assertRaises(c.SafetyError):
            transport.read_flash_block(c.FLASH_BASE + 65535, 1)
        self.assertEqual(reads, [])

    def test_flash_block_api_must_return_exact_requested_length(self):
        transport = PyOCDSWD('mock')
        transport.session = types.SimpleNamespace(target=types.SimpleNamespace(
            is_running=lambda: True))
        transport._flash_capacity = 65536
        for reply in ([1], [1, 2, 3]):
            transport.session.target.read_memory_block8 = lambda address, count: reply
            with self.assertRaises(c.SafetyError):
                transport.read_flash_block(c.FLASH_BASE, 2)
        transport.session.target.read_memory_block8 = lambda address, count: [42]
        self.assertEqual(transport.read_flash_block(c.FLASH_BASE + 65535, 1), b'*')


class GUIFrameRaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = InstrumentWindow()
        self.window.single()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()

    def real_frame(self, sequence=1):
        return c.Frame((c.Point(1, 100, 0.01), c.Point(1.1, 200, 0.01)),
                       3300, c.REAL_SOURCE, c.REAL_CLOCK, sequence)

    def test_pause_rejects_late_queued_real_frame(self):
        before = self.window.current_frame
        self.window._accept_real_frame = True
        self.window.pause()
        self.window._on_real_frame(self.real_frame())
        self.assertIs(self.window.current_frame, before)

    def test_single_real_frame_is_not_overwritten_by_duplicate(self):
        first = self.real_frame()
        self.window._accept_real_frame = True
        self.window._on_real_frame(first)
        self.window._on_real_frame(self.real_frame(2))
        self.assertIs(self.window.current_frame, first)

    def test_queued_continuation_cannot_restart_after_pause(self):
        self.window._accept_real_frame = self.window._real_continuous = True
        with patch.object(self.window, 'start_real_sampling') as start:
            self.window._on_real_frame(self.real_frame())
            self.window.pause()
            self.app.processEvents()
            start.assert_not_called()

    def test_profile_menu_excludes_unusable_build_metadata(self):
        choices = [Path(self.window.profile_combo.itemData(i)).name for i in range(self.window.profile_combo.count())]
        self.assertIn('servo_reference.json', choices)
        self.assertNotIn('reference_artifact.json', choices)


if __name__ == '__main__':
    unittest.main()
