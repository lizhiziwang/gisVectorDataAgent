"""界面主题：参照 Claude Code 桌面端的视觉语言重做。

设计要点（都是 Claude Code 那边的做法）：
· **近乎单色**：暖白纸色底 + 近黑文字，全界面只有一处强调色（陶土橙）；
· **少边框、无阴影**：分隔靠 1px 暖灰细线，容器不用大圆角卡片；
· **对话不像气泡**：助手回复是纯文本段落，用户消息是一块浅色底块；
· **工具调用收成一行**：状态点 + 名称 + 耗时，细节缩进在下面（可折叠）；
· **输入区是一个整体**：圆角容器里放输入框 + 圆形发送键。
"""

COLORS = {
    "bg": "#faf9f5",          # 纸白
    "bg2": "#f3f1ea",         # 侧栏 / 浅填充
    "panel": "#ffffff",
    "line": "#e7e3d8",        # 暖灰细线
    "line2": "#d8d3c4",
    "ink": "#1f1e1d",
    "ink2": "#43413c",
    "muted": "#6f6c64",
    "faint": "#9a968c",
    "accent": "#c96442",      # 陶土橙（文字/描边）
    "accent_fill": "#d97757", # 陶土橙（填充）
    "accent_weak": "#f6ece6",
    "ok": "#3f7d58",
    "warn": "#a9762b",
    "err": "#b04a3f",
}

