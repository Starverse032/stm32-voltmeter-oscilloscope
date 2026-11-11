"""已审查 profile 的加载与校验。

profile 必须绑定：设备ID/Flash容量、原固件只读镜像摘要、HSE配置假设、
已审查资源清单。空摘要、任意 `approved=true`、缺失审查人都会被拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

from .core import SafetyError, FLASH_BASE


class ProfileError(SafetyError):
    pass


MAX_PROFILE_BYTES = 1 << 20
MAX_ARTIFACT_BYTES = 16 << 20
_HEX64 = re.compile(r'^[0-9a-fA-F]{64}$')

# 源码审查和独立重建后登记的固件；JSON自称reviewed不能自行获得写权限。
RESOURCE_KEYS = ('adc2_free', 'tim1_ch1_free', 'pa4_free', 'pa8_free',
                 'tim4_free', 'pb6_free', 'pb7_free', 'pa7_free', 'adcpre_free', 'tim3_free', 'pa6_free', 'pb10_free',
                 'adc_input', 'meter_input', 'continuity_input')

TRUSTED_PROFILES = {
    'servo-reference-armcc5-v1': {
        'image_sha256': '2f5f8ad3e43078cc53b1e356c37324bdf7cc59e03416529e13896a38a64bc988',
        'artifact_sha256': 'a674b4cf911e8e90e61d4f654936373d826895a36b40459ec5342106eba985c1',
        'dev_id': 1040,
        'flash_size_kb': 64,
        'hse_hz': 8000000,
        'resources': {
            'adc2_free': True,
            'tim1_ch1_free': True,
            'pa4_free': True,
            'pa8_free': True,
            'tim4_free': False,
            'pb6_free': False,
            'pb7_free': False,
            'pa7_free': True,
            'adcpre_free': False,
            'adc_input': 'PA4',
            'tim3_free': True,
            'pa6_free': True,
            'pb10_free': True,
            'meter_input': 'PA6',
            'continuity_input': 'PB10',
        },
    },
    'air-harp-armcc5-v1': {
        'image_sha256': 'f1c774d8d37ec51427fd2a267d76ce22330226b9420ee17eb43fdce27ab895f8',
        'artifact_sha256': '53ff2d178976073b3ab1368d7a212bd723afccf7da1eeff78564faa17d8cd8ef',
        'dev_id': 1040,
        'flash_size_kb': 64,
        'hse_hz': 8000000,
        'resources': {
            'adc2_free': True,
            'tim1_ch1_free': False,
            'pa4_free': True,
            'pa8_free': False,
            'tim4_free': False,
            'pb6_free': False,
            'pb7_free': False,
            'pa7_free': True,
            'adcpre_free': True,
            'adc_input': 'PA4',
            'tim3_free': False,
            'pa6_free': False,
            'pb10_free': True,
            'meter_input': None,
            'continuity_input': 'PB10',
        },
    },
    'lazy-rescue-armcc5-v1': {
        'image_sha256': '5e9df00691490c37777bc415ef6b05549589be4cc9c736f548c0649206575229',
        'artifact_sha256': 'c7d62f04da2d64b5a3a2bbafb876d47c1d2d6fbac5e86fdcc561b26ce7ed95e8',
        'dev_id': 1040,
        'flash_size_kb': 64,
        'hse_hz': 8000000,
        'resources': {
            'adc2_free': True,
            'tim1_ch1_free': True,
            'pa4_free': True,
            'pa8_free': True,
            'tim4_free': False,
            'pb6_free': False,
            'pb7_free': False,
            'pa7_free': True,
            'adcpre_free': False,
            'adc_input': 'PA4',
            'tim3_free': False,
            'pa6_free': True,
            'pb10_free': True,
            'meter_input': None,
            'continuity_input': 'PB10',
        },
    },
    'light-master-armcc5-v1': {
        'image_sha256': 'f5d7c2cede8a3fa1d66ca8b121818a316a10be5afcaa1dec23c7c87639c8257c',
        'artifact_sha256': 'bf090c537de9f8b42a7491a370f5ecab7780380748566fe25721a33ab830790e',
        'dev_id': 1040,
        'flash_size_kb': 64,
        'hse_hz': 8000000,
        'resources': {
            'adc2_free': True,
            'tim1_ch1_free': True,
            'pa4_free': True,
            'pa8_free': True,
            'tim4_free': False,
            'pb6_free': False,
            'pb7_free': False,
            'pa7_free': True,
            'adcpre_free': True,
            'adc_input': 'PA4',
            'tim3_free': True,
            'pa6_free': True,
            'pb10_free': True,
            'meter_input': 'PA6',
            'continuity_input': 'PB10',
        },
    },
    'oled-awakening-armcc5-v1': {
        'image_sha256': 'fdf9cddfd006b84b7c07d369b0902dd96b3b5bffa7a504eada3c05e3d64000b4',
        'artifact_sha256': '1ce7527448b0d20b5e57fd8e80696505500724867465ca26e6917c21a69f15c2',
        'dev_id': 1040,
        'flash_size_kb': 64,
        'hse_hz': 8000000,
        'resources': {
            'adc2_free': True,
            'tim1_ch1_free': True,
            'pa4_free': True,
            'pa8_free': True,
            'tim4_free': False,
            'pb6_free': False,
            'pb7_free': False,
            'pa7_free': True,
            'adcpre_free': True,
            'adc_input': 'PA4',
            'tim3_free': True,
            'pa6_free': True,
            'pb10_free': True,
            'meter_input': 'PA6',
            'continuity_input': 'PB10',
        },
    },
    'road-blocked-armcc5-v1': {
        'image_sha256': '71e96dc5aa0dd4af65a1e8e383be3f9cbe28f0c59422866c226102048129a45e',
        'artifact_sha256': 'b28813c157d25acb5a19a33a11c3319294cf16bbe5d0d5a116a958af0e09a65f',
        'dev_id': 1040,
        'flash_size_kb': 64,
        'hse_hz': 8000000,
        'resources': {
            'adc2_free': True,
            'tim1_ch1_free': True,
            'pa4_free': True,
            'pa8_free': True,
            'tim4_free': False,
            'pb6_free': False,
            'pb7_free': False,
            'pa7_free': True,
            'adcpre_free': False,
            'adc_input': 'PA4',
            'tim3_free': True,
            'pa6_free': True,
            'pb10_free': True,
            'meter_input': 'PA6',
            'continuity_input': 'PB10',
        },
    },
    'running-led-armcc5-v1': {
        'image_sha256': '459ea31f1282323f6579b669bc873d80ccce7f98c34bff1b313da42b818e00a5',
        'artifact_sha256': '740a49244957cafbc7de6442cddd80d2a14e69c763da9cad64f51562d9a2c13e',
        'dev_id': 1040,
        'flash_size_kb': 64,
        'hse_hz': 8000000,
        'resources': {
            'adc2_free': True,
            'tim1_ch1_free': True,
            'pa4_free': False,
            'pa8_free': True,
            'tim4_free': False,
            'pb6_free': False,
            'pb7_free': False,
            'pa7_free': False,
            'adcpre_free': True,
            'adc_input': None,
            'tim3_free': True,
            'pa6_free': False,
            'pb10_free': True,
            'meter_input': None,
            'continuity_input': 'PB10',
        },
    },
    'wind-motor-armcc5-v1': {
        'image_sha256': '425dd454ae8d3e0338eef1e06dc75c6114e4dfb7f001543732953e4c24668491',
        'artifact_sha256': '78260f0447d3417e309e647212526693e565151722d457958f13fbbd6a85ac80',
        'dev_id': 1040,
        'flash_size_kb': 64,
        'hse_hz': 8000000,
        'resources': {
            'adc2_free': True,
            'tim1_ch1_free': True,
            'pa4_free': False,
            'pa8_free': True,
            'tim4_free': False,
            'pb6_free': False,
            'pb7_free': False,
            'pa7_free': True,
            'adcpre_free': True,
            'adc_input': 'PA7',
            'tim3_free': True,
            'pa6_free': True,
            'pb10_free': True,
            'meter_input': 'PA6',
            'continuity_input': 'PB10',
        },
    },
}


def assert_trusted_profile(profile) -> None:
    expected = TRUSTED_PROFILES.get(profile.profile_id)
    if expected is None:
        raise ProfileError('profile未登记为已审查原固件，禁止仅凭reviewed字段放行')
    if (profile.device.dev_id != expected['dev_id']
            or profile.device.flash_size_kb != expected['flash_size_kb']
            or profile.firmware.image_sha256 != expected['image_sha256']
            or profile.hse_hz != expected['hse_hz']
            or profile.firmware.flash_start != FLASH_BASE
            or profile.firmware.image_size != expected['flash_size_kb'] * 1024):
        raise ProfileError('profile与已审查镜像、完整Flash区间或时钟假设不一致')
    if ('artifact_sha256' in expected
            and profile.firmware.artifact_sha256 != expected['artifact_sha256']):
        raise ProfileError('产物摘要与可信登记不一致')
    if {key: profile.resources[key] for key in RESOURCE_KEYS} != {key: expected['resources'].get(key, None if key.endswith('_input') else False) for key in RESOURCE_KEYS}:
        raise ProfileError('profile资源授权与可信登记不一致')


# Known application use, transcribed from existing profile original_uses and candidate uses.
# PB6/PB7 were conservative old grants, not evidence of firmware use.
_KNOWN_USED = {
    'servo-reference': ({'TIM2'}, {'PA0', 'PA1'}),
    'air-harp': ({'TIM1', 'TIM3'}, {'PA6', 'PA8', 'PB12'}),
    'lazy-rescue': ({'TIM2', 'TIM3'}, {'PA0', 'PA1'}),
    'light-master': ({'TIM2'}, {'PA0', 'PA1', 'PA2', 'PB1', 'PB11'}),
    'oled-awakening': (set(), set()),
    'road-blocked': ({'TIM2'}, {'PA0', 'PA1', 'PB0', 'PB1'}),
    'running-led': ({'TIM2'}, {*(f'PA{i}' for i in range(8)), 'PB12', 'PB15'}),
    'wind-motor': ({'TIM2'}, {'PA2', 'PA4', 'PA5', 'PB1', 'PB11'}),
}
for _slug, (_timers, _pins) in _KNOWN_USED.items():
    TRUSTED_PROFILES[_slug + '-armcc5-v1']['restrictions'] = {
        'timers': frozenset(_timers), 'pins': frozenset(_pins | {'PB8', 'PB9'})}


@dataclass(frozen=True)
class DeviceSpec:
    dev_id: int
    flash_size_kb: int


@dataclass(frozen=True)
class FirmwareSpec:
    flash_start: int
    image_size: int
    image_sha256: str
    artifact_file: str | None
    artifact_sha256: str | None

    @property
    def flash_end(self) -> int:
        return self.flash_start + self.image_size


@dataclass(frozen=True)
class ReviewedProfile:
    path: Path
    profile_id: str
    device: DeviceSpec
    firmware: FirmwareSpec
    hse_hz: int
    vref_mv: int
    resources: dict
    reviewed_by: str
    reviewed_at: str
    notes: tuple[str, ...]
    display_name: str = ''

    def __post_init__(self):
        if not self.display_name:
            object.__setattr__(self, 'display_name', self.profile_id)

    @property
    def adc_input(self) -> str | None:
        return self.resources['adc_input']

    @property
    def functions(self) -> dict:
        r = self.resources
        return {'adc': r['adc2_free'] and r['adc_input'] is not None,
                'pwm': r['tim1_ch1_free'] and r['pa8_free'],
                'meter': (r['meter_input'] == 'PA6' and r['tim3_free'] and r['pa6_free']
                          or r['meter_input'] == 'PB7' and r['tim4_free'] and r['pb7_free']),
                'continuity': (r['continuity_input'] is not None
                               and r[r['continuity_input'].lower() + '_free'])}

    def summary(self) -> dict:
        return {
            'profile_id': self.profile_id,
            'display_name': self.display_name,
            'path': str(self.path),
            'device': {'dev_id': self.device.dev_id,
                       'flash_size_kb': self.device.flash_size_kb},
            'firmware': {'flash_start': self.firmware.flash_start,
                         'flash_end': self.firmware.flash_end,
                         'image_size': self.firmware.image_size,
                         'image_sha256': self.firmware.image_sha256},
            'hse_hz': self.hse_hz,
            'vref_mv': self.vref_mv,
            'resources': dict(self.resources),
            'reviewed_by': self.reviewed_by,
            'reviewed_at': self.reviewed_at,
            'notes': list(self.notes),
        }


def _require(condition: bool, message: str):
    if not condition:
        raise ProfileError(message)


def _integer(mapping: dict, key: str, low: int, high: int, label: str) -> int:
    value = mapping.get(key)
    _require(type(value) is int and low <= value <= high,
             f'{label}必须是{low}～{high}的整数')
    return value


def _hash(mapping: dict, key: str, label: str, required: bool = True) -> str | None:
    value = mapping.get(key)
    if value is None and not required:
        return None
    _require(type(value) is str and bool(_HEX64.fullmatch(value)),
             f'{label}必须是64位十六进制SHA-256，不接受空摘要')
    return value.lower()


def load_profile(path: str | Path) -> ReviewedProfile:
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ProfileError(f'无法读取profile：{path.name}: {exc}') from exc
    _require(0 < size <= MAX_PROFILE_BYTES, 'profile文件为空或过大')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProfileError(f'profile不是有效UTF-8 JSON：{exc}') from exc
    _require(type(raw) is dict, 'profile根必须是对象')
    _require(raw.get('schema_version') == 1, '不支持的profile schema_version')
    profile_id = raw.get('profile_id')
    _require(type(profile_id) is str and 1 <= len(profile_id.strip()) <= 128,
             'profile_id缺失或过长')

    display_name = raw.get('display_name', profile_id)
    _require('display_name' not in raw or (type(display_name) is str
             and bool(display_name.strip()) and len(display_name) <= 64),
             'display_name必须是非空且不超过64字符的字符串')

    device_raw = raw.get('device')
    _require(type(device_raw) is dict, 'device段缺失')
    dev_id = _integer(device_raw, 'dev_id', 0, 0xFFF, 'device.dev_id')
    flash_kb = _integer(device_raw, 'flash_size_kb', 16, 2048,
                        'device.flash_size_kb')

    fw_raw = raw.get('firmware')
    _require(type(fw_raw) is dict, 'firmware段缺失')
    flash_start = _integer(fw_raw, 'flash_start', FLASH_BASE,
                           FLASH_BASE + flash_kb * 1024 - 1,
                           'firmware.flash_start')
    _require(flash_start == FLASH_BASE,
             '当前仅允许从0x08000000开始的已审查原固件镜像')
    image_size = _integer(fw_raw, 'image_size', 64,
                          min(flash_kb * 1024, 1 << 20), 'firmware.image_size')
    image_sha = _hash(fw_raw, 'image_sha256', 'firmware.image_sha256')
    artifact_file = fw_raw.get('artifact_file')
    if artifact_file is not None:
        _require(type(artifact_file) is str and artifact_file == os.path.basename(artifact_file),
                 'firmware.artifact_file必须是profile同目录内的文件名')
    artifact_sha = _hash(fw_raw, 'artifact_sha256', 'firmware.artifact_sha256',
                         required=False)

    clock_raw = raw.get('clock')
    _require(type(clock_raw) is dict, 'clock段缺失')
    hse_hz = _integer(clock_raw, 'hse_hz', 1_000_000, 25_000_000,
                      'clock.hse_hz')
    _require(clock_raw.get('hse_source', 'reviewed-assumption')
             in ('reviewed-assumption', 'profile-assumption'),
             'clock.hse_source必须是配置假设，不能声称现场测量')
    vref_mv = _integer(raw, 'vref_mv', 2500, 3600, 'vref_mv')

    resources = raw.get('resources')
    _require(type(resources) is dict, 'resources段缺失')
    resources = dict(resources)
    _require(not (set(resources) - set(RESOURCE_KEYS) - {'original_uses'}), '未知资源字段')
    for key in RESOURCE_KEYS:
        if key in ('adc_input', 'meter_input', 'continuity_input'):
            continue
        if key not in resources and key not in RESOURCE_KEYS[:4]:
            resources[key] = False
        _require(type(resources.get(key)) is bool, f'resources.{key}必须是布尔值')
    resources.setdefault('meter_input', None)
    resources.setdefault('continuity_input', None)
    _require(resources['meter_input'] in (None, 'PA6', 'PB7'), 'meter_input 必须是 PA6、PB7 或 null')
    _require(resources['continuity_input'] in (None, 'PB10', 'PB6'), 'continuity_input 必须是 PB10、PB6 或 null')
    for selection, grants in (('meter_input', {'PA6': ('tim3_free', 'pa6_free'), 'PB7': ('tim4_free', 'pb7_free')}),
                              ('continuity_input', {'PB10': ('pb10_free',), 'PB6': ('pb6_free',)})):
        pin = resources[selection]
        _require(pin is None or all(resources[key] for key in grants[pin]), f'{selection} 引脚未授权')
    resources.setdefault('adc_input', 'PA4')
    _require(resources['adc_input'] in (None, 'PA4', 'PA7'), 'adc_input必须是PA4、PA7或null')
    if resources['adc2_free'] and resources['adc_input'] is not None:
        _require(resources[resources['adc_input'].lower() + '_free'], 'ADC引脚未授权')
    original = resources.get('original_uses')
    _require(type(original) is list and original and
             all(type(item) is str and item.strip() for item in original),
             'resources.original_uses必须列出原程序已用资源')

    review = raw.get('review')
    _require(type(review) is dict, 'review段缺失')
    _require(review.get('status') == 'reviewed',
             'review.status必须是reviewed，gating复选框或approved=true无效')
    reviewed_by = review.get('reviewed_by')
    _require(type(reviewed_by) is str and reviewed_by.strip(),
             'review.reviewed_by缺失')
    reviewed_at = review.get('reviewed_at')
    _require(type(reviewed_at) is str and reviewed_at.strip(),
             'review.reviewed_at缺失')

    notes = raw.get('notes', [])
    _require(type(notes) is list and all(type(item) is str for item in notes),
             'notes必须是字符串数组')

    profile = ReviewedProfile(
        path=path,
        profile_id=profile_id.strip(),
        device=DeviceSpec(dev_id, flash_kb),
        firmware=FirmwareSpec(flash_start, image_size, image_sha,
                              artifact_file, artifact_sha),
        hse_hz=hse_hz,
        vref_mv=vref_mv,
        resources=dict(resources),
        reviewed_by=reviewed_by.strip(),
        reviewed_at=reviewed_at.strip(),
        notes=tuple(notes),
        display_name=display_name.strip(),
    )
    assert_trusted_profile(profile)
    return profile


def verify_local_artifact(profile: ReviewedProfile) -> tuple[bool, str]:
    """可选：校验profile旁的本地产物文件；板上摘要仍以硬读比对为准。"""
    name = profile.firmware.artifact_file
    if not name:
        return True, 'profile未声明本地产物文件；仅按板上只读摘要比对'
    path = profile.path.parent / name
    if not path.exists():
        return False, f'本地产物{name}不存在，无法核对声明的交付物'
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_ARTIFACT_BYTES:
            return False, f'本地产物{name}大小为0或过大'
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b''):
                digest.update(chunk)
    except OSError as exc:
        return False, f'本地产物{name}读取失败：{exc}'
    expected = profile.firmware.artifact_sha256
    if expected and digest.hexdigest() != expected:
        return False, f'本地产物{name}摘要不匹配，拒绝信任该profile'
    return True, f'本地产物{name}摘要已校验' if expected else f'本地产物{name}存在但profile未给摘要'


def discover_profiles(directory: str | Path) -> list[Path]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob('*.json'):
        try:
            if not path.is_file() or path.stat().st_size > MAX_PROFILE_BYTES:
                continue
            raw = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(raw, dict) and isinstance(raw.get('profile_id'), str):
                found.append(path)
        except (OSError, ValueError, UnicodeError):
            continue
    return sorted(found)
