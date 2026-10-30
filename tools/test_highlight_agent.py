"""端到端：假大模型返回 highlight 计划 → 跑 → 地图上有高亮。"""
import os
import sys

sys.path.insert(0, '.')
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

import urllib.request

from PySide6.QtWidgets import QApplication

import app.config as app_config

FAKE_CFG = {
    "base_url": "https://fake.local/v1", "api_key": "fake-key",
    "model": "fake-model", "enabled": True, "api_format": "openai_chat",
    "api_overrides": {},
}
app_config.is_ready = lambda cfg: True
app_config.load_cfg = lambda: dict(FAKE_CFG)
app_config.save_cfg = lambda cfg: None

_ok = 0
_bad = 0


def check(name, got, want):
    global _ok, _bad
    if got == want:
        print("  OK  %s: %r" % (name, got))
        _ok += 1
    else:
        print("  !!  %s: got=%r want=%r" % (name, got, want))
        _bad += 1


def check_true(name, got):
    check(name, bool(got), True)


# ---- 假大模型：返回一个 highlight 计划
FAKE_REPLY = {
    "plan": [{"tool": "highlight", "args": {"layer": "测试点", "expression": "area > 1000"}}],
    "reply": "已经在地图上高亮了面积大于 1000 的要素。",
}


def _fake_urlopen(req, timeout=None):
    import json
    body = json.loads(req.data.decode())
    payload = {
        "choices": [{"message": {"content": json.dumps(FAKE_REPLY, ensure_ascii=False)},
                     "finish_reason": "stop"}],
        "usage": {"total_tokens": 10},
    }
    import io

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return R(json.dumps(payload).encode())


urllib.request.urlopen = _fake_urlopen

from native.app import MainWindow          # noqa: E402

app = QApplication.instance() or QApplication([])
win = MainWindow()
win.show()
app.processEvents()

import geopandas as gpd                       # noqa: E402
from shapely.geometry import Point            # noqa: E402

gdf = gpd.GeoDataFrame(
    {"name": ["A", "B", "C"], "area": [100, 5000, 20000]},
    geometry=[Point(104.0, 30.0), Point(104.1, 30.1), Point(104.2, 30.2)],
    crs="EPSG:4326",
)
win.store.add(gdf, "测试点")
win.refresh_layers()
app.processEvents()

print("[1] 大模型返回 highlight 计划 → 跑起来")
win.chat.sent.emit("新写一个图层高亮显示工具")
# 聊天面板发的是信号，这里直接调 on_send 走完整链路
win.on_send("新写一个图层高亮显示工具")
app.processEvents()

# 等后台任务完成（假大模型是同步的，但任务在调度器线程里）
import time
for _ in range(60):
    app.processEvents()
    time.sleep(0.05)
    if win.map.highlight_set_count():
        break

check_true("地图上有高亮", win.map.highlight_set_count() > 0)
check("高亮了 2 个要素", win.map.highlight_set_count(), 2)

print("[2] 规则引擎兜底也能听懂")
from app.agent import Planner                # noqa: E402

planner = Planner(win.store)
plan = planner.rule_plan("高亮显示测试点中面积大于 1000 的要素", None)
check("拆出了步骤", len(plan) >= 1, True)
if plan:
    check("用的是 highlight", plan[0]["tool"], "highlight")

print()
print("结论: %s" % ("PASS" if _bad == 0 else "FAIL %s" % _bad))
sys.exit(0 if _bad == 0 else 1)
