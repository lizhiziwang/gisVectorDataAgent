"""记忆管理面板（设计文档 §6.4）。

按类型分组展示记忆，显示来源、置信度、最后使用时间；
支持编辑、置顶、删除、清空项目记忆，以及 JSON 导入导出。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

TYPE_LABEL = {
    "preference": "偏好",
    "project_fact": "项目事实",
    "workflow": "工作流",
    "correction": "纠正",
    "summary": "摘要",
}

TYPE_ORDER = ["preference", "correction", "project_fact", "workflow", "summary"]


class MemoryPanel(QDialog):
    """记忆管理面板。"""

    def __init__(self, memory: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mem = memory
        self.setWindowTitle("记忆管理")
        self.resize(720, 560)

        vl = QVBoxLayout(self)
        vl.setContentsMargins(14, 12, 14, 12)
        vl.setSpacing(8)

        # 顶部说明
        tip = QLabel("这些记忆会自动注入到规划提示里，帮助 Agent 记住你的偏好和项目习惯。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        vl.addWidget(tip)

        # 树
        self.tree = QTreeWidget()
        self.tree.setObjectName("memoryTree")
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["记忆", "类型", "置信度", "重要性", "最后使用"])
        self.tree.setRootIsDecorated(True)
        self.tree.header().setStretchLastSection(False)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        vl.addWidget(self.tree, 1)

        # 操作按钮
        bh = QHBoxLayout()
        bh.setSpacing(6)
        self.btn_pin = QPushButton("置顶 / 取消置顶")
        self.btn_pin.clicked.connect(self._toggle_pin)
        bh.addWidget(self.btn_pin)

        self.btn_edit = QPushButton("编辑…")
        self.btn_edit.clicked.connect(self._edit)
        bh.addWidget(self.btn_edit)

        self.btn_forget = QPushButton("忘记这条")
        self.btn_forget.clicked.connect(self._forget)
        bh.addWidget(self.btn_forget)

        self.btn_clear = QPushButton("清空项目记忆")
        self.btn_clear.clicked.connect(self._clear)
        bh.addWidget(self.btn_clear)
        bh.addStretch(1)

        self.btn_export = QPushButton("导出 JSON")
        self.btn_export.clicked.connect(self._export)
        bh.addWidget(self.btn_export)

        self.btn_import = QPushButton("导入 JSON")
        self.btn_import.clicked.connect(self._import)
        bh.addWidget(self.btn_import)

        vl.addLayout(bh)

        # 底部：自动提取开关 + 关闭
        foot = QHBoxLayout()
        self.chk_auto = QCheckBox("允许自动提取记忆（偏好 / 事实 / 工作流 / 纠正）")
        self.chk_auto.setChecked(True)
        foot.addWidget(self.chk_auto)
        foot.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        foot.addWidget(btn_close)
        vl.addLayout(foot)

        self.reload()

    # ------------------------------------------------------------------ 展示
    def reload(self) -> None:
        self.tree.clear()
        grouped = self.mem.grouped() if self.mem is not None else {}
        for type_ in TYPE_ORDER:
            items = grouped.get(type_) or []
            if not items:
                continue
            top = QTreeWidgetItem(self.tree, [f"{TYPE_LABEL.get(type_, type_)}（{len(items)}）"])
            top.setFirstColumnSpanned(False)
            top.setFlags(Qt.ItemIsEnabled)
            top.setExpanded(True)
            for m in items:
                used = (f"{time.strftime('%m-%d %H:%M', time.localtime(m.last_used_at))}"
                        if m.last_used_at else "未使用")
                child = QTreeWidgetItem(top, [
                    self._render_value(m),
                    TYPE_LABEL.get(m.type, m.type),
                    f"{m.confidence:.2f}",
                    f"{m.importance:.2f}" + ("  ★" if m.pinned else ""),
                    used,
                ])
                child.setData(0, Qt.UserRole, m.id)
                child.setData(0, Qt.UserRole + 1, m.key)
                child.setToolTip(0, f"key: {m.key}\n"
                                    f"来源 run: {m.source_run_id or '—'}\n"
                                    f"来源消息: {m.source_message_id or '—'}")
        for i, w in enumerate((220, 80, 70, 80, 110)):
            self.tree.setColumnWidth(i, w)

    @staticmethod
    def _render_value(m: Any) -> str:
        v = m.value
        if isinstance(v, str):
            return v[:120]
        try:
            return json.dumps(v, ensure_ascii=False)[:120]
        except Exception:
            return str(v)[:120]

    def _selected(self) -> QTreeWidgetItem | None:
        items = self.tree.selectedItems()
        for it in items:
            if it.data(0, Qt.UserRole):
                return it
        return None

    # ------------------------------------------------------------------ 操作
    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        """双击记忆 → 直接编辑。"""
        if item.data(0, Qt.UserRole):
            self._edit()

    def _toggle_pin(self) -> None:
        it = self._selected()
        if it is None:
            QMessageBox.information(self, "记忆管理", "先选一条记忆。")
            return
        mid = it.data(0, Qt.UserRole)
        try:
            cur = [m for m in (self.mem.grouped(limit=1000).get("preference") or [])]
            allm = []
            for v in (self.mem.grouped(limit=1000) or {}).values():
                allm.extend(v)
            hit = next((m for m in allm if m.id == mid), None)
            pinned = not (hit.pinned if hit else False)
            self.mem.pin(mid, pinned)
            self.reload()
        except Exception as e:
            QMessageBox.warning(self, "记忆管理", f"操作失败：{e}")

    def _edit(self) -> None:
        it = self._selected()
        if it is None:
            QMessageBox.information(self, "记忆管理", "先选一条记忆。")
            return
        mid = it.data(0, Qt.UserRole)
        allm = []
        for v in (self.mem.grouped(limit=1000) or {}).values():
            allm.extend(v)
        hit = next((m for m in allm if m.id == mid), None)
        if hit is None:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("编辑记忆")
        dlg.resize(520, 320)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"key：{hit.key}"))
        edit = QPlainTextEdit()
        edit.setPlainText(self._render_value(hit))
        v.addWidget(edit, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(dlg.reject)
        row.addWidget(cancel)
        ok = QPushButton("保存")
        ok.clicked.connect(dlg.accept)
        row.addWidget(ok)
        v.addLayout(row)
        if dlg.exec() != QDialog.Accepted:
            return
        text = edit.toPlainText().strip()
        try:
            from app.models import Memory
            hit.value = text
            self.mem.store.upsert_memory(hit)
            self.reload()
        except Exception as e:
            QMessageBox.warning(self, "记忆管理", f"保存失败：{e}")

    def _forget(self) -> None:
        it = self._selected()
        if it is None:
            QMessageBox.information(self, "记忆管理", "先选一条记忆。")
            return
        mid = it.data(0, Qt.UserRole)
        if self.mem.forget(mid):
            self.reload()
        else:
            QMessageBox.warning(self, "记忆管理", "删除失败。")

    def _clear(self) -> None:
        if not self._confirm_clear():
            return
        self.mem.clear()
        self.reload()

    def _confirm_clear(self) -> bool:
        """清空前确认。拆成独立方法，方便测试替换。"""
        box = QMessageBox(self)
        box.setWindowTitle("清空项目记忆")
        box.setIcon(QMessageBox.Warning)
        box.setText("要清空这个项目的全部记忆吗？")
        box.setInformativeText("图层和会话记录不受影响；以后 Agent 还是会重新学到这些偏好。")
        yes = box.addButton("清空", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(yes)
        box.exec()
        return box.clickedButton() is yes

    # ------------------------------------------------------------------ 导入导出
    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出记忆", str(Path.home() / "gis_agent_memories.json"), "*.json")
        if not path:
            return
        try:
            allm = []
            for v in (self.mem.grouped(limit=5000) or {}).values():
                allm.extend(v)
            data = [{"type": m.type, "key": m.key, "value": m.value,
                     "confidence": m.confidence, "importance": m.importance,
                     "pinned": m.pinned} for m in allm]
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            self.set_status(f"已导出 {len(data)} 条记忆")
        except Exception as e:
            QMessageBox.warning(self, "记忆管理", f"导出失败：{e}")

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入记忆", "", "*.json")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("文件格式不对：应该是一个数组")
            from app.models import Memory
            n = 0
            for item in data:
                if not isinstance(item, dict) or "key" not in item:
                    continue
                self.mem.store.upsert_memory(Memory(
                    id="", project_id=self.mem.project_id,
                    type=str(item.get("type") or "preference"),
                    key=str(item["key"]), value=item.get("value"),
                    confidence=float(item.get("confidence") or 0.8),
                    importance=float(item.get("importance") or 0.5),
                    pinned=bool(item.get("pinned"))))
                n += 1
            self.reload()
            self.set_status(f"已导入 {n} 条记忆")
        except Exception as e:
            QMessageBox.warning(self, "记忆管理", f"导入失败：{e}")

    def set_status(self, text: str) -> None:
        self.setWindowTitle(f"记忆管理 · {text}")
