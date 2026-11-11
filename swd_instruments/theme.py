"""界面主题：深色仪器面板的调色板与样式表。只影响外观，不含任何硬件逻辑。"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette

BG = '#0e1217'
PANEL = '#151a21'
CARD = '#1a2029'
INSET = '#11161c'
BORDER = '#2b3440'
TEXT = '#e7ebf1'
MUTED = '#8d98a8'
ACCENT = '#3fd0c2'
SIM = '#6aa9ff'      # 模拟数据
REAL = '#4cc38a'     # 真实测量 / 读回确认
AMBER = '#f1b547'    # 历史数据、注意事项
RED = '#f0655f'      # 状态未知、失败

MONO = '"Cascadia Mono", "Consolas", "Courier New", monospace'


def build_palette() -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.Window: BG,
        QPalette.WindowText: TEXT,
        QPalette.Base: INSET,
        QPalette.AlternateBase: CARD,
        QPalette.Text: TEXT,
        QPalette.Button: '#252d38',
        QPalette.ButtonText: TEXT,
        QPalette.ToolTipBase: CARD,
        QPalette.ToolTipText: TEXT,
        QPalette.PlaceholderText: '#5d6877',
        QPalette.Highlight: '#2aa89c',
        QPalette.HighlightedText: '#061311',
        QPalette.Link: ACCENT,
        QPalette.Light: '#344050',
        QPalette.Midlight: '#2b3440',
        QPalette.Mid: '#222a34',
        QPalette.Dark: '#0a0d11',
        QPalette.Shadow: '#000000',
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor('#5d6877'))
    return palette


def ui_font() -> QFont:
    font = QFont('Microsoft YaHei UI', 10)
    font.setStyleHint(QFont.SansSerif)
    return font


STYLESHEET = f"""
QWidget#root {{ background: {BG}; }}
QFrame#header {{ background: {PANEL}; border: none; border-bottom: 1px solid {BORDER}; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#tile {{ background: {PANEL}; border: 1px solid #262e39; border-radius: 8px; }}
QFrame#footer {{ background: {PANEL}; border: none; border-top: 1px solid {BORDER};
    border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; }}

