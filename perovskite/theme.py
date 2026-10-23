"""Shared light desktop theme; no network assets or platform theme dependencies."""
from pathlib import Path
import re

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


COLORS_LIGHT = {
    '#244e63':'#1d1d1f', '#f5f7fa':'#f5f5f7', '#fff':'#ffffff', '#ffffff':'#ffffff',
    'white':'#ffffff', '#bdcbd5':'#d1d1d6', '#dcebf3':'#dbeafe', '#edf4f8':'#f0f0f4',
    '#8aafc3':'#aeaeb2', '#5792ad':'#007aff', '#9aa8b1':'#aeaeb2', '#f5f7f9':'#f2f2f7',
    '#d6dfe5':'#e5e5ea', '#bdd9e7':'#bfdbfe', '#f1f6f9':'#fafafa', '#d6e2ea':'#d1d1d6',
    '#f1f5f8':'#f5f5f7', '#b6d1df':'#007aff', '#d7e0e7':'#e5e5ea', '#edf3f7':'#f2f2f7',
    '#a1adb5':'#aeaeb2', '#e1e7ed':'#e8e8ed', '#637382':'#6e6e73', '#647d8c':'#86868b',
    '#214d62':'#1d1d1f',
}
COLORS_DARK = {
    '#244e63':'#f5f5f7', '#f5f7fa':'#1c1c1e', '#fff':'#2c2c2e', '#ffffff':'#2c2c2e',
    'white':'#2c2c2e', '#bdcbd5':'#48484a', '#dcebf3':'#184a74', '#edf4f8':'#3a3a3c',
    '#8aafc3':'#636366', '#5792ad':'#0a84ff', '#9aa8b1':'#8e8e93', '#f5f7f9':'#252527',
    '#d6dfe5':'#3a3a3c', '#bdd9e7':'#205b90', '#f1f6f9':'#303032', '#d6e2ea':'#48484a',
    '#f1f5f8':'#1c1c1e', '#b6d1df':'#0a84ff', '#d7e0e7':'#38383a', '#edf3f7':'#3a3a3c',
    '#a1adb5':'#8e8e93', '#e1e7ed':'#161618', '#637382':'#aeaeb2', '#647d8c':'#98989d',
    '#214d62':'#f5f5f7', '#e8e8ed':'#2c2c2e', '#e5e5ea':'#3a3a3c',
}


def is_dark():
    app = QApplication.instance()
    return bool(app and app.property('perovskiteTheme') == 'dark')


