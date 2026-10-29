"""Agent 闭环（规划/校验/修复/澄清）的离线测试：用假 LLM，不联网。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import geopandas as gpd
from shapely.geometry import LineString, Polygon

from app.agent import Planner, run_agent
from app.engine import Store

fails = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


def store() -> Store:
    st = Store()
    parcels = gpd.GeoDataFrame(
        {"类型": ["住宅", "商业"]},
        geometry=[Polygon([(0, 0), (0, 0.01), (0.01, 0.01), (0.01, 0)]),
                  Polygon([(0.02, 0), (0.02, 0.01), (0.03, 0.01), (0.03, 0)])],
        crs=4326)
    st.add(parcels, "地块")
    roads = gpd.GeoDataFrame({"名称": ["路"]},
                             geometry=[LineString([(0, 0), (0.03, 0.03)])], crs=4326)
    st.add(roads, "道路")
    return st


class FakePlanner(Planner):
    def __init__(self, st, responses):
        super().__init__(st)
        self.responses = list(responses)
        self.calls = []

    def _call_llm(self, cfg, msgs, json_mode, max_tokens=1600):
        self.calls.append(msgs[-1].get("content", "")[:80])
        if not self.responses:
            return {"__error__": "no more fake responses", "__status__": 500}
        obj = self.responses.pop(0)
        return json.dumps(obj, ensure_ascii=False), "stop"


CFG = {"api_key": "x", "base_url": "http://example.invalid/v1",
       "model": "fake", "enabled": True}


def run_case(name, responses, text="对地块做缓冲", expected=None):
    st = store()
    pl = FakePlanner(st, responses)
    res = run_agent(st, pl, text, "地块", CFG, [], use_llm=True)
    print(f"[{name}] reply={res.get('reply','')[:60]!r} steps={len(res.get('steps') or [])}")
    if expected:
        expected(res)
    return res


print("[1] 大模型只给自然语言回复时不能丢")
r = run_case("chat", [{"plan": [], "reply": "这个图层适合做缓冲区分析。"}],
             expected=lambda x: check("保留 LLM 回复", x["reply"], "这个图层适合做缓冲区分析。"))
check("planner 标记为 llm", r["planner"], "llm")

print("[2] 澄清")
r = run_case("clarify", [{"plan": [], "clarify": {"question": "要处理哪个图层？",
                                                  "options": ["地块", "道路"]}}],
             expected=lambda x: check("澄清选项", x["clarify"]["options"], ["地块", "道路"]))
check("澄清回复含选项", "地块" in r["reply"], True)

print("[3] 计划校验失败 -> LLM 修复")
r = run_case("validate_repair", [
    {"plan": [{"tool": "buffer", "args": {"layer": "地块"}}], "reply": ""},
    {"plan": [{"tool": "buffer", "args": {"layer": "地块", "distance": 100, "unit": "m"}}],
     "reply": "补上距离"},
], expected=lambda x: check("修复后执行 buffer", x["steps"][0]["tool"], "buffer"))
check("有成功图层", r["steps"][0].get("layer") is not None, True)

print("[4] 执行失败 -> LLM 修复后继续")
r = run_case("exec_repair", [
    {"plan": [{"tool": "filter", "args": {"layer": "地块", "expression": "不存在的字段 > 1"}}],
     "reply": ""},
    {"plan": [{"tool": "buffer", "args": {"layer": "地块", "distance": 200, "unit": "m"}}],
     "reply": "改用缓冲区"},
], expected=lambda x: check("失败后继续执行", any(s["tool"] == "buffer" for s in x["steps"]), True))
check("失败步骤保留", any(s["status"] == "error" for s in r["steps"]), True)

print("[5] 预检拒绝未知算子（无副作用）")
st = store()
pl = FakePlanner(st, [
    {"plan": [{"tool": "not_a_tool", "args": {}}], "reply": ""},
    {"plan": [], "reply": ""},
])
before = len(st.layers)
r = run_agent(st, pl, "做点奇怪的事", "地块", CFG, [], use_llm=True)
check("没有新建图层", len(st.layers), before)
check("回复说明不能执行", "不能直接执行" in r["reply"] or "不支持" in r["reply"], True)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
