"""Standalone UART terminal. Hardware access occurs only after an explicit click.

Injected serial implementations must honor the supplied read timeout and provide
bounded writes/close, as pyserial does with the default factory below.
"""
from __future__ import annotations

import codecs
from datetime import datetime
import queue
import threading

import serial
from serial.tools import list_ports
from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QVBoxLayout, QWidget,
)


def _default_port_lister():
    return [(p.device, p.description) for p in list_ports.comports()]


def _default_serial_factory(port, baudrate, timeout):
    return serial.Serial(port=port, baudrate=baudrate, timeout=timeout,
                         write_timeout=0.2, bytesize=serial.EIGHTBITS,
                         parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE)


class _PortScan(QThread):
    listed = Signal(object)
    failed = Signal(str)

    def __init__(self, lister, parent):
        super().__init__(parent)
        self.lister = lister

    def run(self):
        try:
            self.listed.emit(self.lister())
        except Exception as exc:
            self.failed.emit(f'刷新串口失败：{exc}')


class _SerialWorker(QThread):
    opened = Signal()
    received = Signal(bytes)
    sent = Signal(int)
    failed = Signal(str)

    def __init__(self, factory, port, baud, parent):
        super().__init__(parent)
        self.factory, self.port, self.baud = factory, port, baud
        self.stop = threading.Event()
        self.outgoing = queue.Queue(maxsize=128)

    def run(self):
        connection = None
        try:
            connection = self.factory(self.port, self.baud, 0.05)
            if self.stop.is_set():
                return
            self.opened.emit()
            while not self.stop.is_set():
                # Limit each turn so sustained sends cannot starve reads/stop.
                try:
                    payload = self.outgoing.get_nowait()
                except queue.Empty:
                    payload = b''
                while payload and not self.stop.is_set():
                    count = connection.write(payload)
                    if not isinstance(count, int) or not 0 < count <= len(payload):
                        raise OSError('串口写入未完成')
                    self.sent.emit(count)
                    payload = payload[count:]
                if self.stop.is_set():
                    break
                data = connection.read(min(max(connection.in_waiting, 1), 4096))
                if data:
                    self.received.emit(bytes(data))
        except Exception as exc:
            if not self.stop.is_set():
                self.failed.emit(f'串口错误：{exc}')
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception as exc:
                    self.failed.emit(f'关闭串口失败：{exc}')


