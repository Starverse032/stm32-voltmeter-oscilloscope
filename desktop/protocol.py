# -*- coding: utf-8 -*-
"""
protocol.py —— STM32 电压表/示波器通信协议（电脑端纯逻辑层）

唯一依据：docs/protocol.md v1（2026-11-09）。本模块不依赖 Qt / pyserial，
可以独立用 unittest 测试，也供 GUI 与模拟数据源共同使用。

帧结构（所有多字节整数一律小端 Little Endian）：

    偏移  长度  字段
    0     4     MAGIC   A5 5A C3 3C
    4     1     VER     = 0x01
    5     1     TYPE
    6     2     LEN     payload 字节数，0..2060
    8     1     SEQ
    9     1     FLAGS   保留，必须为 0
    10    LEN   PAYLOAD
    10+LEN 2    CRC16   uint16 LE（低字节在前）

固定头 10 字节，整帧长度 = LEN + 12，LEN_MAX = 2060，整帧合法上限 2072。

CRC-16/CCITT-FALSE：poly 0x1021，初值 0xFFFF，输入/输出不反射，xorout 0，
逐字节 MSB first。覆盖范围 = 从 VER(偏移 4) 到 PAYLOAD 最后一个字节，
共 6 + LEN 字节；不含 MAGIC，也不含帧尾 CRC 本身。
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
MAGIC = b"\xA5\x5A\xC3\x3C"
VER = 0x01

TYPE_SAMPLE = 0x10
TYPE_COMMAND = 0x20
TYPE_RESPONSE = 0x21
TYPE_EVENT = 0x22
KNOWN_TYPES = (TYPE_SAMPLE, TYPE_COMMAND, TYPE_RESPONSE, TYPE_EVENT)

LEN_MAX = 2060                 # payload 上限
FRAME_MAX = 2072               # 整帧上限 = LEN_MAX + 12
FIXED_HEADER = 10              # MAGIC4 + VER1 + TYPE1 + LEN2 + SEQ1 + FLAGS1
RX_BUF_MAX = 8192              # 接收缓冲硬上限
FRAME_TIMEOUT = 1.0            # 截断帧超时（秒）

# 命令 ID（TYPE = 0x20）
CMD_START = 0x01
CMD_STOP = 0x02
CMD_SINGLE = 0x03
CMD_SET_RATE = 0x04
CMD_SET_VREF = 0x05
CMD_PING = 0x06
CMD_SELFTEST_PWM = 0x07
CMD_QUERY_STATUS = 0x08

# 响应 status（TYPE = 0x21）
STATUS_OK = 0x00
STATUS_BAD_PARAM = 0x01
STATUS_BUSY = 0x02
STATUS_NOT_SUPPORTED = 0x03
STATUS_HW_ERROR = 0x04

STATUS_TEXT = {
    STATUS_OK: "OK（已生效）",
    STATUS_BAD_PARAM: "参数非法",
    STATUS_BUSY: "设备忙，未切换",
    STATUS_NOT_SUPPORTED: "不支持",
    STATUS_HW_ERROR: "硬件错误",
}

# 响应 run_state
RUN_IDLE = 0
RUN_RUN = 1
RUN_SINGLE_BUSY = 2

# 事件 code（TYPE = 0x22）
EVENT_PROTOCOL_ERROR = 0x01
EVENT_ADC_OVERFLOW = 0x02
EVENT_FRAME_DROPPED = 0x03
EVENT_FRAME_GAP = 0x04
EVENT_OTHER = 0x05

EVENT_TEXT = {
    EVENT_PROTOCOL_ERROR: "协议错误",
    EVENT_ADC_OVERFLOW: "ADC 溢出/DMA 异常",
    EVENT_FRAME_DROPPED: "帧被丢弃",
    EVENT_FRAME_GAP: "帧间隔异常",
    EVENT_OTHER: "其他",
}

# 采样帧约束
SAMPLE_POINT_MIN = 1
SAMPLE_POINT_MAX = 1024
ADC_MAX_CODE = 4095
ALLOWED_RATES = (10000, 100000)
VREF_MIN_MV = 2500
VREF_MAX_MV = 3600


# --------------------------------------------------------------------------
# CRC-16/CCITT-FALSE（自己实现，不用第三方库）
# --------------------------------------------------------------------------
def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE。标准自检向量：crc16(b'123456789') == 0x29B1。

    逐字节：先把字节放到 CRC 高 8 位异或，再迭代 8 次"左移 + 条件异或
    0x1021"。这正是 docs/protocol.md 第 5 节 CRC_STEP 宏展开后的语义。
    """
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF


