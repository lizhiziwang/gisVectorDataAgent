"""左栏「工作区」页签回归：工作区 → 会话 → 会话图层。

覆盖：
· 左栏有「图层 / 工作区」两个页签；
· 工作区根节点 + 未分组 + 会话列表；
· 会话显示相对时间、图层数、图层子项；
· 点击会话发出 open_session；搜索过滤；
· 新建会话按钮发出 new_session。

用法：.venv/Scripts/python.exe tools/test_workspace_ui.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from native.workspace import WorkspacePanel, _relative_time

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def find(item, name):
    if hasattr(item, "topLevelItemCount"):
        n = item.topLevelItemCount()
        get = item.topLevelItem
    else:
        n = item.childCount()
        get = item.child
    for i in range(n):
        if get(i).text(0) == name:
            return get(i)
    return None


def main() -> int:
    app = QApplication.instance() or QApplication([])
    now = time.time()
    sessions = [
        {"id": "s1", "title": "栅格渲染影响矢量渲染", "updated_at": now - 16 * 60,
         "layer_count": 2, "layer_names": ["乡行政区划", "村行政区划"]},
        {"id": "s2", "title": "你是什么模型", "updated_at": now - 21 * 3600,
         "layer_count": 0, "layer_names": []},
        {"id": "s3", "title": "Agent 应用优化", "updated_at": now - 3 * 86400,
         "layer_count": 1, "layer_names": ["影像"]},
    ]

    print("[1] 工作区/会话结构")
    panel = WorkspacePanel(lambda: list(sessions), workspace_name="gis-agent")
    panel.resize(272, 500)
    panel.show()
    app.processEvents()
    root = panel.tree.topLevelItem(0)
    check("根节点是工作区名", root.text(0), "gis-agent")
    group = find(root, "未分组")
    check("有未分组分组", group is not None, True)
    check("三个会话", group.childCount() if group else 0, 3)
    first = find(group, "栅格渲染影响矢量渲染") if group else None
    check("会话时间列", first.text(1) if first else "", "16 分钟")
    check("会话图层数子项", first.childCount() if first else 0, 2)
    check("会话图层名", first.child(0).text(0) if first else "", "乡行政区划")
    check("会话图标不是系统默认", first.icon(0).isNull() if first else True, False)

    print("[2] 点击打开会话")
    got = []
    panel.open_session.connect(lambda sid: got.append(sid))
    if first is not None:
        panel._on_click(first, 0)
        check("发出打开会话信号", got, ["s1"])

    print("[3] 搜索过滤")
    panel.search.setText("模型")
    app.processEvents()
    group = find(panel.tree.topLevelItem(0), "未分组")
    names = [group.child(i).text(0) for i in range(group.childCount())] if group else []
    check("只留下匹配会话", names, ["你是什么模型"])
    panel.search.clear()
    app.processEvents()

    print("[4] 新建会话按钮")
    made = []
    panel.new_session.connect(lambda: made.append(True))
    panel.btn_new.click()
    app.processEvents()
    check("发出新建会话信号", made, [True])

    print("[5] 相对时间格式")
    check("刚刚", _relative_time(now - 10), "刚刚")
    check("小时", _relative_time(now - 2 * 3600), "2 小时")
    check("天", _relative_time(now - 3 * 86400), "3 天")

    print("[6] 主窗口页签")
    os.environ["GIS_AGENT_PERSIST"] = "0"
    from native.app import MainWindow

    win = MainWindow()
    win.resize(1000, 760)
    win.show()
    app.processEvents()
    check("两个页签", win.side_tabs.count(), 2)
    check("图层页存在", win.side_tabs.tabText(0).startswith("图层"), True)
    check("工作区页存在", win.side_tabs.tabText(1), "工作区")
    check("工作区有供应商提供的会话", win.workspace.tree.topLevelItemCount() > 0, True)
    win.tasks.shutdown(wait=False)
    win.close()
    panel.close()
    panel.deleteLater()
    win.deleteLater()
    app.processEvents()
    app.quit()

    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
