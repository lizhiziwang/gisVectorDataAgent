"""大模型返回不合格式时的处理：不能只丢一句「解析不出步骤」。"""
import os
import sys

sys.path.insert(0, '.')
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

import json
import urllib.request

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


def check_false(name, got):
    check(name, bool(got), False)


def make_planner(reply_content):
    """造一个只会返回指定内容的大模型。"""
    from app.agent import Planner
    from app.engine import Store
    import geopandas as gpd
    from shapely.geometry import Point

    store = Store()
    gdf = gpd.GeoDataFrame(
        {"name": ["A", "B"], "area": [100, 5000]},
        geometry=[Point(104, 30), Point(104.1, 30.1)], crs="EPSG:4326")
    store.add(gdf, "测试点")
    p = Planner(store)

    def _fake(cfg, msgs, json_mode, max_tokens=1600, **kw):
        return reply_content, "stop"

    p._call_llm = _fake
    return p


CFG = {"base_url": "https://x/v1", "api_key": "k", "model": "m",
       "enabled": True, "api_format": "openai_chat", "api_overrides": {}}

print("[1] 模型说了人话 → 直接把原话当回复")
p = make_planner("我来帮你高亮这些要素。")
out = p.llm_plan("高亮显示", None, CFG, [])
check_false("不再是错误", "error" in out)
check("拿到了原话", out.get("reply"), "我来帮你高亮这些要素。")

print("[2] 模型返回 JSON 但字段名不对 → 能捞出里面的说明")
p = make_planner(json.dumps({"result": {"reply": "高亮完成，共 3 个要素"}},
                            ensure_ascii=False))
out = p.llm_plan("高亮显示", None, CFG, [])
check_false("不再是错误", "error" in out)
check("捞到了说明", out.get("reply"), "高亮完成，共 3 个要素")

print("[3] 模型返回的 JSON 里有未知字段 → 给出能指导下一步的话")
p = make_planner(json.dumps({"foo": "bar", "baz": 1}, ensure_ascii=False))
out = p.llm_plan("高亮显示", None, CFG, [])
err = out.get("error", "")
check_true("有话要说", bool(err or out.get("reply")))
if err:
    check_false("不说「解析不出步骤」", "解析不出" in err)
    check_true("告诉用户下一步", "更具体" in err or "测试连接" in err)

print("[4] 全流程：run_agent 也不该出现「解析不出步骤」")
from app.agent import run_agent               # noqa: E402

p = make_planner(json.dumps({"foo": "bar"}, ensure_ascii=False))
res = run_agent(p.store, p, "高亮显示", None, CFG, [], use_llm=True)
text = " ".join(str(res.get(k) or "") for k in ("reply", "note"))
check_false("回复里没有「解析不出步骤」", "解析不出" in text)
check_true("有内容给用户看", bool(text.strip()))

print("[5] 重试提示要具体，不能只说「输出 JSON」")
p = make_planner("乱七八糟")
seen = []


def _spy(cfg, msgs, json_mode, max_tokens=1600, **kw):
    seen.append(list(msgs))
    return "乱七八糟", "stop"


p._call_llm = _spy
p.llm_plan("高亮显示", None, CFG, [])
check_true("重试了", len(seen) >= 2)
if len(seen) >= 2:
    retry = seen[-1][-1]["content"]
    check_true("提示里有 plan 字段说明", "plan" in retry)
    check_true("提示里有 tool/args 示例", "tool" in retry and "args" in retry)

print()
print("结论: %s" % ("PASS" if _bad == 0 else "FAIL %s" % _bad))
sys.exit(0 if _bad == 0 else 1)
