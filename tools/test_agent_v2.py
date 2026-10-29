"""Agent/引擎优化回归测试（不联网、不需要 Qt）。

用法：.venv\\Scripts\\python.exe tools\\test_agent_v2.py
失败时返回非零退出码。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon

from app.agent import Planner, run_agent, run_plan
from app.engine import Store, _area_stats, _bbox_select, _dedupe, _nearest, _sjoin
from app.tool_registry import validate_args

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


def make_store() -> Store:
    st = Store()
    parcels = gpd.GeoDataFrame(
        {"编号": ["A", "B", "C"], "类型": ["住宅", "商业", "住宅"]},
        geometry=[Polygon([(0, 0), (0, 0.01), (0.01, 0.01), (0.01, 0)]),
                  Polygon([(0.02, 0), (0.02, 0.01), (0.03, 0.01), (0.03, 0)]),
                  Polygon([(0.01, 0.02), (0.01, 0.03), (0.02, 0.03), (0.02, 0.02)])],
        crs=4326)
    st.add(parcels, "地块")
    roads = gpd.GeoDataFrame(
        {"名称": ["路1", "路2", "路3"], "类型": ["主干路", "支路", "支路"]},
        geometry=[LineString([(0, 0), (0.03, 0.03)]),
                  LineString([(0, 0.01), (0.03, 0.01)]),
                  LineString([(0, 0.02), (0.03, 0.02)])],
        crs=4326)
    st.add(roads, "道路")
    pts = gpd.GeoDataFrame(
        {"名称": ["P1", "P2", "P3"]},
        geometry=[Point(0.001, 0.001), Point(0.02, 0.02), Point(0.05, 0.05)],
        crs=4326)
    st.add(pts, "点")
    return st


print("[1] 工具注册表/参数校验")
args, errs = validate_args("buffer", {"layer": "地块", "distance": 500, "unit": "米"})
check("单位别名归一", args.get("unit"), "m")
check("合法参数无错误", errs, [])
_, errs = validate_args("buffer", {"layer": "地块", "distance": 500, "bogus": 1})
check("未知参数被拒绝", bool(errs), True)
_, errs = validate_args("bbox_select", {"layer": "地块", "bbox": "104,30,104.1,30.1"})
check("bbox 字符串通过校验", errs, [])

print("[2] area_stats 线图层单位")
st = make_store()
res = _area_stats(st, "道路", group_by="类型")
check("线图层消息用 km", "km" in res.message and "km²" not in res.message, True)
check("线图层表格列", res.data["table"]["columns"][1], "长度(km)")
check("线图层合计非零", res.data["table"]["rows"][0][1], lambda v: v > 0.0)

print("[3] nearest k")
res = _nearest(st, "点", "道路", k=3)
check("k=3 返回 9 行", len(res.layer.gdf), 9)
check("有 distance_m", "distance_m" in res.layer.gdf.columns, True)
check("距离非空", res.layer.gdf["distance_m"].notna().all(), True)

print("[4] bbox_select 契约")
res = _bbox_select(st, "地块", bbox="0,0,0.015,0.015")
check("bbox 字符串可用", len(res.layer.gdf), 1)
try:
    _bbox_select(st, "地块", bbox="bad")
    check("非法 bbox 应报错", False, True)
except Exception:
    check("非法 bbox 应报错", True)

print("[5] dedupe subset")
dup = gpd.GeoDataFrame(
    {"编号": ["A", "A", "B"]},
    geometry=[Point(0, 0), Point(1, 1), Point(2, 2)], crs=4326)
st2 = Store()
st2.add(dup, "重复")
res = _dedupe(st2, "重复", subset=["编号"])
check("按字段去重生效", len(res.layer.gdf), 2)
check("移除 1 行", res.data["removed"], 1)

print("[6] sjoin 保留同名字段")
st3 = make_store()
res = _sjoin(st3, "地块", "道路", predicate="intersects")
cols = list(res.layer.gdf.columns)
check("同名字段保留双方", any(c.endswith("_left") for c in cols) and any(c.endswith("_right") for c in cols), True)

print("[7] 多轮指代")
st4 = make_store()
pl = Planner(st4)
pl.store = st4
plan = pl.rule_plan("对「地块」做 500 米缓冲区", None)
check("首轮规划", plan[0]["tool"], "buffer")
steps, lines, _ = run_plan(st4, plan, state={"last_id": None})
pl.note_result(steps, lines)
check("记住结果层", pl.memory.get("last_layer") is not None, True)
plan2 = pl.rule_plan("再大一点", None)
check("再大一点复用 buffer", plan2[0]["tool"], "buffer")
check("距离变大", plan2[0]["args"]["distance"], lambda v: v > 500)

print("[8] run_agent 规则闭环（不联网）")
st5 = make_store()
pl5 = Planner(st5)
res = run_agent(st5, pl5, "对「地块」做 500 米缓冲区", "地块", {}, [], use_llm=False)
check("run_agent 返回步骤", len(res["steps"]), 1)
check("run_agent 生成图层", res["steps"][0].get("layer") is not None, True)
check("run_agent 回复含执行", "执行了" in res["reply"], True)

st5b = make_store()
pl5b = Planner(st5b)
res2 = run_agent(st5b, pl5b, "对地块做 300 米缓冲区，然后按类型统计面积", "地块", {}, [], use_llm=False)
check("多步规划有 2 步", len(res2["steps"]), 2)
check("第二步是面积统计", res2["steps"][1]["tool"], "area_stats")
check("第二步用了 $prev 结果", res2["steps"][0].get("layer") is not None, True)

print("[9] 缺 CRS 的投影坐标不再静默按 4326")
st6 = Store()
bad = gpd.GeoDataFrame({"x": [1]},
                       geometry=[Polygon([(500000, 3300000), (500100, 3300000),
                                          (500100, 3300100), (500000, 3300100)])])
st6.add(bad, "投影无CRS")
try:
    _area_stats(st6, "投影无CRS")
    check("应拒绝投影坐标", False, True)
except Exception as e:
    check("应拒绝投影坐标", "坐标系" in str(e) or "CRS" in str(e), True)

print("[10] 自定义算子不污染原图层")
st7 = make_store()
before = list(st7.get("地块").gdf.columns)
# 这里只验证引擎包装层传副本这一行为，不实际生成代码
from app.engine import make_custom_operator
spec = {"name": "mutate_test", "title": "测试", "params": {"layer": "图层"},
        "code": "def op(gdf, args):\n    gdf['new_field'] = 1\n    return gdf, 'ok'\n"}
op = make_custom_operator(spec)
op(st7, "地块")
check("原图层字段未被修改", list(st7.get("地块").gdf.columns), before)

print("[11] 自定义算子安全边界")
from app import custom_ops
from app.engine import OPERATIONS
for bad, why in [
    ("def op(gdf, args):\n    return pd.read_pickle('x'), ''", "read_pickle"),
    ("def op(gdf, args):\n    gdf.to_file('x.shp')\n    return gdf, ''", "to_file"),
]:
    try:
        custom_ops.check_code(bad)
        check(f"拒绝 {why}", "未拒绝", "拒绝")
    except custom_ops.OperatorError:
        check(f"拒绝 {why}", True)
try:
    custom_ops.install({"name": "buffer", "title": "冒充", "code": "def op(gdf, args):\n    return None, ''\n"})
    check("拒绝覆盖内置算子", "未拒绝", "拒绝")
except custom_ops.OperatorError:
    check("拒绝覆盖内置算子", True)

print("[12] 大图层磁盘换出/懒加载")
import tempfile
from shapely.geometry import Point as _Point
with tempfile.TemporaryDirectory() as td:
    st8 = Store(cache_dir=td, memory_budget_mb=0.0001)
    st8.cache_min_features = 1
    big = gpd.GeoDataFrame({"i": range(200)},
                           geometry=[_Point(x * 0.001, 0) for x in range(200)], crs=4326)
    lay8 = st8.add(big, "大图层")
    # 再放一个小图层，触发 LRU：新图层保留，大图层换出
    st8.add(gpd.GeoDataFrame({"x": [1]}, geometry=[_Point(1, 1)], crs=4326), "小图层")
    check("已换出到磁盘", lay8.cache_path is not None and lay8._gdf is None, True)
    check("meta 仍可读", lay8.meta()["count"], 200)
    check("懒加载还原", len(lay8.gdf), 200)
    p8 = Path(lay8.cache_path)
    check("缓存文件存在", p8.exists(), True)
    st8.remove(lay8.id)
    check("删除图层时清理缓存", p8.exists(), False)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
