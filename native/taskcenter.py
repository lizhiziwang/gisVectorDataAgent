"""任务中心：可折叠的任务队列面板。

设计文档 §5.5 的桌面端实现：
    ● 正在执行   对地块做 500 米缓冲区            [取消]
      步骤 1/2  缓冲区  0.8s
    ◌ 排队中     统计道路长度                     [取消]
    ✓ 已完成     导入 道路.shp            12s  [查看图层]
    ! 失败       自定义算子生成                 [重试] [看日志]

只依赖 Qt + 一个「快照提供方」回调，不认识 TaskScheduler，
方便单独测试和以后接 CLI。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

#: 状态 → (符号, 颜色, 中文)
STATUS_STYLE = {
    "running":    ("●", "#d97757", "执行中"),
    "queued":     ("◌", "#8a8578", "排队中"),
    "succeeded":  ("✓", "#6b8f5a", "已完成"),
    "failed":     ("!", "#c0503a", "失败"),
    "cancelled":  ("⊘", "#8a8578", "已取消"),
    "interrupted": ("!", "#b8860b", "未完成"),
    "waiting":    ("◌", "#8a8578", "等待中"),
}


class TaskRow(QFrame):
    """一行任务。"""

    cancel_requested = Signal(str)   # task_id
    retry_requested = Signal(str)    # task_id
    log_requested = Signal(str)      # task_id
    locate_requested = Signal(str)   # layer_id

    def __init__(self, task: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task = task
        self.task_id = str(task.get("id") or "")
        self.setObjectName("taskRow")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        vl = QVBoxLayout(self)
        vl.setContentsMargins(10, 7, 10, 7)
        vl.setSpacing(3)

        # 第一行：状态符号 + 标签 + 操作按钮
        hl = QHBoxLayout()
        hl.setSpacing(6)
        hl.setContentsMargins(0, 0, 0, 0)
        self.dot = QLabel()
        self.dot.setObjectName("taskDot")
        hl.addWidget(self.dot)

        self.title = QLabel()
        self.title.setObjectName("taskTitle")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hl.addWidget(self.title, 1)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("tiny")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.setToolTip("取消这个任务")
        self.btn_cancel.clicked.connect(lambda: self.cancel_requested.emit(self.task_id))
        hl.addWidget(self.btn_cancel)

        self.btn_retry = QPushButton("重试")
        self.btn_retry.setObjectName("tiny")
        self.btn_retry.setCursor(Qt.PointingHandCursor)
        self.btn_retry.setToolTip("重新执行这个任务")
        self.btn_retry.clicked.connect(lambda: self.retry_requested.emit(self.task_id))
        hl.addWidget(self.btn_retry)

        self.btn_log = QPushButton("日志")
        self.btn_log.setObjectName("tiny")
        self.btn_log.setCursor(Qt.PointingHandCursor)
        self.btn_log.setToolTip("查看这次运行的完整日志")
        self.btn_log.clicked.connect(lambda: self.log_requested.emit(self.task_id))
        hl.addWidget(self.btn_log)

        vl.addLayout(hl)

        # 第二行：状态文字 + 耗时 / 错误
        self.sub = QLabel()
        self.sub.setObjectName("taskSub")
        self.sub.setWordWrap(True)
        vl.addWidget(self.sub)

        self._apply()

    def _apply(self) -> None:
        t = self.task
        status = str(t.get("status") or "queued")
        symbol, color, label = STATUS_STYLE.get(status, ("·", "#8a8578", status))
        self.dot.setText(symbol)
        self.dot.setStyleSheet(f"color:{color}; font-size:13px;")
        name = str(t.get("label") or t.get("kind") or "任务")
        self.title.setText(f"{name}")

        detail = label
        elapsed = t.get("elapsed") or 0
        if status == "running":
            detail += f" · {elapsed:.1f}s"
        elif status in ("succeeded", "failed", "cancelled", "interrupted"):
            detail += f" · {elapsed:.1f}s"
        err = t.get("error")
        if err:
            detail += f" · {str(err)[:120]}"
        self.sub.setText(detail)

        # 按钮按状态显隐
        self.btn_cancel.setVisible(status in ("queued", "running"))
        self.btn_retry.setVisible(status in ("failed", "cancelled", "interrupted"))
        self.btn_log.setVisible(status in ("failed", "succeeded", "interrupted"))


class TaskCenter(QFrame):
    """任务中心面板（默认折叠，有任务时自动展开）。"""

    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    log_requested = Signal(str)

    def __init__(self, snapshot: Callable[[], list[dict]] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._snapshot = snapshot or (lambda: [])
        self.setObjectName("taskCenter")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # 折叠头
        head = QFrame()
        head.setObjectName("taskHead")
        head.setCursor(Qt.PointingHandCursor)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 7, 12, 7)
        hl.setSpacing(6)
        self.head_label = QLabel("任务中心")
        self.head_label.setObjectName("taskHeadTitle")
        hl.addWidget(self.head_label)
        self.badge = QLabel("")
        self.badge.setObjectName("taskBadge")
        hl.addWidget(self.badge)
        hl.addStretch(1)
        self.btn_clear = QPushButton("清除已完成")
        self.btn_clear.setObjectName("tiny")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setToolTip("从列表里移除已结束的任务")
        hl.addWidget(self.btn_clear)
        self.arrow = QLabel("▾")
        self.arrow.setObjectName("taskHeadTitle")
        hl.addWidget(self.arrow)
        vl.addWidget(head)

        # 任务列表
        self.scroll = QScrollArea()
        self.scroll.setObjectName("taskScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setMaximumHeight(230)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("taskBody")
        self.rows_layout = QVBoxLayout(self.body)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.rows_layout.addStretch(1)
        self.scroll.setWidget(self.body)
        vl.addWidget(self.scroll)

        self._expanded = False
        self._should_show = False
        self._rows: dict[str, TaskRow] = {}
        #: 已被用户「清除」的 task id —— 下次 refresh 时不再显示
        self._dismissed: set[str] = set()
        head.mousePressEvent = self._toggle   # type: ignore[assignment]
        self.btn_clear.clicked.connect(self._clear_finished)
        self.cancel_requested.connect(self._on_cancel)
        self.retry_requested.connect(self._on_retry)
        self.log_requested.connect(self._on_log)

        self.setVisible(False)

    # ------------------------------------------------------------------ 交互
    def _toggle(self, _event=None) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.scroll.setVisible(expanded)
        self.arrow.setText("▾" if expanded else "▸")

    def _clear_finished(self) -> None:
        """从列表里移除已结束的任务。

        移除只是「不再显示」——任务记录仍在 DB 里，日志照样能看。
        """
        for tid, row in list(self._rows.items()):
            if row.task.get("status") not in ("queued", "running", "waiting"):
                row.setParent(None)
                row.deleteLater()
                self._rows.pop(tid, None)
                self._dismissed.add(tid)
        self.refresh()

    def _on_cancel(self, task_id: str) -> None:
        pass  # 由外部接上真正的取消逻辑

    def _on_retry(self, task_id: str) -> None:
        pass

    def _on_log(self, task_id: str) -> None:
        pass

    # ------------------------------------------------------------------ 刷新
    def refresh(self) -> None:
        """从快照重建列表（保持已展开状态）。"""
        try:
            tasks = self._snapshot() or []
        except Exception:
            tasks = []
        # 最多显示最近 30 条
        tasks = tasks[:30]
        seen: set[str] = set()
        for t in tasks:
            tid = str(t.get("id") or "")
            if not tid or tid in self._dismissed:
                continue
            seen.add(tid)
            row = self._rows.get(tid)
            if row is None:
                row = TaskRow(t, self)
                row.cancel_requested.connect(self.cancel_requested)
                row.retry_requested.connect(self.retry_requested)
                row.log_requested.connect(self.log_requested)
                # 插到 stretch 之前
                self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
                self._rows[tid] = row
            else:
                row.task = t
                row._apply()
        # 移除已不存在的
        for tid, row in list(self._rows.items()):
            if tid not in seen:
                row.setParent(None)
                row.deleteLater()
                self._rows.pop(tid, None)
        # 清除标记只保留最近的一批，别无限涨
        if len(self._dismissed) > 200:
            self._dismissed = set(list(self._dismissed)[-100:])

        active = sum(1 for t in tasks if t.get("status") in ("running", "queued", "waiting"))
        running = sum(1 for t in tasks if t.get("status") == "running")
        pending = sum(1 for t in tasks if t.get("status") in ("queued", "waiting"))
        # 记录「应该显示」的状态：父面板还没 show 出来时 isVisible() 会是 False，
        # 用这个标志做断言更可靠。
        self._should_show = bool(active)
        if active:
            self.badge.setText(f"{running} 执行 · {pending} 排队" if pending else f"{running} 执行")
            self.badge.setVisible(True)
            if not self._expanded:
                self.set_expanded(True)
            self.setVisible(True)
        else:
            self.badge.setVisible(False)
            self.setVisible(False)
            self.set_expanded(False)

    @property
    def should_show(self) -> bool:
        """面板「应该显示」吗（不依赖父窗口是否已 show）。"""
        return self._should_show


class TaskNoticeRow(QFrame):
    """一条漂浮通知行：状态 + 标题 + 耗时 + 当前状态可用的操作。"""

    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    log_requested = Signal(str)
    dismiss_requested = Signal(str)

    def __init__(self, task: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task = task
        self.task_id = str(task.get("id") or "")
        self.setObjectName("taskNoticeRow")

        hl = QHBoxLayout(self)
        hl.setContentsMargins(9, 5, 8, 5)
        hl.setSpacing(7)

        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        hl.addWidget(self.dot, 0, Qt.AlignTop)

        info = QVBoxLayout()
        info.setSpacing(1)
        self.title = QLabel()
        self.title.setObjectName("taskNoticeName")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info.addWidget(self.title)
        self.sub = QLabel()
        self.sub.setObjectName("taskNoticeSub")
        info.addWidget(self.sub)
        hl.addLayout(info, 1)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("tiny")
        self.btn_cancel.setCursor(Qt.PointingHandCursor)
        self.btn_cancel.clicked.connect(lambda: self.cancel_requested.emit(self.task_id))
        hl.addWidget(self.btn_cancel)

        self.btn_retry = QPushButton("重试")
        self.btn_retry.setObjectName("tiny")
        self.btn_retry.setCursor(Qt.PointingHandCursor)
        self.btn_retry.clicked.connect(lambda: self.retry_requested.emit(self.task_id))
        hl.addWidget(self.btn_retry)

        self.btn_log = QPushButton("日志")
        self.btn_log.setObjectName("tiny")
        self.btn_log.setCursor(Qt.PointingHandCursor)
        self.btn_log.clicked.connect(lambda: self.log_requested.emit(self.task_id))
        hl.addWidget(self.btn_log)

        self.btn_dismiss = QPushButton("×")
        self.btn_dismiss.setObjectName("taskNoticeClose")
        self.btn_dismiss.setFixedSize(18, 18)
        self.btn_dismiss.setCursor(Qt.PointingHandCursor)
        self.btn_dismiss.setToolTip("移除这条通知")
        self.btn_dismiss.clicked.connect(lambda: self.dismiss_requested.emit(self.task_id))
        hl.addWidget(self.btn_dismiss)

        self._apply()

    def _apply(self) -> None:
        t = self.task
        status = str(t.get("status") or "queued")
        symbol, color, label = STATUS_STYLE.get(status, ("·", "#8a8578", status))
        self.dot.setStyleSheet(f"background:{color};border-radius:4px;")
        name = str(t.get("label") or t.get("kind") or "任务")
        self.title.setText(name)
        elapsed = float(t.get("elapsed") or 0)
        if status == "running":
            self.sub.setText(f"{label} · {elapsed:.1f}s")
        elif status in ("queued", "waiting"):
            self.sub.setText(label)
        elif status in ("succeeded", "failed", "cancelled", "interrupted"):
            err = str(t.get("error") or "").strip()
            self.sub.setText(f"{label} · {elapsed:.1f}s" + (f" · {err[:80]}" if err else ""))
        else:
            self.sub.setText(label)
        active = status in ("queued", "running", "waiting")
        done = status in ("succeeded", "failed", "cancelled", "interrupted")
        self.btn_cancel.setVisible(active)
        self.btn_retry.setVisible(status in ("failed", "cancelled", "interrupted"))
        self.btn_log.setVisible(status in ("failed", "succeeded", "interrupted"))
        self.btn_dismiss.setVisible(done)


class TaskNotice(QFrame):
    """地图顶部居中的消息通知（替代右上角任务中心）。

    · 有排队/执行任务时显示，显示数量和耗时；
    · 完成后短暂保留（失败保留更久），自动淡出；
    · 失败通知带重试/日志，执行中带取消；
    · 自身是地图的子控件，绝对定位在地图顶部中央，不占布局高度。
    """

    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    log_requested = Signal(str)

    _DONE_TTL = 8.0
    _FAILED_TTL = 30.0
    _MAX_ROWS = 3

    def __init__(self, snapshot: Callable[[], list[dict]] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._snapshot = snapshot or (lambda: [])
        self._rows: dict[str, TaskNoticeRow] = {}
        self._dismissed: set[str] = set()
        self.setObjectName("taskNotice")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Maximum)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("任务通知")
        title.setObjectName("taskNoticeTitle")
        head.addWidget(title)
        self.badge = QLabel("")
        self.badge.setObjectName("taskNoticeBadge")
        head.addWidget(self.badge)
        head.addStretch(1)
        self.btn_clear = QPushButton("清除已完成")
        self.btn_clear.setObjectName("taskNoticeClear")
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.setToolTip("移除已完成的通知（失败和进行中的保留）")
        self.btn_clear.clicked.connect(self._clear_finished)
        head.addWidget(self.btn_clear)
        outer.addLayout(head)

        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        outer.addWidget(self.rows_host)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(40, 35, 25, 45))
        self.setGraphicsEffect(shadow)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.hide()

        p = parent
        if p is not None:
            p.installEventFilter(self)

    # ---------------------------------------------------------------- 定位
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.parentWidget() and event.type() in (QEvent.Resize, QEvent.Show):
            QTimer.singleShot(0, self._reposition)
        return super().eventFilter(obj, event)

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        maxw = max(320, parent.width() - 72)
        self.setFixedWidth(min(620, maxw))
        self.adjustSize()
        x = max(10, (parent.width() - self.width()) // 2)
        self.move(x, 10)
        self.raise_()

    # ---------------------------------------------------------------- 刷新
    def _active_tasks(self, tasks: list[dict]) -> list[dict]:
        return [t for t in tasks if t.get("status") in ("queued", "running", "waiting")]

    def _visible_tasks(self) -> list[dict]:
        now = time.time()
        tasks = self._snapshot() or []
        active = sorted(self._active_tasks(tasks), key=lambda t: t.get("created_at") or 0)
        recent = []
        for t in tasks:
            status = str(t.get("status") or "")
            if status in ("queued", "running", "waiting"):
                continue
            if str(t.get("id") or "") in self._dismissed:
                continue
            finish = t.get("finished_at")
            if finish is None:
                continue
            ttl = self._FAILED_TTL if status in ("failed", "interrupted") else self._DONE_TTL
            if now - float(finish) <= ttl:
                recent.append(t)
        recent.sort(key=lambda t: t.get("finished_at") or 0, reverse=True)
        return (active + recent)[: self._MAX_ROWS]

    def refresh(self) -> None:
        tasks = self._snapshot() or []
        visible = self._visible_tasks()
        if not visible:
            self.hide()
            for row in self._rows.values():
                row.deleteLater()
            self._rows.clear()
            return

        seen = set()
        for t in visible:
            tid = str(t.get("id") or "")
            if not tid:
                continue
            seen.add(tid)
            row = self._rows.get(tid)
            if row is None:
                row = TaskNoticeRow(t, self.rows_host)
                row.cancel_requested.connect(self.cancel_requested)
                row.retry_requested.connect(self.retry_requested)
                row.log_requested.connect(self.log_requested)
                row.dismiss_requested.connect(self._dismiss)
                self.rows_layout.addWidget(row)
                self._rows[tid] = row
            else:
                row.task = t
                row._apply()
        for tid, row in list(self._rows.items()):
            if tid not in seen:
                row.setParent(None)
                row.deleteLater()
                self._rows.pop(tid, None)

        active = self._active_tasks(tasks)
        running = sum(1 for t in active if t.get("status") == "running")
        pending = sum(1 for t in active if t.get("status") in ("queued", "waiting"))
        if active:
            self.badge.setText(
                f"{running} 执行" + (f" · {pending} 排队" if pending else ""))
        else:
            self.badge.setText("最近完成")
        self.badge.show()
        self.btn_clear.setVisible(any(t.get("status") not in ("queued", "running", "waiting")
                                      for t in visible))
        self.show()
        self._reposition()

    def _dismiss(self, task_id: str) -> None:
        self._dismissed.add(task_id)
        self.refresh()

    def _clear_finished(self) -> None:
        for t in self._snapshot() or []:
            if t.get("status") not in ("queued", "running", "waiting"):
                self._dismissed.add(str(t.get("id") or ""))
        self.refresh()

    @property
    def should_show(self) -> bool:
        """兼容旧任务中心断言：只表示「还有活动任务」。"""
        return bool(self._active_tasks(self._snapshot() or []))