def stylesheet(mode='light'):
    assets = (Path(__file__).resolve().parent.parent / 'assets').as_posix()
    css = r'''
    QWidget { color:#244e63; }
    QPushButton,QToolButton,QLineEdit,QComboBox,QAbstractSpinBox,QTabBar,QHeaderView,QAbstractItemView,QCheckBox,QRadioButton,QMenu { font-size:12px; }
    QMainWindow,QDialog { background:#f5f7fa; }
    QPushButton,QToolButton,QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QDateEdit,QDateTimeEdit {
        border:1px solid #bdcbd5; border-radius:7px; background:#fff; color:#244e63;
        selection-background-color:#dcebf3; selection-color:#244e63;
    }
    QPushButton { padding:3px 8px; min-height:20px; }
    QToolButton { padding:3px; }
    QLineEdit { padding:3px 8px; min-height:20px; }
    QComboBox { padding:3px 24px 3px 8px; min-height:20px; }
    QSpinBox,QDoubleSpinBox,QDateEdit,QDateTimeEdit { padding:3px 22px 3px 6px; min-height:20px; }
    QWidget#literaturePanel QLineEdit,QWidget#literaturePanel QPushButton { padding:2px 6px; min-height:18px; }
    QWidget#literaturePanel QComboBox { padding:2px 22px 2px 6px; min-height:18px; }
    QPushButton[compact="true"] { padding:0; min-height:22px; }
    QPushButton:hover,QToolButton:hover,QComboBox:hover { background:#edf4f8; border-color:#8aafc3; }
    QPushButton:pressed,QPushButton:checked,QToolButton:pressed,QToolButton:checked {
        background:#dcebf3; border-color:#8aafc3;
    }
    QPushButton:focus,QToolButton:focus,QLineEdit:focus,QComboBox:focus,
    QSpinBox:focus,QDoubleSpinBox:focus,QDateEdit:focus,QDateTimeEdit:focus,
    QTextEdit:focus,QPlainTextEdit:focus,QAbstractItemView:focus { border-color:#5792ad; }
    QPushButton:disabled,QToolButton:disabled,QLineEdit:disabled,QComboBox:disabled,
    QSpinBox:disabled,QDoubleSpinBox:disabled,QDateEdit:disabled,QDateTimeEdit:disabled {
        color:#9aa8b1; background:#f5f7f9; border-color:#d6dfe5;
    }
    QComboBox::drop-down { subcontrol-origin:padding; subcontrol-position:top right; width:20px; border:none; }
    QComboBox::down-arrow { image:url("@ASSETS@/chevron-down.svg"); width:12px; height:12px; }
    QComboBox QAbstractItemView { border:1px solid #bdcbd5; border-radius:4px; background:white; outline:0; padding:3px; }
    QAbstractSpinBox::up-button,QAbstractSpinBox::down-button {
        subcontrol-origin:border; width:20px; border:none; background:#edf4f8;
    }
    QAbstractSpinBox::up-button { subcontrol-position:top right; border-top-right-radius:4px; }
    QAbstractSpinBox::down-button { subcontrol-position:bottom right; border-bottom-right-radius:4px; }
    QAbstractSpinBox::up-button:hover,QAbstractSpinBox::down-button:hover { background:#dcebf3; }
    QAbstractSpinBox::up-button:pressed,QAbstractSpinBox::down-button:pressed { background:#bdd9e7; }
    QAbstractSpinBox::up-arrow { image:url("@ASSETS@/chevron-up.svg"); width:10px; height:10px; }
    QAbstractSpinBox::down-arrow { image:url("@ASSETS@/chevron-down.svg"); width:10px; height:10px; }
    QTextEdit,QPlainTextEdit,QTextBrowser,QAbstractItemView {
        border:1px solid #bdcbd5; border-radius:4px; background:#fff; alternate-background-color:#f1f6f9;
        selection-background-color:#dcebf3; selection-color:#244e63;
    }
    QTextEdit,QPlainTextEdit,QTextBrowser { padding:4px; }
    QAbstractItemView { outline:0; }
    QAbstractItemView::item { padding:3px; border-radius:5px; }
    QAbstractItemView::item:hover { background:#edf4f8; }
    QAbstractItemView::item:selected { background:#dcebf3; color:#244e63; }
    QHeaderView::section { background:#fff; color:#637382; padding:4px; border:0; border-bottom:1px solid #d6dfe5; }
    QTableWidget { gridline-color:#d6dfe5; }
    QTableCornerButton::section { background:#edf4f8; border:1px solid #bdcbd5; }
    QTabWidget::pane { border:none; background:transparent; }
    QTabBar { background:#e8e8ed; border-radius:8px; padding:2px; }
    QTabBar::tab { background:transparent; color:#244e63; border:1px solid transparent; border-radius:6px; padding:4px 6px; margin:0 1px; }
    QTabBar::tab:hover { background:#edf4f8; }
    QTabBar::tab:selected { background:white; border-color:#d6dfe5; }
    QTabBar::tab:disabled { color:#9aa8b1; background:#f5f7f9; }
    QTabBar QToolButton { padding:0; }
    QToolBar { background:transparent; border:none; spacing:4px; }
    QToolBar#mainToolbar QToolButton {
        border:none; background:transparent; padding:4px; border-radius:0; outline:0;
    }
    QToolBar#mainToolbar QToolButton:hover,QToolBar#mainToolbar QToolButton:focus {
        background:#dcebf3; border-radius:4px;
    }
    QToolBar#mainToolbar QToolButton:pressed { background:#bdd9e7; border-radius:4px; }
    QToolBar#mainToolbar QToolButton:disabled { background:transparent; }
    QToolBar::separator { background:#d6dfe5; width:1px; margin:4px; }
    QMenu,QMenuBar { background:white; color:#244e63; }
    QMenu { border:1px solid #bdcbd5; border-radius:8px; padding:4px; }
    QMenu::item { padding:6px 24px; border-radius:5px; }
    QMenu::item:selected,QMenuBar::item:selected { background:#dcebf3; }
    QMenu::item:disabled { color:#9aa8b1; }
    QMenu::separator { height:1px; background:#d6dfe5; margin:4px; }
    QToolTip { background:white; color:#244e63; border:1px solid #bdcbd5; border-radius:4px; padding:5px; }
    QCheckBox,QRadioButton { spacing:6px; padding:3px 0; }
    QCheckBox::indicator,QRadioButton::indicator { width:16px; height:16px; border:1px solid #bdcbd5; background:white; }
    QCheckBox::indicator { border-radius:4px; }
    QRadioButton::indicator { border-radius:9px; }
    QCheckBox::indicator:hover,QRadioButton::indicator:hover { border-color:#8aafc3; background:#edf4f8; }
    QCheckBox::indicator:checked { image:url("@ASSETS@/check.svg"); background:#5792ad; border-color:#5792ad; }
    QCheckBox::indicator:indeterminate { image:url("@ASSETS@/minus.svg"); background:#5792ad; }
    QRadioButton::indicator:checked { image:url("@ASSETS@/radio-dot.svg"); background:#5792ad; border-color:#5792ad; }
    QCheckBox:disabled,QRadioButton:disabled { color:#9aa8b1; }
    QGroupBox { border:1px solid #bdcbd5; border-radius:8px; margin-top:9px; padding:10px 6px 6px; }
    QGroupBox::title { subcontrol-origin:margin; left:8px; padding:0 4px; }
    QSlider::groove:horizontal { height:4px; background:#d6e2ea; border-radius:2px; }
    QSlider::sub-page:horizontal { background:#8aafc3; border-radius:2px; }
    QSlider::handle:horizontal { background:white; border:1px solid #8aafc3; width:12px; margin:-5px 0; border-radius:6px; }
    QSlider::handle:horizontal:hover { background:#dcebf3; border-color:#5792ad; }
    QSlider#zoomRuler { padding-bottom:14px; }
    QSlider::groove:vertical { width:4px; background:#d6e2ea; border-radius:2px; }
    QSlider::handle:vertical { background:white; border:1px solid #8aafc3; height:12px; margin:0 -5px; border-radius:6px; }
    QScrollBar:vertical { background:#f1f5f8; width:12px; margin:0; border-radius:4px; }
    QScrollBar:horizontal { background:#f1f5f8; height:12px; margin:0; border-radius:4px; }
    QScrollBar::handle { background:#bdcbd5; border-radius:4px; min-height:24px; min-width:24px; }
    QScrollBar::handle:hover { background:#8aafc3; }
    QScrollBar::add-line,QScrollBar::sub-line { width:0; height:0; }
    QScrollBar::add-page,QScrollBar::sub-page { background:transparent; }
    QProgressBar { border:1px solid #bdcbd5; border-radius:4px; background:white; text-align:center; max-height:16px; }
    QProgressBar::chunk { background:#b6d1df; border-radius:3px; }
    QSplitter::handle { background:#d7e0e7; }
    QLabel#appBanner { font-size:18px; font-weight:600; color:#214d62; padding:2px; }
    QLabel#appSignature { font-size:12px; font-weight:600; color:#647d8c; padding:2px 6px; }
    QLabel#dataPath { color:#637382; font-size:11px; }
    QWidget#pdfDocument { background:#e1e7ed; }
    QScrollArea#pdfScroll { background:#e1e7ed; border:1px solid #bdcbd5; }
    QWidget#noteFontGroup { border:1px solid #bdcbd5; border-radius:7px; background:white; }
    QSpinBox#annotationFont { padding:0 3px; min-height:0; border:none; background:white; color:#244e63; font-size:12px; }
    QPushButton[fontStepper="true"] { padding:0; min-height:0; border:none; border-radius:3px; background:#edf3f7; color:#244e63; font-size:17px; font-weight:600; }
    QPushButton[fontStepper="true"]:hover { background:#dcebf3; }
    QPushButton[fontStepper="true"]:pressed { background:#bdd9e7; }
    QPushButton[fontStepper="true"]:disabled { color:#a1adb5; background:#f5f7f9; }
    QPushButton#browseModeButton {
        color:rgba(255,255,255,255); border-color:#8962dd;
        background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #3465da,stop:0.5 #7b38d0,stop:1 #d8428f);
    }
    QPushButton#browseModeButton:hover { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #4679ef,stop:0.5 #9455e6,stop:1 #ed59a4); }
    QPushButton#browseModeButton:pressed { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #294fb0,stop:1 #913792); }
    QPushButton#browseModeButton:checked { border-color:#4096c8; background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #087f9d,stop:0.5 #356ed4,stop:1 #7554cc); }
    QPushButton#browseModeButton:checked:hover { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #129bb9,stop:0.5 #4b86e8,stop:1 #8f6de3); }
    QPushButton#browseModeButton:checked:pressed { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #086781,stop:1 #58439f); }
    QPushButton#browseModeButton:disabled { color:rgba(235,235,245,255); border-color:#9398b2; background:#858ba8; }
    '''.replace('@ASSETS@', assets)
    colors=COLORS_DARK if mode=='dark' else COLORS_LIGHT
    css = re.sub(r'#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|\bwhite\b',lambda match:colors.get(match[0],match[0]),css)
    if mode == 'dark':
        # Replace whole color tokens once; never cascade replacements or modify
        # user PDF colors. SVG indicators have a separate, high-contrast asset.
        for asset in ['chevron-down','chevron-up','check','minus','radio-dot']:
            css = css.replace(f'/{asset}.svg',f'/{asset}-dark.svg')
        css+='QTabBar::tab:selected{background:#636366;border-color:#737376;color:#ffffff;} QSlider::handle:horizontal{background:#e5e5ea;}'
    return css


