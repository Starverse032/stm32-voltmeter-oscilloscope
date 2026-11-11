"""冻结的控制器接口：驱动层实现，GUI 层只依赖这里的数据类型。

真实硬件路径只有在 `preflight()` 通过后才允许 `activate()`。所有长操作都
接受 CancelToken；取消不保证中断正在进行的单次 USB 传输，调用方必须把
“已请求取消”和“硬件状态未知”区分开。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Protocol


class CancelToken:
    """线程安全的协作取消标记，可重复设置。"""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class PreflightReport:
    """resources includes adc/pwm/meter/continuity bool availability,
    reasons dict[str, str] for unavailable functions, adcpre_divider int | None
    for a planned shared divider, and register snapshots (documented in README).
    issues contains blocking identity/global failures or no available function.
    """
    ok: bool
    issues: tuple[str, ...]
    device: dict
    clocks: dict
    resources: dict
    firmware_sha256: str | None
    profile_id: str
    notes: tuple[str, ...] = ()
    read_only: bool = True


@dataclass(frozen=True)
class ActivationReport:
    ok: bool
    issues: tuple[str, ...]
    adc2_ready: bool
    tim1_ready: bool
    output_active: bool
    clocks: dict
    notes: tuple[str, ...] = ()
    meter_ready: bool = False
    continuity_ready: bool = False


@dataclass(frozen=True)
class PulseResult:
    """States: measured, no-signal, out-of-range, not-ready, error, cancelled.

    session_active reports whether the controller still holds its live transaction.
    """
    ok: bool
    state: str
    period_s: float | None
    high_s: float | None
    frequency_hz: float | None
    duty_percent: float | None
    tick_s: float | None
    message: str
    session_active: bool = True


@dataclass(frozen=True)
class ContinuityResult:
    ok: bool
    closed: bool | None
    message: str


@dataclass(frozen=True)
class RecoveryReport:
    ok: bool
    restored: tuple[int, ...]
    errors: tuple[str, ...]
    output_state: str  # 'off-confirmed' | 'unknown-may-still-output' | 'not-owned'


@dataclass(frozen=True)
class PwmResult:
    ok: bool
    output_active: bool
    output_state: str
    actual_hz: float | None
    actual_duty: float | None
    plan: object | None
    message: str


@dataclass(frozen=True)
class SampleRequest:
    count: int = 64
    interval_s: float = 0.01


class RealController(Protocol):
    """hardware.RealDeviceController 必须实现的行为。"""

    @property
    def is_active(self) -> bool: ...

    def preflight(self, cancel: CancelToken | None = None) -> PreflightReport: ...

    def activate(self, cancel: CancelToken | None = None) -> ActivationReport: ...

    def sample(self, request: SampleRequest, cancel: CancelToken): ...

    def set_pwm(self, frequency_hz: float, duty_percent: float) -> PwmResult: ...

    def measure_pulse(self, timeout_s: float = 1.5,
                      cancel: CancelToken | None = None) -> PulseResult: ...

    def check_continuity(self) -> ContinuityResult: ...

    def output_off(self) -> PwmResult: ...

    def recover(self) -> RecoveryReport: ...

    def close(self) -> RecoveryReport | None: ...


@dataclass
class WorkerState:
    """GUI 侧展示用的连接状态快照，不代表硬件实时状态。"""

    connected: bool = False
    preflight_ok: bool = False
    activated: bool = False
    output_state: str = 'not-owned'
    profile_id: str = ''
    notes: tuple[str, ...] = field(default_factory=tuple)
