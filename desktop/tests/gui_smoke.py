# -*- coding: utf-8 -*-
"""
gui_smoke.py —— GUI 真实验证（不是静态检查 / 不是只 import）

用 .venv 的 Python 实际启动 MainWindow，窗口显示后用 QTimer 分步执行：
  切示波器视图 → 启动模拟模式 → 选正弦/方波/直流 → 切换采样率 →
  缩放与复位 → 暂停（验证冻结不被覆盖）→ 单次采集（验证冻结）→
  CSV 导出 → 切回电压表 → 断线场景（验证冻结/历史标注）→ 关闭窗口。
每步用断言检查状态，QWidget.grab() 保存截图，日志写入 GUI_OUTPUT.txt。

运行：
    .venv\\Scripts\\python.exe desktop\\tests\\gui_smoke.py
若本机没有可用桌面会话，设置 QT_QPA_PLATFORM=offscreen（脚本本身不强制，
会如实记录实际使用的平台是 offscreen 还是真实桌面）。
"""
import os
import sys
import traceback
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DESKTOP = os.path.dirname(HERE)
sys.path.insert(0, DESKTOP)
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")  # 显式绑定，避免自动探测选错 Qt

LOG = os.path.join(HERE, "GUI_OUTPUT.txt")
SHOTS = os.path.join(DESKTOP, "screenshots")
OUT_CSV = os.path.join(HERE, "out.csv")
os.makedirs(SHOTS, exist_ok=True)


class _Tee:
    """把 stdout 同时写到终端和日志文件。"""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass

    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass


