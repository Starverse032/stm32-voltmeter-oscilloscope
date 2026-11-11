"""Descriptor-based controller. Preflight is read-only; ownership is journaled."""
import hashlib
import time
from types import SimpleNamespace
from . import core as c
from .contracts import ActivationReport, PreflightReport, RecoveryReport, PwmResult, ContinuityResult
from .hardware import RealDeviceController, RealRegisterTransaction
from .pins import CANDIDATES, DEFAULT_OCCUPIED, timer_registers, clock, union_masks
from .profiles import TRUSTED_PROFILES, assert_trusted_profile, verify_local_artifact

RISK = ('原程序以后可能启用相同外设或引脚而相互干扰；工具能检测大部分变化并停止，'
        '但不能保证零影响。ADC仅限0–3.3V；非FT数字输入禁止直接接5V。')


def restricted(profile, p):
    if profile is None:
        return False
    r = TRUSTED_PROFILES[profile.profile_id].get('restrictions', {})
    return p.pin in r.get('pins', ()) or p.timer in r.get('timers', ())


class SelectionTransaction(RealRegisterTransaction):
    def __init__(self, transport, baseline):
        super().__init__(transport)
        self.baseline = baseline

    def write(self, address, mask, value, **kwargs):
        owned = self._owned.get(address)
        new = mask & ~(owned.mask if owned else 0)
        if new and address in self.baseline and address not in c.COMMAND_REGISTERS:
            kwargs.update(guard_mask=new, guard_value=self.baseline[address])
        return super().write(address, mask, value, **kwargs)


