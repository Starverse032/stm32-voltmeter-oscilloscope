import csv
import json
from pathlib import Path
import hashlib
import tempfile
import unittest

from swd_instruments import core as c
from swd_instruments.profiles import (
    ProfileError, discover_profiles, load_profile, verify_local_artifact, TRUSTED_PROFILES,
)


def valid_profile(root: Path, **overrides):
    image = bytearray(b'\xFF' * 65536)
    image[0:4] = (0x20005000).to_bytes(4, 'little')
    image[4:8] = (0x08000101).to_bytes(4, 'little')
    digest = hashlib.sha256(image).hexdigest()
    TRUSTED_PROFILES['test-profile'] = {'image_sha256': digest,
        'dev_id': 0x410, 'flash_size_kb': 64, 'hse_hz': 8_000_000,
        'resources': dict(adc2_free=True, tim1_ch1_free=True, pa4_free=True,
                          pa8_free=True, tim4_free=False, pb6_free=False,
                          pb7_free=False, pa7_free=False, adc_input='PA4')}
    data = {
        'schema_version': 1,
        'profile_id': 'test-profile',
        'device': {'dev_id': 0x410, 'flash_size_kb': 64},
        'firmware': {
            'flash_start': 0x08000000,
            'image_size': len(image),
            'image_sha256': digest,
        },
        'clock': {'hse_hz': 8_000_000, 'hse_source': 'reviewed-assumption'},
        'vref_mv': 3300,
        'resources': {
            'adc2_free': True, 'tim1_ch1_free': True,
            'pa4_free': True, 'pa8_free': True,
            'original_uses': ['ADC1/PA0', 'TIM2_CH2/PA1', 'OLED PB8/PB9'],
        },
        'review': {'status': 'reviewed', 'reviewed_by': 'test',
                   'reviewed_at': '2026-11-10'},
        'notes': [],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    path = root / 'artifact.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path, bytes(image)


class ClockContract(unittest.TestCase):
    def test_hse_pll_72mhz_clock_tree(self):
        tree = c.derive_clocks(0x001D840A, 8_000_000)
        self.assertEqual((tree.source, tree.sysclk_hz, tree.hclk_hz),
                         ('PLL(HSE)', 72_000_000, 72_000_000))
        self.assertEqual((tree.pclk1_hz, tree.pclk2_hz), (36_000_000, 72_000_000))
        self.assertEqual((tree.adcclk_hz, tree.tim1_hz), (12_000_000, 72_000_000))
        self.assertTrue(tree.hse_is_assumption)

    def test_hsi_and_apb2_divider(self):
        tree = c.derive_clocks(0x00000000, 8_000_000)
        self.assertEqual((tree.source, tree.sysclk_hz, tree.adcclk_hz, tree.tim1_hz),
                         ('HSI', 8_000_000, 4_000_000, 8_000_000))
        # APB2 /2: PCLK2=36MHz, TIM1=72MHz; ADC /6=6MHz
        tree = c.derive_clocks(0x001DA40A, 8_000_000)
        self.assertEqual(tree.pclk2_hz, 36_000_000)
        self.assertEqual(tree.tim1_hz, 72_000_000)
        self.assertEqual(tree.adcclk_hz, 6_000_000)

    def test_invalid_clock_inputs_rejected(self):
        for cfgr, hse in ((0x0000000C, 8_000_000), (0x001D840A, 26_000_000),
                          (0x001D840A, 500_000)):
            with self.subTest(cfgr=cfgr, hse=hse), self.assertRaises(ValueError):
                c.derive_clocks(cfgr, hse)


class RealFrameContract(unittest.TestCase):
    def test_real_frame_requires_registered_producer_labels(self):
        points = (c.Point(1.0, 100, 0.0001), c.Point(1.1, 200, 0.0002))
        frame = c.Frame(points, 3300, c.REAL_SOURCE, c.REAL_CLOCK, 1)
        self.assertTrue(frame.is_real)
        with self.assertRaises(c.SafetyError):
            c.Frame(points, 3300, '真实测量', 'host', 1)

    def test_realtime_csv_has_uncertainty_and_actual_time(self):
        frame = c.Frame((c.Point(10.0, 1, 0.001), c.Point(10.2, 2, 0.002)),
                        3300, c.REAL_SOURCE, c.REAL_CLOCK, 7)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'real.csv'
            c.export_csv(frame, path)
            text = path.read_text(encoding='utf-8-sig')
            self.assertIn('time_uncertainty_s', text)
            data = list(csv.DictReader(line for line in text.splitlines()
                                       if not line.startswith('#')))
        self.assertEqual(len(data), 2)
        self.assertAlmostEqual(float(data[0]['time_s']), 10.0, places=8)
        self.assertAlmostEqual(float(data[0]['time_uncertainty_s']), 0.001, places=8)

    def test_metadata_newlines_do_not_break_csv(self):
        frame = c.Frame((c.Point(0.0, 1),), 3300, c.REAL_SOURCE,
                        c.REAL_CLOCK, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'one.csv'
            c.export_csv(frame, path)
            lines = path.read_text(encoding='utf-8-sig').splitlines()
        self.assertEqual(sum(1 for line in lines if line.startswith('#')), 7)


class ProfileContract(unittest.TestCase):
    def test_valid_profile_and_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, image = valid_profile(root)
            profile = load_profile(path)
            self.assertEqual(profile.firmware.image_sha256, hashlib.sha256(image).hexdigest())
            self.assertEqual(profile.device.flash_size_kb, 64)
            self.assertTrue(verify_local_artifact(profile)[0])
            self.assertEqual(discover_profiles(root), [path])

    def test_bad_profiles_rejected(self):
        bad_cases = [
            {'firmware': {'image_sha256': ''}},
            {'firmware': {'image_sha256': 'a' * 63}},
            {'resources': {'adc2_free': False}},
            {'resources': {'tim1_ch1_free': False}},
            {'review': {'status': 'approved', 'approved': True}},
            {'review': {'reviewed_by': ''}},
            {'firmware': {'flash_start': 0x08001000}},
            {'device': {'dev_id': '410'}},
            {'device': {'flash_size_kb': 0}},
        ]
        for case in bad_cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                path, _ = valid_profile(Path(tmp), **case)
                with self.assertRaises(ProfileError):
                    load_profile(path)

    def test_local_artifact_digest_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path, _ = valid_profile(root, firmware={
                'artifact_file': 'reference_original.hex',
                'artifact_sha256': '0' * 64,
            })
            (root / 'reference_original.hex').write_text('abc', encoding='utf-8')
            profile = load_profile(path)
            ok, note = verify_local_artifact(profile)
            self.assertFalse(ok)
            self.assertIn('不匹配', note)


if __name__ == '__main__':
    unittest.main()