# --------------------------------------------------------------------------
# 帧编码（供命令下发、模拟数据源、测试辅助使用）
# --------------------------------------------------------------------------
def encode_frame(ftype: int, seq: int, payload: bytes = b"",
                 flags: int = 0, ver: int = VER) -> bytes:
    """按协议拼一整帧并计算 CRC。CRC 覆盖段 = 头 6 字节(VER..FLAGS) + payload。"""
    payload = bytes(payload)
    length = len(payload)
    # VER TYPE LEN_L LEN_H SEQ FLAGS，共 6 字节；后面接 payload 即 CRC 覆盖段
    head = struct.pack(
        "<BBBBBB",
        ver & 0xFF,
        ftype & 0xFF,
        length & 0xFF,
        (length >> 8) & 0xFF,
        seq & 0xFF,
        flags & 0xFF,
    )
    body = head + payload
    crc = crc16_ccitt_false(body)
    # CRC 低字节在前（小端）
    return MAGIC + body + struct.pack("<H", crc)


def build_command(cmd_id: int, seq: int, params: bytes = b"", flags: int = 0) -> bytes:
    """命令帧 0x20：payload[0] = cmd_id，其后为参数。"""
    return encode_frame(TYPE_COMMAND, seq, bytes([cmd_id]) + bytes(params), flags)


def build_start(seq: int) -> bytes:
    return build_command(CMD_START, seq)


def build_stop(seq: int) -> bytes:
    return build_command(CMD_STOP, seq)


def build_single(seq: int) -> bytes:
    return build_command(CMD_SINGLE, seq)


def build_set_rate(rate_hz: int, seq: int) -> bytes:
    """SET_RATE：4 字节 uint32 LE。"""
    return build_command(CMD_SET_RATE, seq, struct.pack("<I", rate_hz))


def build_set_vref(vref_mv: int, seq: int) -> bytes:
    """SET_VREF：2 字节 uint16 LE。"""
    return build_command(CMD_SET_VREF, seq, struct.pack("<H", vref_mv))


def build_ping(seq: int) -> bytes:
    return build_command(CMD_PING, seq)


def build_selftest_pwm(enable: bool, seq: int) -> bytes:
    return build_command(CMD_SELFTEST_PWM, seq, bytes([1 if enable else 0]))


def build_query_status(seq: int) -> bytes:
    return build_command(CMD_QUERY_STATUS, seq)


def build_response(ack_cmd: int, status: int, actual_rate_hz: int, vref_mv: int,
                   run_state: int, dev_flags: int, frames_sent: int,
                   seq: int) -> bytes:
    """响应帧 0x21，LEN 固定 14。ack_cmd/status/实际采样率/vref/状态/标志/帧计数。

    status != OK 时 actual_rate_hz 与 vref_mv 仍填设备当前真实值（用于回滚）。
    """
    payload = struct.pack(
        "<BBIHBBI",
        ack_cmd & 0xFF,
        status & 0xFF,
        actual_rate_hz & 0xFFFFFFFF,
        vref_mv & 0xFFFF,
        run_state & 0xFF,
        dev_flags & 0xFF,
        frames_sent & 0xFFFFFFFF,
    )
    return encode_frame(TYPE_RESPONSE, seq, payload)


def build_event(code: int, seq: int, text: str = "") -> bytes:
    """事件帧 0x22：code + 可选 ASCII 说明（≤63 字节，禁止中文）。"""
    payload = bytes([code]) + text.encode("ascii", errors="replace")[:63]
    return encode_frame(TYPE_EVENT, seq, payload)


