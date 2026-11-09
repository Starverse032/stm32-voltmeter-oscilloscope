# -*- coding: utf-8 -*-
"""
main_window.py —— 中文仪器窗口（PySide6 + PyQtGraph）

两个视图：电压表 / 示波器。
真实串口数据与模拟数据都汇合到同一个 _handle_frame()，共用同一套
protocol.Frame、processing 换算/统计/触发函数，不给模拟数据开小灶：
  - 真实：SerialWorker(QThread) 内部 FrameParser → frame_ready(Frame) 信号；
  - 模拟：Simulator 产出"真实格式"的帧字节 → 主线程同类型 FrameParser → Frame。
两条路径得到同样的 Frame 对象，之后处理完全一致。

状态语义严格按 docs/protocol.md 第 9 节：
  - 未收到任何帧 → "等待设备"，不显示"测量中"；
  - 模拟模式显著标注"模拟数据"，禁用真实连接按钮；
  - 断线 → "已断开"，冻结最后一帧并标注"历史数据"，绝不继续刷新；
  - 暂停 / 单次冻结后，后台新帧不覆盖当前显示帧（FrameHold）；
  - 采样率 / 参考电压切换：显示"切换中"，只有 SEQ 匹配 + status OK +
    回读值 == 请求值才生效（ParameterSwitch），否则回滚报错；
  - 横轴按设备上报的 sample_rate_hz 换算，CSV 注明帧间不连续。
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QPushButton,
    QSpinBox, QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

try:
    import pyqtgraph as pg
    _HAS_PG = True
except Exception:                       # PyQtGraph 缺失时不让整个程序崩掉
    pg = None
    _HAS_PG = False

import protocol as p
import processing as proc
from simulator import Simulator, SCENE_NAMES
from serial_worker import SerialWorker, enumerate_ports, has_serial

if _HAS_PG:
    pg.setConfigOption("background", (25, 26, 32))
    pg.setConfigOption("foreground", (205, 210, 220))


DARK_QSS = """
QWidget { background-color: #1b1d24; color: #e8eaf0;
          font-family: 'Microsoft YaHei','SimHei',sans-serif; font-size: 10pt; }
