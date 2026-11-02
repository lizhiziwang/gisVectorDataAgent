"""对话面板：消息气泡 + 步骤卡片 + 输入区（纯 Qt 控件）。"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSize, QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,
                               QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
                               QTabBar, QTableWidget, QTableWidgetItem, QTextEdit,
                               QVBoxLayout, QWidget)

from .format import kv_text
from .icons import panel_icon


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
            if str(lay.get("geomType") or "") == "Raster":
                r = lay.get("raster") or {}
                tag = (f"＋ 新栅格图层「{lay.get('name')}」· "
                       f"{r.get('width')}×{r.get('height')} 像元 · {r.get('bands')} 波段")
            else:
                tag = f"＋ 新图层「{lay.get('name')}」· {lay.get('count')} 要素"
            bl.addWidget(_label(tag, "newLayerTag"))

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


class PlanCard(QFrame):
    """执行计划卡：先列出所有步骤，再随执行逐行变成「执行中 / 完成 / 失败」。

    和最终结果卡不同，这里放的是**计划阶段**就能知道的信息（算子、目标图层、
    参数），不依赖算子返回；用户不用盯着一个「正在规划并执行…」转圈。
    """

    _DOT = {
        "pending": "#c9c4b8",
        "confirm": "#6f8fb5",
        "running": "#d8a54a",
        "ok": "#3f7d58",
        "warn": "#d8a54a",
        "error": "#b04a3f",
        "skipped": "#a9a396",
        "cancelled": "#b04a3f",
    }

    def __init__(self, plan: list[dict], source: str = "", win=None):
        super().__init__()
        self.setObjectName("planCard")
        self._win = win
        self._rows: list[dict] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(5)

        head = QHBoxLayout()
        head.setSpacing(7)
        title = QLabel("工具调用计划")
        title.setObjectName("planTitle")
        head.addWidget(title)
        src = "大模型规划" if source in ("llm", "chat") else "规则规划"
        self._source = QLabel(src)
        self._source.setObjectName("planSource")
        head.addWidget(self._source)
        head.addStretch(1)
        self._count = QLabel(f"0/{max(0, len(plan))} 完成")
        self._count.setObjectName("planCount")
        head.addWidget(self._count)
        outer.addLayout(head)

        self._host = QWidget()
        self._vl = QVBoxLayout(self._host)
        self._vl.setContentsMargins(0, 0, 0, 0)
        self._vl.setSpacing(0)
        outer.addWidget(self._host)

        for i, st in enumerate(plan or []):
            self._add_row(i, st)
        self._refresh_count()

    # ---------------------------------------------------------------- 行
    @staticmethod
    def _detail_text(step: dict) -> str:
        detail = str(step.get("detail") or "").strip()
        if detail:
            return detail
        args = step.get("args") or {}
        if not isinstance(args, dict):
            return ""
        bits = []
        for k in ("layer", "other", "mask", "layer2", "join_layer", "raster"):
            v = args.get(k)
            if isinstance(v, str) and v:
                bits.append(v)
        return " · ".join(bits[:3])

    def _add_row(self, index: int, step: dict) -> dict:
        row = QFrame()
        row.setObjectName("planRow")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(2, 4, 2, 4)
        hl.setSpacing(7)

        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("background:%s;border-radius:4px;"
                          % self._DOT["pending"])
        hl.addWidget(dot, 0, Qt.AlignTop)

        num = QLabel(f"{index + 1:02d}")
        num.setObjectName("planNum")
        num.setFixedWidth(20)
        hl.addWidget(num, 0, Qt.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(1)
        name = _label(step.get("label") or step.get("tool") or "算子", "planName", True, 260)
        detail = _label(self._detail_text(step), "planDetail", True, 300)
        detail.setVisible(bool(detail.text()))
        info.addWidget(name)
        info.addWidget(detail)
        hl.addLayout(info, 1)

        state = _label("等待", "planState", False)
        state.setFixedWidth(56)
        state.setAlignment(Qt.AlignRight | Qt.AlignTop)
        hl.addWidget(state, 0, Qt.AlignTop)

        self._vl.addWidget(row)
        item = {"dot": dot, "name": name, "detail": detail, "state": state,
                "step": dict(step or {})}
        self._rows.append(item)
        return item

    def update_step(self, index: int, payload: dict) -> None:
        if index < 0:
            return
        while index >= len(self._rows):
            self._add_row(len(self._rows), payload)
        item = self._rows[index]
        st = str(payload.get("status") or "running")
        item["step"].update(payload)
        color = self._DOT.get(st, self._DOT["pending"])
        item["dot"].setStyleSheet("background:%s;border-radius:4px;" % color)
        if payload.get("label"):
            item["name"].setText(_soft_break(str(payload["label"])))
        if payload.get("detail"):
            item["detail"].setText(_soft_break(str(payload["detail"])))
            item["detail"].setVisible(True)
        el = payload.get("elapsed") or 0
        if st == "running":
            item["state"].setText("执行中…")
        elif st == "confirm":
            item["state"].setText("待确认")
        elif st == "pending":
            item["state"].setText("等待")
        elif st == "skipped":
            item["state"].setText("已跳过")
        elif st == "cancelled":
            item["state"].setText("已取消")
            msg = str(payload.get("message") or "").strip()
            if msg:
                item["detail"].setText(_soft_break(msg))
                item["detail"].setVisible(True)
        elif st == "error":
            item["state"].setText("失败")
            msg = str(payload.get("message") or "").strip()
            if msg:
                item["detail"].setText(_soft_break(msg))
                item["detail"].setVisible(True)
        elif st == "warn":
            item["state"].setText(f"{el:.1f}s" if el else "完成")
        else:
            item["state"].setText(f"{el:.1f}s" if el else "完成")
        self._refresh_count()

    def finish(self, failed: bool = False) -> None:
        if failed:
            text = f"{sum(1 for r in self._rows if r['step'].get('status') == 'error')} 步失败"
        else:
            skipped = sum(1 for r in self._rows
                          if r["step"].get("status") == "skipped")
            done = sum(1 for r in self._rows
                       if r["step"].get("status") in ("ok", "warn", "error"))
            text = f"{done}/{len(self._rows)} 完成"
            if skipped:
                text += f" · {skipped} 跳过"
        self._count.setText(text)
        self._count.setObjectName("planErr" if failed else "planCount")
        self._count.style().unpolish(self._count)
        self._count.style().polish(self._count)

    def fail(self, message: str = "") -> None:
        for item in self._rows:
            if str(item["step"].get("status") or "pending") in ("running", "pending", "confirm"):
                item["state"].setText("未执行")
        self._count.setText("已中止" if not message else "执行出错")
        self._count.setObjectName("planErr")
        self._count.style().unpolish(self._count)
        self._count.style().polish(self._count)

    def _refresh_count(self) -> None:
        done = sum(1 for r in self._rows
                   if str(r["step"].get("status") or "") in
                   ("ok", "warn", "error", "skipped", "cancelled"))
        self._count.setText(f"{done}/{len(self._rows)} 完成")


class ChatPanel(QWidget):
    sent = Signal(str)

    def __init__(self, win):
        super().__init__()
        self._win = win
        self._typing: QFrame | None = None
        self._hist: list[str] = []      # 发过的指令，↑/↓ 翻（shell 手感）
        self._hist_pos = -1
        #: @ 补全弹窗：懒创建
        self._mention_popup = None
        #: 当前 @ 的位置（-1 表示没有）
        self._mention_at = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 头部：助手标题 + 后台任务胶囊 + 面板开关 + 清空
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 8, 8, 6)
        hl.setSpacing(8)
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
        self.task_pill = QPushButton("")
        self.task_pill.setObjectName("taskPill")
        self.task_pill.setCursor(Qt.PointingHandCursor)
        self.task_pill.setToolTip("后台任务运行中")
        self.task_pill.hide()
        self.task_pill.clicked.connect(
            lambda _=False: getattr(getattr(self._win, "task_notice", None),
                                    "refresh", lambda: None)())
        hl.addWidget(self.task_pill)
        self.pane_btn = QPushButton()
        self.pane_btn.setObjectName("icon")
        self.pane_btn.setFixedSize(26, 26)
        self.pane_btn.setIcon(panel_icon())
        self.pane_btn.setIconSize(QSize(15, 15))
        self.pane_btn.setToolTip("显示 / 隐藏左侧工作区")
        self.pane_btn.setCursor(Qt.PointingHandCursor)
        self.pane_btn.clicked.connect(
            lambda _=False: getattr(self._win, "toggle_left_panel", lambda: None)())
        hl.addWidget(self.pane_btn)
        self.clear_btn = QPushButton("⟲")
        self.clear_btn.setObjectName("icon")
        self.clear_btn.setFixedSize(26, 26)
        self.clear_btn.setToolTip("清空图层与对话")
        hl.addWidget(self.clear_btn)
        root.addWidget(head)

        self.tabs = QTabBar()
        self.tabs.setObjectName("chatTabs")
        self.tabs.addTab("对话")
        self.tabs.addTab("轨迹")
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        tab_wrap = QWidget()
        tw = QHBoxLayout(tab_wrap)
        tw.setContentsMargins(14, 0, 10, 0)
        tw.addWidget(self.tabs)
        tw.addStretch(1)
        root.addWidget(tab_wrap)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background:#e7e3d8;")
        root.addWidget(line)

        self.chat_stack = QStackedWidget()

        # 消息页
        chat_page = QWidget()
        cp = QVBoxLayout(chat_page)
        cp.setContentsMargins(0, 0, 0, 0)
        cp.setSpacing(0)
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
        self.msg_layout.setContentsMargins(16, 14, 16, 10)
        self.msg_layout.setSpacing(14)
        self.msg_layout.addStretch(1)
        self.scroll.setWidget(self.msgs)
        cp.addWidget(self.scroll, 1)
        self.chat_stack.addWidget(chat_page)

        # 轨迹页：工具调用流水（只读，不参与消息气泡滚动）
        self.trace_view = QPlainTextEdit()
        self.trace_view.setObjectName("traceView")
        self.trace_view.setReadOnly(True)
        self.trace_view.setPlaceholderText("工具调用轨迹会出现在这里。")
        self.chat_stack.addWidget(self.trace_view)
        root.addWidget(self.chat_stack, 1)

        # 「贴底」：人在底部时，新消息、表格、气泡重排长高之后都自动跟到最新；
        # 手动往上翻就停在他看的位置，不再被强行拽回去。
        self._stick = True
        _bar = self.scroll.verticalScrollBar()
        _bar.valueChanged.connect(self._on_scrolled)
        _bar.rangeChanged.connect(self._on_range_changed)

        # 输入区
        foot = QWidget()
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(10, 6, 10, 8)
        fl.setSpacing(5)

        # 当前工具调用状态（正在调用工具 · xxx）
        self.tool_activity = QFrame()
        self.tool_activity.setObjectName("toolActivity")
        al = QHBoxLayout(self.tool_activity)
        al.setContentsMargins(8, 4, 8, 4)
        al.setSpacing(6)
        self.tool_spinner = QLabel("◌")
        self.tool_spinner.setObjectName("toolSpinner")
        al.addWidget(self.tool_spinner)
        self.tool_label = QLabel("")
        self.tool_label.setObjectName("toolActivityText")
        al.addWidget(self.tool_label, 1)
        self.tool_activity.hide()
        fl.addWidget(self.tool_activity)

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
        box = QVBoxLayout(composer)
        box.setContentsMargins(10, 6, 7, 6)
        box.setSpacing(4)
        self.input = QTextEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText("描述你要做的空间分析…（用 @ 指定图层）")
        self.input.setFixedHeight(38)
        self.input.installEventFilter(self)
        self.input.textChanged.connect(self._on_input_changed)
        box.addWidget(self.input)

        # 输入框内部底栏：模型选择器在发送键左侧（和 Harness 一致）
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.model_btn = QPushButton("模型")
        self.model_btn.setObjectName("modelPicker")
        self.model_btn.setCursor(Qt.PointingHandCursor)
        self.model_btn.setToolTip("当前规划模型 / 打开模型设置")
        self.model_btn.clicked.connect(self._open_model_menu)
        bottom.addStretch(1)
        bottom.addWidget(self.model_btn)
        self.send_btn = QPushButton()
        self.send_btn.setObjectName("send")
        self.send_btn.setFixedSize(30, 30)
        self.send_btn.setIconSize(QSize(16, 16))
        self.send_btn.setIcon(_send_icon())
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.setToolTip("发送（Enter）")
        self.send_btn.clicked.connect(lambda _checked=False: self._emit_send())
        bottom.addWidget(self.send_btn, 0, Qt.AlignBottom)
        box.addLayout(bottom)
        fl.addWidget(composer)

        self.stats = QLabel("0 轮 · 0 步")
        self.stats.setObjectName("chatStats")
        self.stats.setAlignment(Qt.AlignRight)
        fl.addWidget(self.stats)

        hint = QLabel("Enter 发送 · Shift+Enter 换行 · 原始数据只读，有产出才建新图层")
        hint.setObjectName("tiny")
        fl.addWidget(hint)
        root.addWidget(foot)
        self._refresh_model_button()

    # ------------------------------------------------------------------ 事件
    def eventFilter(self, obj, event):  # noqa: N802
        from PySide6.QtCore import QEvent
        if obj is self.input and event.type() == QEvent.KeyPress:
            # @ 补全弹窗开着时，方向键/回车/ESC 都归它管
            if self._mention_popup is not None and self._mention_popup.isVisible():
                if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                    self._mention_popup.accept()
                    return True
                if event.key() == Qt.Key_Escape:
                    self._close_mention_popup()
                    return True
                if event.key() == Qt.Key_Up:
                    self._mention_popup.move_selection(-1)
                    return True
                if event.key() == Qt.Key_Down:
                    self._mention_popup.move_selection(1)
                    return True
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

    def _app_key_filter(self, obj, event) -> bool:
        """应用级兜底：@ 弹窗开着时，回车/ESC/↑↓ 一律归它管。

        输入框上的事件过滤器只在焦点在输入框时管用。
        万一焦点跑到别处（Popup 抢焦点、用户点了别的地方），
        回车就变成「发消息」而不是「选图层」——
        这里在应用层面再兜一次，保证行为一致。
        """
        from PySide6.QtCore import QEvent
        if (self._mention_popup is None or not self._mention_popup.isVisible()
                or event.type() != QEvent.KeyPress):
            return False
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
            self._mention_popup.accept()
            return True
        if key == Qt.Key_Escape:
            self._close_mention_popup()
            return True
        if key == Qt.Key_Up:
            self._mention_popup.move_selection(-1)
            return True
        if key == Qt.Key_Down:
            self._mention_popup.move_selection(1)
            return True
        return False

    def _set_input(self, text: str) -> None:
        self.input.setPlainText(text)
        cur = self.input.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.input.setTextCursor(cur)

    # -------------------------------------------------------------- @ 补全
    def _mention_layers(self) -> list:
        """当前可用图层（供补全列表用）。"""
        store = getattr(self._win, "store", None)
        if store is None:
            return []
        try:
            return list(store.layers.values())
        except Exception:
            return []

    def _mention_popup_obj(self):
        """懒创建补全弹窗（第一次用到时才建）。"""
        if self._mention_popup is None:
            from .mention_popup import MentionPopup
            self._mention_popup = MentionPopup()
            self._mention_popup.picked.connect(self._on_mention_picked)
            self._mention_popup.dismissed.connect(self._close_mention_popup)
            # 应用级兜底：焦点不在输入框时，回车也要能选图层
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self)
        return self._mention_popup

    def _on_input_changed(self) -> None:
        """输入变化时，判断要不要弹 @ 补全。"""
        cur = self.input.textCursor()
        if not cur.hasSelection():
            pos = cur.position()
            upto = self.input.toPlainText()[:pos]
            # 光标前最近的 @，中间不能有空格（否则 @ 只是普通字符）
            at = upto.rfind("@")
            if at >= 0:
                between = upto[at + 1:]
                if " " not in between and "\n" not in between and len(between) <= 40:
                    query = between
                    layers = self._mention_layers()
                    if layers:
                        popup = self._mention_popup_obj()
                        popup.set_candidates(layers, query)
                        if popup.count():
                            self._mention_at = at
                            rect = self.input.cursorRect()
                            popup.show_at(self.input, rect.topLeft())
                            return
        self._close_mention_popup()

    def _close_mention_popup(self) -> None:
        if self._mention_popup is not None and self._mention_popup.isVisible():
            self._mention_popup.hide()
        self._mention_at = -1

    def _on_mention_picked(self, name: str) -> None:
        """选中了一个图层：把 @查询词 换成 @图层名。"""
        at = self._mention_at
        self._close_mention_popup()
        if at < 0:
            return
        text = self.input.toPlainText()
        cur = self.input.textCursor()
        pos = cur.position()
        # 从 @ 一直换到光标处
        new_text = text[:at] + "@" + name + text[pos:]
        self.input.setPlainText(new_text)
        # 光标停在图层名后面
        new_cur = self.input.textCursor()
        new_cur.setPosition(min(at + 1 + len(name), len(new_text)))
        self.input.setTextCursor(new_cur)
        self.input.setFocus()

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

    # ---------------------------------------------------------------- Harness 顶栏
    def _refresh_model_button(self) -> None:
        try:
            from app.config import load_cfg
            cfg = load_cfg()
        except Exception:
            cfg = {}
        model = str(cfg.get("model") or "规则引擎")
        if not cfg.get("enabled", True):
            model = "规则引擎"
        if not model:
            model = "规则引擎"
        self.model_btn.setText(f"{model} · Max  ▾")

    def _open_model_menu(self) -> None:
        menu = QMenu(self)
        try:
            from app.config import load_cfg
            cfg = load_cfg()
            model = str(cfg.get("model") or "规则引擎")
            if not cfg.get("enabled", True):
                model = "规则引擎"
        except Exception:
            model = "当前模型"
        act = menu.addAction(model)
        act.setEnabled(False)
        menu.addSeparator()
        menu.addAction("打开模型设置…", lambda: self._win.open_settings())
        menu.addAction("刷新模型信息", self._refresh_model_button)
        menu.exec(self.model_btn.mapToGlobal(self.model_btn.rect().bottomLeft()))

    def set_background_tasks(self, running: int, pending: int = 0) -> None:
        n = int(running or 0) + int(pending or 0)
        if n <= 0:
            self.task_pill.hide()
            return
        extra = f" · {pending} 排队" if pending else ""
        self.task_pill.setText(f"◌ {n} 个后台任务运行中{extra}")
        self.task_pill.show()

    def set_tool_activity(self, name: str = "", active: bool = True) -> None:
        if not active or not name:
            self.tool_activity.hide()
            return
        self.tool_label.setText(f"正在调用工具 · {name}")
        self.tool_activity.show()

    def set_stats(self, turns: int = 0, steps: int = 0, tok_s: float = 0.0,
                  total_tokens: int = 0, cache_pct: float | None = None,
                  context_pct: float | None = None) -> None:
        bits = [f"{int(turns)} 轮", f"{int(steps)} 步"]
        if tok_s > 0:
            bits.append(f"{tok_s:.0f} tok/s")
        if total_tokens:
            if total_tokens >= 1_000_000_000:
                tok_text = f"{total_tokens / 1_000_000_000:.1f}B"
            elif total_tokens >= 1_000_000:
                tok_text = f"{total_tokens / 1_000_000:.1f}M"
            else:
                tok_text = f"{total_tokens / 1000:.0f}K"
            bits.append(f"{tok_text} tok")
        if cache_pct is not None:
            bits.append(f"缓存 {cache_pct:.1f}%")
        if context_pct is not None:
            bits.append(f"上下文 {context_pct:.0f}%")
        self.stats.setText(" · ".join(bits))

    def add_trace(self, text: str) -> None:
        """往「轨迹」页追加一条工具调用/状态流水。"""
        stamp = __import__("time").strftime("%H:%M:%S")
        self.trace_view.appendPlainText(f"[{stamp}] {text}")

    def _on_tab_changed(self, index: int) -> None:
        if self.chat_stack is not None:
            self.chat_stack.setCurrentIndex(1 if index == 1 else 0)

    def show_chat_tab(self) -> None:
        if self.tabs.currentIndex() != 0:
            self.tabs.setCurrentIndex(0)

    def clear(self) -> None:
        while self.msg_layout.count() > 1:
            it = self.msg_layout.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        try:
            self.trace_view.clear()
            self.set_tool_activity("", False)
        except Exception:
            pass

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
        self.show_chat_tab()
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addStretch(1)
        hl.addWidget(self._make_bubble(text, "user"))
        self._append(row)

    def add_agent(self, text: str, steps: list[dict] | None = None) -> None:
        self.show_chat_tab()
        wrap = QWidget()
        vl = QVBoxLayout(wrap)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(7)

        vl.addWidget(self._make_bubble(text, "agent"), 0, Qt.AlignLeft)

        for i, st in enumerate(steps or []):
            vl.addWidget(StepCard(i, st, self._win))
        self._append(wrap)

    def add_plan(self, card: PlanCard) -> None:
        """追加执行计划卡（步骤状态由主窗口的流事件更新）。"""
        self.show_chat_tab()
        self._append(card)

    def add_clarify(self, question: str, options: list[str],
                   on_pick: Callable[[str], None]) -> None:
        """渲染澄清问题 + 可点选项。

        点一个选项就等于用户回答了这个问题——直接把选中的文本回填给 Agent，
        让它带着答案重新规划（而不是让用户再打一遍）。
        """
        wrap = QWidget()
        vl = QVBoxLayout(wrap)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(6)

        vl.addWidget(self._make_bubble(question, "agent"), 0, Qt.AlignLeft)

        if options:
            row = QWidget()
            hl = QHBoxLayout(row)
            hl.setContentsMargins(2, 0, 0, 0)
            hl.setSpacing(5)
            for opt in options[:6]:
                b = QPushButton(str(opt))
                b.setObjectName("chip")
                b.setCursor(Qt.PointingHandCursor)
                b.setToolTip(f"选择「{opt}」")
                b.clicked.connect(lambda _=False, t=str(opt): on_pick(t))
                hl.addWidget(b)
            hl.addStretch(1)
            vl.addWidget(row)
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
