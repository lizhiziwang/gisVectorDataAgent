"""工具库管理（Agent 设置 →「工具」）的回归测试（不联网）。

覆盖：
· tool_tree 默认隐藏停用工具；include_disabled=True 时全量返回
· 停用的工具不进大模型候选，执行前被拦下并给出「去哪儿启用」的提示
· 设置页工具树：内置/自定义来源、分组计数、搜索过滤、勾选停用
· 保存后 cfg["tools_disabled"] 落盘，主窗口启动时按它过滤左栏工具库
· 自定义工具可编辑（改名/说明/代码/分组）与删除；内置工具不能改/删
· 重新启用后工具回到工具库

用法：python tools/test_tool_manage.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else want(got)
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


# 设置写盘改成临时文件：测试不能动用户真实的 data/settings.json
import app.config as cfg_mod

_tmp = tempfile.mkdtemp(prefix="gistool_")
_orig_cfg_path = cfg_mod.CONFIG
cfg_mod.CONFIG = Path(_tmp) / "settings.json"

from app import custom_ops, tool_prefs
from app.agent import active_tool_specs, run_plan
from app.engine import OPERATIONS, Store
from app.tool_catalog import tool_tree

CUSTOM_CODE = 'def op(gdf, args):\n    return gdf, "ok"\n'


def tree_names(include_disabled=True):
    """把两级分类拍平成算子名列表。"""
    out = []
    for _, sections in tool_tree(include_disabled=include_disabled):
        for _, names in sections:
            out.extend(names)
    return out


print("[1] 停用状态影响分类、模型候选与执行")
tool_prefs.set_disabled(["buffer"])
check("分类默认隐藏停用工具", "buffer" in tree_names(include_disabled=False), False)
check("设置页仍能看到停用工具", "buffer" in tree_names(include_disabled=True), True)
check("停用工具不进模型候选",
      "buffer" in [s["name"] for s in active_tool_specs()], False)
check("启用工具仍在模型候选",
      "clip" in [s["name"] for s in active_tool_specs()], True)

store = Store()
steps, lines, _layers = run_plan(
    store, [{"tool": "buffer", "args": {"layer": "地块", "distance": 100, "unit": "m"}}])
check("停用工具执行被拦下", steps and steps[0]["status"], "error")
check("报错提示去设置里启用", "停用" in (steps[0]["message"] if steps else ""), True)

tool_prefs.set_disabled([])

print("[2] 设置页工具树")
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
app = QApplication.instance() or QApplication([])

from native.agent_settings import AgentSettingsDialog

dlg = AgentSettingsDialog(None)


def leaves(tree):
    out = []
    for i in range(tree.topLevelItemCount()):
        kind = tree.topLevelItem(i)
        for j in range(kind.childCount()):
            group = kind.child(j)
            for k in range(group.childCount()):
                out.append(group.child(k))
    return out


def find_leaf(tree, name):
    for item in leaves(tree):
        if str(item.data(0, 0x0100) or "") == name:      # Qt.UserRole == 0x0100
            return item
    return None


kinds = [dlg.tool_tree.topLevelItem(i).text(0)
         for i in range(dlg.tool_tree.topLevelItemCount())]
check("一级分类是矢量/栅格", [k.split("（")[0] for k in kinds], ["矢量", "栅格"])
check("工具总数与注册表一致", len(leaves(dlg.tool_tree)), len(OPERATIONS))
check("所有工具默认勾选",
      all(i.checkState(0) == Qt.CheckState.Checked for i in leaves(dlg.tool_tree)), True)

buffer_item = find_leaf(dlg.tool_tree, "buffer")
check("工具名旁边有来源列", buffer_item.text(1), "内置")
check("内置工具可勾选停用",
      bool(buffer_item.flags() & Qt.ItemFlag.ItemIsUserCheckable), True)

print("[3] 搜索过滤")
dlg.tool_search.setText("缓冲")
visible = [i for i in leaves(dlg.tool_tree) if not i.isHidden()]
check("只留下匹配项", [i.text(0) for i in visible], ["缓冲区"])
check("没有命中的分组被收起", dlg.tool_tree.topLevelItem(1).isHidden(), True)
dlg.tool_search.setText("整理")
group_hits = [i for i in leaves(dlg.tool_tree) if not i.isHidden()]
check("按分组名筛选整组留下", len(group_hits), 7)
dlg.tool_search.setText("")
check("清空搜索后全部恢复",
      len([i for i in leaves(dlg.tool_tree) if not i.isHidden()]), len(OPERATIONS))

print("[4] 勾选停用并保存")
buffer_item.setCheckState(0, Qt.CheckState.Unchecked)
check("取消勾选后标记为停用", dlg._tool_enabled.get("buffer"), False)
check("停用的工具加删除线", buffer_item.font(0).strikeOut(), True)
check("底部计数显示停用数", "停用 1" in dlg.tool_count.text(), True)
check("保存成功", dlg._save(), True)
check("停用列表写进配置", cfg_mod.load_cfg().get("tools_disabled"), ["buffer"])

print("[5] 主窗口启动时按配置隐藏")
from native.app import MainWindow

win = MainWindow()
win._refresh_tools()
shown = [n for _, sections in win._tool_tree() for _, names in sections for n in names]
check("左栏不再显示停用工具", "buffer" in shown, False)
check("徽标数字 = 启用数", win.tool_badge.text(), str(len(OPERATIONS) - 1))
check("徽标提示说明已停用", "已停用" in win.tool_badge.toolTip(), True)

# 设置窗保存 -> tools_changed -> 主窗口立刻刷新（不用重开窗口）。
# 连接方式与 MainWindow.open_agent_settings() 里保持一致。
dlgw = AgentSettingsDialog(win)
dlgw.tools_changed.connect(win._apply_tool_settings)
find_leaf(dlgw.tool_tree, "clip").setCheckState(0, Qt.CheckState.Unchecked)
check("弹窗保存成功", dlgw._save(), True)
shown2 = [n for _, sections in win._tool_tree() for _, names in sections for n in names]
check("左栏立刻跟着去掉", "clip" in shown2, False)
check("徽标数字再减一", win.tool_badge.text(), str(len(OPERATIONS) - 2))
win.close()

print("[6] 自定义工具可编辑")
saved = custom_ops.install(custom_ops.save({
    "name": "tman_edit", "title": "旧名字", "desc": "旧说明",
    "group": "整理", "code": CUSTOM_CODE, "params": {"layer": "图层名"}}))
from native.dialogs import OperatorDialog
from PySide6.QtWidgets import QDialogButtonBox

edlg = OperatorDialog(saved, None, group_choices=["整理", "我的工具"], editing=True)
check("编辑窗标题", "编辑工具" in edlg.windowTitle(), True)
btns = edlg.findChild(QDialogButtonBox)
check("编辑窗按钮文案", btns.button(QDialogButtonBox.Ok).text(), "保存修改")
check("名称可编辑", edlg.title_edit.text(), "旧名字")
edlg.title_edit.setText("新名字")
edlg.desc_edit.setText("新说明")
edlg.group_combo.setCurrentText("我的工具")
edlg.editor.setPlainText(CUSTOM_CODE + "\n# 改过")
value = edlg.value()
check("编辑不改算子名", value.get("name"), "tman_edit")
check("编辑带出新名称", value.get("title"), "新名字")
check("编辑带出新分组", value.get("group"), "我的工具")

from app.agent import TOOL_LABEL
from app.tool_registry import REGISTRY

updated = custom_ops.install(custom_ops.save(value))
check("工具表名称已更新", TOOL_LABEL.get("tman_edit"), "新名字")
check("工具表说明已更新", REGISTRY["tman_edit"].desc, "新说明")
check("工具表分组已更新", REGISTRY["tman_edit"].group, "我的工具")

print("[7] 删除自定义工具（对话框确认）")
import native.agent_settings as AS
from PySide6.QtWidgets import QMessageBox


class _YesBox:
    """替身：把「删除确认」点成 Yes，同时保证不该弹的警告不会静默。

    直接改 QMessageBox.question 在 PySide6 上不一定可写，整类替换最稳。
    """
    Yes = QMessageBox.Yes
    No = QMessageBox.No

    @staticmethod
    def question(*_a, **_k):
        return QMessageBox.Yes

    @staticmethod
    def warning(*_a, **_k):
        raise AssertionError("删除流程不应该弹警告")


dlg2 = AgentSettingsDialog(None)
dlg2._refresh_tools()
item = find_leaf(dlg2.tool_tree, "tman_edit")
check("树上能找到自定义工具", item is not None, True)
if item is not None:
    check("来源列标自定义", item.text(1), "自定义")
dlg2._select_tool("tman_edit")
check("选中自定义工具后可编辑", dlg2.b_edit.isEnabled(), True)

_orig_box = AS.QMessageBox
AS.QMessageBox = _YesBox
try:
    dlg2._delete_tool()
finally:
    AS.QMessageBox = _orig_box
check("文件已删除", (custom_ops.OPS_DIR / "tman_edit.json").exists(), False)
check("运行时注册已移除", "tman_edit" in OPERATIONS, False)
check("树上已消失", find_leaf(dlg2.tool_tree, "tman_edit") is None, True)

print("[8] 内置工具不能编辑/删除")
dlg2._select_tool("buffer")
check("内置工具禁用编辑按钮", dlg2.b_edit.isEnabled(), False)
check("内置工具禁用删除按钮", dlg2.b_del.isEnabled(), False)

print("[9] 重新启用")
tool_prefs.set_disabled([])
check("启用后回到分类", "buffer" in tree_names(include_disabled=False), True)
dlg3 = AgentSettingsDialog(None)
check("设置页显示为勾选",
      find_leaf(dlg3.tool_tree, "buffer").checkState(0), Qt.CheckState.Checked)
dlg3.reject()

# 清理：测试改过的配置与工具文件
cfg_mod.CONFIG = _orig_cfg_path
for n in ("tman_edit",):
    custom_ops.remove(n)
tool_prefs.set_disabled([])
try:
    import shutil
    shutil.rmtree(_tmp, ignore_errors=True)
except Exception:
    pass

print()
if fails:
    print("失败", len(fails), "项：", fails)
    sys.exit(1)
print("全部通过")
