"""左侧「工作区」：工作区 → 会话 → 会话图层 的导航。

概念和常见 coding agent 的工作区一致：工作区是项目，里面是历史会话；
GIS 多出来一层「会话图层」——打开会话时，图层会同步恢复到地图和图层列表。

图层数据**不按会话拷贝**：矢量图层落在共享的 `data/cache/`，
会话状态只存图层 id、缓存路径/栅格源路径，恢复时引用同一份缓存。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .icons import folder_icon, layer_icon, plus_icon, search_icon, session_icon


def _relative_time(ts: float | None) -> str:
    if not ts:
        return ""
    delta = max(0.0, time.time() - float(ts))
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时"
    if delta < 86400 * 7:
        return f"{int(delta // 86400)} 天"
    return time.strftime("%m-%d", time.localtime(float(ts)))


class WorkspacePanel(QFrame):
    """工作区会话列表。

    sessions_provider 返回：
        [{"id","title","updated_at","layer_count","layer_names"}, ...]
    顺序按 updated_at 倒序。
    """

    open_session = Signal(str)
    new_session = Signal()

    def __init__(self, sessions_provider: Callable[[], list[dict]],
                 workspace_name: str = "gis-agent",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("workspace")
        self._sessions = sessions_provider
        self._workspace_name = workspace_name or "gis-agent"
        self._current_session = ""
        self._expanded_groups: set[str] = {"未分组"}

        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(12, 7, 10, 4)
        title = QLabel("工作区")
        title.setObjectName("panelTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_new = QPushButton()
        self.btn_new.setObjectName("icon")
        self.btn_new.setFixedSize(22, 22)
        self.btn_new.setIcon(plus_icon())
        self.btn_new.setIconSize(QSize(14, 14))
        self.btn_new.setToolTip("新建会话")
        self.btn_new.setCursor(Qt.PointingHandCursor)
        self.btn_new.clicked.connect(lambda: self.new_session.emit())
        head.addWidget(self.btn_new)
        vl.addLayout(head)

        self.search = QLineEdit()
        self.search.setObjectName("workspaceSearch")
        self.search.setPlaceholderText("搜索会话…")
        self.search.addAction(search_icon(), QLineEdit.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        vl.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setObjectName("workspaceTree")
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setIndentation(13)
        self.tree.setColumnCount(2)
        self.tree.setColumnWidth(0, 176)
        self.tree.setColumnWidth(1, 58)
        self.tree.setIconSize(QSize(16, 16))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemClicked.connect(self._on_click)
        self.tree.itemExpanded.connect(self._on_expanded)
        vl.addWidget(self.tree, 1)

        self.empty = QLabel("还没有会话。\n点右上角「＋」新建一个。")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        vl.addWidget(self.empty)

        self.refresh()

    # ---------------------------------------------------------------- 状态
    def set_current(self, session_id: str) -> None:
        self._current_session = str(session_id or "")
        self._select_current()

    def _select_current(self) -> None:
        found = None

        def walk(item: QTreeWidgetItem) -> None:
            nonlocal found
            sid = str(item.data(0, Qt.UserRole) or "")
            if sid and not sid.startswith("__"):
                item.setIcon(0, session_icon(active=(sid == self._current_session)))
                if sid == self._current_session:
                    found = item
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        if found is not None:
            self.tree.setCurrentItem(found)

    def refresh(self) -> None:
        query = self.search.text().strip().lower()
        try:
            sessions = list(self._sessions() or [])
        except Exception:
            sessions = []
        if query:
            sessions = [s for s in sessions
                        if query in str(s.get("title") or "").lower()
                        or any(query in str(n).lower()
                               for n in (s.get("layer_names") or []))]

        self.tree.clear()

        root = QTreeWidgetItem([self._workspace_name, ""])
        root.setIcon(0, folder_icon(active=True))
        root.setData(0, Qt.UserRole, "__workspace__")
        root.setToolTip(0, "当前工作区")
        font = root.font(0)
        font.setBold(True)
        root.setFont(0, font)
        self.tree.addTopLevelItem(root)

        group = QTreeWidgetItem(["未分组", ""])
        group.setIcon(0, folder_icon())
        group.setData(0, Qt.UserRole, "__group__")
        group.setExpanded("未分组" in self._expanded_groups)
        root.addChild(group)
        root.setExpanded(True)

        for s in sessions:
            sid = str(s.get("id") or "")
            title = str(s.get("title") or "新会话")
            count = int(s.get("layer_count") or 0)
            item = QTreeWidgetItem([title, _relative_time(s.get("updated_at"))])
            item.setData(0, Qt.UserRole, sid)
            item.setIcon(0, session_icon(active=(sid == self._current_session)))
            names = [str(x) for x in (s.get("layer_names") or [])]
            tip = f"{title}\n图层 {count} 个" + ("：" + "、".join(names[:8]) if names else "")
            item.setToolTip(0, tip)
            item.setToolTip(1, "最近更新")
            group.addChild(item)
            # 会话图层：不复制数据，只在这里展示引用
            for name in names[:20]:
                child = QTreeWidgetItem([name, ""])
                child.setData(0, Qt.UserRole, "")
                child.setIcon(0, layer_icon(14))
                child.setForeground(0, Qt.gray)
                item.addChild(child)
            if names:
                item.setExpanded(False)

        self.empty.setVisible(not sessions)
        self._select_current()

    # ---------------------------------------------------------------- 交互
    def _session_id(self, item: QTreeWidgetItem) -> str:
        sid = str(item.data(0, Qt.UserRole) or "")
        return sid if sid and not sid.startswith("__") else ""

    def _on_click(self, item: QTreeWidgetItem, _col: int) -> None:
        sid = self._session_id(item)
        if sid:
            self._current_session = sid
            self.open_session.emit(sid)
        elif str(item.data(0, Qt.UserRole) or "") == "__group__":
            item.setExpanded(not item.isExpanded())
        elif str(item.data(0, Qt.UserRole) or "") == "__workspace__":
            item.setExpanded(not item.isExpanded())

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        if str(item.data(0, Qt.UserRole) or "") == "__group__":
            if item.isExpanded():
                self._expanded_groups.add("未分组")
            else:
                self._expanded_groups.discard("未分组")

    def _menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        sid = self._session_id(item)
        if not sid:
            return
        from PySide6.QtWidgets import QApplication, QMenu
        menu = QMenu(self)
        menu.addAction("打开会话", lambda: self.open_session.emit(sid))
        menu.addAction("复制会话标题",
                       lambda: QApplication.clipboard().setText(item.text(0)))
        menu.exec(self.tree.viewport().mapToGlobal(pos))
