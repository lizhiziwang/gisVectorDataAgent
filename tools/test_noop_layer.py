"""验证：零变化的修复/去重不建新图层，且 $prev 链路不断。"""
import sys
sys.path.insert(0, r"D:/gis-agent")
import geopandas as gpd
from shapely.geometry import Polygon
from app.engine import Store, _fix, _dedupe
from app.agent import run_plan

store = Store()
g = gpd.GeoDataFrame({"a": [1, 2]},
                     geometry=[Polygon([(0,0),(1,0),(1,1),(0,1)]),
                               Polygon([(2,2),(3,2),(3,3),(2,3)])], crs=4326)
lay = store.add(g, "地块")

n0 = len(store.layers)
r = _fix(store, lay.id)
print("修复(零变化): ok=", r.ok, "layer=", r.layer, "|", r.message)
assert r.layer is None and len(store.layers) == n0

r2 = _dedupe(store, lay.id)
print("去重(零重复): ok=", r2.ok, "layer=", r2.layer, "|", r2.message)
assert r2.layer is None and len(store.layers) == n0

# 有变化的修复仍然建层
bad = Polygon([(0,0),(2,2),(2,0),(0,2)])   # 自相交
store.add(gpd.GeoDataFrame({"a":[1]}, geometry=[bad], crs=4326), "坏面")
badlay = [l for l in store.layers.values() if l.name == "坏面"][0]
r3 = _fix(store, badlay.id)
print("修复(有变化): ok=", r3.ok, "layer=", r3.layer.name if r3.layer else None, "|", r3.message)
assert r3.layer is not None

# $prev 兜底：修复零变化之后，下一步还能用 $prev
steps, lines, new = run_plan(store, [
    {"tool": "fix_geometry", "args": {"layer": lay.id}},
    {"tool": "buffer", "args": {"layer": "$prev", "distance": 10}},
])
print("链路:", [(s["tool"], s["status"]) for s in steps])
assert steps[0]["status"] == "ok" and steps[0].get("layer") is None
assert steps[1]["status"] == "ok" and steps[1].get("layer") is not None
print("结论: PASS")
