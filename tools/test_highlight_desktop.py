"""桌面端「高亮显示」的端到端回归：真实窗口 + 假大模型。

离线：不发真实网络请求。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

import urllib.request

from PySide6.QtWidgets import QApplication

# ---- 先把配置换成假的（必须在 import native.app 之前）
import app.config as app_config

FAKE_CFG = {
    "base_url": "https://fake.local/v1", "api_key": "fake-key",
    "model": "fake-model", "enabled": True, "api_format": "openai_chat",
    "api_overrides": {},
}
app_config.is_ready = lambda cfg: True
app_config.load_cfg = lambda: dict(FAKE_CFG)
app_config.save_cfg = lambda cfg: None


def _no_network(*a, **kw):
    raise AssertionError("测试中不允许真实网络请求")


urllib.request.urlopen = _no_network

from native.app import MainWindow          # noqa: E402

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


app = QApplication.instance() or QApplication([])
win = MainWindow()
win.show()
app.processEvents()


def _make_layer():
    import geopandas as gpd
    from shapely.geometry import Point
    gdf = gpd.GeoDataFrame(
        {"name": ["A", "B", "C"], "area": [100, 5000, 20000]},
        geometry=[Point(104.0, 30.0), Point(104.1, 30.1), Point(104.2, 30.2)],
        crs="EPSG:4326",
    )
    return win.store.add(gdf, "测试点")


print("[1] 高亮算子能写进 store，桌面端能同步到地图")
lay = _make_layer()
win.refresh_layers()
app.processEvents()

from app.engine import op_highlight          # noqa: E402

op_highlight(win.store, "测试点", expression="area > 1000")
win._sync_highlight_to_map()
app.processEvents()
check_true("地图上有高亮集", win.map.highlight_set_count() > 0)
check("高亮数是 2", win.map.highlight_set_count(), 2)

print("[2] 地图真的把高亮画出来了（按像素查色）")
from PySide6.QtGui import QColor, QImage   # noqa: E402

win.map.set_layers(list(win.store.layers.values()))
win.map.fit_all()
app.processEvents()
img = QImage(win.map.size(), QImage.Format_ARGB32)
img.fill(QColor("#ffffff"))
win.map.render(img)
# 高亮是橙红色系（#d97757 淡化后会和底色混合，所以看的是
# 「红通道明显高于蓝通道」这个特征，而不是某一个固定色值）
orange = 0
for y in range(0, img.height()):
    for x in range(0, img.width()):
        c = QColor(img.pixel(x, y))
        if c.red() - c.blue() > 40 and c.red() > 150:
            orange += 1
check_true("画出了高亮像素", orange > 0)

print("[3] 清除高亮")
win.map.clear_highlight_set()
app.processEvents()
check("清空后没有高亮", win.map.highlight_set_count(), 0)

print("[4] 规则引擎能听懂「高亮显示」")
from app.agent import Planner                # noqa: E402

planner = Planner(win.store)
plan = planner.rule_plan("高亮显示测试点", lay.id)
check("拆出了步骤", len(plan) >= 1, True)
if plan:
    check("用的是 highlight 算子", plan[0]["tool"], "highlight")

print("[5] 带条件的说法也能拆")
plan = planner.rule_plan("高亮显示测试点中面积大于 1000 的要素", lay.id)
check("拆出了步骤", len(plan) >= 1, True)
if plan:
    check("用的是 highlight 算子", plan[0]["tool"], "highlight")
    check("带上了条件", bool(plan[0]["args"].get("expression")), True)

print("[6] 算子出现在大模型的算子表里")
from app.agent import TOOL_SPECS              # noqa: E402

names = [s["name"] for s in TOOL_SPECS]
check_true("TOOL_SPECS 里有 highlight", "highlight" in names)
from app.agent import TOOL_LABEL              # noqa: E402

check("有中文名", TOOL_LABEL.get("highlight"), "高亮显示")

print("[7] 解析失败不再是一句死胡同的话")
from app.agent import _salvage_reply          # noqa: E402

check("能捞出自然语言回复", _salvage_reply("我来帮你高亮这些要素"), "我来帮你高亮这些要素")
check("能从 JSON 里捞出 reply", _salvage_reply('{"reply":"高亮完成"}'), "高亮完成")
check("能从嵌套里捞出 reply", _salvage_reply('{"result":{"reply":"已高亮"}}'), "已高亮")
check("纯 JSON 捞不出", _salvage_reply('{"foo":1}'), "")

print()
print("结论: %s" % ("PASS" if _bad == 0 else "FAIL %s" % _bad))
sys.exit(0 if _bad == 0 else 1)
