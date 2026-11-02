"""桌面端 @ 提及的端到端回归：真实窗口 + 假大模型。

离线：不发真实网络请求。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_CONFIRM_STEPS", "0")
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

app = QApplication.instance() or QApplication([])
fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


def pump(n=60):
    """跑几轮事件循环，让后台任务把结果送回主线程。"""
    for _ in range(n):
        app.processEvents()
        import time
        time.sleep(0.02)


win = MainWindow()
# 造两个图层，名字故意相近（小区 / 小区_合并）
from app.engine import Layer                      # noqa: E402


class FakeLayer:
    def __init__(self, i, n):
        self.id, self.name = i, n

    def meta(self):
        return {"geomType": "Polygon", "count": 10, "crs": "EPSG:4326", "fields": []}


win.store.layers["l1"] = FakeLayer("l1", "小区")
win.store.layers["l2"] = FakeLayer("l2", "小区_合并")

print("[1] @ 精确指定 → 走这一层，不走相近的那层")
seen = {}


class FakePlanner:
    def __init__(self, store):
        self.store = store
        self.last_clarify = None
        self.current_hint = None

    def chit_chat(self, text, has_data=True):
        return ""

    def wants_new_operator(self, text):
        return False

    def llm_plan(self, text, current, cfg, history, memory_context="",
                 clarify_context=None, mention_context=""):
        seen["text"] = text
        seen["current"] = current
        seen["mention"] = mention_context
        return {"plan": [], "reply": "收到"}

    def rule_plan(self, text, current):
        seen["rule_text"] = text
        seen["rule_current"] = current
        return [{"tool": "buffer", "args": {"layer": current, "distance": 500.0,
                                            "unit": "m"}}]

    def gen_operator(self, brief, current, cfg):
        return {"error": "不测"}


win.planner = FakePlanner(win.store)
win.on_send("对 @小区 做 500 米缓冲区")
pump()
check("模型收到的正文没有 @", "@" not in seen.get("text", "@"), True)
check("当前图层换成了 小区", seen.get("current"), "l1")
check("点名说明告诉模型用 小区", "小区" in seen.get("mention", ""), True)

print("[2] @ 名字写错 → 明确告诉用户，不静默操作别的图层")
win.on_send("对 @不存在的图层 做缓冲区")
pump()
texts = [w.toPlainText() for w in win.chat.findChildren(type(win.chat))] \
    if False else []
# 从对话区找提示文字
found = ""
for w in win.chat.findChildren(object):
    try:
        t = w.toPlainText()
    except Exception:
        continue
    if "没有对应" in t or "没有叫" in t:
        found = t
check("有「没有对应图层」的提示", "没有" in found, True)
check("提示里带上了错的名字", "不存在" in found, True)

print("[3] @ 有歧义 → 弹澄清让用户选")
# 「@小」同时匹配 小区 和 小区_合并 → 有歧义，要问用户
win.on_send("对 @小 做缓冲区")
pump()
has_clarify = False
for w in win.chat.findChildren(object):
    try:
        t = w.toPlainText()
    except Exception:
        continue
    if "指的是哪个图层" in t:
        has_clarify = True
check("弹了澄清问题", has_clarify, True)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
