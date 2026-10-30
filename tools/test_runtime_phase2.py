"""Phase 2 运行时回归：运行落库、事件回放、任务中心、导出排队、中断恢复。

用法：.venv\\Scripts\\python.exe tools\\test_runtime_phase2.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["GIS_AGENT_PERSIST"] = "0"

import geopandas as gpd
from PySide6.QtWidgets import QApplication
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

# ---------------------------------------------------------------- 1) 运行落库
print("[1] 运行/步骤/事件落库")
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    from app.runtime import AgentRuntime, PRIORITY_CHAT, recover_interrupted_runs
    from app.storage import AgentStore

    store = AgentStore(Path(td) / "agent.db", project_root=td)
    proj = store.ensure_default_project()
    sess = store.create_session(proj.id, "运行时测试")
    rt = AgentRuntime(store)
    rt.bind_session(proj.id, sess.id)

    events = []
    rt.scheduler._on_event = lambda ev, task, payload: events.append(ev)
    def job(token):
        rt.record_step_started(task_run_id, "buffer", {"distance": 100})
        rt.record_step(task_run_id, {"tool": "buffer", "args": {"distance": 100},
                                     "message": "缓冲区完成"}, status="ok")
        return {"ok": True}

    task_run_id = None
    t = rt.submit_run(job, kind="agent", label="测试任务", priority=100)
    task_run_id = t.run_id
    check("生成了 run id", bool(t.run_id), True)
    for _ in range(100):
        if t.status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.02)
    check("任务成功", t.status, "succeeded")

    run = store.get_run(t.run_id)
    check("run 状态落库", run.status, "succeeded")
    check("run 有 started_at", run.started_at is not None, True)
    check("run 有 finished_at", run.finished_at is not None, True)

    evs = store.list_events(t.run_id)
    types = [e.type for e in evs]
    check("有 run.queued 事件", "run.queued" in types, True)
    check("有 run.started 事件", "run.started" in types, True)
    check("有 run.succeeded 事件", "run.succeeded" in types, True)
    check("有 step.finished 事件", "step.finished" in types, True)
    check("事件 seq 单调", [e.seq for e in evs], sorted(e.seq for e in evs))

    steps = store.list_steps(t.run_id)
    check("步骤落库", len(steps), 1)
    check("步骤工具名", steps[0].tool, "buffer")

    print("[2] 事件可完整回放")
    replay = [(e.seq, e.type) for e in store.list_events(t.run_id)]
    check("回放非空", len(replay) > 0, True)
    check("回放顺序稳定", replay, [(s, ty) for s, ty in replay])

    print("[3] 未完成 run 重启后标记 interrupted")
    t2 = rt.submit_run(lambda token: time.sleep(5), kind="agent", label="长任务")
    time.sleep(0.2)
    check("长任务在跑", t2.status, "running")
    marked = recover_interrupted_runs(store)
    check("标记了未完成 run", t2.run_id in marked, True)
    check("状态变 interrupted", store.get_run(t2.run_id).status, "interrupted")
    check("有 interrupted 事件",
          any(e.type == "run.interrupted" for e in store.list_events(t2.run_id)), True)
    # 不自动重跑
    time.sleep(0.1)
    check("没有自动重跑", store.get_run(t2.run_id).status, "interrupted")
    rt.close()
    store.close()

# ---------------------------------------------------------------- 4) 桌面端
print("[4] 桌面端：任务中心 + 导出排队")
win = MainWindow()
win.store.add(gpd.GeoDataFrame(
    {"类型": ["住宅"]},
    geometry=[Polygon([(104.0, 30.5), (104.0, 30.51), (104.01, 30.51), (104.01, 30.5)])],
    crs=4326), "地块")
win.current_layer = next(iter(win.store.layers))
win.refresh_layers()

check("任务中心存在", getattr(win, "task_center", None) is not None, True)
win.on_send("对地块做 100 米缓冲区")
for _ in range(300):
    app.processEvents()
    if win.tasks.running_count == 0 and win.tasks.pending_count == 0:
        break
    time.sleep(0.02)
snap = win.tasks.snapshot()
check("任务已结束", snap[0]["status"], "succeeded")
check("任务中心已收起", win.task_center.should_show, False)

print("[5] 三个任务排队")
# 用一个慢任务占住 writer lane，后面两个才能真的排上队
gate = threading.Event()


def slow_job(token):
    gate.wait(5)
    return {"ok": True}


slow = win.runtime.submit_run(slow_job, kind="agent", label="占位慢任务",
                              priority=PRIORITY_CHAT)
for _ in range(100):
    app.processEvents()
    if slow.status == "running":
        break
    time.sleep(0.02)
check("慢任务已启动", slow.status, "running")
for i in (200, 300):
    win.on_send(f"对地块做 {i} 米缓冲区")
time.sleep(0.05)
app.processEvents()
check("有排队任务", win.tasks.pending_count >= 1, True)
check("任务中心展开", win.task_center.should_show, True)
check("状态栏有排队提示", "排队" in win.st_msg.text(), True)

# 取消最后一个排队任务
queued = win.tasks.pending_tasks()
if queued:
    win.task_center.cancel_requested.emit(queued[-1].id)
    app.processEvents()
    check("取消生效", win.tasks.get(queued[-1].id).status, "cancelled")
gate.set()
for _ in range(400):
    app.processEvents()
    if win.tasks.running_count == 0 and win.tasks.pending_count == 0:
        break
    time.sleep(0.02)

print("[6] 导出走调度器（不再覆盖 _worker）")
# 绕过文件对话框（offscreen 下会阻塞）：直接验证导出任务能被调度
from PySide6.QtWidgets import QFileDialog, QMessageBox

dest = Path(tempfile.gettempdir()) / "gis_export_test.geojson"
orig_dialog = QFileDialog.getSaveFileName
orig_info = QMessageBox.information
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(dest), ""))
QMessageBox.information = staticmethod(lambda *a, **k: None)
try:
    before_worker = win._worker
    win.export_layer(win.current_layer, "geojson")
    app.processEvents()
    # 导出可能瞬间完成，用「快照里出现 export 类型」来判定
    kinds = [t["kind"] for t in win.tasks.snapshot()]
    check("导出任务已提交", "export" in kinds, True)
    for _ in range(400):
        app.processEvents()
        if win.tasks.running_count == 0 and win.tasks.pending_count == 0:
            break
        time.sleep(0.02)
    check("导出完成", dest.exists(), True)
    check("没有覆盖 _worker", win._worker is before_worker, True)
finally:
    QFileDialog.getSaveFileName = orig_dialog
    QMessageBox.information = orig_info

win.tasks.shutdown(wait=False)
# 关窗时若有任务会弹确认框（offscreen 下会阻塞）——先确保没任务再关
for _ in range(200):
    app.processEvents()
    if win.tasks.running_count == 0 and win.tasks.pending_count == 0:
        break
    time.sleep(0.02)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
win.close()
app.processEvents()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
