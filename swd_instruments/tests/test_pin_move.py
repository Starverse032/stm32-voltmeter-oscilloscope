"""离线 TIM3/PB10/可选 ADC 引脚验证；不创建真实传输。"""
import json
import tempfile
import unittest
from pathlib import Path
from swd_instruments import core as c
from swd_instruments.contracts import CancelToken, SampleRequest
from swd_instruments.hardware import RealDeviceController
from swd_instruments.profiles import load_profile, TRUSTED_PROFILES, RESOURCE_KEYS, ProfileError
from test_hardware import FakeF1Transport, build_image, write_profile


class PA6MeterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_profile(Path(self.tmp.name), build_image())
        self.data = json.loads(self.path.read_text())
        self.data['resources'].update(tim3_free=True, pb10_free=True, pa6_free=True, pb7_free=False, pb6_free=False, tim4_free=False,
                                     pa7_free=True, adc_input='PA4', meter_input='PA6', continuity_input='PB10')
        self.register()
        self.fake = FakeF1Transport(build_image())
        self.controller = RealDeviceController(self.fake, load_profile(self.path),
            monotonic=lambda: self.fake.now, sleep=self.advance_meter_time)

    def advance_meter_time(self, seconds):
        # Virtual polling can be sparse for slow waveforms. The model still
        # generates every intervening edge and checks overcapture/overflow.
        if self.fake.registers[c.TIM3_CR1] & 1 and self.fake.pa6_signal:
            period, _ = self.fake.pa6_signal
            seconds = max(seconds, min(period / 8, .02))
        self.fake.advance(seconds)

    def register(self):
        self.path.write_text(json.dumps(self.data), encoding='utf-8')
        r = self.data['resources']
        TRUSTED_PROFILES['fake-f103']['resources'] = {k: r.get(k, False) for k in RESOURCE_KEYS}

    def tearDown(self):
        self.controller.close()
        self.tmp.cleanup()

    def test_preflight_busy_timer_pin_remap_and_clock(self):
        for address, value in ((c.RCC_APB1ENR, 2), (c.TIM3_CR1, 1),
                               (c.TIM3_SMCR, 4), (c.TIM3_DIER, 1),
                               (c.TIM3_CCMR1, 1), (c.TIM3_CCMR2, 1),
                               (c.TIM3_CCER, 1), (c.AFIO_MAPR, 0x400),
                               (c.GPIOA_CRL, 0x4B4444B0)):
            original = self.fake.registers[address]
            self.fake.registers[address] = value
            pre = self.controller.preflight()
            self.assertTrue(pre.ok, pre)
            self.assertFalse(pre.resources['meter'], hex(address))
            self.assertTrue(pre.resources['adc'] and pre.resources['pwm'] and pre.resources['continuity'])
            self.assertEqual(self.fake.write_log, [])
            self.fake.registers[address] = original

    def test_tim3_mapping_matches_vendor_header(self):
        import re
        header = (Path(__file__).resolve().parent / 'data' / 'stm32f10x.h').read_text(encoding='latin-1')
        self.assertRegex(header, r'TIM3_BASE\s+\(APB1PERIPH_BASE \+ 0x0400\)')
        timer = header[:header.index('} TIM_TypeDef;')].rsplit('typedef struct', 1)[1]
        members = re.findall(r'(?:__IO )?uint16_t\s+(\w+);', timer)
        for name in ('CR1', 'SMCR', 'DIER', 'SR', 'EGR', 'CCMR1', 'CCMR2', 'CCER', 'PSC', 'ARR', 'CCR1', 'CCR2'):
            self.assertEqual(getattr(c, 'TIM3_' + name), 0x40000400 + members.index(name) * 2)
        self.assertEqual(c.REAL_WRITE_MASKS[c.GPIOB_CRH], 0xFFFFFFFF)
        self.assertNotIn(c.GPIOB_ODR, c.REAL_WRITE_MASKS)

    def test_signals(self):
        self.assertTrue(self.controller.activate().ok)
        for frequency, duty in ((50, 7.5), (20000, 50), (5, 50), (100000, 50), (0.5, 50), (523, 30), (988, 70)):
            with self.subTest(frequency=frequency):
                self.fake.inject_pa6(1 / frequency, duty / 100 / frequency)
                result = self.controller.measure_pulse(15)
                self.assertTrue(result.ok, result)
                self.assertAlmostEqual(result.period_s, 1 / frequency, delta=result.tick_s * 2)
                self.assertAlmostEqual(result.high_s, duty / 100 / frequency, delta=result.tick_s * 2)
                self.assertEqual(self.fake.registers[c.TIM3_CR1] & 1, 0)

    def test_no_signal_high_and_low(self):
        self.assertTrue(self.controller.activate().ok)
        for level in (0, 1):
            self.fake.registers[c.GPIOB_IDR] = level << 7
            self.fake.inject_pa6()
            self.assertEqual(self.controller.measure_pulse(.1).state, 'no-signal')

    def test_slow_and_fast_out_of_range(self):
        self.controller.activate()
        for freq in (.25, 200000):
            self.fake.inject_pa6(1/freq, .5/freq)
            self.assertEqual(self.controller.measure_pulse(25 if freq < 1 else .1).state, 'out-of-range')

    def test_not_ready_cancel_stop_and_write_failure(self):
        self.assertEqual(self.controller.measure_pulse().state, 'not-ready')
        self.controller.activate()
        token = CancelToken(); token.cancel()
        # 取消不是故障：只停频率计，会话与其他功能保持接管（2026-11-11 实物缺陷回归）。
        cancelled = self.controller.measure_pulse(cancel=token)
        self.assertEqual(cancelled.state, 'cancelled')
        self.assertTrue(cancelled.session_active)
        self.assertTrue(self.controller.is_active)
        self.fake.fail_write.add(c.TIM3_CCMR1)
        failed = self.controller.measure_pulse()
        self.assertEqual(failed.state, 'error')
        self.assertFalse(failed.session_active)
        self.assertFalse(self.controller.is_active)
        self.assertEqual(self.fake.registers[c.RCC_APB1ENR], 0)
        self.fake.fail_write.clear()
        self.assertTrue(self.controller.activate().ok)
        def stopped(seconds):
            self.fake.advance(seconds)
            self.fake.running = False
        self.controller._sleep = stopped
        self.assertEqual(self.controller.measure_pulse().state, 'error')

    def test_busy_meter_keeps_adc(self):
        self.fake.registers[c.TIM3_DIER] = 1
        pre = self.controller.preflight()
        self.assertTrue(pre.ok)
        self.assertFalse(pre.resources['meter'])
        active = self.controller.activate()
        self.assertTrue(active.adc2_ready)
        self.assertFalse(active.meter_ready)
        self.assertFalse(any(a == c.RCC_APB1ENR for a, *_ in self.fake.write_log))

    def test_continuity_and_recovery_and_guards(self):
        initial = dict(self.fake.registers)
        self.controller.activate()
        self.fake.inject_pa6(.02, .0015)
        self.assertTrue(self.controller.measure_pulse().ok)
        for levels, closed in (([0]*5, True), ([1]*5, False), ([0,1,0,1,0], None)):
            self.fake.pb10_levels = levels[:]
            self.assertIs(self.controller.check_continuity().closed, closed)
        report = self.controller.recover()
        self.assertTrue(report.ok, report)
        # CCR是硬件捕获结果，不能当作配置重放；所有写过的可恢复寄存器精确还原。
        for address, *_ in self.fake.write_log:
            if address not in c.COMMAND_REGISTERS and address != c.TIM3_SR:
                self.assertEqual(self.fake.registers[address], initial[address], hex(address))
        self.assertEqual(self.fake.registers[c.GPIOB_ODR], initial[c.GPIOB_ODR])
        self.assertEqual(self.fake.registers[c.TIM3_SR] & 0x1E5F,
                         initial[c.TIM3_SR] & 0x1E5F)
        for address, *_ in self.fake.write_log:
            self.assertIn(address, c.REAL_WRITE_MASKS)
        for address in (c.ADC1_DR, c.ADC2_DR, *c.COMMAND_REGISTERS):
            self.assertNotIn(address, self.fake.read_log)

    def test_subsets_and_pa7(self):
        self.data['resources'].update(tim1_ch1_free=False, pa8_free=False,
                                      pa4_free=False, adc_input='PA7')
        self.register()
        self.controller.profile = load_profile(self.path)
        self.fake.registers[c.TIM1_CR1] = 1
        active = self.controller.activate()
        self.assertTrue(active.ok, active)
        self.assertFalse(active.tim1_ready)
        self.assertEqual(self.fake.registers[c.ADC2_SQR3], 7)
        self.assertEqual(self.fake.registers[c.ADC2_SMPR2] & (7 << 21), 7 << 21)
        self.assertEqual(self.fake.registers[c.GPIOA_CRL] >> 28, 0)
        self.assertTrue(self.controller.recover().ok)

    def test_profile_extra_grant_and_bad_pin_rejected(self):
        self.data['resources']['meter_input'] = None
        self.data['resources']['tim3_free'] = False
        self.register()
        self.data['resources']['tim3_free'] = True
        self.path.write_text(json.dumps(self.data))
        with self.assertRaises(ProfileError): load_profile(self.path)
        self.data['resources']['adc_input'] = 'PB0'
        self.path.write_text(json.dumps(self.data))
        with self.assertRaises(ProfileError): load_profile(self.path)

    def test_model_rejects_bad_configuration(self):
        self.controller.activate()
        self.fake.inject_pa6(.02, .0015)
        original = self.fake.write_masked
        def wrong(address, mask, value):
            before, after = original(address, mask, value)
            if address == c.TIM3_SMCR: self.fake.registers[address] = 0
            return before, after
        self.fake.write_masked = wrong
        self.assertFalse(self.controller.measure_pulse(.1).ok)

    def test_original_pullup_and_oled_bits_preserved(self):
        self.fake.registers[c.GPIOB_ODR] = 0x700
        self.controller.activate()
        self.controller.check_continuity()
        self.fake.registers[c.GPIOB_ODR] ^= 0x100  # OLED运行期变化
        self.assertTrue(self.controller.recover().ok)
        self.assertEqual(self.fake.registers[c.GPIOB_ODR], 0x600)
        self.assertFalse(any(a == c.GPIOB_BRR for a, *_ in self.fake.write_log))

    def test_takeover_refuses_writes_and_recovery(self):
        self.controller.activate()
        self.fake.registers[c.TIM3_CR1] = 1
        before = len(self.fake.write_log)
        self.assertEqual(self.controller.measure_pulse().state, 'error')
        # 所有权漂移时事务逐字段拒绝覆盖；不应启动计数或改动CCER。
        self.assertEqual(self.fake.registers[c.TIM3_CR1], 1)
        self.assertFalse(self.controller.recover().ok)

    def test_tim3_clock_derivation(self):
        self.assertEqual(c.derive_clocks(0, 8000000).tim3_hz, 8000000)
        self.assertEqual(c.derive_clocks(4 << 8, 8000000).tim3_hz, 8000000)
        self.assertEqual(c.derive_clocks(5 << 8, 8000000).tim3_hz, 4000000)

    def test_continuity_swd_latency(self):
        self.assertTrue(self.controller.activate().ok)
        # 5 ms reproduces the host failure. 50 ms gives headroom for 100 kHz.
        for latency in (.005, .05):
            self.fake.read_latency_s = latency
            for levels, closed in (([0]*5, True), ([1]*5, False),
                                   ([0, 1, 0, 1, 0], None)):
                with self.subTest(latency=latency, closed=closed):
                    self.fake.pb10_levels = levels[:]
                    start = len(self.fake.read_log)
                    result = self.controller.check_continuity()
                    self.assertTrue(result.ok, result)
                    self.assertIs(result.closed, closed)
                    self.assertEqual(self.fake.read_log[start:].count(c.GPIOB_IDR), 5)
        self.fake.read_latency_s = 0
        self.assertTrue(self.controller.recover().ok)
        self.assertEqual(self.fake.registers[c.GPIOB_CRH] >> 8 & 15, 4)
        self.assertEqual(self.fake.registers[c.GPIOB_ODR] & 0x400, 0)

    def test_continuity_late_read_and_last_read_timeout(self):
        from swd_instruments.hardware import CONTINUITY_TIMEOUT_S
        self.assertTrue(self.controller.activate().ok)
        original = self.fake.read32
        for late_index in (1, 5):
            count = 0
            def delayed(address):
                nonlocal count
                if address == c.GPIOB_IDR:
                    count += 1
                    if count == late_index:
                        self.fake.advance(CONTINUITY_TIMEOUT_S + .01)
                return original(address)
            self.fake.read32 = delayed
            result = self.controller.check_continuity()
            self.assertFalse(result.ok)
            self.assertIn('超时', result.message)
            self.assertEqual(count, late_index)
        self.fake.read32 = original

    def test_continuity_stop_cancel_and_environment_change(self):
        self.assertTrue(self.controller.activate().ok)
        original = self.fake.read32
        token = CancelToken()
        self.controller._txn.cancel_token = token
        for failure in ('stop', 'cancel', 'environment'):
            count = 0
            def changed(address):
                nonlocal count
                value = original(address)
                if address == c.GPIOB_IDR:
                    count += 1
                    if count == 5:
                        if failure == 'stop':
                            self.fake.running = False
                        elif failure == 'cancel':
                            token.cancel()
                        else:
                            self.fake.registers[c.AFIO_MAPR] ^= 0x400
                return value
            self.fake.read32 = changed
            self.assertFalse(self.controller.check_continuity().ok)
            self.fake.running = True
            token = CancelToken()
            self.controller._txn.cancel_token = token
            self.fake.registers[c.AFIO_MAPR] = 0
        self.fake.read32 = original

    def test_tim3_status_restored_and_existing_flags_preserved(self):
        for initial in (0, 8, 0x10, 0x400, 1, 0x1E59):
            with self.subTest(initial=initial):
                self.fake.registers[c.TIM3_SR] = initial
                self.assertTrue(self.controller.activate().ok)
                self.fake.inject_pa6(.02, .0015)
                start = len(self.fake.write_log)
                result = self.controller.measure_pulse(2)
                self.assertEqual(result.ok, not bool(initial & 1), result)
                if initial & 1:
                    self.assertIn('UIF', result.message)
                self.assertEqual(self.fake.registers[c.TIM3_SR] & 0x1E5F, initial)
                self.assertTrue(self.controller.recover().ok)
                self.assertEqual(self.fake.registers[c.TIM3_SR] & 0x1E5F, initial)
                for address, mask, value, *_ in self.fake.write_log[start:]:
                    if address == c.TIM3_SR:
                        self.assertEqual(mask & initial, 0)
                        self.assertEqual(value, 0)

    def test_tim3_existing_capture_flags_avoid_destructive_ccr_reads(self):
        self.fake.registers[c.TIM3_SR] = 6
        self.assertTrue(self.controller.activate().ok)
        start = len(self.fake.read_log)
        self.assertFalse(self.controller.measure_pulse().ok)
        self.assertNotIn(c.TIM3_CCR1, self.fake.read_log[start:])
        self.assertNotIn(c.TIM3_CCR2, self.fake.read_log[start:])
        self.assertTrue(self.controller.recover().ok)
        self.assertEqual(self.fake.registers[c.TIM3_SR], 6)

    def test_tim3_model_compare_flags_without_channel_enable(self):
        self.assertTrue(self.controller.activate().ok)
        txn = self.controller._txn
        txn.write(c.TIM3_ARR, 0xFFFF, 9)
        txn.write(c.TIM3_CR1, 5, 5)
        self.fake.advance(.001)
        self.assertEqual(self.fake.read32(c.TIM3_SR) & 0x18, 0x18)
        self.assertEqual(self.fake.registers[c.TIM3_CCER], 0)
        self.assertEqual(self.fake.registers[c.TIM3_SR] & 0x1800, 0)
        self.assertTrue(self.controller.recover().ok)
        self.assertEqual(self.fake.registers[c.TIM3_SR] & 0x1E5F, 0)

    def test_tim3_measurement_read_failure_cleans_status(self):
        self.assertTrue(self.controller.activate().ok)
        self.fake.inject_pa6(.02, .0015)
        original = self.fake.read32
        failed = False
        def fail_once(address):
            nonlocal failed
            if address == c.TIM3_CCR1 and not failed:
                failed = True
                raise OSError('capture read failed')
            return original(address)
        self.fake.read32 = fail_once
        self.assertFalse(self.controller.measure_pulse(2).ok)
        self.assertTrue(failed)
        self.assertEqual(self.fake.registers[c.TIM3_CR1], 0)
        self.assertEqual(self.fake.registers[c.TIM3_SR] & 0x1E5F, 0)
        self.assertTrue(self.controller.recover().ok)

    def test_transport_commands_and_rc_w0(self):
        from types import SimpleNamespace
        from swd_instruments.transport import PyOCDSWD
        writes = []
        reads = []
        class Target:
            def is_running(self): return True
            def read32(self, address):
                if address in c.COMMAND_REGISTERS: raise AssertionError('命令不可读')
                reads.append(address)
                return 0x607
            def write32(self, address, value): writes.append((address, value))
        transport = PyOCDSWD('fake-only')
        transport.session = SimpleNamespace(target=Target())
        transport._write_lease_session = id(transport.session)
        transport.activate_selection({a: c.REAL_WRITE_MASKS[a] for a in c.COMMAND_REGISTERS} | {c.TIM3_SR: 4})
        for address in c.COMMAND_REGISTERS:
            transport.write_masked(address, c.REAL_WRITE_MASKS[address], c.REAL_WRITE_MASKS[address])
        transport.write_masked(c.TIM3_SR, 4, 0)
        self.assertEqual(writes[-1], (c.TIM3_SR, 0xFFFFFFFB))
        self.assertEqual(reads, [c.TIM3_SR, c.TIM3_SR])
