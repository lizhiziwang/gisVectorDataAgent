"""@ 提及的输入辅助：输入 @ 时弹出图层补全列表。

交互照着常见的 @ 文件引用做：
· 输入 `@` → 弹出所有图层；
· 继续打字 → 列表按名字过滤（支持模糊匹配）；
· ↑/↓ 选，Enter/Tab 确认，Esc 关闭；
· 确认后把光标停在图层名后面，可以接着打「 做 300 米缓冲区」。

列表是浮在输入框上方的独立窗口，不占布局位置 —— 免得输入框被顶来顶去。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QVBoxLayout, QWidget)

from app.mentions import Mention


class MentionPopup(QFrame):
    """@ 补全弹窗。

    ``picked``  信号：发出选中的图层名。
    ``dismissed`` 信号：用户关掉了弹窗。
    """

    picked = Signal(str)
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(None)          # 顶层窗口，才能浮在输入框上面
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("mentionPopup")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        self.box = QFrame()
        self.box.setObjectName("mentionBox")
        inner = QVBoxLayout(self.box)
        inner.setContentsMargins(4, 4, 4, 4)
        inner.setSpacing(2)

        self.title = QLabel("选择图层")
        self.title.setObjectName("tiny")
        inner.addWidget(self.title)

        self.list = QListWidget()
        self.list.setObjectName("mentionList")
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setFixedHeight(min(220, 34 * 8 + 12))
        inner.addWidget(self.list)

        outer.addWidget(self.box)
        self.setFixedWidth(300)

        self._items: list[tuple[str, str]] = []   # (显示名, 图层名)
        self._current = 0

    # ------------------------------------------------------------------ 数据
    def set_candidates(self, layers, query: str = "") -> None:
        """设置候选图层。``layers`` 是 Layer 对象列表。"""
        self.list.clear()
        self._items = []
        q = (query or "").lower()
        scored: list[tuple[int, object]] = []
        for lay in layers:
            name = getattr(lay, "name", "") or ""
            if not name:
                continue
            if not q:
                scored.append((0, lay))
                continue
            low = name.lower()
            if q in low:
                # 开头命中排前面
                scored.append((0 if low.startswith(q) else 1, lay))
            else:
                # 模糊：所有字符都在名字里（顺序不拘）
                if all(ch in low for ch in q):
                    scored.append((2, lay))
        scored.sort(key=lambda t: (t[0], len(getattr(t[1], "name", ""))))
        for _rank, lay in scored[:8]:
            name = getattr(lay, "name", "")
            meta = getattr(lay, "meta", None)
            geom = count = ""
            if callable(meta):
                try:
                    m = meta()
                    geom = m.get("geomType") or ""
                    count = m.get("count")
                except Exception:
                    pass
            sub = " · ".join(x for x in (geom, f"{count:,} 个要素" if count else "") if x)
            item = QListWidgetItem(f"{name}" + (f"    {sub}" if sub else ""))
            item.setData(Qt.UserRole, name)
            self.list.addItem(item)
            self._items.append((name, name))
        self._current = 0
        if self.list.count():
            self.list.setCurrentRow(0)
        self.title.setText(f"选择图层（{self.list.count()}）" if not q
                           else f"匹配「{query}」（{self.list.count()}）")

    def count(self) -> int:
        return self.list.count()

    def current_name(self) -> str:
        if not self._items:
            return ""
        idx = max(0, min(self._current, len(self._items) - 1))
        return self._items[idx][1]

    # ------------------------------------------------------------------ 键盘
    def move_selection(self, delta: int) -> None:
        """上下移动选中项。

        注意别叫 `move` —— QWidget.move(x, y) 是移动窗口，
        会被同名方法盖掉（show_at 里调 self.move(x, y) 直接 TypeError）。
        """
        if not self._items:
            return
        self._current = (self._current + delta) % len(self._items)
        self.list.setCurrentRow(self._current)

    def accept(self) -> None:
        name = self.current_name()
        if name:
            self.picked.emit(name)
        else:
            self.dismissed.emit()

    # ------------------------------------------------------------------ 定位
    def show_at(self, anchor: QWidget, cursor_rect: QPoint | None = None) -> None:
        """在输入框上方定位显示。"""
        screen = QGuiApplication.screenAt(anchor.rect().center()) or QGuiApplication.primaryScreen()
        pos = anchor.mapToGlobal(QPoint(0, 0))
        if cursor_rect is not None:
            pos = anchor.mapToGlobal(cursor_rect)
        h = self.sizeHint().height()
        y = pos.y() - h - 6
        if y < 0:                       # 上方放不下就翻到下方
            y = pos.y() + anchor.height() + 6
        x = max(0, min(pos.x(), (screen.availableGeometry().width() if screen else 1920) - self.width()))
        self.move(x, y)
        self.show()
