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

print("[8] 「新建工具」的各种说法都要能识别")
from app.agent import Planner

for t in ["新建工具", "写一个工具", "帮我实现一个工具", "新工具", "新算子",
          "自定义工具", "自定义算子", "新增算子", "新建一个工具",
          "把这段代码封装成一个工具", "生成工具：统计每个面内道路长度"]:
    check(f"认得：{t}", Planner.wants_new_operator(t), True)

print("[9] 普通分析指令不能被误判成要造工具")
for t in ["高亮显示小区", "对地块做 500 米缓冲区", "筛选面积大于 10000 的要素",
          "用边界裁剪地块", "统计用地类型面积"]:
    check(f"不误判：{t}", Planner.wants_new_operator(t), False)

print("[10] 算法描述要从「生成工具：xxx」里抠出来")
check("中文短描述",
      Planner.extract_operator_brief("生成算子：算面积"), "算面积")
check("长描述",
      Planner.extract_operator_brief("生成工具：统计每个面内道路的总长度和条数"),
      "统计每个面内道路的总长度和条数")
check("没有冒号就整句",
      Planner.extract_operator_brief("写一个工具"), "写一个工具")

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
