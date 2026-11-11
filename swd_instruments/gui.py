"""中文模拟仪器 + 真实SWD实验页（ADC2/PA4 采样、TIM1_CH1/PA8 方波）。

真实路径只在已审查 profile 通过只读预检后由后端事务开放。GUI 不包含绕过
白名单、复位目标、强杀线程或把模拟帧改标真实测量的开关。
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListView, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTabWidget, QVBoxLayout, QWidget, QGroupBox, QListWidget, QListWidgetItem,
)
import pyqtgraph as pg

from .pins import CANDIDATES, CONTINUITY, DEFAULT_OCCUPIED
from .selectable import restricted, RISK
from .contracts import CancelToken, SampleRequest
from .core import Simulator, diagnose, export_csv, plan_pwm
from .hardware import open_real_controller
from .profiles import ProfileError, discover_profiles, load_profile
from .serial_terminal import SerialTerminalWidget
from .theme import AMBER, INSET, MUTED, REAL, SIM, STYLESHEET, build_palette, ui_font
from .transport import ALLOWED_SWD_HZ, DEFAULT_SWD_HZ, PyOCDSWD


def PyOCDDiagnostic(serial_number: str, swd_hz: int | None = None):
    """兼容旧名；延迟转发到当前模块的 PyOCDSWD，便于测试替换任一名字。"""
    return PyOCDSWD(serial_number) if swd_hz is None else PyOCDSWD(serial_number, swd_hz)


# (时钟Hz, 界面文字, 2026-11-10 实物记录的平均秒/点)。秒/点只用于估算等待时间，
# 不是性能保证；测速方法与结果见仓库 README 的“SWD 时钟”一节。
SWD_CLOCK_CHOICES = (
    (950_000, '950 kHz（默认，实测约14点/秒）', 0.072),
    (1_800_000, '1.8 MHz（实测约17点/秒）', 0.057),
    (480_000, '480 kHz（实测约9点/秒）', 0.109),
    (240_000, '240 kHz（实测约6点/秒）', 0.167),
    (100_000, '100 kHz（最保守，实测约3点/秒）', 0.346),
)
assert SWD_CLOCK_CHOICES[0][0] == DEFAULT_SWD_HZ
assert {choice[0] for choice in SWD_CLOCK_CHOICES} == set(ALLOWED_SWD_HZ)


LIMITATIONS = (
    '输出限制：只支持固定逻辑幅值的方波/PWM（0～3.3V 逻辑，不保证任意幅值、正弦或三角）；'
    'ADC 仅测 0–3.3 V，PA4/PA7 不耐受 5 V；PA6 不耐受 5 V，仅限 0–3.3 V；HC-SR04 Echo 等 5 V 信号禁止直接连接；PB10 耐受 5 V，通断仅检测接地；'
    '主机轮询时间戳非等间隔，高频输入可能混叠；'
    '参考电压是 profile 配置值，不是现场校准；'
    '仅完成接地与慢速PWM回环初验，精度及无干扰运行仍未验收。'
)


class DiagnosticWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, serial_number: str, parent=None, swd_hz: int | None = None):
        super().__init__(parent)
        self.serial_number = serial_number
        self.swd_hz = swd_hz
        self._stop = threading.Event()

    def request_stop(self):
        # 只能协作取消；底层单次 USB 读写不保证可中断。
        self._stop.set()

    def run(self):
        try:
            with PyOCDDiagnostic(self.serial_number, self.swd_hz) as transport:
                report = diagnose(transport, should_stop=self._stop.is_set)
            # 关闭异常也要报告失败，不能提前宣称干净断开。
            self.result.emit(report)
        except Exception as exc:
            self.failed.emit(f'{type(exc).__name__}: {exc}')


class RealWorker(QThread):
    """在后台持有真实控制器；命令串行执行，关闭时恢复并 emit closed。"""

    preflight_ready = Signal(object)
    activated = Signal(object)
    frame_ready = Signal(object)
    pwm_ready = Signal(object)
    pulse_ready = Signal(object)
    continuity_ready = Signal(object)
    failed = Signal(str)
    closed = Signal(object)
    status = Signal(str)

    def __init__(self, serial_number: str, profile_path, parent=None, swd_hz: int | None = None, selection=None, general_mode=False):
        super().__init__(parent)
        self.serial_number = serial_number
        self.profile_path = Path(profile_path) if profile_path is not None else None
        self.selection = selection
        self.general_mode = general_mode
        self.general_confirmed = False
        self.swd_hz = swd_hz
        self._commands: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._sample_token: CancelToken | None = None
        self._lifecycle_token = CancelToken()
        self._controller = None

    # -- GUI 线程调用 -----------------------------------------------------
    def request_preflight(self):
        self._commands.put(('preflight',))

    def request_activate(self):
        self._commands.put(('activate', self.general_confirmed))

    def request_sample(self, request: SampleRequest):
        token = CancelToken()
        self._sample_token = token
        self._commands.put(('sample', request, token))

    def request_pwm(self, frequency_hz: float, duty_percent: float):
        self._commands.put(('pwm', frequency_hz, duty_percent))

    def request_output_off(self):
        self._commands.put(('off',))

    def request_measure_pulse(self, timeout_s):
        token = CancelToken()
        self._sample_token = token
        self._commands.put(('pulse', timeout_s, token))

    def request_continuity(self):
        self._commands.put(('continuity',))

    def cancel_sampling(self):
        token = self._sample_token
        if token is not None:
            token.cancel()

    def request_stop(self):
        self._stop.set()
        self._lifecycle_token.cancel()
        token = self._sample_token
        if token is not None:
            token.cancel()

    # -- 工作线程 ---------------------------------------------------------
    def run(self):
        controller = None
        report = None
        try:
            self.status.emit('正在打开SWD会话并加载已审查profile；不会复位目标……')
            options = dict(swd_hz=self.swd_hz)
            if self.selection is not None or self.general_mode:
                options.update(selection=self.selection, general_mode=self.general_mode)
            controller = open_real_controller(self.serial_number, self.profile_path, **options)
            self._controller = controller
            if not self._stop.is_set():
                self.status.emit('会话已打开；正在执行只读预检……')
                self.preflight_ready.emit(controller.preflight(self._lifecycle_token))
            while not self._stop.is_set():
                try:
                    command = self._commands.get(timeout=0.05)
                except queue.Empty:
                    continue
                if self._stop.is_set():
                    break
                try:
                    self._dispatch(controller, command)
                except Exception as exc:
                    self.failed.emit(f'{type(exc).__name__}: {exc}')
        except Exception as exc:
            self.failed.emit(f'{type(exc).__name__}: {exc}')
        finally:
            self._controller = None
            try:
                if controller is not None:
                    self.status.emit('正在关闭SWD会话并尽力恢复本工具持有的字段……')
                    report = controller.close()
            except Exception as exc:
                self.failed.emit(f'关闭失败：{type(exc).__name__}: {exc}')
            finally:
                self.closed.emit(report)

    def _dispatch(self, controller, command):
        kind = command[0]
        if kind == 'preflight':
            self.preflight_ready.emit(controller.preflight(self._lifecycle_token))
        elif kind == 'activate':
            if self.general_mode and hasattr(controller, 'confirm_general_mode'):
                controller.confirm_general_mode(len(command) > 1 and command[1] is True)
            self.activated.emit(controller.activate(self._lifecycle_token))
        elif kind == 'sample':
            # 取消会由后端抛 CancelledError；未完成的帧绝不发布。
            self.frame_ready.emit(controller.sample(command[1], command[2]))
        elif kind == 'pwm':
            self.pwm_ready.emit(controller.set_pwm(command[1], command[2]))
        elif kind == 'off':
            self.pwm_ready.emit(controller.output_off())
        elif kind == 'pulse':
            self.pulse_ready.emit(controller.measure_pulse(command[1], command[2]))
        elif kind == 'continuity':
            self.continuity_ready.emit(controller.check_continuity())


INFO_HTML = f"""
<p><b>能做什么</b></p>
<table cellspacing="0" cellpadding="4">
<tr><td style="color:{MUTED}">电压表</td><td>ADC2 / PA4 或 PA7，按 profile 接线，仅限 0–3.3 V</td></tr>
<tr><td style="color:{MUTED}">采样曲线</td><td>主机经 SWD 逐点轮询，默认时钟（950 kHz）实测约 14 点/秒，时间戳非等间隔；不是高速示波器</td></tr>
<tr><td style="color:{MUTED}">信号发生器</td><td>TIM1_CH1 / PA8 逻辑电平方波与 PWM</td></tr>
</table>
<p>频率计 {{meter_pin}} 测量重复逻辑信号，范围 0.5 Hz–100 kHz。慢信号约需六个周期，应选择更长超时。
分辨率来自计时 tick，精度尚需独立校验。通断 {{continuity_pin}}→GND 通过内部上拉检测接地，无法测量电阻。</p>
<p>ADC 输入 {{adc_pin}} 由 profile 决定，仅限 0–3.3 V，不耐受 5 V。PA6 不耐受 5 V，仅限 0–3.3 V；HC-SR04 Echo 等 5 V 信号禁止直接连接。PB10 耐受 5 V，通断仅检测接地。
PWM 输出 PA8。“串口调试”页需手动刷新并打开串口，可收发文本与 HEX。</p>
<p>电流、电阻、正弦/三角与任意幅值输出需要额外前端电路或 DAC。</p>
<p><b>接线</b> SWDIO→PA13，SWCLK→PA14，GND→GND。电压输入按 profile 选择 PA4 或 PA7，禁止负压、高压、市电。
PA8 只能驱动高阻输入，回环前核对接线与 profile。</p>
<p><b>安全边界</b>：原工程与 Flash 不改动；真实写入只在已审查profile预检通过后由后端事务开放。
不提供任意地址读写或强制放行。attach 仍会配置调试访问，不保证零干扰；禁止同时使用其他调试器。</p>
<p style="color:{AMBER}"><b>限制</b>：{LIMITATIONS}</p>
"""


class WrappedLabel(QLabel):
    """Keep scroll-area layouts from shrinking wrapped text below its current height."""

    def _update_text_height(self):
        if self.wordWrap():
            height = max(0, self.heightForWidth(self.width()))
            if self.minimumHeight() != height:
                self.setMinimumHeight(height)

    def setText(self, text):
        super().setText(text)
        self._update_text_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_text_height()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_text_height()

    def changeEvent(self, event):
        super().changeEvent(event)
        self._update_text_height()


class InstrumentWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('SWD 实验仪器｜模拟三功能 · 真实ADC2/TIM1低速实验')
        self.resize(1360, 920)
        self.setMinimumSize(1120, 700)
        self.setFont(ui_font())
        self.setPalette(build_palette())
        self.setStyleSheet(STYLESHEET)
        self.simulator = Simulator()
        self.current_frame = None
        self.last_real_frame = None
        self.worker = None
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.capture)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(200)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._close_pending = False
        self._pending_commands = 0
        self._sampling = False
        self._accept_real_frame = False
        self._real_continuous = False
        self._preflight_ok = False
        self._preflight_failed = False
        self._real_activated = False
        self._last_command_kind = ''
        self._output_ui = 'idle'
        self._sample_started = 0.0
        self._estimate_s = 0.0
        self._persist_settings = False
        self._resources = {}
        self._ready = {}
        self._meter_continuous = False
        self._accept_pulse = False
        self._meter_state = '未激活'
        self._meter_timer = QTimer(self)
        self._meter_timer.setSingleShot(True)
        self._meter_timer.setInterval(500)
        self._meter_timer.timeout.connect(self._continue_meter)

        root = QWidget()
        root.setObjectName('root')
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(root)
        outer.addWidget(self._build_header())
        body = QHBoxLayout()
        body.setContentsMargins(14, 14, 14, 10)
        body.setSpacing(14)
        outer.addLayout(body, 1)
        main = self._build_main()
        body.addWidget(self._build_side())
        body.addWidget(main, 1)

        self.statusBar().showMessage('等待模拟操作；没有采集真实电压')
        self._update_estimate()
        self._refresh_real_buttons()

    # ------------------------------------------------------------------
    # 界面构建辅助
    @staticmethod
    def _label(text: str, role: str | None = None, wrap: bool = False) -> QLabel:
        label = WrappedLabel(text) if wrap else QLabel(text)
        if role:
            label.setProperty('role', role)
        label.setWordWrap(wrap)
        return label

    @staticmethod
    def _button(text: str, callback, variant: str | None = None, tip: str | None = None) -> QPushButton:
        button = QPushButton(text)
        if variant:
            button.setProperty('variant', variant)
        if tip:
            button.setToolTip(tip)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def _card(self, title: str, with_chip: bool = False):
        frame = QFrame()
        frame.setObjectName('card')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(9)
        head = QHBoxLayout()
        frame.title_label = self._label(title, 'cardTitle')
        head.addWidget(frame.title_label)
        head.addStretch()
        chip = None
        if with_chip:
            chip = QLabel('—')
            chip.setObjectName('chip')
            head.addWidget(chip)
        layout.addLayout(head)
        return frame, layout, chip

    @staticmethod
    def _form() -> QFormLayout:
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return form

    @staticmethod
    def _restyle(widget, name: str, value: str):
        if widget.property(name) == value:
            return
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _set_chip(self, chip: QLabel, text: str, state: str):
        chip.setText(text)
        self._restyle(chip, 'state', state)

    def _set_source(self, text: str, kind: str):
        """kind 只决定徽标颜色：idle/sim/real/history/warn；文字仍是来源事实。"""
        self.source.setText(text)
        self._restyle(self.source, 'kind', kind)

    # ------------------------------------------------------------------
    # 顶栏
    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName('header')
        header.setFixedHeight(64)
        row = QHBoxLayout(header)
        row.setContentsMargins(18, 0, 18, 0)
        row.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(1)
        titles.addStretch()
        titles.addWidget(self._label('SWD 实验仪器', 'appTitle'))
        titles.addWidget(self._label('低压电压表 · 低速采样曲线 · PWM 方波｜原工程与 Flash 不改动', 'muted'))
        titles.addStretch()
        row.addLayout(titles)
        row.addStretch()
        self.source = QLabel('未采集｜未连接任何实物；可先运行模拟')
        self.source.setObjectName('badge')
        self.source.setProperty('kind', 'idle')
        self.source.setToolTip('当前显示数据的来源。模拟数据与真实测量始终分开标注。')
        row.addWidget(self.source, 0, Qt.AlignVCenter)
        row.addWidget(self._button('导出当前帧 CSV', self.save_csv,
                                   tip='保存当前显示的这一帧：时间戳、ADC码、电压、来源和参考电压'),
                      0, Qt.AlignVCenter)
        return header

    # ------------------------------------------------------------------
    # 左侧：模拟 / 真实 两套操作面板
    def _build_side(self) -> QWidget:
        self.mode_tabs = QTabWidget()
        self.mode_tabs.setFixedWidth(400)
        self.mode_tabs.addTab(self._build_sim_page(), '模拟（离线）')
        self.mode_tabs.addTab(self._build_real_tab(), '真实 SWD（接板）')
        return self.mode_tabs

    def _build_sim_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        card, box, _ = self._card('模拟输入')
        form = self._form()
        self.scene = QComboBox()
        self.scene.addItems(['正弦', '直流', 'PWM回环'])
        self.scene.setMinimumHeight(30)
        self.vref = QSpinBox()
        self.vref.setRange(2500, 3600)
        self.vref.setValue(3300)
        self.vref.setSuffix(' mV')
        self.vref.setMinimumHeight(30)
        form.addRow('信号场景', self.scene)
        form.addRow('参考电压', self.vref)
        box.addLayout(form)
        grid = QGridLayout()
        grid.setSpacing(8)
        grid.addWidget(self._button('▶  运行模拟', self.start, 'primary',
                                    '每 0.2 秒刷新一帧模拟数据'), 0, 0)
        grid.addWidget(self._button('暂停', self.pause,
                                    tip='冻结当前帧；读数、曲线和导出保持同一帧'), 0, 1)
        grid.addWidget(self._button('单次', self.single, tip='只采一帧模拟数据并冻结'), 1, 0)
        grid.addWidget(self._button('断开模拟', self.disconnect,
                                    tip='停止模拟并关闭模拟PWM；不影响实物'), 1, 1)
        box.addLayout(grid)
        box.addWidget(self._label('模拟数据不来自开发板，读数和导出文件都会标注“模拟数据”。', 'muted', True))
        layout.addWidget(card)

        card, box, _ = self._card('模拟信号发生器')
        form = self._form()
        self.frequency = QDoubleSpinBox()
        self.frequency.setDecimals(3)
        self.frequency.setRange(0.1, 100_000)
        self.frequency.setValue(100)
        self.frequency.setSuffix(' Hz')
        self.frequency.setMinimumHeight(30)
        self.duty = QDoubleSpinBox()
        self.duty.setRange(0, 100)
        self.duty.setValue(50)
        self.duty.setSuffix(' %')
        self.duty.setMinimumHeight(30)
        form.addRow('请求频率', self.frequency)
        form.addRow('请求占空比', self.duty)
        box.addLayout(form)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._button('开启 / 更新模拟PWM', self.enable_sim_output, 'primary',
                                   '把模拟PWM回环到模拟输入；只更新模拟器'), 2)
        row.addWidget(self._button('关闭', self.disable_sim_output, tip='关闭模拟PWM；不影响实物输出'), 1)
        box.addLayout(row)
        self.output_status = self._label('模拟输出：关闭；实物输出状态请查看“真实 SWD”页', 'status', True)
        box.addWidget(self.output_status)
        box.addWidget(self._label(
            '模拟定时器假定72MHz，逻辑幅值0～模拟参考电压；不是实测时钟/幅值。'
            '约1ms的模拟采样间隔下，高频PWM可能混叠，不能从图形推断真实输出频率。'
            '正弦/三角输出、可调模拟幅值需要滤波或DAC等额外电路。', 'muted', True))
        layout.addWidget(card)
        layout.addStretch()
        return page

    def _build_real_tab(self) -> QWidget:
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(12, 12, 8, 12)
        layout.setSpacing(12)

        card, box, self.step_preflight = self._card('① 连接 · 预检 · 激活', True)
        self.step_activate = QLabel('—')
        self.step_activate.setObjectName('chip')
        box.itemAt(0).layout().addWidget(self.step_activate)
        self.serial = QLineEdit()
        self.serial.setPlaceholderText('输入ST-Link完整序列号；不自动选择设备')
        self.serial.setClearButtonEnabled(True)
        self.serial.setMinimumHeight(30)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumHeight(30)
        box.addWidget(self._label('ST-Link 探头完整序列号', 'muted'))
        box.addWidget(self.serial)
        box.addWidget(self._label('板上运行的工程', 'muted'))
        box.addWidget(self.profile_combo)
        self.profile_hint = self._label('', 'muted', True)
        box.addWidget(self.profile_hint)

        # 注意事项默认折叠成一行，避免一屏橙色文字；profile_notes 本身保持可见状态，
        # 由外框控制展开。
        self.notes_toggle = QPushButton('注意事项')
        self.notes_toggle.setObjectName('linkButton')
        self.notes_toggle.setCheckable(True)
        self.notes_toggle.setCursor(Qt.PointingHandCursor)
        self.notes_toggle.toggled.connect(self._toggle_notes)
        self.notes_toggle.hide()
        box.addWidget(self.notes_toggle, 0, Qt.AlignLeft)
        self.notes_box = QFrame()
        self.notes_box.setObjectName('notesBox')
        notes_layout = QVBoxLayout(self.notes_box)
        notes_layout.setContentsMargins(10, 8, 10, 8)
        self.profile_notes = self._label('', 'caution', True)
        self.profile_notes.hide()
        notes_layout.addWidget(self.profile_notes)
        self.notes_box.hide()
        box.addWidget(self.notes_box)

        # 引脚分配：卡片里只显示一行摘要，完整设置在对话框中。
        pin_frame = QFrame()
        pin_frame.setObjectName('pinSummaryBox')
        pin_row = QHBoxLayout(pin_frame)
        pin_row.setContentsMargins(10, 8, 8, 8)
        pin_row.setSpacing(8)
        self.pin_summary = self._label('', 'pinSummary', True)
        self.pin_button = self._button('引脚设置…', self.open_pin_dialog,
                                       tip='选择各功能使用的引脚，并标记面包板上已被其他器件占用的引脚')
        pin_row.addWidget(self.pin_summary, 1)
        pin_row.addWidget(self.pin_button, 0, Qt.AlignVCenter)
        box.addWidget(pin_frame)
        self._build_pin_dialog()

        clock_row = QHBoxLayout()
        clock_row.setSpacing(10)
        self.swd_clock = QComboBox()
        self.swd_clock.setMinimumHeight(30)
        for hz, text, _ in SWD_CLOCK_CHOICES:
            self.swd_clock.addItem(text, hz)
        self.swd_clock.setToolTip(
            '只能选ST-Link支持的固定档位。提高时钟不减少任何保护检查，只缩短每次SWD访问的时间。'
            '如果预检或采样出现通信错误，先断开，换成更低的档位再试。会话打开后不能更改。')
        self.swd_clock.currentIndexChanged.connect(self._update_estimate)
        clock_row.addWidget(self._label('SWD 时钟', 'muted'))
        clock_row.addWidget(self.swd_clock, 1)
        box.addLayout(clock_row)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.preflight_button = self._button(
            '只读预检', self.run_preflight, 'primary',
            '打开SWD会话，只读核对固件摘要、时钟和空闲资源；不写外设、不复位')
        self.activate_button = self._button(
            '激活可用功能', self.activate_real,
            tip='预检全部通过后才可用。只写白名单内的字段并读回校验；激活后输出仍保持关闭')
        row.addWidget(self.preflight_button, 1)
        row.addWidget(self.activate_button, 1)
        box.addLayout(row)
        layout.addWidget(card)

        card, box, self.step_sample = self._card('② 采样 · ADC 输入', True)
        self.sample_card = card
        self.adc_pin_hint = self._label('PA4 或 PA7，见预检日志；仅限 0–3.3 V', 'muted', True)
        self.sample_reason = self._label('', 'caution', True)
        box.addWidget(self.adc_pin_hint)
        box.addWidget(self.sample_reason)
        form = self._form()
        self.sample_points = QSpinBox()
        self.sample_points.setRange(1, 4096)
        self.sample_points.setValue(64)
        self.sample_points.setSuffix(' 点')
        self.sample_points.setMinimumHeight(30)
        self.sample_points.setToolTip('每帧采样点数，1～4096')
        self.sample_interval = QDoubleSpinBox()
        self.sample_interval.setDecimals(3)
        self.sample_interval.setRange(0.0, 1.0)
        self.sample_interval.setSingleStep(0.001)
        self.sample_interval.setValue(0.01)
        self.sample_interval.setSuffix(' s')
        self.sample_interval.setMinimumHeight(30)
        self.sample_interval.setToolTip('请求的最小点间隔，0～1秒；实际间隔受SWD通信限制，以数据时间戳为准')
        form.addRow('每帧点数', self.sample_points)
        form.addRow('请求间隔', self.sample_interval)
        box.addLayout(form)
        self.sample_estimate = self._label('', 'muted', True)
        box.addWidget(self.sample_estimate)
        self.sample_points.valueChanged.connect(self._update_estimate)
        self.sample_interval.valueChanged.connect(self._update_estimate)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.sample_button = self._button('单次采样', self.single_real_sampling, 'primary',
                                          '采一帧真实数据后停止')
        self.real_run_button = self._button('连续采样', self.start_real_continuous,
                                            tip='一帧接一帧采样，直到取消')
        self.cancel_button = self._button('取消', self.cancel_real_sampling,
                                          tip='协作取消；未完成的帧不显示。不会关闭PWM')
        self.cancel_button.setEnabled(False)
        row.addWidget(self.sample_button, 2)
        row.addWidget(self.real_run_button, 2)
        row.addWidget(self.cancel_button, 1)
        box.addLayout(row)
        self.sample_progress = QProgressBar()
        self.sample_progress.setTextVisible(False)
        self.sample_progress.setRange(0, 1)
        self.sample_progress.setValue(0)
        box.addWidget(self.sample_progress)
        layout.addWidget(card)

        card, box, self.step_output = self._card('③ PWM 输出 · PA8', True)
        self.pwm_card = card
        self.pwm_reason = self._label('', 'caution', True)
        box.addWidget(self.pwm_reason)
        form = self._form()
        self.real_frequency = QDoubleSpinBox()
        self.real_frequency.setDecimals(3)
        self.real_frequency.setRange(0.1, 100_000)
        self.real_frequency.setValue(1)
        self.real_frequency.setSuffix(' Hz')
        self.real_frequency.setMinimumHeight(30)
        self.real_frequency.setToolTip(
            '0.1～100000 Hz。回环到PA4观察时频率要远低于采样速率（默认时钟约14点/秒），建议1Hz或更慢')
        self.real_duty = QDoubleSpinBox()
        self.real_duty.setRange(0, 100)
        self.real_duty.setValue(50)
        self.real_duty.setSuffix(' %')
        self.real_duty.setMinimumHeight(30)
        form.addRow('频率', self.real_frequency)
        form.addRow('占空比', self.real_duty)
        box.addLayout(form)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.real_pwm_button = self._button('开启 / 更新输出', self.set_real_pwm, 'primary',
                                            'PA8 输出3.3V逻辑方波；只能驱动高阻输入')
        self.real_off_button = self._button('关闭输出', self.real_output_off,
                                            tip='只有读回确认后才显示已关闭')
        row.addWidget(self.real_pwm_button, 2)
        row.addWidget(self.real_off_button, 1)
        box.addLayout(row)
        self.real_pwm_status = self._label('真实输出：未激活；未读回确认前不显示已关闭', 'status', True)
        box.addWidget(self.real_pwm_status)
        layout.addWidget(card)
        layout.addWidget(self._build_meter_card())
        layout.addWidget(self._build_continuity_card())
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)

        # “断开并恢复”固定在面板底部，滚动到任何位置都能点到。
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        page_layout.addWidget(scroll, 1)
        footer = QFrame()
        footer.setObjectName('footer')
        foot = QVBoxLayout(footer)
        foot.setContentsMargins(12, 10, 12, 12)
        foot.setSpacing(6)
        self.recover_button = self._button(
            '⑥ 断开并恢复', self.recover_real, 'danger',
            '先关闭输出，再断开。协作取消当前操作，只写回本工具持有的字段并关闭SWD会话；不复位目标')
        foot.addWidget(self.recover_button)
        foot.addWidget(self._label(
            '精度、带宽与无干扰运行尚未验收；断线后PWM可能继续输出。完整限制见右下“说明与限制”。',
            'caution', True))
        page_layout.addWidget(footer)
        self._load_profiles()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        return page

    # ------------------------------------------------------------------
    # 引脚设置对话框
    def _build_pin_dialog(self):
        dialog = QDialog(self)
        dialog.setObjectName('pinDialog')
        dialog.setWindowTitle('引脚设置')
        dialog.setMinimumWidth(660)
        self.pin_dialog = dialog
        outer = QVBoxLayout(dialog)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)
        outer.addWidget(self._label('引脚设置', 'appTitle'))
        outer.addWidget(self._label(
            '下拉框只列出芯片支持、当前工程未占用、且没有标记为物理占用的引脚。'
            '连接后的只读预检还会确认它们此刻空闲；同一个引脚或定时器只能给一个功能用。',
            'muted', True))

        self.pin_group = QGroupBox()
        self.pin_group.setObjectName('pinGroup')
        group = QVBoxLayout(self.pin_group)
        group.setContentsMargins(0, 0, 0, 0)
        group.setSpacing(10)
        self.pin_contents = QFrame()
        self.pin_contents.setObjectName('card')
        pin_form = QFormLayout(self.pin_contents)
        pin_form.setContentsMargins(16, 14, 16, 14)
        pin_form.setHorizontalSpacing(16)
        pin_form.setVerticalSpacing(10)
        pin_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.pin_combos = {}
        for key, title in (('adc', '电压采样'), ('pwm', 'PWM 输出'),
                           ('meter', '频率计'), ('continuity', '通断')):
            combo = QComboBox()
            combo.setMinimumHeight(32)
            combo.setMinimumWidth(420)
            self.pin_combos[key] = combo
            pin_form.addRow(self._label(title, 'formLabel'), combo)
            combo.currentIndexChanged.connect(self._pin_selection_changed)
        group.addWidget(self.pin_contents)

        occupied = QFrame()
        occupied.setObjectName('card')
        occ = QVBoxLayout(occupied)
        occ.setContentsMargins(16, 14, 16, 14)
        occ.setSpacing(8)
        occ.addWidget(self._label('物理占用引脚', 'cardTitle'))
        occ.addWidget(self._label(
            '勾选面包板上已经接了其他器件的引脚，例如 OLED 模块占用 PB6–PB9。'
            '勾选后它们不会出现在上面的选项里。鼠标悬停可查看是否耐 5 V。', 'muted', True))
        self.occupied_list = QListWidget()
        self.occupied_list.setObjectName('occupiedGrid')
        self.occupied_list.setViewMode(QListView.ListMode)
        self.occupied_list.setFlow(QListView.LeftToRight)
        self.occupied_list.setWrapping(True)
        self.occupied_list.setResizeMode(QListView.Adjust)
        self.occupied_list.setUniformItemSizes(True)
        self.occupied_list.setGridSize(QSize(86, 32))
        self.occupied_list.setSelectionMode(QListWidget.NoSelection)
        self.occupied_list.setFocusPolicy(Qt.NoFocus)
        self.occupied_list.setMinimumHeight(4 * 32 + 14)
        for p in CONTINUITY:
            item = QListWidgetItem(p.pin)
            item.setToolTip(p.label)
            item.setData(Qt.UserRole, p.pin)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if p.pin in DEFAULT_OCCUPIED else Qt.Unchecked)
            self.occupied_list.addItem(item)
        self.occupied_list.itemChanged.connect(self._occupied_changed)
        occ.addWidget(self.occupied_list)
        group.addWidget(occupied)
        outer.addWidget(self.pin_group)

        self.pin_lock_hint = self._label('会话进行中，引脚不能修改；点“断开并恢复”后再调整。', 'caution', True)
        self.pin_lock_hint.hide()
        outer.addWidget(self.pin_lock_hint)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self._button('完成', dialog.accept, 'primary'))
        outer.addLayout(buttons)

    def open_pin_dialog(self):
        self.pin_lock_hint.setVisible(not self.pin_group.isEnabled())
        self.pin_dialog.open()

    @staticmethod
    def _pin_name(key):
        """'TIM3_CH1/PA6' -> 'PA6'；None -> '不启用'。"""
        if not key:
            return '不启用'
        return key.split('/')[-1]

    @staticmethod
    def _pin_detail(key):
        if not key:
            return '不启用'
        if '/' in key:
            timer, pin = key.split('/')
            return f'{pin}（{timer}）'
        return key

    def _update_pin_summary(self):
        if not hasattr(self, 'pin_summary'):
            return
        names = {k: self._pin_name(cb.currentData()) for k, cb in self.pin_combos.items()}
        self.pin_summary.setText(
            f'电压 <b>{names["adc"]}</b>　PWM <b>{names["pwm"]}</b><br>'
            f'频率计 <b>{names["meter"]}</b>　通断 <b>{names["continuity"]}</b>')
        if hasattr(self, 'strip_meter_pin'):
            self.strip_meter_pin.setText(self._pin_detail(self.pin_combos['meter'].currentData()))
            continuity = self.pin_combos['continuity'].currentData()
            self.strip_cont_pin.setText(f'{continuity}→GND' if continuity else '不启用')

    def _toggle_notes(self, checked):
        self.notes_box.setVisible(bool(checked and self.profile_notes.text()))
        self._sync_notes()

    def _sync_notes(self):
        text = self.profile_notes.text() if not self.profile_notes.isHidden() else ''
        count = len([line for line in text.splitlines() if line.strip()])
        self.notes_toggle.setVisible(bool(count))
        if not count:
            self.notes_box.hide()
            return
        expanded = self.notes_toggle.isChecked()
        self.notes_toggle.setText(f'⚠ 注意事项（{count} 条）' + ('  收起 ▴' if expanded else '  展开 ▾'))
        self.notes_box.setVisible(expanded)

    # ------------------------------------------------------------------
    # 右侧：读数 + 曲线 + 日志同屏
    def _build_meter_card(self):
        card, box, self.step_meter = self._card('④ 脉宽 / 频率计 · 待选择', True)
        self.meter_card = card
        self.meter_title = card.title_label
        self.meter_pin_hint = self._label('', 'caution', True)
        box.addWidget(self.meter_pin_hint)
        self.meter_reason = self._label('', 'caution', True)
        box.addWidget(self.meter_reason)
        self.meter_timeout = QComboBox()
        for label, seconds in [('快速 1.5 s', 1.5), ('慢信号 10 s', 10.0), ('很慢 30 s', 30.0)]:
            self.meter_timeout.addItem(label, seconds)
        box.addWidget(self.meter_timeout)
        row = QHBoxLayout()
        self.meter_button = self._button('测量一次', self.measure_pulse, 'primary')
        self.meter_run_button = self._button('连续测量', self.toggle_meter)
        self.meter_run_button.setCheckable(True)
        row.addWidget(self.meter_button)
        row.addWidget(self.meter_run_button)
        box.addLayout(row)
        self.meter_high = self._label('高电平时间 —', 'cardTitle', True)
        self.meter_high.setObjectName('pulseHigh')
        self.meter_result = self._label('尚无测量结果', 'status', True)
        box.addWidget(self.meter_high)
        box.addWidget(self.meter_result)
        box.addWidget(self._label('重复逻辑信号 0.5 Hz–100 kHz；慢信号约需六个周期。分辨率由计时 tick 决定，精度待校验。', 'muted', True))
        return card

    def _build_continuity_card(self):
        card, box, self.step_continuity = self._card('⑤ 通断 · 待选择', True)
        self.continuity_card = card
        self.continuity_title = card.title_label
        self.continuity_pin_hint = self._label('', 'muted', True)
        box.addWidget(self.continuity_pin_hint)
        self.continuity_reason = self._label('', 'caution', True)
        box.addWidget(self.continuity_reason)
        self.continuity_button = self._button('检查通断', self.check_continuity, 'primary')
        self.continuity_result = self._label('不确定 · 尚未检查', 'cardTitle', True)
        box.addWidget(self.continuity_button)
        box.addWidget(self.continuity_result)
        box.addWidget(self._label('仅通过内部上拉检测与 GND 的连接，无法测量电阻。', 'caution', True))
        return card

    def _function_ready(self, key):
        return bool(self._preflight_ok and self._real_activated and self._resources.get(key, False)
                    and self._ready.get(key, False) and isinstance(self.worker, RealWorker)
                    and self.worker.isRunning() and not self.worker._stop.is_set())

    def _stop_meter(self):
        self._meter_continuous = False
        self._accept_pulse = False
        self._meter_timer.stop()
        self.meter_run_button.setChecked(False)
        self.meter_run_button.setText('连续测量')
        if self._meter_state == '测量中':
            self._meter_state = '就绪'
        if isinstance(self.worker, RealWorker) and self._last_command_kind == 'pulse':
            self.worker.cancel_sampling()

    def toggle_meter(self):
        if self._meter_continuous:
            self._stop_meter()
            self._refresh_real_buttons()
            return
        self._meter_continuous = True
        self.meter_run_button.setChecked(True)
        self.meter_run_button.setText('停止测量')
        self.measure_pulse()

    def _continue_meter(self):
        if self._meter_continuous and not self._close_pending:
            self.measure_pulse()

    def measure_pulse(self):
        if not self._function_ready('meter'):
            self._stop_meter()
            return
        if self._pending_commands:
            if self._meter_continuous:
                self._meter_timer.start()
            return
        self._accept_pulse = True
        self._pending_commands += 1
        self._last_command_kind = 'pulse'
        self._meter_state = '测量中'
        self._refresh_real_buttons()
        self.worker.request_measure_pulse(float(self.meter_timeout.currentData()))

    @staticmethod
    def _pulse_time(seconds):
        if seconds >= 1:
            return f'{seconds:.6g} s'
        if seconds >= 0.001:
            return f'{seconds * 1000:.6g} ms'
        if seconds >= 1e-6:
            return f'{seconds * 1_000_000:.6g} µs'
        return f'{seconds * 1_000_000_000:.3g} ns'

    def _on_pulse(self, result):
        # 即使界面已不再接收这次结果（例如刚停止连续测量），也必须先看会话是否还在：
        # 驱动出错恢复后会撤销全部接管，界面不能继续显示“已激活”。
        if getattr(result, 'session_active', True) is False:
            self._mark_session_lost('频率计测量出错，驱动已撤销本次会话的全部接管')
        if not self._accept_pulse or self._close_pending or result.state == 'cancelled':
            self._accept_pulse = False
            if result.state == 'cancelled' and self._meter_state == '测量中':
                self._meter_state = '就绪'
            self._ack_command()
            return
        self._accept_pulse = False
        states = {'measured': '已测得', 'no-signal': '无信号', 'out-of-range': '超出范围',
                  'not-ready': '不可用', 'error': '错误'}
        self._meter_state = states.get(result.state, '错误')
        values = (result.frequency_hz, result.period_s, result.high_s, result.duty_percent, result.tick_s)
        measured = result.ok and result.state == 'measured' and all(value is not None for value in values)
        if measured:
            hz = result.frequency_hz
            frequency = f'{hz / 1000:.6g} kHz' if hz >= 1000 else f'{hz:.6g} Hz'
            text = (f'已测得 · 频率 {frequency}\n周期 {self._pulse_time(result.period_s)}'
                    f'\n高电平 {self._pulse_time(result.high_s)} · 占空比 {result.duty_percent:.6g}%'
                    f'\n计时分辨率 {self._pulse_time(result.tick_s)}（精度待校验）')
            self.meter_high.setText(f'高电平 {result.high_s * 1000:.6g} ms')
        else:
            text = self._meter_state
            self.meter_high.setText('高电平时间 —')
        self.meter_result.setText(text)
        self.meter_result.setToolTip(result.message)
        self._update_strip_pulse(result, measured)
        timeout = float(self.meter_timeout.currentData() or 0)
        if result.state == 'no-signal' and timeout < 5:
            # 快速档只等 1.5 s，低于约 4 Hz 的信号凑不够周期，必然显示无信号。
            hint = '没测到边沿。低于约 4 Hz 的信号请把超时改成“慢信号 10 s”或“很慢 30 s”'
            self.strip_meter_state.setText(hint)
            self.strip_meter_state.setToolTip(hint)
            self.statusBar().showMessage(hint)
        description = self._result_description(self._meter_state, result.message)
        log = f'{self.meter_title.text()}：{description}'
        if measured:
            log += '\n' + text.split(' · ', 1)[1]
        self.real_text.appendPlainText(log)
        if result.state in ('error', 'not-ready') or (not result.ok and result.state != 'no-signal'):
            self._stop_meter()
        self._ack_command()
        if self._meter_continuous:
            self._meter_timer.start()

    def check_continuity(self):
        if not self._function_ready('continuity') or self._pending_commands:
            return
        self._pending_commands += 1
        self._last_command_kind = 'continuity'
        self._refresh_real_buttons()
        self.worker.request_continuity()

    def _on_continuity(self, result):
        if not self._close_pending and self._function_ready('continuity'):
            text = '不确定'
            if result.ok and result.closed is not None:
                text = '导通' if result.closed else '开路'
            self.continuity_result.setText(text)
            self.continuity_result.setToolTip(result.message)
            self.strip_cont.setText(text)
            self.strip_cont.setToolTip(result.message)
            self._restyle(self.strip_cont, 'state',
                          {'导通': 'closed', '开路': 'open'}.get(text, 'unknown'))
            self.real_text.appendPlainText(f'{self.continuity_title.text()}：{self._result_description(text, result.message)}')
        self._ack_command()

    @staticmethod
    def _result_description(state, message):
        message = ' '.join(message.split())
        if not message:
            return state
        if message.startswith(state):
            return message
        return f'{state}（{message}）'

    def _build_main(self) -> QWidget:
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(12)
        # 显式最小高度：窗口缩到最小尺寸时，分隔条不能把读数卡片和曲线压到被裁切。
        reading = self._build_reading_card()
        reading.setMinimumHeight(148)
        strip = self._build_instrument_strip()
        strip.setMinimumHeight(96)
        plot = self._build_plot_card()
        plot.setMinimumHeight(170)
        top_layout.addWidget(reading)
        top_layout.addWidget(strip)
        top_layout.addWidget(plot, 1)
        splitter.addWidget(top)
        bottom = self._build_bottom_tabs()
        bottom.setMinimumHeight(120)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([620, 190])
        return splitter

    def _build_reading_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName('card')
        row = QHBoxLayout(card)
        row.setContentsMargins(20, 14, 16, 14)
        row.setSpacing(18)
        left = QVBoxLayout()
        left.setSpacing(2)
        left.addWidget(self._label('电压表 · 当前帧平均值', 'cardTitle'))
        self.reading = QLabel('— V')
        self.reading.setObjectName('reading')
        left.addWidget(self.reading)
        self.statistics = self._label('尚无数据：在左侧点击“运行模拟”或“单次”即可试用', 'muted')
        left.addWidget(self.statistics)
        left.addWidget(self._label(
            '输入范围 0–3.3 V，禁止接负压或 5 V；数据来源见右上角标签。', 'muted', True))
        row.addLayout(left, 1)

        tiles = QGridLayout()
        tiles.setSpacing(8)
        self.stat_min = self._stat_tile(tiles, 0, 0, '最小')
        self.stat_max = self._stat_tile(tiles, 0, 1, '最大')
        self.stat_peak = self._stat_tile(tiles, 1, 0, '峰峰值')
        self.stat_span = self._stat_tile(tiles, 1, 1, '帧跨度')
        row.addLayout(tiles)
        return card

    def _build_instrument_strip(self) -> QWidget:
        """电压表下方的频率计 + 通断读数栏，让三种仪表的结果同屏可见。"""
        strip = QWidget()
        row = QHBoxLayout(strip)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        meter = QFrame()
        meter.setObjectName('card')
        mrow = QHBoxLayout(meter)
        mrow.setContentsMargins(18, 12, 16, 12)
        mrow.setSpacing(16)
        head = QVBoxLayout()
        head.setSpacing(2)
        title = QHBoxLayout()
        title.setSpacing(8)
        title.addWidget(self._label('频率计', 'cardTitle'))
        self.strip_meter_pin = self._label('—', 'muted')
        title.addWidget(self.strip_meter_pin)
        title.addStretch()
        head.addLayout(title)
        self.strip_freq = QLabel('— Hz')
        self.strip_freq.setObjectName('stripValue')
        self.strip_freq.setProperty('kind', 'none')
        head.addWidget(self.strip_freq)
        self.strip_meter_state = self._label('尚未测量', 'muted')
        head.addWidget(self.strip_meter_state)
        mrow.addLayout(head, 1)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.strip_period = self._mini_tile(tiles, '周期')
        self.strip_high = self._mini_tile(tiles, '高电平')
        self.strip_duty = self._mini_tile(tiles, '占空比')
        mrow.addLayout(tiles)
        row.addWidget(meter, 3)

        cont = QFrame()
        cont.setObjectName('card')
        crow = QVBoxLayout(cont)
        crow.setContentsMargins(18, 12, 18, 12)
        crow.setSpacing(6)
        ctitle = QHBoxLayout()
        ctitle.setSpacing(8)
        ctitle.addWidget(self._label('通断', 'cardTitle'))
        self.strip_cont_pin = self._label('—', 'muted')
        ctitle.addWidget(self.strip_cont_pin)
        ctitle.addStretch()
        crow.addLayout(ctitle)
        self.strip_cont = QLabel('—')
        self.strip_cont.setObjectName('contPill')
        self.strip_cont.setProperty('state', 'idle')
        self.strip_cont.setAlignment(Qt.AlignCenter)
        crow.addWidget(self.strip_cont)
        crow.addStretch()
        row.addWidget(cont, 1)
        return strip

    def _mini_tile(self, layout: QHBoxLayout, caption: str) -> QLabel:
        tile = QFrame()
        tile.setObjectName('tile')
        tile.setFixedWidth(118)
        box = QVBoxLayout(tile)
        box.setContentsMargins(10, 7, 10, 7)
        box.setSpacing(1)
        box.addWidget(self._label(caption, 'muted'))
        value = QLabel('—')
        value.setObjectName('miniValue')
        box.addWidget(value)
        layout.addWidget(tile)
        return value

    def _update_strip_pulse(self, result, measured: bool):
        if measured:
            hz = result.frequency_hz
            self.strip_freq.setText(f'{hz / 1000:.4f} kHz' if hz >= 1000 else f'{hz:.6g} Hz')
            self.strip_period.setText(self._pulse_time(result.period_s))
            self.strip_high.setText(self._pulse_time(result.high_s))
            self.strip_duty.setText(f'{result.duty_percent:.2f}%')
            self.strip_meter_state.setText(f'分辨率 {self._pulse_time(result.tick_s)} · 硬件捕获')
            self._restyle(self.strip_freq, 'kind', 'real')
        else:
            self.strip_freq.setText(self._meter_state)
            for label in (self.strip_period, self.strip_high, self.strip_duty):
                label.setText('—')
            self.strip_meter_state.setText(' '.join(result.message.split())[:60] or '—')
            self._restyle(self.strip_freq, 'kind', 'none')

    def _stat_tile(self, grid: QGridLayout, r: int, c: int, caption: str) -> QLabel:
        tile = QFrame()
        tile.setObjectName('tile')
        tile.setFixedWidth(158)
        box = QVBoxLayout(tile)
        box.setContentsMargins(12, 8, 12, 8)
        box.setSpacing(1)
        box.addWidget(self._label(caption, 'muted'))
        value = QLabel('—')
        value.setObjectName('statValue')
        box.addWidget(value)
        grid.addWidget(tile, r, c)
        return value

    def _build_plot_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName('card')
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self._label('采样曲线', 'cardTitle'))
        head.addWidget(self._label('模拟时钟或主机轮询时间戳；不是已验证的高速示波器。'
                                   '真实帧的圆点是实际采样，连线仅帮助观察。', 'muted', True), 1)
        # 连接绑定方法而不是捕获 self 的 lambda：避免窗口与按钮形成引用环，
        # 否则窗口要等到循环垃圾回收才销毁，进程退出时会偶发崩溃。
        head.addWidget(self._button('复位视图', self.reset_view,
                                    tip='滚轮缩放、拖动平移；点此恢复自动范围'))
        box.addLayout(head)
        self.plot = pg.PlotWidget()
        self.plot.setBackground(INSET)
        self.plot.setFrameShape(QFrame.NoFrame)
        label_style = {'color': '#a9b4c2', 'font-size': '10pt'}
        self.plot.setLabel('bottom', '帧内时间', units='s', **label_style)
        self.plot.setLabel('left', '电压', units='V', **label_style)
        for name in ('bottom', 'left'):
            axis = self.plot.getAxis(name)
            axis.setPen(pg.mkPen('#3a4554'))
            axis.setTextPen(pg.mkPen('#a9b4c2'))
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.curve = self.plot.plot(pen=pg.mkPen(SIM, width=2))
        box.addWidget(self.plot, 1)
        return card

    def _build_bottom_tabs(self) -> QWidget:
        self.tabs = QTabWidget()

        log = QWidget()
        log_layout = QVBoxLayout(log)
        log_layout.setContentsMargins(10, 10, 10, 10)
        self.real_text = QPlainTextEdit()
        self.real_text.setReadOnly(True)
        self.real_text.setPlainText('尚未打开真实SWD会话。没有真实测量或输出。')
        log_layout.addWidget(self.real_text)
        self.tabs.addTab(log, '实验日志')

        diagnostic = QWidget()
        diag_layout = QVBoxLayout(diagnostic)
        diag_layout.setContentsMargins(10, 10, 10, 10)
        diag_layout.setSpacing(8)
        row = QHBoxLayout()
        row.addWidget(self._label(
            '只读取寄存器，不是测量。attach仍会配置调试访问，不保证零干扰；禁止同时使用其他调试器。'
            '序列号使用左侧“真实 SWD”页的输入框。', 'muted', True), 1)
        self.diag_button = self._button('执行一次SWD只读诊断', self.start_diagnostic)
        row.addWidget(self.diag_button)
        diag_layout.addLayout(row)
        self.diagnostic_text = QPlainTextEdit()
        self.diagnostic_text.setReadOnly(True)
        self.diagnostic_text.setPlainText('尚未连接。真实写入仅在已审查profile预检通过后开放；Flash始终只读。')
        diag_layout.addWidget(self.diagnostic_text, 1)
        self.tabs.addTab(diagnostic, 'ST-Link 只读诊断')

        self.serial_terminal = SerialTerminalWidget()
        self.tabs.addTab(self.serial_terminal, '串口调试')

        info = QLabel(INFO_HTML.replace('{meter_pin}', '待选择').replace('{continuity_pin}', '待选择').replace('{adc_pin}', '待选择'))
        self.info_label = info
        info.setTextFormat(Qt.RichText)
        info.setWordWrap(True)
        info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        info.setContentsMargins(12, 6, 12, 10)
        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        info_scroll.setWidget(info)
        self.tabs.addTab(info_scroll, '说明与限制')
        return self.tabs

    def reset_view(self):
        self.plot.enableAutoRange()

    def _on_diagnostic_result(self, report):
        self.diagnostic_text.setPlainText(
            '诊断结果（不作为真实测量数据）：\n' + json.dumps(report, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------
    # 便利功能：估算时长、步骤状态、记住序列号
    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds < 90:
            return f'{seconds:.0f} 秒'
        return f'{seconds / 60:.1f} 分钟'

    def _update_estimate(self, *_):
        count = self.sample_points.value()
        observed = SWD_CLOCK_CHOICES[max(self.swd_clock.currentIndex(), 0)][2]
        per_point = max(float(self.sample_interval.value()), observed)
        self._estimate_s = count * per_point
        self.sample_estimate.setText(
            f'每帧约 {self._format_duration(self._estimate_s)}（按该时钟档实测约 '
            f'{observed:g} 秒/点估算）')
        self.sample_estimate.setToolTip(
            'SWD逐点轮询的平均点间隔来自2026-11-10实物记录，仅作等待时间估算，不是性能保证。'
            '请求间隔小于它时不会更快；实际时间以数据时间戳为准。')

    def _tick_elapsed(self):
        if not self._sampling:
            return
        elapsed = time.monotonic() - self._sample_started
        self._set_chip(self.step_sample,
                       f'采样中 {elapsed:.0f}s / 约{self._estimate_s:.0f}s', 'busy')

    def _update_steps(self):
        pending = self._pending_commands > 0
        kind = self._last_command_kind
        if pending and kind == 'preflight':
            self._set_chip(self.step_preflight, '预检中', 'busy')
        elif self._preflight_ok:
            self._set_chip(self.step_preflight, '预检通过', 'ok')
        elif self._preflight_failed:
            self._set_chip(self.step_preflight, '预检未通过', 'fail')
        else:
            self._set_chip(self.step_preflight, '未预检', 'idle')

        if pending and kind == 'activate':
            self._set_chip(self.step_activate, '激活中', 'busy')
        elif self._real_activated:
            self._set_chip(self.step_activate, '已激活', 'ok')
        else:
            self._set_chip(self.step_activate, '未激活', 'idle')

        if self._sampling:
            if not self._elapsed_timer.isActive():
                self._elapsed_timer.start()
            self.sample_progress.setRange(0, 0)
            self._tick_elapsed()
        else:
            self._elapsed_timer.stop()
            self.sample_progress.setRange(0, 1)
            self.sample_progress.setValue(0)
            if self._real_activated and self.last_real_frame is not None:
                self._set_chip(self.step_sample, f'已取得帧 {self.last_real_frame.sequence}', 'ok')
            else:
                self._set_chip(self.step_sample, '就绪' if self._real_activated else '未激活', 'idle')

        text, state = {
            'on': ('输出中 · 已读回', 'on'),
            'off': ('已关闭 · 已读回', 'ok'),
            'unknown': ('状态未知', 'fail'),
        }.get(self._output_ui, ('未开启', 'idle'))
        self._set_chip(self.step_output, text, state)
        if not self._function_ready('adc'):
            self._set_chip(self.step_sample, '不可用' if self._resources.get('adc') is False else '未激活', 'idle')
        if not self._ready.get('pwm', False):
            self._set_chip(self.step_output, '不可用' if self._resources.get('pwm') is False else '未激活', 'idle')
        self._set_chip(self.step_continuity, '就绪' if self._function_ready('continuity') else
                       ('不可用' if self._resources.get('continuity') is False else '未激活'), 'idle')

    @staticmethod
    def _settings() -> QSettings:
        return QSettings(QSettings.IniFormat, QSettings.UserScope, 'swd_instruments', 'gui')

    def enable_persistence(self):
        """仅由 main() 调用：回填手动选择的 profile 和序列号。"""
        self._persist_settings = True
        settings = self._settings()
        profile_name = str(settings.value('profile', '') or '')
        for index in range(self.profile_combo.count()):
            path = self.profile_combo.itemData(index)
            if path and Path(path).name == profile_name:
                self.profile_combo.setCurrentIndex(index)
                break
        if hasattr(self, 'occupied_list'):
            occupied = settings.value('occupied', list(DEFAULT_OCCUPIED))
            if isinstance(occupied, str):
                occupied = [occupied]
            self.occupied_list.blockSignals(True)
            for i in range(self.occupied_list.count()):
                item = self.occupied_list.item(i)
                item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in occupied else Qt.Unchecked)
            self.occupied_list.blockSignals(False)
            self._populate_pins()
        self._remember_profile()
        saved = str(settings.value('serial', '') or '').strip()
        if saved and not self.serial.text():
            self.serial.setText(saved)
            self.profile_hint.setText(
                self.profile_hint.text()
                + ' 已填入上次手动输入的序列号，请核对；点击“只读预检”后才会连接。')

    def _remember_serial(self):
        if self._persist_settings:
            self._settings().setValue('serial', self.serial.text().strip())

    def _remember_profile(self):
        path = self.profile_combo.currentData()
        if self._persist_settings and path:
            self._settings().setValue('profile', Path(path).name)

    def _on_profile_changed(self, *_):
        self._apply_profile_change()
        self._sync_notes()
        self._update_pin_summary()

    def _apply_profile_change(self):
        self._remember_profile()
        self.profile_notes.clear()
        self.profile_notes.hide()
        path = self.profile_combo.currentData()
        self._populate_pins()
        if path == '__general__':
            self.profile_hint.setText('通用模式：只读预检后仍需逐会话确认风险。' + RISK)
            self._pin_selection_changed()
            return
        if not path:
            self.adc_pin_hint.setText('尚未选择已审查 profile')
            return
        try:
            profile = load_profile(Path(path))
        except (ProfileError, OSError) as exc:
            self.profile_hint.setText(f'无法加载 profile：{exc}')
            self.adc_pin_hint.setText('ADC 输入待确认，请检查 profile')
            return
        pin = profile.adc_input
        meter_pin = profile.resources['meter_input'] or '不可用'
        continuity_pin = profile.resources['continuity_input'] or '不可用'
        self.meter_title.setText(f'④ 频率计 · {meter_pin}')
        self.continuity_title.setText(f'⑤ 通断 · {continuity_pin}→GND')
        self.meter_pin_hint.setText(
            'PA6 不耐受 5 V，仅允许 0–3.3 V 逻辑信号；HC-SR04 Echo 等 5 V 信号禁止直接连接。'
            if meter_pin == 'PA6' else f'脉冲输入 {meter_pin}，按 profile 授权。')
        self.continuity_pin_hint.setText(f'{continuity_pin}→GND；PB10 耐受 5 V，通断仅检测与 GND 的连接。')
        if hasattr(self, 'info_label'):
            self.info_label.setText(INFO_HTML.replace('{meter_pin}', meter_pin).replace('{continuity_pin}', continuity_pin).replace('{adc_pin}', pin or '不可用'))
        self.adc_pin_hint.setText(f'ADC 输入：{pin}（仅 0–3.3 V）' if pin else
                                 '该工程未授权 ADC 输入（引脚被固件占用）')
        names = {'adc': f'电压采样 {pin}', 'pwm': 'PWM 输出 PA8',
                 'meter': f'频率计 {meter_pin}', 'continuity': f'通断 {continuity_pin}'}
        grants = ' · '.join(names[key] for key, granted in profile.functions.items() if granted)
        self.profile_hint.setText(f'审查授权可用：{grants or "无"}。本次实际可用性以预检和激活结果为准。')
        self.profile_notes.setText('\n'.join(profile.notes))
        self.profile_notes.setVisible(bool(self.profile_notes.text()))
        self._pin_selection_changed()

    def _confirm_general_dialog(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle('通用模式风险确认')
        dialog.setIcon(QMessageBox.Warning)
        dialog.setText('板上固件未登记，将以通用模式激活。')
        dialog.setInformativeText(RISK + '\n\n本次会话完整镜像摘要已显示在预检日志，可用于以后登记。')
        accept = dialog.addButton('我已了解，继续', QMessageBox.AcceptRole)
        dialog.addButton('取消', QMessageBox.RejectRole)
        dialog.exec()
        return dialog.clickedButton() is accept

    def _occupied_pins(self):
        return [self.occupied_list.item(i).data(Qt.UserRole) for i in range(self.occupied_list.count())
                if self.occupied_list.item(i).checkState() == Qt.Checked]

    def _pin_selection(self):
        result = {key: combo.currentData() for key, combo in self.pin_combos.items()}
        result['occupied'] = self._occupied_pins()
        return result

    def _populate_pins(self):
        if not hasattr(self, 'pin_combos'):
            return
        path = self.profile_combo.currentData()
        try:
            profile = load_profile(path) if path and path != '__general__' else None
        except (OSError, ProfileError):
            profile = None
        defaults = dict(adc=(profile.adc_input or 'PA4') if profile else 'PA4', pwm='TIM1_CH1/PA8',
                        meter='TIM3_CH1/PA6', continuity='PB10')
        self._candidate_status = {}
        occupied = self._occupied_pins()
        saved = {}
        if self._persist_settings:
            try:
                saved = json.loads(str(self._settings().value('pins/' + str(path), '{}')))
            except ValueError:
                pass
        chosen = []
        for key, combo in self.pin_combos.items():
            combo.blockSignals(True)
            combo.clear()
            for p in CANDIDATES[key]:
                if p.pin not in occupied and not restricted(profile, p):
                    combo.addItem(p.label, p.key)
            combo.addItem('不启用', None)
            target = saved.get(key, defaults[key])
            index = combo.findData(target)
            combo.setCurrentIndex(index if index >= 0 else 0)
            if key not in saved:
                p = next((p for p in CANDIDATES[key] if p.key == combo.currentData()), None)
                if p and any(q.pin == p.pin or q.timer and q.timer == p.timer for q in chosen):
                    alternatives = [p for p in CANDIDATES[key] if combo.findData(p.key) >= 0 and not any(
                        q.pin == p.pin or q.timer and q.timer == p.timer for q in chosen)]
                    combo.setCurrentIndex(combo.findData(alternatives[0].key) if alternatives else combo.count() - 1)
            p = next((p for p in CANDIDATES[key] if p.key == combo.currentData()), None)
            if p:
                chosen.append(p)
            combo.blockSignals(False)
        self._update_pin_options()

    def _update_pin_options(self):
        if not hasattr(self, 'pin_combos'):
            return
        chosen = {k: next((p for p in CANDIDATES[k] if p.key == cb.currentData()), None)
                  for k, cb in self.pin_combos.items()}
        for key, combo in self.pin_combos.items():
            status = {row['key']: row['reason'] for row in getattr(self, '_candidate_status', {}).get(key, [])}
            for i in range(combo.count()):
                p = next((p for p in CANDIDATES[key] if p.key == combo.itemData(i)), None)
                if p is None:
                    continue
                conflict = any(q and (p.pin == q.pin or p.timer and p.timer == q.timer)
                               for k, q in chosen.items() if k != key)
                reason = status.get(p.key, '') or ('与其他功能的引脚或定时器冲突' if conflict else '')
                combo.model().item(i).setEnabled(not reason)
                combo.setItemData(i, reason, Qt.ToolTipRole)

    def _pin_selection_changed(self, *_):
        if not hasattr(self, 'pin_combos'):
            return
        self._update_pin_options()
        if self._persist_settings:
            self._settings().setValue('pins/' + str(self.profile_combo.currentData()),
                                      json.dumps(self._pin_selection()))
        for key, attr in (('adc', 'adc_pin_hint'), ('meter', 'meter_pin_hint'), ('continuity', 'continuity_pin_hint')):
            if hasattr(self, attr):
                combo = self.pin_combos[key]
                text = combo.currentText()
                warning = '；不耐受 5 V，仅允许 0–3.3 V；HC-SR04 Echo 等5V禁止直连' if '仅3.3V' in text else ''
                if key == 'adc':
                    text = (f'ADC 输入：{self.pin_combos[key].currentData()}（仅 0–3.3 V）'
                            if self.pin_combos[key].currentData() else 'ADC 不启用')
                    warning = ''
                getattr(self, attr).setText(text + warning + ('；通断启用内置上拉，只接干接点/GND，禁止外加5V' if key == 'continuity' else ''))
        if hasattr(self, 'meter_title'):
            self.meter_title.setText('④ 频率计 · ' + self._pin_detail(self.pin_combos['meter'].currentData()))
            self.continuity_title.setText('⑤ 通断 · ' + (self.pin_combos['continuity'].currentData() or '不启用') + '→GND')
        self._update_pin_summary()
        if hasattr(self, 'info_label'):
            text = INFO_HTML
            for key in ('adc', 'meter', 'continuity'):
                text = text.replace('{' + key + '_pin}', self.pin_combos[key].currentData() or '不启用')
            output = self.pin_combos['pwm'].currentData() or '不启用'
            text = text.replace('TIM1_CH1 / PA8', output).replace('PWM 输出 PA8', 'PWM 输出 ' + output)
            text = text.replace('PA8 只能驱动', output + ' 只能驱动')
            text = text.replace('ADC2 / PA4 或 PA7，按 profile 接线', 'ADC2 / ' + (self.pin_combos['adc'].currentData() or '不启用'))
            self.info_label.setText(text)
        if hasattr(self, 'real_pwm_button'):
            self.real_pwm_button.setToolTip((self.pin_combos['pwm'].currentData() or '不启用') + ' 输出3.3V逻辑方波；只能驱动高阻输入')
        if hasattr(self, 'pwm_card'):
            labels = self.pwm_card.findChildren(QLabel)
            for label in labels:
                if label.text().startswith('③ PWM'):
                    label.setText('③ PWM 输出 · ' + self._pin_detail(self.pin_combos['pwm'].currentData()))

    def _occupied_changed(self, *_):
        if self._persist_settings:
            self._settings().setValue('occupied', self._occupied_pins())
        self._populate_pins()
        self._pin_selection_changed()

    def _load_profiles(self):
        self.profile_combo.addItem('通用模式（未登记固件）', '__general__')
        directory = Path(__file__).parent / 'profiles'
        try:
            paths = discover_profiles(directory)
        except OSError:
            paths = []
        if not paths:
            self.profile_combo.setEnabled(True)
            self._on_profile_changed()
            return
        self.profile_combo.setEnabled(True)
        for path in paths:
            try:
                name = json.loads(path.read_text(encoding='utf-8')).get('display_name')
            except (OSError, ValueError):
                name = None
            label = f'{name}（{path.name}）' if name else path.name
            self.profile_combo.addItem(label, str(path))
        names = [path.name for path in paths]
        if 'servo_reference.json' in names:
            self.profile_combo.setCurrentIndex(names.index('servo_reference.json') + 1)
        self._on_profile_changed()

    # ------------------------------------------------------------------
    # 模拟路径（保持原有行为）
    def _show_frame(self, frame):
        self.current_frame = frame
        mean, low, high, peak = frame.stats
        is_real = bool(getattr(frame, 'is_real', False))
        times = frame.relative_times
        self.reading.setText(f'{mean:.3f} V')
        self._restyle(self.reading, 'kind', 'real' if is_real else 'sim')
        self.stat_min.setText(f'{low:.3f} V')
        self.stat_max.setText(f'{high:.3f} V')
        self.stat_peak.setText(f'{peak:.3f} V')
        self.stat_span.setText(f'{times[-1]:.3f} s')
        self.statistics.setText(
            f'{len(frame.points)} 点 · 参考电压 {frame.vref_mv} mV（配置值，非现场校准）')
        if is_real:
            # 真实帧点少且非等间隔：画出每个实际采样点，连线仅帮助观察。
            self.curve.setData(times, frame.volts, pen=pg.mkPen(REAL, width=1.5),
                               symbol='o', symbolSize=6, symbolBrush=REAL, symbolPen=None)
        else:
            self.curve.setData(times, frame.volts, pen=pg.mkPen(SIM, width=2), symbol=None)

    def capture(self):
        self.simulator.scene = self.scene.currentText()
        frame = self.simulator.capture(vref_mv=self.vref.value())
        self._show_frame(frame)
        self._set_source(f'{frame.source}｜{frame.clock}｜帧 {frame.sequence}', 'sim')

    def start(self):
        self._discard_real_sampling()
        self.capture()
        self.timer.start()
        self.statusBar().showMessage('正在模拟刷新；没有改变实物PWM，请另看真实实验页的输出状态')

    def pause(self):
        self.timer.stop()
        self._discard_real_sampling()
        self.statusBar().showMessage('显示已暂停，统计/曲线/导出保持同一帧')

    def single(self):
        self.timer.stop()
        self._discard_real_sampling()
        self.capture()
        self.statusBar().showMessage('单次模拟采集已冻结')

    def disconnect(self):
        self.timer.stop()
        self._discard_real_sampling()
        self.disable_sim_output()
        if getattr(self.current_frame, 'is_real', False):
            self._set_source(self._history_text('模拟刷新已停止'), 'history')
            self.statusBar().showMessage('已停止模拟刷新；当前保留的是历史真实数据')
        else:
            self._set_source('模拟已断开｜保留的读数和曲线为历史模拟数据', 'history')
            self.statusBar().showMessage('已停止模拟刷新；此操作没有改变实物PWM状态')

    def enable_sim_output(self):
        try:
            plan = plan_pwm(self.frequency.value(), self.duty.value())
        except ValueError as exc:
            self.statusBar().showMessage(str(exc))
            return
        self.simulator.output = plan
        self.scene.setCurrentText('PWM回环')
        self.output_status.setText(f'模拟输出已开启：量化频率 {plan.actual_hz:.6f} Hz，'
                                   f'占空比 {plan.actual_duty:.4f}%；'
                                   f'PSC={plan.psc}, ARR={plan.arr}, CCR={plan.ccr}。仅更新模拟器，实物状态见“真实 SWD”页。')

    def disable_sim_output(self):
        self.simulator.output = None
        self.output_status.setText('模拟输出：关闭；此按钮未改变实物输出，请查看“真实 SWD”页')

    def save_csv(self):
        if self.current_frame is None:
            self.statusBar().showMessage('请先运行模拟或单次采集')
            return
        is_real = bool(getattr(self.current_frame, 'is_real', False))
        default = 'swd_real_measurement.csv' if is_real else 'swd_simulation.csv'
        name, _ = QFileDialog.getSaveFileName(self, '导出当前显示帧', default, 'CSV (*.csv)')
        if name:
            try:
                export_csv(self.current_frame, name)
                self.statusBar().showMessage(f'已保存帧 {self.current_frame.sequence}：{name}')
            except OSError as exc:
                QMessageBox.warning(self, '导出失败', str(exc))

    # ------------------------------------------------------------------
    # 只读诊断
    def start_diagnostic(self):
        if self.worker and self.worker.isRunning():
            if isinstance(self.worker, RealWorker):
                self.statusBar().showMessage('真实会话正在运行；为安全起见不能同时诊断同一ST-Link')
            return
        serial = self.serial.text().strip()
        if not serial:
            self.diagnostic_text.setPlainText('请填写完整ST-Link序列号；没有连接任何设备。')
            return
        self._remember_serial()
        self.pause()
        self.tabs.setCurrentIndex(1)
        self.diag_button.setEnabled(False)
        self.worker = DiagnosticWorker(serial, self, swd_hz=self.swd_clock.currentData())
        self.worker.result.connect(self._on_diagnostic_result)
        self.worker.failed.connect(self.diagnostic_text.setPlainText)
        self.worker.finished.connect(self.diagnostic_finished)
        self.diagnostic_text.setPlainText('诊断进行中；未请求采集或输出写入。')
        self.worker.start()

    def diagnostic_finished(self):
        self.diag_button.setEnabled(True)
        worker = self.sender()
        if worker is None:
            worker = self.worker
        if worker is self.worker:
            self.worker = None
        if worker is not None:
            worker.deleteLater()
        self._maybe_finish_pending_close()

    # ------------------------------------------------------------------
    # 真实实验：设备/线程管理
    def _thread_busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _create_real_worker(self):
        if self._thread_busy():
            if not isinstance(self.worker, RealWorker):
                self.statusBar().showMessage('诊断或其他线程正在运行；不能同时打开真实会话')
            return None
        serial = self.serial.text().strip()
        if not serial:
            self.real_text.setPlainText('请填写完整ST-Link序列号；不会自动选择设备。')
            self.statusBar().showMessage('请填写完整ST-Link序列号；没有连接任何设备')
            return None
        data = self.profile_combo.currentData()
        if not data:
            self.real_text.setPlainText('未找到已审查profile；不会自动生成或伪造。')
            self.statusBar().showMessage('未找到已审查profile；真实实验保持锁定')
            return None
        general = data == '__general__'
        worker = RealWorker(serial, None if general else Path(data), self,
            swd_hz=self.swd_clock.currentData(), selection=self._pin_selection(), general_mode=general)
        worker.preflight_ready.connect(self._on_preflight)
        worker.activated.connect(self._on_activated)
        worker.frame_ready.connect(self._on_real_frame)
        worker.pwm_ready.connect(self._on_pwm)
        worker.pulse_ready.connect(self._on_pulse)
        worker.continuity_ready.connect(self._on_continuity)
        worker.failed.connect(self._on_real_failed)
        worker.closed.connect(self._on_real_closed)
        worker.status.connect(self._on_real_status)
        worker.finished.connect(self._real_finished)
        self.worker = worker
        self._remember_serial()
        self._preflight_failed = False
        self._last_command_kind = 'preflight'
        self._pending_commands += 1  # 打开后自动执行一次只读预检
        self._refresh_real_buttons()
        worker.start()
        return worker

    def _ack_command(self):
        if self._pending_commands > 0:
            self._pending_commands -= 1
        self._last_command_kind = ''
        controller = getattr(self.worker, '_controller', None) if isinstance(self.worker, RealWorker) else None
        if self._real_activated and controller is not None and getattr(controller, 'is_active', True) is False:
            self._mark_session_lost('驱动报告本次会话已不再持有外设')
        self._refresh_real_buttons()

    def _mark_session_lost(self, reason: str):
        """驱动已自行恢复并撤销接管时，界面同步回到“未激活”，提示用户重新连接。"""
        if not self._real_activated:
            return
        self._real_activated = False
        self._ready = {}
        self._real_continuous = False
        self._accept_real_frame = False
        self._sampling = False
        self._meter_continuous = False
        self._meter_timer.stop()
        self.meter_run_button.setChecked(False)
        self.meter_run_button.setText('连续测量')
        self._meter_state = '未激活'
        message = (f'{reason}。各功能已停止，输出已由驱动关闭并恢复；'
                   '请点“断开并恢复”，再重新预检和激活。')
        self.real_text.appendPlainText(f'会话已失效：{message}')
        self.real_pwm_status.setText('真实输出：会话已失效，驱动已关闭输出；请断开并恢复后重新激活')
        self._output_ui = 'idle'
        self._set_source(self._history_text('会话已失效，请断开并重新激活'), 'warn')
        self.statusBar().showMessage(f'会话已失效：{message}')
        self._refresh_real_buttons()

    @staticmethod
    def _usb_hint(text: str) -> str:
        lowered = str(text).lower()
        if 'usb error' in lowered or 'errno 5' in lowered or 'input/output error' in lowered or 'libusb' in lowered:
            return ('探头 USB 连接已中断（常见于 ST-Link 被碰到或重新插拔过）。'
                    '请点“断开并恢复”，关闭软件，重新插拔 ST-Link 后再打开。')
        return ''

    def _refresh_real_buttons(self):
        pending = self._pending_commands > 0
        session = isinstance(self.worker, RealWorker) and self.worker.isRunning()
        self.preflight_button.setEnabled(not pending)
        self.activate_button.setEnabled(not pending and self._preflight_ok and not self._real_activated)
        self.recover_button.setEnabled(session)
        self.sample_button.setEnabled(not pending and self._function_ready('adc'))
        self.real_run_button.setEnabled(not pending and self._function_ready('adc'))
        self.cancel_button.setEnabled(self._sampling)
        self.real_pwm_button.setEnabled(not pending and self._function_ready('pwm'))
        self.real_off_button.setEnabled(not pending and self._ready.get('pwm', False) and session)
        self.meter_button.setEnabled(not pending and self._function_ready('meter'))
        self.meter_run_button.setEnabled(self._meter_continuous or (not pending and self._function_ready('meter')))
        self.meter_timeout.setEnabled(not pending and not self._meter_continuous)
        self.continuity_button.setEnabled(not pending and self._function_ready('continuity'))
        for key, card, reason in (
            ('adc', self.sample_card, self.sample_reason),
            ('pwm', self.pwm_card, self.pwm_reason),
            ('meter', self.meter_card, self.meter_reason),
            ('continuity', self.continuity_card, self.continuity_reason),
        ):
            card.setEnabled(self._function_ready(key))
            unavailable = key in self._resources and not self._resources[key]
            reasons = self._resources.get('reasons', {})
            text = (reasons.get(key) if isinstance(reasons, dict) else None) if unavailable else ''
            reason.setText(text or ('当前工程的固件占用了该资源或未授权' if unavailable else ''))
            reason.setVisible(bool(reason.text()))
        if not self._function_ready('meter'):
            self._meter_state = '不可用' if self._resources.get('meter') is False else '未激活'
        self._set_chip(self.step_meter, self._meter_state, 'busy' if self._last_command_kind == 'pulse' else 'idle')
        self.swd_clock.setEnabled(not self._thread_busy())
        self.profile_combo.setEnabled(self.worker is None)
        self.pin_group.setEnabled(self.worker is None)
        self._update_steps()

    def _history_text(self, prefix: str) -> str:
        if self.current_frame is None:
            detail = '没有历史数据'
        elif getattr(self.current_frame, 'is_real', False):
            detail = '保留的读数为历史真实 ADC 数据'
        else:
            detail = '保留的读数和曲线为历史模拟数据'
        return f'{prefix}｜{detail}'

    # ------------------------------------------------------------------
    # 真实实验：命令入口
    def run_preflight(self):
        if self._pending_commands:
            return
        if self._thread_busy():
            if not isinstance(self.worker, RealWorker):
                self.statusBar().showMessage('诊断线程正在运行；不能同时打开真实会话')
                return
            self._pending_commands += 1
            self._last_command_kind = 'preflight'
            self._preflight_failed = False
            self._refresh_real_buttons()
            self.worker.request_preflight()
        else:
            if self._create_real_worker() is None:
                return
            self._last_command_kind = 'preflight'
        self.tabs.setCurrentIndex(0)
        self.real_text.appendPlainText(
            f'已请求只读预检（SWD时钟档位 {self.swd_clock.currentData()} Hz）：不写外设、不复位。')
        self.statusBar().showMessage('只读预检进行中；不写外设、不复位')

    def activate_real(self):
        if self._pending_commands:
            return
        if self._real_activated:
            self.statusBar().showMessage('已经激活；请先断开并恢复再重新激活')
            return
        if not isinstance(self.worker, RealWorker) or not self.worker.isRunning():
            self.statusBar().showMessage('请先执行只读预检并通过；不会自动激活')
            return
        if not self._preflight_ok:
            self.statusBar().showMessage('只读预检尚未通过；拒绝激活')
            return
        if self.worker.general_mode and not self.worker.general_confirmed:
            if not self._confirm_general_dialog():
                return
            self.worker.general_confirmed = True
        self._pending_commands += 1
        self._last_command_kind = 'activate' 
        self._refresh_real_buttons()
        self.worker.request_activate()
        self.real_text.appendPlainText('已请求激活可用功能；由后端事务写入并读回校验。')
        self.statusBar().showMessage('激活请求已排队；真实写入属于后端受限事务')

    def _discard_real_sampling(self):
        self._stop_meter()
        self._accept_real_frame = False
        self._real_continuous = False
        if isinstance(self.worker, RealWorker):
            self.worker.cancel_sampling()

    def single_real_sampling(self):
        self._real_continuous = False
        self.start_real_sampling()

    def start_real_continuous(self):
        self._real_continuous = True
        self.start_real_sampling()

    def start_real_sampling(self):
        if not isinstance(self.worker, RealWorker) or not self.worker.isRunning():
            self.statusBar().showMessage('没有真实会话；拒绝采样')
            return
        if not self._function_ready('adc'):
            self.statusBar().showMessage('ADC2尚未激活；拒绝真实采样')
            return
        if self._pending_commands:
            return
        self.timer.stop()
        self._accept_real_frame = True
        count = self.sample_points.value()
        interval = float(self.sample_interval.value())
        self._pending_commands += 1
        self._last_command_kind = 'sample'
        self._sampling = True
        self._sample_started = time.monotonic()
        self._refresh_real_buttons()
        self.worker.request_sample(SampleRequest(count, interval))
        self.real_text.appendPlainText(
            f'已请求真实采样：{count}点，间隔{interval:g}s；主机轮询时间戳非等间隔。')
        self.statusBar().showMessage('真实采样进行中；曲线时间来自主机轮询，不是硬件触发')

    def cancel_real_sampling(self):
        self._accept_real_frame = False
        self._real_continuous = False
        if isinstance(self.worker, RealWorker) and self.worker.isRunning():
            self.worker.cancel_sampling()
        self._sampling = False
        self._refresh_real_buttons()
        self.statusBar().showMessage('已请求取消采样；未完成帧不发布。正在等待当前USB操作返回，硬件状态未知')

    def set_real_pwm(self):
        if not isinstance(self.worker, RealWorker) or not self.worker.isRunning():
            self.statusBar().showMessage('没有真实会话；拒绝PWM写入')
            return
        if not self._function_ready('pwm') or self._pending_commands:
            self.statusBar().showMessage('TIM1尚未激活；拒绝真实PWM写入')
            return
        frequency = float(self.real_frequency.value())
        duty = float(self.real_duty.value())
        self._pending_commands += 1
        self._last_command_kind = 'pwm'
        self._refresh_real_buttons()
        self.worker.request_pwm(frequency, duty)
        self.real_text.appendPlainText(
            f'已请求真实PWM：{frequency:g} Hz，占空比 {duty:g}%；以读回状态决定显示。')
        self.statusBar().showMessage('真实PWM写入已排队；未读回确认前不宣称输出状态')

    def real_output_off(self):
        if not isinstance(self.worker, RealWorker) or not self.worker.isRunning():
            self.statusBar().showMessage('没有真实会话；没有输出需要关闭')
            return
        if not self._ready.get('pwm', False) or self._pending_commands:
            self.statusBar().showMessage('TIM1尚未激活；没有本工具持有的输出')
            return
        self._pending_commands += 1
        self._last_command_kind = 'off'
        self._refresh_real_buttons()
        self.worker.request_output_off()
        self.real_text.appendPlainText('已请求关闭真实输出；只有读回确认才显示已关闭。')
        self.statusBar().showMessage('关闭真实输出已排队；等待读回确认')

    def recover_real(self):
        self._discard_real_sampling()
        worker = self.worker
        if not isinstance(worker, RealWorker) or not worker.isRunning():
            self.statusBar().showMessage('没有正在运行的真实会话；没有输出需要关闭')
            return
        worker.request_stop()
        self._sampling = False
        self._refresh_real_buttons()
        self._set_source(self._history_text('正在断开真实SWD会话，输出状态待读回确认'), 'history')
        self.real_text.appendPlainText('已请求断开并恢复；协作取消，不kill线程、不复位目标。')
        self.statusBar().showMessage(
            '已请求协作取消，不会强杀线程。正在等待当前USB操作返回，硬件状态未知')

    # ------------------------------------------------------------------
    # 真实实验：信号处理
    def _on_real_status(self, message: str):
        self.statusBar().showMessage(message)

    def _on_preflight(self, report):
        self._resources = dict(report.resources)
        self._candidate_status = self._resources.get('candidates', {})
        for key, chosen in self._resources.get('selection', {}).items():
            combo = self.pin_combos[key]
            index = combo.findData(chosen)
            if index >= 0:
                combo.setCurrentIndex(index)
        self._update_pin_options()
        if not self._resources.get('adc', False):
            self._set_source(self._history_text('当前工程 ADC 不可用'), 'history')
            self.statistics.setText('当前工程 ADC 不可用；显示区保留历史数据')
        self._preflight_ok = bool(report.ok)
        self._preflight_failed = not report.ok
        # 预检失败不能清除“已激活”事实：此时输出可能仍由本工具持有，
        # 必须保留关闭/恢复入口，不能把按钮全禁用后假装没有输出。
        if not report.ok and self._real_activated:
            self.real_pwm_status.setText(
                '预检失败但本工具仍持有TIM1；输出状态待读回确认，请使用“关闭真实输出”或“断开并恢复”')
        lines = ['只读预检：' + ('通过（仍需核对本次接线与原程序状态，不代表无干扰）'
                                 if report.ok else '未通过，真实写入保持锁定')]
        lines.extend(f'问题：{item}' for item in report.issues)
        usb_hint = next((self._usb_hint(item) for item in report.issues if self._usb_hint(item)), '')
        if usb_hint:
            lines.append(f'处理办法：{usb_hint}')
        lines.extend(f'说明：{item}' for item in report.notes)
        if report.firmware_sha256:
            lines.append(f'板上固件摘要：{report.firmware_sha256}')
        if report.profile_id:
            lines.append(f'profile：{report.profile_id}')
        self.real_text.appendPlainText('\n'.join(lines))
        self._ack_command()
        self.statusBar().showMessage('只读预检通过；可请求激活' if report.ok
                                     else (usb_hint or '只读预检未通过；真实写入保持锁定'))

    def _on_activated(self, report):
        self._ready = dict(adc=bool(report.adc2_ready), pwm=bool(report.tim1_ready),
                           meter=bool(getattr(report, 'meter_ready', False)),
                           continuity=bool(getattr(report, 'continuity_ready', False)))
        self._real_activated = bool(report.ok and any(self._ready.values()))
        self._meter_state = '就绪' if self._ready['meter'] else '未激活'
        if self._real_activated:
            self._preflight_ok = True
        lines = ['激活：' + ('可用功能已就绪，具体状态见各功能卡片'
                            if self._real_activated else '未完成，真实采样与输出不可用')]
        lines.extend(f'问题：{item}' for item in report.issues)
        lines.extend(f'说明：{item}' for item in report.notes)
        self.real_text.appendPlainText('\n'.join(lines))
        self._output_ui = 'idle'
        if self._ready.get('pwm', False):
            self.real_pwm_status.setText('真实输出：未开启（激活流程保持CC1E/MOE/CEN=0，尚未读回确认关闭）')
        else:
            self.real_pwm_status.setText('真实输出：未激活；不显示任何已关闭结论')
        self._ack_command()
        self.statusBar().showMessage('真实激活成功；可用功能见各卡片' if self._real_activated
                                     else '真实激活未完成；没有采样或输出')

    def _on_real_frame(self, frame):
        self._sampling = False
        if not self._accept_real_frame or self._close_pending:
            self._ack_command()
            return
        if not getattr(frame, 'is_real', False):
            self.real_text.appendPlainText('收到非真实来源帧，拒绝显示。')
            self.statusBar().showMessage('拒绝显示非真实来源帧')
            self._ack_command()
            return
        self.last_real_frame = frame
        self._show_frame(frame)
        self._set_source(f'{frame.source}｜{frame.clock}｜帧 {frame.sequence}', 'real')
        self.real_text.appendPlainText(
            f'已接收真实帧 {frame.sequence}：{len(frame.points)}点，'
            f'vref={frame.vref_mv}mV（profile配置值）。')
        self.statusBar().showMessage('真实SWD采样帧已显示；主机轮询时间戳非等间隔')
        self._ack_command()
        if self._real_continuous:
            QTimer.singleShot(0, self._continue_real_sampling)
        else:
            self._accept_real_frame = False

    def _continue_real_sampling(self):
        if self._real_continuous and not self._close_pending:
            self.start_real_sampling()

    def _on_pwm(self, result):
        text = '真实PWM'
        if result.actual_hz is not None and result.actual_duty is not None:
            text += f'：实际量化频率 {result.actual_hz:.6f} Hz，占空比 {result.actual_duty:.4f}%'
        else:
            text += '：未获得量化频率/占空比'
        state = result.output_state
        if state == 'on-confirmed' and result.output_active:
            text += '；输出已开启并读回确认（固定逻辑幅值方波，不保证任意幅值）'
            self._output_ui = 'on'
        elif state == 'off-confirmed':
            text += '；输出关闭已读回确认'
            self._output_ui = 'off'
        elif state == 'unknown-may-still-output':
            text += '；输出状态未知，可能仍在输出，绝不假定已关闭'
            self._output_ui = 'unknown'
        else:
            text += '；本工具未持有TIM1输出'
            self._output_ui = 'idle'
        if not result.ok:
            text = '真实PWM操作失败；' + text
        if result.message:
            text += f'。后端说明：{result.message}'
        self.real_pwm_status.setText(text)
        self.real_text.appendPlainText(text)
        if state == 'unknown-may-still-output':
            self._set_source(self._history_text('输出未知，可能仍在输出'), 'warn')
        self._ack_command()
        self.statusBar().showMessage(text)

    def _on_real_failed(self, message: str):
        if self._last_command_kind == 'pulse':
            self._stop_meter()
            self._meter_state = '错误'
            self.meter_result.setText('错误')
            self.meter_high.setText('高电平时间 —')
        self._real_continuous = False
        self._accept_real_frame = False
        kind = self._last_command_kind
        if kind == 'preflight':
            self._preflight_failed = True
        if self._sampling:
            self._sampling = False
        if self._ready.get('pwm', False) or kind in ('pwm', 'off'):
            self.real_pwm_status.setText(
                '真实输出状态未知，可能仍在输出；不得假定已关闭。请使用“断开并恢复”并检查所选输出引脚。')
            self._output_ui = 'unknown'
            self._set_source(self._history_text('输出未知，可能仍在输出'), 'warn')
        self.real_text.appendPlainText(f'真实操作失败：{message}')
        usb_hint = self._usb_hint(message)
        if usb_hint:
            self.real_text.appendPlainText(f'处理办法：{usb_hint}')
        self._ack_command()
        if usb_hint:
            self.statusBar().showMessage(usb_hint)
        elif self._sampling is False and kind == 'sample':
            self.statusBar().showMessage(
                f'真实采样失败或已取消：{message}。未完成帧不发布，硬件状态未知')
        else:
            self.statusBar().showMessage(f'真实操作失败：{message}')

    def _on_real_closed(self, report):
        self._stop_meter()
        self._ready = {}
        self._real_continuous = False
        self._accept_real_frame = False
        self._pending_commands = 0
        self._sampling = False
        self._preflight_ok = False
        self._preflight_failed = False
        self._real_activated = False
        self._last_command_kind = ''
        self._refresh_real_buttons()
        if report is None:
            if self.current_frame is None:
                self._set_source('未连接｜没有真实测量数据', 'idle')
            self.statusBar().showMessage('真实SWD会话未建立或已结束；没有输出状态报告')
            return
        state = getattr(report, 'output_state', 'unknown-may-still-output')
        if state == 'unknown-may-still-output':
            self._output_ui = 'unknown'
            self._set_source(self._history_text('输出未知，可能仍在输出'), 'warn')
            self.real_pwm_status.setText(
                '输出状态未知，可能仍在输出；不得假定已关闭。请检查所选输出引脚和外部电路。')
        elif state == 'off-confirmed':
            self._output_ui = 'off'
            self._set_source(self._history_text('已断开'), 'history')
            self.real_pwm_status.setText('真实输出：关闭已读回确认')
        else:
            self._output_ui = 'idle'
            self._set_source(self._history_text('已断开'), 'history')
            self.real_pwm_status.setText('真实输出：本工具未持有，无需关闭')
        self._update_steps()
        lines = ['真实会话已结束。',
                 f'输出状态：{state}',
                 f'已恢复字段数：{len(getattr(report, "restored", ()))}']
        lines.extend(f'恢复错误：{item}' for item in getattr(report, 'errors', ()))
        self.real_text.appendPlainText('\n'.join(lines))
        if getattr(report, 'errors', ()):
            self.statusBar().showMessage(f'真实会话已关闭但存在恢复错误：{report.errors[0]}')
        else:
            self.statusBar().showMessage('真实SWD会话已关闭；输出状态按读回结果标识')

    def _real_finished(self):
        self._stop_meter()
        self._ready = {}
        worker = self.sender()
        if worker is self.worker:
            self.worker = None
        if worker is not None:
            worker.deleteLater()
        self._pending_commands = 0
        self._sampling = False
        self._preflight_ok = False
        self._preflight_failed = False
        self._real_activated = False
        self._last_command_kind = ''
        self._refresh_real_buttons()
        self._maybe_finish_pending_close()

    def _maybe_finish_pending_close(self):
        if self._close_pending:
            self._close_pending = False
            QTimer.singleShot(0, self.close)

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        self.timer.stop()
        self.simulator.output = None
        self._discard_real_sampling()
        if self.worker is not None and self.worker.isRunning():
            if hasattr(self.worker, 'request_stop'):
                self.worker.request_stop()
                self._close_pending = True
                self.statusBar().showMessage(
                    '已请求协作取消，不会强杀线程。正在等待当前USB操作返回，硬件状态未知')
            else:
                self.statusBar().showMessage('线程尚未结束，请结束后再关闭；不会强杀线程')
            event.ignore()
            return
        self._close_pending = False
        self.timer.stop()
        self.simulator.output = None
        self.serial_terminal.shutdown()
        event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setPalette(build_palette())
    app.setFont(ui_font())
    window = InstrumentWindow()
    window.enable_persistence()
    window.show()
    return app.exec()
