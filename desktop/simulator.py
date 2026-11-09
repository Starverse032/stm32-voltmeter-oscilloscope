# -*- coding: utf-8 -*-
"""
simulator.py —— 模拟数据源（纯逻辑，无 Qt 依赖）

关键约束：模拟数据必须与真实设备数据走**完全一致**的处理和显示链路：
  1. 用 protocol.build_sample() 构造"真实格式"的采样帧字节（含
     sample_rate_hz 与 vref_mv）；
  2. 字节流喂给 FrameParser（与串口线程共用的同一个解析器类型）；
  3. 得到 Frame 对象后，GUI 用与真实数据完全相同的统计 / 触发 / 换算函数。
本模块不直接产生"给 GUI 的便利数据"，不给界面开小灶。

场景：正弦、方波、直流、接近量程上下限、无触发、断线。
断线场景 next_frame_bytes() 返回 None，由 GUI 切换到"已断开"状态。
"""

from __future__ import annotations

import math
from typing import List, Optional

import protocol as p

SCENE_NAMES = {
    "sine": "正弦波",
    "square": "方波",
    "dc": "直流",
    "near_rails": "接近量程上下限",
    "no_trigger": "无触发（低电平直流）",
    "disconnect": "断线（不产帧）",
}


class Simulator:
    def __init__(self, scene: str = "sine", sample_rate_hz: int = 10000,
                 vref_mv: int = 3300, points: int = 1024) -> None:
        if scene not in SCENE_NAMES:
            raise ValueError("未知模拟场景: %s" % scene)
        self.scene = scene
        # 已确认生效的采样率 / 参考电压（切换要经响应确认后才改这里）
        self.sample_rate_hz = sample_rate_hz
        self.vref_mv = vref_mv
        self.points = points

        # 模拟设备"当前真实参数"，用于生成 SET_RATE / SET_VREF 的响应帧
        self.device_rate_hz = sample_rate_hz
        self.device_vref_mv = vref_mv
        # 可配置为 STATUS_BUSY 等，用于验证"设备忙，未切换"的回滚逻辑
        self.rate_status = p.STATUS_OK
        self.vref_status = p.STATUS_OK

        self._seq = 0            # 采样帧 SEQ 与累计帧数
        self._frame_index = 0    # 让连续帧相位缓慢推进，运行起来是"活"的

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    def set_scene(self, scene: str) -> None:
        if scene not in SCENE_NAMES:
            raise ValueError("未知模拟场景: %s" % scene)
        self.scene = scene

    def set_rate(self, rate_hz: int) -> None:
        """仅在响应确认生效后由 GUI 调用，同步模拟源与设备真实采样率。"""
        self.sample_rate_hz = rate_hz
        self.device_rate_hz = rate_hz

    def set_vref(self, vref_mv: int) -> None:
        self.vref_mv = vref_mv
        self.device_vref_mv = vref_mv

    # ------------------------------------------------------------------
    # 采样帧
    # ------------------------------------------------------------------
    def _gen_codes(self) -> List[int]:
        n = self.points
        rate = self.sample_rate_hz
        # 固定每帧约 5 个周期，保证不同采样率下截图都清晰
        cycles = 5.0
        freq = cycles * rate / n
        phase = 2.0 * math.pi * 0.1 * self._frame_index
        self._frame_index += 1
        out: List[int] = []
        for i in range(n):
            ang = 2.0 * math.pi * freq * i / rate + phase
            s = math.sin(ang)
            if self.scene == "sine":
                v = 2048 + 1800 * s
            elif self.scene == "square":
                v = 3600 if s >= 0 else 500
            elif self.scene == "dc":
                v = 2048
            elif self.scene == "near_rails":
                v = 2048 + 2040 * s          # 贴 0 / 4095 两端
            elif self.scene == "no_trigger":
                v = 300                       # 恒定低电平，永远不穿越默认阈值
            else:
                v = 2048
            out.append(max(0, min(p.ADC_MAX_CODE, int(round(v)))))
        return out

    def next_frame_bytes(self) -> Optional[bytes]:
        """返回一帧真实格式的采样帧字节；断线场景返回 None。

        GUI 应把返回值喂给统一的 FrameParser 入口（与串口数据一致）。
        """
        if self.scene == "disconnect":
            return None
        codes = self._gen_codes()
        seq = self._seq & 0xFF
        self._seq += 1
        return p.build_sample(self.sample_rate_hz, self.vref_mv, codes, seq)

    # ------------------------------------------------------------------
    # 参数变更响应（模拟设备应答，仍以帧字节形式经 FrameParser 走真实链路）
    # ------------------------------------------------------------------
    def build_rate_response(self, seq: int, requested_hz: int) -> bytes:
        """对 SET_RATE 的应答。status OK 时回读值 = 请求值；忙/失败时回读设备真实值。"""
        status = self.rate_status
        if status == p.STATUS_OK:
            self.device_rate_hz = requested_hz
        actual = self.device_rate_hz
        return p.build_response(p.CMD_SET_RATE, status, actual,
                                self.device_vref_mv, p.RUN_IDLE, 0,
                                self._seq, seq)

    def build_vref_response(self, seq: int, requested_mv: int) -> bytes:
        status = self.vref_status
        if status == p.STATUS_OK:
            self.device_vref_mv = requested_mv
        actual = self.device_vref_mv
        return p.build_response(p.CMD_SET_VREF, status, self.device_rate_hz,
                                actual, p.RUN_IDLE, 0, self._seq, seq)
