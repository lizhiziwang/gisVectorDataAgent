"""测试 highlight 算子。"""
import os
import sys

sys.path.insert(0, '.')
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

from app.engine import Store, op_highlight

ok = 0
bad = 0


def check(name, got, want):
    global ok, bad
    if got == want:
        print("  OK  %s: %r" % (name, got))
        ok += 1
    else:
        print("  !!  %s: got=%r want=%r" % (name, got, want))
        bad += 1


import geopandas as gpd
from shapely.geometry import Point, Polygon

store = Store()
gdf = gpd.GeoDataFrame(
    {"name": ["A", "B", "C", "D"], "area": [100, 5000, 20000, 300],
     "kind": ["甲", "乙", "甲", "乙"]},
    geometry=[Point(104, 30), Point(104.1, 30.1), Point(104.2, 30.2), Point(104.3, 30.3)],
    crs="EPSG:4326",
)
lay = store.add(gdf, "测试点")

print("[1] 高亮全部")
res = op_highlight(store, "测试点")
check("成功", res.ok, True)
check("高亮数", res.data["count"], 4)
check("store 里有高亮记录", "测试点" in [l.name for l in store.layers.values()] and
      len(store.get_highlight(lay.id)[0]), 4)
check("消息提到高亮", "高亮显示" in res.message, True)

print("[2] 按条件高亮")
res = op_highlight(store, "测试点", expression="area > 1000")
check("高亮数", res.data["count"], 2)
check("store 高亮数", len(store.get_highlight(lay.id)[0]), 2)
check("消息带条件", "area > 1000" in res.message, True)

print("[3] 中文条件")
res = op_highlight(store, "测试点", expression="kind == '甲'")
check("高亮数", res.data["count"], 2)

print("[4] 没有匹配")
res = op_highlight(store, "测试点", expression="area > 999999")
check("高亮数", res.data["count"], 0)
check("提示要检查条件", "检查" in res.message, True)

print("[5] 表达式非法要报错")
try:
    op_highlight(store, "测试点", expression="不存在的字段 > 5")
    check("应该抛异常", False, True)
except ValueError as e:
    check("抛 ValueError", True, True)
    check("错误信息有用", "表达式" in str(e) or "无法执行" in str(e), True)

print("[6] 颜色可以指定")
res = op_highlight(store, "测试点", color="#ff0000")
check("颜色记下了", store.get_highlight(lay.id)[1], "#ff0000")

print("[7] 清除高亮")
store.clear_highlight(lay.id)
check("清空了", len(store.get_highlight(lay.id)[0]), 0)

print("[8] 不产生新图层")
n_before = len(store.layers)
op_highlight(store, "测试点")
check("图层数没变", len(store.layers), n_before)

print()
print("结论: %s" % ("PASS" if bad == 0 else "FAIL %s" % bad))
sys.exit(0 if bad == 0 else 1)