def apply_theme(application=None,mode=None):
    application = application or QApplication.instance()
    if mode is None:
        mode = application.property('perovskiteTheme') or 'light'
    if not isinstance(mode,str) or mode not in {'light','dark'}:
        mode = 'light'
    if application.property('perovskiteTheme') == mode:
        return
    if not application.property('perovskiteTheme'):
        application.setStyle('Fusion')
    palette = QPalette()
    for role, color in [(QPalette.ColorRole.Window,'#f5f7fa'),(QPalette.ColorRole.WindowText,'#244e63'),
                        (QPalette.ColorRole.Base,'#ffffff'),(QPalette.ColorRole.Text,'#244e63'),
                        (QPalette.ColorRole.Button,'#ffffff'),(QPalette.ColorRole.ButtonText,'#244e63'),
                        (QPalette.ColorRole.Highlight,'#dcebf3'),(QPalette.ColorRole.HighlightedText,'#244e63')]:
        palette.setColor(role,QColor((COLORS_DARK if mode=='dark' else COLORS_LIGHT).get(color,color)))
    palette.setColor(QPalette.ColorGroup.Disabled,QPalette.ColorRole.Text,QColor('#8e8e93' if mode=='dark' else '#aeaeb2'))
    palette.setColor(QPalette.ColorGroup.Disabled,QPalette.ColorRole.ButtonText,QColor('#8e8e93' if mode=='dark' else '#aeaeb2'))
    palette.setColor(QPalette.ColorRole.Link,QColor('#0a84ff' if mode=='dark' else '#007aff'))
    application.setProperty('perovskiteTheme',mode)
    application.setPalette(palette)
    application.setStyleSheet(stylesheet(mode))
