"""整机冒烟：真实（offscreen 之外）启动路径 + 持久化 + 任务中心。

验证 MainWindow 在「有 DB」的情况下能正常构造、跑一轮对话、写库。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

WORKDIR = Path(tempfile.mkdtemp(prefix="gis_smoke_"))
os.environ["GIS_AGENT_PERSIST"] = "1"
os.environ["GIS_AGENT_DB"] = str(WORKDIR / "agent.db")

import geopandas as gpd
from PySide6.QtWidgets import QApplication
from shapely.geometry import Polygon

import app.config as app_config
app_config.is_ready = lambda cfg: False

from native.app import MainWindow

app = QApplication.instance() or QApplication([])
fails = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


print("[1] 启动")
win = MainWindow()
check("持久化开启", win.persist, True)
check("有 DB", win.agent_db is not None, True)
check("有记忆管理器", win.memory is not None, True)
check("有运行时", win.runtime is not None, True)
check("会话已绑定", win.runtime.session_id, win.session_id)
check("项目已绑定", win.runtime.project_id, win.project_id)

print("[2] 跑一轮")
win.store.add(gpd.GeoDataFrame(
    {"类型": ["住宅"], "面积": [100.0]},
    geometry=[Polygon([(104.0, 30.5), (104.0, 30.51), (104.01, 30.51), (104.01, 30.5)])],
    crs=4326), "地块")
win.current_layer = next(iter(win.store.layers))
win.refresh_layers()
win.on_send("对地块做 100 米缓冲区")
for _ in range(400):
    app.processEvents()
    if win.tasks.running_count == 0 and win.tasks.pending_count == 0:
        break
    time.sleep(0.02)
check("任务完成", win.tasks.snapshot()[0]["status"], "succeeded")
check("生成了图层", len(win.store.layers), 2)

print("[3] 落库检查")
runs = win.agent_db.list_recent_runs(win.session_id, limit=10)
check("有运行记录", len(runs) >= 1, True)
check("运行成功", runs[0].status, "succeeded")
evs = win.agent_db.list_events(runs[0].id)
check("有事件", len(evs) >= 3, True)
steps = win.agent_db.list_steps(runs[0].id)
check("有步骤", len(steps) >= 1, True)
layers = win.agent_db.list_layers(win.project_id)
check("图层入库", len([l for l in layers if l.deleted_at is None]) >= 1, True)
msgs = win.agent_db.list_messages(win.session_id)
check("消息入库", len(msgs) >= 1, True)

print("[4] 记忆写入")
mems = win.agent_db.list_memories(win.project_id)
check("有记忆", len(mems) >= 1, True)

print("[5] 关闭")
win.tasks.shutdown(wait=False)
win.close()
app.processEvents()
check("干净退出", True, True)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