QLabel[role="appTitle"] {{ font-size: 19px; font-weight: 600; color: {TEXT}; }}
QLabel[role="muted"] {{ color: {MUTED}; font-size: 12px; }}
QLabel[role="cardTitle"] {{ font-size: 14px; font-weight: 600; color: #d5dce6; }}
QLabel[role="caution"] {{ color: {AMBER}; font-size: 12px; }}
QLabel[role="status"] {{ color: #c3ccd8; font-size: 12px; }}

QLabel#reading {{ font-family: {MONO}; font-size: 62px; font-weight: 600; color: #5d6877; }}
QLabel#reading[kind="sim"] {{ color: {SIM}; }}
QLabel#reading[kind="real"] {{ color: {REAL}; }}
QLabel#statValue {{ font-family: {MONO}; font-size: 19px; color: {TEXT}; }}
QLabel#pulseHigh {{ font-family: {MONO}; font-size: 21px; color: {REAL}; }}
QLabel#stripValue {{ font-family: {MONO}; font-size: 30px; font-weight: 600; color: {REAL}; }}
QLabel#stripValue[kind="none"] {{ color: #5d6877; font-size: 22px; }}
QLabel#miniValue {{ font-family: {MONO}; font-size: 15px; color: {TEXT}; }}
QLabel#contPill {{ border-radius: 10px; padding: 10px 0; font-size: 22px; font-weight: 700;
    background: #1b2129; color: #5d6877; border: 1px solid #262e39; }}
QLabel#contPill[state="closed"] {{ background: rgba(76,195,138,0.16); color: {REAL}; border-color: rgba(76,195,138,0.55); }}
QLabel#contPill[state="open"] {{ background: #252d38; color: #c3ccd8; border-color: #415062; }}
QLabel#contPill[state="unknown"] {{ background: rgba(241,181,71,0.14); color: {AMBER}; border-color: rgba(241,181,71,0.5); }}
QLabel[role="pinSummary"] {{ color: #c3ccd8; font-size: 12px; }}
QLabel[role="formLabel"] {{ color: #c3ccd8; font-size: 13px; }}
QFrame#pinSummaryBox {{ background: {INSET}; border: 1px solid {BORDER}; border-radius: 8px; }}
QFrame#notesBox {{ background: rgba(241,181,71,0.06); border: 1px solid rgba(241,181,71,0.28); border-radius: 8px; }}
QPushButton#linkButton {{ background: transparent; border: none; padding: 2px 0; color: {AMBER};
    font-size: 12px; text-align: left; min-height: 0; }}
QPushButton#linkButton:hover {{ color: #f6c86a; background: transparent; }}
QDialog#pinDialog {{ background: {BG}; }}
QGroupBox#pinGroup {{ border: none; margin: 0; padding: 0; }}
QListWidget#occupiedGrid {{ background: {INSET}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px; color: {TEXT}; outline: none; }}
QListWidget#occupiedGrid::item {{ padding: 2px 4px; border-radius: 6px; }}
QListWidget#occupiedGrid::item:hover {{ background: #1f2630; }}

QLabel#badge {{ border-radius: 13px; padding: 5px 14px; font-weight: 600; font-size: 12px;
    background: #252d38; color: #a9b4c2; border: 1px solid #344050; }}
QLabel#badge[kind="sim"] {{ background: rgba(106,169,255,0.14); color: {SIM}; border-color: rgba(106,169,255,0.5); }}
QLabel#badge[kind="real"] {{ background: rgba(76,195,138,0.14); color: {REAL}; border-color: rgba(76,195,138,0.5); }}
QLabel#badge[kind="history"] {{ background: rgba(241,181,71,0.12); color: {AMBER}; border-color: rgba(241,181,71,0.45); }}
QLabel#badge[kind="warn"] {{ background: rgba(240,101,95,0.16); color: {RED}; border-color: rgba(240,101,95,0.6); }}

QLabel#chip {{ border-radius: 9px; padding: 2px 9px; font-size: 11px;
    background: #252d38; color: #a9b4c2; }}
QLabel#chip[state="busy"] {{ background: rgba(63,208,194,0.16); color: {ACCENT}; }}
QLabel#chip[state="ok"] {{ background: rgba(76,195,138,0.16); color: {REAL}; }}
QLabel#chip[state="on"] {{ background: rgba(241,181,71,0.18); color: {AMBER}; }}
QLabel#chip[state="fail"] {{ background: rgba(240,101,95,0.18); color: {RED}; }}

QPushButton {{ background: #252d38; border: 1px solid #344050; border-radius: 6px;
    padding: 6px 12px; min-height: 20px; color: {TEXT}; }}
QPushButton:hover {{ background: #2d3744; border-color: #415062; }}
QPushButton:pressed {{ background: #202733; }}
QPushButton[variant="primary"] {{ background: #2aa89c; border-color: {ACCENT}; color: #061311; font-weight: 600; }}
QPushButton[variant="primary"]:hover {{ background: {ACCENT}; }}
QPushButton[variant="primary"]:pressed {{ background: #249287; }}
QPushButton[variant="danger"] {{ background: transparent; border-color: rgba(240,101,95,0.7); color: {RED}; }}
QPushButton[variant="danger"]:hover {{ background: rgba(240,101,95,0.14); }}
QPushButton:disabled, QPushButton[variant="primary"]:disabled, QPushButton[variant="danger"]:disabled {{
    background: #1b2129; border-color: #262e39; color: #566170; font-weight: 400; }}

QLineEdit, QPlainTextEdit {{ background: {INSET}; border: 1px solid {BORDER}; border-radius: 6px;
    padding: 5px 8px; color: {TEXT}; selection-background-color: #2aa89c; selection-color: #061311; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QPlainTextEdit {{ font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI"; font-size: 12px; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; background: {PANEL}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 8px 16px; border: none;
    border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:hover {{ color: #c9d1dc; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #303a47; border-radius: 4px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #415062; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}

QProgressBar {{ background: {INSET}; border: 1px solid {BORDER}; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:vertical {{ height: 8px; }}
QStatusBar {{ background: {PANEL}; color: #a9b4c2; border-top: 1px solid {BORDER}; }}
QToolTip {{ background: {CARD}; color: {TEXT}; border: 1px solid #415062; padding: 4px 6px; }}
"""
