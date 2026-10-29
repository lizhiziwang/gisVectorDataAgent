"""对话面板：消息气泡 + 步骤卡片 + 输入区（纯 Qt 控件）。"""
from __future__ import annotations

from PySide6.QtCore import QSize, QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizePolicy, QTableWidget, QTableWidgetItem, QTextEdit,
                               QVBoxLayout, QWidget)

from .format import kv_text


def _send_icon(busy: bool = False, color: str = "#ffffff", px: int = 16) -> QIcon:
    """发送键的图标：空闲是一支上箭头，执行中是三个点。

    用 QPainter 画而不是往按钮里塞「↑」「发送」——中文字号塞进 30px 圆里必然溢出，
    而箭头字形在不同字体下粗细、基线都不一样。
    """
    dpr = 2.0
    pm = QPixmap(int(px * dpr), int(px * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.6, px / 9.0))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)

    cx, cy = px / 2.0, px / 2.0
    if busy:                                    # 三个点
        r = max(1.2, px * 0.075)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        for dx in (-px * 0.24, 0.0, px * 0.24):
            p.drawEllipse(int(cx + dx - r), int(cy - r), int(r * 2), int(r * 2))
    else:                                       # 上箭头：一竖 + 两撇
        from PySide6.QtCore import QPointF as P
        p.drawLine(P(cx, cy + px * 0.28), P(cx, cy - px * 0.26))
        p.drawLine(P(cx, cy - px * 0.26), P(cx - px * 0.24, cy + px * 0.02))
        p.drawLine(P(cx, cy - px * 0.26), P(cx + px * 0.24, cy + px * 0.02))
    p.end()
    return QIcon(pm)


class Worker(QThread):
    """把耗时活儿（规划、空间运算、读大文件）丢到后台，别冻住界面。

    注意：QThread 对象在 Python 侧被垃圾回收时，如果线程还在跑，
    进程会直接崩溃（"QThread: Destroyed while thread is still running"）。
    典型触发场景：一次拖入多个文件，每次都 new 一个 Worker 覆盖掉旧引用。
    所以这里维护一个全局存活表，线程结束后再放掉引用。
    """

    done = Signal(object)
    failed = Signal(str)

    # 正在跑的 Worker 强引用（线程结束后移除）
    _alive: set = set()

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        Worker._alive.add(self)
        self.finished.connect(lambda: Worker._alive.discard(self))

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.failed.emit(str(e))


_ZWSP = "\u200b"
# QLabel 的 wordWrap 只在「词边界」断行，连续的 ASCII 串（图层名、路径、数字）没有词边界，
# 会整行溢出后被硬裁掉。这些字符后面补一个零宽空格，给排版器一个断点。
_SOFT_AFTER = "_/()（）-·"


def _soft_break(text: str) -> str:
    if len(text) < 20 or not any(c in text for c in _SOFT_AFTER):
        return text
    return "".join(c + _ZWSP if c in _SOFT_AFTER else c for c in text)


