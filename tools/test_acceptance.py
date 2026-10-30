"""验收标准回归（设计文档 §8）：跨会话记忆、纠正、重启恢复。

用法：.venv\\Scripts\\python.exe tools\\test_acceptance.py
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

app = QApplication.instance() or QApplication([])

WORKDIR = Path(tempfile.mkdtemp(prefix="gis_acc_"))
DB = WORKDIR / "agent.db"
os.environ["GIS_AGENT_PERSIST"] = "1"
os.environ["GIS_AGENT_DB"] = str(DB)


def make_window() -> MainWindow:
    win = MainWindow()
    win.store.add(gpd.GeoDataFrame(
        {"类型": ["住宅", "商业"], "面积": [100.0, 200.0]},
        geometry=[Polygon([(104.0, 30.5), (104.0, 30.51), (104.01, 30.51), (104.01, 30.5)]),
                  Polygon([(104.02, 30.5), (104.02, 30.51), (104.03, 30.51), (104.03, 30.5)])],
        crs=4326), "地块")
    win.current_layer = next(iter(win.store.layers))
    win.refresh_layers()
    return win


def wait_idle(win, limit=400):
    for _ in range(limit):
        app.processEvents()
        if win.tasks.running_count == 0 and win.tasks.pending_count == 0:
            return True
        time.sleep(0.02)
    return False


print("[1] 偏好跨会话生效")
win = make_window()
check("持久化已开启", win.persist, True)
win.on_send("以后默认用公里")
wait_idle(win)
prefs = win.memory.store.list_memories(win.project_id, "preference")
check("偏好已写入", any(m.key == "default_unit" for m in prefs), True)
check("偏好值正确", next((m.value for m in prefs if m.key == "default_unit"), None), "km")

# 新会话（清空后开新会话）——偏好应该还在
win.clear_all()
check("换了新会话", win.session_id != "", True)
ctx = win.memory.context("统计面积")
check("新会话仍注入偏好", "default_unit" in ctx, True)
check("新会话仍是公里", "km" in ctx, True)

print("[2] 纠正被记住")
win.on_send("我说的不是道路，是地块内的管线")
wait_idle(win)
corr = win.memory.store.list_memories(win.project_id, "correction")
check("纠正已写入", len(corr) >= 1, True)
check("纠正是管线",
      any((m.value or {}).get("term") == "管线" for m in corr
          if isinstance(m.value, dict)), True)

print("[3] 删除记忆后不再注入")
mems = win.memory.store.list_memories(win.project_id, "preference")
if mems:
    win.memory.forget(mems[0].id)
    ctx = win.memory.context("统计面积")
    check("删除后不注入", "default_unit" not in ctx, True)

print("[4] 清空项目记忆不影响图层")
n_layers_before = len(win.store.layers)
win.memory.clear()
check("图层还在", len(win.store.layers), n_layers_before)
check("记忆清空", len(win.memory.store.list_memories(win.project_id)), 0)

print("[5] 重启恢复：会话 / 图层元信息 / 偏好")
# 重新加一个图层，验证图层元信息落库（前面的 clear_all 会逻辑删除图层）
win2_pre = make_window()
check("图层元信息已落库", len(win2_pre.agent_db.list_layers(win2_pre.project_id)) >= 1, True)
layers = win2_pre.agent_db.list_layers(win2_pre.project_id)
live = [l for l in layers if l.deleted_at is None]
check("图层字段已记录", bool(live and live[0].fields), True)
check("图层要素数已记录", bool(live and live[0].feature_count >= 1), True)
win2_pre.tasks.shutdown(wait=False)
win2_pre.close()
app.processEvents()

win2 = MainWindow()
check("持久化仍开启", win2.persist, True)
check("复用了同一会话", win2.session_id, win.session_id)
check("恢复了历史消息", len(win2.history) >= 1, True)
layers = win2.agent_db.list_layers(win2.project_id)
live = [l for l in layers if l.deleted_at is None]
check("重启后图层元信息还在", len(live) >= 1, True)

print("[6] 中断任务：重启后标记 interrupted，不自动重跑")
# 直接往 DB 里塞一个 running 的 run
from app.models import Run
run = win2.agent_db.create_run(win2.session_id, kind="chat")
win2.agent_db.update_run(run.id, status="running")
win2.tasks.shutdown(wait=False)
win2.close()
app.processEvents()

win3 = MainWindow()
runs = win3.agent_db.list_recent_runs(win3.session_id, limit=20)
target = next((r for r in runs if r.id == run.id), None)
check("上次未完成的 run 存在", target is not None, True)
check("标记为 interrupted", target.status if target else None, "interrupted")
check("没有自动重跑", win3.tasks.running_count, 0)

print("[7] 事件可回放")
runs3 = win3.agent_db.list_recent_runs(win3.session_id, limit=20)
with_events = [r for r in runs3 if win3.agent_db.list_events(r.id)]
check("有可回放的 run", len(with_events) >= 1, True)

win3.tasks.shutdown(wait=False)
win3.close()
app.processEvents()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
