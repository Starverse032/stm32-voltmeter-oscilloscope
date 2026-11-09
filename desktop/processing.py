# -*- coding: utf-8 -*-
"""
processing.py —— 纯数据处理层（无 Qt / 无串口依赖）

职责：
  - ADC 原始码 → 电压换算（严格按 docs/protocol.md 4.1）；
  - 帧内时间轴（每帧从 0 起，帧间空隙不隐藏）；
  - 统计量：平均 / 最小 / 最大 / 峰峰值；
  - 上升沿触发：在完整帧内找穿越阈值的点并对齐，绝不伪造预触发数据；
  - 可选频率估计：处理直流、噪声、周期不足、越界，无法判断返回"不可用"；
  - FrameHold：运行 / 暂停 / 单次冻结的纯逻辑，保证冻结后新帧不覆盖显示帧。

显示位数说明：12 位 ADC 在 3.3 V 下 1 LSB ≈ 0.806 mV，故电压统一显示
3 位小数（1 mV 量级），不冒充更高精度。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

# 3.3 V 参考下的 LSB 分辨率（仅用于界面标注，不参与换算）
LSB_MV_AT_3300 = 3300.0 / 4095.0        # ≈ 0.8059 mV
VOLT_DECIMALS = 3                        # 显示位数，不冒充精度
ADC_FULL_SCALE = 4095.0


def codes_to_volts(codes: Sequence[int], vref_mv: int) -> List[float]:
    """V = code / 4095.0 * (vref_mv / 1000.0)。

    设备 OLED 与电脑端必须用同一个 vref_mv；电脑端不得自行假定 3.300 V。
    """
    scale = (vref_mv / 1000.0) / ADC_FULL_SCALE
    return [float(c) * scale for c in codes]


def time_axis(n: int, sample_rate_hz: int) -> List[float]:
    """t_i = i / sample_rate_hz（秒），每帧从 0 重新开始。

    帧间存在空隙，本协议不声称连续无损实时流，故时间轴只是"帧内相对时间"。
    """
    if sample_rate_hz <= 0 or n <= 0:
        return []
    rate = float(sample_rate_hz)
    return [i / rate for i in range(n)]


@dataclass
class Stats:
    count: int
    mean: float
    vmin: float
    vmax: float
    vpp: float


def statistics(volts: Sequence[float]) -> Optional[Stats]:
    """平均 / 最小 / 最大 / 峰峰值；空数据返回 None（UI 显示 '--'）。"""
    if not volts:
        return None
    vmin = min(volts)
    vmax = max(volts)
    return Stats(
        count=len(volts),
        mean=sum(volts) / len(volts),
        vmin=vmin,
        vmax=vmax,
        vpp=vmax - vmin,
    )


@dataclass
class TriggerResult:
    """上升沿触发对齐结果。

    不伪造预触发数据：保留本帧全部真实样本，只是把时间零点平移到触发点，
    触发点之前的样本时间为负（它们是本帧真实采集到的数据，不是凭空补的）。
    若需要"帧开始之前"的数据，协议明确禁止伪造，故不做补点。
    """
    trigger_index: int          # 触发点在原帧中的索引
    volts: List[float]          # 原帧全部电压（未裁剪）
    times: List[float]          # 平移后的时间轴（触发点对应 t=0）
    threshold: float
    sample_rate_hz: int


def find_rising_trigger(volts: Sequence[float], threshold: float,
                        sample_rate_hz: int) -> Optional[TriggerResult]:
    """在完整帧内找第一个"上升沿穿越阈值"的点：v[i-1] < 阈值 <= v[i]。

    - 找不到（含直流、阈值超出波形范围、数据不足 2 点）→ 返回 None，
      UI 据此显示"等待触发 / 未触发"；
    - 直流恒定等于阈值时不算穿越（没有从低到高的变化），正确返回 None；
    - 找到则把时间轴平移，使触发点对应 t=0，返回 TriggerResult。
    """
    if sample_rate_hz <= 0 or len(volts) < 2:
        return None
    rate = float(sample_rate_hz)
    for i in range(1, len(volts)):
        if volts[i - 1] < threshold <= volts[i]:
            times = [(j - i) / rate for j in range(len(volts))]
            return TriggerResult(i, list(volts), times, threshold, sample_rate_hz)
    return None


@dataclass
class FreqResult:
    ok: bool
    hz: float
    reason: str


def estimate_frequency(volts: Sequence[float], sample_rate_hz: int) -> FreqResult:
    """可选频率估计（基于中线上升沿穿越 + 迟滞），无法判断一律返回不可用。

    覆盖：直流 / 幅度过小、噪声（迟滞 + 幅度门限）、周期不足（帧内不足一个
    完整周期或每周期采样点过少）、越界（超过奈奎斯特或频率非正）。
    """
    n = len(volts)
    if n < 8 or sample_rate_hz <= 0:
        return FreqResult(False, 0.0, "数据不足")
    vmin = min(volts)
    vmax = max(volts)
    amp = vmax - vmin
    # 直流 / 幅度过小（相对 3.3 V 量程取 50 mV 门限，抑制噪声误判）
    if amp < 0.05:
        return FreqResult(False, 0.0, "直流或幅度过小")
    mid = (vmin + vmax) / 2.0
    hyst = amp * 0.1
    # 带迟滞的上升沿穿越：先低于 mid-hyst 武装，再高于 mid+hyst 记一次
    crossings: List[int] = []
    armed = False
    for i, v in enumerate(volts):
        if not armed and v < mid - hyst:
            armed = True
        elif armed and v > mid + hyst:
            crossings.append(i)
            armed = False
    if len(crossings) < 2:
        return FreqResult(False, 0.0, "未检出完整周期（周期不足）")
    periods = [crossings[k + 1] - crossings[k] for k in range(len(crossings) - 1)]
    avg_period = sum(periods) / len(periods)
    if avg_period < 4.0:
        return FreqResult(False, 0.0, "每周期采样点过少（周期不足）")
    freq = sample_rate_hz / avg_period
    nyquist = sample_rate_hz / 2.0
    if freq <= 0 or freq > nyquist * 0.95:
        return FreqResult(False, 0.0, "估计频率越界（超过奈奎斯特）")
    return FreqResult(True, freq, "")


class FrameHold:
    """运行 / 暂停(保持) / 单次冻结 的纯逻辑控制器。

    语义（docs/protocol.md 第 9 节）：暂停或单次冻结后，后台线程即使收到
    新帧也不得覆盖当前显示帧。把这段逻辑独立出来便于单元测试，GUI 只负责
    把"是否更新"的结果映射到绘图。

    mode:
      "run"    —— submit() 覆盖当前帧；
      "hold"   —— submit() 一律不覆盖（保留历史帧）；
      "single" —— arm_single() 后只接收第一帧，随后自动冻结，不再覆盖。
    """

    MODE_RUN = "run"
    MODE_HOLD = "hold"
    MODE_SINGLE = "single"

    def __init__(self) -> None:
        self.mode = self.MODE_RUN
        self.frame: Any = None

    def set_mode(self, mode: str) -> None:
        if mode not in (self.MODE_RUN, self.MODE_HOLD, self.MODE_SINGLE):
            raise ValueError("未知显示模式: %s" % mode)
        self.mode = mode

    def arm_single(self) -> None:
        """进入单次采集：清空当前帧，等待下一帧，收到后自动冻结。"""
        self.mode = self.MODE_SINGLE
        self.frame = None

    def submit(self, frame_data: Any) -> bool:
        """提交一帧。返回 True 表示"当前显示帧被更新"，False 表示被冻结丢弃。"""
        if self.mode == self.MODE_HOLD:
            return False
        if self.mode == self.MODE_SINGLE:
            if self.frame is not None:
                return False          # 已冻结，新帧不覆盖
            self.frame = frame_data
            return True
        # MODE_RUN
        self.frame = frame_data
        return True

    def clear(self) -> None:
        self.frame = None


# --------------------------------------------------------------------------
# 参数变更确认（docs/protocol.md 2.1 / 4.3 / 第 9 节）
# --------------------------------------------------------------------------
import protocol as p  # noqa: E402  （纯逻辑模块，无 Qt 依赖）


@dataclass
class _PendingChange:
    seq: int
    cmd: int
    value: int
    label: str


class ParameterSwitch:
    """采样率 / 参考电压切换的"切换中 → 确认 → 生效或回滚"纯逻辑。

    只有同时满足：响应 SEQ 匹配、ack_cmd 匹配、status == 0x00、且回读值
    等于请求值，才判定"生效"；否则一律保持旧状态并给出原因（含 status=0x02
    "设备忙，未切换"）。GUI 只把判定结果映射到界面，不自行放宽条件。
    """

    def __init__(self) -> None:
        self.pending: Optional[_PendingChange] = None

    def arm(self, seq: int, cmd: int, value: int, label: str = "") -> None:
        """发出 SET_RATE / SET_VREF 命令后记录待确认项，界面显示"切换中"。"""
        self.pending = _PendingChange(seq, cmd, value, label)

    def is_pending(self) -> bool:
        return self.pending is not None

    def cancel(self) -> None:
        self.pending = None

    def check(self, frame_seq: int, rd: "p.ResponseData") -> Tuple[bool, str]:
        """收到响应帧时判定。返回 (是否生效, 提示文本)。"""
        if self.pending is None:
            return False, "没有待确认的参数变更"
        pend = self.pending
        if frame_seq != pend.seq:
            return False, ("响应 SEQ 不匹配（0x%02X != 0x%02X），保持旧状态"
                           % (frame_seq & 0xFF, pend.seq & 0xFF))
        if rd.ack_cmd != pend.cmd:
            return False, "响应命令不匹配，保持旧状态"
        if rd.status != p.STATUS_OK:
            self.pending = None
            return False, ("%s（status=0x%02X），未生效并回滚"
                           % (p.STATUS_TEXT.get(rd.status, "设备未确认"), rd.status))
        # status OK 时仍必须回读正确
        if pend.cmd == p.CMD_SET_RATE and rd.actual_rate_hz != pend.value:
            self.pending = None
            return False, ("回读采样率 %d != 请求 %d，回滚"
                           % (rd.actual_rate_hz, pend.value))
        if pend.cmd == p.CMD_SET_VREF and rd.vref_mv != pend.value:
            self.pending = None
            return False, ("回读参考电压 %d mV != 请求 %d mV，回滚"
                           % (rd.vref_mv, pend.value))
        self.pending = None
        return True, "%s 已生效" % (pend.label or "参数")