QSS = """
* { font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif; }
QWidget { color: #1f1e1d; font-size: 12px; }
QMainWindow, #central { background: #faf9f5; }

QFrame#panel {
    background: #f3f1ea;
    border: none;
    border-right: 1px solid #e7e3d8;
    border-radius: 0px;
}
QFrame#chatPanel {
    background: #ffffff;
    border: none;
    border-left: 1px solid #e7e3d8;
}
QFrame#topbar { background: #faf9f5; border-bottom: 1px solid #e7e3d8; }
QLabel#brandTitle { font-size: 14px; font-weight: 600; color: #1f1e1d; }
QLabel#brandSub { font-size: 11px; color: #8d897e; }
QLabel#panelTitle {
    font-size: 11px; font-weight: 500; color: #6f6c64; letter-spacing: .6px;
}
QLabel#muted { color: #6f6c64; font-size: 11px; }
QLabel#tiny { color: #9a968c; font-size: 10px; }

QProgressBar#loadProg {
    background: #ece8db; border: none; border-radius: 2px;
}
QProgressBar#loadProg::chunk { background: #d97757; border-radius: 2px; }

QLabel#badge {
    background: #ece8db; color: #6f6c64; border-radius: 8px;
    padding: 0px 6px; font-size: 10px;
}
QLabel#pill {
    background: transparent; color: #6f6c64; border: 1px solid #e0dbcd;
    border-radius: 11px; padding: 2px 10px; font-size: 11px;
}
QLabel#pillOn {
    background: #f6ece6; color: #b0522f; border: 1px solid #eccfc0;
    border-radius: 11px; padding: 2px 10px; font-size: 11px;
}

QPushButton {
    background: transparent; color: #43413c;
    border: 1px solid #ddd8ca; border-radius: 7px;
    padding: 4px 11px; font-size: 12px;
}
QPushButton:hover { background: #f2efe6; border-color: #cfc9b8; color: #1f1e1d; }
QPushButton:pressed { background: #eae6da; }
QPushButton:disabled { color: #b6b1a4; border-color: #ebe7db; }

QPushButton#primary {
    background: #d97757; color: #ffffff; border: 1px solid #d97757; font-weight: 500;
}
QPushButton#primary:hover { background: #c96442; border-color: #c96442; }
QPushButton#primary:disabled { background: #e8cec2; border-color: #e8cec2; color: #ffffff; }

QPushButton#send {
    background: #d97757; border: none; border-radius: 15px; padding: 0px;
}
QPushButton#send:hover { background: #c96442; }
QPushButton#send:pressed { background: #b85738; }
QPushButton#send:disabled { background: #eee7db; }

QPushButton#icon {
    padding: 0px; border-radius: 7px; font-size: 13px; color: #6f6c64;
    background: transparent; border: 1px solid transparent;
}
QPushButton#icon:hover { background: #f2efe6; border-color: #ddd8ca; }

QPushButton#ghost {
    border: 1px solid transparent; background: transparent; color: #43413c;
    padding: 4px 9px;
}
QPushButton#ghost:hover { background: #f2efe6; color: #1f1e1d; }

QLabel#toolGroup {
    color: #8d897e; font-size: 10px;
    padding: 12px 4px 3px 8px;      /* 左内边距对齐 chip 文字（chip: 1px 边框 + 7px 内边距） */
    border-top: 1px solid #efece1;
}
QLabel#toolGroup[first="1"] { padding-top: 2px; border-top: none; }
QPushButton#toolChip {
    text-align: left; padding: 3px 7px; font-size: 11px;
    background: transparent; border: 1px solid transparent; border-radius: 6px; color: #57544c;
}
QPushButton#toolChip:hover { background: #ece8db; color: #1f1e1d; }
QPushButton#toolChip:pressed { background: #e4dfd0; }

QPushButton#chip {
    padding: 2px 9px; font-size: 11px; border-radius: 11px;
    background: transparent; border: 1px solid #e0dbcd; color: #6f6c64;
}
QPushButton#chip:hover { border-color: #cfc9b8; color: #1f1e1d; background: #f2efe6; }

QPushButton#tiny {
    padding: 1px 7px; font-size: 11px; border-radius: 5px;
    background: transparent; border: 1px solid transparent; color: #6f6c64;
}
QPushButton#tiny:hover { background: #f2efe6; color: #1f1e1d; border-color: #ddd8ca; }

QFrame#dropzone {
    background: transparent; border: 1px dashed #d8d3c4; border-radius: 9px;
}
QFrame#dropzone:hover { border-color: #d97757; background: #f8f3ee; }

QListWidget#layerList { background: transparent; border: none; outline: none; }
QListWidget#layerList::item {
    border-radius: 6px; margin: 0px 4px; padding: 0px;
    border: 1px solid transparent; border-left: 2px solid transparent;
}
QListWidget#layerList::item:hover { background: #ece8db; }
QListWidget#layerList::item:selected {
    background: #e9e4d6; border-left: 2px solid #d97757;
}

QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QWidget#chatMsgs { background: #ffffff; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #ddd8ca; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: #c9c3b2; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #ddd8ca; border-radius: 5px; min-width: 28px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QFrame#composer {
    background: #ffffff; border: 1px solid #ddd8ca; border-radius: 14px;
}
QFrame#composer:focus-within { border-color: #d0c9b7; }
QTextEdit#chatInput {
    border: none; background: transparent; padding: 2px 0px; font-size: 13px;
}
QTextEdit#chatInput:focus { border: none; }

QFrame#userBubble {
    background: #f0ece1; border: 1px solid #e5e0d2; border-radius: 10px;
}
QLabel#userText { color: #1f1e1d; font-size: 13px; }
QFrame#agentBubble { background: transparent; border: none; }
QLabel#agentText { color: #1f1e1d; font-size: 13px; }

QFrame#stepCard { background: transparent; border: none; }
QFrame#stepHead { background: transparent; border: none; }
QLabel#stepDot { font-size: 11px; color: #3f7d58; }
QLabel#stepDotErr { font-size: 11px; color: #b04a3f; }
QLabel#stepName { font-size: 12px; font-weight: 500; color: #1f1e1d; }
QLabel#stepOk { color: #7d7a70; font-size: 11px; }
QLabel#stepErr { color: #b04a3f; font-size: 11px; }
QLabel#stepBody { font-size: 12px; color: #57544c; }
QLabel#kv {
    background: #f6f4ec; color: #6f6c64; border-radius: 4px; padding: 1px 6px; font-size: 10px;
}
QLabel#warnBox {
    background: transparent; color: #8a6a2f; border-left: 2px solid #d8a54a;
    border-radius: 0px; padding: 1px 0px 1px 8px; font-size: 11px;
}
QLabel#plannerOn { color: #b0522f; font-size: 11px; }
QLabel#newLayerTag {
    color: #3f7d58; font-size: 11px;
}

QLabel#legendRow { font-size: 11px; color: #43413c; }
QLabel#mapInfo {
    background: rgba(250,249,245,0.94); border: 1px solid #e7e3d8;
    border-radius: 7px; padding: 3px 8px; color: #6f6c64; font-size: 10px;
}
QFrame#legendBox, QFrame#infoBox {
    background: rgba(250,249,245,0.96); border: 1px solid #e7e3d8; border-radius: 8px;
}

QStatusBar { background: #faf9f5; border-top: 1px solid #e7e3d8; color: #8d897e; font-size: 11px; }
QStatusBar::item { border: none; }

QLineEdit, QComboBox {
    border: 1px solid #ddd8ca; border-radius: 7px; padding: 5px 9px;
    background: #ffffff; font-size: 12px; color: #1f1e1d;
}
QLineEdit:focus, QComboBox:focus { border-color: #d97757; }

QTableWidget {
    background: #ffffff; border: none; gridline-color: #f1efe6;
    font-size: 11px; selection-background-color: #f6ece6; selection-color: #1f1e1d;
}
QHeaderView::section {
    background: #f7f5ee; color: #6f6c64; border: none;
    border-bottom: 1px solid #e7e3d8; padding: 5px 8px; font-size: 11px; font-weight: 500;
}

QDialog { background: #faf9f5; }
QCheckBox { font-size: 12px; spacing: 6px; }
QToolTip {
    background: #1f1e1d; color: #faf9f5; border: none; padding: 5px 8px; font-size: 11px;
}
QMenu {
    background: #ffffff; border: 1px solid #e7e3d8; border-radius: 7px; padding: 4px;
}
QMenu::item { padding: 5px 18px 5px 12px; border-radius: 5px; font-size: 12px; }
QMenu::item:selected { background: #f2efe6; color: #1f1e1d; }
QProgressDialog { background: #faf9f5; }
QPlainTextEdit#codeEditor {
    background: #1f1e1d; color: #ece9df; border-radius: 8px;
    border: 1px solid #33312c; padding: 8px;
}
"""
