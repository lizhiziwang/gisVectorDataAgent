"""桌面端澄清闭环：模型问 → 界面给可点选项 → 用户点 → 重新规划 → 执行。

用假 LLM（不联网）驱动整个 MainWindow 流程，验证 UI 和 Agent 的接线。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_CONFIRM_STEPS", "0")
os.environ["GIS_AGENT_PERSIST"] = "0"

import geopandas as gpd
from PySide6.QtWidgets import QApplication, QFrame, QPushButton
from shapely.geometry import LineString, Polygon

import app.config as app_config
from app.agent import Planner

# 本测试必须完全离线：
# 1) is_ready 恒为 True —— 让桌面端走「大模型规划」这条路；
# 2) load_cfg 给一个假 key —— Planner.llm_plan 会检查 api_key/base_url 为空就直接返回 None，
#    那样根本进不了模型分支，测试就测不到东西了。
#    真接口调用由下面的 urlopen 兜底挡死（绝不能拿 data/settings.json 里的真 key 发请求）。
app_config.is_ready = lambda cfg: True
app_config.load_cfg = lambda: {"api_key": "fake-key-not-real", "base_url": "http://localhost:1/v1",
                               "model": "fake", "enabled": True}

from native.app import MainWindow

app = QApplication.instance() or QApplication([])
fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


class FakePlanner(Planner):
    """按剧本回放模型回答，并记录收到的提示。"""

    def __init__(self, st, script):
        super().__init__(st)
        self.script = list(script)
        self.systems: list[str] = []
        self.users: list[str] = []

    def _call_llm(self, cfg, msgs, json_mode, max_tokens=1600):
        for m in msgs:
            if m.get("role") == "system":
                self.systems.append(m.get("content", ""))
                break
        self.users.append(msgs[-1].get("content", ""))
        if not self.script:
            return {"__error__": "script exhausted", "__status__": 500}
        obj = self.script.pop(0)
        return json.dumps(obj, ensure_ascii=False), "stop"

    def gen_operator(self, description, current, cfg):
        """本测试不写算子；真调接口会花钱，直接拒绝。"""
        return {"error": "测试里不生成算子"}


# 兜底：任何漏网的真实网络请求都让它炸掉，而不是悄悄打到一个付费接口上。
_real_urlopen = urllib.request.urlopen


def _no_network(*a, **kw):
    raise AssertionError("测试中不允许真实网络请求（会打到付费 LLM 接口）")


urllib.request.urlopen = _no_network


def make_win(script):
    win = MainWindow()
    gdf = gpd.GeoDataFrame(
        {"类型": ["住宅", "商业"]},
        geometry=[Polygon([(0, 0), (0, 0.01), (0.01, 0.01), (0.01, 0)]),
                  Polygon([(0.02, 0), (0.02, 0.01), (0.03, 0.01), (0.03, 0)])],
        crs=4326)
    win.store.add(gdf, "地块")
    win.store.add(gpd.GeoDataFrame({"名称": ["路"]},
                                   geometry=[LineString([(0, 0), (0.03, 0.03)])],
                                   crs=4326), "道路")
    win.refresh_layers()
    win.planner = FakePlanner(win.store, script)
    return win


def wait_idle(win, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        snap = win.tasks.snapshot()
        active = [t for t in snap if t.get("status") in ("queued", "running", "waiting")]
        # 任务状态先变 succeeded、done 事件随后才到；必须等 handler 清掉，
        # 否则会在 `_on_plan_done` 之前检查 _pending_clarify（测试竞态）。
        if not active and not win._task_handlers:
            return True
        time.sleep(0.02)
    return False


print("[1] 模型提问 → 界面出现可点选项")
win = make_win([
    {"plan": [], "clarify": {"question": "要处理哪个图层？", "options": ["地块", "道路"]}},
])
win.on_send("给我做缓冲区")
check("任务跑完了", wait_idle(win), True)
check("记住了待澄清状态", win._pending_clarify is not None, True)
pend = win._pending_clarify or {}
check("澄清里有问题", pend.get("question"), "要处理哪个图层？")
check("澄清里有选项", pend.get("options"), ["地块", "道路"])
check("原始需求也留着", pend.get("origin"), "给我做缓冲区")

print("[2] 界面渲染了选项按钮")
from PySide6.QtWidgets import QPushButton
btns = [b for b in win.chat.findChildren(QPushButton)
        if b.objectName() == "chip" and b.text() in ("地块", "道路")]
check("渲染出 2 个选项按钮", len(btns), 2)

print("[3] 点选项 → 重新规划 → 真的执行了")
# 追加一段剧本：这一轮应当按用户的选择（道路）做缓冲
win.planner.script.append({
    "plan": [{"tool": "buffer", "args": {"layer": "道路", "distance": 100, "unit": "m"}}],
    "reply": "按你的选择对道路做缓冲",
})
target = [b for b in btns if b.text() == "道路"][0]
target.click()
check("第二轮跑完了", wait_idle(win), True)
check("澄清状态已清空", win._pending_clarify is None, True)
# 系统提示里应带上「最初需求 + 问题 + 回答」
sys2 = win.planner.systems[-1] if win.planner.systems else ""
check("提示里带用户回答", "用户的回答" in sys2, True)
check("提示里带原始需求", "给我做缓冲区" in sys2, True)
check("提示里带问题", "要处理哪个图层？" in sys2, True)
# 应当生成了新的图层（道路缓冲）
names = [l.name for l in win.store.layers.values()]
check("生成了缓冲区图层", any("道路" in n and "缓" in n for n in names), True)

print("[4] 用户回答出现在对话里")
msgs = win.chat.msg_layout.count()
check("对话里有新消息", msgs >= 3, True)
# 界面上只显示用户点的那个选项（不是拼给模型的那一长串）
user_bubbles = []
for i in range(win.chat.msg_layout.count()):
    w = win.chat.msg_layout.itemAt(i).widget()
    if w is None:
        continue
    for frame in w.findChildren(QFrame):
        if getattr(frame, "_kind", None) == "user":
            user_bubbles.append(getattr(frame, "_text", ""))
check("界面显示用户的选项", user_bubbles[-1], "道路")
check("界面没显示拼好的上下文", "刚才的问题" not in user_bubbles[-1], True)

print("[5] 大模型说做不到 → 不退回规则引擎")
win2 = make_win([
    {"plan": [], "reply": "现有算子做不了多边形内插值；可以先做 buffer 缓冲区。"},
])
win2.on_send("帮我做多边形内插值")
check("任务跑完了", wait_idle(win2), True)
check("没有偷偷执行规则引擎", len(win2.store.layers), 2)
# 把聊天区里所有气泡文字拼起来，找模型的解释
blob = []
for i in range(win2.chat.msg_layout.count()):
    w = win2.chat.msg_layout.itemAt(i).widget()
    if w is None:
        continue
    for frame in w.findChildren(QFrame):
        t = getattr(frame, "_text", "")
        if t:
            blob.append(t)
text_all = "\n".join(blob)
check("回复里是模型的解释", "多边形内插值" in text_all, True)

win.close()
win2.close()
app.processEvents()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
