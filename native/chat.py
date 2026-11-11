"""对话面板：消息气泡 + 步骤卡片 + 输入区（纯 Qt 控件）。"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, QRect, QSize, QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout, QMenu,
                               QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QTabBar, QTableWidget, QTableWidgetItem,
                               QTextEdit, QVBoxLayout, QWidget)

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
# 输入框的两套占位文案：Agent 反问你的时候，提示要跟着变成「回答问题」
_INPUT_HINT = "描述你要做的空间分析…（用 @ 指定图层）"
_ASK_HINT = "直接回答上面的问题，或点一个选项…"
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


class FlowLayout(QLayout):
    """自动换行的流式布局。

    给长短不一的按钮/标签用：横着排一长串必然超出容器宽度（右边被裁掉一半），
    流式布局按容器宽度自动折行。

    实现参考 Qt 官方 Flow Layout 示例。
    """

    def __init__(self, parent=None, margin: int = 0, h_spacing: int = -1,
                 v_spacing: int = -1):
        super().__init__(parent)
        self._items: list = []
        self._h_space = h_spacing
        self._v_space = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def __del__(self):  # pragma: no cover - Qt 析构顺序
        while self.count():
            self.takeAt(0)

    # -------------------------------------------------------- QLayout 接口
    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(Qt.Orientation(0))

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # ---------------------------------------------------------------- 排版
    def _layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = area.x(), area.y()
        line_h = 0
        for item in self._items:
            widget = item.widget()
            hint = item.sizeHint()
            space_x = self._h_space
            space_y = self._v_space
            if widget is not None:
                style = widget.style()
                if space_x < 0:
                    space_x = style.layoutSpacing(QSizePolicy.PushButton,
                                                 QSizePolicy.PushButton, Qt.Horizontal)
                if space_y < 0:
                    space_y = style.layoutSpacing(QSizePolicy.PushButton,
                                                 QSizePolicy.PushButton, Qt.Vertical)
            next_x = x + hint.width() + space_x
            # 放不下就换行（第一行除外）
            if next_x - space_x > area.right() and line_h > 0:
                x = area.x()
                y = y + line_h + space_y
                next_x = x + hint.width() + space_x
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class StepCard(QFrame):
    """一步算子的执行卡片：标题 / 说明 / 指标 / 告警 / 小表格 / 操作按钮。"""

    def __init__(self, index: int, step: dict, win, avail: int = 330):
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

        bl.addWidget(_label(step.get("message") or "", "stepBody", True, avail))

        kv = kv_text(step.get("data") or {})
        if kv:
            bl.addWidget(_label(kv, "muted", True, avail))

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
        tw.setFocusPolicy(Qt.NoFocus)
        tw.setShowGrid(False)
        # 固定高度必须按「表头 + 每一行」实际高度算，不能按 26px 表头拍脑袋：
        # QSS 的 QHeaderView padding + 字体实际会让表头到 34px，固定 48px 的
        # 小表只给数据区留下 14px，行文字会被裁得几乎看不见（只剩表头）。
        tw.verticalHeader().setDefaultSectionSize(22)
        header_h = 26
        tw.horizontalHeader().setFixedHeight(header_h)
        # 横向策略用 Ignored：表格列宽是内容决定的，最小宽度往往超过面板，
        # 一旦让它参与撑宽，整列消息（尤其右对齐的用户气泡）就会被顶出可视区。
        # 列宽交给表格自己的横向滚动条，窄面板下也能看全。
        tw.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for r, row in enumerate(rows):
            for c, val in enumerate(row[:len(cols)]):
                item = QTableWidgetItem("—" if val is None else str(val))
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                tw.setItem(r, c, item)
        tw.resizeColumnsToContents()
        # 行高由字体决定（setDefaultSectionSize 只是下限），所以最后按真实
        # 行高加总，保证最后一行不会被表格底边裁掉。
        if tw.rowCount():
            tw.resizeRowsToContents()
            rows_h = sum(tw.rowHeight(r) for r in range(tw.rowCount()))
        else:
            rows_h = 22
        tw.setFixedHeight(header_h + rows_h + 2)
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

    def __init__(self, plan: list[dict], source: str = "", win=None, avail: int = 300):
        super().__init__()
        self.setObjectName("planCard")
        self._win = win
        self._rows: list[dict] = []
        self._avail = max(160, int(avail))
        # 横向不参与撑宽：计划卡里的文字再长，也不该把整列消息顶出可视区
        pol = self.sizePolicy()
        pol.setHorizontalPolicy(QSizePolicy.Ignored)
        self.setSizePolicy(pol)

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
        self._source_text = str(source or "")
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
        name = _label(step.get("label") or step.get("tool") or "算子", "planName", True, self._avail)
        detail = _label(self._detail_text(step), "planDetail", True, self._avail)
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

    @staticmethod
    def _public_step(step: dict) -> dict:
        """只留下能 JSON 序列化、回放计划卡需要的字段。

        步进事件里可能带 ``gate``（StepConfirmGate）或 Qt 对象；直接
        ``json.dumps(default=str)`` 虽然不会炸，但会把对象 repr 写进库，
        下次恢复显示一堆 ``<... object at 0x...>``。
        """
        out = {}
        for key in ("label", "tool", "detail", "args", "status", "elapsed",
                    "message", "index", "warnings", "data", "layer"):
            if key in step and step[key] is not None:
                out[key] = step[key]
        return out

    def to_state(self) -> dict:
        """保存给会话回放用：计划来源 + 每一行的最终状态。"""
        return {"source": self._source_text,
                "plan": [self._public_step(row["step"]) for row in self._rows]}


def _restyle(*widgets) -> None:
    """改完动态属性（state）后重新套样式；Qt 不会自动感知属性变化。"""
    for w in widgets:
        w.style().unpolish(w)
        w.style().polish(w)
        w.update()


class _ClarifyOption(QFrame):
    """问题卡里的一个可点选项：序号 + 会换行的正文。

    选项文字经常是一整句中文（「加载北京市/区县行政区划图层，我再按范围裁剪或定位」），
    做成小 chip 只能压成一行 11px 小字，换行之后还读不全。所以这里做成整行可点：
    序号是「这是个可选项」的强提示，正文按面板宽度换行，多长的选项都读得完；
    整行可点、可 Tab 聚焦、回车/空格确认。
    """

    def __init__(self, num: int, text: str, card: "ClarifyCard"):
        super().__init__(card)
        self.setObjectName("clarifyOption")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setProperty("state", "idle")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.text = str(text)
        self._card = card
        self._done = False

        row = QHBoxLayout(self)
        row.setContentsMargins(9, 7, 10, 7)
        row.setSpacing(9)
        self.num_lb = QLabel(str(num))
        self.num_lb.setObjectName("clarifyNum")
        self.num_lb.setAttribute(Qt.WA_StyledBackground, True)
        self.num_lb.setProperty("state", "idle")
        # 数字和正文都不吃鼠标：点哪儿都算点这一行，不会变成拖选文字
        self.num_lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.num_lb.setAlignment(Qt.AlignCenter)
        self.num_lb.setFixedSize(17, 17)
        row.addWidget(self.num_lb, 0, Qt.AlignTop)
        self.lb = _label(self.text, "clarifyOptText", True)
        self.lb.setTextInteractionFlags(Qt.NoTextInteraction)
        self.lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        row.addWidget(self.lb, 1)
        self.setAccessibleName(f"选项 {num}：{self.text}")
        self.setToolTip("点击选择这个答案（Tab 聚焦后按回车也可以）")

    # 尺寸统一由 ClarifyCard.fit_width() 算：QLabel 的换行高度不会穿过布局传上去
    def fit_width(self, text_w: int) -> None:
        w = max(60, int(text_w))
        self.lb.setFixedWidth(w)
        h = max(16, self.lb.heightForWidth(w))
        self.lb.setFixedHeight(h)
        self.setFixedHeight(max(32, h + 14))

    def set_state(self, state: str) -> None:
        """idle / picked / muted（没被选中的那几项）。"""
        for w in (self, self.num_lb, self.lb):
            w.setProperty("state", state)
        if state == "picked":
            self.num_lb.setText("✓")
        if state != "idle":
            self._done = True
            self.setCursor(Qt.ArrowCursor)
            self.setFocusPolicy(Qt.NoFocus)
            self.setToolTip("")
        _restyle(self, self.num_lb, self.lb)

    def _fire(self) -> None:
        if self._done:
            return
        self._card._picked(self)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if (e.button() == Qt.LeftButton and not self._done
                and self.rect().contains(e.position().toPoint())):
            self._fire()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self._fire()
            e.accept()
            return
        super().keyPressEvent(e)


class ClarifyCard(QFrame):
    """Agent 反问你时的那张卡：一句问题 + 几个可点的备选答案。

    以前问题是普通 agent 气泡，底下挂一排小 chip：看起来和普通回复没区别
    （用户不知道在等自己）、长选项读不全、点完也没状态（手滑再点会重复发送）。
    现在收成一张卡：状态标题、带序号的可点整行、点完立刻锁住并标记「已选」。

    ``picked`` 在用户点选后发出；如果用户改用在输入框里回答，
    由 ChatPanel 调 resolve() 把卡收掉，避免旧选项还留在那儿能点。
    """

    picked = Signal(str)
    MAX_W = 560

    def __init__(self, question: str, options: list[str],
                 avail: int = 360):
        super().__init__()
        self.setObjectName("clarifyCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setProperty("state", "open")
        self.awaiting = True
        self.chosen: str | None = None
        self._rows: list[_ClarifyOption] = []
        self._box: QWidget | None = None

        vl = QVBoxLayout(self)
        vl.setContentsMargins(13, 11, 13, 11)
        vl.setSpacing(8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(5)
        self._dot = QLabel("●")
        self._dot.setObjectName("clarifyDot")
        self._dot.setProperty("state", "open")
        head.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._kicker = QLabel("需要你确认")
        self._kicker.setObjectName("clarifyKicker")
        self._kicker.setProperty("state", "open")
        head.addWidget(self._kicker)
        head.addStretch(1)
        vl.addLayout(head)

        self.question_lb = _label(str(question), "clarifyQuestion", True)
        vl.addWidget(self.question_lb)

        if options:
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 2, 0, 0)
            bl.setSpacing(5)
            for i, opt in enumerate(options[:6]):
                row = _ClarifyOption(i + 1, opt, self)
                self._rows.append(row)
                bl.addWidget(row)
            vl.addWidget(box)
            self._box = box
            hint = "点一个选项直接继续；也可以直接在下方输入框回答。"
        else:
            self._box = None
            hint = "在下方输入框里回答即可。"

        self.hint = _label(hint, "clarifyHint", True)
        vl.addWidget(self.hint)
        self.fit_width(avail)

    # ------------------------------------------------------------------ 尺寸
    def fit_width(self, avail: int) -> None:
        """面板变宽变窄时重排：卡宽跟着走，正文按剩余宽度换行。"""
        w = max(220, min(self.MAX_W, int(avail)))
        self.setFixedWidth(w)
        inner = w - 26                       # 左右内边距
        self.question_lb.setFixedWidth(inner)
        self.question_lb.setFixedHeight(max(16, self.question_lb.heightForWidth(inner)))
        self.hint.setFixedWidth(inner)
        self.hint.setFixedHeight(max(14, self.hint.heightForWidth(inner)))
        text_w = inner - 9 - 10 - 17 - 9     # 行内边距 + 序号 + 间距
        for row in self._rows:
            row.fit_width(text_w)

    # ------------------------------------------------------------------ 状态
    def _picked(self, row: _ClarifyOption) -> None:
        if not self.awaiting:
            return
        self.awaiting = False
        self.chosen = row.text
        for r in self._rows:
            r.set_state("picked" if r is row else "muted")
        self._finish("已确认", "已选好，正在按这个答案继续…")
        self.picked.emit(row.text)

    def resolve(self, option: str | None = None, note: str = "") -> None:
        """不通过点选结束这一问（用户在输入框里回答了 / 被新问题取代）。"""
        if not self.awaiting:
            return
        self.awaiting = False
        self.chosen = option
        for r in self._rows:
            r.set_state("muted")
        if self._box is not None and option is None:
            # 用输入框答的：选项已经没意义了，收起来，别让一列灰按钮占着对话高度
            self._box.hide()
        self._finish("已回答", note or "这个问题已经处理过了。")

    def _finish(self, kicker: str, hint: str) -> None:
        self._kicker.setText(kicker)
        self.hint.setText(hint)
        for w in (self, self._dot, self._kicker):
            w.setProperty("state", "done")
        _restyle(self, self._dot, self._kicker)

    def restore(self, chosen: str | None = None, state: str = "open",
                note: str = "") -> None:
        """从会话记录恢复一张历史问题卡，不再可点。

        重启后没有 ``_pending_clarify``，所以不能把旧卡恢复成“等待回答”：
        否则用户点一下会带着空上下文重新规划。已选过的答案按原样标绿，
        没选过的统一收成“已回答 / 未回答”的只读状态。
        """
        self.awaiting = False
        self.chosen = chosen or None
        if chosen:
            for row in self._rows:
                row.set_state("picked" if row.text == chosen else "muted")
            self._finish("已确认" if state == "picked" else "已回答",
                         note or "这一问上次已经回答过了。")
            return
        for row in self._rows:
            row.set_state("muted")
        if self._box is not None and state in ("answered", "superseded"):
            self._box.hide()
        self._finish("已回答" if state == "answered" else "未回答",
                     note or "上次会话没有完成这一问，可以重新描述需求。")


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
        #: 当前正等用户回答的问题卡（同一时间只允许一张可点）
        self._clarify_card: ClarifyCard | None = None

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
        # 消息容器**永远**只跟视口一样宽。
        # QScrollArea 在 setWidgetResizable(True) 时是把容器撑到它自己的 sizeHint，
        # 而不是视口宽度；只要有一条消息（澄清选项、步骤卡里的小表格…）报了个
        # 很大的宽度，容器就会比视口宽，右半截被裁掉 —— 而用户气泡是右对齐的，
        # 于是正好被裁出可视区，看上去就是「用户消息不显示」。
        # 这里显式钉住最小宽度 0，剩下的交给下面每条消息的 Ignored 策略兜底。
        self.msgs.setMinimumWidth(0)
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

        composer = QFrame()
        composer.setObjectName("composer")
        box = QVBoxLayout(composer)
        box.setContentsMargins(10, 6, 7, 6)
        box.setSpacing(4)
        self.input = QTextEdit()
        self.input.setObjectName("chatInput")
        self.input.setPlaceholderText(_INPUT_HINT)
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
        self._clarify_card = None
        self._set_awaiting(False)
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
        # 气泡宽度上限跟着面板走：面板拉宽后长消息应该更宽，而不是卡在 300/400。
        avail = self._avail()
        base = min(avail, 360) if frame._kind == "user" else min(avail, 520)
        lb = frame._label
        # 用「插过零宽空格」的文本量宽，才是真正要显示的宽度；字体取 label 自己的
        # （主题样式 polish 之前 self.fontMetrics() 还是默认字体，量出来偏小）
        w = _bubble_size(lb.text(), lb.fontMetrics(), max(120, base))
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
        avail = max(200, self._avail() - 6)
        for c in self.msgs.findChildren(ClarifyCard):
            c.fit_width(avail)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._reflow()

    @staticmethod
    def _msg_row() -> QWidget:
        """一条消息的外壳。

        横向尺寸策略必须设成 Ignored：QScrollArea 会把消息容器撑到它自己的
        sizeHint，只要有一条消息报了个很大的宽度（澄清选项、步骤卡里的小表格、
        很长的引用），容器就会比视口宽，右半截被裁掉 —— 用户气泡是右对齐的，
        于是正好被裁出可视区，看上去就是「用户消息不显示」。
        """
        row = QWidget()
        pol = row.sizePolicy()
        pol.setHorizontalPolicy(QSizePolicy.Ignored)
        row.setSizePolicy(pol)
        return row

    def add_user(self, text: str) -> None:
        # 用户已经用输入框回答了（或者干脆换了话题）：把还挂着的问题卡收掉，
        # 免得旧选项留在上面，过一会儿手滑又点出一个过期的回答。
        self.resolve_clarify(None, "你已经在下方输入框里回答了这个问题。")
        self.show_chat_tab()
        row = self._msg_row()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addStretch(1)
        hl.addWidget(self._make_bubble(text, "user"))
        self._append(row)

    def add_agent(self, text: str, steps: list[dict] | None = None) -> None:
        self.show_chat_tab()
        wrap = self._msg_row()
        vl = QVBoxLayout(wrap)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(7)

        vl.addWidget(self._make_bubble(text, "agent"), 0, Qt.AlignLeft)

        for i, st in enumerate(steps or []):
            vl.addWidget(StepCard(i, st, self._win, max(200, self._avail() - 26)))
        self._append(wrap)

    def add_steps(self, steps: list[dict]) -> None:
        """只追加步骤结果卡（重启回放时文本和步骤可能分在不同 part）。"""
        if not steps:
            return
        self.show_chat_tab()
        wrap = self._msg_row()
        vl = QVBoxLayout(wrap)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(7)
        for i, st in enumerate(steps):
            vl.addWidget(StepCard(i, st, self._win, max(200, self._avail() - 26)))
        self._append(wrap)

    def add_plan(self, card: PlanCard) -> None:
        """追加执行计划卡（步骤状态由主窗口的流事件更新）。"""
        self.show_chat_tab()
        self._append(card)

    def add_clarify(self, question: str, options: list[str],
                   on_pick: Callable[[str], None]) -> ClarifyCard:
        """渲染一张「需要你回答」的问题卡，点选项就等于回答了问题。

        点选的文本会原样回填给 Agent，让它带着答案重新规划（而不是让用户再打一遍）；
        也可以忽略选项、直接在输入框里回答 —— 那会通过 resolve_clarify() 把卡收掉。

        同一时间只留一张可点的卡：后一个问题来了，前一张先标成「已被取代」。
        """
        self.resolve_clarify(None, "这个问题已经被新的问题取代。")
        self.show_chat_tab()

        card = ClarifyCard(question, options, max(200, self._avail() - 6))

        def _relay(text: str) -> None:
            if self._clarify_card is card:
                self._clarify_card = None
            self._set_awaiting(False)
            on_pick(text)

        card.picked.connect(_relay)
        self._clarify_card = card
        self._append(card)
        self._set_awaiting(True)
        # 这是等着用户动手的问题：就算他正翻着上面的历史，也把他带到问题这里，
        # 否则消息沉在下面，状态栏提示一句「需要你确认」人却看不见问题。
        self._stick = True
        self._pin_bottom()
        return card

    def restore_clarify(self, card: ClarifyCard, chosen: str | None = None,
                        state: str = "open", note: str = "") -> None:
        """恢复历史问题卡并把它从“等待回答”状态里摘掉。"""
        card.restore(chosen, state, note)
        if self._clarify_card is card:
            self._clarify_card = None
        self._set_awaiting(False)

    def resolve_clarify(self, option: str | None = None, note: str = "") -> None:
        """把当前问题卡收成只读（用户用输入框回答 / 问题被新问题取代）。"""
        card = self._clarify_card
        if card is None:
            return
        self._clarify_card = None
        card.resolve(option, note)
        self._set_awaiting(False)

    def _set_awaiting(self, on: bool) -> None:
        """问题等着回答时，输入框的占位文案跟着变 —— 不然用户不知道该在哪儿答。"""
        if not hasattr(self, "input"):
            return
        self.input.setPlaceholderText(_ASK_HINT if on else _INPUT_HINT)

    def add_typing(self) -> None:
        frame = QFrame()
        frame.setObjectName("agentBubble")
        pol = frame.sizePolicy()
        pol.setHorizontalPolicy(QSizePolicy.Ignored)
        frame.setSizePolicy(pol)
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
