"""执行计划卡片 & Agent 流事件回归。

覆盖：
· run_plan / run_agent 在规划后、每步执行前后推 plan/step 事件；
· 计划卡把全部步骤一次列出（含图层名、距离等参数）；
· step 事件把对应行从「执行中」更新成「完成」；
· 失败/完成时卡片收尾。

用法：.venv/Scripts/python.exe tools/test_plan_flow.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

import geopandas as gpd
from shapely.geometry import Polygon

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    from app.agent import Planner, run_agent, run_plan
    from app.engine import Store

    store = Store()
    gdf = gpd.GeoDataFrame(
        {"a": [1]},
        geometry=[Polygon([(104, 30), (104, 30.01), (104.01, 30.01), (104.01, 30)])],
        crs=4326)
    lay = store.add(gdf, "地块")

    print("[1] run_plan 的 step 事件")
    events = []
    steps, lines, _layers = run_plan(
        store, [{"tool": "buffer", "args": {"layer": "地块", "distance": 100, "unit": "m"}}],
        on_event=lambda kind, payload: events.append((kind, payload)))
    check("收到 running + ok 两个事件", [e[1]["status"] for e in events], ["running", "ok"])
    check("事件索引为 0", [e[1]["index"] for e in events], [0, 0])
    check("执行结果 1 步", len(steps), 1)
    check("详情显示图层名", "地块" in events[0][1]["detail"], True)

    print("[2] run_agent 先推 plan 再推 step")
    events = []
    planner = Planner(store)
    res = run_agent(
        store, planner, "对地块做 100 米缓冲区", lay.id, {}, [], use_llm=False,
        on_event=lambda kind, payload: events.append((kind, payload)))
    kinds = [e[0] for e in events]
    check("第一个事件是 plan", kinds[:1], ["plan"])
    check("plan 后跟 step", "step" in kinds, True)
    plan = events[0][1]["plan"]
    check("计划有 1 步", len(plan), 1)
    check("计划带标题", plan[0]["label"], "缓冲区")
    check("计划带参数详情", "距离：100" in plan[0]["detail"], True)
    check("执行结果 1 步", len(res.get("steps") or []), 1)

    print("[3] PlanCard 状态更新")
    from PySide6.QtWidgets import QApplication

    from native.app import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.resize(1000, 760)
    win.show()
    uid = "run-test"
    win._on_agent_event(uid, "plan", {
        "plan": [
            {"tool": "buffer", "label": "缓冲区", "detail": "图层：地块 · 距离：100 米"},
            {"tool": "intersects", "label": "图层相交", "detail": "图层：地块 · 对象：影像"},
        ],
        "source": "llm",
    })
    card = win._live_plans.get(uid)
    check("主窗口创建计划卡", card is not None, True)
    check("计划卡列出 2 行", len(card._rows), 2)
    win._on_agent_event(uid, "step", {"index": 0, "status": "running"})
    check("第 1 行执行中", card._rows[0]["state"].text(), "执行中…")
    win._on_agent_event(uid, "step", {"index": 0, "status": "ok", "elapsed": 1.2})
    check("第 1 行完成", card._rows[0]["state"].text(), "1.2s")
    check("计数 1/2", card._count.text(), "1/2 完成")
    win._on_agent_event(uid, "step", {
        "index": 1, "status": "error", "message": "没有相交要素", "elapsed": 0.3})
    check("第 2 行失败", card._rows[1]["state"].text(), "失败")
    win._finish_live_plan(uid, failed=True)
    check("结束时标记失败", card._count.text(), "1 步失败")
    win.close()
    app.processEvents()

    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
