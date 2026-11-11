"""SWD 仪器纯逻辑：寄存器白名单、时钟推导、帧/CSV、模拟器。

真实外设写入不再用无条件抛错桩；写权限由 transport 的字段白名单和
hardware.RealDeviceController 的事务共同约束。本模块自身仍不接触硬件。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import math
from pathlib import Path
import random
from typing import Iterable


class SafetyError(RuntimeError):
    pass


class CancelledError(RuntimeError):
    """用户请求取消；不代表已经安全关闭任何硬件输出。"""


# ---------------------------------------------------------------------------
# 地址与字段白名单

IDCODE = 0xE0042000
FLASH_SIZE_REG = 0x1FFFF7E0
FLASH_BASE = 0x08000000
RCC_CR = 0x40021000
RCC_CFGR = 0x40021004
RCC_APB2ENR = 0x40021018
AFIO_MAPR = 0x40010004
GPIOA_CRL = 0x40010800
GPIOA_CRH = 0x40010804
ADC1_SR = 0x40012400
ADC1_CR1 = 0x40012404
ADC1_CR2 = 0x40012408
ADC1_DR = 0x4001244C
ADC2_SR = 0x40012800
ADC2_CR1 = 0x40012804
ADC2_CR2 = 0x40012808
ADC2_SMPR2 = 0x40012810
ADC2_SQR1 = 0x4001282C
ADC2_SQR3 = 0x40012834
ADC2_DR = 0x4001284C
TIM1_CR1 = 0x40012C00
TIM1_DIER = 0x40012C0C
TIM1_EGR = 0x40012C14
TIM1_CCMR1 = 0x40012C18
TIM1_CCMR2 = 0x40012C1C
TIM1_CCER = 0x40012C20
TIM1_PSC = 0x40012C28
TIM1_ARR = 0x40012C2C
TIM1_CCR1 = 0x40012C34
TIM1_CCR2 = 0x40012C38
TIM1_CCR3 = 0x40012C3C
TIM1_CCR4 = 0x40012C40
TIM1_BDTR = 0x40012C44

RCC_APB1ENR = 0x4002101C
GPIOB_CRH = 0x40010C04
GPIOB_CRL = 0x40010C00
GPIOB_IDR = 0x40010C08
GPIOB_ODR = 0x40010C0C
GPIOB_BSRR = 0x40010C10
GPIOB_BRR = 0x40010C14
TIM4_CR1 = 0x40000800
TIM4_SMCR = 0x40000808
TIM4_DIER = 0x4000080C
TIM4_SR = 0x40000810
TIM4_EGR = 0x40000814
TIM4_CCMR1 = 0x40000818
TIM4_CCMR2 = 0x4000081C
TIM4_CCER = 0x40000820
TIM4_PSC = 0x40000828
TIM4_ARR = 0x4000082C
TIM4_CCR1 = 0x40000834
TIM4_CCR2 = 0x40000838
TIM3_CR1 = 0x40000400
TIM3_SMCR = 0x40000408
TIM3_DIER = 0x4000040C
TIM3_SR = 0x40000410
TIM3_EGR = 0x40000414
TIM3_CCMR1 = 0x40000418
TIM3_CCMR2 = 0x4000041C
TIM3_CCER = 0x40000420
TIM3_PSC = 0x40000428
TIM3_ARR = 0x4000042C
TIM3_CCR1 = 0x40000434
TIM3_CCR2 = 0x40000438
COMMAND_REGISTERS = frozenset((TIM1_EGR, TIM3_EGR, TIM4_EGR, GPIOB_BSRR, GPIOB_BRR))
TIM3_STATUS_MASK = 0x65F
TIM4_STATUS_MASK = TIM3_STATUS_MASK  # UIF/CC1IF–CC4IF/TIF/CC1OF/CC2OF, rc_w0

DIAGNOSTIC_REGISTERS = {
    'DBGMCU_IDCODE': IDCODE,
    'FLASH_SIZE': FLASH_SIZE_REG,
    'RCC_CR': RCC_CR,
    'RCC_CFGR': RCC_CFGR,
    'RCC_APB2ENR': RCC_APB2ENR,
    'AFIO_MAPR': AFIO_MAPR,
    'GPIOA_CRL': GPIOA_CRL,
    'GPIOA_CRH': GPIOA_CRH,
    'ADC1_SR': ADC1_SR,
    'ADC1_CR1': ADC1_CR1,
    'ADC1_CR2': ADC1_CR2,
    'ADC2_SR': ADC2_SR,
    'ADC2_CR1': ADC2_CR1,
    'ADC2_CR2': ADC2_CR2,
    'ADC2_SMPR2': ADC2_SMPR2,
    'ADC2_SQR1': ADC2_SQR1,
    'ADC2_SQR3': ADC2_SQR3,
    'TIM1_CR1': TIM1_CR1,
    'TIM1_DIER': TIM1_DIER,
    'TIM1_CCMR1': TIM1_CCMR1,
    'TIM1_CCMR2': TIM1_CCMR2,
    'TIM1_CCER': TIM1_CCER,
    'TIM1_PSC': TIM1_PSC,
    'TIM1_ARR': TIM1_ARR,
    'TIM1_CCR1': TIM1_CCR1,
    'TIM1_BDTR': TIM1_BDTR,
}
# ADC1_DR/ADC2_DR 有读副作用（清 EOC），永远不进入通用读取白名单。
SAFE_READ_ADDRESSES = frozenset(DIAGNOSTIC_REGISTERS.values()) | frozenset((
    RCC_APB1ENR, GPIOB_CRH, GPIOB_CRL, GPIOB_IDR, GPIOB_ODR, TIM4_CR1, TIM4_SMCR,
    TIM4_DIER, TIM4_SR, TIM4_CCMR1, TIM4_CCMR2, TIM4_CCER, TIM4_PSC,
    TIM4_ARR, TIM4_CCR1, TIM4_CCR2,
    TIM3_CR1, TIM3_SMCR, TIM3_DIER, TIM3_SR, TIM3_CCMR1, TIM3_CCMR2, TIM3_CCER, TIM3_PSC, TIM3_ARR, TIM3_CCR1, TIM3_CCR2))

# 真实 transport 只接受这些地址/字段；GUI 不能提交任意地址。
REAL_WRITE_MASKS = {
    RCC_CFGR: 0x0000C000,  # 仅 ADCPRE，需要已审查 adcpre_free 授权
    RCC_APB2ENR: (1 << 2) | (1 << 3) | (1 << 10) | (1 << 11),  # GPIOA/B/ADC2/TIM1 时钟
    GPIOA_CRL: 0xFF0F0000,                          # 仅 PA4/PA6/PA7 nibble
    GPIOA_CRH: 0x0000000F,                          # 仅 PA8 nibble
    ADC2_CR1: 0x00000100,                           # 仅 SCAN
    ADC2_CR2: 0x005E090F,                           # ADON/CONT/CAL/RSTCAL/DMA/ALIGN/EXTSEL/EXTTRIG/SWSTART
    ADC2_SMPR2: 0x00E07000,                         # 仅 SMP4/SMP7
    ADC2_SQR1: 0x00F00000,                          # 仅 L[3:0]
    ADC2_SQR3: 0x0000001F,                          # 仅 SQ1[4:0]
    TIM1_CR1: 0x00000081,                           # CEN + ARPE
    TIM1_EGR: 0x00000001,                           # 仅 UG
    TIM1_CCMR1: 0x000000FF,                         # 仅 OC1/CC1 字段
    TIM1_CCER: 0x00000003,                          # 仅 CC1E/CC1P，禁止互补输出
    TIM1_PSC: 0x0000FFFF,
    TIM1_ARR: 0x0000FFFF,
    TIM1_CCR1: 0x0000FFFF,
    RCC_APB1ENR: 6,  # 仅 TIM3/TIM4 时钟
    GPIOB_CRH: 0x00000F00,  # PB10 only
    GPIOB_CRL: 0xFF000000,  # 仅 PB6/PB7
    GPIOB_BSRR: 0x440, GPIOB_BRR: 0x440,  # 原子 PB6/PB10 操作
    TIM3_CR1: 5, TIM3_SMCR: 0x77, TIM3_CCMR1: 0x303,
    TIM3_CCER: 0x33, TIM3_PSC: 0xFFFF, TIM3_ARR: 0xFFFF,
    TIM3_EGR: 1, TIM3_SR: TIM4_STATUS_MASK,
    TIM4_CR1: 5,  # CEN/URS，reset事件不产生UIF
    TIM4_SMCR: 0x77,  # SMS/TS
    TIM4_CCMR1: 0x303,  # CC1S/CC2S
    TIM4_CCER: 0x33,  # CC1/2 enable/polarity
    TIM4_PSC: 0xFFFF, TIM4_ARR: 0xFFFF,  # 16 位时基
    TIM4_EGR: 1,  # UG 加载 PSC
    TIM4_SR: TIM4_STATUS_MASK,  # 计数/捕获/触发可能置位的 rc_w0 标志
    TIM1_BDTR: 0x00008000,                          # 仅 MOE
}
# 硬件自清位：写 1 后读回为 0，不能用“读回等于写入值”校验。
SELF_CLEARING_MASKS = {
    ADC2_CR2: 0x0000000C | 0x00400000,  # CAL | RSTCAL | SWSTART
    TIM3_EGR: 1, TIM4_EGR: 1, GPIOB_BSRR: 0x440, GPIOB_BRR: 0x440,
    TIM1_EGR: 0x00000001,               # UG
}
SIM_WRITE_MASKS = REAL_WRITE_MASKS

# 与标准库常量核对（tests/data/stm32f10x.h，ST 标准外设库 V3.5.0）。
ADC_SR_EOC = 0x02
ADC_CR1_SCAN = 0x0100
ADC_CR2_ADON = 0x00000001
ADC_CR2_CONT = 0x00000002
ADC_CR2_CAL = 0x00000004
ADC_CR2_RSTCAL = 0x00000008
ADC_CR2_DMA = 0x00000100
ADC_CR2_ALIGN = 0x00000800
ADC_CR2_EXTSEL = 0x000E0000
ADC_CR2_EXTTRIG = 0x00100000
ADC_CR2_SWSTART = 0x00400000
ADC_CR2_SWSTART_SELECT = 0x000E0000  # EXTSEL=111: SWSTART
ADC_SMPR2_SMP4 = 0x00007000          # 239.5 cycles
ADC_SQR1_L = 0x00F00000
ADC_SQR3_SQ1 = 0x0000001F
ADC_CHANNEL_PA4 = 4
TIM_CR1_CEN = 0x0001
TIM_CR1_ARPE = 0x0080
TIM_EGR_UG = 0x0001
TIM_CCMR1_OC1PE = 0x0008
TIM_CCMR1_OC1M_PWM1 = 0x0060        # OC1M=110
TIM_CCER_CC1E = 0x0001
TIM_CCER_CC1NE = 0x0004
TIM_BDTR_MOE = 0x8000
RCC_APB2ENR_IOPAEN = 1 << 2
RCC_APB2ENR_ADC2EN = 1 << 10
RCC_APB2ENR_TIM1EN = 1 << 11

ADC2_CR2_SAFE_BASE = ADC_CR2_EXTSEL | ADC_CR2_EXTTRIG  # CONT=0，单次转换
ADC2_CR2_SINGLE = ADC2_CR2_SAFE_BASE
TIM1_CCMR1_PWM1 = TIM_CCMR1_OC1PE | TIM_CCMR1_OC1M_PWM1

# 驱动使用的ADC2限定名称，与F1通用位定义共享同一数值。
ADC2_CR2_ADON = ADC_CR2_ADON
ADC2_CR2_CAL = ADC_CR2_CAL
ADC2_CR2_RSTCAL = ADC_CR2_RSTCAL
ADC2_CR2_SWSTART = ADC_CR2_SWSTART
ADC2_SMPR2_SMP4 = ADC_SMPR2_SMP4
ADC2_SQR3_SQ1 = ADC_SQR3_SQ1


def safe_read(transport, address: int) -> int:
    if address not in SAFE_READ_ADDRESSES:
        raise SafetyError(f'拒绝读取非诊断白名单地址 0x{address:08X}')
    return transport.read32(address)


# ---------------------------------------------------------------------------
# 时钟推导只读 RCC，ADCPRE 所有权由控制器按 profile 管理。

@dataclass(frozen=True)
class ClockTree:
    source: str
    hse_hz: int
    sysclk_hz: int
    hclk_hz: int
    pclk1_hz: int
    pclk2_hz: int
    adcclk_hz: int
    tim1_hz: int
    hse_is_assumption: bool = True
    notes: tuple[str, ...] = ()

    @property
    def tim4_hz(self) -> int:
        return self.pclk1_hz * (2 if self.pclk1_hz != self.hclk_hz else 1)

    @property
    def tim3_hz(self) -> int:
        return self.tim4_hz

    def as_dict(self) -> dict:
        return {
            'source': self.source,
            'hse_hz': self.hse_hz,
            'sysclk_hz': self.sysclk_hz,
            'hclk_hz': self.hclk_hz,
            'pclk1_hz': self.pclk1_hz,
            'pclk2_hz': self.pclk2_hz,
            'adcclk_hz': self.adcclk_hz,
            'tim1_hz': self.tim1_hz,
            'tim4_hz': self.tim4_hz,
            'tim3_hz': self.tim3_hz,
            'hse_is_assumption': self.hse_is_assumption,
            'notes': list(self.notes),
        }


def _ahb_div(code: int) -> int:
    return 1 if code < 8 else (2, 4, 8, 16, 64, 128, 256, 512)[code - 8]


def _apb_div(code: int) -> int:
    return 1 if code < 4 else 2 ** (code - 3)


def derive_clocks(rcc_cfgr: int, hse_hz: int) -> ClockTree:
    """从 RCC_CFGR 推导时钟；HSE 频率来自已审查 profile（配置假设）。"""
    if type(rcc_cfgr) is not int or not 0 <= rcc_cfgr <= 0xFFFFFFFF:
        raise ValueError('RCC_CFGR 无效')
    if not 1_000_000 <= hse_hz <= 25_000_000:
        raise ValueError('profile HSE 频率必须在1～25MHz')
    sws = (rcc_cfgr >> 2) & 0x3
    hpre = (rcc_cfgr >> 4) & 0xF
    ppre1 = (rcc_cfgr >> 8) & 0x7
    ppre2 = (rcc_cfgr >> 11) & 0x7
    adcpre = (rcc_cfgr >> 14) & 0x3
    pllsrc = (rcc_cfgr >> 16) & 0x1
    pllxtpre = (rcc_cfgr >> 17) & 0x1
    raw_mul = (rcc_cfgr >> 18) & 0xF

    if sws == 0:
        source, sysclk = 'HSI', 8_000_000
    elif sws == 1:
        source, sysclk = 'HSE', hse_hz
    elif sws == 2:
        if pllsrc:
            pll_in = hse_hz / (2 if pllxtpre else 1)
            source = 'PLL(HSE)'
        else:
            pll_in = 8_000_000 / 2
            source = 'PLL(HSI/2)'
        if not 1_000_000 <= pll_in <= 25_000_000:
            raise ValueError('PLL输入频率超出1～25MHz，拒绝推导')
        # 本工具目标是F103中容量，不套用F105/F107互联型的x6.5编码。
        mul = min(raw_mul + 2, 16)
        sysclk = int(pll_in * mul)
    else:
        raise ValueError('RCC_CFGR 的 SWS 为保留值，拒绝推导')

    hclk = sysclk // _ahb_div(hpre)
    pclk1 = hclk // _apb_div(ppre1)
    pclk2 = hclk // _apb_div(ppre2)
    adc_div = (2, 4, 6, 8)[adcpre]
    adcclk = pclk2 // adc_div
    tim1_hz = pclk2 if _apb_div(ppre2) == 1 else pclk2 * 2
    notes = (
        'HSE频率来自已审查profile的配置假设，不是现场晶振频率测量',
        'ADC时钟由共享ADCPRE推导；仅adcpre_free授权允许事务调整该分频',
        'TIM1时钟按APB2分频及定时器倍频规则推导，不假定固定72MHz',
    )
    return ClockTree(source, hse_hz, sysclk, hclk, pclk1, pclk2, adcclk, tim1_hz,
                     True, notes)


# ---------------------------------------------------------------------------
# 数据帧

SIM_SOURCE = '模拟数据（非真实测量）'
SIM_CLOCK = '模拟时钟（非等间隔）'
REAL_SOURCE = '真实SWD测量（ADC2/PA4）'
REAL_CLOCK = '主机轮询时间戳（非等间隔，非等间隔示波器）'


@dataclass(frozen=True)
class Point:
    time_s: float
    adc_code: int
    time_uncertainty_s: float = 0.0

    def __post_init__(self):
        if type(self.time_s) not in (int, float) or not math.isfinite(self.time_s):
            raise ValueError('时间戳必须是有限值')
        if type(self.adc_code) is not int or not 0 <= self.adc_code <= 4095:
            raise ValueError('ADC码必须是0～4095整数')
        if (type(self.time_uncertainty_s) not in (int, float)
                or not math.isfinite(self.time_uncertainty_s)
                or self.time_uncertainty_s < 0):
            raise ValueError('时间不确定度必须是有限非负数')


@dataclass(frozen=True)
class Frame:
    points: tuple[Point, ...]
    vref_mv: int
    source: str
    clock: str
    sequence: int

    def __post_init__(self):
        object.__setattr__(self, 'points', tuple(self.points))
        if type(self.vref_mv) is not int or not 2500 <= self.vref_mv <= 3600:
            raise ValueError('参考电压必须在2500～3600mV范围')
        if not self.points or len(self.points) > 4096:
            raise ValueError('帧必须含1～4096点')
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError('帧序号必须是正整数')
        if any(a.time_s >= b.time_s for a, b in zip(self.points, self.points[1:])):
            raise ValueError('时间戳必须严格递增，不能用名义采样率替代')
        if (self.source, self.clock) == (SIM_SOURCE, SIM_CLOCK):
            return
        if (self.source, self.clock) == (REAL_SOURCE, REAL_CLOCK):
            return
        raise SafetyError('未注册的数据来源/时钟组合，禁止把任意数据标为真实测量')

    @property
    def origin(self) -> str:
        return 'real' if self.source == REAL_SOURCE else 'simulation'

    @property
    def is_real(self) -> bool:
        return self.origin == 'real'

    @property
    def volts(self) -> tuple[float, ...]:
        return tuple(p.adc_code / 4095 * self.vref_mv / 1000 for p in self.points)

    @property
    def relative_times(self) -> tuple[float, ...]:
        return tuple(p.time_s - self.points[0].time_s for p in self.points)

    @property
    def uncertainties(self) -> tuple[float, ...]:
        return tuple(p.time_uncertainty_s for p in self.points)

    @property
    def stats(self) -> tuple[float, float, float, float]:
        values = self.volts
        return sum(values) / len(values), min(values), max(values), max(values) - min(values)


def _metadata_value(text: str) -> str:
    # 元数据行不能因换行破坏CSV结构。
    return ' '.join(str(text).splitlines()).replace(',', '，')


def export_csv(frame: Frame, filename: str | Path) -> None:
    with Path(filename).open('w', encoding='utf-8-sig', newline='') as stream:
        stream.write(f'# source,{_metadata_value(frame.source)}\n')
        stream.write(f'# clock,{_metadata_value(frame.clock)}\n')
        stream.write('# timing,逐点主机单调时间；不是硬件触发时间，也不是等间隔连续流\n')
        stream.write(f'# sequence,{frame.sequence}\n# vref_mv,{frame.vref_mv}\n')
        stream.write('# voltage_formula,adc_code/4095*vref_mv/1000\n')
        stream.write('# vref_note,参考电压是配置值，不冒充校准精度\n')
        writer = csv.writer(stream)
        writer.writerow(['time_s', 'relative_time_s', 'time_uncertainty_s',
                         'adc_code', 'voltage_v'])
        for point, relative, voltage in zip(frame.points, frame.relative_times, frame.volts):
            writer.writerow([f'{point.time_s:.9f}', f'{relative:.9f}',
                             f'{point.time_uncertainty_s:.9f}',
                             point.adc_code, f'{voltage:.6f}'])


# ---------------------------------------------------------------------------
# PWM 规划

@dataclass(frozen=True)
class PWMPlan:
    timer_hz: int
    psc: int
    arr: int
    ccr: int
    requested_hz: float
    requested_duty: float

    @property
    def actual_hz(self) -> float:
        return self.timer_hz / (self.psc + 1) / (self.arr + 1)

    @property
    def actual_duty(self) -> float:
        # CCR > ARR 时PWM1模式输出恒高，表示100%。
        return 100.0 if self.ccr > self.arr else self.ccr / (self.arr + 1) * 100


def plan_pwm(frequency_hz: float, duty_percent: float, timer_hz: int = 72_000_000) -> PWMPlan:
    if not math.isfinite(frequency_hz) or not 0.1 <= frequency_hz <= 100_000:
        raise ValueError('频率必须在0.1～100000Hz范围')
    if not math.isfinite(duty_percent) or not 0 <= duty_percent <= 100:
        raise ValueError('占空比必须在0～100%范围')
    if not 1 <= timer_hz <= 72_000_000 or frequency_hz > timer_hz / 2:
        raise ValueError('定时器时钟不足或无效')
    divisor = max(1, math.ceil(timer_hz / frequency_hz / 65535))
    if divisor > 65536:
        raise ValueError('频率无法由16位PSC/ARR表示')
    count = max(2, min(65535, round(timer_hz / frequency_hz / divisor)))
    ccr = round(count * duty_percent / 100)
    return PWMPlan(timer_hz, divisor - 1, count - 1, ccr, frequency_hz, duty_percent)


# ---------------------------------------------------------------------------
# 白名单编辑与模拟事务（真实事务在 hardware.py）

@dataclass(frozen=True)
class Edit:
    address: int
    mask: int
    value: int

    def validate(self):
        if self.address not in REAL_WRITE_MASKS or self.mask <= 0:
            raise SafetyError('地址或掩码禁止写入')
        if self.mask & ~REAL_WRITE_MASKS[self.address] or self.value & ~self.mask:
            raise SafetyError('写入越过允许字段')


class SimRegisterBank:
    """仅用于模拟事务测试的寄存器替身，不是真实驱动。"""

    def __init__(self):
        self.registers = {
            IDCODE: 0x20036410,
            FLASH_SIZE_REG: 64,
            RCC_CR: 0x03030083,
            RCC_CFGR: 0x001D840A,  # HSE 8MHz, PLL x9 => 72MHz, APB1 /2, APB2 /1, ADC /6
            RCC_APB2ENR: (1 << 2) | (1 << 9),
            GPIOA_CRL: 0x444444B0,
            GPIOA_CRH: 0x44444444,
        }
        self.running = True
        self.write_log: list[tuple[int, int]] = []

    def read32(self, address: int) -> int:
        if address in (ADC1_DR, ADC2_DR):
            raise SafetyError('禁止通过通用接口读取ADC数据寄存器')
        return self.registers.get(address, 0)

    def apply(self, edit: Edit) -> tuple[int, int]:
        edit.validate()
        if not self.running:
            raise SafetyError('目标未运行')
        before = self.read32(edit.address)
        after = (before & ~edit.mask) | edit.value
        self.registers[edit.address] = after
        self.write_log.append((edit.address, after))
        return before & edit.mask, after & edit.mask


class SimTransaction:
    def __init__(self, bank: SimRegisterBank):
        if type(bank) is not SimRegisterBank:
            raise SafetyError('模拟事务只能接受独立模拟寄存器，不可用于实物')
        self.bank = bank
        self.owned: list[tuple[Edit, int, int]] = []

    def apply(self, edits: Iterable[Edit]):
        edits = tuple(edits)
        for edit in edits:
            edit.validate()
        try:
            for edit in edits:
                before, after = self.bank.apply(edit)
                self.owned.append((edit, before, after))
        except Exception:
            self.restore()
            raise

    def restore(self) -> None:
        errors = []
        while self.owned:
            edit, before, expected = self.owned.pop()
            now = self.bank.read32(edit.address)
            if now & edit.mask != expected:
                errors.append(f'0x{edit.address:08X}所有权已变化，未覆盖')
                continue
            self.bank.apply(Edit(edit.address, edit.mask, before))
        if errors:
            raise SafetyError('; '.join(errors))


# ---------------------------------------------------------------------------
# 只读诊断

def diagnose(transport, should_stop=None) -> dict:
    values = {}
    for name, address in DIAGNOSTIC_REGISTERS.items():
        if should_stop is not None and should_stop():
            raise CancelledError('诊断已按请求取消；已完成只读读取，未写外设')
        values[name] = safe_read(transport, address)
    running = transport.is_running() if hasattr(transport, 'is_running') else transport.running
    issues = []
    if values['DBGMCU_IDCODE'] & 0xFFF != 0x410:
        issues.append('设备标识不匹配F1中容量候选范围')
    if not running:
        issues.append('目标未处于运行状态')
    if values['RCC_APB2ENR'] & RCC_APB2ENR_ADC2EN or values['ADC2_CR2'] & 1:
        issues.append('ADC2已启用，不可接管')
    if values['RCC_APB2ENR'] & RCC_APB2ENR_TIM1EN or values['TIM1_CR1'] & 1:
        issues.append('TIM1已启用，不可接管')
    if (values['GPIOA_CRL'] >> 16) & 0xF not in (0, 4):
        issues.append('PA4配置不是空闲候选状态')
    if values['GPIOA_CRH'] & 0xF not in (0, 4):
        issues.append('PA8配置不是空闲候选状态')
    if (values['ADC1_CR1'] >> 16) & 0xF:
        issues.append('ADC1/ADC2处于双重模式或DUALMOD非独立，拒绝接管ADC2')
    if values['TIM1_CCER'] & 0x3FF0 or values['TIM1_CCMR2']:
        issues.append('TIM1其他通道/互补输出已配置，拒绝接管CH1')
    if values['TIM1_DIER'] & 0x7FFF:
        issues.append('TIM1中断/DMA使能非零，拒绝接管')
    if values['TIM1_BDTR'] & 0x7FFF:
        issues.append('TIM1死区/AOE等非MOE字段非零，拒绝接管')
    issues.append('原固件身份、实际引脚用途与无干扰运行尚未验证；真实写入仅在预检通过后按事务开放')
    return {'running': bool(running), 'registers': values, 'issues': issues,
            'writes_allowed': False}


# ---------------------------------------------------------------------------
# 模拟器

class Simulator:
    def __init__(self, seed: int = 7):
        self.random = random.Random(seed)
        self.time = 0.0
        self.sequence = 0
        self.output: PWMPlan | None = None
        self.scene = '正弦'

    def capture(self, count: int = 512, vref_mv: int = 3300) -> Frame:
        if type(vref_mv) is not int or not 2500 <= vref_mv <= 3600:
            raise ValueError('参考电压必须在2500～3600mV范围')
        if type(count) is not int or not 1 <= count <= 4096:
            raise ValueError('采样点数超出范围')
        if self.scene not in ('正弦', '直流', 'PWM回环'):
            raise ValueError('未知模拟场景')
        points = []
        for _ in range(count):
            self.time += 0.001 * self.random.uniform(0.85, 1.15)
            if self.scene == '直流':
                voltage = 1.65
            elif self.scene == 'PWM回环':
                voltage = (vref_mv / 1000 if self.output and
                           (self.time * self.output.actual_hz) % 1 < self.output.actual_duty / 100 else 0)
            else:
                voltage = 1.65 + 1.35 * math.sin(2 * math.pi * 5 * self.time)
            code = max(0, min(4095, round(voltage * 1000 / vref_mv * 4095)))
            points.append(Point(self.time, code))
        self.sequence += 1
        return Frame(tuple(points), vref_mv, SIM_SOURCE, SIM_CLOCK, self.sequence)
