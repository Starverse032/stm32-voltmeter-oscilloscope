"""Registered firmware and controller paths, using emulated transport only."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from swd_instruments import core as c
from swd_instruments.contracts import SampleRequest
from swd_instruments.hardware import RealDeviceController
from swd_instruments.profiles import load_profile, TRUSTED_PROFILES, RESOURCE_KEYS, ProfileError
from test_hardware import FakeF1Transport

PROFILES = Path(__file__).resolve().parents[1] / 'profiles'
SLUGS = ('lazy-rescue', 'road-blocked', 'light-master', 'air-harp',
         'oled-awakening', 'running-led', 'wind-motor')


def hex_image(path):
    """Independently validate HEX checksums and reconstruct the full C8 Flash."""
    image = bytearray(b'\xff' * 65536)
    covered = set()
    base = 0
    eof = False
    for line in path.read_text(encoding='ascii').splitlines():
        if not line.strip():
            continue
        assert line.startswith(':') and not eof
        record = bytes.fromhex(line[1:])
        count = record[0]
        assert len(record) == count + 5 and sum(record) % 256 == 0
        address = int.from_bytes(record[1:3], 'big')
        kind = record[3]
        payload = record[4:-1]
        if kind == 0:
            start = base + address - c.FLASH_BASE
            assert 0 <= start <= start + count <= len(image)
            for offset, value in enumerate(payload, start):
                assert offset not in covered or image[offset] == value
                covered.add(offset)
                image[offset] = value
        elif kind in (2, 4):
            assert count == 2
            base = int.from_bytes(payload, 'big') << (4 if kind == 2 else 16)
        elif kind == 1:
            assert count == 0
            eof = True
        else:
            assert kind in (3, 5) and count == 4
    assert eof and covered
    return bytes(image)


class ReviewedProfiles(unittest.TestCase):
    def controller(self, slug, fast_adc=False):
        profile = load_profile(PROFILES / (slug + '.json'))
        fake = FakeF1Transport(hex_image(PROFILES / profile.firmware.artifact_file))
        if profile.resources['adcpre_free']:
            fake.registers[c.RCC_APB2ENR] &= ~(1 << 9)
        if fast_adc:
            fake.registers[c.RCC_CFGR] &= ~0xC000
        controller = RealDeviceController(fake, profile,
            monotonic=lambda: fake.now, sleep=fake.advance)
        self.addCleanup(controller.close)
        return controller, fake

    def test_all_registered_hashes_grants_and_names(self):
        for slug in ('servo_reference', *SLUGS):
            with self.subTest(slug=slug):
                profile = load_profile(PROFILES / (slug + '.json'))
                expected = TRUSTED_PROFILES[profile.profile_id]
                artifact = PROFILES / profile.firmware.artifact_file
                self.assertEqual(hashlib.sha256(hex_image(artifact)).hexdigest(), expected['image_sha256'])
                digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                self.assertEqual(digest, profile.firmware.artifact_sha256)
                self.assertEqual(digest, expected['artifact_sha256'])
                self.assertEqual({k: profile.resources[k] for k in RESOURCE_KEYS}, expected['resources'])
                self.assertNotIn(profile.resources['meter_input'], ('PB6', 'PB7'))
                self.assertEqual(profile.resources['continuity_input'], 'PB10')
                self.assertFalse(profile.resources['pb7_free'])
                self.assertTrue(profile.display_name)
                self.assertEqual(profile.summary()['display_name'], profile.display_name)
                self.assertEqual(profile.device.dev_id, 0x410)
                self.assertEqual(profile.device.flash_size_kb, 64)
                self.assertEqual(profile.hse_hz, 8_000_000)
                self.assertTrue(profile.resources['original_uses'])
                if slug != 'servo_reference':
                    self.assertEqual(profile.reviewed_by, 'Codex (source review + offline rebuild)')
                    self.assertEqual(profile.reviewed_at, '2026-11-10')

    def test_display_name_validation_fallback_and_extra_grants(self):
        raw = json.loads((PROFILES / 'air-harp.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'profile.json'
            for value in ('', ' ', 'x' * 65, ' ' + 'x' * 64, None, 12):
                raw['display_name'] = value
                path.write_text(json.dumps(raw), encoding='utf-8')
                with self.assertRaises(ProfileError):
                    load_profile(path)
            raw.pop('display_name')
            path.write_text(json.dumps(raw), encoding='utf-8')
            self.assertEqual(load_profile(path).display_name, raw['profile_id'])
            for grant in ('tim1_ch1_free', 'pa8_free', 'pb7_free'):
                changed = json.loads(json.dumps(raw))
                changed['resources'][grant] = True
                path.write_text(json.dumps(changed), encoding='utf-8')
                with self.assertRaises(ProfileError):
                    load_profile(path)
            raw['resources']['adcpre_free'] = False
            path.write_text(json.dumps(raw), encoding='utf-8')
            with self.assertRaises(ProfileError):
                load_profile(path)

    def test_running_led_has_no_adc_and_no_prescaler_write(self):
        controller, fake = self.controller('running-led', True)
        pre = controller.preflight()
        self.assertTrue(pre.ok, pre)
        self.assertIsNone(controller.profile.adc_input)
        self.assertFalse(pre.resources['adc'])
        self.assertIn('adc', pre.resources['reasons'])
        for function in ('pwm', 'continuity'):
            self.assertTrue(pre.resources[function])
        active = controller.activate()
        self.assertTrue(active.ok, active)
        self.assertFalse(active.adc2_ready)
        self.assertTrue(active.tim1_ready and not active.meter_ready and active.continuity_ready)
        with self.assertRaises(c.SafetyError):
            controller.sample(SampleRequest(1, 0))
        self.assertNotIn(c.RCC_CFGR, [entry[0] for entry in fake.write_log])
        self.assertNotIn(c.GPIOA_CRL, [entry[0] for entry in fake.write_log])

    def test_air_harp_meter_without_tim1(self):
        controller, fake = self.controller('air-harp', True)
        fake.registers[c.TIM1_CR1] = 1
        fake.registers[c.GPIOA_CRH] = 0x44444444
        pre = controller.preflight()
        self.assertTrue(pre.ok, pre)
        self.assertFalse(pre.resources['pwm'])
        self.assertTrue(controller.activate().ok)
        self.assertFalse(controller.set_pwm(50, 50).ok)
        fake.inject_pa6(.02, .005)
        self.assertFalse(controller.measure_pulse(.2).ok)
        self.assertFalse(any(a in (c.TIM1_CR1, c.TIM1_CCER, c.GPIOA_CRH) for a, *_ in fake.write_log))
        self.assertTrue(any('PB6/PB7' in note for note in controller.profile.notes))

    def test_wind_motor_channel7_preserves_pa4(self):
        controller, fake = self.controller('wind-motor', True)
        fake.registers[c.GPIOA_CRL] |= 0xB << 16
        original = fake.registers[c.GPIOA_CRL]
        self.assertTrue(controller.activate().ok)
        self.assertEqual(fake.registers[c.ADC2_SQR3], 7)
        self.assertEqual(fake.registers[c.ADC2_SMPR2] & (7 << 21), 7 << 21)
        self.assertEqual(fake.registers[c.GPIOA_CRL] & 0xF0000, original & 0xF0000)
        self.assertTrue(controller.recover().ok)
        for address, mask, *_ in fake.write_log:
            if address == c.GPIOA_CRL:
                self.assertEqual(mask & 0xF0000, 0)

    def test_adcpre_planned_adjusted_and_fully_restored(self):
        controller, fake = self.controller('light-master', True)
        original = dict(fake.registers)
        pre = controller.preflight()
        self.assertTrue(pre.ok and pre.resources['adc'])
        self.assertEqual(pre.resources['adcpre_divider'], 6)
        self.assertEqual(pre.clocks['adcclk_hz'], 36_000_000)
        self.assertEqual(fake.write_log, [])
        active = controller.activate()
        self.assertTrue(active.ok, active)
        self.assertEqual(active.clocks['adcclk_hz'], 12_000_000)
        self.assertEqual(fake.registers[c.RCC_CFGR] & 0xC000, 0x8000)
        fake.inject_pa6(.02, .005)
        self.assertTrue(controller.measure_pulse(.2).ok)
        self.assertTrue(controller.recover().ok)
        for address, mask, _, before, after in fake.write_log:
            if address == c.RCC_CFGR:
                self.assertEqual(mask, 0xC000)
                self.assertEqual(before & ~mask, after & ~mask)
        for address, value in original.items():
            if address in (c.TIM3_CCR1, c.TIM3_CCR2, c.TIM4_CCR1, c.TIM4_CCR2):
                continue  # Hardware capture data are read-only and cannot be replayed.
            self.assertEqual(fake.registers[address], value, hex(address))

    def test_adc1_users_never_write_adcpre_when_too_fast(self):
        for slug in ('servo_reference', 'lazy-rescue', 'road-blocked'):
            with self.subTest(slug=slug):
                controller, fake = self.controller(slug, True)
                pre = controller.preflight()
                self.assertTrue(pre.ok, pre)
                self.assertFalse(pre.resources['adc'])
                self.assertIn('14MHz', pre.resources['reasons']['adc'])
                self.assertTrue(controller.activate().ok)
                self.assertNotIn(c.RCC_CFGR, [entry[0] for entry in fake.write_log])
                self.assertNotIn(c.ADC2_CR2, [entry[0] for entry in fake.write_log])
                self.assertTrue(controller.recover().ok)

    def test_adcpre_external_takeover_blocks_restore_writes(self):
        controller, fake = self.controller('light-master', True)
        self.assertTrue(controller.activate().ok)
        fake.registers[c.RCC_CFGR] ^= 1 << 14
        count = len(fake.write_log)
        self.assertFalse(controller.recover().ok)
        self.assertEqual(len(fake.write_log), count)

    def test_identity_failure_disables_every_function_and_all_writes(self):
        for mismatch in ('image', 'device', 'capacity', 'running', 'clock'):
            with self.subTest(mismatch=mismatch):
                controller, fake = self.controller('light-master', True)
                if mismatch == 'image': fake.flash[100] ^= 1
                if mismatch == 'device': fake.registers[c.IDCODE] ^= 1
                if mismatch == 'capacity': fake.registers[c.FLASH_SIZE_REG] = 128
                if mismatch == 'running': fake.running = False
                if mismatch == 'clock': fake.registers[c.RCC_CFGR] |= 12
                pre = controller.preflight()
                self.assertFalse(pre.ok)
                self.assertTrue(pre.issues)
                self.assertFalse(any(pre.resources[k] for k in ('adc', 'pwm', 'meter', 'continuity')))
                self.assertFalse(controller.activate().ok)
                self.assertEqual(fake.write_log, [])

    def test_busy_adc1_blocks_prescaler_and_keeps_other_functions(self):
        for address, bit in ((c.RCC_APB2ENR, 1 << 9), (c.ADC1_CR2, 1)):
            with self.subTest(address=address):
                controller, fake = self.controller('light-master', True)
                fake.registers[address] = fake.registers.get(address, 0) | bit
                pre = controller.preflight()
                self.assertTrue(pre.ok)
                self.assertFalse(pre.resources['adc'])
                self.assertIsNone(pre.resources['adcpre_divider'])
                self.assertTrue(controller.activate().ok)
                self.assertNotIn(c.RCC_CFGR, [entry[0] for entry in fake.write_log])
                self.assertTrue(controller.recover().ok)

    def test_no_available_functions_blocks_activation(self):
        controller, fake = self.controller('light-master', True)
        fake.registers[c.ADC2_CR2] = 1
        fake.registers[c.TIM1_CR1] = 1
        fake.registers[c.TIM3_CR1] = 1
        fake.registers[c.GPIOB_CRH] |= 0xB << 8
        self.assertFalse(controller.preflight().ok)
        self.assertFalse(controller.activate().ok)
        self.assertEqual(fake.write_log, [])

    def test_all_eight_activate_their_granted_subset(self):
        for slug in ('servo_reference', *SLUGS):
            with self.subTest(slug=slug):
                controller, fake = self.controller(slug, True)
                if not controller.profile.resources['adcpre_free']:
                    fake.registers[c.RCC_CFGR] |= 0x8000
                pre = controller.preflight()
                self.assertTrue(pre.ok, pre)
                active = controller.activate()
                self.assertTrue(active.ok, active)
                actual = dict(adc=active.adc2_ready, pwm=active.tim1_ready,
                              meter=active.meter_ready, continuity=active.continuity_ready)
                self.assertEqual(actual, controller.profile.functions)
                self.assertTrue(controller.recover().ok)

    def test_transport_prescaler_authorization_and_mask(self):
        from types import SimpleNamespace
        from swd_instruments.transport import PyOCDSWD
        class Target:
            value = 0xA51D040A
            writes = []
            def is_running(self): return True
            def read32(self, address): return self.value
            def write32(self, address, value):
                self.value = value
                self.writes.append((address, value))
        target = Target()
        transport = PyOCDSWD('emulated-only')
        transport.session = SimpleNamespace(target=target)
        for slug, allowed in (('servo_reference', False), ('light-master', True)):
            profile = load_profile(PROFILES / (slug + '.json'))
            transport.authorize_profile(profile, profile.firmware.image_sha256)
            if allowed:
                original = target.value
                transport.activate_selection({c.RCC_CFGR: 0xC000})
                transport.write_masked(c.RCC_CFGR, 0xC000, 0x8000)
                self.assertEqual(target.value & ~0xC000, original & ~0xC000)
            else:
                with self.assertRaises(c.SafetyError):
                    transport.activate_selection({c.RCC_CFGR: 0xC000})
                with self.assertRaises(c.SafetyError):
                    transport.write_masked(c.RCC_CFGR, 0xC000, 0x8000)
            with self.assertRaises(c.SafetyError):
                transport.write_masked(c.RCC_CFGR, 0xC001, 0)
        transport.revoke_resource_access()
        with self.assertRaises(c.SafetyError):
            transport.write_masked(c.RCC_CFGR, 0xC000, 0)
        self.assertEqual(len(target.writes), 1)
