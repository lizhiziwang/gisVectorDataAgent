"""自定义工具「分组」功能的回归测试（不联网）。

覆盖：
· 新建工具弹窗有分组下拉，选项 = 内置分组 + 我的工具，默认选中「我的工具」
· 没选分组 → 落「我的工具」；选了 → 落选中的组；非法分组 → 回落默认
· 分组随工具一起落盘，重启能装回来并保留分组
· 工具库里自定义工具按自己的分组归位
· 同进程内二次安装（改完重存）不被误判成「与内置算子冲突」
· 撞上真正的内置算子名仍然被拒绝

用法：python tools/test_tool_groups.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else want(got)
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


def cleanup(names):
    """把测试产生的算子文件与运行期注册清掉，不留残留。"""
    from app import custom_ops
    from app.engine import OPERATIONS
    from app.tool_registry import REGISTRY
    for n in names:
        custom_ops.remove(n)
        OPERATIONS.pop(n, None)
        REGISTRY.pop(n, None)


CODE = 'def op(gdf, args):\n    return gdf, "ok"\n'

print("[1] 分组常量与候选列表")
from app.tool_registry import (BUILTIN_GROUP_ORDER, CUSTOM_GROUP, GROUP_CHOICES,
                                groups as registry_groups)

check("默认分组名", CUSTOM_GROUP, "我的工具")
check("候选含全部内置分组", all(g in GROUP_CHOICES for g in BUILTIN_GROUP_ORDER), True)
check("候选含默认分组", CUSTOM_GROUP in GROUP_CHOICES, True)
check("默认分组排在最后", GROUP_CHOICES[-1], CUSTOM_GROUP)
check("groups() 排序表用新名", "我的算子" not in
      [g for g, _ in registry_groups()], True)

print("[2] 弹窗有分组下拉")
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])

from app import tool_prefs

from native.dialogs import GroupCombo, NewToolDialog, OperatorDialog

# --- 输入弹窗（「＋ 新建工具」第一步，截图里那个）
inp = NewToolDialog(None, group_choices=GROUP_CHOICES)
items_in = [inp.group_combo.itemText(i) for i in range(inp.group_combo.count())]
check("输入弹窗选项 = 内置分组 + 我的工具 + 新建入口", items_in,
      GROUP_CHOICES + [GroupCombo.NEW_ITEM])
check("输入弹窗默认选中我的工具", inp.group_combo.currentText(), CUSTOM_GROUP)
inp.editor.setPlainText("统计每个面内道路的总长度")
check("输入弹窗 value() 带出描述", inp.value()[0], "统计每个面内道路的总长度")
check("输入弹窗 value() 带出分组", inp.value()[1], CUSTOM_GROUP)

inp2 = NewToolDialog(None, group_choices=GROUP_CHOICES)
inp2.group_combo.setCurrentText("属性与统计")
check("输入弹窗选了别的组", inp2.value()[1], "属性与统计")

inp3 = NewToolDialog(None)
check("输入弹窗不传候选就用当前候选",
      [inp3.group_combo.itemText(i) for i in range(inp3.group_combo.count())],
      tool_prefs.group_choices() + [GroupCombo.NEW_ITEM])

# --- 预览弹窗（生成之后的代码预览）
dlg = OperatorDialog({"name": "t1", "title": "测试", "code": CODE}, None,
                      group_choices=GROUP_CHOICES)
items = [dlg.group_combo.itemText(i) for i in range(dlg.group_combo.count())]
check("选项 = 内置分组 + 我的工具 + 新建入口", items,
      GROUP_CHOICES + [GroupCombo.NEW_ITEM])
check("默认选中我的工具", dlg.group_combo.currentText(), CUSTOM_GROUP)
check("value() 带出分组", dlg.value().get("group"), CUSTOM_GROUP)

dlg2 = OperatorDialog({"name": "t2", "title": "测试2", "code": CODE}, None)
items2 = [dlg2.group_combo.itemText(i) for i in range(dlg2.group_combo.count())]
check("不传候选就用当前候选", items2,
      tool_prefs.group_choices() + [GroupCombo.NEW_ITEM])
check("不传候选时默认选中", dlg2.group_combo.currentText(), CUSTOM_GROUP)

dlg3 = OperatorDialog({"name": "t3", "title": "测试3", "group": "整理", "code": CODE},
                       None, group_choices=GROUP_CHOICES)
check("spec 带分组时预选中", dlg3.group_combo.currentText(), "整理")
check("value() 带出预选分组", dlg3.value().get("group"), "整理")

dlg4 = OperatorDialog({"name": "t4", "title": "测试4", "group": "不存在的组",
                        "code": CODE}, None, group_choices=GROUP_CHOICES)
check("非法分组回落默认", dlg4.group_combo.currentText(), CUSTOM_GROUP)

print("[3] 分组随工具落盘")
from app import custom_ops
from app.tool_registry import REGISTRY

NAMES = ["grp_a", "grp_b", "grp_c"]
try:
    s = custom_ops.save({"name": "grp_a", "title": "A", "code": CODE,
                         "params": {"layer": "图层名"}})
    check("未指定分组 -> 我的工具", s["group"], CUSTOM_GROUP)

    s2 = custom_ops.save({"name": "grp_b", "title": "B", "group": "整理", "code": CODE,
                          "params": {"layer": "图层名"}})
    check("指定分组 -> 保留", s2["group"], "整理")

    s3 = custom_ops.save({"name": "grp_c", "title": "C", "group": "瞎写的组", "code": CODE,
                          "params": {"layer": "图层名"}})
    check("非法分组 -> 回落", s3["group"], CUSTOM_GROUP)

    import json
    raw = json.loads((custom_ops.OPS_DIR / "grp_b.json").read_text(encoding="utf-8"))
    check("分组写进了 json", raw.get("group"), "整理")

    print("[4] install 把分组透传给工具表")
    custom_ops.install(s)
    check("默认组已登记", REGISTRY["grp_a"].group, CUSTOM_GROUP)
    custom_ops.install(s2)
    check("指定组已登记", REGISTRY["grp_b"].group, "整理")

    print("[5] 同进程二次安装（改完重存）不应误报冲突")
    try:
        custom_ops.install(s2)
        check("允许覆盖自家算子", True, True)
    except Exception as e:
        check("允许覆盖自家算子", f"抛错：{e}", False)

    print("[6] 撞内置算子名仍要拒绝")
    try:
        custom_ops.install({"name": "buffer", "title": "x", "code": CODE})
        check("拒绝覆盖内置 buffer", False, True)
    except Exception:
        check("拒绝覆盖内置 buffer", True, True)

    print("[7] 工具库里按自己的分组归位")
    from native.app import MainWindow
    # _tool_groups 依赖 TOOL_SPEC_GROUP，它由 _refresh_tools 从 TOOL_GROUP 同步
    from native.app import TOOL_SPEC_GROUP
    from app.agent import TOOL_GROUP
    TOOL_SPEC_GROUP.clear()
    TOOL_SPEC_GROUP.update(TOOL_GROUP)
    tg = MainWindow._tool_groups()
    in_zhengli = any("grp_b" in names for title, names in tg if title == "整理")
    in_mine = any("grp_a" in names for title, names in tg if title == CUSTOM_GROUP)
    check("指定组的进了「整理」", in_zhengli, True)
    check("默认组的进了「我的工具」", in_mine, True)

    print("[8] 输入弹窗选的分组一路传到落盘")
    inp = NewToolDialog(None, group_choices=GROUP_CHOICES)
    inp.group_combo.setCurrentText("属性与统计")
    inp.editor.setPlainText("按用地类型统计面积")
    _text, picked = inp.value()
    # 模拟 _handle_operator_gen：输入弹窗的分组作为预览弹窗的默认值
    spec = {"name": "grp_d", "title": "D", "code": CODE}
    spec.setdefault("group", picked)
    prev = OperatorDialog(spec, None, group_choices=GROUP_CHOICES)
    check("预览弹窗预选了输入弹窗的组", prev.group_combo.currentText(), "属性与统计")
    saved_d = custom_ops.save(prev.value())
    check("落盘保留该组", saved_d["group"], "属性与统计")
    custom_ops.install(saved_d)
    check("注册表保留该组", REGISTRY["grp_d"].group, "属性与统计")
    NAMES.append("grp_d")

    print("[9] remove 清理干净")
    from app.agent import TOOL_GROUP
    custom_ops.remove("grp_b")
    check("已从 OPERATIONS 移除", "grp_b" in __import__("app.engine", fromlist=["OPERATIONS"]).OPERATIONS, False)
    check("已从工具表移除", "grp_b" in REGISTRY, False)
    check("已从分组表移除", "grp_b" in TOOL_GROUP, False)
    check("文件已删", (custom_ops.OPS_DIR / "grp_b.json").exists(), False)
finally:
    cleanup(NAMES)

print("[10] 工具库两级分类与收缩面板")
from native.app import CollapsibleSection, MainWindow
tree = MainWindow._tool_tree()
kinds = [k for k, _ in tree]
check("一级分类是 矢量 / 栅格", kinds, ["矢量", "栅格"])
raster_sections = dict(tree[1][1])
check("栅格按功能分组", list(raster_sections), ["概览", "处理", "分析", "转换"])
check("栅格分析组有栅格计算/分区统计",
      raster_sections["分析"], ["raster_calc", "zonal_stats"])
check("矢量组仍保留整理", any(t == "整理" for t, _ in tree[0][1]), True)

sec = CollapsibleSection("演示分组", 2)
check("默认展开", sec.is_expanded() and not sec.body.isHidden(), True)
sec.header.setChecked(False)
check("点击后收起", (not sec.is_expanded()) and sec.body.isHidden(), True)
check("收起后箭头朝右", sec.header.text().startswith("▸"))
sec.header.setChecked(True)
check("再点展开", sec.is_expanded() and not sec.body.isHidden(), True)
check("展开后箭头朝下", sec.header.text().startswith("▾"))

flat = MainWindow._tool_groups()
check("扁平兼容视图里栅格分组有前缀",
      any(t.startswith("栅格 · ") for t, _ in flat), True)
check("扁平兼容视图里仍能找到矢量整理",
      any(t == "整理" for t, _ in flat), True)
sec.deleteLater()


print("[11] 新建分组（「＋ 新建分组…」）")
from app.tool_registry import CUSTOM_GROUP as _DEFAULT_GROUP

tool_prefs.set_groups([])
check("初始没有自定义分组", tool_prefs.custom_groups(), [])
check("候选里也没有", "城市规划" in tool_prefs.group_choices(), False)
check("新建分组返回名字", tool_prefs.add_group("城市规划"), "城市规划")
check("记进了自定义分组", tool_prefs.custom_groups(), ["城市规划"])
check("候选里排在「我的工具」前面",
      tool_prefs.group_choices()[-2:], ["城市规划", CUSTOM_GROUP])
check("重复新建是幂等的", tool_prefs.add_group("城市规划"), "城市规划")
check("名字会去掉多余空白", tool_prefs.add_group("  国土  空间 "), "国土 空间")
try:
    tool_prefs.add_group("   ")
    check("空名字被拒绝", False, True)
except ValueError:
    check("空名字被拒绝", True, True)
try:
    tool_prefs.add_group("一二三四五六七八九十十一十二")
    check("超长名字被拒绝", False, True)
except ValueError:
    check("超长名字被拒绝", True, True)

# 弹窗：新分组能加进下拉、value() 带得出来
inp = NewToolDialog(None)
check("新分组进了输入弹窗下拉", inp.group_combo.findText("城市规划") >= 0, True)
inp.group_combo.setCurrentText("城市规划")
inp.editor.setPlainText("按道路等级统计里程")
check("输入弹窗 value() 带出新分组", inp.value()[1], "城市规划")

# 落盘：白名单认自定义分组，工具库按新分组分组
g = custom_ops.save({"name": "grp_new", "title": "新组工具", "group": "城市规划",
                     "code": CODE, "params": {"layer": "图层名"}})
NAMES.append("grp_new")
check("自定义分组能落盘", g["group"], "城市规划")
custom_ops.install(g)
from app.agent import TOOL_GROUP as _TG
check("分组透传给工具表", _TG.get("grp_new"), "城市规划")
g2 = custom_ops.save({"name": "grp_new2", "title": "默认组工具",
                      "code": CODE, "params": {"layer": "图层名"}})
NAMES.append("grp_new2")
custom_ops.install(g2)
tree2 = MainWindow._tool_tree()
titles = [t for _, secs in tree2 for t, _ in secs]
check("工具库出现新分组", "城市规划" in titles, True)
check("新分组里有这个工具",
      dict(tree2[0][1]).get("城市规划"), ["grp_new"])
vector_titles = [t for t, _ in tree2[0][1]]
check("「我的工具」在矢量分组最后", vector_titles[-1], CUSTOM_GROUP)
check("自定义分组排在「我的工具」前面",
      vector_titles[-2:], ["城市规划", CUSTOM_GROUP])

# 配置：tool_groups 存得住、坏值被清掉
from app.config import normalize
cfg2 = normalize({"tool_groups": ["城市规划", "   ", "x" * 20, "城市规划"]})
check("配置里的分组被规整", cfg2["tool_groups"], ["城市规划"])

# 真点一次「＋ 新建分组…」：把 QInputDialog 换成假的，走一遍真实信号路径
import native.dialogs as _dlg_mod

_real_get = _dlg_mod.QInputDialog.getText
_reply = {"text": "管线巡检", "ok": True}


def _fake_get(parent, title, label, *a, **k):
    return (_reply["text"], _reply["ok"])


_dlg_mod.QInputDialog.getText = _fake_get
_real_warn = _dlg_mod.QMessageBox.warning
_dlg_mod.QMessageBox.warning = staticmethod(lambda *a, **k: print('    (拦下提示框)'))
try:
    inp4 = NewToolDialog(None, group_choices=tool_prefs.group_choices())
    inp4.group_combo.setCurrentIndex(inp4.group_combo.count() - 1)
    inp4.group_combo.activated.emit(inp4.group_combo.currentIndex())
    app.processEvents()
    check("点「＋ 新建分组…」后选中新分组", inp4.group_combo.currentText(), "管线巡检")
    check("新分组进了候选", "管线巡检" in tool_prefs.group_choices(), True)
    check("value() 带出新建的分组", inp4.value()[1], "管线巡检")

    # 取消输入：回到原来的选择，不留半个分组
    inp4.group_combo.setCurrentText(CUSTOM_GROUP)
    inp4.group_combo.setCurrentIndex(inp4.group_combo.count() - 1)
    _reply.update({"text": "", "ok": False})
    inp4.group_combo.activated.emit(inp4.group_combo.currentIndex())
    app.processEvents()
    check("取消新建 → 回到原来的组", inp4.group_combo.currentText(), CUSTOM_GROUP)

    # 名字不合法：拦住并回退，不写进候选
    before = tool_prefs.group_choices()
    _reply.update({"text": "x" * 40, "ok": True})
    inp4.group_combo.setCurrentIndex(inp4.group_combo.count() - 1)
    inp4.group_combo.activated.emit(inp4.group_combo.currentIndex())
    app.processEvents()
    check("非法名字不写进候选", tool_prefs.group_choices(), before)
finally:
    _dlg_mod.QInputDialog.getText = _real_get
    _dlg_mod.QMessageBox.warning = _real_warn


check("配置里没有就不写这个键", "tool_groups" in normalize({}), False)
custom_ops.remove("grp_new")
custom_ops.remove("grp_new2")
tool_prefs.set_groups([])


print("[12] 主窗口把新建分组写进 settings.json")
import native.app as _app_mod

_store: dict = {}
_real_load_cfg = _app_mod.load_cfg
_real_save_cfg = _app_mod.save_cfg
_app_mod.load_cfg = lambda: dict(_store)
_app_mod.save_cfg = lambda c: _store.update(dict(c))
_win = None
try:
    tool_prefs.set_groups([])
    _win = _app_mod.MainWindow()
    _win._save_tool_group("管线巡检")
    check("写进了 tool_groups", _store.get("tool_groups"), ["管线巡检"])
    # 再报一次同名分组不应该重复写
    _win._save_tool_group("管线巡检")
    check("重复保存不叠加", _store.get("tool_groups"), ["管线巡检"])
    # 重启：清空内存再从配置灌回去
    tool_prefs.set_groups([])
    check("清空后候选里没有", "管线巡检" in tool_prefs.group_choices(), False)
    tool_prefs.set_groups(_store.get("tool_groups") or [])
    check("重启后能读回", tool_prefs.custom_groups(), ["管线巡检"])
    # 内置分组不该被当成「自建」记下来
    _win._save_tool_group("整理")
    check("内置分组不进 tool_groups", _store.get("tool_groups"), ["管线巡检"])
    _win._save_tool_group("  ")
    check("空名字直接忽略", _store.get("tool_groups"), ["管线巡检"])

    # 走一遍「＋ 新建工具」的真实入口：弹窗返回值要带着分组传到生成流程
    from PySide6.QtWidgets import QDialog as _QDialog

    class _FakeNewToolDialog:
        def __init__(self, parent=None, group_choices=None):
            self.choices = list(group_choices or [])

        def exec(self):
            return _QDialog.Accepted

        def value(self):
            return ("统计每个面内道路的总长度", "管线巡检")

    _seen = {}
    _real_dlg_cls = _app_mod.NewToolDialog
    _real_ready = _app_mod.is_ready
    _app_mod.NewToolDialog = _FakeNewToolDialog
    _app_mod.is_ready = lambda cfg: True
    try:
        _win._run_operator_gen = (
            lambda brief, group="": _seen.update({"brief": brief, "group": group}))
        _win.prompt_new_operator()
    finally:
        _app_mod.NewToolDialog = _real_dlg_cls
        _app_mod.is_ready = _real_ready
    check("描述进了生成流程", _seen.get("brief"), "统计每个面内道路的总长度")
    check("分组一起传下去", _seen.get("group"), "管线巡检")
    check("入口里也落了盘", _store.get("tool_groups"), ["管线巡检"])

    # 启动阶段：磁盘上的 tool_groups 要灌进 tool_prefs（左栏和两个弹窗都靠它）
    tool_prefs.set_groups([])
    _app_mod.load_cfg = lambda: {"tool_groups": ["城市规划"]}
    _old_persist = os.environ.get("GIS_AGENT_PERSIST")
    os.environ["GIS_AGENT_PERSIST"] = "0"   # 第二个窗口别去开同一份会话库
    _win2 = None
    try:
        _win2 = _app_mod.MainWindow()
        check("启动即装载自建分组", tool_prefs.custom_groups(), ["城市规划"])
        check("启动后候选里有它", "城市规划" in tool_prefs.group_choices(), True)
    finally:
        if _old_persist is None:
            os.environ.pop("GIS_AGENT_PERSIST", None)
        else:
            os.environ["GIS_AGENT_PERSIST"] = _old_persist
        if _win2 is not None:
            _win2.deleteLater()
        _app_mod.load_cfg = lambda: dict(_store)
finally:
    _app_mod.load_cfg = _real_load_cfg
    _app_mod.save_cfg = _real_save_cfg
    if _win is not None:
        _win.deleteLater()
    tool_prefs.set_groups([])

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(1 if fails else 0)
