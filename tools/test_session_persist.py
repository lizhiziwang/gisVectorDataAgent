"""桌面端会话持久化回归：重启后恢复消息与 planner 状态。

用法：.venv\\Scripts\\python.exe tools\\test_session_persist.py
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

from app.storage import AgentStore
from native.app import MainWindow

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    os.environ["GIS_AGENT_DB"] = str(Path(td) / "agent.db")
    app = QApplication.instance() or QApplication([])

    print("[1] 第一轮：写入消息")
    w1 = MainWindow()
    check("持久化已开启", w1.persist, True)
    w1.chat.add_user("以后默认用米")
    w1.chat.add_agent("好的")
    w1._remember("以后默认用米", "好的")
    w1.planner.memory["last_tool"] = "buffer"
    w1.planner.memory["last_args"] = {"distance": 500}
    w1._persist_state()
    sid = w1.session_id
    w1.close()
    app.processEvents()
    w1.deleteLater()
    app.processEvents()

    print("[2] 第二轮：恢复消息")
    w2 = MainWindow()
    check("复用同一会话", w2.session_id, sid)
    check("history 恢复最后一条", w2.history[-1]["content"], "好的")
    texts = [x.text() for x in w2.chat.msgs.findChildren(QLabel)
             if x.objectName() in ("userText", "agentText")]
    check("对话面板恢复用户消息", any("以后默认用米" in t for t in texts), True)
    check("planner 状态恢复", w2.planner.memory.get("last_tool"), "buffer")
    w2.close()
    app.processEvents()
    w2.deleteLater()
    app.processEvents()

    print("[3] 数据库内容")
    s = AgentStore(os.environ["GIS_AGENT_DB"])
    msgs = s.list_messages(sid, 20)
    check("消息已落库", len(msgs), lambda n: n >= 2)
    check("有 layer 表", "layers" in s.stats(), True)
    s.close()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