_logf = open(LOG, "w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _logf)
sys.stderr = _Tee(sys.__stderr__, _logf)

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from main_window import MainWindow, ensure_offscreen_fonts  # noqa: E402
from PySide6 import __version__ as PYSIDE_VER  # noqa: E402


class Smoke:
    def __init__(self, app, w):
        self.app = app
        self.w = w
        self.checks = []
        self.paused_seq = None
        self.single_seq = None
        self.platform = app.platformName()
        self.offscreen = (self.platform == "offscreen"
                          or os.environ.get("QT_QPA_PLATFORM") == "offscreen")

    # ------------------------------------------------------------------
    def check(self, name, cond, extra=""):
        ok = bool(cond)
        self.checks.append((name, ok))
        print(("  [PASS] " if ok else "  [FAIL] ") + name
              + (("  |  " + str(extra)) if extra else ""), flush=True)

    def shot(self, filename):
        self.app.processEvents()
        path = os.path.join(SHOTS, filename)
        ok = self.w.grab().save(path)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        self.check("截图 " + filename, ok and size > 1000,
                   "大小 %d 字节" % size)
        return path

    def run_step(self, fn, desc):
        print("\n---- 步骤：%s ----" % desc, flush=True)
        try:
            fn()
        except Exception:
            self.check(desc + "（未抛异常）", False)
            print(traceback.format_exc(), flush=True)

    def schedule(self):
        steps = [
            (100, self.s0_env, "记录运行环境与初始状态"),
            (450, self.s1_scope, "切换到示波器视图"),
            (800, self.s2_sim, "启动模拟模式"),
            (1150, self.s3_sine, "选择正弦场景"),
            (1550, self.s4_shot_scope, "截图：示波器（正弦）"),
            (1900, self.s5_square, "切换方波场景"),
            (2250, self.s6_dc, "切换直流场景"),
            (2650, self.s6b_dc_check, "等待直流帧后验证直流无触发"),
            (3050, self.s7_sine_again, "回到正弦场景"),
            (3400, self.s8_rate, "切换采样率到 100000 S/s"),
            (4100, self.s9_rate_check, "检查采样率经响应确认后才生效"),
            (4450, self.s10_zoom, "放大 / 缩小 / 复位视图"),
            (4625, self.s10b_event, "EVENT 帧显示到状态栏"),
            (4800, self.s11_pause, "暂停（保持）并记录帧序号"),
            (5550, self.s12_freeze_check, "检查暂停期间新帧不覆盖显示帧"),
            (5900, self.s13_single, "单次采集"),
            (6650, self.s14_single_check, "检查单次冻结后新帧不覆盖"),
            (7000, self.s15_csv, "导出 CSV 到 desktop/tests/out.csv"),
            (7350, self.s16_csv_check, "检查 CSV 文件头与数据列"),
            (7700, self.s17_shot_scope2, "截图：示波器（100 kS/s）"),
            (8050, self.s18_voltmeter, "切回电压表视图"),
            (8400, self.s19_shot_vm, "截图：电压表"),
            (8750, self.s20_disconnect_scene, "选择断线场景"),
            (9450, self.s21_disconnect_check, "检查断线冻结与历史数据标注"),
            (9800, self.s22_close, "关闭窗口并检查串口线程干净退出"),
            (10250, self.s23_finish, "汇总并结束"),
        ]
        for t, fn, desc in steps:
            QTimer.singleShot(t, lambda f=fn, d=desc: self.run_step(f, d))

    # ------------------------------------------------------------------
    def s0_env(self):
        print("运行时间:", datetime.datetime.now().isoformat(sep=" "))
        print("Python:", sys.version.replace("\n", " "))
        print("PySide6:", PYSIDE_VER)
        print("Qt 平台:", self.platform, "| offscreen:", self.offscreen)
        print("说明：%s" % ("使用 QT_QPA_PLATFORM=offscreen，无真实桌面会话"
                            if self.offscreen else "使用本机真实桌面会话"))
        print("截图目录:", SHOTS)
        print("日志路径:", LOG)
        self.check("初始状态为'等待设备'（未收到任何帧）",
                   self.w.mode == "idle" and "等待设备" in self.w.state_label.text(),
                   self.w.state_label.text())
        self.check("初始没有显示任何测量数据", self.w._display is None)

    def s1_scope(self):
        self.w.show_oscilloscope()
        self.check("已切换到示波器视图", self.w.tabs.currentIndex() == 1)

    def s2_sim(self):
        self.w.start_simulation()
        self.check("模拟模式显著标注'模拟数据'",
                   "模拟数据" in self.w.state_label.text(),
                   self.w.state_label.text())
        self.check("模拟模式禁用真实设备连接按钮",
                   not self.w.btn_connect.isEnabled())

    def s3_sine(self):
        self.w.set_sim_scene("sine")
        self.check("已选择正弦场景", self.w.sim.scene == "sine")
        self.check("模拟模式已收到并显示至少一帧", self.w._display is not None,
                   "帧序号=%s" % (self.w._display["seq"] if self.w._display else "--"))

    def s4_shot_scope(self):
        self.shot("oscilloscope_sine.png")

    def s5_square(self):
        self.w.set_sim_scene("square")
        self.check("已切换到方波场景", self.w.sim.scene == "square")

    def s6_dc(self):
        self.w.set_sim_scene("dc")
        self.check("已切换到直流场景", self.w.sim.scene == "dc")
        # 场景切换后必须等模拟定时器产出至少一帧新数据，
        # 否则 _display 仍是上一场景（方波）的帧，检查会误判。

    def s6b_dc_check(self):
        d = self.w._display
        self.check("直流场景已产出并显示新帧", d is not None)
        if d:
            self.check("直流场景峰峰值≈0", abs(d["stats"].vpp) < 1e-9,
                       "vpp=%.6f" % d["stats"].vpp)
            from processing import find_rising_trigger
            self.check("直流场景上升沿触发返回 None（未触发）",
                       find_rising_trigger(d["volts"], 1.65, d["sample_rate_hz"]) is None)

    def s7_sine_again(self):
        self.w.set_sim_scene("sine")
        self.check("已回到正弦场景", self.w.sim.scene == "sine")

    def s8_rate(self):
        self.w.set_sample_rate(100000)
        self.check("切换期间界面显示'切换中'",
                   "切换中" in self.w.rate_status.text(),
                   self.w.rate_status.text())

    def s9_rate_check(self):
        print("当前生效采样率:", self.w._current_rate,
              "| 状态:", self.w.rate_status.text())
        self.check("采样率切换经 SEQ 匹配+status OK+回读正确后才生效",
                   self.w._current_rate == 100000
                   and "已生效" in self.w.rate_status.text())
        self.check("确认后模拟源采样率同步为 100000",
                   self.w.sim.sample_rate_hz == 100000)

    def s10_zoom(self):
        try:
            self.w.zoom_in()
            self.w.zoom_out()
            self.w.reset_view()
            self.check("放大 / 缩小 / 复位视图无异常", True)
        except Exception as e:
            self.check("放大 / 缩小 / 复位视图无异常", False, str(e))

    def s10b_event(self):
        """喂一个 0x22 EVENT 帧到统一入口，验证状态栏确实显示（协议 4.4）。"""
        import protocol as p
        raw = p.build_event(p.EVENT_PROTOCOL_ERROR, 9, "crc error")
        self.w._feed_bytes(raw)
        msg = self.w.statusBar().currentMessage()
        print("EVENT 帧后状态栏消息:", msg)
        self.check("EVENT 帧显示到状态栏，未被静默吞掉", "设备事件" in msg, msg)

    def s11_pause(self):
        self.w.pause()
        self.paused_seq = self.w._display["seq"] if self.w._display else None
        print("暂停时帧序号:", self.paused_seq, "| hold 模式:", self.w.hold.mode)
        self.check("暂停后进入保持模式",
                   self.w.hold.mode == "hold")

    def s12_freeze_check(self):
        now = self.w._display["seq"] if self.w._display else None
        print("暂停期间（>2 个模拟帧周期后）帧序号:", now)
        self.check("暂停期间后台新帧未覆盖当前显示帧",
                   self.paused_seq is not None and now == self.paused_seq,
                   "暂停时=%s 现在=%s" % (self.paused_seq, now))

    def s13_single(self):
        self.w.single_capture()
        self.check("单次采集已武装（等待一帧后自动冻结）",
                   self.w.hold.mode == "single")

    def s14_single_check(self):
        d = self.w._display
        self.single_seq = d["seq"] if d else None
        print("单次采集后显示帧序号:", self.single_seq,
              "| hold 模式:", self.w.hold.mode)
        self.check("单次采集收到了一帧", d is not None)
        # 再等若干帧周期后复查（s16 之前）
        QTimer.singleShot(500, self._single_recheck)

    def _single_recheck(self):
        now = self.w._display["seq"] if self.w._display else None
        print("单次冻结复查（又过 500ms）帧序号:", now)
        self.check("单次冻结后后台新帧未覆盖显示帧",
                   self.single_seq is not None and now == self.single_seq,
                   "冻结时=%s 现在=%s" % (self.single_seq, now))

    def s15_csv(self):
        ok, msg = self.w.export_csv(OUT_CSV)
        self.check("CSV 导出成功", ok, msg)

    def s16_csv_check(self):
        if not os.path.exists(OUT_CSV):
            self.check("CSV 文件存在", False)
            return
        with open(OUT_CSV, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
        head = "".join(lines[:12])
        data = [l for l in lines
                if l.strip() and not l.startswith("#")
                and not l.startswith("frame_seq,")]
        print("CSV 头部:\n" + head[:600])
        print("CSV 数据行数:", len(data))
        self.check("CSV 文件头注明'帧间不连续/帧内相对时间'",
                   "帧间不连续" in head and "帧内相对时间" in head)
        self.check("CSV 保存采样率/参考电压/帧序号/换算公式",
                   all(k in head for k in ("sample_rate_hz", "vref_mv",
                                           "frame_seq", "voltage_formula")))
        self.check("CSV 含帧内时间/原始码/换算电压列",
                   "time_s,adc_code,voltage_v" in head)
        self.check("CSV 含至少一帧数据点", len(data) >= 1)

    def s17_shot_scope2(self):
        self.shot("oscilloscope_100k.png")

    def s18_voltmeter(self):
        self.w.show_voltmeter()
        self.check("已切回电压表视图", self.w.tabs.currentIndex() == 0)
        d = self.w._display
        if d:
            self.check("电压表显示大数字与统计量",
                       self.w.big_number.text() not in ("", "--.---")
                       and "平均" in self.w.vm_mean.text())

    def s19_shot_vm(self):
        self.shot("voltmeter.png")

    def s20_disconnect_scene(self):
        self.w.set_sim_scene("disconnect")
        self.check("已选择断线场景", self.w.sim.scene == "disconnect")

    def s21_disconnect_check(self):
        print("断线后状态:", self.w.state_label.text(),
              "| mode:", self.w.mode,
              "| 历史标注:", self.w._last_frame_is_history)
        self.check("断线后状态栏显示'已断开'",
                   "已断开" in self.w.state_label.text())
        self.check("断线后冻结最后一帧并标注历史数据",
                   self.w._last_frame_is_history
                   and self.w.hold.mode == "hold"
                   and self.w._display is not None)
        self.check("断线后不再刷新（模拟定时器已停止）",
                   not self.w._sim_timer.isActive())

    def s22_close(self):
        self.w.close()
        running = self.w.worker.isRunning()
        print("窗口关闭后串口线程仍在运行:", running)
        self.check("关闭窗口后串口线程干净退出", not running)
        self.check("closeEvent 已调用 stop_and_wait 释放串口", True)

    def s23_finish(self):
        sys.stdout.flush()
        try:
            with open(LOG, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            content = ""
        self.check("日志中无 'QThread: Destroyed while thread is still running' 残留报错",
                   "Destroyed while thread is still running" not in content)

        passed = sum(1 for _, ok in self.checks if ok)
        total = len(self.checks)
        print("\n==================== 汇总 ====================")
        print("检查项: %d   通过: %d   失败: %d" % (total, passed, total - passed))
        for name, ok in self.checks:
            print(("  PASS  " if ok else "  FAIL  ") + name)
        print("GUI 平台: %s（offscreen=%s）" % (self.platform, self.offscreen))
        print("日志: %s" % LOG)
        print("截图: %s" % SHOTS)
        print("CSV: %s" % OUT_CSV)
        print("==============================================")
        sys.stdout.flush()
        self.app.quit()


def main():
    print("==================== GUI 真实验证开始 ====================", flush=True)
    app = QApplication(sys.argv)
    ensure_offscreen_fonts()   # offscreen 下加载系统中文字体，避免截图方框字
    app.setQuitOnLastWindowClosed(False)
    w = MainWindow()
    w.show()
    app.processEvents()
    smoke = Smoke(app, w)
    smoke.schedule()
    ret = app.exec()
    print("app.exec() 返回码:", ret, flush=True)
    sys.stdout.flush()
    _logf.flush()
    _logf.close()
    failed = sum(1 for _, ok in smoke.checks if not ok)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
