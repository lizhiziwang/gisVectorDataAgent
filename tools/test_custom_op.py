"""自定义算子：安全校验 / 执行 / 落盘 / 重新装载 的回归测试（不联网）。

用法：python tools/test_custom_op.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geopandas as gpd
from shapely.geometry import Polygon

from app import custom_ops
from app.engine import OPERATIONS, Store

fails = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else want(got)
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


GOOD = '''def op(gdf, args):
    field = args.get("field", "长宽比")
    g = metric(gdf)
    b = g.geometry.bounds
    w = b["maxx"] - b["minx"]
    h = b["maxy"] - b["miny"]
    out = gdf.copy()
    long_side = np.maximum(w, h)
    short_side = np.minimum(w, h).replace(0, np.nan)
    out[field] = (long_side / short_side).round(4).values
    return to_wgs(out, gdf), "已计算长宽比"
'''

print("[1] 安全检查")
for bad, why in [
    ("import os\ndef op(gdf, args):\n    return None, ''", "import"),
    ("def op(gdf, args):\n    while True:\n        pass", "while"),
    ("def op(gdf, args):\n    return gdf.__class__, ''", "dunder"),
    ("def op(gdf, args):\n    return open('x').read(), ''", "open"),
    ("def op(gdf, args):\n    eval('1')\n    return None, ''", "eval"),
]:
    try:
        custom_ops.check_code(bad)
        check(f"应拒绝 {why}", "未拒绝", "拒绝")
    except custom_ops.OperatorError as e:
        check(f"拒绝 {why}", True)

try:
    custom_ops.check_code(GOOD)
    check("合法代码通过校验", True)
except Exception as e:
    check("合法代码通过校验", str(e), "通过")

print("[2] 执行")
gdf = gpd.GeoDataFrame(
    {"name": ["a", "b"]},
    geometry=[Polygon([(0, 0), (0.01, 0), (0.01, 0.02), (0, 0.02)]),
              Polygon([(1, 1), (1.03, 1), (1.03, 1.01), (1, 1.01)])],
    crs=4326)
spec = {"name": "test_ratio", "title": "长宽比", "desc": "测试用",
        "params": {"field": "字段名"}, "code": GOOD}
out, msg = custom_ops.run_op(spec, gdf, {})
check("返回要素数", len(out), 2)
check("新字段已写入", "长宽比" in out.columns, True)
check("长边/短边≈2", float(out["长宽比"].iloc[0]), lambda v: 1.9 < v < 2.1)
check("坐标系保持不变", out.crs.to_epsg(), 4326)

print("[3] 落盘 + 安装 + 重新装载")
spec_saved = custom_ops.save(spec)
check("文件已写入", (custom_ops.OPS_DIR / f"{spec_saved['name']}.json").exists(), True)
custom_ops.install(spec_saved)
check("已注册进 OPERATIONS", spec_saved["name"] in OPERATIONS, True)

st = Store()
st.add(gdf, "地块")
res = OPERATIONS[spec_saved["name"]](st, "地块", field="比例")
check("通过算子接口执行", res.ok, True)
check("生成了新图层", res.layer is not None, True)
check("新图层字段", "比例" in res.layer.gdf.columns, True)

loaded = [s["name"] for s in custom_ops.load_all()]
check("重启后能读回", spec_saved["name"] in loaded, True)

from app.agent import TOOL_LABEL, TOOL_SPECS
check("写进工具表（大模型可见）", spec_saved["name"] in TOOL_LABEL, True)
check("写进算子说明", spec_saved["name"] in {s["name"] for s in TOOL_SPECS}, True)

custom_ops.remove(spec_saved["name"])
OPERATIONS.pop(spec_saved["name"], None)
check("清理", (custom_ops.OPS_DIR / f"{spec_saved['name']}.json").exists(), False)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
