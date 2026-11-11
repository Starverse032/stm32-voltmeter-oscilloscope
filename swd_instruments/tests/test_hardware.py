import csv
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from swd_instruments import core as c
from swd_instruments.contracts import CancelToken, SampleRequest
from swd_instruments.hardware import RealDeviceController
from swd_instruments.profiles import load_profile, TRUSTED_PROFILES
from swd_instruments.transport import PyOCDSWD


def build_image():
    image = bytearray(b'\xFF' * 65536)
    image[0:4] = (0x20005000).to_bytes(4, 'little')
    image[4:8] = (0x08000101).to_bytes(4, 'little')
    return bytes(image)


def write_profile(root: Path, image: bytes) -> Path:
    TRUSTED_PROFILES['fake-f103'] = {'image_sha256': hashlib.sha256(image).hexdigest(),
        'dev_id': 0x410, 'flash_size_kb': 64, 'hse_hz': 8_000_000,
        'resources': dict(adc2_free=True, tim1_ch1_free=True, pa4_free=True,
                          pa8_free=True, tim4_free=False, pb6_free=False,
                          pb7_free=False, pa7_free=False, tim3_free=False, pa6_free=False,
                          pb10_free=False, meter_input=None, continuity_input=None, adc_input='PA4')}
    data = {
        'schema_version': 1,
        'profile_id': 'fake-f103',
        'device': {'dev_id': 0x410, 'flash_size_kb': 64},
        'firmware': {
            'flash_start': 0x08000000,
            'image_size': len(image),
            'image_sha256': hashlib.sha256(image).hexdigest(),
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
    path = root / 'artifact.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


class FakeF1Transport:
    """模拟寄存器、Flash、自清位和EOC的薄F1替身；不是“返回预期值”的桩。"""

    def __init__(self, image: bytes, flash_kb: int = 64):
        self.registers = {
            c.IDCODE: 0x20036410,
            c.FLASH_SIZE_REG: flash_kb,
            c.RCC_CR: 0x03030083,
            c.RCC_CFGR: 0x001D840A,
            c.RCC_APB2ENR: (1 << 2) | (1 << 3) | (1 << 9),
            c.AFIO_MAPR: 0,
            c.GPIOA_CRL: 0x444444B0,
            c.GPIOA_CRH: 0x44444444,
            c.ADC1_CR1: 0,
            c.ADC2_CR1: 0, c.ADC2_CR2: 0, c.ADC2_SR: 0,
            c.ADC2_SMPR2: 0, c.ADC2_SQR1: 0, c.ADC2_SQR3: 0,
            c.TIM1_CR1: 0, c.TIM1_DIER: 0, c.TIM1_CCMR1: 0,
            c.TIM1_CCMR2: 0, c.TIM1_CCER: 0, c.TIM1_PSC: 0,
            c.TIM1_ARR: 0, c.TIM1_CCR1: 0, c.TIM1_BDTR: 0,
        }
        self.registers.update({c.RCC_APB1ENR: 0, c.GPIOB_CRL: 0x44444444,
            c.GPIOB_IDR: 0x40, c.GPIOB_ODR: 0,
            **{a: 0 for a in (c.TIM4_CR1, c.TIM4_SMCR, c.TIM4_DIER, c.TIM4_SR,
                c.TIM4_CCMR1, c.TIM4_CCMR2, c.TIM4_CCER, c.TIM4_PSC,
                c.TIM4_ARR, c.TIM4_CCR1, c.TIM4_CCR2,
                c.TIM4_CCR2 + 4, c.TIM4_CCR2 + 8)}})
        self.registers[c.GPIOB_CRH] = 0x44444444
        self.registers.update({getattr(c, 'TIM3_' + name): 0 for name in
            ('CR1', 'SMCR', 'DIER', 'SR', 'CCMR1', 'CCMR2', 'CCER', 'PSC', 'ARR', 'CCR1', 'CCR2')})
        self.registers[c.TIM3_CCR2 + 4] = self.registers[c.TIM3_CCR2 + 8] = 0
        self.pa6_signal = None
        self.pb10_levels = []
        self._timer3_start = self._reset3_at = 0.0
        self._last3_rise = self._last3_fall = None
        self.pb7_signal = None  # (period_s, high_s)，None为恒定电平
        self.pb6_levels = []
        self.now = 0.0
        self.read_latency_s = 0.0
        self._timer_start = 0.0
        self._last_rise = None
        self._last_fall = None
        self._reset_at = 0.0
        self.flash = bytearray(flash_kb * 1024)
        self.flash[:len(image)] = image
        self.running = True
        self.write_log = []
        self.read_log = []
        self.fail_write = set()
        self.fail_read = set()
        self.stuck_cal = False
        self.stuck_rstcal = False
        self.eoc_timeout = False
        self.adc_code = 1234
        self.adc2_owner = False
        self.closed = 0

    def inject_pb7(self, period_s=None, high_s=None):
        self.pb7_signal = None if period_s is None else (period_s, high_s)
        self._last_rise = self._last_fall = None

    def advance(self, seconds):
        self.now += seconds

    def _update_tim4(self):
        import math
        r = self.registers
        if not (r[c.RCC_APB1ENR] & 4 and r[c.TIM4_CR1] & 1):
            return
        hz = c.derive_clocks(r[c.RCC_CFGR], 8000000).tim4_hz
        tick = (r[c.TIM4_PSC] + 1) / hz

        def compare_until(stamp):
            start = int(round((self._timer_start - self._reset_at) / tick))
            end = int(round((stamp - self._reset_at) / tick))
            modulus = r[c.TIM4_ARR] + 1
            for channel in (3, 4):
                # Output compare flags are independent of CCxE. Output mode
                # does not generate overcapture flags (RM0008 TIMx_SR).
                if (r[c.TIM4_CCMR2] >> (8 * (channel - 3))) & 3:
                    continue
                ccr = r[c.TIM4_CCR2 + 4 * (channel - 2)]
                if ccr <= r[c.TIM4_ARR] and (end - ccr) // modulus > (start - ccr) // modulus:
                    r[c.TIM4_SR] |= 1 << channel

        period, high = self.pb7_signal or (float('inf'), 0)
        valid = (r[c.TIM4_SMCR] == 0x64 and r[c.TIM4_CCMR1] == 0x102
                 and r[c.TIM4_CCER] == 0x13 and r[c.TIM4_DIER] == 0
                 and (r[c.GPIOB_CRL] >> 28) == 4)
        # 按时间推进边沿。2026-11-11实测：N个tick在复位边沿捕获复位前计数N-1。
        events = []
        if valid and math.isfinite(period) and 0 < high < period:
            n0 = int(self._timer_start // period)
            n1 = int(self.now // period)
            for n in range(n0, n1 + 1):
                rising = n * period
                falling = rising + high
                if self._timer_start < rising <= self.now and rising != self._last_rise:
                    events.append((rising, 2))
                if self._timer_start < falling <= self.now and falling != self._last_fall:
                    events.append((falling, 1))
        for stamp, channel in sorted(events):
            compare_until(stamp)
            count = max(0, round((stamp - self._reset_at) / tick) - 1)
            if count > r[c.TIM4_ARR]:
                r[c.TIM4_SR] |= 1
            flag = 1 << channel
            if r[c.TIM4_SR] & flag:
                r[c.TIM4_SR] |= 1 << (channel + 8)
            r[c.TIM4_CCR2 if channel == 2 else c.TIM4_CCR1] = count % (r[c.TIM4_ARR] + 1)
            r[c.TIM4_SR] |= flag
            if channel == 2:
                self._last_rise = stamp
                self._reset_at = stamp
                r[c.TIM4_SR] |= 0x40  # TI2 trigger in slave reset mode
                for output_channel in (3, 4):
                    input_mode = (r[c.TIM4_CCMR2] >> (8 * (output_channel - 3))) & 3
                    if not input_mode and r[c.TIM4_CCR2 + 4 * (output_channel - 2)] == 0:
                        r[c.TIM4_SR] |= 1 << output_channel
                if not r[c.TIM4_CR1] & 4:
                    r[c.TIM4_SR] |= 1
            else:
                self._last_fall = stamp
            self._timer_start = stamp
        compare_until(self.now)
        if (self.now - self._reset_at) / tick > r[c.TIM4_ARR] + 1:
            r[c.TIM4_SR] |= 1
        self._timer_start = self.now

    def inject_pa6(self, period_s=None, high_s=None):
        self.pa6_signal = None if period_s is None else (period_s, high_s)
        self._last3_rise = self._last3_fall = None

    def _update_tim3(self):
        import math
        r = self.registers
        if not (r[c.RCC_APB1ENR] & 2 and r[c.TIM3_CR1] & 1):
            return
        hz = c.derive_clocks(r[c.RCC_CFGR], 8000000).tim3_hz
        tick = (r[c.TIM3_PSC] + 1) / hz

        def compare_until(stamp):
            start = int(round((self._timer3_start - self._reset3_at) / tick))
            end = int(round((stamp - self._reset3_at) / tick))
            modulus = r[c.TIM3_ARR] + 1
            for channel in (3, 4):
                # Output compare flags are independent of CCxE. Output mode
                # does not generate overcapture flags (RM0008 TIMx_SR).
                if (r[c.TIM3_CCMR2] >> (8 * (channel - 3))) & 3:
                    continue
                ccr = r[c.TIM3_CCR2 + 4 * (channel - 2)]
                if ccr <= r[c.TIM3_ARR] and (end - ccr) // modulus > (start - ccr) // modulus:
                    r[c.TIM3_SR] |= 1 << channel

        period, high = self.pa6_signal or (float('inf'), 0)
        valid = (r[c.TIM3_SMCR] == 0x54 and r[c.TIM3_CCMR1] == 0x201
                 and r[c.TIM3_CCER] == 0x31 and r[c.TIM3_DIER] == 0
                 and (r[c.GPIOA_CRL] >> 24 & 15) == 4)
        # 按时间推进边沿。2026-11-11实测：N个tick在复位边沿捕获复位前计数N-1。
        events = []
        if valid and math.isfinite(period) and 0 < high < period:
            n0 = int(self._timer3_start // period)
            n1 = int(self.now // period)
            for n in range(n0, n1 + 1):
                rising = n * period
                falling = rising + high
                if self._timer3_start < rising <= self.now and rising != self._last3_rise:
                    events.append((rising, 1))
                if self._timer3_start < falling <= self.now and falling != self._last3_fall:
                    events.append((falling, 2))
        for stamp, channel in sorted(events):
            compare_until(stamp)
            count = max(0, round((stamp - self._reset3_at) / tick) - 1)
            if count > r[c.TIM3_ARR]:
                r[c.TIM3_SR] |= 1
            flag = 1 << channel
            if r[c.TIM3_SR] & flag:
                r[c.TIM3_SR] |= 1 << (channel + 8)
            r[c.TIM3_CCR2 if channel == 2 else c.TIM3_CCR1] = count % (r[c.TIM3_ARR] + 1)
            r[c.TIM3_SR] |= flag
            if channel == 1:
                self._last3_rise = stamp
                self._reset3_at = stamp
                r[c.TIM3_SR] |= 0x40  # TI2 trigger in slave reset mode
                for output_channel in (3, 4):
                    input_mode = (r[c.TIM3_CCMR2] >> (8 * (output_channel - 3))) & 3
                    if not input_mode and r[c.TIM3_CCR2 + 4 * (output_channel - 2)] == 0:
                        r[c.TIM3_SR] |= 1 << output_channel
                if not r[c.TIM3_CR1] & 4:
                    r[c.TIM3_SR] |= 1
            else:
                self._last3_fall = stamp
            self._timer3_start = stamp
        compare_until(self.now)
        if (self.now - self._reset3_at) / tick > r[c.TIM3_ARR] + 1:
            r[c.TIM3_SR] |= 1
        self._timer3_start = self.now

    def is_running(self):
        return self.running

    def read32(self, address):
        self.advance(self.read_latency_s)
        if address in c.COMMAND_REGISTERS:
            raise c.SafetyError("禁止读取命令寄存器")
        if address in (c.ADC1_DR, c.ADC2_DR):
            raise c.SafetyError('禁止通用读取ADC数据寄存器')
        if address in self.fail_read:
            raise OSError('simulated read failure')
        if not self.running:
            raise c.SafetyError('目标已停止')
        self._update_tim4()
        self._update_tim3()
        self.read_log.append(address)
        if address == c.GPIOB_IDR and self.pb6_levels:
            return self.pb6_levels.pop(0) << 6
        if address == c.GPIOB_IDR and self.pb10_levels:
            return self.pb10_levels.pop(0) << 10
        if address == c.GPIOB_IDR and self.registers[c.GPIOB_CRH] >> 8 & 15 == 8:
            return self.registers[c.GPIOB_ODR] & 0x400
        value = self.registers.get(address, 0)
        if address in (c.TIM4_CCR1, c.TIM4_CCR2):
            self.registers[c.TIM4_SR] &= ~(2 if address == c.TIM4_CCR1 else 4)
        if address in (c.TIM3_CCR1, c.TIM3_CCR2):
            self.registers[c.TIM3_SR] &= ~(2 if address == c.TIM3_CCR1 else 4)
        return value

    def read_flash_block(self, address, size):
        if address < c.FLASH_BASE or size <= 0 or address + size > c.FLASH_BASE + (1 << 20):
            raise c.SafetyError('越界Flash读取')
        if not self.running:
            raise c.SafetyError('目标已停止')
        self.read_log.append(('flash', address, size))
        start = address - c.FLASH_BASE
        return bytes(self.flash[start:start + size])

    def read_flash_size_kb(self):
        return int(self.registers[c.FLASH_SIZE_REG]) & 0xFFFF

    def write_masked(self, address, mask, value):
        if address not in c.REAL_WRITE_MASKS:
            raise c.SafetyError('地址不在写入白名单')
        if mask & ~c.REAL_WRITE_MASKS[address] or value & ~mask:
            raise c.SafetyError('掩码越界')
        if not self.running:
            raise c.SafetyError('目标已停止')
        if address in self.fail_write:
            raise OSError('simulated write failure')
        self._update_tim4()
        self._update_tim3()
        before = self.registers.get(address, 0)
        after = (before & ~mask) | (value & mask)
        if address == c.ADC2_CR2:
            if after & c.ADC2_CR2_RSTCAL and not self.stuck_rstcal:
                after &= ~c.ADC2_CR2_RSTCAL
            if after & c.ADC2_CR2_CAL and not self.stuck_cal:
                after &= ~c.ADC2_CR2_CAL
            if after & c.ADC2_CR2_SWSTART:
                after &= ~c.ADC2_CR2_SWSTART
                if not self.eoc_timeout:
                    self.registers[c.ADC2_SR] = self.registers.get(c.ADC2_SR, 0) | c.ADC_SR_EOC
        if address in (c.TIM3_SR, c.TIM4_SR):
            after = before & (value | ~mask)  # rc_w0，掩码外写1
        if address in (c.GPIOB_BSRR, c.GPIOB_BRR):
            if address == c.GPIOB_BSRR:
                self.registers[c.GPIOB_ODR] |= value
            else:
                self.registers[c.GPIOB_ODR] &= ~value
            after = 0
        if address == c.TIM3_EGR:
            self._reset3_at = self._timer3_start = self.now
            after = 0
        if address == c.TIM3_CR1 and after & 1 and not before & 1:
            self._timer3_start = self._reset3_at = self.now
        if address == c.TIM4_EGR:
            self._reset_at = self._timer_start = self.now
            after = 0
        if address == c.TIM4_CR1 and after & 1 and not before & 1:
            self._timer_start = self._reset_at = self.now
        if address == c.TIM1_EGR:
            after &= ~c.TIM_EGR_UG
        self.registers[address] = after
        self.write_log.append((address, mask, value & mask, before, after))
        return before & mask, after & mask

    def claim_adc2_data_owner(self):
        if not self.running:
            raise c.SafetyError('目标已停止')
        self.adc2_owner = True

    def release_adc2_data_owner(self):
        self.adc2_owner = False

    def read_adc2_dr(self):
        if not self.adc2_owner:
            raise c.SafetyError('尚未取得ADC2所有权')
        if not self.running:
            raise c.SafetyError('目标已停止')
        self.registers[c.ADC2_SR] &= ~c.ADC_SR_EOC
        return self.adc_code

    def close(self):
        self.closed += 1


class HardwareContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.image = build_image()
        self.profile = load_profile(write_profile(self.root, self.image))
        self.fake = FakeF1Transport(self.image)
        self.controller = RealDeviceController(self.fake, self.profile,
                                               monotonic=time.perf_counter, sleep=lambda _: None)

    def tearDown(self):
        try:
            self.controller.close()
        finally:
            self._tmp.cleanup()

    def assert_preflight_rejected(self, mutate):
        fake = FakeF1Transport(self.image)
        mutate(fake)
        controller = RealDeviceController(fake, self.profile, sleep=lambda _: None)
        report = controller.preflight()
        self.assertFalse(report.ok)
        self.assertEqual(fake.write_log, [])
        self.assertNotIn(c.ADC1_DR, fake.read_log)

    def test_preflight_ok_reads_no_data_registers_and_writes_nothing(self):
        report = self.controller.preflight()
        self.assertTrue(report.ok, report.issues)
        self.assertEqual(self.fake.write_log, [])
        self.assertNotIn(c.ADC1_DR, self.fake.read_log)
        self.assertNotIn(c.ADC2_DR, self.fake.read_log)
        self.assertEqual(report.clocks['adcclk_hz'], 12_000_000)
        self.assertEqual(report.clocks['tim1_hz'], 72_000_000)

    def test_preflight_rejects_unknown_firmware_and_wrong_device(self):
        self.assert_preflight_rejected(lambda f: f.flash.__setitem__(100, f.flash[100] ^ 1))
        self.assert_preflight_rejected(
            lambda f: f.registers.__setitem__(c.IDCODE, 0x20036411))
        self.assert_preflight_rejected(
            lambda f: f.registers.__setitem__(c.FLASH_SIZE_REG, 128))
        self.assert_preflight_rejected(lambda f: setattr(f, 'running', False))

    def test_preflight_disables_busy_function_without_writing_it(self):
        mutations = [
            lambda f: f.registers.__setitem__(
                c.RCC_APB2ENR, f.registers[c.RCC_APB2ENR] | (1 << 10)),
            lambda f: f.registers.__setitem__(c.ADC2_CR2, c.ADC2_CR2_ADON),
            lambda f: f.registers.__setitem__(c.ADC2_CR1, 0x100),
            lambda f: f.registers.__setitem__(
                c.RCC_APB2ENR, f.registers[c.RCC_APB2ENR] | (1 << 11)),
            lambda f: f.registers.__setitem__(c.TIM1_CR1, 1),
            lambda f: f.registers.__setitem__(c.TIM1_CR1, 0x80),
            lambda f: f.registers.__setitem__(c.TIM1_CCER, 0x10),
            lambda f: f.registers.__setitem__(c.TIM1_CCMR2, 1),
            lambda f: f.registers.__setitem__(c.TIM1_CCMR1, 0x100),
            lambda f: f.registers.__setitem__(c.TIM1_DIER, 1),
            lambda f: f.registers.__setitem__(c.TIM1_BDTR, 1),
            lambda f: f.registers.__setitem__(c.AFIO_MAPR, 1 << 6),
            lambda f: f.registers.__setitem__(
                c.GPIOA_CRL, f.registers[c.GPIOA_CRL] | (0xB << 16)),
            lambda f: f.registers.__setitem__(
                c.GPIOA_CRH, f.registers[c.GPIOA_CRH] | 0xB),
            lambda f: f.registers.__setitem__(c.ADC1_CR1, 0x60000),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                fake = FakeF1Transport(self.image)
                mutate(fake)
                controller = RealDeviceController(fake, self.profile, sleep=lambda _: None)
                report = controller.preflight()
                function = 'adc' if index in (0, 1, 2, 12, 14) else 'pwm'
                self.assertTrue(report.ok, report)
                self.assertFalse(report.resources[function])
                self.assertIn(function, report.resources['reasons'])
                self.assertEqual(fake.write_log, [])
                self.assertNotIn(c.ADC1_DR, fake.read_log)
                self.assertTrue(controller.activate().ok)
                for address, mask, *_ in fake.write_log:
                    if function == 'adc':
                        self.assertNotIn(address, (c.ADC2_CR1, c.ADC2_CR2, c.ADC2_SMPR2,
                                                  c.ADC2_SQR1, c.ADC2_SQR3, c.GPIOA_CRL))
                        if address == c.RCC_APB2ENR:
                            self.assertFalse(mask & c.RCC_APB2ENR_ADC2EN)
                    else:
                        self.assertNotIn(address, (c.GPIOA_CRH, c.TIM1_CR1, c.TIM1_CCER,
                                                  c.TIM1_CCMR1, c.TIM1_BDTR, c.TIM1_EGR,
                                                  c.TIM1_PSC, c.TIM1_ARR, c.TIM1_CCR1))
                        if address == c.RCC_APB2ENR:
                            self.assertFalse(mask & c.RCC_APB2ENR_TIM1EN)
                self.assertTrue(controller.recover().ok)

    def test_activation_sequence_and_preserved_fields(self):
        old = dict(self.fake.registers)
        report = self.controller.activate()
        self.assertTrue(report.ok, report.issues)
        self.assertTrue(report.adc2_ready and report.tim1_ready)
        self.assertFalse(report.output_active)
        regs = self.fake.registers
        self.assertEqual(regs[c.GPIOA_CRL] & 0x000F0000, 0)
        self.assertEqual(regs[c.GPIOA_CRH] & 0xF, 0xB)
        self.assertEqual(regs[c.RCC_APB2ENR] & 0xC04, 0xC04)
        self.assertTrue(regs[c.ADC2_CR2] & c.ADC2_CR2_ADON)
        self.assertEqual(regs[c.ADC2_CR2] & 0x900, 0)
        self.assertFalse(regs[c.ADC2_CR2] & (c.ADC2_CR2_CAL | c.ADC2_CR2_RSTCAL))
        self.assertEqual(regs[c.ADC2_SMPR2] & 0x7000, 0x7000)
        self.assertEqual(regs[c.ADC2_SQR1] & 0x00F00000, 0)
        self.assertEqual(regs[c.ADC2_SQR3] & 0x1F, 4)
        self.assertEqual(regs[c.TIM1_CCER], 0)
        self.assertEqual(regs[c.TIM1_BDTR] & c.TIM_BDTR_MOE, 0)
        self.assertEqual(regs[c.TIM1_CR1] & c.TIM_CR1_CEN, 0)
        # 原程序资源与共享时钟分频保持原值。
        self.assertEqual(regs[c.RCC_CFGR], old[c.RCC_CFGR])
        self.assertEqual(regs[c.GPIOA_CRL] & ~0x000F0000,
                         old[c.GPIOA_CRL] & ~0x000F0000)
        self.assertEqual(regs[c.GPIOA_CRH] & ~0xF, old[c.GPIOA_CRH] & ~0xF)
        for address in (c.ADC1_CR1, c.ADC1_CR2, c.ADC2_SR):
            self.assertEqual(regs.get(address, 0), old.get(address, 0))
        written = {entry[0] for entry in self.fake.write_log}
        self.assertNotIn(c.ADC1_DR, written)
        self.assertNotIn(c.TIM1_CCMR2, written)

    def test_calibration_timeout_rolls_back(self):
        self.fake.stuck_rstcal = True
        report = self.controller.activate()
        self.assertFalse(report.ok)
        self.assertTrue(any('复位校准' in item for item in report.issues), report.issues)
        self.assertEqual(self.fake.registers[c.ADC2_CR2], 0)
        self.assertEqual(self.fake.registers[c.RCC_APB2ENR], (1 << 2) | (1 << 3) | (1 << 9))
        self.assertEqual(self.fake.registers[c.GPIOA_CRL], 0x444444B0)
        self.assertEqual(self.fake.registers[c.GPIOA_CRH], 0x44444444)

    def test_partial_initialization_failure_rolls_back_and_preserves_other_bits(self):
        self.fake.registers[c.GPIOA_CRL] = 0x123444B0
        self.fake.fail_write.add(c.ADC2_SQR3)
        report = self.controller.activate()
        self.assertFalse(report.ok)
        self.assertIn('激活失败', report.issues[0])
        self.assertEqual(self.fake.registers[c.GPIOA_CRL], 0x123444B0)
        self.assertEqual(self.fake.registers[c.GPIOA_CRH], 0x44444444)
        self.assertEqual(self.fake.registers[c.ADC2_CR1], 0)
        self.assertEqual(self.fake.registers[c.ADC2_CR2], 0)
        self.assertEqual(self.fake.registers[c.ADC2_SMPR2], 0)
        self.assertEqual(self.fake.registers[c.ADC2_SQR1], 0)
        self.assertEqual(self.fake.registers[c.RCC_APB2ENR],
                         (1 << 2) | (1 << 3) | (1 << 9))

    def test_drift_is_not_overwritten_and_other_runtime_bits_survive(self):
        self.assertTrue(self.controller.activate().ok)
        self.fake.registers[c.GPIOA_CRL] ^= 1 << 28
        self.fake.registers[c.GPIOA_CRL] = (
            self.fake.registers[c.GPIOA_CRL] & ~0x000F0000) | 0x00070000
        report = self.controller.recover()
        self.assertFalse(report.ok)
        self.assertTrue(any('所有权已变化' in item for item in report.errors), report.errors)
        self.assertEqual((self.fake.registers[c.GPIOA_CRL] >> 16) & 0xF, 7)
        self.assertTrue(self.fake.registers[c.GPIOA_CRL] & (1 << 28))

    def test_sampling_real_frame_uncertainty_and_csv(self):
        self.assertTrue(self.controller.activate().ok)
        self.fake.adc_code = 2048
        frame = self.controller.sample(SampleRequest(5, 0.0))
        self.assertTrue(frame.is_real)
        self.assertEqual(frame.source, c.REAL_SOURCE)
        self.assertEqual(frame.clock, c.REAL_CLOCK)
        self.assertEqual(len(frame.points), 5)
        self.assertTrue(all(point.time_uncertainty_s > 0 for point in frame.points))
        self.assertTrue(all(a.time_s < b.time_s
                            for a, b in zip(frame.points, frame.points[1:])))
        self.assertEqual({point.adc_code for point in frame.points}, {2048})
        with tempfile.TemporaryDirectory() as tmp:
            name = Path(tmp) / 'real.csv'
            c.export_csv(frame, name)
            rows = list(csv.DictReader(
                line for line in name.read_text(encoding='utf-8-sig').splitlines()
                if not line.startswith('#')))
        self.assertEqual(len(rows), 5)
        self.assertIn('time_uncertainty_s', rows[0])
        with self.assertRaises(c.SafetyError):
            self.fake.read32(c.ADC2_DR)

    def test_sampling_cancel_and_stopped_target_do_not_publish_partial_frames(self):
        self.assertTrue(self.controller.activate().ok)
        token = CancelToken()
        token.cancel()
        with self.assertRaises(c.CancelledError):
            self.controller.sample(SampleRequest(5, 0.0), token)
        self.fake.running = False
        with self.assertRaises(c.SafetyError):
            self.controller.sample(SampleRequest(5, 0.0))

    def test_pwm_quantization_endpoints_and_no_other_channels(self):
        self.assertTrue(self.controller.activate().ok)
        result = self.controller.set_pwm(1000, 50)
        self.assertTrue(result.ok, result.message)
        self.assertAlmostEqual(result.actual_hz, 1000, places=6)
        self.assertAlmostEqual(result.actual_duty, 50, places=6)
        self.assertEqual(self.fake.registers[c.TIM1_CCER] & 0xF, 1)
        self.assertTrue(self.fake.registers[c.TIM1_BDTR] & c.TIM_BDTR_MOE)
        self.assertTrue(self.fake.registers[c.TIM1_CR1] & c.TIM_CR1_CEN)

        zero = self.controller.set_pwm(1000, 0)
        self.assertTrue(zero.ok)
        self.assertEqual(self.fake.registers[c.TIM1_CCR1], 0)
        self.assertEqual(zero.actual_duty, 0)

        full = self.controller.set_pwm(1000, 100)
        self.assertTrue(full.ok)
        arr = self.fake.registers[c.TIM1_ARR]
        ccr = self.fake.registers[c.TIM1_CCR1]
        self.assertEqual(ccr, arr + 1)
        self.assertLessEqual(ccr, 65535)
        self.assertEqual(full.actual_duty, 100)

        for address, mask, value, _, _ in self.fake.write_log:
            if address == c.TIM1_CCER:
                self.assertEqual(mask, 0x0003)
                self.assertIn(value, (0, 1))
            self.assertNotIn(address, (c.TIM1_CCMR2, c.TIM1_CCR2, c.TIM1_CCR3, c.TIM1_CCR4))

    def test_communication_failure_never_claims_off_without_readback(self):
        self.assertTrue(self.controller.activate().ok)
        self.fake.fail_read.add(c.TIM1_CCER)
        result = self.controller.set_pwm(1000, 50)
        self.assertFalse(result.ok)
        self.assertEqual(result.output_state, 'unknown-may-still-output')
        self.fake.fail_read.clear()
        off = self.controller.output_off()
        self.assertTrue(off.ok)
        self.assertEqual(off.output_state, 'off-confirmed')

    def test_recovery_when_target_stopped_reports_unknown_and_errors(self):
        self.assertTrue(self.controller.activate().ok)
        self.assertTrue(self.controller.set_pwm(1000, 50).ok)
        self.fake.running = False
        report = self.controller.recover()
        self.assertFalse(report.ok)
        self.assertEqual(report.output_state, 'unknown-may-still-output')
        self.assertTrue(report.errors)

    def test_close_is_idempotent_and_does_not_reset(self):
        self.assertTrue(self.controller.activate().ok)
        report = self.controller.close()
        self.assertIsNotNone(report)
        self.assertEqual(self.fake.closed, 1)
        self.assertIsNone(self.controller.close())
        self.assertEqual(self.fake.closed, 1)
        self.assertFalse(hasattr(self.fake, 'reset'))


class TransportGuardContract(unittest.TestCase):
    def test_unopened_transport_cannot_read_or_write(self):
        transport = PyOCDSWD('exact')
        for address in (c.ADC1_DR, c.ADC2_DR, 0x20000000):
            with self.subTest(address=address), self.assertRaises(c.SafetyError):
                transport.read32(address)
        with self.assertRaises(c.SafetyError):
            transport.write_masked(c.ADC2_CR2, c.ADC2_CR2_ADON, c.ADC2_CR2_ADON)
        with self.assertRaises(c.SafetyError):
            transport.write_masked(c.ADC2_CR2, 0xFFFFFFFF, 0)
        with self.assertRaises(c.SafetyError):
            transport.read_flash_block(c.FLASH_BASE, 16)
        with self.assertRaises(c.SafetyError):
            transport.read_flash_block(c.FLASH_BASE - 4, 16)
        with self.assertRaises(c.SafetyError):
            transport.read_flash_block(c.FLASH_BASE, (1 << 20) + 4)
        with self.assertRaises(c.SafetyError):
            transport.read_adc2_dr()
        with self.assertRaises(c.SafetyError):
            transport.write32(c.ADC2_CR2, 1)

    def test_swd_clock_only_accepts_registered_steps(self):
        from swd_instruments.transport import ALLOWED_SWD_HZ, DEFAULT_SWD_HZ, SESSION_OPTIONS
        self.assertIn(DEFAULT_SWD_HZ, ALLOWED_SWD_HZ)
        self.assertEqual(PyOCDSWD('exact').swd_hz, DEFAULT_SWD_HZ)
        self.assertEqual(SESSION_OPTIONS['frequency'], DEFAULT_SWD_HZ)
        for value in ALLOWED_SWD_HZ:
            self.assertEqual(PyOCDSWD('exact', value).swd_hz, value)
        for value in (0, 99_999, 123_456, 4_600_000, 10_000_000, 480_000.0, '480000', None, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PyOCDSWD('exact', value)

    def test_write_mask_preserves_other_bits(self):
        fake = FakeF1Transport(build_image())
        before = fake.registers[c.GPIOA_CRL]
        fake.write_masked(c.GPIOA_CRL, 0x000F0000, 0)
        self.assertEqual(fake.registers[c.GPIOA_CRL] & ~0x000F0000,
                         before & ~0x000F0000)


if __name__ == '__main__':
    unittest.main()