def build_sample(sample_rate_hz: int, vref_mv: int, samples: List[int],
                 seq: int) -> bytes:
    """采样帧 0x10：10 字节元信息 + 2*N 字节样本，供模拟数据源走真实编码链路。

    注意：模拟源也调用本函数构造"真实格式"的帧字节，再经 FrameParser 解析，
    与串口真实数据完全共用同一条处理/显示链路，不给 GUI 开小灶。
    """
    n = len(samples)
    payload = struct.pack("<IHHH", sample_rate_hz, n, vref_mv, 0)
    payload += struct.pack("<%dH" % n, *samples)
    return encode_frame(TYPE_SAMPLE, seq, payload)


# --------------------------------------------------------------------------
# payload 解析（CRC 已由 FrameParser 校验通过后调用）
# --------------------------------------------------------------------------
@dataclass
class Frame:
    """一帧已经通过 VER / LEN 自洽 / CRC 校验的合法帧（TYPE 已知）。"""
    ftype: int
    ver: int
    seq: int
    flags: int
    length: int
    payload: bytes
    raw: bytes
    crc: int


@dataclass
class SampleData:
    ok: bool
    reason: str = ""
    sample_rate_hz: int = 0
    point_count: int = 0
    vref_mv: int = 0
    samples: List[int] = field(default_factory=list)


@dataclass
class ResponseData:
    ok: bool
    reason: str = ""
    ack_cmd: int = 0
    status: int = 0
    actual_rate_hz: int = 0
    vref_mv: int = 0
    run_state: int = 0
    dev_flags: int = 0
    frames_sent: int = 0


@dataclass
class EventData:
    code: int = 0
    text: str = ""


def parse_command_payload(payload: bytes) -> Tuple[Optional[int], bytes]:
    """返回 (cmd_id, 参数)。空 payload 返回 (None, b'')。"""
    if not payload:
        return None, b""
    return payload[0], bytes(payload[1:])


def parse_sample_payload(payload: bytes) -> SampleData:
    """解析 SAMPLE payload 并做语义校验。

    约束（docs/protocol.md 4.1）：
      - LEN == 10 + 2*point_count，point_count 1..1024；
      - reserved == 0；
      - 每个样本码 0..4095，>4095 视为数据损坏，整帧丢弃（ok=False）。
    """
    if len(payload) < 10:
        return SampleData(False, "采样 payload 不足 10 字节元信息")
    sample_rate_hz, point_count, vref_mv, reserved = struct.unpack_from("<IHHH", payload, 0)
    if point_count < SAMPLE_POINT_MIN or point_count > SAMPLE_POINT_MAX:
        return SampleData(False, "point_count 越界（需 1..1024）")
    if len(payload) != 10 + 2 * point_count:
        return SampleData(False, "LEN 与 point_count 不符")
    if reserved != 0:
        return SampleData(False, "reserved 非 0")
    if sample_rate_hz not in ALLOWED_RATES:
        return SampleData(False, "采样率不在设备支持范围内")
    if not VREF_MIN_MV <= vref_mv <= VREF_MAX_MV:
        return SampleData(False, "参考电压越界")
    samples = list(struct.unpack_from("<%dH" % point_count, payload, 10))
    for s in samples:
        if s > ADC_MAX_CODE:
            return SampleData(False, "样本码 > 4095，数据损坏，整帧丢弃")
    return SampleData(True, "", sample_rate_hz, point_count, vref_mv, samples)


def parse_response_payload(payload: bytes) -> ResponseData:
    """解析 RESPONSE payload，LEN 固定 14。"""
    if len(payload) != 14:
        return ResponseData(False, "响应帧 LEN != 14")
    (ack_cmd, status, actual_rate_hz, vref_mv,
     run_state, dev_flags, frames_sent) = struct.unpack("<BBIHBBI", payload)
    if (actual_rate_hz not in ALLOWED_RATES
            or not VREF_MIN_MV <= vref_mv <= VREF_MAX_MV
            or run_state not in (RUN_IDLE, RUN_RUN, RUN_SINGLE_BUSY)
            or status not in STATUS_TEXT):
        return ResponseData(False, "响应状态/采样率/参考电压非法")
    return ResponseData(True, "", ack_cmd, status, actual_rate_hz, vref_mv,
                        run_state, dev_flags, frames_sent)


def parse_event_payload(payload: bytes) -> EventData:
    """解析 EVENT payload：code + ASCII 文本。"""
    if not payload:
        return EventData(0, "")
    return EventData(payload[0], payload[1:].decode("ascii", errors="replace"))