QMainWindow { background-color: #1b1d24; }
QGroupBox { border: 1px solid #3a3f4d; border-radius: 6px; margin-top: 10px; padding: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px;
                   color: #82b1ff; font-weight: bold; }
QPushButton { background-color: #2b2f3b; border: 1px solid #4a5162;
              border-radius: 5px; padding: 5px 14px; }
QPushButton:hover { background-color: #394054; }
QPushButton:pressed { background-color: #47506a; }
QPushButton:disabled { color: #717784; border-color: #333743; }
QComboBox, QSpinBox, QDoubleSpinBox { background-color: #242733;
    border: 1px solid #4a5162; border-radius: 4px; padding: 4px 8px; min-width: 90px; }
QTabWidget::pane { border: 1px solid #3a3f4d; border-radius: 6px; }
QTabBar::tab { background: #262a35; padding: 8px 24px;
               border: 1px solid #3a3f4d; border-bottom: none; }
QTabBar::tab:selected { background: #31374a; color: #82b1ff; font-weight: bold; }
QLabel#bigNumber { font-size: 52pt; color: #4dd0e1; font-weight: bold; }
QLabel#bigUnit { font-size: 18pt; color: #9aa3b5; }
QLabel#sectionTitle { color: #82b1ff; font-weight: bold; }
QPlainTextEdit { background-color: #14161c; border: 1px solid #3a3f4d; font-size: 9pt; }
QStatusBar { background-color: #14161c; color: #aab2c0; }
"""


class MainWindow(QMainWindow):
    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("STM32 电压表 / 示波器  ·  低压单通道教学仪器")
        self.resize(1120, 760)

        # ---- 运行状态 ----
        self.mode = "idle"                 # idle / sim / real / disconnected
        self._ever_connected = False
        self.parser = p.FrameParser()      # 供模拟数据使用的解析器（与串口线程同类型）
        self.hold = proc.FrameHold()       # 运行/暂停/单次冻结
        self.rate_switch = proc.ParameterSwitch()
        self.vref_switch = proc.ParameterSwitch()
        self.sim = Simulator("sine", 10000, 3300, 1024)
        self._seq = 0                      # 命令 SEQ（0..255 回绕）
        self._display: Optional[dict] = None   # 当前显示帧（唯一真值）
        self._last_frame_is_history = False
        self._counts = self.parser.counts.as_dict()
        self._semantic_discards = 0        # 采样帧语义损坏被丢弃的次数
        self._errors: list = []
        self._log_lines: list = []
        self._current_rate = 10000
        self._current_vref = 3300
        self._trigger_mode = "auto"        # auto / rising
        self._trigger_threshold = 1.65
        self._single_pending_seq: Optional[int] = None
        self._waiting_trigger = False
        self._control_pending = None
        self._device_run_state = None

        self._build_ui()

        # ---- 串口线程 ----
        self.worker = SerialWorker(self)
        self.worker.frame_ready.connect(self._on_serial_frame)
        self.worker.errors_ready.connect(self._on_serial_errors)
        self.worker.counts_ready.connect(self._on_counts)
        self.worker.connected.connect(self._on_connected)
        self.worker.disconnected.connect(self._on_disconnected)
        self.worker.log.connect(self._append_log)

        # ---- 定时器 ----
        self._sim_timer = QTimer(self)
        self._sim_timer.setInterval(130)
        self._sim_timer.timeout.connect(self._sim_tick)
        self._switch_timer = QTimer(self)
        self._switch_timer.setSingleShot(True)
        self._switch_timer.setInterval(2500)
        self._switch_timer.timeout.connect(self._on_switch_timeout)
        self._single_timer = QTimer(self)
        self._single_timer.setSingleShot(True)
        self._single_timer.setInterval(2500)
        self._single_timer.timeout.connect(self._on_single_timeout)
        self._control_timer = QTimer(self)
        self._control_timer.setSingleShot(True)
        self._control_timer.setInterval(2500)
        self._control_timer.timeout.connect(self._on_control_timeout)

        self._refresh_ports()
        self._update_status()
        self._refresh_labels()
        self._append_log("窗口初始化完成；未接设备，当前为'等待设备'状态")

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setStyleSheet(DARK_QSS)
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        outer.addWidget(self._build_conn_panel())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_voltmeter_tab(), "电压表")
        self.tabs.addTab(self._build_scope_tab(), "示波器")
        outer.addWidget(self.tabs, 1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("就绪 · 未接设备时请使用'启动模拟模式'")

    def _build_conn_panel(self) -> QGroupBox:
        box = QGroupBox("连接与数据源")
        lay = QGridLayout(box)

        lay.addWidget(QLabel("串口端口："), 0, 0)
        self.port_combo = QComboBox()
        lay.addWidget(self.port_combo, 0, 1)
        self.btn_refresh = QPushButton("刷新端口")
        self.btn_refresh.clicked.connect(self._refresh_ports)
        lay.addWidget(self.btn_refresh, 0, 2)
        self.btn_connect = QPushButton("连接真实设备")
        self.btn_connect.clicked.connect(self._on_connect)
        lay.addWidget(self.btn_connect, 0, 3)
        self.btn_disconnect = QPushButton("断开")
        self.btn_disconnect.clicked.connect(self._on_disconnect_clicked)
        lay.addWidget(self.btn_disconnect, 0, 4)

        lay.addWidget(QLabel("模拟场景："), 1, 0)
        self.scene_combo = QComboBox()
        for key, name in SCENE_NAMES.items():
            self.scene_combo.addItem(name, key)
        self.scene_combo.currentIndexChanged.connect(self._on_scene_changed)
        lay.addWidget(self.scene_combo, 1, 1)
        self.btn_start_sim = QPushButton("启动模拟模式")
        self.btn_start_sim.clicked.connect(self.start_simulation)
        lay.addWidget(self.btn_start_sim, 1, 2)
        lay.addWidget(QLabel("（模拟数据非真实测量）"), 1, 3, 1, 2)

        self.state_label = QLabel("● 等待设备")
        self.state_label.setObjectName("stateLabel")
        lay.addWidget(self.state_label, 2, 0, 1, 5)
        return box

    def _build_voltmeter_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        title = QLabel("直流电压读数（平均值）")
        title.setObjectName("sectionTitle")
        lay.addWidget(title)

        big_row = QHBoxLayout()
        big_row.addStretch(1)
        self.big_number = QLabel("--.---")
        self.big_number.setObjectName("bigNumber")
        big_row.addWidget(self.big_number)
        unit = QLabel("V")
        unit.setObjectName("bigUnit")
        big_row.addWidget(unit)
        big_row.addStretch(1)
        lay.addLayout(big_row)

        self.lbl_src = QLabel("数据来源：--")
        self.lbl_src.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.lbl_src)

        stats_box = QGroupBox("统计量（本帧）")
        g = QGridLayout(stats_box)
        self.vm_mean = QLabel("平均：-- V")
        self.vm_min = QLabel("最小：-- V")
        self.vm_max = QLabel("最大：-- V")
        self.vm_pp = QLabel("峰峰值：-- V")
        self.vm_count = QLabel("点数：--")
        g.addWidget(self.vm_mean, 0, 0)
        g.addWidget(self.vm_min, 0, 1)
        g.addWidget(self.vm_max, 1, 0)
        g.addWidget(self.vm_pp, 1, 1)
        g.addWidget(self.vm_count, 2, 0, 1, 2)
        lay.addWidget(stats_box)

        note = QLabel(
            "说明：换算 V = code / 4095.0 × (设备参考电压 mV / 1000.0)；"
            "1 LSB ≈ %.4f mV @3.3 V，电压显示 3 位小数，不冒充更高精度。\n"
            "参考电压由设备上报，掉电不保存；悬空 ADC 不代表 0 V。" % proc.LSB_MV_AT_3300
        )
        note.setWordWrap(True)
        lay.addWidget(note)

        self.lbl_vref_vm = QLabel("设备参考电压：-- mV（仅本次运行有效，掉电不保存）")
        self.lbl_vref_vm.setWordWrap(True)
        lay.addWidget(self.lbl_vref_vm)
        lay.addStretch(1)
        return w

    def _build_scope_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        # --- 控制行 1：运行 / 缩放 / 导出 ---
        row1 = QHBoxLayout()
        self.btn_run = QPushButton("运行")
        self.btn_pause = QPushButton("暂停（保持）")
        self.btn_single = QPushButton("单次采集")
        self.btn_run.clicked.connect(self.resume)
        self.btn_pause.clicked.connect(self.pause)
        self.btn_single.clicked.connect(self.single_capture)
        row1.addWidget(self.btn_run)
        row1.addWidget(self.btn_pause)
        row1.addWidget(self.btn_single)
        row1.addSpacing(16)
        bzin = QPushButton("放大")
        bzout = QPushButton("缩小")
        breset = QPushButton("复位视图")
        bzin.clicked.connect(self.zoom_in)
        bzout.clicked.connect(self.zoom_out)
        breset.clicked.connect(self.reset_view)
        row1.addWidget(bzin)
        row1.addWidget(bzout)
        row1.addWidget(breset)
        row1.addSpacing(16)
        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.clicked.connect(self._on_export_clicked)
        row1.addWidget(self.btn_export)
        row1.addStretch(1)
        lay.addLayout(row1)

        # --- 控制行 2：触发 ---
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("触发方式："))
        self.trigger_combo = QComboBox()
        self.trigger_combo.addItem("自动显示", "auto")
        self.trigger_combo.addItem("上升沿触发", "rising")
        self.trigger_combo.currentIndexChanged.connect(self._on_trigger_mode_changed)
        row2.addWidget(self.trigger_combo)
        row2.addWidget(QLabel("触发阈值："))
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0.0, 3.3)
        self.threshold_spin.setDecimals(3)
        self.threshold_spin.setSingleStep(0.05)
        self.threshold_spin.setValue(1.65)
        self.threshold_spin.setSuffix(" V")
        self.threshold_spin.valueChanged.connect(self._on_threshold_changed)
        row2.addWidget(self.threshold_spin)
        self.lbl_trigger_state = QLabel("触发：自动显示")
        row2.addWidget(self.lbl_trigger_state)
        row2.addStretch(1)
        lay.addLayout(row2)

        # --- 控制行 3：采样率 / 参考电压 ---
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("采样率："))
        self.rate_combo = QComboBox()
        self.rate_combo.addItem("10 kS/s (10000)", 10000)
        self.rate_combo.addItem("100 kS/s (100000)", 100000)
        self.rate_combo.currentIndexChanged.connect(self._on_rate_changed)
        row3.addWidget(self.rate_combo)
        self.rate_status = QLabel("采样率：10000 S/s（已生效）")
        row3.addWidget(self.rate_status)
        row3.addSpacing(12)
        row3.addWidget(QLabel("参考电压："))
        self.vref_spin = QSpinBox()
        self.vref_spin.setRange(p.VREF_MIN_MV, p.VREF_MAX_MV)
        self.vref_spin.setSingleStep(50)
        self.vref_spin.setValue(3300)
        self.vref_spin.setSuffix(" mV")
        row3.addWidget(self.vref_spin)
        self.btn_vref_apply = QPushButton("应用")
        self.btn_vref_apply.clicked.connect(self._on_vref_apply)
        row3.addWidget(self.btn_vref_apply)
        self.vref_status = QLabel("设备参考电压：3300 mV")
        row3.addWidget(self.vref_status)
        row3.addStretch(1)
        lay.addLayout(row3)

        hint = QLabel("提示：参考电压仅设备本次运行有效，掉电不保存；"
                      "参数切换需设备确认后才更新横轴与标签。鼠标滚轮也可缩放波形。")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # --- 波形 ---
        if _HAS_PG:
            self.plot = pg.PlotWidget()
            self.plot.setLabel("bottom", "时间 (ms，帧内相对)")
            self.plot.setLabel("left", "电压 (V)")
            self.plot.showGrid(x=True, y=True, alpha=0.3)
            self.plot.setYRange(-0.2, 3.5)
            self.plot.getViewBox().setMouseEnabled(x=True, y=True)
            self.curve = self.plot.plot(pen=pg.mkPen("#4dd0e1", width=2))
            lay.addWidget(self.plot, 1)
        else:
            self.plot = None
            self.curve = None
            lay.addWidget(QLabel("PyQtGraph 未安装，波形绘制不可用（其余功能仍可用）"), 1)

        # --- 信息区 ---
        info = QGroupBox("设备 / 数据状态")
        ig = QGridLayout(info)
        self.lbl_rate = QLabel("采样率：-- S/s")
        self.lbl_frame_len = QLabel("帧长度：-- 点")
        self.lbl_data_status = QLabel("数据状态：等待设备")
        self.lbl_frame_seq = QLabel("帧序号：--")
        self.lbl_error_count = QLabel("错误计数：0")
        self.lbl_run_state = QLabel("运行：--")
        self.lbl_freq = QLabel("频率：--")
        self.lbl_vref = QLabel("设备参考电压：-- mV")
        ig.addWidget(self.lbl_rate, 0, 0)
        ig.addWidget(self.lbl_frame_len, 0, 1)
        ig.addWidget(self.lbl_data_status, 0, 2)
        ig.addWidget(self.lbl_frame_seq, 1, 0)
        ig.addWidget(self.lbl_error_count, 1, 1)
        ig.addWidget(self.lbl_run_state, 1, 2)
        ig.addWidget(self.lbl_freq, 2, 0)
        ig.addWidget(self.lbl_vref, 2, 1, 1, 2)
        lay.addWidget(info)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(72)
        self.log_view.setPlaceholderText("运行日志 / 协议错误信息")
        lay.addWidget(self.log_view)
        return w

    # ------------------------------------------------------------------
    # 统一的帧处理入口（真实与模拟共用）
    # ------------------------------------------------------------------
    def _feed_bytes(self, data: bytes) -> None:
        """模拟帧字节 / 模拟响应帧字节 → 同一 FrameParser → 同一 _handle_frame。"""
        frames, errors = self.parser.feed(data)
        self._counts = self.parser.counts.as_dict()
        for e in errors:
            self._append_error(e)
        for f in frames:
            self._handle_frame(f)
        self._refresh_labels()

    def _on_serial_frame(self, frame) -> None:
        """串口线程已解析好的 Frame，直接汇合到同一处理链。"""
        if self.mode != "real":
            return  # 切换数据源后可能仍有已排队的旧线程信号
        self._handle_frame(frame)
        self._refresh_labels()

    def _on_serial_errors(self, errors) -> None:
        if self.mode not in ("real", "connecting"):
            return
        for e in errors:
            self._append_error(e)
        self._refresh_labels()

    def _on_counts(self, counts) -> None:
        if self.mode != "real":
            return
        self._counts = counts
        self._refresh_labels()

    def _handle_frame(self, f: p.Frame) -> None:
        if f.ftype == p.TYPE_SAMPLE:
            sd = p.parse_sample_payload(f.payload)
            if not sd.ok:
                # 样本码 >4095 / 长度不符等：整帧丢弃，不显示
                self._semantic_discards += 1
                self._append_error("采样帧数据损坏：%s（SEQ=0x%02X，整帧丢弃）"
                                   % (sd.reason, f.seq))
                return
            volts = proc.codes_to_volts(sd.samples, sd.vref_mv)
            times = proc.time_axis(sd.point_count, sd.sample_rate_hz)
            frame_data = {
                "seq": f.seq,
                "codes": sd.samples,
                "volts": volts,
                "times": times,
                "sample_rate_hz": sd.sample_rate_hz,
                "vref_mv": sd.vref_mv,
                "point_count": sd.point_count,
                "stats": proc.statistics(volts),
                "freq": proc.estimate_frequency(volts, sd.sample_rate_hz),
                "source": "模拟数据（非真实测量）" if self.mode == "sim" else "真实设备",
            }
            # 暂停或等待 SINGLE 的 ACK 时不消费旧的在途帧。
            if self.hold.mode == proc.FrameHold.MODE_HOLD:
                return
            if (self._trigger_mode == "rising" and
                    proc.find_rising_trigger(volts, self._trigger_threshold,
                                             sd.sample_rate_hz) is None):
                self._waiting_trigger = True
                self.lbl_trigger_state.setText("触发：等待触发 / 未触发（保持上一帧）")
                # 真设备 SINGLE 只发一帧；未触发就请求下一帧，直到触发或用户取消。
                if self.mode == "real" and self.hold.mode == proc.FrameHold.MODE_SINGLE:
                    self._request_real_single()
                return
            # 冻结控制：返回 False 表示当前显示帧被保持，新帧不覆盖
            if self.hold.submit(frame_data):
                self._waiting_trigger = False
                self._display = frame_data
                self._last_frame_is_history = False
                self._redraw_all()
        elif f.ftype == p.TYPE_RESPONSE:
            rd = p.parse_response_payload(f.payload)
            if rd.ok:
                self._on_response(f.seq, rd)
            else:
                self._append_error("响应帧解析失败：%s" % rd.reason)
        elif f.ftype == p.TYPE_EVENT:
            ev = p.parse_event_payload(f.payload)
            text = ("设备事件 [0x%02X %s]：%s"
                    % (ev.code, p.EVENT_TEXT.get(ev.code, ""), ev.text))
            self._append_error(text)
            # docs/protocol.md 4.4：EVENT 必须显示到状态栏，不得静默吞掉
            self.statusBar().showMessage(text)
        elif f.ftype == p.TYPE_COMMAND:
            self._append_error("收到意外的命令帧（设备不应发 0x20），已忽略")

    # ------------------------------------------------------------------
    # 参数变更响应 / 超时
    # ------------------------------------------------------------------
    def _on_response(self, frame_seq: int, rd: p.ResponseData) -> None:
        # 过期 ACK 不能取消新的请求，也不能抢先改写当前参数标签。
        for cmd, switch in ((p.CMD_SET_RATE, self.rate_switch),
                            (p.CMD_SET_VREF, self.vref_switch)):
            if rd.ack_cmd == cmd:
                if switch.pending is None or switch.pending.seq != frame_seq:
                    self._append_log("忽略过期或不匹配的参数响应 SEQ=0x%02X" % frame_seq)
                    return
        if rd.ack_cmd in (p.CMD_START, p.CMD_STOP):
            if self._control_pending != (frame_seq, rd.ack_cmd):
                self._append_log("忽略过期运行控制响应 SEQ=0x%02X" % frame_seq)
                return
            self._control_pending = None
            self._control_timer.stop()
            expected = p.RUN_RUN if rd.ack_cmd == p.CMD_START else p.RUN_IDLE
            if rd.status == p.STATUS_OK and rd.run_state == expected:
                self.hold.set_mode(proc.FrameHold.MODE_RUN if rd.ack_cmd == p.CMD_START
                                   else proc.FrameHold.MODE_HOLD)
            else:
                self.hold.set_mode(proc.FrameHold.MODE_HOLD)
                self._append_error("设备未确认请求的运行状态，显示保持暂停")
        if rd.ack_cmd == p.CMD_SINGLE:
            if self._single_pending_seq != frame_seq:
                self._append_log("忽略过期单次响应 SEQ=0x%02X" % frame_seq)
                return
            self._single_pending_seq = None
            self._single_timer.stop()
            if rd.status == p.STATUS_OK and rd.run_state == p.RUN_SINGLE_BUSY:
                self.hold.arm_single()
            else:
                self.hold.set_mode(proc.FrameHold.MODE_HOLD)
                self._append_error("单次采集被设备拒绝：%s" % p.STATUS_TEXT.get(rd.status, "未知状态"))
        self._device_run_state = rd.run_state
        # 协议要求 status != OK 时仍填设备真实值，据此回滚显示
        self._current_vref = rd.vref_mv
        self._current_rate = rd.actual_rate_hz

        if self.rate_switch.is_pending() and rd.ack_cmd == p.CMD_SET_RATE:
            ok, msg = self.rate_switch.check(frame_seq, rd)
            if ok:
                self.sim.set_rate(rd.actual_rate_hz)
                self.rate_status.setText("采样率 %d S/s（已生效）" % rd.actual_rate_hz)
                self._append_log("采样率切换已确认：%s" % msg)
            else:
                self.rate_switch.cancel()
                self.rate_status.setText("切换失败：%s" % msg)
                self._rollback_combo(self.rate_combo, self._current_rate)
                self._append_error("采样率切换失败：%s" % msg)
            self._update_switch_controls()
            self._refresh_labels()
            return

        if self.vref_switch.is_pending() and rd.ack_cmd == p.CMD_SET_VREF:
            ok, msg = self.vref_switch.check(frame_seq, rd)
            if ok:
                self.sim.set_vref(rd.vref_mv)
                self.vref_status.setText("设备参考电压：%d mV（已生效）" % rd.vref_mv)
                self._append_log("参考电压切换已确认：%s" % msg)
            else:
                self.vref_switch.cancel()
                self.vref_status.setText("切换失败：%s" % msg)
                self.vref_spin.blockSignals(True)
                self.vref_spin.setValue(self._current_vref)
                self.vref_spin.blockSignals(False)
                self._append_error("参考电压切换失败：%s" % msg)
            self._update_switch_controls()
            self._refresh_labels()
            return

        self._append_log("收到响应：ack=0x%02X status=0x%02X rate=%d vref=%d"
                         % (rd.ack_cmd, rd.status, rd.actual_rate_hz, rd.vref_mv))
        self._refresh_labels()

    def _on_switch_timeout(self) -> None:
        if self.rate_switch.is_pending():
            self.rate_switch.cancel()
            self.rate_status.setText("切换失败：设备无响应（超时），已回滚")
            self._rollback_combo(self.rate_combo, self._current_rate)
            self._append_error("采样率切换超时，保持旧状态")
        if self.vref_switch.is_pending():
            self.vref_switch.cancel()
            self.vref_spin.setValue(self._current_vref)
            self.vref_status.setText("切换失败：设备无响应（超时），已回滚")
            self._append_error("参考电压切换超时，保持旧状态")
        self._update_switch_controls()
        self._refresh_labels()

    def _update_switch_controls(self) -> None:
        self.rate_combo.setEnabled(not self.rate_switch.is_pending())
        self.vref_spin.setEnabled(not self.vref_switch.is_pending())
        self.btn_vref_apply.setEnabled(not self.vref_switch.is_pending())

    def _rollback_combo(self, combo: QComboBox, value: int) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)

    def _next_seq(self) -> int:
        s = self._seq & 0xFF
        self._seq += 1
        return s

    # ------------------------------------------------------------------
    # 串口连接
    # ------------------------------------------------------------------
    def _refresh_ports(self) -> None:
        self.port_combo.clear()
        ports = enumerate_ports()
        if not ports:
            self.port_combo.addItem("（未发现串口，可用模拟模式）", None)
        for dev, desc in ports:
            self.port_combo.addItem("%s - %s" % (dev, desc), dev)
        self._append_log("刷新串口：发现 %d 个" % len(ports))

    def _on_connect(self) -> None:
        if not has_serial():
            self._append_error("pyserial 未安装，无法连接真实设备")
            return
        port = self.port_combo.currentData()
        if not port:
            self._append_error("没有可用串口，请刷新端口或使用模拟模式")
            return
        self._sim_timer.stop()
        self.parser.reset()
        self._counts = self.parser.counts.as_dict()
        self._cancel_pending()
        self._display = None
        self._redraw_all()
        self.mode = "connecting"
        self._ever_connected = False
        self._update_status()
        if not self.worker.connect_to(port):
            self._append_error("串口线程已在运行")
            return
        self._append_log("正在连接 %s …" % port)

    def _on_connected(self, info: str) -> None:
        if self.mode != "connecting":
            return
        self.mode = "real"
        self._ever_connected = True
        self._last_frame_is_history = False
        self.hold.set_mode(proc.FrameHold.MODE_RUN)
        self.worker.send(p.build_query_status(self._next_seq()))
        self.btn_connect.setEnabled(False)
        self._append_log(info)
        self._update_status()
        self._refresh_labels()

    def _on_disconnected(self, reason: str) -> None:
        if self.mode not in ("real", "connecting"):
            return
        self._on_disconnect(reason)

    def _on_disconnect_clicked(self) -> None:
        self.worker.request_stop()
        self._on_disconnect("用户主动断开")

    def _on_disconnect(self, reason: str) -> None:
        """断线：冻结最后一帧、标注历史数据，绝不继续刷新假装在线。"""
        self._sim_timer.stop()
        self._cancel_pending()
        self._last_frame_is_history = True
        self.hold.set_mode(proc.FrameHold.MODE_HOLD)
        if self._ever_connected or self._display is not None:
            self.mode = "disconnected"
        else:
            self.mode = "idle"
        self.btn_connect.setEnabled(True)
        self._append_error("断线：%s" % reason)
        self.statusBar().showMessage("已断开 · 最后一帧为历史数据")
        self._update_status()
        self._refresh_labels()

    # ------------------------------------------------------------------
    # 模拟模式
    # ------------------------------------------------------------------
    def start_simulation(self) -> None:
        """启动明确标记的模拟模式；模拟帧走与真实数据一致的处理链路。"""
        self._sim_timer.stop()
        if not self.worker.stop_and_wait():
            self._append_error("串口线程未退出，不能切换数据源")
            return
        self._cancel_pending()
        self.mode = "sim"
        self._ever_connected = False
        self.parser.reset()
        self._counts = self.parser.counts.as_dict()
        self._errors.clear()
        self._semantic_discards = 0
        self._display = None
        self._last_frame_is_history = False
        self.hold.set_mode(proc.FrameHold.MODE_RUN)
        scene = self.sim.scene
        self.sim = Simulator(scene, self._current_rate, self._current_vref, 1024)
        self.btn_connect.setEnabled(False)
        self._sim_timer.start()
        self._append_log("已进入模拟模式（模拟数据，非真实测量）")
        self._update_status()
        self._refresh_labels()
        self._redraw_all()

    def _sim_tick(self) -> None:
        if self.mode != "sim":
            return
        raw = self.sim.next_frame_bytes()
        if raw is None:
            self._on_disconnect("模拟场景：断线（不产帧）")
            return
        self._feed_bytes(raw)

    def _on_scene_changed(self, index: int) -> None:
        key = self.scene_combo.currentData()
        if key:
            self.sim.set_scene(key)
            self._append_log("模拟场景切换为：%s" % SCENE_NAMES.get(key, key))

    def set_sim_scene(self, key: str) -> None:
        """公开方法：供自动化脚本选择场景（与 UI 下拉一致）。"""
        idx = self.scene_combo.findData(key)
        if idx >= 0:
            self.scene_combo.setCurrentIndex(idx)
        else:
            self.sim.set_scene(key)

    # ------------------------------------------------------------------
    # 运行 / 暂停 / 单次
    # ------------------------------------------------------------------
    def resume(self) -> None:
        self._cancel_single()
        if self.mode == "real":
            self._request_control(p.CMD_START)
        else:
            self.hold.set_mode(proc.FrameHold.MODE_RUN)
        self._append_log("运行（连续显示）")
        self._refresh_labels()

    def pause(self) -> None:
        self._cancel_single()
        self.hold.set_mode(proc.FrameHold.MODE_HOLD)
        if self.mode == "real":
            self._request_control(p.CMD_STOP)
        self._append_log("暂停（保持当前帧，后台新帧不覆盖）")
        self._refresh_labels()

    def single_capture(self) -> None:
        self._cancel_single()
        self._control_pending = None
        self._control_timer.stop()
        self._display = None
        self._redraw_all()
        if self.mode == "real":
            self._request_real_single()
        else:
            self.hold.arm_single()
        self._append_log("单次采集：等待一帧后自动冻结")
        self._refresh_labels()

    def _request_real_single(self) -> None:
        self.hold.set_mode(proc.FrameHold.MODE_HOLD)
        self._single_pending_seq = self._next_seq()
        self.worker.send(p.build_single(self._single_pending_seq))
        self._single_timer.start()

    def _cancel_single(self) -> None:
        self._single_pending_seq = None
        self._single_timer.stop()
        self._waiting_trigger = False

    def _cancel_pending(self) -> None:
        self._cancel_single()
        self._control_pending = None
        self._device_run_state = None
        self._control_timer.stop()
        self._switch_timer.stop()
        self.rate_switch.cancel()
        self.vref_switch.cancel()
        self._update_switch_controls()

    def _on_single_timeout(self) -> None:
        self._cancel_single()
        self.hold.set_mode(proc.FrameHold.MODE_HOLD)
        self._append_error("单次采集未获设备确认（超时），请重试")
        self._refresh_labels()

    def _request_control(self, cmd: int) -> None:
        self.hold.set_mode(proc.FrameHold.MODE_HOLD)
        seq = self._next_seq()
        self._control_pending = (seq, cmd)
        self.worker.send(p.build_command(cmd, seq))
        self._control_timer.start()

    def _on_control_timeout(self) -> None:
        self._control_pending = None
        self._device_run_state = None
        self.hold.set_mode(proc.FrameHold.MODE_HOLD)
        self._append_error("运行控制无设备确认（超时），显示保持暂停，设备状态未知")
        self._refresh_labels()

    # ------------------------------------------------------------------
    # 触发 / 缩放 / 采样率 / 参考电压
    # ------------------------------------------------------------------
    def _on_trigger_mode_changed(self, index: int) -> None:
        self._trigger_mode = self.trigger_combo.currentData()
        if _HAS_PG:
            self.curve.setData([], [])   # 切到触发模式先清空，等待对齐帧
        self._append_log("触发方式：%s" % ("上升沿触发" if self._trigger_mode == "rising" else "自动显示"))
        self._redraw_waveform()

    def _on_threshold_changed(self, value: float) -> None:
        self._trigger_threshold = float(value)
        self._redraw_waveform()

    def _zoom_view(self, factor: float) -> None:
        if _HAS_PG:
            self.plot.getViewBox().scaleBy((factor, factor))

    def zoom_in(self) -> None:
        self._zoom_view(0.8)

    def zoom_out(self) -> None:
        self._zoom_view(1.25)

    def reset_view(self) -> None:
        if _HAS_PG:
            self.plot.autoRange()
            self.plot.setYRange(-0.2, 3.5)
        self._append_log("复位视图")

    def _on_rate_changed(self, index: int) -> None:
        rate = self.rate_combo.currentData()
        if rate is None:
            return
        self._request_rate(int(rate))

    def _request_rate(self, rate: int) -> None:
        if self.mode not in ("sim", "real"):
            self._append_error("未连接设备，无法切换采样率")
            self._rollback_combo(self.rate_combo, self._current_rate)
            return
        if self.rate_switch.is_pending():
            return
        seq = self._next_seq()
        self.rate_switch.arm(seq, p.CMD_SET_RATE, rate, "采样率")
        self.rate_status.setText("切换中…（等待设备确认）")
        self._update_switch_controls()
        self._switch_timer.start()
        if self.mode == "sim":
            # 模拟设备延迟应答：应答帧字节仍走统一 FrameParser → 响应处理链
            QTimer.singleShot(150, lambda: self._feed_bytes(
                self.sim.build_rate_response(seq, rate)))
        else:
            self.worker.send(p.build_set_rate(rate, seq))
        self._append_log("请求切换采样率 → %d S/s（SEQ=0x%02X），显示'切换中'" % (rate, seq))

    def set_sample_rate(self, rate_hz: int) -> None:
        """公开方法：供自动化脚本触发采样率切换（走完整确认流程）。"""
        self._rollback_combo(self.rate_combo, rate_hz)   # 更新下拉（会触发回调）
        self._request_rate(rate_hz)

    def _on_vref_apply(self) -> None:
        mv = int(self.vref_spin.value())
        self._request_vref(mv)

    def _request_vref(self, mv: int) -> None:
        if self.mode not in ("sim", "real"):
            self._append_error("未连接设备，无法修改参考电压")
            return
        if self.vref_switch.is_pending():
            return
        seq = self._next_seq()
        self.vref_switch.arm(seq, p.CMD_SET_VREF, mv, "参考电压")
        self.vref_status.setText("切换中…（等待设备确认）")
        self._update_switch_controls()
        self._switch_timer.start()
        if self.mode == "sim":
            QTimer.singleShot(150, lambda: self._feed_bytes(
                self.sim.build_vref_response(seq, mv)))
        else:
            self.worker.send(p.build_set_vref(mv, seq))
        self._append_log("请求修改参考电压 → %d mV（SEQ=0x%02X）" % (mv, seq))

    def set_vref(self, vref_mv: int) -> None:
        """公开方法：供自动化脚本触发参考电压切换。"""
        self.vref_spin.setValue(vref_mv)
        self._request_vref(vref_mv)

    # ------------------------------------------------------------------
    # CSV 导出
    # ------------------------------------------------------------------
    def export_csv(self, path: str):
        """导出当前显示帧。返回 (成功, 说明)。文件头注明帧间不连续。"""
        d = self._display
        if d is None:
            return False, "无数据可导出"
        src = d["source"]  # 来源属于采集帧，断线/切换模式不得把模拟帧标成真实测量
        if self._last_frame_is_history:
            src += "（历史数据）"
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write("# 帧间不连续，时间轴为帧内相对时间（每帧从 0 起）\n")
                f.write("# 说明：本文件只含一帧；帧间空隙未隐藏成连续采样\n")
                f.write("# sample_rate_hz,%d\n" % d["sample_rate_hz"])
                f.write("# vref_mv,%d\n" % d["vref_mv"])
                f.write("# frame_seq,%d\n" % d["seq"])
                f.write("# point_count,%d\n" % d["point_count"])
                f.write("# data_source,%s\n" % src)
                f.write("# voltage_formula,code/4095.0*(vref_mv/1000.0)\n")
                f.write("# resolution_note,1LSB≈%.4fmV@3.3V;显示3位小数不冒充精度\n"
                        % proc.LSB_MV_AT_3300)
                f.write("frame_seq,index,time_s,adc_code,voltage_v\n")
                for i, (t, c, v) in enumerate(zip(d["times"], d["codes"], d["volts"])):
                    f.write("%d,%d,%.9f,%d,%.6f\n" % (d["seq"], i, t, c, v))
            self._append_log("CSV 已导出：%s（%d 点，帧序号 %d）"
                             % (path, d["point_count"], d["seq"]))
            return True, path
        except Exception as e:
            self._append_error("CSV 导出失败：%s" % e)
            return False, str(e)

    def _on_export_clicked(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", "waveform.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        ok, msg = self.export_csv(path)
        self.statusBar().showMessage(("已导出 " + msg) if ok else ("导出失败：" + msg))

    # ------------------------------------------------------------------
    # 绘制 / 刷新
    # ------------------------------------------------------------------
    def _redraw_all(self) -> None:
        self._redraw_waveform()
        self._refresh_labels()

    def _redraw_waveform(self) -> None:
        d = self._display
        if not _HAS_PG:
            return
        if d is None:
            self.curve.setData([], [])
            self.lbl_trigger_state.setText("触发：--")
            return
        volts, times = d["volts"], d["times"]
        if self._trigger_mode == "rising":
            tr = proc.find_rising_trigger(volts, self._trigger_threshold,
                                          d["sample_rate_hz"])
            if tr is None:
                # 不伪造预触发数据；保留上一帧波形，明确显示"等待触发/未触发"
                self.lbl_trigger_state.setText(
                    "触发：等待触发 / 未触发（阈值 %.3f V，保持上一帧）"
                    % self._trigger_threshold)
                return
            x = [t * 1000.0 for t in tr.times]
            y = tr.volts
            self.lbl_trigger_state.setText(
                "触发：上升沿已触发（索引 %d，阈值 %.3f V）"
                % (tr.trigger_index, self._trigger_threshold))
        else:
            x = [t * 1000.0 for t in times]
            y = volts
            self.lbl_trigger_state.setText("触发：自动显示")
        self.curve.setData(x, y)

    def _data_status_text(self) -> str:
        if self.mode == "idle":
            return "等待设备"
        if self.mode == "disconnected":
            return "已断开（历史数据）"
        if self._single_pending_seq is not None:
            return "单次采集：等待设备确认"
        if self._display is None:
            return "等待触发 / 未触发" if self._waiting_trigger else "等待数据"
        if self._last_frame_is_history:
            return "历史数据（已冻结）"
        if self._waiting_trigger:
            return "等待触发 / 未触发"
        if self.hold.mode == proc.FrameHold.MODE_HOLD:
            return "保持（暂停，不刷新）"
        if self.hold.mode == proc.FrameHold.MODE_SINGLE:
            return "单次冻结"
        return "实时刷新 · %s" % ("模拟数据" if self.mode == "sim" else "真实设备")

    def _run_state_text(self) -> str:
        display = {
            proc.FrameHold.MODE_RUN: "运行",
            proc.FrameHold.MODE_HOLD: "暂停（保持）",
            proc.FrameHold.MODE_SINGLE: "单次冻结",
        }.get(self.hold.mode, "--")
        if self._control_pending is not None:
            return "显示保持；设备控制待确认"
        if self.mode == "real":
            device = {p.RUN_IDLE: "IDLE", p.RUN_RUN: "RUN",
                      p.RUN_SINGLE_BUSY: "SINGLE"}.get(self._device_run_state, "未知")
            return "显示%s；设备回报 %s" % (display, device)
        return display

    def _error_total(self) -> int:
        keys = ("CRC 错误", "非法长度", "版本不匹配", "未知类型",
                "超时帧", "缓冲区溢出")
        return sum(int(self._counts.get(k, 0)) for k in keys) + self._semantic_discards

    def _refresh_labels(self) -> None:
        d = self._display
        src = d["source"] if d else "--"
        if self._last_frame_is_history:
            src += "（历史数据）"

        self.lbl_rate.setText("采样率：%d S/s" % self._current_rate)
        self.lbl_frame_len.setText("帧长度：%s 点" % (d["point_count"] if d else "--"))
        self.lbl_data_status.setText("数据状态：" + self._data_status_text())
        self.lbl_frame_seq.setText("帧序号：%s" % (d["seq"] if d else "--"))
        self.lbl_error_count.setText("错误计数：%d" % self._error_total())
        self.lbl_run_state.setText("运行：%s" % self._run_state_text())
        self.lbl_vref.setText("设备参考电压：%d mV（仅本次运行有效，掉电不保存）"
                              % self._current_vref)

        if d:
            s = d["stats"]
            self.big_number.setText("%.3f" % s.mean)
            self.vm_mean.setText("平均：%.3f V" % s.mean)
            self.vm_min.setText("最小：%.3f V" % s.vmin)
            self.vm_max.setText("最大：%.3f V" % s.vmax)
            self.vm_pp.setText("峰峰值：%.3f V" % s.vpp)
            self.vm_count.setText("点数：%d（采样率 %d S/s）"
                                  % (d["point_count"], d["sample_rate_hz"]))
            fr = d["freq"]
            self.lbl_freq.setText("频率：%s" %
                                  ("%.2f Hz" % fr.hz if fr.ok else "不可用（%s）" % fr.reason))
        else:
            self.big_number.setText("--.---")
            self.vm_mean.setText("平均：-- V")
            self.vm_min.setText("最小：-- V")
            self.vm_max.setText("最大：-- V")
            self.vm_pp.setText("峰峰值：-- V")
            self.vm_count.setText("点数：--")
            self.lbl_freq.setText("频率：--")

        self.lbl_src.setText("数据来源：" + src)
        self.lbl_vref_vm.setText(
            "设备参考电压：%d mV（仅本次运行有效，掉电不保存）" % self._current_vref)
        self.log_view.setPlainText("\n".join(self._log_lines[-8:]))

    def _update_status(self) -> None:
        if self.mode == "sim":
            text, color = "● 模拟数据（非真实测量）", "#ffb74d"
        elif self.mode == "real":
            text, color = "● 真实设备（串口在线）", "#69f0ae"
        elif self.mode == "disconnected":
            text, color = "● 已断开（最后一帧为历史数据，不再刷新）", "#ef5350"
        else:
            text, color = "● 等待设备（未收到任何帧）", "#9aa3b5"
        self.state_label.setText(text)
        self.state_label.setStyleSheet(
            "QLabel { color:%s; background-color:#14161c; font-size:12pt;"
            " font-weight:bold; padding:6px 10px; border:1px solid #3a3f4d;"
            " border-radius:5px; }" % color)

    # ------------------------------------------------------------------
    # 日志 / 视图切换 / 退出
    # ------------------------------------------------------------------
    def _append_log(self, text: str) -> None:
        line = "[INFO] " + text
        self._log_lines.append(line)
        self._log_lines = self._log_lines[-200:]
        print(line, flush=True)

    def _append_error(self, text: str) -> None:
        line = "[ERROR] " + text
        self._log_lines.append(line)
        self._log_lines = self._log_lines[-200:]
        self._errors.append(text)
        self._errors = self._errors[-20:]
        print(line, flush=True)

    def show_oscilloscope(self) -> None:
        self.tabs.setCurrentIndex(1)

    def show_voltmeter(self) -> None:
        self.tabs.setCurrentIndex(0)

    def stop(self) -> None:
        """停止定时器与串口线程（关闭窗口前调用）。"""
        self._sim_timer.stop()
        self._switch_timer.stop()
        self._single_timer.stop()
        self._control_timer.stop()
        self.worker.stop_and_wait(3000)
        self._append_log("已停止模拟定时器与串口线程")

    def closeEvent(self, event) -> None:
        self._sim_timer.stop()
        self._switch_timer.stop()
        self._single_timer.stop()
        self._control_timer.stop()
        ok = self.worker.stop_and_wait(3000)
        self._append_log("窗口关闭：串口线程停止=%s，串口已释放" % ok)
        event.accept()


def ensure_offscreen_fonts() -> None:
    """offscreen 平台初始字体库为空，中文和数字都会渲染成方框。

    仅在 offscreen 且字体库为空时，从 Windows 字体目录加载系统字体；
    真实桌面会话完全不受影响。供 main.py / gui_smoke.py 在创建
    QApplication 之后调用（QFontDatabase 需要 app 实例）。
    """
    from PySide6.QtGui import QFont, QFontDatabase
    app = QApplication.instance()
    if app is None or app.platformName() != "offscreen":
        return
    if QFontDatabase.families():
        return
    base = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc",
                 "arial.ttf", "segoeui.ttf"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)
    fams = QFontDatabase.families()
    if fams:
        family = "Microsoft YaHei" if "Microsoft YaHei" in fams else fams[0]
        app.setFont(QFont(family, 10))
        print("[INFO] offscreen 已加载字体：%s（共 %d 个）" % (family, len(fams)),
              flush=True)
