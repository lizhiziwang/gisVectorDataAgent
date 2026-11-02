"""桌面端多任务回归：连续两条指令排队执行、任务状态、结果落层。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_CONFIRM_STEPS", "0")
os.environ["GIS_AGENT_PERSIST"] = "0"

import geopandas as gpd
from PySide6.QtWidgets import QApplication
from shapely.geometry import Polygon

fails = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


import app.config as app_config
app_config.is_ready = lambda cfg: False

from native.app import MainWindow

app = QApplication.instance() or QApplication([])
win = MainWindow()
win.store.add(gpd.GeoDataFrame(
    {"类型": ["住宅"]},
    geometry=[Polygon([(104.0, 30.5), (104.0, 30.51), (104.01, 30.51), (104.01, 30.5)])],
    crs=4326), "地块")
win.current_layer = next(iter(win.store.layers))
win.refresh_layers()

before = len(win.store.layers)
print("[1] 连续提交两条指令")
win.on_send("对地块做 100 米缓冲区")
win.on_send("对地块做 200 米缓冲区")
for _ in range(200):
    app.processEvents()
    if win.tasks.running_count == 0 and win.tasks.pending_count == 0:
        break
    time.sleep(0.03)
# 调度器计数归零时 done 事件可能还在 Qt 事件队列里；多泵几轮再断言
for _ in range(10):
    app.processEvents()
    time.sleep(0.01)
snap = win.tasks.snapshot()
# 不看顺序：snapshot 是按创建时间倒序的，两条任务谁先谁后不影响结论。
statuses = sorted(t["status"] for t in snap[:2])
check("两条任务都成功", statuses, ["succeeded", "succeeded"])
check("生成两个新图层", len(win.store.layers) - before, 2)
check("任务处理表已清理", len(win._task_handlers), 0)
check("取消按钮隐藏", win.st_task_cancel.isHidden(), True)

print("[2] 排队任务可取消")
win.on_send("对地块做 300 米缓冲区")
win.on_send("对地块做 400 米缓冲区")
# 第一条正在跑或已结束；取消尚未开始的最后一条
time.sleep(0.05)
app.processEvents()
queued = win.tasks.pending_tasks()
if queued:
    ok = win.tasks.cancel(queued[-1].id)
    check("取消排队任务", ok, True)
else:
    check("取消排队任务（任务已快速完成，跳过）", True, True)
for _ in range(200):
    app.processEvents()
    if win.tasks.running_count == 0 and win.tasks.pending_count == 0:
        break
    time.sleep(0.03)

win.tasks.shutdown(wait=False)
win.close()
app.processEvents()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