# --------------------------------------------------------------------------
# LEN 与 TYPE 自洽检查（docs/protocol.md 第 3 节允许 LEN + 4.1/4.3）
# --------------------------------------------------------------------------
def _len_type_consistent(ftype: int, length: int) -> bool:
    if ftype == TYPE_SAMPLE:
        # LEN = 10 + 2*N，N = 1..1024 → LEN ∈ {12, 14, ..., 2058}
        if not (12 <= length <= LEN_MAX):
            return False
        if (length - 10) % 2 != 0:
            return False
        n = (length - 10) // 2
        return SAMPLE_POINT_MIN <= n <= SAMPLE_POINT_MAX
    if ftype == TYPE_COMMAND:
        return 1 <= length <= 8
    if ftype == TYPE_RESPONSE:
        return length == 14
    if ftype == TYPE_EVENT:
        return 1 <= length <= 64
    # 未知 TYPE：无法按表判定，交给 CRC 通过后的"整帧丢弃"分支处理
    return 0 <= length <= LEN_MAX


# --------------------------------------------------------------------------
# 增量字节流解析器（严格按 docs/protocol.md 第 6 节状态机）
# --------------------------------------------------------------------------
@dataclass
class ParserCounts:
    valid_frames: int = 0
    crc_errors: int = 0
    illegal_length: int = 0
    version_mismatch: int = 0
    unknown_type: int = 0
    timeouts: int = 0
    buffer_overflows: int = 0
    noise_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "有效帧": self.valid_frames,
            "CRC 错误": self.crc_errors,
            "非法长度": self.illegal_length,
            "版本不匹配": self.version_mismatch,
            "未知类型": self.unknown_type,
            "超时帧": self.timeouts,
            "缓冲区溢出": self.buffer_overflows,
            "丢弃噪声字节": self.noise_bytes,
        }


