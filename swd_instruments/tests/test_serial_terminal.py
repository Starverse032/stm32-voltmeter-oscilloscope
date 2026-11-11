"""Fake-only UART tests: real enumeration/open are guarded for every test."""
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

import serial
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from swd_instruments.serial_terminal import SerialTerminalWidget


class FakeSerial:
    def __init__(self):
        self.incoming = queue.Queue()
        self.writes = []
        self.closed = False
        self.timeout = 0.01
        self.io_threads = []
        self.partial = False

    @property
    def in_waiting(self):
        return 0

    def read(self, n):
        self.io_threads.append(threading.get_ident())
        try:
            item = self.incoming.get(timeout=self.timeout)
        except queue.Empty:
            return b''
        if isinstance(item, Exception):
            raise item
        return item

    def write(self, data):
        self.io_threads.append(threading.get_ident())
        count = min(2, len(data)) if self.partial else len(data)
        self.writes.append(data[:count])
        return count

    def close(self):
        self.io_threads.append(threading.get_ident())
        self.closed = True


class SerialTerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # Guard both wrappers and underlying APIs, including accidental fallback.
        self.guards = []
        for name in ['swd_instruments.serial_terminal._default_port_lister',
                     'swd_instruments.serial_terminal._default_serial_factory',
                     'serial.tools.list_ports.comports', 'serial.Serial']:
            patcher = patch(name, side_effect=AssertionError('REAL HARDWARE FORBIDDEN'))
            self.guards.append(patcher.start())
            self.addCleanup(patcher.stop)
        self.port = FakeSerial()
        self.lister = Mock(return_value=[('FAKE-ONLY', '模拟 CH340')])
        self.factory = Mock(return_value=self.port)
        self.widget = SerialTerminalWidget(port_lister=self.lister, serial_factory=self.factory)
        self.widget.show()
        self.app.processEvents()

    def tearDown(self):
        self.widget.shutdown()
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()
        for guard in self.guards:
            guard.assert_not_called()

    def until(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail('Timed out waiting for fake worker')

    def refresh(self):
        self.widget.refresh_button.click()
        self.until(lambda: self.widget._scan is None)

    def open(self):
        self.refresh()
        self.widget.open_button.click()
        self.until(self.widget.is_open)

    def receive(self, data, expected):
        self.port.incoming.put(data)
        self.until(lambda: expected in self.widget.receive_view.toPlainText())

    def test_no_automatic_access_and_refresh_only(self):
        self.lister.assert_not_called()
        self.factory.assert_not_called()
        self.assertFalse(self.widget.is_open())
        self.widget.hide()
        self.widget.show()
        self.app.processEvents()
        self.lister.assert_not_called()
        self.refresh()
        self.lister.assert_called_once_with()
        self.factory.assert_not_called()
        self.assertEqual(self.widget.port_combo.currentData(), 'FAKE-ONLY')

    def test_open_receive_split_utf8_and_counters(self):
        self.open()
        self.factory.assert_called_once_with('FAKE-ONLY', 9600, 0.05)
        self.receive(b'hello ', 'hello ')
        character = '中'.encode()
        self.port.incoming.put(character[:1])
        self.until(lambda: self.widget.rx_bytes == 7)
        self.assertEqual(self.widget.receive_view.toPlainText(), 'hello ')
        self.receive(character[1:], 'hello 中')
        self.assertEqual(self.widget.rx_bytes, 9)
        self.assertNotIn('�', self.widget.receive_view.toPlainText())
        self.assertTrue(all(t != threading.get_ident() for t in self.port.io_threads))

    def test_hex_display_timestamp_clear_and_block_cap(self):
        self.open()
        self.widget.display_combo.setCurrentText('HEX')
        self.widget.timestamp_check.setChecked(True)
        self.receive(b'\x00\x48\xff', '00 48 FF')
        self.assertRegex(self.widget.receive_view.toPlainText(), r'\[\d{2}:\d{2}:\d{2}\.\d+\]')
        self.widget.clear_button.click()
        self.assertEqual(self.widget.receive_view.toPlainText(), '')
        self.assertEqual(self.widget.rx_bytes, 3)
        self.widget.receive_view.setPlainText('x\n' * 4000)
        self.assertLessEqual(self.widget.receive_view.document().blockCount(), 3000)

    def test_gbk_split_and_replacement(self):
        self.open()
        self.widget.encoding_combo.setCurrentText('GBK')
        encoded = '文'.encode('gbk')
        self.port.incoming.put(encoded[:1])
        self.until(lambda: self.widget.rx_bytes == 1)
        self.receive(encoded[1:], '文')
        self.widget.encoding_combo.setCurrentText('UTF-8')
        self.receive(b'\xff', '�')

    def test_send_all_endings_and_enter(self):
        self.open()
        self.assertEqual(self.widget.ending_combo.currentData(), b'\r\n')
        expected = []
        for index, ending in enumerate([b'', b'\n', b'\r\n']):
            self.widget.ending_combo.setCurrentIndex(index)
            self.widget.send_edit.setText('SETLED 3')
            if index == 1:
                QTest.keyClick(self.widget.send_edit, Qt.Key.Key_Return)
            else:
                self.widget.send_button.click()
            expected.append(b'SETLED 3' + ending)
            self.until(lambda: self.port.writes == expected)
        self.until(lambda: self.widget.tx_bytes == sum(map(len, expected)))

    def test_hex_send_valid_invalid_no_write(self):
        self.open()
        self.widget.hex_send_check.setChecked(True)
        self.widget.ending_combo.setCurrentIndex(0)
        self.widget.send_edit.setText('48 65 6C')
        self.widget.send_button.click()
        self.until(lambda: self.port.writes == [b'Hel'])
        for invalid in ['GG', '4', '48 6Z', '0x48']:
            self.widget.send_edit.setText(invalid)
            self.widget.send_button.click()
            self.assertIn('格式错误', self.widget.status_label.text())
        QTest.qWait(40)
        self.assertEqual(self.port.writes, [b'Hel'])
        self.assertEqual(self.widget.tx_bytes, 3)

    def test_open_failure(self):
        self.factory.side_effect = OSError('fake open failure')
        self.refresh()
        self.widget.open_button.click()
        self.until(lambda: self.widget._worker is None)
        self.assertFalse(self.widget.is_open())
        self.assertIn('fake open failure', self.widget.status_label.text())
        self.assertTrue(self.widget.open_button.isEnabled())

    def test_device_errors_close_cleanly(self):
        for exception in [serial.SerialException('fake unplug'), OSError('fake IO')]:
            with self.subTest(exception=exception):
                self.port = FakeSerial()
                self.factory.return_value = self.port
                self.open()
                worker = self.widget._worker
                self.port.incoming.put(exception)
                self.until(lambda: self.widget._worker is None)
                self.assertTrue(not isValid(worker) or not worker.isRunning())
                self.assertTrue(self.port.closed)
                self.assertFalse(self.widget.is_open())
                self.assertIn('串口错误', self.widget.status_label.text())
                self.assertIn(str(exception), self.widget.status_label.text())

    def test_shutdown_idempotent_and_thread_finished(self):
        self.open()
        worker = self.widget._worker
        start = time.monotonic()
        self.widget.shutdown()
        self.assertLess(time.monotonic() - start, 1)
        self.assertFalse(worker.isRunning())
        self.assertTrue(self.port.closed)
        self.assertFalse(self.widget.is_open())
        self.widget.shutdown()
        self.widget.close_port()

    def test_toggle_close_and_reopen_ignores_stale_signals(self):
        self.open()
        self.widget.open_button.click()
        self.assertFalse(self.widget.is_open())
        self.port = FakeSerial()
        self.factory.return_value = self.port
        self.widget.open_button.click()
        self.until(self.widget.is_open)
        self.receive(b'reopened', 'reopened')

    def test_baud_validation_custom_and_refresh_failure(self):
        self.refresh()
        for invalid in ['bad', '299', '2000001', '9600.5']:
            self.widget.baud_combo.setCurrentText(invalid)
            self.widget.open_button.click()
            self.assertIn('整数', self.widget.status_label.text())
        self.factory.assert_not_called()
        self.widget.baud_combo.setCurrentText('12345')
        self.widget.open_button.click()
        self.until(self.widget.is_open)
        self.factory.assert_called_once_with('FAKE-ONLY', 12345, 0.05)
        self.widget.close_port()
        self.lister.side_effect = OSError('fake scan failure')
        self.refresh()
        self.assertIn('刷新串口失败', self.widget.status_label.text())

    def test_partial_write_counted_and_send_error(self):
        self.open()
        self.port.partial = True
        self.widget.send_edit.setText('FLASH 5')
        self.widget.send_button.click()
        self.until(lambda: self.widget.tx_bytes == 9)
        self.assertEqual(b''.join(self.port.writes), b'FLASH 5\r\n')
        self.port.write = Mock(side_effect=OSError('fake write failure'))
        self.widget.send_button.click()
        self.until(lambda: self.widget._worker is None)
        self.assertIn('fake write failure', self.widget.status_label.text())


if __name__ == '__main__':
    unittest.main()
