"""Agent 设置窗口：技能管理 + 每步确认开关。

头部的「Agent 设置」打开这里；模型接口仍由原来的「模型设置」管理，
本窗口只放 Agent 自己的行为配置，避免把两类配置混在一起。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QTabWidget, QVBoxLayout, QWidget)

from app.config import load_cfg, save_cfg
from app.skills import (SKILLS_DIR, Skill, delete_skill, ensure_dir, list_skills,
                        save_skill, set_skill_enabled)


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
    """Agent 自己的设置（和模型接口设置分开）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Agent 设置")
        self.resize(620, 560)
        self._pending: dict[str, bool] = {}
        cfg = load_cfg()
        vl = QVBoxLayout(self)
        vl.setContentsMargins(14, 12, 14, 12)
        vl.setSpacing(8)
        tabs = QTabWidget()
        tabs.setObjectName("agentSettingsTabs")
        tabs.addTab(self._build_skills_tab(), "技能")
        tabs.addTab(self._build_general_tab(cfg), "通用")
        vl.addWidget(tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        vl.addWidget(buttons)

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
        model = QLabel(f"当前模型：{cfg.get('model') or '未配置'}")
        model.setObjectName("muted")
        model_row.addWidget(model)
        model_row.addStretch(1)
        b_model = QPushButton("打开模型设置…")
        b_model.setObjectName("ghost")
        b_model.clicked.connect(self._open_model_settings)
        model_row.addWidget(b_model)
        vl.addLayout(model_row)
        return w

    def _open_model_settings(self) -> None:
        try:
            from .dialogs import SettingsDialog
            SettingsDialog(load_cfg(), self).exec()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "模型设置", str(e))

    # ------------------------------------------------------------- 保存
    def _save(self) -> None:
        cfg = load_cfg()
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
        try:
            save_cfg(cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.accept()
