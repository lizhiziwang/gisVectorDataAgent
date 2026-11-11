"""重启后恢复澄清卡 / 工具计划卡，而不是只剩一行文字。

用法：.venv\\Scripts\\python.exe tools\\test_chat_persist_cards.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["GIS_AGENT_PERSIST"] = "1"

from PySide6.QtWidgets import QApplication, QLabel

from native.app import MainWindow
from native.chat import ClarifyCard, PlanCard, StepCard

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    os.environ["GIS_AGENT_DB"] = str(Path(td) / "agent.db")
    app = QApplication.instance() or QApplication([])

    print("[1] 第一轮：澄清卡 + 计划卡")
    w1 = MainWindow()
    w1._show_clarify(
        {"question": "要在哪个城市定位？", "options": ["杭州", "西湖"]},
        "对道路图层做地名坐标定位", {"source": "llm"})
    app.processEvents()
    pending = w1._pending_clarify or {}
    check("澄清已落库", bool(pending.get("message_id")), True)
    # 模拟点选，但不真的启动第二轮任务
    old_on_send = w1.on_send
    w1.on_send = lambda *args, **kwargs: None
    w1._on_clarify_picked("杭州")
    w1.on_send = old_on_send
    app.processEvents()

    uid = "run-persist-test"
    w1._on_agent_event(uid, "plan", {"source": "llm", "plan": [
        {"label": "地名坐标定位", "tool": "geocode"},
    ]})
    w1._on_agent_event(uid, "step", {
        "index": 0, "label": "地名坐标定位", "tool": "geocode",
        "status": "error", "message": "找不到图层 [None]",
    })
    w1._on_plan_done({
        "_run_uid": uid, "user": "对道路图层做地名坐标定位",
        "steps": [{"label": "地名坐标定位", "tool": "geocode",
                   "status": "error", "message": "找不到图层 [None]"}],
        "lines": ["地名坐标定位失败"],
        "reply": "执行了 1 步，其中 1 步失败：",
    })
    sid = w1.session_id
    w1.close()
    app.processEvents()
    w1.deleteLater()
    app.processEvents()

    print("[2] 第二轮：恢复两张卡")
    w2 = MainWindow()
    app.processEvents()
    check("复用同一会话", w2.session_id, sid)
    cards = w2.chat.msgs.findChildren(ClarifyCard)
    check("恢复澄清卡", len(cards), 1)
    if cards:
        check("澄清卡已确认", cards[0].chosen, "杭州")
        check("澄清卡不再等待", cards[0].awaiting, False)
        check("选中行标记", cards[0]._rows[0].num_lb.text(), "✓")
    plans = w2.chat.msgs.findChildren(PlanCard)
    check("恢复计划卡", len(plans), 1)
    if plans:
        check("计划有 1 步", len(plans[0]._rows), 1)
        check("失败状态恢复", plans[0]._rows[0]["step"].get("status"), "error")
        check("失败详情恢复", "找不到图层" in plans[0]._rows[0]["detail"].text(), True)
    step_cards = w2.chat.msgs.findChildren(StepCard)
    check("恢复结果步骤卡", len(step_cards), 1)
    texts = [x.text() for x in w2.chat.msgs.findChildren(QLabel)
             if x.objectName() in ("agentText",)]
    check("题干没有重复成普通气泡", any("要在哪个城市定位" in t for t in texts), False)
    check("结果文字恢复", any("执行了 1 步" in t for t in texts), True)
    w2.close()
    app.processEvents()
    w2.deleteLater()
    app.processEvents()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
