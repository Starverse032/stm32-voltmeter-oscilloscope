"""真实 SWD 仪器控制器：可选 ADC2、TIM1 PWM、按 profile 选择脉冲及通断输入。

安全边界：
- 只写 core.REAL_WRITE_MASKS 中的字段；raw 写和任意地址写不存在。
- Flash 只读、限定在 profile 声明的镜像区间；不擦除、不编程、不复位、不暂停。
- 所有写入属于事务，只恢复本工具仍持有的字段；其他运行期变化保留并报告。
- 断线后输出可能继续，只有读回确认才显示“已关闭”。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
import time

from .contracts import (
    ActivationReport, CancelToken, PwmResult, PreflightReport, RecoveryReport,
    SampleRequest,
)
from .core import (
    ADC1_CR1, ADC1_CR2, ADC2_CR1, ADC2_CR2, ADC2_SR, ADC2_SMPR2, ADC2_SQR1, ADC2_SQR3,
    ADC2_CR2_ADON, ADC2_CR2_CAL, ADC2_CR2_RSTCAL, ADC2_CR2_SWSTART,
    ADC2_CR2_SINGLE, ADC2_SMPR2_SMP4,
    ADC2_SQR3_SQ1, ADC_CHANNEL_PA4, ADC_SR_EOC,
    AFIO_MAPR, FLASH_BASE, GPIOA_CRH, GPIOA_CRL, IDCODE,
    RCC_APB2ENR, RCC_APB2ENR_ADC2EN, RCC_APB2ENR_IOPAEN,
    RCC_APB2ENR_TIM1EN, RCC_CFGR, RCC_CR,
    REAL_SOURCE, REAL_CLOCK, TIM1_ARR, TIM1_BDTR, TIM1_CCER, TIM1_CCMR1,
    TIM1_CCMR2, TIM1_CCR1, TIM1_CR1, TIM1_DIER, TIM1_EGR, TIM1_PSC,
    TIM_BDTR_MOE, TIM_CCER_CC1E, TIM_CR1_ARPE,
    TIM_CR1_CEN, TIM_EGR_UG, TIM1_CCMR1_PWM1,
    CancelledError, ClockTree, Edit, Frame, Point, SafetyError, SELF_CLEARING_MASKS,
    derive_clocks, plan_pwm,
)
from .core import (
    RCC_APB1ENR, GPIOB_CRL, GPIOB_IDR, GPIOB_ODR, GPIOB_BSRR, GPIOB_BRR,
    TIM4_CR1, TIM4_SMCR, TIM4_DIER, TIM4_SR, TIM4_EGR, TIM4_CCMR1,
    TIM4_CCMR2, TIM4_CCER, TIM4_PSC, TIM4_ARR, TIM4_CCR1, TIM4_CCR2,
    COMMAND_REGISTERS, TIM4_STATUS_MASK,
)
from .contracts import PulseResult, ContinuityResult
from .profiles import ReviewedProfile, load_profile, verify_local_artifact, assert_trusted_profile
from .transport import PyOCDSWD
from . import core as registers
from types import SimpleNamespace

POLL_S = 0.0002
CAL_TIMEOUT_S = 0.05
EOC_TIMEOUT_S = 0.01
ACTIVATION_STABILIZE_S = 0.001
SAMPLING_CANCEL_SLICE_S = 0.02
CONTINUITY_SAMPLES = 5
CONTINUITY_PAUSE_S = 0.001
CONTINUITY_TIMEOUT_S = 0.5


def _hex32(value: int) -> str:
    return f'0x{value:08X}'


@dataclass
class _Owned:
    original: int
    mask: int = 0
    written: int = 0
    self_clearing: int = 0


class RealRegisterTransaction:
    """真实字段事务：读改写、读回校验、按所有权恢复。"""

    def __init__(self, transport):
        self.transport = transport
        self._owned: dict[int, _Owned] = {}
        self.cancel_token = None

    @property
    def owned_addresses(self) -> tuple[int, ...]:
        return tuple(self._owned)

    def write(self, address: int, mask: int, value: int,
              self_clearing: int = 0,
              guard_mask: int | None = None, guard_value: int = 0):
        Edit(address, mask, value).validate()
        if self.cancel_token and self.cancel_token.cancelled:
            raise CancelledError('写入前已请求取消')
        if self_clearing & ~mask:
            raise SafetyError('自清位必须属于写入掩码')
        self_clearing |= SELF_CLEARING_MASKS.get(address, 0) & mask
        before_now = 0 if address in registers.COMMAND_REGISTERS else int(self.transport.read32(address))
        owned = self._owned.get(address)
        if owned is not None:
            stable = owned.mask & ~owned.self_clearing
            if before_now & stable != owned.written & stable:
                raise SafetyError(f'{_hex32(address)}所有权已变化，拒绝覆盖')
        if guard_mask is not None:
            if (before_now & guard_mask) != (guard_value & guard_mask):
                raise SafetyError(
                    f'{_hex32(address)}资源占用状态改变，拒绝写入'
                    f'（期望{_hex32(guard_value)}，实际{_hex32(before_now)}）')
        if owned is None:
            owned = _Owned(before_now)
            self._owned[address] = owned
        new_bits = mask & ~owned.mask
        owned.original = (owned.original & ~new_bits) | (before_now & new_bits)
        # 在底层写入前登记，包含“写成功但读回失败”的不确定情形。
        owned.mask |= mask
        owned.written = (owned.written & ~mask) | (value & mask)
        owned.self_clearing |= self_clearing
        before, after = self.transport.write_masked(address, mask, value)
        verify_mask = mask & ~self_clearing
        if (after & verify_mask) != (value & verify_mask):
            raise SafetyError(
                f'{_hex32(address)}写入后读回不符：期望{_hex32(value & verify_mask)}，'
                f'实际{_hex32(after & verify_mask)}')
        return before, after

    def pullup(self, bit):
        if GPIOB_ODR not in self._owned:
            before = int(self.transport.read32(GPIOB_ODR))
            self._owned[GPIOB_ODR] = _Owned(before, bit, bit)
        self.transport.write_masked(GPIOB_BSRR, bit, bit)
        if not int(self.transport.read32(GPIOB_ODR)) & bit:
            raise SafetyError('通断输入上拉读回失败')

    def assert_owned(self):
        for address, owned in self._owned.items():
            stable = owned.mask & ~owned.self_clearing
            if stable and int(self.transport.read32(address)) & stable != owned.written & stable:
                raise SafetyError(f'{_hex32(address)}所有权已变化，拒绝继续操作')

    def restore(self) -> tuple[tuple[int, ...], tuple[str, ...]]:
        restored = []
        errors = []
        while self._owned:
            address, owned = self._owned.popitem()
            check_mask = owned.mask & ~owned.self_clearing
            if address in registers.COMMAND_REGISTERS:
                continue  # 事件不可回放，不能因恢复再次产生UG。
            try:
                current = int(self.transport.read32(address))
                if (current & check_mask) == (owned.original & check_mask):
                    restored.append(address)
                    continue
                if (current & check_mask) != (owned.written & check_mask):
                    errors.append(
                        f'{_hex32(address)}所有权已变化（当前{_hex32(current)}，'
                        f'本工具写入{_hex32(owned.written)}），未覆盖')
                    continue
                target = owned.original & owned.mask & ~owned.self_clearing
                if address == GPIOB_ODR:
                    self.transport.write_masked(GPIOB_BSRR if target else GPIOB_BRR, owned.mask, owned.mask)
                    after = int(self.transport.read32(GPIOB_ODR))
                else:
                    _, after = self.transport.write_masked(address, owned.mask, target)
                if (after & check_mask) != (target & check_mask):
                    errors.append(f'{_hex32(address)}恢复后读回不符，状态未知')
                else:
                    restored.append(address)
            except Exception as exc:  # 恢复错误与最初错误分别报告
                errors.append(f'{_hex32(address)}恢复失败：{type(exc).__name__}: {exc}')
        return tuple(restored), tuple(errors)


class RealDeviceController:
    @property
    def is_active(self) -> bool:
        return self._activated and self._txn is not None

    def __init__(self, transport: PyOCDSWD, profile: ReviewedProfile,
                 monotonic=time.monotonic, sleep=time.sleep):
        self.transport = transport
        self.profile = profile
        self._monotonic = monotonic
        self._sleep = sleep
        self._txn: RealRegisterTransaction | None = None
        self._activated = False
        self._adc2_owned = False
        self._tim1_owned = False
        self._output_state = 'not-owned'
        self._meter_owned = False
        self._continuity_owned = False
        self._sequence = 0
        self.clocks: ClockTree | None = None
        self._protected = {}
        self._conversion_pending = False
        self._operation_cancel = None
        self._ti1 = profile.resources['meter_input'] == 'PA6'
        timer = 'TIM3' if self._ti1 else 'TIM4'
        self._timer = SimpleNamespace(**{
            name: getattr(registers, timer + '_' + name)
            for name in ('CR1', 'SMCR', 'DIER', 'SR', 'EGR', 'CCMR1',
                         'CCMR2', 'CCER', 'PSC', 'ARR', 'CCR1', 'CCR2')})
        self._meter_clock_bit = 2 if self._ti1 else 4
        self._meter_gpio = GPIOA_CRL if self._ti1 else GPIOB_CRL
        self._meter_pin_shift = 24 if self._ti1 else 28
        self._meter_pin_mask = 15 << self._meter_pin_shift
        self._meter_pin_value = 4 << self._meter_pin_shift
        self._continuity_pb10 = profile.resources['continuity_input'] == 'PB10'
        self._continuity_gpio = registers.GPIOB_CRH if self._continuity_pb10 else GPIOB_CRL
        self._continuity_shift = 8 if self._continuity_pb10 else 24
        self._continuity_bit = 1 << (10 if self._continuity_pb10 else 6)

    # -- 只读预检 ---------------------------------------------------------
    def preflight(self, cancel: CancelToken | None = None) -> PreflightReport:
        issues: list[str] = []
        notes: list[str] = []
        device: dict = {}
        resources: dict = dict.fromkeys(('adc', 'pwm', 'meter', 'continuity'), False)
        function_issues = {key: [] for key in resources}
        planned_divider = None
        clocks: dict = {}
        digest = None
        try:
            assert_trusted_profile(self.profile)
            if self.transport is None:
                raise SafetyError('没有SWD会话')
            if not self.transport.is_running():
                raise SafetyError('目标未运行；拒绝预检和任何外设写入')
            idcode = int(self.transport.read32(IDCODE))
            dev_id = idcode & 0xFFF
            flash_kb = int(self.transport.read_flash_size_kb())
            device = {'idcode': idcode, 'dev_id': dev_id, 'flash_size_kb': flash_kb}
            if dev_id != self.profile.device.dev_id:
                issues.append(
                    f'设备ID 0x{dev_id:03X}与profile的0x{self.profile.device.dev_id:03X}不符')
            if flash_kb != self.profile.device.flash_size_kb:
                issues.append(
                    f'Flash容量{flash_kb}KB与profile的{self.profile.device.flash_size_kb}KB不符')
            flash_limit = FLASH_BASE + min(flash_kb, self.profile.device.flash_size_kb) * 1024
            fw = self.profile.firmware
            if fw.flash_start != FLASH_BASE:
                issues.append('profile固件起始地址不是0x08000000')
            if fw.flash_end > flash_limit or fw.image_size > (1 << 20):
                issues.append('profile镜像区间超出已确认Flash范围')
            if not issues:
                chunks = []
                deadline = self._monotonic() + 30.0
                for offset in range(0, fw.image_size, 1024):
                    if cancel and cancel.cancelled:
                        raise CancelledError('固件只读比对已取消')
                    if self._monotonic() > deadline:
                        raise TimeoutError('固件只读比对超时')
                    chunks.append(self.transport.read_flash_block(fw.flash_start + offset,
                                        min(1024, fw.image_size - offset)))
                image = b''.join(chunks)
                if len(image) != fw.image_size:
                    raise SafetyError('Flash只读返回长度不足')
                digest = hashlib.sha256(image).hexdigest()
                if digest != fw.image_sha256:
                    issues.append(
                        f'原固件镜像摘要不匹配：profile {fw.image_sha256[:12]}…，'
                        f'板上 {digest[:12]}…')
                if len(image) >= 8:
                    sp = int.from_bytes(image[0:4], 'little')
                    reset = int.from_bytes(image[4:8], 'little')
                    if not 0x20000000 < sp <= 0x20005000 or sp % 8:
                        issues.append('向量表初始SP不在SRAM范围')
                    if not (reset & 1) or not fw.flash_start <= (reset & ~1) < fw.flash_end:
                        issues.append('向量表复位向量不在profile镜像区间')
                else:
                    issues.append('镜像过短，无法检查向量表')

            cfgr = int(self.transport.read32(RCC_CFGR))
            rcc_cr = int(self.transport.read32(RCC_CR))
            try:
                tree = derive_clocks(cfgr, self.profile.hse_hz)
            except ValueError as exc:
                issues.append(f'时钟推导失败：{exc}')
                tree = None
            if tree is not None:
                self.clocks = tree
                clocks = tree.as_dict()
                clocks['rcc_cr'] = rcc_cr
                notes.extend(tree.notes)
                if self.profile.functions['adc'] and tree.adcclk_hz > 14_000_000:
                    if self.profile.resources['adcpre_free']:
                        planned_divider = next(div for div in (2, 4, 6, 8)
                                               if tree.pclk2_hz <= 14_000_000 * div)
                        notes.append(f'ADC激活计划将共享ADCPRE设为/{planned_divider}，事务恢复原值')
                    else:
                        function_issues['adc'].append(
                            f'ADC时钟{tree.adcclk_hz / 1e6:.3f}MHz超过14MHz，未授权修改共享ADCPRE')
                if (tree.sysclk_hz > 72_000_000 or tree.hclk_hz > 72_000_000
                        or tree.pclk1_hz > 36_000_000 or tree.pclk2_hz > 72_000_000):
                    issues.append('F103C8系统或总线时钟超出已审查范围')

            apb2 = int(self.transport.read32(RCC_APB2ENR))
            adc2_cr1 = int(self.transport.read32(ADC2_CR1))
            adc2_cr2 = int(self.transport.read32(ADC2_CR2))
            adc2_sr = int(self.transport.read32(ADC2_SR))
            adc1_cr1 = int(self.transport.read32(ADC1_CR1))
            adc1_cr2 = int(self.transport.read32(ADC1_CR2))
            crl = int(self.transport.read32(GPIOA_CRL))
            crh = int(self.transport.read32(GPIOA_CRH))
            tim1_cr1 = int(self.transport.read32(TIM1_CR1))
            tim1_dier = int(self.transport.read32(TIM1_DIER))
            tim1_ccmr1 = int(self.transport.read32(TIM1_CCMR1))
            tim1_ccmr2 = int(self.transport.read32(TIM1_CCMR2))
            tim1_ccer = int(self.transport.read32(TIM1_CCER))
            tim1_bdtr = int(self.transport.read32(TIM1_BDTR))
            afio = int(self.transport.read32(AFIO_MAPR))
            pa4 = (crl >> 16) & 0xF
            pa8 = crh & 0xF
            resources = {
                'rcc_apb2enr': apb2, 'adc1_cr1': adc1_cr1, 'adc1_cr2': adc1_cr2,
                'adc2_cr1': adc2_cr1, 'adc2_cr2': adc2_cr2, 'adc2_sr': adc2_sr,
                'pa4_nibble': pa4, 'pa8_nibble': pa8,
                'tim1_cr1': tim1_cr1, 'tim1_dier': tim1_dier,
                'tim1_ccmr1': tim1_ccmr1, 'tim1_ccmr2': tim1_ccmr2,
                'tim1_ccer': tim1_ccer, 'tim1_bdtr': tim1_bdtr,
                'afio_mapr': afio,
                'gpioa_crl': crl, 'gpioa_crh': crh, 'rcc_cfgr': cfgr,
            }
            if self.profile.functions['adc']:
                if planned_divider is not None and (apb2 & (1 << 9) or adc1_cr2 & 1):
                    function_issues['adc'].append('ADC1时钟或ADON已启用，拒绝修改共享ADCPRE')
                if apb2 & RCC_APB2ENR_ADC2EN or adc2_cr2 & ADC2_CR2_ADON:
                    function_issues['adc'].append('ADC2时钟或ADON已启用，资源不空闲')
                if adc2_cr1 & 0x0100:
                    function_issues['adc'].append('ADC2 SCAN模式已配置，资源不空闲')
                if adc2_cr2:
                    function_issues['adc'].append('ADC2配置不是未使用状态')
                if (adc1_cr1 >> 16) & 0xF:
                    function_issues['adc'].append('ADC1/ADC2处于双重模式，拒绝接管ADC2')
            if self.profile.functions['pwm']:
                if apb2 & RCC_APB2ENR_TIM1EN:
                    function_issues['pwm'].append('TIM1时钟已启用，资源不空闲')
                if tim1_cr1:
                    function_issues['pwm'].append('TIM1 CR1非零（计数器/模式/预装载已配置），资源不空闲')
                if tim1_ccer:
                    function_issues['pwm'].append('TIM1 CCER非零（其他通道/互补/极性或CH1已配置），拒绝接管')
                if tim1_ccmr1 & 0xFF00:
                    function_issues['pwm'].append('TIM1 CH2配置非零，拒绝接管')
                if tim1_ccmr2:
                    function_issues['pwm'].append('TIM1 CH3/CH4配置非零，拒绝接管')
                if tim1_dier & 0x7FFF:
                    function_issues['pwm'].append('TIM1中断/DMA使能非零，拒绝接管')
                if tim1_bdtr & 0x7FFF:
                    function_issues['pwm'].append('TIM1死区/AOE等非MOE字段非零，拒绝接管')
                if pa8 not in (0, 4):
                    function_issues['pwm'].append(f'PA8配置nibble=0x{pa8:X}不是空闲候选状态')
                if (afio >> 6) & 0x3:
                    function_issues['pwm'].append('AFIO已将TIM1重映射，PA8不再是CH1默认引脚')
            pin = int(self.profile.adc_input[-1]) if self.profile.adc_input else None
            if self.profile.functions['adc'] and ((crl >> (4 * pin)) & 15) not in (0, 4):
                function_issues['adc'].append('ADC输入引脚不空闲')
            resources.update(self.profile.functions)
            apb1 = int(self.transport.read32(RCC_APB1ENR))
            bcrl = int(self.transport.read32(GPIOB_CRL))
            bcrh = int(self.transport.read32(registers.GPIOB_CRH))
            resources.update(rcc_apb1enr=apb1, gpiob_crl=bcrl, gpiob_crh=bcrh)
            resources[self._timer.SR] = int(self.transport.read32(self._timer.SR))
            meter_idle = not (apb1 & self._meter_clock_bit or afio & (0xC00 if self._ti1 else 0x1000))
            for address in (self._timer.CR1, self._timer.SMCR, self._timer.DIER, self._timer.CCMR1, self._timer.CCMR2, self._timer.CCER):
                value = int(self.transport.read32(address))
                resources[address] = value
                meter_idle &= value == 0
            meter_gpio = crl if self._ti1 else bcrl
            meter_idle &= ((meter_gpio >> self._meter_pin_shift) & 15) in (0, 4)
            if resources['meter'] and not meter_idle:
                function_issues['meter'].append(f"{self.profile.resources['meter_input']}不空闲，测量不可用")
            continuity_gpio = bcrh if self._continuity_pb10 else bcrl
            if resources['continuity'] and ((continuity_gpio >> self._continuity_shift) & 15) not in (0, 4):
                function_issues['continuity'].append(f"{self.profile.resources['continuity_input']}不空闲，通断检测不可用")
            notes.append('ADC1_DR未读取；ADC1_CR1仅用于确认独立模式')
            notes.append('预检通过只表示当前寄存器与已审查profile一致，不证明原程序将来不会使用这些资源')
        except Exception as exc:
            issues.append(f'预检失败：{type(exc).__name__}: {exc}')
        if self.profile.firmware.artifact_file:
            local_ok, local_note = verify_local_artifact(self.profile)
            notes.append(local_note)
            if not local_ok:
                issues.append(local_note)
        notes.append('HSE频率来自profile配置假设，不是现场频率测量')
        notes.append('主机轮询不是等间隔示波器；高频输入可能混叠')
        reasons = {}
        for function, granted in self.profile.functions.items():
            if issues:
                resources[function] = False
                reasons[function] = '身份或全局安全检查失败，禁止激活'
            elif not granted:
                resources[function] = False
                reasons[function] = '已审查profile未授权所需外设或引脚'
            elif function_issues[function]:
                resources[function] = False
                reasons[function] = '；'.join(function_issues[function])
            if function in reasons:
                notes.append(f'{function}不可用，{reasons[function]}')
        resources['reasons'] = reasons
        resources['adcpre_divider'] = planned_divider if resources['adc'] else None
        if not issues and not any(resources[key] for key in self.profile.functions):
            issues.append('没有可用功能')
        notes.extend(self.profile.notes)
        return PreflightReport(not issues, tuple(issues), device, clocks,
                               resources, digest, self.profile.profile_id,
                               tuple(notes))

    # -- 激活 -------------------------------------------------------------
    def activate(self, cancel: CancelToken | None = None) -> ActivationReport:
        if self._txn is not None:
            return ActivationReport(False, ('控制器已经激活，先恢复再重新激活',),
                self._adc2_owned, self._tim1_owned,
                self._output_state == 'on-confirmed', self.clocks.as_dict() if self.clocks else {})
        pre = self.preflight(cancel)
        if not pre.ok:
            return ActivationReport(False, pre.issues, False, False, False,
                                    pre.clocks, pre.notes)
        if self._txn is not None:
            return ActivationReport(False, ('控制器已经激活，先恢复再重新激活',),
                                    self._adc2_owned, self._tim1_owned,
                                    self._output_state == 'on-confirmed', pre.clocks)
        self.clocks = derive_clocks(int(self.transport.read32(RCC_CFGR)),
                                    self.profile.hse_hz)
        if hasattr(self.transport, 'authorize_profile'):
            self.transport.authorize_profile(self.profile, pre.firmware_sha256)
        r = pre.resources
        if hasattr(self.transport, 'activate_selection'):
            from .pins import Candidate, union_masks
            selected = {}
            if r['adc']:
                selected['adc'] = Candidate(self.profile.adc_input, channel=int(self.profile.adc_input[2:]))
            if r['pwm']:
                selected['pwm'] = Candidate('PA8', 'TIM1', 1)
            if r['meter']:
                selected['meter'] = Candidate(self.profile.resources['meter_input'], 'TIM3' if self._ti1 else 'TIM4', 1 if self._ti1 else 2)
            if r['continuity']:
                selected['continuity'] = Candidate(self.profile.resources['continuity_input'])
            masks = union_masks(selected)
            if r['adcpre_divider'] is not None:
                masks[RCC_CFGR] = 0xC000
            self.transport.activate_selection(masks)
        self._protected = {
            RCC_CFGR: (0xFFFFFFFF, r['rcc_cfgr']),
            ADC1_CR1: (0xFFFFFFFF, r['adc1_cr1']),
            GPIOA_CRL: (0xFFF0FFFF, r['gpioa_crl']),
            GPIOA_CRH: (0xFFFFFFF0, r['gpioa_crh']),
            RCC_APB2ENR: (0xFFFFFFFF & ~0xC04, r['rcc_apb2enr']),
            AFIO_MAPR: (0xC0, r['afio_mapr']),
            TIM1_DIER: (0x7FFF, r['tim1_dier']),
            TIM1_CCMR1: (0xFF00, r['tim1_ccmr1']),
            TIM1_CCMR2: (0xFFFFFFFF, r['tim1_ccmr2']),
            TIM1_CCER: (0xFFFF & ~3, r['tim1_ccer']),
            TIM1_BDTR: (0x7FFF, r['tim1_bdtr']),
        }
        if r['adcpre_divider'] is not None:
            self._protected[ADC1_CR2] = (0xFFFFFFFF, r['adc1_cr2'])
        pin = int(self.profile.adc_input[-1]) if r['adc'] else None
        self._adc_pin = pin
        self._pre_continuity = r['gpiob_crh'] if self._continuity_pb10 else r['gpiob_crl']
        self._pre_meter_sr = r[self._timer.SR]
        adc_mask = (15 << (pin * 4)) if r['adc'] else 0
        clock_mask = ((1 << 2) if r['adc'] or r['pwm'] or (r['meter'] and self._ti1) else 0)
        clock_mask |= RCC_APB2ENR_ADC2EN if r['adc'] else 0
        clock_mask |= RCC_APB2ENR_TIM1EN if r['pwm'] else 0
        clock_mask |= 8 if (r['meter'] and not self._ti1) or r['continuity'] else 0
        self._protected[GPIOA_CRL] = (0xFFFFFFFF & ~(adc_mask | (self._meter_pin_mask if r['meter'] and self._ti1 else 0)), r['gpioa_crl'])
        self._protected[GPIOA_CRH] = (0xFFFFFFF0 if r['pwm'] else 0xFFFFFFFF, r['gpioa_crh'])
        self._protected[RCC_APB2ENR] = (0xFFFFFFFF & ~clock_mask, r['rcc_apb2enr'])
        self._protected[RCC_APB1ENR] = (0xFFFFFFFF & ~(self._meter_clock_bit if r['meter'] else 0), r['rcc_apb1enr'])
        bmask = (0xF0000000 if r['meter'] and not self._ti1 else 0) | (0x0F000000 if r['continuity'] and not self._continuity_pb10 else 0)
        self._protected[GPIOB_CRL] = (0xFFFFFFFF & ~bmask, r['gpiob_crl'])
        self._protected[registers.GPIOB_CRH] = (0xFFFFF0FF if r['continuity'] and self._continuity_pb10 else 0xFFFFFFFF, r['gpiob_crh'])
        self._protected[AFIO_MAPR] = (0x1CC0, r['afio_mapr'])
        if r['meter']:
            for address, mask in ((self._timer.DIER, 0x7FFF), (self._timer.CCMR2, 0xFFFF),
                                  (self._timer.CCMR1, 0xFCFC), (self._timer.CCER, 0xFFCC),
                                  (self._timer.CR1, 0xFFFA), (self._timer.SMCR, 0xFF88)):
                self._protected[address] = (mask, r[address])
        if not r['pwm']:
            for address in (TIM1_DIER, TIM1_CCMR1, TIM1_CCMR2, TIM1_CCER, TIM1_BDTR):
                self._protected.pop(address, None)
        txn = RealRegisterTransaction(self.transport)
        txn.cancel_token = cancel
        self._operation_cancel = cancel
        self._txn = txn
        try:
            if cancel and cancel.cancelled:
                raise CancelledError('激活前已取消')
            self._assert_environment()
            if r['adcpre_divider'] is not None:
                divider = r['adcpre_divider']
                txn.write(RCC_CFGR, 0xC000, (divider // 2 - 1) << 14,
                          guard_mask=0xFFFFFFFF, guard_value=r['rcc_cfgr'])
                self._protected[RCC_CFGR] = (0xFFFF3FFF, r['rcc_cfgr'])
                self.clocks = derive_clocks(int(self.transport.read32(RCC_CFGR)), self.profile.hse_hz)
            if clock_mask:
                txn.write(RCC_APB2ENR, clock_mask, clock_mask,
                          guard_mask=clock_mask & (RCC_APB2ENR_ADC2EN | RCC_APB2ENR_TIM1EN))
            if r['adc']:
                txn.write(GPIOA_CRL, adc_mask, 0, guard_mask=adc_mask, guard_value=r['gpioa_crl'])
            if r['pwm']:
                txn.write(GPIOA_CRH, 15, 11)
            if r['adc']:
                txn.write(ADC2_CR1, 0x0100, 0)                      # SCAN=0
                txn.write(ADC2_CR2, 0x005E090F, ADC2_CR2_SINGLE,
                          guard_mask=ADC2_CR2_ADON, guard_value=0)   # 单次/软件触发
                txn.write(ADC2_SMPR2, 7 << (pin * 3), 7 << (pin * 3))  # 239.5 cycles
                txn.write(ADC2_SQR1, 0x00F00000, 0)                 # L=0, 1次转换
                txn.write(ADC2_SQR3, ADC2_SQR3_SQ1, pin)
                txn.write(ADC2_CR2, ADC2_CR2_ADON, ADC2_CR2_ADON,
                          guard_mask=ADC2_CR2_ADON, guard_value=0)
                self._sleep(ACTIVATION_STABILIZE_S)
                txn.write(ADC2_CR2, ADC2_CR2_RSTCAL, ADC2_CR2_RSTCAL,
                          self_clearing=ADC2_CR2_RSTCAL)
                self._poll_clear(ADC2_CR2, ADC2_CR2_RSTCAL, CAL_TIMEOUT_S,
                                 'ADC2复位校准')
                txn.write(ADC2_CR2, ADC2_CR2_CAL, ADC2_CR2_CAL,
                          self_clearing=ADC2_CR2_CAL)
                self._poll_clear(ADC2_CR2, ADC2_CR2_CAL, CAL_TIMEOUT_S, 'ADC2校准')
                self._adc2_owned = True
                if hasattr(self.transport, 'claim_adc2_data_owner'):
                    self.transport.claim_adc2_data_owner()
    
            if r['pwm']:
                # TIM1: 配置CH1 PWM，但CC1E/MOE/CEN保持0，输出关闭。
                txn.write(TIM1_PSC, 0xFFFF, 0)
                txn.write(TIM1_ARR, 0xFFFF, 999)
                txn.write(TIM1_CCR1, 0xFFFF, 0)
                txn.write(TIM1_CCMR1, 0xFF, TIM1_CCMR1_PWM1)
                txn.write(TIM1_CCER, 0x0003, 0)
                txn.write(TIM1_BDTR, TIM_BDTR_MOE, 0)
                txn.write(TIM1_CR1, TIM_CR1_CEN | TIM_CR1_ARPE, TIM_CR1_ARPE)
                txn.write(TIM1_EGR, TIM_EGR_UG, TIM_EGR_UG,
                          self_clearing=TIM_EGR_UG)
                self._tim1_owned = True
            if r['meter']:
                txn.write(RCC_APB1ENR, self._meter_clock_bit, self._meter_clock_bit, guard_mask=self._meter_clock_bit)
                txn.write(self._meter_gpio, self._meter_pin_mask, self._meter_pin_value,
                          guard_mask=self._meter_pin_mask, guard_value=r['gpioa_crl'] if self._ti1 else r['gpiob_crl'])
                # 立即登记空闲配置，防止激活后至测量前被运行固件接管。
                for address, mask in ((self._timer.CR1, 5), (self._timer.SMCR, 0x77),
                                      (self._timer.CCER, 0x33), (self._timer.CCMR1, 0x303)):
                    txn.write(address, mask, 0, guard_mask=0xFFFF)
                self._meter_owned = True
            self._continuity_owned = r['continuity']
            self._activated = True
            self._output_state = 'off-confirmed' if r['pwm'] else 'not-owned'
            return ActivationReport(
                True, (), self._adc2_owned, self._tim1_owned, False,
                self.clocks.as_dict(), pre.notes,
                self._meter_owned, self._continuity_owned)
        except Exception as exc:
            failed = f'激活失败：{type(exc).__name__}: {exc}'
            txn.cancel_token = self._operation_cancel = None
            self._best_effort_off()
            restored, restore_errors = txn.restore()
            if hasattr(self.transport, 'release_adc2_data_owner'):
                self.transport.release_adc2_data_owner()
            self._txn = None
            self._activated = False
            self._adc2_owned = False
            self._tim1_owned = False
            self._meter_owned = self._continuity_owned = False
            if hasattr(self.transport, 'revoke_resource_access'):
                self.transport.revoke_resource_access()
            issues = (failed,) + tuple(restore_errors)
            if self._output_state != 'off-confirmed':
                issues += ('恢复后输出状态未知，PA8可能仍有波形',)
            return ActivationReport(False, issues, False, False,
                                    self._output_state == 'on-confirmed', {})
        finally:
            self._operation_cancel = None
            txn.cancel_token = None

    # -- 采样 -------------------------------------------------------------
    def sample(self, request: SampleRequest, cancel: CancelToken | None = None) -> Frame:
        cancel = cancel or CancelToken()
        count = request.count
        interval = request.interval_s
        if type(count) is not int or not 1 <= count <= 4096:
            raise ValueError('采样点数必须是1～4096整数')
        if not math.isfinite(interval) or not 0 <= interval <= 1.0:
            raise ValueError('采样间隔必须在0～1秒')
        if not self._activated or not self._adc2_owned or self._txn is None:
            raise SafetyError('ADC2未激活，拒绝采样')
        self._assert_adc2_config()
        points: list[Point] = []
        previous = -math.inf
        for index in range(count):
            if cancel.cancelled:
                raise CancelledError('采样已取消；未完成的帧不发布')
            if not self.transport.is_running():
                raise SafetyError('目标已停止；ADC2/TIM1状态未知，不发布部分帧')
            self._assert_adc2_config()
            if self._conversion_pending:
                self._wait_eoc(EOC_TIMEOUT_S, cancel)
                self.transport.read_adc2_dr()
                self._conversion_pending = False
            if int(self.transport.read32(ADC2_SR)) & ADC_SR_EOC:
                self.transport.read_adc2_dr()  # 丢弃取消/超时后可能留下的旧EOC。
            before = self._monotonic()
            self._conversion_pending = True
            self.transport.write_masked(ADC2_CR2, ADC2_CR2_SWSTART, ADC2_CR2_SWSTART)
            self._wait_eoc(EOC_TIMEOUT_S, cancel)
            code = int(self.transport.read_adc2_dr()) & 0xFFFF
            after = self._monotonic()
            self._conversion_pending = False
            self._assert_adc2_config()
            midpoint = (before + after) / 2
            if after < before or midpoint <= previous:
                raise SafetyError('主机时间戳未递增，拒绝伪造采样时刻')
            stamp = midpoint
            uncertainty = max((after - before) / 2, POLL_S / 2)
            points.append(Point(stamp, code, uncertainty))
            previous = stamp
            if index + 1 < count:
                self._sleep_until(before + interval, cancel)
        self._sequence += 1
        return Frame(tuple(points), self.profile.vref_mv, REAL_SOURCE, REAL_CLOCK,
                     self._sequence)

    # -- TIM1 输出 --------------------------------------------------------
    def measure_pulse(self, timeout_s: float = 1.5,
                      cancel: CancelToken | None = None) -> PulseResult:
        def result(state, message, tick=None, period=None, high=None):
            return PulseResult(state == 'measured', state, period, high,
                1 / period if period else None,
                100 * high / period if period and high is not None else None,
                tick, message, session_active=self.is_active)
        if not self._activated or not self._meter_owned or self._txn is None:
            return result('not-ready', f"{self.profile.resources['meter_input']}未激活或未授权")
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 60:
            return result('error', '超时必须在0～60秒')
        # Reading CCR1/2 clears their capture flags even without an SR write.
        if self._pre_meter_sr & 6:
            return result('error', '会话前CC1IF/CC2IF已置位，读取捕获值会清除原标志')
        if self._pre_meter_sr & 1:
            return result('error', '会话前UIF已置位，无法在保留原标志时判断自动量程溢出')
        txn = self._txn
        deadline = self._monotonic() + timeout_s
        tick = None
        answer = result('no-signal', '期限内无完整周期；恒定电平或信号太慢')
        try:
            self._assert_environment()
            timer_hz = (self._timer_hz(self.selection['meter']) if hasattr(self, 'selection')
                        else self.clocks.tim3_hz if self._ti1 else self.clocks.tim4_hz)
            # Doubling the divider after each overflow gives 32768..65536
            # period ticks at the first fitting range (unless PSC=0 already fits).
            # Keep one shared deadline and advance immediately on UIF.
            divisors = (1 << exponent for exponent in range(17))
            for divisor in divisors:
                tick = divisor / timer_hz
                if cancel and cancel.cancelled:
                    raise CancelledError('脉冲测量已取消')
                txn.write(self._timer.CR1, 5, 4)  # URS=1: reset事件不置UIF，只有溢出置位
                txn.write(self._timer.CCER, 0x33, 0)
                txn.write(self._timer.PSC, 0xFFFF, divisor - 1)
                txn.write(self._timer.ARR, 0xFFFF, 0xFFFF)
                txn.write(self._timer.CCMR1, 0x303, 0x201 if self._ti1 else 0x102)
                txn.write(self._timer.SMCR, 0x77, 0x54 if self._ti1 else 0x64)
                txn.write(self._timer.EGR, 1, 1)
                self._clear_meter_status()
                txn.write(self._timer.CCER, 0x33, 0x31 if self._ti1 else 0x13)
                txn.write(self._timer.CR1, 5, 5)
                previous = None
                discard = True
                overflow = False
                while self._monotonic() < deadline:
                    if cancel and cancel.cancelled:
                        raise CancelledError('脉冲测量已取消')
                    self._assert_environment()
                    status = int(self.transport.read32(self._timer.SR)) & ~self._pre_meter_sr
                    if status & 1:
                        overflow = True
                        break
                    if status & (2 if self._ti1 else 4):
                        # 禁用捕获，CNT继续计数；SWD读取不会混入另一周期的CCR。
                        txn.write(self._timer.CCER, 0x33, 0x20 if self._ti1 else 0x02)
                        before = int(self.transport.read32(self._timer.SR)) & ~self._pre_meter_sr
                        period_ticks = int(self.transport.read32(self._timer.CCR1 if self._ti1 else self._timer.CCR2)) + 1
                        high_ticks = int(self.transport.read32(self._timer.CCR2 if self._ti1 else self._timer.CCR1)) + 1
                        after = int(self.transport.read32(self._timer.SR)) & ~self._pre_meter_sr
                        self._clear_meter_status()
                        txn.write(self._timer.CCER, 0x33, 0x31 if self._ti1 else 0x13)
                        if discard:
                            discard = False
                            continue
                        # 2026-11-11 主机实测，servo、950kHz SWD、PA8→PA6：
                        # 1us tick下1000Hz捕获999/249，20kHz捕获49/24。
                        # PWM输入reset模式在边沿复位前捕获N-1，周期和高电平均为(CCR+1)*tick。
                        # CCxIF由读CCR清除。旧OF表示漏采周期；冻结后OF/UIF不能再变。
                        # 重开捕获后的首周期可能漏过reset，丢弃后再比较两个新鲜快照。
                        discard = True
                        if (before ^ after) & 0x601 or after & 1:
                            previous = None
                            continue
                        if not before & (4 if self._ti1 else 2) or not 0 < high_ticks < period_ticks:
                            previous = None
                            continue
                        pair = (period_ticks, high_ticks)
                        if previous and all(abs(x-y) <= 1 for x,y in zip(pair, previous)):
                            period, high = period_ticks * tick, high_ticks * tick
                            # Include a full tick of capture quantisation plus 1%
                            # at either documented boundary, including 719/721 ticks
                            # around the nominal 720-tick 100kHz period at 72MHz.
                            minimum_period = (1 / 100000) * .99 - tick
                            maximum_period = 2 * 1.01 + tick
                            if not minimum_period <= period <= maximum_period:
                                answer = result('out-of-range', '频率超出0.5Hz～100kHz', tick)
                            else:
                                answer = result('measured', '硬件捕获；时基由RCC配置推导，非独立精度校验', tick, period, high)
                            break
                        previous = pair
                    self._sleep(min(POLL_S, max(0, deadline - self._monotonic())))
                if answer.ok or not overflow:
                    break
                answer = result('out-of-range', '计数溢出，期限内未取得可测周期', tick)
            # 无捕获且达到最大量程时不能区分恒定电平和极慢信号。
            if answer.state == 'out-of-range' and not previous:
                answer = result('no-signal', '期限内无有效边沿；恒定电平或低于可识别频率', tick)
        except CancelledError:
            answer = result('cancelled', '测量已取消')
        except Exception as exc:
            answer = result('error', f'脉冲测量失败：{type(exc).__name__}: {exc}', tick)
            try:
                if hasattr(self, 'selection'):
                    recovery = self.recover()
                    if recovery.errors:
                        answer = result('error', answer.message + '；' + '；'.join(recovery.errors), tick)
                else:
                    self._assert_environment(check_owned=False)
                    # 失败写可能尚未生效，原值允许回滚；第三种值表示外部接管，全部跳过。
                    for address, owned in txn._owned.items():
                        stable = owned.mask & ~owned.self_clearing
                        if stable:
                            current = int(self.transport.read32(address)) & stable
                            if current not in (owned.original & stable, owned.written & stable):
                                raise SafetyError('所有权已变化，跳过恢复写入')
                    txn.write(self._timer.CCER, 0x33, 0)
                    txn.write(self._timer.CR1, 5, 4)
                    self._clear_meter_status()
                    _, errors = txn.restore()
                    self._txn = None
                    self._activated = self._adc2_owned = self._tim1_owned = False
                    self._meter_owned = self._continuity_owned = False
                    self._output_state = 'unknown-may-still-output' if errors else 'off-confirmed'
                    if hasattr(self.transport, 'release_adc2_data_owner'):
                        self.transport.release_adc2_data_owner()
                    if hasattr(self.transport, 'revoke_resource_access'):
                        self.transport.revoke_resource_access()
                    if errors:
                        answer = result('error', answer.message + '；' + '；'.join(errors), tick)
            except Exception as recovery_error:
                answer = result('error', answer.message + f'；恢复状态未知：{recovery_error}', tick)
        finally:
            if self._txn is txn:
                saved_cancel = txn.cancel_token
                txn.cancel_token = None  # Cancellation cannot veto stopping the meter.
                try:
                    for attempt in range(2):
                        try:
                            self._assert_environment()
                            txn.write(self._timer.CCER, 0x33, 0)
                            txn.write(self._timer.CR1, 5, 4)
                            self._clear_meter_status()
                            break
                        except CancelledError:
                            answer = result('cancelled', '测量已取消')
                            if attempt:
                                raise
                    if cancel and cancel.cancelled:
                        answer = result('cancelled', '测量已取消')
                except Exception as exc:
                    answer = result('error', f'计数器停止未确认：{exc}', tick)
                finally:
                    txn.cancel_token = None if saved_cancel and saved_cancel.cancelled else saved_cancel
        return replace(answer, session_active=self.is_active)

    def check_continuity(self) -> ContinuityResult:
        if not self._activated or not self._continuity_owned or self._txn is None:
            return ContinuityResult(False, None, f"{self.profile.resources['continuity_input']}未激活或未授权")
        try:
            self._assert_environment()
            if GPIOB_ODR not in self._txn._owned:
                nibble = int(self.transport.read32(self._continuity_gpio)) & (15 << self._continuity_shift)
                if nibble != (self._pre_continuity & (15 << self._continuity_shift)):
                    raise SafetyError(f"{self.profile.resources['continuity_input']}所有权已变化")
            self._txn.pullup(self._continuity_bit)
            self._txn.write(self._continuity_gpio, 15 << self._continuity_shift, 8 << self._continuity_shift)
            deadline = self._monotonic() + CONTINUITY_TIMEOUT_S
            levels = []
            for index in range(CONTINUITY_SAMPLES):
                if self._txn.cancel_token and self._txn.cancel_token.cancelled:
                    raise CancelledError(f"{self.profile.resources['continuity_input']}通断检查已取消")
                if not self.transport.is_running():
                    raise SafetyError(f"{self.profile.resources['continuity_input']}通断检查期间目标停止")
                if self._monotonic() >= deadline:
                    raise TimeoutError(f"{self.profile.resources['continuity_input']}去抖超时")
                levels.append(bool(int(self.transport.read32(GPIOB_IDR)) & self._continuity_bit))
                if self._monotonic() >= deadline:
                    raise TimeoutError(f"{self.profile.resources['continuity_input']}去抖超时")
                if index + 1 < CONTINUITY_SAMPLES:
                    self._sleep(CONTINUITY_PAUSE_S)
            self._assert_environment()
            if self._txn.cancel_token and self._txn.cancel_token.cancelled:
                raise CancelledError(f"{self.profile.resources['continuity_input']}通断检查已取消")
            if all(levels):
                return ContinuityResult(True, False, f"开路（{self.profile.resources['continuity_input']}高电平）")
            if not any(levels):
                return ContinuityResult(True, True, f"导通至GND（{self.profile.resources['continuity_input']}低电平）")
            return ContinuityResult(True, None, '电平不稳定，无法确认通断')
        except Exception as exc:
            return ContinuityResult(False, None, f'通断检查失败：{exc}')

    def set_pwm(self, frequency_hz: float, duty_percent: float) -> PwmResult:
        if not self._activated or not self._tim1_owned or self._txn is None:
            return PwmResult(False, False, self._output_state, None, None, None,
                             'TIM1未激活，未写入')
        if self.clocks is None:
            return PwmResult(False, False, self._output_state, None, None, None,
                             '缺少已推导的TIM1时钟，未写入')
        try:
            plan = plan_pwm(frequency_hz, duty_percent, self.clocks.tim1_hz)
        except ValueError as exc:
            return PwmResult(False, self._output_state == 'on-confirmed',
                             self._output_state, None, None, None, str(exc))
        try:
            self._assert_tim1_free_for_update()
            txn = self._txn
            txn.write(TIM1_CCER, 0x0003, 0)
            txn.write(TIM1_BDTR, TIM_BDTR_MOE, 0)
            txn.write(TIM1_CR1, TIM_CR1_CEN | TIM_CR1_ARPE, 0)
            txn.write(TIM1_PSC, 0xFFFF, plan.psc)
            txn.write(TIM1_ARR, 0xFFFF, plan.arr)
            txn.write(TIM1_CCR1, 0xFFFF, plan.ccr)
            txn.write(TIM1_CCMR1, 0x00FF, TIM1_CCMR1_PWM1)
            txn.write(TIM1_EGR, TIM_EGR_UG, TIM_EGR_UG,
                      self_clearing=TIM_EGR_UG)
            txn.write(TIM1_CCER, 0x0003, TIM_CCER_CC1E)
            txn.write(TIM1_BDTR, TIM_BDTR_MOE, TIM_BDTR_MOE)
            txn.write(TIM1_CR1, TIM_CR1_CEN | TIM_CR1_ARPE,
                      TIM_CR1_CEN | TIM_CR1_ARPE)
            self._verify_tim1_running()
            self._output_state = 'on-confirmed'
            return PwmResult(
                True, True, self._output_state, plan.actual_hz, plan.actual_duty,
                plan, f'输出使能位已读回确认；配置量化频率{plan.actual_hz:.6f}Hz，'
                      f'占空比{plan.actual_duty:.4f}%')
        except Exception as exc:
            state = self._best_effort_off()
            message = f'PWM写入失败：{type(exc).__name__}: {exc}'
            if state == 'off-confirmed':
                message += '；已读回确认输出关闭'
            else:
                message += '；输出状态未知，PA8可能仍在输出，勿假定已关闭'
            return PwmResult(False, state == 'on-confirmed', state, None, None,
                             None, message)

    def output_off(self) -> PwmResult:
        if not self._activated or not self._tim1_owned:
            if self._output_state == 'unknown-may-still-output':
                return PwmResult(False, True, self._output_state, None, None, None,
                                 '输出状态未知，缺少所有权时不能盲目接管或声称关闭')
            return PwmResult(True, False, self._output_state, None, None, None,
                             'TIM1未由本工具持有，没有可关闭的输出')
        state = self._best_effort_off()
        if state == 'off-confirmed':
            return PwmResult(True, False, state, None, None, None,
                             '输出已关闭并读回确认')
        return PwmResult(False, True, state, None, None, None,
                         '关闭失败或读回不符；输出状态未知，可能仍在输出')

    # -- 恢复与关闭 -------------------------------------------------------
    def recover(self) -> RecoveryReport:
        errors: list[str] = []
        restored: tuple[int, ...] = ()
        state = self._output_state
        if self._txn is not None:
            try:
                self._assert_environment()
            except Exception as exc:
                # 其他通道/共享配置变化后，连CEN/MOE/时钟也不能盲目恢复。
                self._output_state = 'unknown-may-still-output'
                self._txn = None
                self._activated = self._adc2_owned = self._tim1_owned = False
                self._meter_owned = self._continuity_owned = False
                if hasattr(self.transport, 'release_adc2_data_owner'):
                    self.transport.release_adc2_data_owner()
                if hasattr(self.transport, 'revoke_resource_access'):
                    self.transport.revoke_resource_access()
                return RecoveryReport(False, (),
                    (f'恢复前检查失败（所有权已变化或状态未知）：{exc}；跳过恢复写入',),
                    self._output_state)
        if self._tim1_owned:
            state = self._best_effort_off()
            if state != 'off-confirmed':
                errors.append('输出关闭未获读回确认；PA8可能仍在输出')
        if hasattr(self.transport, 'release_adc2_data_owner'):
            self.transport.release_adc2_data_owner()
        if self._txn is not None:
            if self._meter_owned:
                try:
                    self._txn.write(self._timer.CCER, 0x33, 0)
                    self._txn.write(self._timer.CR1, 5, 4)
                    self._clear_meter_status()
                except Exception as exc:
                    errors.append(f'脉冲计状态恢复失败：{exc}')
            restored, restore_errors = self._txn.restore()
            errors.extend(restore_errors)
        self._txn = None
        self._activated = False
        self._adc2_owned = False
        self._tim1_owned = False
        self._meter_owned = self._continuity_owned = False
        if hasattr(self.transport, 'revoke_resource_access'):
            self.transport.revoke_resource_access()
        if state not in ('off-confirmed', 'not-owned'):
            self._output_state = state
        return RecoveryReport(not errors, tuple(restored), tuple(errors), state)

    def close(self) -> RecoveryReport | None:
        report = None
        if self._txn is not None or self._activated:
            report = self.recover()
        if report is None and self._output_state == 'unknown-may-still-output':
            report = RecoveryReport(False, (), ('输出仍然未知，未确认关闭',), self._output_state)
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception as exc:
                if report is None:
                    report = RecoveryReport(False, (), (f'关闭SWD会话失败：{exc}',),
                                            self._output_state)
                else:
                    report = RecoveryReport(False, report.restored,
                                            report.errors + (f'关闭SWD会话失败：{exc}',),
                                            report.output_state)
            finally:
                self.transport = None
        return report

    # -- 内部工具 ---------------------------------------------------------
    def _clear_meter_status(self):
        # CCMR2 stays in output compare mode, so CH3/4 cannot set CC3OF/CC4OF.
        # rc_w0 writes ones outside this mask, preserving pre-session flags.
        mask = TIM4_STATUS_MASK & ~self._pre_meter_sr
        if mask:
            self.transport.write_masked(self._timer.SR, mask, 0)

    def _assert_environment(self, check_owned=True):
        if not self.transport.is_running():
            raise SafetyError('目标未运行')
        for address, (mask, expected) in self._protected.items():
            if int(self.transport.read32(address)) & mask != expected & mask:
                raise SafetyError(f'{_hex32(address)}受保护配置发生变化，拒绝继续')
        if self._txn and check_owned:
            self._txn.assert_owned()

    def _assert_adc2_config(self):
        self._assert_environment()
        cr1 = int(self.transport.read32(ADC2_CR1))
        cr2 = int(self.transport.read32(ADC2_CR2))
        if cr1 & 0x0100:
            raise SafetyError('ADC2 SCAN被外部改变，拒绝采样')
        if not cr2 & ADC2_CR2_ADON:
            raise SafetyError('ADC2 ADON被外部关闭，拒绝采样')
        if cr2 & (0x00000002 | 0x00000100 | 0x00000800):
            raise SafetyError('ADC2 CONT/DMA/ALIGN被外部改变，拒绝采样')
        extsel = cr2 & 0x000E0000
        if extsel != (ADC2_CR2_SINGLE & 0x000E0000) or not (cr2 & 0x00100000):
            raise SafetyError('ADC2触发配置被外部改变，拒绝采样')
        if (int(self.transport.read32(ADC2_SMPR2)) & (7 << (self._adc_pin * 3))) != (7 << (self._adc_pin * 3)):
            raise SafetyError('ADC2 SMP4被外部改变，拒绝采样')
        if int(self.transport.read32(ADC2_SQR1)) & 0x00F00000:
            raise SafetyError('ADC2序列长度被外部改变，拒绝采样')
        if (int(self.transport.read32(ADC2_SQR3)) & ADC2_SQR3_SQ1) != self._adc_pin:
            raise SafetyError('ADC2 SQ1被外部改变，拒绝采样')

    def _poll_clear(self, address: int, mask: int, timeout_s: float, label: str):
        deadline = self._monotonic() + timeout_s
        while True:
            if self._operation_cancel and self._operation_cancel.cancelled:
                raise CancelledError(f'{label}已取消')
            if not self.transport.is_running():
                raise SafetyError(f'{label}期间目标停止；硬件状态未知')
            if not int(self.transport.read32(address)) & mask:
                return
            if self._monotonic() >= deadline:
                raise TimeoutError(f'{label}超时（{timeout_s * 1000:.1f}ms）')
            self._sleep(min(POLL_S, max(0, deadline - self._monotonic())))

    def _wait_eoc(self, timeout_s: float, cancel: CancelToken):
        deadline = self._monotonic() + timeout_s
        while True:
            if cancel.cancelled:
                raise CancelledError('等待EOC时收到取消；ADC2状态未知')
            if not self.transport.is_running():
                raise SafetyError('等待EOC时目标停止；硬件状态未知')
            if int(self.transport.read32(ADC2_SR)) & ADC_SR_EOC:
                return
            if self._monotonic() >= deadline:
                raise TimeoutError(f'ADC2 EOC超时（{timeout_s * 1000:.1f}ms）')
            self._sleep(min(POLL_S, max(0, deadline - self._monotonic())))

    def _sleep_until(self, deadline: float, cancel: CancelToken):
        while True:
            if cancel.cancelled:
                raise CancelledError('采样间隔期间收到取消；未完成的帧不发布')
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return
            self._sleep(min(remaining, SAMPLING_CANCEL_SLICE_S))

    def _assert_tim1_free_for_update(self):
        self._assert_environment()
        if int(self.transport.read32(TIM1_CCER)) & 0x3FF0:
            raise SafetyError('TIM1其他通道/互补输出已被占用，拒绝更新CH1')
        if int(self.transport.read32(TIM1_CCMR2)):
            raise SafetyError('TIM1 CH3/CH4配置非零，拒绝更新CH1')
        if int(self.transport.read32(TIM1_DIER)) & 0x7FFF:
            raise SafetyError('TIM1中断/DMA使能非零，拒绝更新CH1')
        if int(self.transport.read32(TIM1_BDTR)) & 0x7FFF:
            raise SafetyError('TIM1死区/AOE非零，拒绝更新CH1')

    def _verify_tim1_running(self):
        checks = (
            (TIM1_BDTR, TIM_BDTR_MOE),
            (TIM1_CR1, TIM_CR1_CEN),
        )
        for address, mask in checks:
            if not int(self.transport.read32(address)) & mask:
                raise SafetyError(f'{_hex32(address)}读回未确认输出开启')
        ccer = int(self.transport.read32(TIM1_CCER))
        if (ccer & 0x000F) != TIM_CCER_CC1E:
            raise SafetyError('TIM1 CC1E/极性/互补位读回不符')
        if ccer & 0x3FF0:
            raise SafetyError('TIM1其他通道被意外使能')

    def _best_effort_off(self) -> str:
        """尝试关闭CH1；只有读回确认才返回off-confirmed。"""
        if not self._tim1_owned and self._output_state == 'not-owned':
            return 'not-owned'
        try:
            self._assert_environment()
        except Exception:
            self._output_state = 'unknown-may-still-output'
            return self._output_state
        errors = []
        operations = (
            (TIM1_CCER, 0x0003, 0),
            (TIM1_BDTR, TIM_BDTR_MOE, 0),
            (TIM1_CR1, TIM_CR1_CEN | TIM_CR1_ARPE, 0),
        )
        for address, mask, value in operations:
            try:
                if self._txn is not None:
                    self._txn.write(address, mask, value)
                else:
                    self.transport.write_masked(address, mask, value)
            except Exception as exc:
                errors.append(f'{_hex32(address)}关闭写失败：{type(exc).__name__}: {exc}')
        try:
            confirmed = (
                not int(self.transport.read32(TIM1_CCER)) & 0x000F
                and not int(self.transport.read32(TIM1_BDTR)) & TIM_BDTR_MOE
                and not int(self.transport.read32(TIM1_CR1)) & TIM_CR1_CEN
            )
        except Exception as exc:
            errors.append(f'关闭后读回失败：{type(exc).__name__}: {exc}')
            confirmed = False
        self._output_state = 'off-confirmed' if confirmed else 'unknown-may-still-output'
        return self._output_state


def open_real_controller(serial_number: str, profile_path,
                         swd_hz: int | None = None, selection=None, general_mode=False) -> RealDeviceController:
    """打开会话并返回控制器；调用方负责在后台线程运行并在结束时close。

    swd_hz 省略时使用 transport 的默认SWD时钟；指定时必须是已登记档位。
    """
    if profile_path is None and not general_mode:
        raise SafetyError('未选择profile或通用模式')
    profile = load_profile(profile_path) if profile_path is not None else None
    transport = PyOCDSWD(serial_number) if swd_hz is None else PyOCDSWD(serial_number, swd_hz)
    try:
        transport.open()
    except Exception:
        transport.close()
        raise
    from .selectable import SelectableController
    return SelectableController(transport, profile, selection, general_mode)