class SelectableController(RealDeviceController):
    def __init__(self, transport, profile, selection=None, general_mode=False,
                 monotonic=time.monotonic, sleep=time.sleep):
        if profile is not None:
            assert_trusted_profile(profile)
        elif not general_mode:
            raise c.SafetyError('需要登记profile或明确选择通用模式')
        self.transport, self.profile = transport, profile
        self.general_mode = profile is None and general_mode
        self.general_confirmed = False
        self._monotonic, self._sleep = monotonic, sleep
        self._txn = None
        self._activated = self._adc2_owned = self._tim1_owned = False
        self._meter_owned = self._continuity_owned = False
        self._output_state = 'not-owned'
        self._protected = {}
        self._conversion_pending = False
        self._operation_cancel = None
        self._sequence = 0
        self.clocks = None
        selection = dict(selection or {})
        self.occupied = frozenset(selection.pop('occupied', DEFAULT_OCCUPIED))
        defaults = dict(adc=profile.adc_input if profile else 'PA4',
                        pwm='TIM1_CH1/PA8', meter='TIM3_CH1/PA6', continuity='PB10')
        self.selection = {}
        self._explicit = set(selection)
        for function, candidates in CANDIDATES.items():
            allowed = [p for p in candidates if p.pin not in self.occupied and not restricted(profile, p)
                       and not any(q and (q.pin == p.pin or q.timer and q.timer == p.timer)
                                   for q in self.selection.values())]
            key = selection.get(function, defaults[function])
            p = next((p for p in candidates if p == key or p.key == key), None)
            if function not in selection and p not in allowed:
                p = allowed[0] if allowed else None
            if function in selection and key is not None and p is None:
                raise c.SafetyError(f'未知候选 {function}: {key}')
            self.selection[function] = p

    @property
    def hse_hz(self):
        return self.profile.hse_hz if self.profile else 8_000_000

    def _candidate_reason(self, function, p):
        r = self._preflight_read
        if p.pin in self.occupied:
            return '物理占用引脚'
        if restricted(self.profile, p):
            return '登记profile已知使用此资源'
        if (r(p.gpio) >> p.shift) & 15 not in (0, 4):
            return '引脚配置不空闲'
        if p.timer:
            t = timer_registers(p.timer)
            a, bit = clock(p.timer)
            remap = dict(TIM1=0xC0, TIM2=0x300, TIM3=0xC00, TIM4=0x1000)[p.timer]
            if r(c.AFIO_MAPR) & remap:
                return '定时器已重映射，工具不修改AFIO'
            if r(a) & bit:
                return '定时器时钟已开启'
            for name in ('CR1', 'SMCR', 'DIER', 'CCMR1', 'CCMR2', 'CCER'):
                if r(getattr(t, name)):
                    return f'{p.timer} {name}非零'
            if p.timer == 'TIM1' and r(t.BDTR):
                return 'TIM1 BDTR非零'
            if function == 'meter' and r(t.SR) & 6:
                return '原捕获标志已置位，读取CCR会清除标志'
        if function == 'adc':
            if r(c.RCC_APB2ENR) & (1 << 10) or r(c.ADC2_CR1) or r(c.ADC2_CR2):
                return 'ADC2已配置或时钟已开启'
            if r(c.ADC1_CR1) & 0xF0000:
                return 'ADC1/ADC2双重模式'
            if self.clocks.adcclk_hz > 14_000_000:
                free = self.profile and self.profile.resources['adcpre_free']
                if not free:
                    return 'ADC时钟超过14MHz；本模式禁止修改ADCPRE'
                if r(c.RCC_APB2ENR) & (1 << 9) or r(c.ADC1_CR2) & 1:
                    return 'ADC1正在使用共享ADCPRE'
        return ''

    def _preflight_read(self, address):
        value = self.transport.read32(address)
        self._pre_reads[address] = value
        return value

    def preflight(self, cancel=None):
        self._pre_reads = {}
        issues, notes, resources, digest, device = [], [RISK], {}, None, {}
        try:
            if self.profile:
                assert_trusted_profile(self.profile)
            if not self.transport.is_running():
                raise c.SafetyError('目标未运行')
            dev = self.transport.read32(c.IDCODE) & 0xFFF
            size = self.transport.read_flash_size_kb()
            device = dict(dev_id=dev, flash_size_kb=size)
            if dev != 0x410 or size != 64:
                raise c.SafetyError('仅支持DEV_ID 0x410、64KiB STM32F103C8')
            chunks = []
            deadline = self._monotonic() + 30
            for offset in range(0, 65536, 1024):
                if cancel and cancel.cancelled:
                    raise c.CancelledError('镜像读取已取消')
                if self._monotonic() > deadline:
                    raise TimeoutError('完整镜像读取超时')
                chunk = self.transport.read_flash_block(c.FLASH_BASE + offset, 1024)
                if len(chunk) != 1024:
                    raise c.SafetyError('镜像读取不完整')
                chunks.append(chunk)
            image = b''.join(chunks)
            digest = hashlib.sha256(image).hexdigest()
            if self.profile and digest != self.profile.firmware.image_sha256:
                raise c.SafetyError('完整镜像摘要不匹配；可另选通用模式并确认风险')
            sp, reset = int.from_bytes(image[:4], 'little'), int.from_bytes(image[4:8], 'little')
            if not (0x20000000 < sp <= 0x20005000 and sp % 8 == 0 and reset & 1
                    and c.FLASH_BASE <= reset & ~1 < c.FLASH_BASE + 65536):
                raise c.SafetyError('向量表不合法')
            self.clocks = c.derive_clocks(self._preflight_read(c.RCC_CFGR), self.hse_hz)
            if (self.clocks.sysclk_hz > 72_000_000 or self.clocks.hclk_hz > 72_000_000
                    or self.clocks.pclk1_hz > 36_000_000 or self.clocks.pclk2_hz > 72_000_000):
                raise c.SafetyError('总线时钟超限')
            candidates, reasons = {}, {}
            for function, entries in CANDIDATES.items():
                candidates[function] = [dict(key=p.key, pin=p.pin, timer=p.timer,
                    channel=p.channel, reason=self._candidate_reason(function, p)) for p in entries]
                idle = [row['key'] for row in candidates[function] if not row['reason']]
                p = self.selection[function]
                if function not in self._explicit and (p is None or p.key not in idle):
                    p = next((p for p in entries if p.key in idle and not any(
                        q and (q.pin == p.pin or q.timer and q.timer == p.timer)
                        for k, q in self.selection.items() if k != function)), p)
                    self.selection[function] = p
                reason = self._candidate_reason(function, p) if p else '未选择可用候选'
                reasons[function] = reason
                resources[function] = not reason
            for f, p in self.selection.items():
                if p is None:
                    continue
                for g, q in self.selection.items():
                    if f < g and q and (p.pin == q.pin or p.timer and p.timer == q.timer):
                        raise c.SafetyError(f'{f}/{g}引脚或定时器冲突')
            resources.update(reasons=reasons, candidates=candidates,
                idle_candidates={f: [x['key'] for x in rows if not x['reason']] for f, rows in candidates.items()},
                selection={f: p.key if p else None for f, p in self.selection.items()}, adcpre_divider=None)
            if resources['adc'] and self.clocks.adcclk_hz > 14_000_000:
                resources['adcpre_divider'] = next(d for d in (2, 4, 6, 8) if self.clocks.pclk2_hz / d <= 14_000_000)
            if not any(resources[f] for f in CANDIDATES):
                issues.append('没有可用功能')
            if self.profile and self.profile.firmware.artifact_file:
                ok, note = verify_local_artifact(self.profile)
                notes.append(note)
                if not ok:
                    issues.append(note)
            notes.append('HSE为配置假设（通用模式8MHz），非现场测量；主机轮询非等间隔')
        except Exception as exc:
            issues.append(str(exc))
        if issues:
            resources.update({f: False for f in CANDIDATES})
        return PreflightReport(not issues, tuple(issues), device,
            self.clocks.as_dict() if self.clocks else {}, resources, digest,
            self.profile.profile_id if self.profile else 'general-mode', tuple(notes))

    def confirm_general_mode(self, confirmed=False):
        self.general_confirmed = confirmed is True

    def activate(self, cancel=None):
        if self._txn:
            return ActivationReport(False, ('先恢复再激活',), False, False, False, {})
        pre = self.preflight(cancel)
        if not pre.ok or self.general_mode and not self.general_confirmed:
            return ActivationReport(False, pre.issues or ('通用模式尚未确认风险',), False, False, False, pre.clocks)
        active = {f: p for f, p in self.selection.items() if pre.resources[f]}
        masks = union_masks(active)
        divider = pre.resources.get('adcpre_divider')
        if divider is not None:
            masks[c.RCC_CFGR] = 0xC000
        if self.general_mode:
            self.transport.authorize_general_mode(pre.firmware_sha256, confirmed=self.general_confirmed)
        elif hasattr(self.transport, 'authorize_profile'):
            self.transport.authorize_profile(self.profile, pre.firmware_sha256)
        if hasattr(self.transport, 'activate_selection'):
            self.transport.activate_selection(masks)
        # Snapshot all stable configuration fields, including unowned timer channels.
        watch = {c.RCC_CFGR, c.RCC_CR, c.RCC_APB1ENR, c.RCC_APB2ENR, c.AFIO_MAPR,
                 c.ADC1_CR1, c.ADC1_CR2, c.ADC2_CR1, c.ADC2_CR2,
                 c.ADC2_SMPR2, c.ADC2_SQR1, c.ADC2_SQR3}
        for p in active.values():
            watch.add(p.gpio)
            if p.timer:
                t = timer_registers(p.timer)
                watch.update(getattr(t, n) for n in ('CR1', 'SMCR', 'DIER', 'CCMR1', 'CCMR2', 'CCER'))
                if p.timer == 'TIM1':
                    watch.add(t.BDTR)
        baseline = {a: self._pre_reads[a] if a in self._pre_reads else self.transport.read32(a) for a in watch}
        self._protected = {a: (0xFFFFFFFF & ~masks.get(a, 0), baseline[a]) for a in watch}
        txn = self._txn = SelectionTransaction(self.transport, baseline)
        txn.cancel_token = self._operation_cancel = cancel
        try:
            self._assert_environment()
            if divider:
                txn.write(c.RCC_CFGR, 0xC000, (divider // 2 - 1) << 14)
                self.clocks = c.derive_clocks(self.transport.read32(c.RCC_CFGR), self.hse_hz)
            for address in (c.RCC_APB1ENR, c.RCC_APB2ENR):
                if masks.get(address):
                    txn.write(address, masks[address], masks[address])
            for function, p in active.items():
                if function == 'adc':
                    self._adc_pin = p.channel
                    txn.write(p.gpio, 15 << p.shift, 0)
                    txn.write(c.ADC2_CR1, 0x100, 0)
                    txn.write(c.ADC2_CR2, 0x5E090F, c.ADC2_CR2_SINGLE)
                    txn.write(c.ADC2_SMPR2, 7 << (p.channel * 3), 7 << (p.channel * 3))
                    txn.write(c.ADC2_SQR1, 0xF00000, 0)
                    txn.write(c.ADC2_SQR3, 31, p.channel)
                    txn.write(c.ADC2_CR2, 1, 1)
                    self._sleep(.001)
                    for bit in (8, 4):
                        txn.write(c.ADC2_CR2, bit, bit, self_clearing=bit)
                        self._poll_clear(c.ADC2_CR2, bit, .05, 'ADC校准')
                    self._adc2_owned = True
                    self.transport.claim_adc2_data_owner()
                elif function == 'pwm':
                    self._pwm = p
                    self._pt = timer_registers(p.timer)
                    self._pre_pwm_sr = self.transport.read32(self._pt.SR)
                    self._tim1_owned = True
                    txn.write(p.gpio, 15 << p.shift, 11 << p.shift)
                    self._configure_pwm(c.plan_pwm(1000, 0, self._timer_hz(p)))
                    self._output_state = 'off-confirmed'
                elif function == 'meter':
                    self._timer = timer_registers(p.timer)
                    self._ti1 = p.channel == 1
                    self._pre_meter_sr = self.transport.read32(self._timer.SR)
                    txn.write(p.gpio, 15 << p.shift, 4 << p.shift)
                    for a, m in ((self._timer.CR1, 5), (self._timer.SMCR, 0x77),
                                 (self._timer.CCER, 0x33), (self._timer.CCMR1, 0x303)):
                        txn.write(a, m, 0)
                    self._meter_owned = True
                else:
                    self._continuity_owned = True
                    self._continuity = p
                    self._continuity_before = self.transport.read32(p.base + 12)
                    # Reserve the idle nibble now; later firmware changes must stop us.
                    before = self.transport.read32(p.gpio)
                    txn.write(p.gpio, 15 << p.shift, before & (15 << p.shift))
            self._activated = True
            return ActivationReport(True, (), self._adc2_owned, self._tim1_owned, False,
                self.clocks.as_dict(), pre.notes, self._meter_owned, self._continuity_owned)
        except Exception as exc:
            txn.cancel_token = self._operation_cancel = None
            report = self.recover()
            return ActivationReport(False, (str(exc),) + report.errors, False, False, False, pre.clocks)
        finally:
            txn.cancel_token = self._operation_cancel = None

    def _timer_hz(self, p):
        return self.clocks.tim1_hz if p.timer == 'TIM1' else self.clocks.tim3_hz

    def _configure_pwm(self, plan, enable=False):
        p, t, txn = self._pwm, self._pt, self._txn
        shift = (p.channel - 1) * 4
        txn.write(t.CCER, 3 << shift, 0)
        if p.timer == 'TIM1':
            txn.write(t.BDTR, 0x8000, 0)
        txn.write(t.CR1, 0x81, 0)
        txn.write(t.PSC, 65535, plan.psc)
        txn.write(t.ARR, 65535, plan.arr)
        txn.write(getattr(t, f'CCR{p.channel}'), 65535, plan.ccr)
        cm = getattr(t, f'CCMR{1 if p.channel <= 2 else 2}')
        cs = (p.channel - 1) % 2 * 8
        txn.write(cm, 255 << cs, 0x68 << cs)
        txn.write(t.EGR, 1, 1, self_clearing=1)
        txn.write(t.CR1, 0x81, 0x80)
        if enable:
            txn.write(t.CCER, 3 << shift, 1 << shift)
            if p.timer == 'TIM1':
                txn.write(t.BDTR, 0x8000, 0x8000)
            txn.write(t.CR1, 0x81, 0x81)
        self._assert_environment()

    def set_pwm(self, frequency_hz, duty_percent):
        if not self._tim1_owned:
            return PwmResult(False, False, self._output_state, None, None, None, 'PWM未激活')
        try:
            self._assert_environment()
            plan = c.plan_pwm(frequency_hz, duty_percent, self._timer_hz(self._pwm))
            self._configure_pwm(plan, True)
            self._output_state = 'on-confirmed'
            return PwmResult(True, True, self._output_state, plan.actual_hz, plan.actual_duty, plan, '输出使能已读回确认')
        except Exception as exc:
            self._best_effort_off()
            return PwmResult(False, self._output_state != 'off-confirmed', self._output_state, None, None, None, str(exc))

    def _best_effort_off(self):
        if not self._tim1_owned:
            return self._output_state
        try:
            self._assert_environment()
            p, t = self._pwm, self._pt
            self._txn.write(t.CCER, 3 << ((p.channel - 1) * 4), 0)
            if p.timer == 'TIM1':
                self._txn.write(t.BDTR, 0x8000, 0)
            self._txn.write(t.CR1, 0x81, 0)
            self._assert_environment()
            self._output_state = 'off-confirmed'
        except Exception:
            self._output_state = 'unknown-may-still-output'
        return self._output_state

    def measure_pulse(self, timeout_s=1.5, cancel=None):
        # Both APB1 timer inputs share a clock; TIM1 input uses APB2 timer clock.
        if not self._meter_owned:
            from .contracts import PulseResult
            return PulseResult(False, 'not-ready', None, None, None, None, None,
                               '频率计未激活或未授权', session_active=self.is_active)
        return super().measure_pulse(timeout_s, cancel)

    def check_continuity(self):
        if not self._continuity_owned:
            return ContinuityResult(False, None, '通断未激活')
        p = self._continuity
        try:
            self._assert_environment()
            bit = 1 << p.number
            odr = p.base + 12
            from .hardware import _Owned
            if odr not in self._txn._owned:
                before = self.transport.read32(odr)
                if before & bit != self._continuity_before & bit:
                    raise c.SafetyError('通断ODR已被原程序改变')
                self._txn._owned[odr] = _Owned(before, bit, bit)
            self.transport.write_masked(p.base + 16, bit, bit)
            if not self.transport.read32(odr) & bit:
                raise c.SafetyError('上拉读回失败')
            self._txn.write(p.gpio, 15 << p.shift, 8 << p.shift)
            levels = []
            deadline = self._monotonic() + .5
            for _ in range(5):
                self._assert_environment()
                if self._monotonic() >= deadline:
                    raise TimeoutError('通断去抖超时')
                levels.append(bool(self.transport.read32(p.base + 8) & bit))
                self._sleep(.001)
            return ContinuityResult(True, not levels[0] if len(set(levels)) == 1 else None, '通断采样完成')
        except Exception as exc:
            return ContinuityResult(False, None, str(exc))

    def recover(self):
        errors, restored = [], ()
        if self._txn:
            try:
                self._assert_environment()
                if self._tim1_owned and self._best_effort_off() != 'off-confirmed':
                    raise c.SafetyError('输出关闭未确认')
                if self._tim1_owned:
                    mask = 0x65F & ~self._pre_pwm_sr
                    if mask:
                        self.transport.write_masked(self._pt.SR, mask, 0)
                if self._meter_owned:
                    self._txn.write(self._timer.CCER, 0x33, 0)
                    self._txn.write(self._timer.CR1, 5, 4)
                    self._clear_meter_status()
                # GPIO ODR restoration uses atomic commands for either port.
                for p in (self._continuity,) if self._continuity_owned else ():
                    a = p.base + 12
                    owned = self._txn._owned.get(a)
                    if owned:
                        bit = 1 << p.number
                        command = p.base + (16 if owned.original & bit else 20)
                        self.transport.write_masked(command, bit, bit)
                        if self.transport.read32(a) & bit != owned.original & bit:
                            raise c.SafetyError('原ODR恢复读回不符')
                        self._txn._owned.pop(a)
                restored, failures = self._txn.restore()
                errors.extend(failures)
            except Exception as exc:
                errors.append(str(exc))
                self._output_state = 'unknown-may-still-output'
        self._txn = None
        self._activated = self._adc2_owned = self._tim1_owned = False
        self._meter_owned = self._continuity_owned = False
        self.general_confirmed = False
        self.transport.release_adc2_data_owner()
        if hasattr(self.transport, 'revoke_resource_access'):
            self.transport.revoke_resource_access()
        return RecoveryReport(not errors, restored, tuple(errors), self._output_state)

    def sample(self, request, cancel=None):
        # Reuse the original timed single-conversion implementation, with a Vref facade.
        original = self.profile
        if original is None:
            self.profile = SimpleNamespace(vref_mv=3300)
        try:
            return super().sample(request, cancel)
        finally:
            self.profile = original
