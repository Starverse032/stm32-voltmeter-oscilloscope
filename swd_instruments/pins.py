"""STM32F103C8 default mapping (DS5319 pin table; RM0008). No remap writes."""
from dataclasses import dataclass
from . import core as c


@dataclass(frozen=True)
class Candidate:
    pin: str
    timer: str | None = None
    channel: int | None = None

    @property
    def number(self):
        return int(self.pin[2:])

    @property
    def base(self):
        return 0x40010800 if self.pin[1] == 'A' else 0x40010C00

    @property
    def gpio(self):
        return self.base + (4 if self.number >= 8 else 0)

    @property
    def shift(self):
        return self.number % 8 * 4

    @property
    def ft(self):
        return (self.pin[1] == 'A' and self.number >= 8 or
                self.pin[1] == 'B' and self.number not in (0, 1, 5))

    @property
    def key(self):
        return f'{self.timer}_CH{self.channel}/{self.pin}' if self.timer else self.pin

    @property
    def label(self):
        parts = ([f'{self.timer}_CH{self.channel}'] if self.timer else [])
        parts += ['FT，关闭上下拉时耐5V' if self.ft else '仅3.3V']
        if self.pin == 'PB2':
            parts += ['BOOT1']
        if self.pin in ('PA11', 'PA12'):
            parts += ['USB D-' if self.pin == 'PA11' else 'USB D+']
        return f'{self.pin}（{ "，".join(parts)}）'


ADC = tuple(Candidate(f'PA{i}', channel=i) for i in range(8)) + (
    Candidate('PB0', channel=8), Candidate('PB1', channel=9))
PWM = tuple(Candidate(pin, timer, i + 1) for timer, pins in (
    ('TIM1', ('PA8', 'PA9', 'PA10', 'PA11')),
    ('TIM2', ('PA0', 'PA1', 'PA2', 'PA3')),
    ('TIM3', ('PA6', 'PA7', 'PB0', 'PB1')),
    ('TIM4', ('PB6', 'PB7', 'PB8', 'PB9'))) for i, pin in enumerate(pins))
METER = tuple(p for p in PWM if p.channel <= 2)
CONTINUITY = tuple(Candidate(f'PA{i}') for i in range(13)) + tuple(
    Candidate(f'PB{i}') for i in (*range(3), *range(5, 16)))
CANDIDATES = dict(adc=ADC, pwm=PWM, meter=METER, continuity=CONTINUITY)
DEFAULT_OCCUPIED = ('PB6', 'PB7', 'PB8', 'PB9')
TIMER_BASES = dict(TIM1=0x40012C00, TIM2=0x40000000, TIM3=0x40000400, TIM4=0x40000800)
OFFSETS = dict(CR1=0, SMCR=8, DIER=12, SR=16, EGR=20, CCMR1=24,
               CCMR2=28, CCER=32, PSC=40, ARR=44, CCR1=52, CCR2=56,
               CCR3=60, CCR4=64, BDTR=68)


def timer_registers(timer):
    from types import SimpleNamespace
    return SimpleNamespace(**{k: TIMER_BASES[timer] + v for k, v in OFFSETS.items()})


def clock(timer):
    return (c.RCC_APB2ENR, 1 << 11) if timer == 'TIM1' else (
        c.RCC_APB1ENR, 1 << (int(timer[-1]) - 2))


def masks_for(function, p):
    masks = {p.gpio: 15 << p.shift, c.RCC_APB2ENR: 1 << (2 if p.pin[1] == 'A' else 3)}
    if function == 'continuity':
        masks.update({p.base + 16: 1 << p.number, p.base + 20: 1 << p.number})
    elif function == 'adc':
        masks[c.RCC_APB2ENR] |= 1 << 10
        masks.update({c.ADC2_CR1: 0x100, c.ADC2_CR2: 0x5E090F,
                      c.ADC2_SMPR2: 7 << (p.channel * 3), c.ADC2_SQR1: 0xF00000,
                      c.ADC2_SQR3: 31})
    else:
        t = timer_registers(p.timer)
        addr, bit = clock(p.timer)
        masks[addr] = masks.get(addr, 0) | bit
        masks.update({t.CR1: 0x81 if function == 'pwm' else 5,
                      t.PSC: 65535, t.ARR: 65535, t.EGR: 1})
        if function == 'pwm':
            masks[t.SR] = 0x65F
            masks[getattr(t, f'CCMR{1 if p.channel <= 2 else 2}')] = 255 << ((p.channel - 1) % 2 * 8)
            masks[t.CCER] = 3 << ((p.channel - 1) * 4)
            masks[getattr(t, f'CCR{p.channel}')] = 65535
            if p.timer == 'TIM1':
                masks[t.BDTR] = 0x8000
        else:
            masks.update({t.SMCR: 0x77, t.CCMR1: 0x303, t.CCER: 0x33, t.SR: 0x65F})
    return masks


def union_masks(selections):
    result = {}
    for function, p in selections.items():
        if p is not None:
            for a, m in masks_for(function, p).items():
                result[a] = result.get(a, 0) | m
    return result


def install_register_policy():
    """Union of candidate fields; a live transport additionally requires a selection lease."""
    safe = set(c.SAFE_READ_ADDRESSES)
    commands = set(c.COMMAND_REGISTERS)
    for function, candidates in CANDIDATES.items():
        for p in candidates:
            for a, m in masks_for(function, p).items():
                c.REAL_WRITE_MASKS[a] = c.REAL_WRITE_MASKS.get(a, 0) | m
            safe.update((p.gpio, p.base + 8, p.base + 12))
            for a in (p.base + 16, p.base + 20):
                commands.add(a)
                c.SELF_CLEARING_MASKS[a] = 0xFFFF
    for timer in TIMER_BASES:
        t = timer_registers(timer)
        safe.update(getattr(t, n) for n in OFFSETS if n != 'EGR' and (n != 'BDTR' or timer == 'TIM1'))
        commands.add(t.EGR)
        c.SELF_CLEARING_MASKS[t.EGR] = 1
    c.SAFE_READ_ADDRESSES = frozenset(safe)
    c.COMMAND_REGISTERS = frozenset(commands)


install_register_policy()
