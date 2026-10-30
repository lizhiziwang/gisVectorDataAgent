"""生成工具时模型写了 import —— 应该自动回灌重试，而不是甩一句死胡同。"""
import os
import sys

sys.path.insert(0, '.')
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

import json

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


def make_planner(responses):
    """responses: 依次返回给模型的 JSON 字符串列表。"""
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

    seq = list(responses)
    seen = []

    def _fake(cfg, msgs, json_mode, max_tokens=1600, **kw):
        seen.append(list(msgs))
        return seq.pop(0) if seq else seq_last, "stop"

    seq_last = responses[-1]
    p._call_llm = _fake
    return p, seen


CFG = {"base_url": "https://x/v1", "api_key": "k", "model": "m",
       "enabled": True, "api_format": "openai_chat", "api_overrides": {}}

BAD = json.dumps({"name": "conv", "title": "格式转换", "desc": "转格式",
                  "params": {"layer": "图层"},
                  "code": "import geopandas as gpd\ndef op(gdf, args):\n    return gdf, 'ok'\n"},
                 ensure_ascii=False)
GOOD = json.dumps({"name": "conv", "title": "格式转换", "desc": "转格式",
                   "params": {"layer": "图层"},
                   "code": "def op(gdf, args):\n    return gdf.to_crs('EPSG:4326'), '已转换'\n"},
                  ensure_ascii=False)

print("[1] 第一次写 import → 自动回灌重试，第二次通过")
p, seen = make_planner([BAD, GOOD])
out = p.gen_operator("转换矢量文件的格式", "测试点", CFG)
check_true("生成了工具", out.get("spec"))
check("调了几次模型", len(seen), 2)
if len(seen) >= 2:
    retry = seen[-1][-1]["content"]
    check_true("重试提示里点明 import", "import" in retry)
    check_true("重试提示说清怎么改", "已经" in retry or "直接用" in retry)
    check("spec 里是修好的代码", "import" in (out.get("spec") or {}).get("code", ""), False)

print("[2] 反复写 import → 给出能指导下一步的话 + 附上代码")
p, seen = make_planner([BAD])
out = p.gen_operator("转换矢量文件的格式", "测试点", CFG)
err = out.get("error", "")
check_true("有错误说明", bool(err))
check_true("说了怎么改", "去掉 import" in err)
check_true("附上了代码", "```python" in err)
check_true("试了 3 次", len(seen) == 3)
check_true("spec 里还留着代码", bool((out.get("spec") or {}).get("code")))

print("[3] 模型压根没给代码 → 也会重试，最后给出人话")
p, seen = make_planner(["这不是 JSON", "这也不是"])
out = p.gen_operator("转换矢量文件的格式", "测试点", CFG)
err = out.get("error", "")
check_true("有错误说明", bool(err))
check_false("不说「解析不出步骤」", "解析不出" in err)
check_true("试了 3 次", len(seen) == 3)

print("[4] 提示词里已经强调不要写 import")
p, seen = make_planner([GOOD])
p.gen_operator("转换矢量文件的格式", "测试点", CFG)
sysmsg = seen[0][0]["content"]
check_true("提示里提到 import", "import" in sysmsg)
check_true("提示里说库已注入", "已经" in sysmsg or "直接用" in sysmsg)
check_true("提示里提到文件格式转换走导出",
           "导出" in sysmsg)

print()
print("结论: %s" % ("PASS" if _bad == 0 else "FAIL %s" % _bad))
sys.exit(0 if _bad == 0 else 1)
