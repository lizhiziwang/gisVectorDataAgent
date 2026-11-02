"""逐步确认：后台 Agent 等待、主线程弹窗收集用户补充要求。

后台线程不能直接弹 Qt 控件；这里用一个 threading.Event 把「确认结果」送回
正在执行 run_plan 的线程。弹窗本身在主线程，用户输入不会被模态循环吞掉。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QVBoxLayout)


class StepConfirmGate:
    """一次步骤确认的线程同步点。"""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.action = "cancel"
        self.instruction = ""

    def resolve(self, action: str, instruction: str = "") -> None:
        self.action = str(action or "cancel")
        self.instruction = str(instruction or "").strip()
        self.event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.event.wait(timeout)


class StepConfirmDialog(QDialog):
    """展示当前步骤，允许用户补充/修改这一步的要求。"""

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.action = "cancel"
        idx = int(info.get("index") or 0)
        total = int(info.get("total") or 0)
        label = str(info.get("label") or info.get("tool") or "这一步")
        detail = str(info.get("detail") or "")
        self.setWindowTitle("确认执行步骤")
        self.setMinimumWidth(460)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(16, 14, 16, 12)
        vl.setSpacing(9)

        head = QLabel(f"第 {idx + 1}/{total} 步 · {label}" if total
                      else f"第 {idx + 1} 步 · {label}")
        head.setObjectName("stepConfirmHead")
        vl.addWidget(head)

        if detail:
            d = QLabel(detail)
            d.setObjectName("stepConfirmDetail")
            d.setWordWrap(True)
            vl.addWidget(d)

        args = info.get("args") or {}
        if args:
            import json
            ar = QPlainTextEdit()
            ar.setObjectName("confirmArgs")
            ar.setReadOnly(True)
            ar.setFixedHeight(58)
            ar.setPlainText(json.dumps(args, ensure_ascii=False, indent=2))
            vl.addWidget(ar)

        tip = QLabel("这一步要执行吗？可以在下面补充/修改要求（留空按原计划执行）。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        vl.addWidget(tip)
        self.input = QPlainTextEdit()
        self.input.setObjectName("confirmInput")
        self.input.setFixedHeight(68)
        self.input.setPlaceholderText(
            "例如：缓冲区改成 800 米；只处理当前选区；按县代码分组统计……")
        vl.addWidget(self.input)

        hint = QLabel("填写修改要求后，模型会先重写这一步；改不出来会停下并提示，"
                      "不会按旧参数硬跑。")
        hint.setObjectName("tiny")
        hint.setWordWrap(True)
        vl.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        b_cancel = QPushButton("取消任务")
        b_skip = QPushButton("跳过这一步")
        b_run = QPushButton("执行这一步")
        b_run.setObjectName("primary")
        b_cancel.clicked.connect(lambda: self._finish("cancel"))
        b_skip.clicked.connect(lambda: self._finish("skip"))
        b_run.clicked.connect(lambda: self._finish("run"))
        row.addWidget(b_cancel)
        row.addStretch(1)
        row.addWidget(b_skip)
        row.addWidget(b_run)
        vl.addLayout(row)
        self.input.setFocus()

    def _finish(self, action: str) -> None:
        self.action = action
        self.accept()

    def instruction(self) -> str:
        return self.input.toPlainText().strip()
