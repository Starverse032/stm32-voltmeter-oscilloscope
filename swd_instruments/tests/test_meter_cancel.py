"""Cancellation and session state with fake controllers/targets only."""
from dataclasses import fields, FrozenInstanceError
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from swd_instruments import core as c
from swd_instruments.contracts import CancelToken, PulseResult, SampleRequest
import test_meter
import test_selectable


class MeterCancellationTests(unittest.TestCase):
    def make(self, selectable):
        if selectable:
            helper = test_selectable.SelectionTests()
            ctrl = helper.make()
            fake = helper.target.fake
            reader = helper.target
            ctrl.confirm_general_mode(True)
            timer = ctrl.selection['meter'].timer
            from swd_instruments.pins import timer_registers
            t = timer_registers(timer)
            cleanup = ctrl.close
            inject = lambda: setattr(helper.target, 'signal', (timer, 1, .02, .0015))
        else:
            helper = test_meter.MeterTests()
            helper.setUp()
            ctrl, fake, reader = helper.controller, helper.fake, helper.fake
            fake.read_latency_s = .00001
            t = ctrl._timer
            cleanup = helper.tearDown
            inject = lambda: fake.inject_pb7(.02, .0015)
        self.addCleanup(cleanup)
        fake.registers[t.SR] = 0x400  # Preserve this pre-session flag.
        self.assertTrue(ctrl.activate().ok)
        self.assertTrue(ctrl.is_active)
        self.assertTrue(ctrl.set_pwm(1, 50).ok)
        self.assertTrue(ctrl.check_continuity().ok)
        return ctrl, fake, reader, t, inject

    def inject_once(self, reader, fake, timer, action):
        original = reader.read32
        fired = []
        def read(address):
            if address == timer.SR and fake.registers[timer.CR1] & 1 and not fired:
                fired.append(True)
                action()
            return original(address)
        return patch.object(reader, 'read32', side_effect=read), fired

    def test_cancel_keeps_every_function_and_meter_can_restart(self):
        for selectable in (False, True):
            for raised in (False, True):
                with self.subTest(selectable=selectable, raised=raised):
                    ctrl, fake, reader, t, inject = self.make(selectable)
                    txn = ctrl._txn
                    token = CancelToken()
                    def cancel():
                        if raised:
                            raise c.CancelledError('injected cancellation')
                        token.cancel()
                    hook, fired = self.inject_once(reader, fake, t, cancel)
                    meter_addresses = set(vars(t).values())
                    before = {a: fake.registers.get(a, 0) for a in txn.owned_addresses
                              if a not in meter_addresses}
                    with hook:
                        result = ctrl.measure_pulse(.1, token)
                    self.assertTrue(fired)
                    self.assertEqual(result.state, 'cancelled')
                    self.assertEqual(result.message, '测量已取消')
                    self.assertFalse(result.ok)
                    self.assertTrue(result.session_active)
                    self.assertTrue(ctrl.is_active)
                    self.assertIs(ctrl._txn, txn)
                    self.assertFalse(fake.registers[t.CR1] & 1)
                    self.assertFalse(fake.registers[t.CCER] & 0x11)
                    self.assertEqual(fake.registers[t.SR] & 0x65F, 0x400)
                    self.assertEqual({a: fake.registers.get(a, 0) for a in before}, before)
                    self.assertTrue(ctrl.sample(SampleRequest(2, .001)).is_real)
                    self.assertTrue(ctrl.set_pwm(50, 7.5).ok)
                    self.assertTrue(ctrl.check_continuity().ok)
                    inject()
                    result = ctrl.measure_pulse(1)
                    self.assertTrue(result.ok, result)
                    self.assertTrue(result.session_active)

    def test_real_error_reports_released_session(self):
        for selectable in (False, True):
            with self.subTest(selectable=selectable):
                ctrl, fake, reader, t, _ = self.make(selectable)
                def fail():
                    raise OSError('injected bus failure')
                hook, fired = self.inject_once(reader, fake, t, fail)
                with hook:
                    result = ctrl.measure_pulse(.1)
                self.assertTrue(fired)
                self.assertEqual(result.state, 'error')
                self.assertFalse(result.session_active)
                self.assertFalse(ctrl.is_active)
                self.assertFalse(ctrl.measure_pulse().session_active)

    def test_cancel_during_stop_and_cancelled_transaction_token(self):
        for selectable in (False, True):
            with self.subTest(selectable=selectable):
                ctrl, fake, _, t, _ = self.make(selectable)
                token = CancelToken()
                ctrl._txn.cancel_token = token
                original = ctrl._clear_meter_status
                fired = []
                def cancel_on_stop():
                    if not fake.registers[t.CR1] & 1 and fake.registers[t.CR1] & 4 and not fired:
                        fired.append(True)
                        token.cancel()
                        raise c.CancelledError('cancelled while stopping')
                    return original()
                with patch.object(ctrl, '_clear_meter_status', side_effect=cancel_on_stop):
                    result = ctrl.measure_pulse(.01, token)
                self.assertEqual(result.state, 'cancelled')
                self.assertTrue(result.session_active)
                self.assertFalse(fake.registers[t.CR1] & 1)
                self.assertIsNone(ctrl._txn.cancel_token)
                self.assertTrue(ctrl.set_pwm(1, 50).ok)

    def test_sample_and_continuity_cancellation_keep_session(self):
        for selectable in (False, True):
            with self.subTest(selectable=selectable):
                ctrl, fake, reader, t, _ = self.make(selectable)
                token = CancelToken()
                token.cancel()
                with self.assertRaises(c.CancelledError):
                    ctrl.sample(SampleRequest(2, .001), token)
                self.assertTrue(ctrl.is_active)
                with patch.object(ctrl, '_sleep', side_effect=c.CancelledError('cancel continuity')):
                    self.assertFalse(ctrl.check_continuity().ok)
                self.assertTrue(ctrl.is_active)
                self.assertTrue(ctrl.sample(SampleRequest(2, .001)).is_real)
                token = CancelToken()
                original_sleep = ctrl._sleep
                def cancel_interval(seconds):
                    original_sleep(seconds)
                    token.cancel()
                with patch.object(ctrl, '_sleep', side_effect=cancel_interval):
                    with self.assertRaises(c.CancelledError):
                        ctrl.sample(SampleRequest(4, .01), token)
                self.assertTrue(ctrl.is_active)
                self.assertTrue(ctrl.sample(SampleRequest(2, .001)).is_real)
                self.assertTrue(ctrl.set_pwm(1, 50).ok)
                self.assertTrue(ctrl.check_continuity().ok)

    def test_activation_rollback_ignores_cancelled_write_token(self):
        for selectable in (False, True):
            with self.subTest(selectable=selectable):
                ctrl, fake, _, _, _ = self.make(selectable)
                # An activation request against an active session must retain it.
                token = CancelToken()
                token.cancel()
                self.assertFalse(ctrl.activate(token).ok)
                self.assertTrue(ctrl.is_active)
                self.assertTrue(ctrl.recover().ok)
                if selectable:
                    ctrl.confirm_general_mode(True)
                token = CancelToken()
                before = dict(fake.registers)
                def cancel_calibration(seconds):
                    fake.advance(seconds)
                    token.cancel()
                with patch.object(ctrl, '_sleep', side_effect=cancel_calibration):
                    result = ctrl.activate(token)
                self.assertFalse(result.ok)
                self.assertFalse(ctrl.is_active)
                for address, value in before.items():
                    if address not in c.COMMAND_REGISTERS:
                        self.assertEqual(fake.registers.get(address), value, hex(address))

    def test_contract_and_owned_sources(self):
        self.assertEqual([f.name for f in fields(PulseResult)], [
            'ok', 'state', 'period_s', 'high_s', 'frequency_hz', 'duty_percent',
            'tick_s', 'message', 'session_active'])
        result = PulseResult(False, 'cancelled', None, None, None, None, None, '测量已取消')
        self.assertTrue(result.session_active)
        with self.assertRaises(FrozenInstanceError):
            result.session_active = False
        root = Path(__file__).resolve().parents[1]
        for name in ('hardware.py', 'selectable.py', 'contracts.py'):
            self.assertIsNone(re.search(r'\?{3,}|\ufffd', (root / name).read_text(encoding='utf-8')), name)
