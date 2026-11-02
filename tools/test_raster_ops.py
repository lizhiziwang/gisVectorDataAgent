"""通用栅格算子的端到端回归（不联网，不需要界面）。

覆盖：
· 8 个栅格算子都注册进 OPERATIONS / 工具库「栅格」组
· 体检、裁剪（bbox / 面）、重投影、重采样、栅格计算、分区统计、镶嵌、转矢量
· 栅格不能被矢量算子误用（校验期就给出中文错误）
· 规则规划能认出「NDVI / 重投影 / 分区统计 / 裁剪」等常见说法
· `$prev` 能把上一步的栅格结果传给下一步

用法：
    .venv/Scripts/python.exe tools/test_raster_ops.py
"""
import os
import shutil
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []
TMP = tempfile.mkdtemp(prefix="gis_raster_ops_")


def check(name, ok, extra=""):
    print(f"  {'OK ' if ok else '!! '} {name}: {ok!r}" + ("" if ok else f"   {extra}"))
    if not ok:
        FAILS.append(name)


def main() -> int:
    import geopandas as gpd
    import rasterio

    from app.engine import OPERATIONS, Store
    from app.raster import read_info
    from app.tool_registry import REGISTRY, groups

    st = Store(cache_dir=TMP)
    dem = st.add_raster(read_info(os.path.join(ROOT, "samples", "raster", "dem.tif")), "高程")
    img = st.add_raster(read_info(os.path.join(ROOT, "samples", "raster", "image.tif")), "影像")
    admin = st.add(gpd.read_file(os.path.join(ROOT, "samples", "行政区.geojson")), "行政区")

    print("[1] 注册与工具库分组")
    names = ["raster_info", "raster_clip", "raster_reproject", "raster_resample",
             "raster_calc", "zonal_stats", "raster_merge", "raster_to_vector"]
    check("8 个栅格算子都在 OPERATIONS", all(n in OPERATIONS for n in names),
          [n for n in names if n not in OPERATIONS])
    check("都有 ToolSpec", all(n in REGISTRY for n in names))
    check("都在「栅格」组", all(REGISTRY[n].group == "栅格" for n in names))
    check("工具库有「栅格」组", any(g == "栅格" and len(v) == 8 for g, v in groups()))

    print("[2] raster_info：不建图层，返回统计")
    r = OPERATIONS["raster_info"](st, layer=dem.id)
    check("ok", r.ok)
    check("不生成图层", r.layer is None)
    check("统计波段 1", "mean" in r.data.get("stats", {}).get(1, {}))
    check("消息提到波段", "波段" in r.message)
    r = OPERATIONS["describe"](st, dem.id)
    check("通用 describe 也能体检栅格", r.ok and "栅格" in r.message)

    print("[3] raster_clip：bbox 与面图层")
    src_w, src_h = dem.info.width, dem.info.height
    r = OPERATIONS["raster_clip"](st, layer=dem.id, bbox="104.0,30.58,104.05,30.62")
    check("bbox 裁剪 ok", r.ok and r.layer is not None)
    check("尺寸变小", r.layer.info.width < src_w and r.layer.info.height < src_h)
    check("源图层未被改", dem.info.width == src_w and dem.info.height == src_h)
    clipped = r.layer

    r = OPERATIONS["raster_clip"](st, layer=dem.id, mask=admin.id)
    check("按面裁剪 ok", r.ok and r.layer is not None)
    check("按面裁剪在范围内", r.layer.info.width <= src_w and r.layer.info.height <= src_h)

    print("[4] raster_reproject")
    r = OPERATIONS["raster_reproject"](st, layer=dem.id, epsg="3857")
    check("重投影 ok", r.ok and r.layer is not None)
    check("CRS 变成 3857", r.layer.info.crs == "EPSG:3857")
    check("尺寸合理", 0 < r.layer.info.width <= 2000 and 0 < r.layer.info.height <= 2000)

    print("[5] raster_resample")
    r = OPERATIONS["raster_resample"](st, layer=dem.id, scale=0.5)
    check("重采样 ok", r.ok and r.layer is not None)
    check("宽高约减半", abs(r.layer.info.width - src_w / 2) <= 1
          and abs(r.layer.info.height - src_h / 2) <= 1)
    r = OPERATIONS["raster_resample"](st, layer=dem.id, resolution=0.001)
    check("按目标分辨率重采样 ok", r.ok and r.layer is not None)
    check("分辨率变成 0.001", abs(r.layer.info.res[0] - 0.001) < 1e-9,
          r.layer.info.res if r.layer else None)

    print("[6] raster_calc：NDVI")
    expr = "(b3-b2)/(b3+b2)"
    r = OPERATIONS["raster_calc"](st, layer=img.id, expression=expr)
    check("计算 ok", r.ok and r.layer is not None)
    check("输出单波段 float32", r.layer.info.bands == 1 and r.layer.info.dtypes[0] == "float32")
    with rasterio.open(r.layer.info.path) as ds:
        a = ds.read(1)
    valid = a[np.isfinite(a)]
    check("NDVI 在 [-1,1]", valid.size > 0 and valid.min() >= -1.01 and valid.max() <= 1.01,
          f"min={valid.min() if valid.size else None} max={valid.max() if valid.size else None}")

    print("[7] zonal_stats：面内统计")
    r = OPERATIONS["zonal_stats"](st, layer=admin.id, raster=dem.id, band=1,
                                  stats="mean,min,max,count", prefix="r")
    check("分区统计 ok", r.ok and r.layer is not None)
    cols = set(map(str, r.layer.gdf.columns))
    check("新增 4 个字段", {"r_mean", "r_min", "r_max", "r_count"} <= cols)
    check("要素数不变", len(r.layer.gdf) == len(admin.gdf))
    check("均值非空", int(r.layer.gdf["r_count"].sum()) > 0)

    print("[8] raster_merge：同 CRS/同 dtype")
    r = OPERATIONS["raster_merge"](st, layers=[dem.id, clipped.id])
    check("镶嵌 ok", r.ok and r.layer is not None)
    check("覆盖原范围", r.layer.info.width >= dem.info.width and r.layer.info.height >= dem.info.height)

    print("[9] raster_to_vector：polygonize")
    r = OPERATIONS["raster_to_vector"](st, layer=img.id, band=1, scale=0.2)
    check("转矢量 ok", r.ok and r.layer is not None)
    check("有面要素", len(r.layer.gdf) > 0 and "value" in r.layer.gdf.columns)
    check("CRS 保留", r.layer.gdf.crs is not None)

    print("[10] 误用保护：栅格不能跑矢量算子")
    from app.agent import Planner
    p = Planner(st)
    errs = p.validate_plan([{"tool": "buffer", "args": {"layer": dem.id, "distance": 10}}])
    check("buffer 栅格被拦截", bool(errs) and "栅格" in errs[0], errs)
    errs = p.validate_plan([{"tool": "raster_calc",
                             "args": {"layer": dem.id, "expression": "b4-b3"}}])
    check("引用不存在的波段由算子报错", not errs)  # 参数层不判断波段，执行期报
    try:
        OPERATIONS["raster_calc"](st, layer=dem.id, expression="b4-b3")
        check("b4 越界应报错", False)
    except Exception as e:  # noqa: BLE001
        check("b4 越界报中文错", "波段" in str(e), str(e))

    print("[11] 规则规划与 $prev")
    for text, tool in [
        ("看一下影像", "raster_info"),
        ("把影像重投影到 EPSG:3857", "raster_reproject"),
        ("把影像重采样到一半", "raster_resample"),
        ("计算影像的 NDVI", "raster_calc"),
        ("用行政区裁剪影像", "raster_clip"),
        ("统计影像在行政区内的均值", "zonal_stats"),
        ("把影像转成矢量", "raster_to_vector"),
        ("把影像和高程镶嵌成一幅", "raster_merge"),
    ]:
        plan = p.rule_plan(text, None)
        got = plan[0]["tool"] if plan else None
        check(f"规则：{text}", got == tool, f"得到 {got}")

    plan = [
        {"tool": "raster_clip", "args": {"layer": dem.id,
                                         "bbox": "104.0,30.58,104.06,30.64"}},
        {"tool": "raster_resample", "args": {"layer": "$prev", "scale": 0.5}},
    ]
    errs = p.validate_plan([dict(s) for s in plan])
    check("$prev 计划校验通过", not errs, errs)
    steps, _lines, new_layers = __import__("app.agent", fromlist=["run_plan"]).run_plan(st, plan)
    check("两步都成功", [s["status"] for s in steps] == ["ok", "ok"],
          [(s["tool"], s["status"], s["message"][:60]) for s in steps])
    check("产出两个图层", len(new_layers) == 2)

    st.clear()
    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