def _label(text: str, obj: str | None = None, wrap: bool = True,
           max_width: int | None = None) -> QLabel:
    lb = QLabel(_soft_break(text) if wrap else text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if wrap:
        # 中文长句不会按词换行，必须显式限宽，否则气泡会被撑到面板外面去
        lb.setMaximumWidth(max_width or 348)
        lb.setMinimumWidth(1)
    return lb


class StepCard(QFrame):
    """一步算子的执行卡片：标题 / 说明 / 指标 / 告警 / 小表格 / 操作按钮。"""

    def __init__(self, index: int, step: dict, win):
        super().__init__()
        self.setObjectName("stepCard")
        self._win = win
        self._body_visible = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = QFrame()
        head.setObjectName("stepHead")
        head.setCursor(Qt.PointingHandCursor)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(2, 3, 2, 3)
        hl.setSpacing(7)
        ok = step.get("status") != "error"
        # 一行式工具调用：状态点 + 名称 + 耗时（Claude Code 里就是这个形态）。
        # 状态点用画的（小圆点），不依赖字体里有没有 ● —— 否则某些环境会显示成方块。
        dot = QLabel()
        dot.setFixedSize(7, 7)
        dot.setStyleSheet("background:%s;border-radius:3px;" % ("#3f7d58" if ok else "#b04a3f"))
        hl.addWidget(dot, 0, Qt.AlignVCenter)
        hl.addWidget(_label(step.get("label") or step.get("tool") or "算子", "stepName", False))
        hl.addStretch(1)
        txt = "完成" if ok else "失败"
        el = step.get("elapsed") or 0
        if ok and el:
            txt = f"{el:.2f}s" if el < 10 else f"{el:.0f}s"
        hl.addWidget(_label(txt, "stepOk" if ok else "stepErr", False))
        outer.addWidget(head)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(17, 2, 2, 8)
        bl.setSpacing(5)

        bl.addWidget(_label(step.get("message") or "", "stepBody", True, 330))

        kv = kv_text(step.get("data") or {})
        if kv:
            bl.addWidget(_label(kv, "muted", True, 330))

        for w in step.get("warnings") or []:
            bl.addWidget(_label("⚠ " + w, "warnBox"))

        table = (step.get("data") or {}).get("table")
        if isinstance(table, dict) and table.get("columns"):
            bl.addWidget(self._mini_table(table))

        if step.get("layer"):
            lay = step["layer"]
            bl.addWidget(_label(f"＋ 新图层「{lay.get('name')}」· {lay.get('count')} 要素", "newLayerTag"))

        acts = QHBoxLayout()
        acts.setSpacing(6)
        if step.get("layer"):
            lid = step["layer"].get("id")
            # 注意：clicked 会带一个 checked(bool) 参数，
            # 写成 `lambda lid=lid: ...` 的话这个 bool 会顶掉默认值，
            # lid 变成 False，按钮看起来就是「没反应」。
            for text, fn in (("定位图层", lambda _checked=False, lid=lid: win.locate_layer(lid)),
                             ("属性表", lambda _checked=False, lid=lid: win.open_table(lid)),
                             ("导出", lambda _checked=False, lid=lid: win.export_layer(lid))):
                b = QPushButton(text)
                b.setObjectName("tiny")
                b.setCursor(Qt.PointingHandCursor)
                b.clicked.connect(fn)
                acts.addWidget(b)
        if step.get("tool") == "export" and (step.get("data") or {}).get("layer_id"):
            lid = step["data"]["layer_id"]
            fmt = step["data"].get("format", "geojson")
            b = QPushButton(f"下载 {fmt.upper()}")
            b.setObjectName("tiny")
            b.clicked.connect(lambda _checked=False, lid=lid, fmt=fmt: win.export_layer(lid, fmt))
            acts.addWidget(b)
        acts.addStretch(1)
        if acts.count() > 1:
            bl.addLayout(acts)

        outer.addWidget(body)
        self._body = body
        head.mousePressEvent = self._toggle  # type: ignore[assignment]

    def _toggle(self, _e=None) -> None:
        self._body_visible = not self._body_visible
        self._body.setVisible(self._body_visible)

    @staticmethod
    def _mini_table(table: dict) -> QWidget:
        cols = [str(c) for c in table.get("columns", [])][:5]
        rows = (table.get("rows") or [])[:6]
        tw = QTableWidget(len(rows), len(cols))
        tw.setHorizontalHeaderLabels(cols)
        tw.verticalHeader().setVisible(False)
        tw.setEditTriggers(QTableWidget.NoEditTriggers)
        tw.setSelectionMode(QTableWidget.NoSelection)
        tw.setShowGrid(False)
        tw.setFixedHeight(26 + 22 * max(len(rows), 1))
        tw.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for r, row in enumerate(rows):
            for c, val in enumerate(row[:len(cols)]):
                item = QTableWidgetItem("—" if val is None else str(val))
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                tw.setItem(r, c, item)
        tw.resizeColumnsToContents()
        return tw


def _bubble_size(text: str, fm, maxw: int) -> int:
    """按文本实际换行结果算宽度：短句不拉满，长句封顶。

    这里的最小值必须很小（原来写死 80）——「你好」「然后呢」这种两三个字的消息，
    80px 的宽度会让气泡右边空掉一大半，看起来像"留白 bug"。
    """
    from PySide6.QtCore import QRect
    r = fm.boundingRect(QRect(0, 0, maxw, 100000), Qt.TextWordWrap | Qt.AlignLeft, text)
    return max(20, min(maxw, r.width() + 4))


class ChatPanel(QWidget):
    sent = Signal(str)

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._typing: QFrame | None = None
        self._hist: list[str] = []      # 发过的指令，↑/↓ 翻（shell 手感）
        self._hist_pos = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 头部
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 10, 10, 10)
        hl.setSpacing(9)
        avatar = QLabel("✳")
        avatar.setFixedSize(22, 22)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setStyleSheet("color:#d97757;font-size:15px;")
        hl.addWidget(avatar)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        t1 = QLabel("空间分析助手")
        t1.setStyleSheet("font-size:13px;font-weight:500;")
        self.planner_label = QLabel("规则引擎 · 离线可用")
        self.planner_label.setObjectName("muted")
        titles.addWidget(t1)
        titles.addWidget(self.planner_label)
        hl.addLayout(titles)
        hl.addStretch(1)
        self.clear_btn = QPushButton("⟲")
        self.clear_btn.setObjectName("icon")
        self.clear_btn.setFixedSize(26, 26)
        self.clear_btn.setToolTip("清空图层与对话")
        hl.addWidget(self.clear_btn)
        root.addWidget(head)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background:#e7e3d8;")
        root.addWidget(line)

        # 消息区
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.msgs = QWidget()
        # 只设 objectName，样式写在主题里（QWidget#chatMsgs 带 id 选择器）。
        # 千万别写成 self.msgs.setStyleSheet("background:…") —— 这种不带选择器的
        # 声明会级联到**所有子控件**，把气泡自己的背景盖掉：
        # 用户气泡（蓝底白字）会变成白底白字，看起来就像「用户消息没显示」。
        self.msgs.setObjectName("chatMsgs")
        self.msg_layout = QVBoxLayout(self.msgs)
        self.msg_layout.setContentsMargins(16, 18, 16, 10)
        self.msg_layout.setSpacing(14)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(self.msgs)
        root.addWidget(self.scroll, 1)
        # 「贴底」：人在底部时，新消息、表格、气泡重排长高之后都自动跟到最新；
        # 手动往上翻就停在他看的位置，不再被强行拽回去。
        self._stick = True
        _bar = self.scroll.verticalScrollBar()
        _bar.valueChanged.connect(self._on_scrolled)
        _bar.rangeChanged.connect(self._on_range_changed)

        # 输入区
        foot = QWidget()
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(10, 8, 10, 9)
        fl.setSpacing(6)

        chip_scroll = QScrollArea()
        chip_scroll.setFixedHeight(30)
        chip_scroll.setWidgetResizable(True)
        chip_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chip_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        chip_host = QWidget()
        self.chip_layout = QHBoxLayout(chip_host)
        self.chip_layout.setContentsMargins(0, 0, 0, 0)
        self.chip_layout.setSpacing(5)
        self.chip_layout.addStretch(1)
        chip_scroll.setWidget(chip_host)
        fl.addWidget(chip_scroll)
        self.chip_scroll = chip_scroll

        composer = QFrame()
        composer.setObjectName("composer")
        box = QHBoxLayout(composer)
        box.setContentsMargins(12, 8, 8, 8)
        box.setSpacing(6)
        self.input = QTextEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("描述你要做的空间分析…")
        self.input.setFixedHeight(44)
        self.input.installEventFilter(self)
        box.addWidget(self.input, 1)
        self.send_btn = QPushButton()
        self.send_btn.setObjectName("send")
        self.send_btn.setFixedSize(30, 30)
        self.send_btn.setIconSize(QSize(16, 16))
        self.send_btn.setIcon(_send_icon())
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setToolTip("发送（Enter）")
        self.send_btn.clicked.connect(lambda _checked=False: self._emit_send())
        box.addWidget(self.send_btn, 0, Qt.AlignBottom)
        fl.addWidget(composer)

        hint = QLabel("Enter 发送 · Shift+Enter 换行 · 原始数据只读，有产出才建新图层")
        hint.setObjectName("tiny")
        fl.addWidget(hint)
        root.addWidget(foot)

    # ------------------------------------------------------------------ 事件
    def eventFilter(self, obj, event):  # noqa: N802
        from PySide6.QtCore import QEvent
        if obj is self.input and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
                self._emit_send()
                return True
            # ↑/↓ 翻指令历史。只在「单行」时接管，否则会和光标上下移动打架。
            single = "\n" not in self.input.toPlainText()
            if event.key() == Qt.Key_Up and single and self._hist:
                self._hist_pos = max(0, (len(self._hist) - 1) if self._hist_pos < 0
                                     else self._hist_pos - 1)
                self._set_input(self._hist[self._hist_pos])
                return True
            if event.key() == Qt.Key_Down and self._hist_pos >= 0:
                if self._hist_pos >= len(self._hist) - 1:
                    self._hist_pos = -1
                    self._set_input("")
                else:
                    self._hist_pos += 1
                    self._set_input(self._hist[self._hist_pos])
                return True
        return super().eventFilter(obj, event)

    def _set_input(self, text: str) -> None:
        self.input.setPlainText(text)
        cur = self.input.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.input.setTextCursor(cur)

    def _emit_send(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        if not self._hist or self._hist[-1] != text:
            self._hist.append(text)         # 连续重复的不记两遍
        self._hist_pos = -1
        self.input.clear()
        self.sent.emit(text)

    # ------------------------------------------------------------------ 内容
    def set_suggestions(self, items: list[str]) -> None:
        while self.chip_layout.count() > 1:
            it = self.chip_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for s in items[:8]:
            b = QPushButton(s)
            b.setObjectName("chip")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, t=s: (self.input.setPlainText(t), self.input.setFocus()))
            self.chip_layout.insertWidget(self.chip_layout.count() - 1, b)

    def set_planner(self, text: str, ready: bool = False) -> None:
        self.planner_label.setText(text)
        self.planner_label.setObjectName("plannerOn" if ready else "muted")
        self.planner_label.style().unpolish(self.planner_label)
        self.planner_label.style().polish(self.planner_label)

    def clear(self) -> None:
        while self.msg_layout.count() > 1:
            it = self.msg_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _append(self, w: QWidget) -> None:
        stick = self._stick
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, w)
        from PySide6.QtCore import QTimer
        # 控件进树、样式 polish 过之后再排一次：建的时候量到的字体会偏小
        QTimer.singleShot(0, self._reflow)
        if stick:
            self._pin_bottom()
        # 布局真正稳定通常在几十毫秒之后（长文本、表格、步骤卡片都会继续长高），
        # 所以再补一次；期间任何高度变化还会走 rangeChanged 兜住
        QTimer.singleShot(30, self._pin_bottom)

    def _on_scrolled(self, v: int) -> None:
        """用户滚动 → 判断是否还贴着底（留 4px 容差，避免亚像素抖动判成「离开底部」）。"""
        bar = self.scroll.verticalScrollBar()
        self._stick = v >= bar.maximum() - 4

    def _on_range_changed(self, _lo: int, hi: int) -> None:
        if self._stick:
            self.scroll.verticalScrollBar().setValue(hi)

    def _pin_bottom(self) -> None:
        if not self._stick:
            return
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # 兼容旧调用
    def _scroll_bottom(self) -> None:
        self._pin_bottom()

    def _make_bubble(self, text: str, kind: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("userBubble" if kind == "user" else "agentBubble")
        # 记住原文和类型：窗口/面板变宽变窄时要重排（见 _fit / resizeEvent）
        frame._text, frame._kind = text, kind   # type: ignore[attr-defined]
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(12, 8, 12, 8)
        lb = _label(text, "userText" if kind == "user" else "agentText", True, self._avail())
        frame._label = lb                        # type: ignore[attr-defined]
        lay.addWidget(lb)
        self._fit(frame)
        return frame

    def _avail(self) -> int:
        """气泡里文字的可用宽度：面板宽 − 固定开销（滚动条 + 边框 + 内边距 + 气泡内边距）。

        注意**不能**用 ``scroll.viewport().width()``：在 resizeEvent 里子控件还没按新尺寸
        重新布局，视口会报旧值（实测面板已收到 411，视口仍报 638），按它算就会把
        agent 气泡撑到 400 宽、右端被面板边界切掉。用面板自身宽度做减法是同步准确的。
        """
        w = self.width()
        if w < 200:                    # 还没布局过
            w = 412
        return max(150, w - 72)

    def _fit(self, frame) -> None:
        base = 300 if frame._kind == "user" else 400
        lb = frame._label
        # 用「插过零宽空格」的文本量宽，才是真正要显示的宽度；字体取 label 自己的
        # （主题样式 polish 之前 self.fontMetrics() 还是默认字体，量出来偏小）
        w = _bubble_size(lb.text(), lb.fontMetrics(), min(base, self._avail()))
        frame.setFixedWidth(w + 26)
        lb.setFixedWidth(w)
        # 高度必须自己定：QLabel 的 heightForWidth 不会穿过中间那层布局传上去，
        # 长串一换行，第二行就会被 1 行高的壳子裁掉（「…（92,455 要素）」只剩「要」）。
        lb.setFixedHeight(max(16, lb.heightForWidth(w)))

    def _reflow(self) -> None:
        if not hasattr(self, "msgs"):
            return
        for f in self.msgs.findChildren(QFrame):
            if getattr(f, "_kind", None) in ("user", "agent"):
                self._fit(f)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._reflow()

    def add_user(self, text: str) -> None:
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addStretch(1)
        hl.addWidget(self._make_bubble(text, "user"))
        self._append(row)

    def add_agent(self, text: str, steps: list[dict] | None = None) -> None:
        wrap = QWidget()
        vl = QVBoxLayout(wrap)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(7)

        vl.addWidget(self._make_bubble(text, "agent"), 0, Qt.AlignLeft)

        for i, st in enumerate(steps or []):
            vl.addWidget(StepCard(i, st, self._win))
        self._append(wrap)

    def add_typing(self) -> None:
        frame = QFrame()
        frame.setObjectName("agentBubble")
        fl = QHBoxLayout(frame)
        fl.setContentsMargins(12, 8, 12, 8)
        self._typing_label = QLabel("正在规划并执行…")
        self._typing_label.setObjectName("muted")
        fl.addWidget(self._typing_label)
        self._typing = frame
        self._append(frame)

    def set_typing_text(self, text: str) -> None:
        if self._typing is not None:
            self._typing_label.setText(text)

    def remove_typing(self) -> None:
        if self._typing is not None:
            self._typing.setParent(None)
            self._typing.deleteLater()
            self._typing = None

    def set_busy(self, busy: bool) -> None:
        self.send_btn.setEnabled(not busy)
        # 图标跟着状态换：执行中三个点（并且是灰的，因为按钮 disabled）
        self.send_btn.setIcon(_send_icon(busy=busy, color="#b3ac9d" if busy else "#ffffff"))
        self.send_btn.setToolTip("正在执行…" if busy else "发送（Enter）")
        self.input.setReadOnly(busy)