class SerialTerminalWidget(QWidget):
    def __init__(self, parent=None, *, port_lister=None, serial_factory=None):
        super().__init__(parent)
        self._port_lister = port_lister if port_lister is not None else _default_port_lister
        self._serial_factory = serial_factory if serial_factory is not None else _default_serial_factory
        self._worker = None
        self._scan = None
        self._open = False
        self._error = ''
        self.rx_bytes = self.tx_bytes = 0
        self.port_combo = QComboBox()
        self.refresh_button = QPushButton('刷新串口')
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(['9600', '115200', '19200', '38400', '57600',
                                  '230400', '460800', '921600'])
        self.open_button = QPushButton('打开串口')
        self.open_button.setProperty('variant', 'primary')
        self.receive_view = QPlainTextEdit()
        self.receive_view.setReadOnly(True)
        self.receive_view.setMaximumBlockCount(3000)
        self.display_combo = QComboBox()
        self.display_combo.addItems(['文本', 'HEX'])
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(['UTF-8', 'GBK'])
        self.timestamp_check = QCheckBox('时间戳')
        self.clear_button = QPushButton('清空')
        self.send_edit = QLineEdit()
        self.send_edit.setPlaceholderText('例如 SETLED 3 / FLASH 5')
        self.send_button = QPushButton('发送')
        self.ending_combo = QComboBox()
        for label, ending in [('无', b''), (r'\n', b'\n'), (r'\r\n', b'\r\n')]:
            self.ending_combo.addItem(label, ending)
        self.ending_combo.setCurrentIndex(2)
        self.hex_send_check = QCheckBox('HEX 发送')
        self.status_label = QLabel('串口未打开；点击“刷新串口”列出设备')
        self.status_label.setProperty('role', 'status')
        self.counter_label = QLabel()
        self.counter_label.setProperty('role', 'muted')
        layout = QVBoxLayout(self)
        for widgets in [
            [QLabel('串口'), self.port_combo, self.refresh_button, QLabel('波特率'),
             self.baud_combo, self.open_button],
            [QLabel('接收'), self.display_combo, self.encoding_combo,
             self.timestamp_check, self.clear_button],
        ]:
            row = QHBoxLayout()
            for widget in widgets:
                row.addWidget(widget)
            layout.addLayout(row)
        layout.addWidget(self.receive_view)
        row = QHBoxLayout()
        for widget in [self.send_edit, self.ending_combo, self.hex_send_check, self.send_button]:
            row.addWidget(widget)
        layout.addLayout(row)
        layout.addWidget(self.status_label)
        layout.addWidget(self.counter_label)
        self.refresh_button.clicked.connect(self._refresh)
        self.open_button.clicked.connect(self._toggle)
        self.send_button.clicked.connect(self._send)
        self.send_edit.returnPressed.connect(self._send)
        self.clear_button.clicked.connect(self._clear)
        self.encoding_combo.currentTextChanged.connect(self._reset_decoder)
        self.display_combo.currentTextChanged.connect(self._reset_decoder)
        self._reset_decoder()
        self._update_controls()
        self._update_counters()

    def is_open(self) -> bool:
        return self._open

    def close_port(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.stop.set()
            worker.wait()  # No GUI callbacks are needed for cooperative completion.
            self._cleanup_worker()

    def shutdown(self) -> None:
        self.close_port()
        if self._scan is not None:
            self._scan.wait()
            self._cleanup_scan()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    @Slot()
    def _refresh(self):
        if self._scan is not None:
            return
        self._scan = _PortScan(self._port_lister, self)
        self._scan.listed.connect(self._ports_listed)
        self._scan.failed.connect(self._scan_error)
        self._scan.finished.connect(self._finish_scan)
        self.refresh_button.setEnabled(False)
        self._scan.start()

    @Slot(object)
    def _ports_listed(self, ports):
        selected = self.port_combo.currentData()
        self.port_combo.clear()
        for device, description in ports:
            self.port_combo.addItem(f'{device} — {description}', device)
        index = self.port_combo.findData(selected)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)
        self.status_label.setText(f'找到 {self.port_combo.count()} 个串口')

    @Slot(str)
    def _scan_error(self, message):
        self.status_label.setText(message)

    @Slot()
    def _finish_scan(self):
        if self.sender() is not self._scan:
            return
        self._cleanup_scan()

    def _cleanup_scan(self):
        if self._scan is not None:
            self._scan.deleteLater()
            self._scan = None
        self.refresh_button.setEnabled(self._worker is None)

    @Slot()
    def _toggle(self):
        if self._worker is not None:
            self.close_port()
            return
        port = self.port_combo.currentData()
        if not port:
            self.status_label.setText('请先刷新并选择串口')
            return
        try:
            baud = int(self.baud_combo.currentText())
            if not 300 <= baud <= 2_000_000:
                raise ValueError()
        except ValueError:
            self.status_label.setText('波特率必须为 300–2000000 的整数')
            return
        self._error = ''
        self._reset_decoder()
        self._worker = _SerialWorker(self._serial_factory, port, baud, self)
        self._worker.opened.connect(self._opened)
        self._worker.received.connect(self._received)
        self._worker.sent.connect(self._sent)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self._finish_worker)
        self.status_label.setText('正在打开串口…')
        self._update_controls()
        self._worker.start()

    def _current_worker_signal(self):
        return self.sender() is self._worker and not self._worker.stop.is_set()

    @Slot()
    def _opened(self):
        if self._current_worker_signal():
            self._open = True
            self.status_label.setText('串口已打开（8N1）')
            self._update_controls()

    @Slot(str)
    def _failed(self, message):
        if self.sender() is self._worker:
            self._error = message
            self.status_label.setText(message)

    @Slot()
    def _finish_worker(self):
        if self.sender() is not self._worker:
            return
        self._cleanup_worker()

    def _cleanup_worker(self):
        worker = self._worker
        if worker is None:
            return
        worker.wait()
        self._worker = None
        self._open = False
        if self.display_combo.currentIndex() == 0:
            self._append(self._decoder.decode(b'', final=True))
        worker.deleteLater()
        self.status_label.setText(self._error or '串口已关闭')
        self._update_controls()

    def _update_controls(self):
        busy = self._worker is not None
        self.port_combo.setEnabled(not busy)
        self.baud_combo.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy and self._scan is None)
        self.open_button.setText('关闭串口' if busy else '打开串口')
        self.send_button.setEnabled(self._open)

    @Slot()
    def _reset_decoder(self):
        self._decoder = codecs.getincrementaldecoder(self.encoding_combo.currentText())(errors='replace')

    @Slot(bytes)
    def _received(self, data):
        if not self._current_worker_signal():
            return
        self.rx_bytes += len(data)
        text = (data.hex(' ').upper() + ' ' if self.display_combo.currentIndex() == 1
                else self._decoder.decode(data))
        if text and self.timestamp_check.isChecked():
            text = f'[{datetime.now():%H:%M:%S.%f}] {text}'
        self._append(text)
        self._update_counters()

    def _append(self, text):
        cursor = self.receive_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.receive_view.setTextCursor(cursor)

    @Slot()
    def _clear(self):
        self.receive_view.clear()
        self._reset_decoder()

    @Slot()
    def _send(self):
        if not self._open or self._worker is None:
            self.status_label.setText('请先打开串口')
            return
        try:
            text = self.send_edit.text()
            payload = (bytes.fromhex(text) if self.hex_send_check.isChecked()
                       else text.encode(self.encoding_combo.currentText()))
            payload += self.ending_combo.currentData()
        except (ValueError, UnicodeError):
            self.status_label.setText('发送格式错误：请检查 HEX 字节或文本编码')
            return
        try:
            self._worker.outgoing.put_nowait(payload)
            self.status_label.setText('发送已排队')
        except queue.Full:
            self.status_label.setText('发送队列已满，请稍后再试')

    @Slot(int)
    def _sent(self, count):
        if self._current_worker_signal():
            self.tx_bytes += count
            self._update_counters()

    def _update_counters(self):
        self.counter_label.setText(f'RX: {self.rx_bytes} 字节    TX: {self.tx_bytes} 字节')
