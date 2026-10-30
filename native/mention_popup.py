"""@ 提及的输入辅助：输入 @ 时弹出图层补全列表。

交互照着常见的 @ 文件引用做：
· 输入 `@` → 弹出所有图层；
· 继续打字 → 列表按名字过滤（支持模糊匹配）；
· ↑/↓ 选，Enter/Tab 确认，Esc 关闭；
· 确认后把光标停在图层名后面，可以接着打「 做 300 米缓冲区」。

列表是浮在输入框上方的独立窗口，不占布局位置 —— 免得输入框被顶来顶去。

样式跟应用主体一致（暖白底 + 橙色强调 + 细边框），
不用 QListWidget 的默认项 —— 它只能塞一整行文本，
图层名和元信息没法左右对齐，看起来像日志不像菜单。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from .theme import QSS

#: 配色（跟 theme.py 保持一致）
_C_BG = QColor("#ffffff")
_C_BORDER = QColor("#e0dbcd")
_C_ACCENT = QColor("#d97757")
_C_ACCENT_SOFT = QColor("#f8f3ee")
_C_TEXT = QColor("#1f1e1d")
_C_MUTED = QColor("#8d897e")
_C_HOVER = QColor("#f5f2e9")
_C_SELECT = QColor("#f3f0e8")

#: 单行高度
_ROW_H = 34
#: 图标圆点直径
_DOT = 7


class _RowDelegate(QStyledItemDelegate):
    """画一行：几何色点 + 图层名 + 右侧元信息。

    不用 QListWidget 默认绘制 —— 它只能塞一整行文本，
    图层名和元信息没法左右对齐，看起来像日志不像菜单。
    """

    def __init__(self, popup: "MentionPopup"):
        super().__init__(popup)
        self.popup = popup

    def paint(self, painter: QPainter, option, index) -> None:
        name = index.data(Qt.UserRole) or index.data(Qt.DisplayRole) or ""
        # 选中态两个来源：QListWidget 自己的 current row，
        # 和我们自己记的 _current（键盘移动时用）。
        selected = bool(option.state & QStyle.State_Selected) \
            or index.row() == self.popup._current
        self.popup._paint_row(painter, option.rect, str(name), selected)


class MentionPopup(QFrame):
    """@ 补全弹窗。

    ``picked``  信号：发出选中的图层名。
    ``dismissed`` 信号：用户关掉了弹窗。
    """

    picked = Signal(str)
    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(None)          # 顶层窗口，才能浮在输入框上面
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint
                            | Qt.WindowDoesNotAcceptFocus)
        # 关键：显示时**不要抢焦点**。
        # Qt.Popup 默认会激活自己，那键盘事件就跑到弹窗去了，
        # 输入框收不到回车 —— 表现为「按回车选不中图层」。
        # 焦点必须留在输入框上，弹窗只负责显示和鼠标点击。
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("mentionPopup")
        # 关键：这是**独立的顶层窗口**，不会继承主窗口的样式表
        # （主窗口只把 QSS 设给了自己）。不在这儿设一遍的话
        # 弹窗里所有 QSS 规则都不生效。
        self.setStyleSheet(QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.box = QFrame()
        self.box.setObjectName("mentionBox")
        # 表面由 paintEvent 自己画（圆角 + 描边 + 投影），
        # QSS 的 border-radius 裁不住子控件，画出来是方的。
        self.box.setAttribute(Qt.WA_TranslucentBackground, True)
        inner = QVBoxLayout(self.box)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        # 头部：标题 + 计数
        head = QWidget()
        head.setObjectName("mentionHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 8, 12, 6)
        hl.setSpacing(0)
        self.title = QLabel("选择图层")
        self.title.setObjectName("mentionTitle")
        hl.addWidget(self.title)
        hl.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("mentionCount")
        hl.addWidget(self.count_label)
        inner.addWidget(head)
        self.head = head

        # 列表
        self.list = QListWidget()
        self.list.setObjectName("mentionList")
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setSpacing(0)
        self.list.setUniformItemSizes(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setMouseTracking(True)
        self.list.itemClicked.connect(self._on_item_clicked)
        self._delegate = _RowDelegate(self)
        self.list.setItemDelegate(self._delegate)
        inner.addWidget(self.list)

        # 底部：快捷键提示
        # 底栏的浅色底由 paintEvent 画（跟着圆角裁），
        # 这儿只放文字 —— 用 QSS 的方角背景会在圆角处露出去。
        foot = QWidget()
        foot.setObjectName("mentionFoot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(12, 5, 12, 6)
        fl.setSpacing(6)
        self.foot_hint = QLabel("↑↓ 选择 · Enter 确认 · Esc 取消")
        self.foot_hint.setObjectName("mentionHint")
        fl.addWidget(self.foot_hint)
        fl.addStretch(1)
        inner.addWidget(foot)

        outer.addWidget(self.box)
        self.setFixedWidth(292)

        #: 每项的元信息：图层名 → (几何类型, 要素数)
        self._meta: dict[str, tuple[str, str]] = {}
        self._items: list[str] = []     # 图层名列表
        self._current = 0
        #: 底栏高度（paintEvent 要靠它画浅色带）
        self._foot_h = 0
        self.foot = foot

    # ------------------------------------------------------------------ 数据
    def set_candidates(self, layers, query: str = "") -> None:
        """设置候选图层。``layers`` 是 Layer 对象列表。"""
        self.list.clear()
        self._items = []
        self._meta = {}
        q = (query or "").lower().strip()
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
            elif all(ch in low for ch in q):
                # 模糊：所有字符都在名字里（顺序不拘）
                scored.append((2, lay))
        scored.sort(key=lambda t: (t[0], len(getattr(t[1], "name", ""))))
        for _rank, lay in scored[:8]:
            name = getattr(lay, "name", "")
            geom = count = ""
            meta = getattr(lay, "meta", None)
            if callable(meta):
                try:
                    m = meta()
                    geom = m.get("geomType") or ""
                    n = m.get("count")
                    count = f"{n:,}" if isinstance(n, int) else ""
                except Exception:
                    pass
            self._meta[name] = (geom, count)
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            item.setSizeHint(QSize(0, _ROW_H))
            self.list.addItem(item)
            self._items.append(name)
        self._current = 0
        if self.list.count():
            self.list.setCurrentRow(0)
        self._update_title(query)

    def _update_title(self, query: str) -> None:
        n = self.list.count()
        if query:
            self.title.setText("匹配图层")
            self.count_label.setText(f"{query} · {n}")
        else:
            self.title.setText("选择图层")
            self.count_label.setText(f"{n} 个")

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.UserRole)
        if name:
            self.picked.emit(str(name))

    def count(self) -> int:
        return self.list.count()

    def current_name(self) -> str:
        if not self._items:
            return ""
        idx = max(0, min(self._current, len(self._items) - 1))
        return self._items[idx]

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

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """兜底：万一弹窗还是拿到了焦点，也能用键盘选。

        正常情况下焦点在输入框上（WA_ShowWithoutActivating），
        这里的按键走的是输入框的事件过滤器。
        但不同平台/窗口管理器对 Popup 的激活处理不完全一样，
        这里再兜一层，保证「回车能选图层」在任何情况下都成立。
        """
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
            self.accept()
            return
        if key == Qt.Key_Escape:
            self.dismissed.emit()
            return
        if key == Qt.Key_Up:
            self.move_selection(-1)
            return
        if key == Qt.Key_Down:
            self.move_selection(1)
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------ 绘制
    def _apply_mask(self) -> None:
        """给整个窗口套一个圆角遮罩。

        光靠 paintEvent 画圆角矩形是不够的 —— 窗口本身还是方的，
        半透明背景下四个角会露出直角。QSS 的 border-radius 也裁不住
        子控件。真正管用的是 setMask()。
        """
        self.clearMask()
        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10.0, 10.0)
        p.end()
        self.setMask(pm.mask())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_mask()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_mask()

    def paintEvent(self, event) -> None:  # noqa: N802
        """自己画弹窗表面：圆角 + 细描边 + 淡投影 + 底栏浅色带。

        遮罩负责裁形状，这里负责配色和层次。
        底栏的浅色也得在这儿画 —— 用 QSS 给子控件上背景，
        方角会在圆角边缘露出去。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 10.0
        # 投影（往下偏一点点）
        shadow = QRectF(r).translated(0, 2)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(31, 30, 29, 26))
        painter.drawRoundedRect(shadow, radius, radius)
        # 底色
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(r, radius, radius)
        # 底栏浅色带（跟着圆角裁，所以画在同一个路径里）
        if self._foot_h > 0:
            foot_rect = QRectF(0, r.bottom() - self._foot_h + 1, r.width(), self._foot_h)
            painter.save()
            path = QPainterPath()
            path.addRoundedRect(r, radius, radius)
            painter.setClipPath(path)
            painter.setBrush(QColor("#fbfaf6"))
            painter.drawRect(foot_rect)
            # 底栏上方的细线
            painter.setPen(QPen(QColor("#efece1"), 1))
            painter.drawLine(QPointF(r.left(), foot_rect.top()),
                             QPointF(r.right(), foot_rect.top()))
            painter.restore()
        # 描边
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(_C_BORDER, 1))
        painter.drawRoundedRect(r, radius, radius)
        painter.end()

    def _paint_row(self, painter: QPainter, rect: QRect, name: str,
                   selected: bool) -> None:
        """画一行：左侧几何色点 + 图层名 + 右侧元信息。"""
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(_C_SELECT)
            painter.drawRoundedRect(rect.adjusted(4, 1, -4, -1), 6, 6)
            # 左侧强调条
            painter.setBrush(_C_ACCENT)
            painter.drawRoundedRect(QRect(rect.left() + 5, rect.top() + 8, 2,
                                         rect.height() - 16), 1, 1)
        geom, count = self._meta.get(name, ("", ""))
        # 几何类型小圆点
        dot_c = {"Point": QColor("#5b8def"), "LineString": QColor("#d9a441"),
                 "Polygon": QColor("#6fa87a"), "MultiPolygon": QColor("#6fa87a")}.get(
                     geom, QColor("#b6b1a3"))
        cy = rect.center().y()
        painter.setPen(Qt.NoPen)
        painter.setBrush(dot_c)
        painter.drawEllipse(QRect(rect.left() + 14, cy - _DOT // 2, _DOT, _DOT))

        # 图层名
        f = painter.font()
        # pointSizeF() 可能是 -1（表示「用应用默认字号」），
        # 这时候 +0.5 会变成负数，Qt 会警告并拒绝设置。
        ps = f.pointSizeF()
        if ps > 0:
            f.setPointSizeF(ps + 0.5)
        if selected:
            f.setBold(True)
        painter.setFont(f)
        painter.setPen(_C_TEXT if selected else QColor("#43413c"))
        # 宽度不能算成负的 —— 窗口窄一点 drawText 会画歪
        name_w = max(40, rect.width() - 28 - 74)
        text_rect = QRect(rect.left() + 28, rect.top(), name_w, rect.height())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, name)

        # 右侧元信息
        painter.setFont(self.font())
        painter.setPen(_C_MUTED)
        meta_text = " · ".join(x for x in (geom, count) if x)
        if meta_text:
            painter.drawText(QRect(rect.right() - 76, rect.top(), 68, rect.height()),
                             Qt.AlignVCenter | Qt.AlignRight, meta_text)
        painter.restore()

    # ------------------------------------------------------------------ 定位
    def show_at(self, anchor: QWidget, cursor_rect: QPoint | None = None) -> None:
        """在输入框上方定位显示。"""
        # 高度跟着候选数走 —— 2 个候选不该占 220px
        self.list.setFixedHeight(_ROW_H * max(1, self.list.count()))
        # 先让布局算一次，拿到底栏的真实高度（paintEvent 要用）
        self.foot.adjustSize()
        self._foot_h = self.foot.sizeHint().height()
        head_h = self.head.sizeHint().height()
        h = head_h + _ROW_H * max(1, self.list.count()) + self._foot_h
        self.setFixedHeight(h)
        screen = QGuiApplication.screenAt(anchor.rect().center()) or QGuiApplication.primaryScreen()
        pos = anchor.mapToGlobal(QPoint(0, 0))
        if cursor_rect is not None:
            pos = anchor.mapToGlobal(cursor_rect)
        y = pos.y() - h - 6
        if y < 0:                       # 上方放不下就翻到下方
            y = pos.y() + anchor.height() + 6
        x = max(0, min(pos.x(), (screen.availableGeometry().width() if screen else 1920) - self.width()))
        self.move(x, y)
        self.show()
