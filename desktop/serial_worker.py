# -*- coding: utf-8 -*-
"""
serial_worker.py —— QThread 串口读线程（pyserial）

要点：
  - 所有串口读写都在 QThread 子线程，主线程只通过 Qt 信号收发，绝不阻塞 GUI；
  - 收到的字节增量喂给 protocol.FrameParser（与模拟数据共用同一解析器类型）；
  - 串口 open 时 timeout=0.1s，即使没有数据 read() 也会周期返回，从而调用
    parser.tick() 驱动 1 秒截断超时恢复；
  - 命令下发用线程安全队列，主线程只入队，不直接碰串口对象；
  - stop_and_wait()：请求停止 → wait(超时) → terminate() 兜底，保证窗口关闭
    时线程与串口被可靠释放；
  - pyserial 缺失时提供降级：枚举返回空表、连接报错，但程序仍可跑模拟模式。

接收缓冲硬上限由 FrameParser 的 RX_BUF_MAX(8192) 保证；绘图数据每帧只保留
当前帧（最多 1024 点），不累积历史波形，避免长时间运行内存增长。
"""

from __future__ import annotations

import queue
import threading
from typing import List, Tuple

from PySide6.QtCore import QThread, Signal

import protocol as p

try:
    import serial
    from serial.tools import list_ports as _list_ports
    _HAS_SERIAL = True
except Exception:                       # pyserial 未安装 / 加载失败
    serial = None
    _list_ports = None
    _HAS_SERIAL = False


def has_serial() -> bool:
    return _HAS_SERIAL


def enumerate_ports() -> List[Tuple[str, str]]:
    """枚举可用串口，返回 [(device, description), ...]。无 pyserial 时返回空表。"""
    if not _HAS_SERIAL:
        return []
    try:
        return [(prt.device, prt.description or prt.device)
                for prt in _list_ports.comports()]
    except Exception:
        return []


class SerialWorker(QThread):
    frame_ready = Signal(object)        # protocol.Frame
    errors_ready = Signal(object)       # List[str]
    counts_ready = Signal(object)       # dict（解析器计数）
    connected = Signal(str)             # 连接成功信息
    disconnected = Signal(str)          # 断线原因
    log = Signal(str)                   # 一般日志

    BAUD = 115200
    READ_TIMEOUT = 0.1                  # 秒：无数据时也周期驱动 tick()
    READ_CHUNK = 2048

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._write_queue: "queue.Queue[bytes]" = queue.Queue()
        self._port = ""
        self._baud = self.BAUD

    # ------------------------------------------------------------------
    # 主线程调用的控制接口
    # ------------------------------------------------------------------
    def connect_to(self, port: str, baud: int = BAUD) -> bool:
        """启动读线程并在线程内打开串口，返回 False 表示已在运行。"""
        if self.isRunning():
            return False
        self._port = port
        self._baud = baud
        self._stop_event.clear()
        while not self._write_queue.empty():
            try:
                self._write_queue.get_nowait()
            except queue.Empty:
                break
        self.start()
        return True

    def send(self, data: bytes) -> None:
        """主线程调用：命令字节入队，由子线程写串口，不阻塞 GUI。"""
        self._write_queue.put(bytes(data))

    def request_stop(self) -> None:
        self._stop_event.set()

    def stop_and_wait(self, timeout_ms: int = 3000) -> bool:
        """可靠停止：置位停止事件 → wait(超时) → terminate() 兜底。返回是否干净退出。"""
        self._stop_event.set()
        if not self.isRunning():
            return True
        ok = self.wait(timeout_ms)
        if not ok:
            self.log.emit("线程未在 %d ms 内退出，执行 terminate() 兜底" % timeout_ms)
            self.terminate()
            ok = self.wait(1000)
        return ok

    # ------------------------------------------------------------------
    # 子线程
    # ------------------------------------------------------------------
    def run(self) -> None:
        if not _HAS_SERIAL:
            self.disconnected.emit("pyserial 未安装，无法连接真实串口（仅模拟模式可用）")
            return
        try:
            ser = serial.Serial(
                port=self._port,
                baudrate=self._baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.READ_TIMEOUT,
                write_timeout=0.5,
            )
        except Exception as e:
            self.disconnected.emit("打开串口失败：%s" % e)
            return

        self.connected.emit("已连接 %s @ %d 8N1" % (self._port, self._baud))
        parser = p.FrameParser()
        try:
            while not self._stop_event.is_set():
                # 1) 写命令（非阻塞取队列）
                while True:
                    try:
                        payload = self._write_queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        ser.write(payload)
                    except Exception as e:
                        self.errors_ready.emit(["命令写入失败：%s" % e])

                # 2) 读（0.1s 超时；无数据时也走 tick 驱动截断超时恢复）
                try:
                    data = ser.read(self.READ_CHUNK)
                except Exception as e:
                    self.disconnected.emit("串口读取异常：%s" % e)
                    break
                if data:
                    frames, errors = parser.feed(data)
                else:
                    frames, errors = parser.tick()
                for f in frames:
                    self.frame_ready.emit(f)
                if errors:
                    self.errors_ready.emit(errors)
                self.counts_ready.emit(parser.counts.as_dict())
        finally:
            try:
                ser.close()
            except Exception:
                pass
            self.log.emit("串口线程已退出，串口已关闭")
