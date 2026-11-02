"""逐步确认回归：run_plan 的确认/跳过/取消/修订，以及确认弹窗。

用法：.venv/Scripts/python.exe tools/test_step_confirm.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")
os.environ["GIS_AGENT_CONFIRM_STEPS"] = "1"

import geopandas as gpd
from shapely.geometry import Point

from app.agent import run_plan
from app.engine import Store

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def make_store():
    st = Store()
    g = gpd.GeoDataFrame(
        {"县": ["A", "B"]},
        geometry=[Point(104.0, 30.0), Point(104.2, 30.2)], crs=4326)
    st.add(g, "点位")
    return st


def main() -> int:
    print("[1] 用户确认并修改参数")
    st = make_store()
    seen = []

    def revise(info):
        seen.append(info["tool"])
        return ("run", {"tool": "buffer", "args": {"layer": "点位", "distance": 800,
                                                   "unit": "m"}})

    steps, lines, _ = run_plan(
        st, [{"tool": "buffer", "args": {"layer": "点位", "distance": 100}}],
        state={}, confirm_step=revise)
    check("确认回调被调用", seen, ["buffer"])
    check("执行成功", steps[0]["status"], "ok")
    check("参数被改写成 800", steps[0]["args"].get("distance"), 800)

    print("[2] 跳过一步")
    st = make_store()
    calls = []

    def skip(info):
        calls.append(info["index"])
        return ("skip", None)

    steps, _, _ = run_plan(
        st, [{"tool": "buffer", "args": {"layer": "点位", "distance": 100}}],
        state={}, confirm_step=skip)
    check("状态为 skipped", steps[0]["status"], "skipped")
    check("没有生成图层", st.get(steps[0].get("layer") or "") if steps[0].get("layer") else None,
          None)

    print("[3] 取消任务")
    st = make_store()
    state = {}
    steps, _, _ = run_plan(
        st, [{"tool": "buffer", "args": {"layer": "点位", "distance": 100}}],
        state=state, confirm_step=lambda info: ("cancel", None))
    check("状态为 cancelled", steps[0]["status"], "cancelled")
    check("state 标记取消", state.get("cancelled"), True)

    print("[4] 修改失败")
    st = make_store()
    state = {}
    steps, _, _ = run_plan(
        st, [{"tool": "buffer", "args": {"layer": "点位", "distance": 100}}],
        state=state, confirm_step=lambda info: ("error", "模型改不出来"))
    check("状态为 error", steps[0]["status"], "error")
    check("错误信息", steps[0]["message"], "模型改不出来")

    print("[5] 确认弹窗")
    from PySide6.QtWidgets import QApplication, QPushButton

    from native.stepconfirm import StepConfirmDialog, StepConfirmGate

    app = QApplication.instance() or QApplication([])
    gate = StepConfirmGate()
    gate.resolve("run", "改成 800 米")
    check("Gate 等待成功", gate.wait(0.2), True)
    check("Gate 动作", gate.action, "run")
    check("Gate 补充要求", gate.instruction, "改成 800 米")

    dlg = StepConfirmDialog({
        "index": 1, "total": 2, "tool": "buffer", "label": "缓冲区",
        "detail": "对「点位」做 100 米缓冲区",
        "args": {"layer": "点位", "distance": 100, "unit": "m"},
    })
    dlg.input.setPlainText("改成 800 米")
    btns = {b.text(): b for b in dlg.findChildren(QPushButton)}
    check("有执行按钮", "执行这一步" in btns, True)
    check("有跳过按钮", "跳过这一步" in btns, True)
    check("有取消按钮", "取消任务" in btns, True)
    btns["执行这一步"].click()
    check("确认动作为 run", dlg.action, "run")
    check("弹窗返回补充要求", dlg.instruction(), "改成 800 米")
    dlg.deleteLater()
    app.processEvents()

    print("[6] 计划卡状态")
    from native.chat import PlanCard

    card = PlanCard([{"tool": "buffer", "label": "缓冲区",
                      "detail": "对点位做缓冲区"}])
    card.update_step(0, {"status": "confirm"})
    check("显示待确认", card._rows[0]["state"].text(), "待确认")
    card.update_step(0, {"status": "skipped", "message": "用户跳过"})
    check("显示已跳过", card._rows[0]["state"].text(), "已跳过")
    card.deleteLater()
    app.processEvents()

    print("[7] 模型改写单步")
    from app.agent import Planner

    st3 = make_store()
    pl = Planner(st3)
    pl._call_llm = lambda *a, **k: (
        '{"step":{"tool":"buffer","args":{"layer":"点位","distance":800,"unit":"m"}},'
        '"reply":"距离改大"}', "stop")
    new = pl.revise_step(
        "对点位做缓冲区",
        {"step": {"tool": "buffer", "args": {"layer": "点位", "distance": 100}}},
        "改成 800 米", None,
        {"base_url": "https://x/v1", "api_key": "k", "api_format": "openai_chat"},
        [])
    check("模型改出距离 800", (new or {}).get("args", {}).get("distance"), 800)

    print("[8] 主窗口确认闭环")
    import time

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QPushButton

    from native.app import MainWindow

    win = MainWindow()
    win.resize(1000, 760)
    win.show()
    app.processEvents()
    # 主窗口构造时会自己建 map；测试只需要一个可被 planner 识别的点图层
    st2 = win.store
    g = gpd.GeoDataFrame(
        {"县": ["A", "B"]},
        geometry=[Point(104.0, 30.0), Point(104.2, 30.2)], crs=4326)
    lay = st2.add(g, "点位")
    win.current_layer = lay.id
    win.refresh_layers(fit=True)
    app.processEvents()
    clicked = {"n": 0}

    def auto_click():
        dlg = win._confirm_dialog
        if dlg is None:
            QTimer.singleShot(80, auto_click)
            return
        for b in dlg.findChildren(QPushButton):
            if b.text() == "执行这一步":
                clicked["n"] += 1
                b.click()
                return
        QTimer.singleShot(80, auto_click)

    QTimer.singleShot(200, auto_click)
    win.on_send("对点位做 100 米缓冲区")
    deadline = time.time() + 30
    while time.time() < deadline:
        app.processEvents()
        if clicked["n"] and win.tasks.running_count == 0 and win._pending_confirm is None:
            break
        time.sleep(0.02)
    app.processEvents()
    check("确实弹出了确认框并执行", clicked["n"], 1)
    check("任务已结束", win.tasks.running_count, 0)
    check("确认框已关闭", win._pending_confirm, None)
    check("生成了缓冲区图层", len(win.store.layers) >= 2, True)
    win.close()
    win.deleteLater()
    app.processEvents()

    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
