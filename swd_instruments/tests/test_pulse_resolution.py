"""Host's 2026-11-11 PA8 -> PA6 observations, exercised without hardware."""
import unittest

from swd_instruments import core as c
from swd_instruments.pins import CANDIDATES, METER, timer_registers
import test_selectable
from test_hardware import FakeF1Transport, build_image


# Configured period/high time -> expected capture at the old 1us tick.
# The 50Hz period looked correct in the host report, while its high time was short.
# 1kHz/20kHz/100kHz explicitly showed N-1 for the captured period.
HOST_FIXTURES = (
    (50, .02, .0015, 19999, 1499),
    (1000, .001, .00025, 999, 249),
    (20000, .00005, .000025, 49, 24),
    (100000, .00001, .000005, 9, 4),
)

# Task J host table. Expectations use configured, TIM1-quantised times,
# rather than reproducing the erroneous readings in the measured column.
QUANTISED_FIXTURES = (
    ('50Hz', 20000.14e-6, .0015),
    ('988.0067Hz', 1012.14e-6, 1012.14e-6 / 2),
    ('1000Hz', .001, .00025),
    ('20kHz', .00005, .000025),
    ('100kHz', .00001, .000005),
    ('5Hz', .2, .1),
)


class PulseResolutionTests(unittest.TestCase):
    def make(self, candidate):
        helper = test_selectable.SelectionTests()
        controller = helper.make(dict.fromkeys(CANDIDATES) | {'meter': candidate.key, 'occupied': []})
        controller.confirm_general_mode(True)
        self.assertTrue(controller.activate().ok)
        return helper, controller

    def test_host_fixtures_on_every_timer_and_input(self):
        for candidate in METER:
            for frequency, period, high, _, _ in HOST_FIXTURES:
                with self.subTest(candidate=candidate.key, frequency=frequency):
                    helper, controller = self.make(candidate)
                    try:
                        helper.target.signal = candidate.timer, candidate.channel, period, high
                        result = controller.measure_pulse(.2)
                        self.assertTrue(result.ok, result)
                        self.assertAlmostEqual(result.period_s, period, delta=result.tick_s * .01)
                        self.assertAlmostEqual(result.high_s, high, delta=result.tick_s * .01)
                        self.assertAlmostEqual(result.frequency_hz, frequency, delta=frequency * 1e-10)
                        if frequency >= 20000:
                            self.assertEqual(result.tick_s, 1 / 72_000_000)
                            expected_ticks = 3600 if frequency == 20000 else 720
                            self.assertAlmostEqual(result.period_s / result.tick_s, expected_ticks)
                            t = timer_registers(candidate.timer)
                            prescalers = [v for a, _, v in helper.transport.write_log if a == t.PSC]
                            self.assertEqual(prescalers, [0])
                    finally:
                        self.assertTrue(controller.recover().ok)

    def test_edge_model_produces_host_capture_values(self):
        for timer, ti in (('TIM3', 1), ('TIM4', 2)):
            for frequency, period, high, period_ccr, high_ccr in HOST_FIXTURES:
                with self.subTest(timer=timer, frequency=frequency):
                    fake = FakeF1Transport(build_image())
                    t = timer_registers(timer)
                    fake.registers.update({c.RCC_APB1ENR: 2 if timer == 'TIM3' else 4,
                        t.PSC: 71, t.ARR: 65535, t.CR1: 5, t.DIER: 0,
                        t.SMCR: 0x54 if ti == 1 else 0x64,
                        t.CCMR1: 0x201 if ti == 1 else 0x102,
                        t.CCER: 0x31 if ti == 1 else 0x13})
                    if ti == 1:
                        fake.inject_pa6(period, high)
                    else:
                        fake.inject_pb7(period, high)
                    fake.advance(period * 3 + high * 1.1)
                    fake.read32(t.SR)
                    self.assertEqual(fake.registers[getattr(t, f'CCR{ti}')], period_ccr)
                    self.assertEqual(fake.registers[getattr(t, f'CCR{3 - ti}')], high_ccr)

    def test_quantised_host_table_uses_finest_fitting_range(self):
        for candidate in METER:
            for label, period, high in QUANTISED_FIXTURES:
                with self.subTest(candidate=candidate.key, signal=label):
                    helper, controller = self.make(candidate)
                    try:
                        helper.target.signal = candidate.timer, candidate.channel, period, high
                        result = controller.measure_pulse(.2)
                        self.assertTrue(result.ok, result)
                        self.assertAlmostEqual(result.period_s, period, delta=result.tick_s)
                        self.assertAlmostEqual(result.high_s, high, delta=result.tick_s)
                        if label == '988.0067Hz':
                            self.assertLessEqual(result.tick_s, 30e-9)
                        if period * 72_000_000 > 65536:
                            self.assertGreaterEqual(result.period_s / result.tick_s, 32000)
                        t = timer_registers(candidate.timer)
                        prescalers = [v for a, _, v in helper.transport.write_log if a == t.PSC]
                        self.assertEqual(prescalers[0], 0)
                        # A factor-of-two-finer tick would overflow at the selected range.
                        if len(prescalers) > 1:
                            self.assertGreater(period / (result.tick_s / 2), 65536)
                    finally:
                        self.assertTrue(controller.recover().ok)

    def test_boundaries_allow_tick_error_and_reject_clear_outliers(self):
        for candidate in METER:
            periods = ((719 / 72_000_000, True), (.00001, True),
                       (721 / 72_000_000, True), (2, True),
                       (2 - 4096 / 72_000_000, True),
                       (2 + 4096 / 72_000_000, True),
                       (1 / 110000, False), (1 / .4, False))
            for period, accepted in periods:
                with self.subTest(candidate=candidate.key, period=period):
                    helper, controller = self.make(candidate)
                    try:
                        helper.target.signal = candidate.timer, candidate.channel, period, period / 2
                        result = controller.measure_pulse(.2)
                        self.assertEqual(result.ok, accepted, result)
                        if accepted:
                            self.assertAlmostEqual(result.period_s, period, delta=result.tick_s)
                        else:
                            self.assertEqual(result.state, 'out-of-range')
                    finally:
                        self.assertTrue(controller.recover().ok)

    def test_slow_signal_advances_on_overflow_and_reports_tick(self):
        # Run the event-driven model for slow signals, including noninteger tick counts.
        import test_pin_move
        for frequency in (988, 5, .5):
            with self.subTest(frequency=frequency):
                case = test_pin_move.PA6MeterTests()
                case.setUp()
                try:
                    self.assertTrue(case.controller.activate().ok)
                    case.fake.inject_pa6(1 / frequency, .5 / frequency)
                    result = case.controller.measure_pulse(15)
                    self.assertTrue(result.ok, result)
                    self.assertAlmostEqual(result.period_s, 1 / frequency, delta=result.tick_s * .51)
                    t = timer_registers('TIM3')
                    prescalers = [v for a, _, v, *_ in case.fake.write_log if a == t.PSC]
                    self.assertEqual(prescalers[0], 0)
                    self.assertGreater(prescalers[-1], 0)
                    if frequency == .5:
                        self.assertGreaterEqual(result.period_s / result.tick_s, 32000)
                        # Event-driven overflow search plus two fresh consistent
                        # captures must fit the original 15-second user deadline.
                        self.assertLess(case.fake.now, 14)
                finally:
                    case.tearDown()

    def test_coarsest_range_and_timeout_budget(self):
        candidate = next(p for p in METER if p.key == 'TIM2_CH2/PA1')
        helper, controller = self.make(candidate)
        try:
            # All ranges overflow. The fake sets UIF immediately for this unrepresentable period.
            helper.target.signal = candidate.timer, candidate.channel, 100, 50
            started = helper.target.fake.now
            result = controller.measure_pulse(.1)
            t = timer_registers(candidate.timer)
            prescalers = [v for a, _, v in helper.transport.write_log if a == t.PSC]
            self.assertEqual(prescalers, [(1 << exponent) - 1 for exponent in range(17)])
            self.assertGreaterEqual(result.tick_s, .0009)
            self.assertLess(helper.target.fake.now - started, .02)
        finally:
            self.assertTrue(controller.recover().ok)

    def test_gui_nanosecond_format(self):
        from swd_instruments.gui import InstrumentWindow
        self.assertEqual(InstrumentWindow._pulse_time(1 / 72_000_000), '13.9 ns')
        self.assertEqual(InstrumentWindow._pulse_time(1e-6), '1 µs')
        self.assertEqual(InstrumentWindow._pulse_time(.0015), '1.5 ms')
