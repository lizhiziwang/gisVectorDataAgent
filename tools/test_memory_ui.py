"""Phase 3 桌面端回归：记忆面板、持久化跨会话、任务中心 UI。

用法：.venv\\Scripts\\python.exe tools\\test_memory_ui.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["GIS_AGENT_PERSIST"] = "0"

import geopandas as gpd
from PySide6.QtWidgets import QApplication, QMessageBox
from shapely.geometry import Polygon

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


import app.config as app_config
app_config.is_ready = lambda cfg: False

from native.app import MainWindow
from native.memory_panel import MemoryPanel

app = QApplication.instance() or QApplication([])

print("[1] 记忆面板基本功能")
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    from app.memory import MemoryManager
    from app.models import Memory
    from app.storage import AgentStore

    store = AgentStore(Path(td) / "agent.db", project_root=td)
    proj = store.ensure_default_project()
    sess = store.create_session(proj.id, "面板测试")
    mem = MemoryManager(store, proj.id, sess.id)
    mem.observe_turn("以后默认用米，导出都用 gpkg", {"steps": []}, sess.id)
    mem.observe_turn("我说的不是道路，是地块内的管线", {"steps": []}, sess.id)
    mem.observe_turn("对地块做缓冲", {"steps": [
        {"tool": "buffer", "status": "ok",
         "layer": {"name": "地块_缓冲", "fields": ["用地类型", "面积"],
                   "crs": "EPSG:4544", "count": 665, "geomType": "Polygon"}}]},
        sess.id)

    panel = MemoryPanel(mem)
    panel.reload()
    check("面板有分组", panel.tree.topLevelItemCount() >= 2, True)
    total = 0
    for i in range(panel.tree.topLevelItemCount()):
        total += panel.tree.topLevelItem(i).childCount()
    check("面板列出记忆", total >= 3, True)

    # 选一条 → 删除
    found = None
    for i in range(panel.tree.topLevelItemCount()):
        top = panel.tree.topLevelItem(i)
        for j in range(top.childCount()):
            child = top.child(j)
            if "default_unit" in (child.toolTip(0) or ""):
                found = child
                break
    check("找到偏好项", found is not None, True)
    if found is not None:
        panel.tree.setCurrentItem(found)
        before = len(store.list_memories(proj.id))
        panel._forget()
        after = len(store.list_memories(proj.id))
        check("删除生效", after, before - 1)

    # 导出 JSON
    out = Path(td) / "mem.json"
    import PySide6.QtWidgets as W
    orig = W.QFileDialog.getSaveFileName
    W.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(out), ""))
    try:
        panel._export()
    finally:
        W.QFileDialog.getSaveFileName = orig
    check("导出文件存在", out.exists(), True)
    if out.exists():
        import json
        data = json.loads(out.read_text(encoding="utf-8"))
        check("导出内容是数组", isinstance(data, list), True)
        check("导出有内容", len(data) >= 1, True)

    # 清空（确认框要绕开，否则 offscreen 下会阻塞）
    panel._confirm_clear = lambda: True
    panel._clear()
    check("清空后无记忆", len(store.list_memories(proj.id)), 0)

    # 确认框拒绝时不清空
    store.upsert_memory(Memory(id="", project_id=proj.id, type="preference",
                               key="k1", value="v"))
    panel._confirm_clear = lambda: False
    panel._clear()
    check("拒绝确认时保留记忆", len(store.list_memories(proj.id)), 1)
    store.close()

print("[2] 桌面端记忆按钮存在")
win = MainWindow()
check("记忆按钮已挪进 Agent 设置", getattr(win, "btn_memory", None) is None, True)
check("Agent 设置入口存在", getattr(win, "btn_agent_settings", None) is not None, True)
check("任务中心存在", getattr(win, "task_center", None) is not None, True)

print("[3] 无持久化时记忆面板给出提示")
orig_info = QMessageBox.information
seen = []
QMessageBox.information = staticmethod(lambda *a, **k: seen.append(a))
try:
    win.open_memory_panel()
finally:
    QMessageBox.information = orig_info
check("给出了提示", len(seen) >= 1, True)

win.tasks.shutdown(wait=False)
win.close()
app.processEvents()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
