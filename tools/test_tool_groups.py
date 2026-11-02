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

from native.dialogs import NewToolDialog, OperatorDialog

# --- 输入弹窗（「＋ 新建工具」第一步，截图里那个）
inp = NewToolDialog(None, group_choices=GROUP_CHOICES)
items_in = [inp.group_combo.itemText(i) for i in range(inp.group_combo.count())]
check("输入弹窗选项 = 内置分组 + 我的工具", items_in, GROUP_CHOICES)
check("输入弹窗默认选中我的工具", inp.group_combo.currentText(), CUSTOM_GROUP)
inp.editor.setPlainText("统计每个面内道路的总长度")
check("输入弹窗 value() 带出描述", inp.value()[0], "统计每个面内道路的总长度")
check("输入弹窗 value() 带出分组", inp.value()[1], CUSTOM_GROUP)

inp2 = NewToolDialog(None, group_choices=GROUP_CHOICES)
inp2.group_combo.setCurrentText("属性与统计")
check("输入弹窗选了别的组", inp2.value()[1], "属性与统计")

inp3 = NewToolDialog(None)
check("输入弹窗不传候选也有默认组",
      [inp3.group_combo.itemText(i) for i in range(inp3.group_combo.count())],
      [CUSTOM_GROUP])

# --- 预览弹窗（生成之后的代码预览）
dlg = OperatorDialog({"name": "t1", "title": "测试", "code": CODE}, None,
                      group_choices=GROUP_CHOICES)
items = [dlg.group_combo.itemText(i) for i in range(dlg.group_combo.count())]
check("选项 = 内置分组 + 我的工具", items, GROUP_CHOICES)
check("默认选中我的工具", dlg.group_combo.currentText(), CUSTOM_GROUP)
check("value() 带出分组", dlg.value().get("group"), CUSTOM_GROUP)

dlg2 = OperatorDialog({"name": "t2", "title": "测试2", "code": CODE}, None)
items2 = [dlg2.group_combo.itemText(i) for i in range(dlg2.group_combo.count())]
check("不传候选也有默认组", items2, [CUSTOM_GROUP])
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

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(1 if fails else 0)