class FrameParser:
    """增量字节流解析器。串口一次 read() 不等于一个完整包。

    用法：每次收到字节调用 feed(data)；没有新数据时也要定期调用 tick() 以驱动
    1 秒截断超时恢复（串口线程里 read 带 0.1s 超时，天然会周期调用）。
    两者都返回 (frames, errors)：frames 为本次解出的合法帧列表，errors 为
    供状态栏显示的文本列表。

    状态码严格按第 6 节：
      1. buf 超过 RX_BUF_MAX(8192) → 整体清空、重新同步、上报一次错误；
      2. 搜索 MAGIC，未找到只保留末尾 3 字节（防止 MAGIC 被分片切断）；
      3. 找到 MAGIC 后不足 6 字节头信息 → 等待；
      4. VER != 0x01 → 丢弃 MAGIC 首字节，回到 2；
      5. LEN > 2060 或与 TYPE 不自洽 → 非法长度，丢弃 MAGIC 首字节，回到 2；
      6. 等 6+LEN+2 齐备后校验 CRC：不匹配丢弃 MAGIC 首字节回到 2；
         匹配则产出 Frame、移除整帧、继续解析（一次 read 含多帧 = 合并帧）；
      7. 已定位 MAGIC 的帧 1s 内未收齐 → 丢弃 MAGIC 首字节回到 2 并提示；
      8. 各类错误分别计数。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._buf = bytearray()
        self._clock = clock
        # 记录"已定位 MAGIC 但帧未收齐"的起始时刻，用于截断超时恢复
        self._pending_since: Optional[float] = None
        self.counts = ParserCounts()
        self.errors: List[str] = []   # 最近一批错误文本（UI 状态栏用）

    def reset(self) -> None:
        """清空缓冲与统计（连接新设备 / 切到模拟模式时调用）。"""
        self._buf.clear()
        self._pending_since = None
        self.counts = ParserCounts()
        self.errors = []

    def feed(self, data: bytes, now: Optional[float] = None) -> Tuple[List[Frame], List[str]]:
        """喂入新收到的字节。now 可注入，便于测试模拟时间流逝。"""
        if now is None:
            now = self._clock()
        errors: List[str] = []
        self._buf.extend(data)
        if len(self._buf) > RX_BUF_MAX:
            self.counts.buffer_overflows += 1
            errors.append("接收缓冲区超过 8192 字节，已整体清空并重新同步")
            self._buf.clear()
            self._pending_since = None
        frames = self._parse(now, errors)
        self.errors = errors[-20:]
        return frames, errors

    def tick(self, now: Optional[float] = None) -> Tuple[List[Frame], List[str]]:
        """无新数据时驱动超时检查；签名与 feed 一致，方便统一处理。"""
        if now is None:
            now = self._clock()
        errors: List[str] = []
        frames = self._parse(now, errors)
        self.errors = errors[-20:]
        return frames, errors

    def _mark_pending(self, now: float) -> None:
        if self._pending_since is None:
            self._pending_since = now

    def _parse(self, now: float, errors: List[str]) -> List[Frame]:
        frames: List[Frame] = []
        while True:
            # 7. 截断包恢复：定位过 MAGIC 但 1s 仍未收齐 → 丢弃 MAGIC 首字节
            if self._pending_since is not None and (now - self._pending_since) >= FRAME_TIMEOUT:
                self.counts.timeouts += 1
                errors.append("帧超时（>1s 未收齐），丢弃该 MAGIC 首字节重新同步")
                if self._buf:
                    self._buf.pop(0)   # 丢弃 A5，回到步骤 2 重新搜索
                self._pending_since = None

            # 2. 搜索 MAGIC
            idx = self._buf.find(MAGIC)
            if idx < 0:
                # 未找到：只保留末尾 3 字节，防止 MAGIC 被分片切断
                if len(self._buf) > 3:
                    self.counts.noise_bytes += len(self._buf) - 3
                    del self._buf[:-3]
                self._pending_since = None
                return frames
            if idx > 0:
                # MAGIC 之前是噪声 / 上一坏帧残骸，丢弃
                self.counts.noise_bytes += idx
                del self._buf[:idx]

            # 3. MAGIC 之后不足 6 字节（即整头不足 10 字节）→ 等待更多数据
            if len(self._buf) < FIXED_HEADER:
                self._mark_pending(now)
                return frames

            ver = self._buf[4]
            ftype = self._buf[5]
            length = self._buf[6] | (self._buf[7] << 8)
            seq = self._buf[8]
            flags = self._buf[9]

            # 4. VER 校验
            if ver != VER:
                self.counts.version_mismatch += 1
                errors.append("VER=0x%02X != 0x01，版本不匹配，丢弃 MAGIC 首字节" % ver)
                self._buf.pop(0)
                self._pending_since = None
                continue

            # 5. LEN 上限与 TYPE 自洽
            if length > LEN_MAX or not _len_type_consistent(ftype, length):
                self.counts.illegal_length += 1
                errors.append("LEN=%d 与 TYPE=0x%02X 不自洽/越界，丢弃 MAGIC 首字节"
                              % (length, ftype))
                self._buf.pop(0)
                self._pending_since = None
                continue

            # 6. 等待整帧齐备（整帧 = 12 + LEN）
            total = FIXED_HEADER + length + 2
            if len(self._buf) < total:
                self._mark_pending(now)
                return frames

            raw = bytes(self._buf[:total])
            # CRC 覆盖：从 VER(偏移4) 到 PAYLOAD 末，共 6+LEN 字节
            crc_calc = crc16_ccitt_false(raw[4:4 + 6 + length])
            crc_recv = raw[total - 2] | (raw[total - 1] << 8)
            if crc_calc != crc_recv:
                self.counts.crc_errors += 1
                errors.append("CRC 不匹配（计算 0x%04X / 帧内 0x%04X），丢弃 MAGIC 首字节"
                              % (crc_calc, crc_recv))
                self._buf.pop(0)
                self._pending_since = None
                continue

            # CRC 匹配：移除整帧，继续解析剩余字节（多帧合并）
            del self._buf[:total]
            self._pending_since = None
            payload = raw[10:10 + length]

            if ftype not in KNOWN_TYPES:
                # 未知 TYPE：整帧丢弃，计数一次协议错误，不解释 payload
                self.counts.unknown_type += 1
                errors.append("未知 TYPE=0x%02X，整帧丢弃" % ftype)
                continue

            self.counts.valid_frames += 1
            frames.append(Frame(ftype, ver, seq, flags, length, payload, raw, crc_recv))
