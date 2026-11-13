"""Agent 设置窗口：一个入口管全部配置。

页签：模型（接口地址 / Key / 格式 / 测试连接）· 记忆（偏好 / 事实 / 工作流）·
技能 · 工具库 · 通用（逐步确认、技能开关）。

以前顶部标题栏还有「模型设置」「记忆」「大模型规划」三个独立按钮，现在都收进
这个窗口 —— 标题栏只留一个「Agent 设置」，界面更安静，配置也只有一处可找。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from app import custom_ops
from app import tool_prefs
from app.agent import TOOL_LABEL, TOOL_SPECS
from app.config import is_ready, load_cfg, save_cfg
from app.skills import (SKILLS_DIR, Skill, delete_skill, ensure_dir, list_skills,
                        save_skill, set_skill_enabled)
from app.tool_catalog import is_custom, tool_tree
from app.tool_prefs import is_enabled

from .dialogs import ModelSettingsPage, OperatorDialog
from .memory_panel import MemoryPage


class SkillEditDialog(QDialog):
    """新增/编辑一张技能卡。"""

    def __init__(self, skill: Skill | None = None, parent=None):
        super().__init__(parent)
        self._skill = skill
        self.setWindowTitle("编辑技能" if skill is not None else "添加技能")
        self.resize(520, 420)
        vl = QVBoxLayout(self)
        vl.setContentsMargins(16, 14, 16, 12)
        vl.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(8)
        self.name = QLineEdit((skill.name if skill else ""))
        self.name.setPlaceholderText("例如：两期遥感影像对比")
        self.desc = QLineEdit((skill.description if skill else ""))
        self.desc.setPlaceholderText("一句话说明什么时候使用这个技能")
        form.addRow("名称", self.name)
        form.addRow("说明", self.desc)
        vl.addLayout(form)

        tip = QLabel("正文会作为领域知识交给规划器。建议写清楚：适用场景、"
                     "参数口径、常见坑和推荐算子顺序。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        vl.addWidget(tip)
        self.body = QPlainTextEdit()
        self.body.setObjectName("skillBody")
        self.body.setPlaceholderText("写下这个技能的具体做法……")
        if skill is not None:
            self.body.setPlainText(skill.body)
        vl.addWidget(self.body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存技能")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        vl.addWidget(buttons)

    def _save(self) -> None:
        name = self.name.text().strip()
        body = self.body.toPlainText().strip()
        if not name:
            QMessageBox.warning(self, "技能", "请填写技能名称。")
            return
        if not body:
            QMessageBox.warning(self, "技能", "技能正文不能为空。")
            return
        try:
            save_skill(name, self.desc.text().strip(), body,
                       self._skill.path if self._skill is not None else None)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.accept()


class SkillRow(QWidget):
    def __init__(self, skill: Skill, checked: bool, on_toggle, parent=None):
        super().__init__(parent)
        self.skill = skill
        vl = QVBoxLayout(self)
        vl.setContentsMargins(6, 5, 6, 5)
        vl.setSpacing(2)
        self.check = QCheckBox(skill.name)
        self.check.setChecked(checked)
        self.check.toggled.connect(lambda v: on_toggle(skill, bool(v)))
        vl.addWidget(self.check)
        desc = QLabel(skill.description or "（没有说明）")
        desc.setObjectName("muted")
        desc.setWordWrap(True)
        vl.addWidget(desc)
        fn = QLabel(skill.filename)
        fn.setObjectName("tiny")
        vl.addWidget(fn)


class AgentSettingsDialog(QDialog):
    """Agent 自己的设置：模型 / 记忆 / 技能 / 工具 / 通用，一个窗口全管。"""

    #: 工具被新增/删除/停用后通知主窗口：同步运行时状态并刷新左栏工具库。
    tools_changed = Signal()

    def __init__(self, parent=None, memory=None, tab: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Agent 设置")
        self.resize(760, 700)
        self._pending: dict[str, bool] = {}
        #: 每个工具当前是否启用（值是「保存后」的状态，不是磁盘上的旧状态）
        self._tool_enabled: dict[str, bool] = {}
        #: 构建树时 itemChanged 会连环触发，用它挡住
        self._building_tools = False
        self._painting_tool = False
        #: 点「新建工具」并且设置保存成功后置为 True，由主窗口接去走生成流程
        self.new_tool_requested = False
        self._memory = memory
        cfg = load_cfg()
        vl = QVBoxLayout(self)
        vl.setContentsMargins(14, 12, 14, 12)
        vl.setSpacing(8)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("agentSettingsTabs")
        self.model_page = None
        self.memory_page = None
        self._tab_index: dict[str, int] = {}
        for key, title, widget in (
                ("模型", "模型", self._build_model_tab(cfg)),
                ("记忆", "记忆", self._build_memory_tab()),
                ("技能", "技能", self._build_skills_tab()),
                ("工具", "工具", self._build_tools_tab()),
                ("通用", "通用", self._build_general_tab(cfg))):
            self._tab_index[key] = self.tabs.addTab(widget, title)
        if tab in self._tab_index:
            self.tabs.setCurrentIndex(self._tab_index[tab])
        vl.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        vl.addWidget(buttons)

    def closeEvent(self, e):  # noqa: N802
        """关窗时收掉模型页的测试线程（不然退出阶段可能崩）。"""
        if self.model_page is not None:
            self.model_page.stop_test()
        super().closeEvent(e)

    @staticmethod
    def _planner_status_text(cfg: dict) -> str:
        if is_ready(cfg):
            return f"● 大模型规划 · {cfg.get('model') or '已配置'}"
        return "● 规则引擎 · 离线可用（接上模型后可以用更自由的说法）"

    # ------------------------------------------------------------- 模型
    def _build_model_tab(self, cfg: dict) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(2, 8, 2, 4)
        vl.setSpacing(6)
        self.model_status = QLabel(self._planner_status_text(cfg))
        self.model_status.setObjectName("plannerOn" if is_ready(cfg) else "muted")
        self.model_status.setToolTip("「● 大模型规划」表示下面的接口已启用且配置完整；"
                                     "否则走离线的规则引擎")
        vl.addWidget(self.model_status)
        scroll = QScrollArea()
        scroll.setObjectName("modelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.model_page = ModelSettingsPage(cfg)
        scroll.setWidget(self.model_page)
        vl.addWidget(scroll, 1)
        return w

    # ------------------------------------------------------------- 记忆
    def _build_memory_tab(self) -> QWidget:
        if self._memory is None:
            w = QWidget()
            vl = QVBoxLayout(w)
            vl.setContentsMargins(8, 12, 8, 8)
            lab = QLabel("当前没有记忆库。\n\n"
                         "记忆跟着本地会话库 data/agent.db 走；以正常桌面方式启动、"
                         "有过对话之后，这里会列出 Agent 记住的偏好、项目事实和工作流。")
            lab.setObjectName("muted")
            lab.setWordWrap(True)
            vl.addWidget(lab)
            vl.addStretch(1)
            return w
        self.memory_page = MemoryPage(self._memory)
        return self.memory_page

    # ------------------------------------------------------------- 技能
    def _build_skills_tab(self) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(6, 8, 6, 6)
        vl.setSpacing(7)
        head = QHBoxLayout()
        title = QLabel("技能会作为领域知识参与下一次规划")
        title.setObjectName("muted")
        head.addWidget(title)
        head.addStretch(1)
        b_open = QPushButton("打开 skills 目录")
        b_open.setObjectName("ghost")
        b_open.clicked.connect(self._open_dir)
        head.addWidget(b_open)
        b_add = QPushButton("+ 添加技能")
        b_add.setObjectName("primary")
        b_add.clicked.connect(self._add_skill)
        head.addWidget(b_add)
        vl.addLayout(head)

        self.skill_list = QListWidget()
        self.skill_list.setObjectName("skillList")
        self.skill_list.setSpacing(1)
        self.skill_list.itemDoubleClicked.connect(lambda _i: self._edit_skill())
        vl.addWidget(self.skill_list, 1)

        row = QHBoxLayout()
        self.skill_count = QLabel("")
        self.skill_count.setObjectName("muted")
        row.addWidget(self.skill_count)
        row.addStretch(1)
        b_refresh = QPushButton("刷新")
        b_refresh.setObjectName("ghost")
        b_refresh.clicked.connect(self._refresh_skills)
        b_edit = QPushButton("编辑")
        b_edit.setObjectName("ghost")
        b_edit.clicked.connect(self._edit_skill)
        b_del = QPushButton("删除")
        b_del.setObjectName("ghost")
        b_del.clicked.connect(self._delete_skill)
        row.addWidget(b_refresh)
        row.addWidget(b_edit)
        row.addWidget(b_del)
        vl.addLayout(row)
        self._refresh_skills()
        return w

    def _enabled_state(self, s: Skill) -> bool:
        if s.name in self._pending:
            return self._pending[s.name]
        return s.enabled

    def _on_toggle(self, skill: Skill, checked: bool) -> None:
        self._pending[skill.name] = bool(checked)

    def _refresh_skills(self) -> None:
        self.skill_list.clear()
        skills = list_skills()
        for s in skills:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, s)
            item.setSizeHint(QSize(460, 66))
            row = SkillRow(s, self._enabled_state(s), self._on_toggle)
            self.skill_list.addItem(item)
            self.skill_list.setItemWidget(item, row)
        self.skill_count.setText(f"共 {len(skills)} 个技能 · "
                                 f"目录 {SKILLS_DIR}")
        if not skills:
            item = QListWidgetItem("还没有技能，点右上角「+ 添加技能」创建一张。")
            item.setFlags(Qt.NoItemFlags)
            self.skill_list.addItem(item)

    def _current_skill(self) -> Skill | None:
        item = self.skill_list.currentItem()
        if item is None:
            return None
        s = item.data(Qt.UserRole)
        return s if isinstance(s, Skill) else None

    def _add_skill(self) -> None:
        dlg = SkillEditDialog(None, self)
        if dlg.exec() == QDialog.Accepted:
            self._pending.pop(dlg.name.text().strip(), None)
            self._refresh_skills()

    def _edit_skill(self) -> None:
        skill = self._current_skill()
        if skill is None:
            QMessageBox.information(self, "技能", "先选中一个技能。")
            return
        if SkillEditDialog(skill, self).exec() == QDialog.Accepted:
            self._refresh_skills()

    def _delete_skill(self) -> None:
        skill = self._current_skill()
        if skill is None:
            QMessageBox.information(self, "技能", "先选中一个技能。")
            return
        if QMessageBox.question(
                self, "删除技能",
                f"删除「{skill.name}」？这会直接删掉 skills/{skill.filename}。") \
                != QMessageBox.Yes:
            return
        try:
            delete_skill(skill.path)
            set_skill_enabled(skill.name, True)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "删除失败", str(e))
            return
        self._pending.pop(skill.name, None)
        self._refresh_skills()

    def _open_dir(self) -> None:
        p = ensure_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    # ------------------------------------------------------------- 工具
    def _build_tools_tab(self) -> QWidget:
        """工具库管理：看得全、搜得到、能停用，自定义工具还能改/删。

        停用只影响「工具库展示 / 模型候选 / 执行前检查」三处，不删实现；
        内置工具不能删，但可以停用 —— 这比把它们从代码里注释掉安全得多。
        """
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(6, 8, 6, 6)
        vl.setSpacing(7)

        head = QHBoxLayout()
        title = QLabel("取消勾选即停用：不再出现在工具库，也不会交给大模型")
        title.setObjectName("muted")
        head.addWidget(title)
        head.addStretch(1)
        b_open = QPushButton("打开工具目录")
        b_open.setObjectName("ghost")
        b_open.setToolTip(f"自定义工具的源码：{custom_ops.OPS_DIR}")
        b_open.clicked.connect(self._open_tools_dir)
        head.addWidget(b_open)
        b_new = QPushButton("+ 新建工具")
        b_new.setObjectName("primary")
        b_new.setToolTip("保存当前设置，再用一段文字描述让大模型生成新工具")
        b_new.clicked.connect(self._request_new_tool)
        head.addWidget(b_new)
        vl.addLayout(head)

        self.tool_search = QLineEdit()
        self.tool_search.setPlaceholderText("搜索工具名、说明或算子名…")
        self.tool_search.setClearButtonEnabled(True)
        self.tool_search.textChanged.connect(self._filter_tools)
        vl.addWidget(self.tool_search)

        self.tool_tree = QTreeWidget()
        self.tool_tree.setObjectName("toolManagerTree")
        self.tool_tree.setColumnCount(2)
        self.tool_tree.setHeaderLabels(["工具", "来源"])
        self.tool_tree.setRootIsDecorated(True)
        self.tool_tree.setUniformRowHeights(True)
        self.tool_tree.setAllColumnsShowFocus(True)
        hh = self.tool_tree.header()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setStretchLastSection(False)
        self.tool_tree.itemChanged.connect(self._on_tool_changed)
        self.tool_tree.itemSelectionChanged.connect(self._sync_tool_buttons)
        self.tool_tree.itemDoubleClicked.connect(self._on_tool_double_clicked)
        vl.addWidget(self.tool_tree, 1)

        row = QHBoxLayout()
        self.tool_count = QLabel("")
        self.tool_count.setObjectName("muted")
        row.addWidget(self.tool_count)
        row.addStretch(1)
        b_refresh = QPushButton("刷新")
        b_refresh.setObjectName("ghost")
        b_refresh.clicked.connect(self._refresh_tools)
        self.b_edit = QPushButton("编辑代码…")
        self.b_edit.setObjectName("ghost")
        self.b_edit.clicked.connect(self._edit_tool)
        self.b_del = QPushButton("删除")
        self.b_del.setObjectName("ghost")
        self.b_del.clicked.connect(self._delete_tool)
        row.addWidget(b_refresh)
        row.addWidget(self.b_edit)
        row.addWidget(self.b_del)
        vl.addLayout(row)
        self._refresh_tools()
        return w

    @staticmethod
    def _bold(item: QTreeWidgetItem) -> None:
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)

    def _refresh_tools(self) -> None:
        """重建工具树：矢量/栅格 → 功能分组 → 工具（带启用复选框）。"""
        tree = tool_tree(include_disabled=True)
        descs = {s.get("name"): str(s.get("desc") or "") for s in TOOL_SPECS}
        self._building_tools = True
        try:
            self.tool_tree.clear()
            seen: set[str] = set()
            for kind, sections in tree:
                kind_count = sum(len(names) for _, names in sections)
                kind_item = QTreeWidgetItem(
                    self.tool_tree, [f"{kind}（{kind_count}）", ""])
                kind_item.setFlags(Qt.ItemIsEnabled)
                self._bold(kind_item)
                for title, names in sections:
                    group_item = QTreeWidgetItem(
                        kind_item, [f"{title}（{len(names)}）", ""])
                    group_item.setFlags(Qt.ItemIsEnabled)
                    self._bold(group_item)
                    for name in names:
                        seen.add(name)
                        custom = is_custom(name)
                        desc = descs.get(name) or ""
                        item = QTreeWidgetItem(group_item, [
                            TOOL_LABEL.get(name, name),
                            "自定义" if custom else "内置"])
                        item.setData(0, Qt.UserRole, name)
                        # 搜索时说明文字也参与匹配：用户更记得「算面积的那个」
                        item.setData(0, Qt.UserRole + 1, desc.lower())
                        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                        on = self._tool_enabled.get(name, is_enabled(name))
                        self._tool_enabled[name] = on
                        item.setCheckState(0, Qt.Checked if on else Qt.Unchecked)
                        item.setForeground(1, QColor("#a9502f" if custom else "#9a968c"))
                        item.setToolTip(0, self._tool_tip(name, desc, custom))
                        self._paint_tool(item, on)
            # 已被删除的工具不能留在待保存状态里，否则会被写回 tools_disabled
            for gone in [n for n in self._tool_enabled if n not in seen]:
                self._tool_enabled.pop(gone, None)
        finally:
            self._building_tools = False
        self.tool_tree.expandAll()
        self._filter_tools(self.tool_search.text())
        self._update_tool_count()
        self._sync_tool_buttons()

    @staticmethod
    def _tool_tip(name: str, desc: str, custom: bool) -> str:
        from app.tool_registry import REGISTRY
        spec = REGISTRY.get(name)
        params = "、".join(spec.params) if spec is not None else ""
        bits = [desc or "（没有说明）", f"算子名：{name}"]
        if params:
            bits.append(f"参数：{params}")
        bits.append("来源：自定义工具（可以编辑代码 / 删除）" if custom
                    else "来源：内置工具（可以停用，不能删除）")
        return "\n".join(bits)

    def _paint_tool(self, item: QTreeWidgetItem, on: bool) -> None:
        """停用的工具用灰色 + 删除线标出来，只看复选框不够显眼。"""
        self._painting_tool = True
        try:
            f = item.font(0)
            f.setStrikeOut(not on)
            item.setFont(0, f)
            item.setForeground(0, QColor("#1f1e1d" if on else "#9a968c"))
        finally:
            self._painting_tool = False

    def _on_tool_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._building_tools or self._painting_tool or column != 0:
            return
        name = str(item.data(0, Qt.UserRole) or "")
        if not name:
            return          # 分组/分类节点没有算子名，不参与
        on = item.checkState(0) == Qt.Checked
        self._tool_enabled[name] = on
        self._paint_tool(item, on)
        self._update_tool_count()

    def _filter_tools(self, text: str) -> None:
        """按关键字收窄：匹配工具名 / 算子名 / 说明，也能按分组名筛。

        分组命中就整组留下 —— 用户输入「整理」多半是想看这一组，
        而不是找名字里带「整理」的某个工具。
        """
        q = (text or "").strip().lower()
        for i in range(self.tool_tree.topLevelItemCount()):
            kind_item = self.tool_tree.topLevelItem(i)
            kind_hit = bool(q) and q in kind_item.text(0).lower()
            any_group = False
            for j in range(kind_item.childCount()):
                group = kind_item.child(j)
                group_hit = kind_hit or (bool(q) and q in group.text(0).lower())
                any_tool = False
                for k in range(group.childCount()):
                    item = group.child(k)
                    name = str(item.data(0, Qt.UserRole) or "")
                    desc = str(item.data(0, Qt.UserRole + 1) or "")
                    hit = (not q) or group_hit or (q in item.text(0).lower()) \
                        or (q in name) or (q in desc)
                    item.setHidden(not hit)
                    any_tool = any_tool or hit
                group.setHidden(not any_tool)
                any_group = any_group or any_tool
            kind_item.setHidden(not any_group)

    def _update_tool_count(self) -> None:
        total = len(self._tool_enabled)
        on = sum(1 for v in self._tool_enabled.values() if v)
        custom = sum(1 for n in self._tool_enabled if is_custom(n))
        bits = [f"共 {total} 个工具", f"启用 {on}"]
        if custom:
            bits.append(f"自定义 {custom}")
        if on < total:
            bits.append(f"停用 {total - on}")
        self.tool_count.setText(" · ".join(bits))

    def _selected_tool(self) -> str:
        item = self.tool_tree.currentItem()
        if item is None:
            return ""
        return str(item.data(0, Qt.UserRole) or "")

    def _select_tool(self, name: str) -> None:
        for i in range(self.tool_tree.topLevelItemCount()):
            kind_item = self.tool_tree.topLevelItem(i)
            for j in range(kind_item.childCount()):
                group = kind_item.child(j)
                for k in range(group.childCount()):
                    item = group.child(k)
                    if str(item.data(0, Qt.UserRole) or "") == name:
                        self.tool_tree.setCurrentItem(item)
                        return

    def _sync_tool_buttons(self) -> None:
        custom = bool(self._selected_tool()) and is_custom(self._selected_tool())
        self.b_edit.setEnabled(custom)
        self.b_del.setEnabled(custom)
        hint = "" if custom else "选中一个自定义工具后可编辑 / 删除；内置工具只能停用"
        self.b_edit.setToolTip(hint or "编辑这个自定义工具的代码、名称和分组")
        self.b_del.setToolTip(hint or "删除这个自定义工具（源码文件一起删掉）")

    def _on_tool_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """双击工具行直接编辑；双击分组/分类行不用管（Qt 自己会展开收起）。"""
        if not item.data(0, Qt.UserRole):
            return
        self._edit_tool()

    def _open_tools_dir(self) -> None:
        try:
            custom_ops.OPS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(custom_ops.OPS_DIR)))

    def _request_new_tool(self) -> None:
        """保存后把「新建工具」交回主窗口：生成流程要调大模型、要弹预览窗，
        放在这个模态设置窗里会叠成两层模态。"""
        if not is_ready(load_cfg()):
            QMessageBox.information(
                self, "需要大模型",
                "按描述生成工具需要接入大模型：先到「通用」页点「打开模型设置」，"
                "填好 API Key（默认用 DeepSeek）。")
            return
        self.new_tool_requested = True
        if not self._save():
            self.new_tool_requested = False

    def _edit_tool(self) -> None:
        name = self._selected_tool()
        if not name:
            QMessageBox.information(self, "工具", "先选中一个工具。")
            return
        if not is_custom(name):
            QMessageBox.information(
                self, "工具", "内置工具不能改代码；不想用的话取消勾选停用即可。")
            return
        spec = next((s for s in custom_ops.load_all() if s.get("name") == name), None)
        if spec is None:
            QMessageBox.warning(self, "工具", f"找不到「{name}」的源码文件，可能已被删除。")
            self._refresh_tools()
            return
        dlg = OperatorDialog(spec, self, group_choices=tool_prefs.group_choices(),
                              editing=True)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            saved = custom_ops.save(dlg.value())
            custom_ops.install(saved)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return
        # 编辑弹窗里可能刚「＋ 新建分组…」：立刻落盘，别等用户点这个窗口的「保存」
        try:
            cfg = load_cfg()
            groups = tool_prefs.custom_groups()
            if cfg.get("tool_groups") != groups:
                cfg["tool_groups"] = groups
                save_cfg(cfg)
        except Exception:
            pass
        # 改完重装后工具表里的中文名/说明/分组都变了，整棵树要重建；
        # 勾选状态仍按 name 保留（用户刚取消勾选、还没保存的情况不能被重置）。
        self._refresh_tools()
        self._select_tool(saved.get("name") or name)
        self.tools_changed.emit()

    def _delete_tool(self) -> None:
        name = self._selected_tool()
        if not name:
            QMessageBox.information(self, "工具", "先选中一个工具。")
            return
        if not is_custom(name):
            QMessageBox.information(
                self, "工具", "内置工具不能删除；不想用的话取消勾选停用即可。")
            return
        title = TOOL_LABEL.get(name, name)
        if QMessageBox.question(
                self, "删除工具",
                f"删除「{title}」（{name}）？\n"
                f"源码文件 data/operators/{name}.json 会一起删掉，无法恢复。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        if not custom_ops.remove(name):
            QMessageBox.warning(self, "删除失败", "删除失败：文件被占用或被拒绝访问。")
            return
        self._tool_enabled.pop(name, None)
        # 删掉的工具同时从停用表里清掉，否则下次启动会留下一个不存在的名字
        try:
            cfg = load_cfg()
            old = list(cfg.get("tools_disabled") or [])
            keep = [n for n in old if n != name]
            if len(keep) != len(old):
                cfg["tools_disabled"] = keep
                save_cfg(cfg)
        except Exception:
            pass
        self._refresh_tools()
        self.tools_changed.emit()

    # ------------------------------------------------------------- 通用
    def _build_general_tab(self, cfg: dict) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(8, 10, 8, 8)
        vl.setSpacing(10)
        self.chk_confirm = QCheckBox("执行前逐步确认每一步")
        self.chk_confirm.setChecked(bool(cfg.get("confirm_steps", True)))
        vl.addWidget(self.chk_confirm)
        tip = QLabel("开启后，每个空间算子执行前都会弹窗确认；确认窗口里可以输入"
                     "补充要求，让模型只重写当前这一步。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        vl.addWidget(tip)
        self.chk_skills = QCheckBox("启用技能（把技能正文加入规划提示）")
        self.chk_skills.setChecked(bool(cfg.get("skills_enabled", True)))
        vl.addWidget(self.chk_skills)
        tip2 = QLabel("技能只对大模型规划生效；规则引擎不使用技能正文。"
                      "技能文件放在项目根目录 skills/。")
        tip2.setObjectName("muted")
        tip2.setWordWrap(True)
        vl.addWidget(tip2)

        vl.addStretch(1)
        model_row = QHBoxLayout()
        model = QLabel("模型接口在「模型」页配置，记忆在「记忆」页管理。")
        model.setObjectName("muted")
        model.setWordWrap(True)
        model_row.addWidget(model)
        model_row.addStretch(1)
        vl.addLayout(model_row)
        return w

    # ------------------------------------------------------------- 保存
    def _save(self) -> bool:
        cfg = load_cfg()
        # 模型页：接口地址 / Key / 格式 / 是否启用，一起写回 settings.json
        if self.model_page is not None:
            cfg.update(self.model_page.value())
        cfg["confirm_steps"] = bool(self.chk_confirm.isChecked())
        cfg["skills_enabled"] = bool(self.chk_skills.isChecked())
        disabled = set(str(x) for x in (cfg.get("skills_disabled") or []))
        for s in list_skills():
            if self._enabled_state(s):
                disabled.discard(s.name)
                disabled.discard(s.filename)
            else:
                disabled.add(s.name)
        cfg["skills_disabled"] = sorted(disabled)
        # 工具停用：以树上的勾选状态为准整表重写，顺带把已删除工具的名字清掉
        cfg["tools_disabled"] = sorted(
            n for n, on in self._tool_enabled.items() if not on)
        # 用户新建的工具分组：编辑工具时也可能刚建了一个，一并存下来
        cfg["tool_groups"] = tool_prefs.custom_groups()
        try:
            save_cfg(cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return False
        # 让主窗口立刻同步运行时状态并刷新左栏工具库，不用重开设置
        self.tools_changed.emit()
        self.accept()
        return True
