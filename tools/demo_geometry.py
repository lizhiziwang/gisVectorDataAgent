"""几何算子实跑演示。

拿 samples/ 下的四个图层（行政区 / 地块 / 道路 / 学校）跑一遍几何管线，
验证 app/engine.py 里的几何算子真的能出结果，而不是只停留在「注册表里写着」。

跑法（项目的 .venv 链接已失效，用系统解释器 + venv 的 site-packages）：
    PYTHONPATH="D:/gis-agent/.venv/Lib/site-packages;D:/gis-agent" \
      "D:/zsh/app/py_3.13.1/python.exe" tools/demo_geometry.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd
import pandas as pd

from app.engine import OPERATIONS, Store
from app.samples import ensure_samples


def run(store: Store, tool: str, **args):
    """执行一个算子并打印报告。"""
    t0 = time.time()
    res = OPERATIONS[tool](store, **args)
    dt = (time.time() - t0) * 1000
    print(f"\n▶ {tool}  ({args})")
    print(f"  {res.message}")
    for w in res.warnings:
        print(f"  ⚠ {w}")
    if res.layer is not None:
        m = res.layer.meta()
        print(f"  → 图层「{m['name']}」 {m['count']} 要素 · {m['geomType']} · {m['crs']}")
    print(f"  ⏱ {dt:.0f} ms")
    return res


def main() -> int:
    paths = ensure_samples()
    store = Store(cache_dir=ROOT / "data" / "cache")
    for p in paths:
        store.add(gpd.read_file(p), p.stem)
    print("载入图层：")
    for lay in store.layers.values():
        m = lay.meta(with_invalid=True)
        print(f"  · {m['name']}：{m['count']} 要素 · {m['geomType']} · {m['crs']} "
              f"· 无效几何 {m['invalid']}")

    # 1. 数据体检：先看几何质量
    run(store, "describe", layer="地块")

    # 2. 缓冲区（距离算子，内部自动投影到 CGCS2000 3 度带）
    buf = run(store, "buffer", layer="地块", distance=500, unit="m")

    # 3. 形状指标：面积 / 周长 / 圆度
    run(store, "shape_metrics", layer="地块")

    # 4. 最小外接矩形（最小面积旋转矩形）
    run(store, "min_rect", layer="地块", mode="rotated")

    # 5. 叠加：地块 ∩ 行政区
    run(store, "intersect", layer="地块", other="行政区")

    # 6. 空间连接：学校挂到地块上
    run(store, "sjoin", layer="学校", other="地块", predicate="intersects", how="left")

    # 7. 泰森多边形（点 → 面）
    run(store, "voronoi", layer="学校")

    # 8. 渔网
    run(store, "grid", layer="行政区", cell=1000, unit="m")

    # 9. 几何简化（抽稀节点，保留拓扑）
    run(store, "simplify", layer="地块", tolerance=10, unit="m")

    # 10. 几何修复（无问题时不应凭空建层）
    run(store, "fix_geometry", layer="行政区")

    # 11. 按用地类型统计面积
    run(store, "area_stats", layer=buf.layer.name, group_by="用地类型")

    # 12. 路线缓冲后的道路长度统计（线图层走长度口径）
    run(store, "area_stats", layer="道路", group_by="等级")

    # ---------------------------------------------------------------- 结果抽样
    print("\n" + "=" * 60)
    print("形状指标抽样（地块_形状指标，前 3 行）：")
    cols = ["地块编号", "用地类型", "area_m2", "perim_m", "rect_ratio", "roundness"]
    print(store.get("地块_形状指标").gdf[cols].head(3).to_string(index=False))

    print("\n学校落点（学校_空间连接，带宗地与行政区信息）：")
    sc = store.get("学校_空间连接").gdf
    show = [c for c in ["名称", "类型", "学生数", "地块编号", "用地类型"] if c in sc.columns]
    print(sc[show].to_string(index=False))

    print("\n求交结果（地块 ∩ 行政区）—— 跨界被切开的宗地：")
    ov = store.get("地块_∩_行政区").gdf
    dup = ov["地块编号"].value_counts()
    print(f"  原始宗地 {store.get('地块').meta()['count']} 个 → 求交后 {len(ov)} 个要素，"
          f"其中 {int((dup > 1).sum())} 块宗地跨行政区被切开。")
    # 面积要用投影坐标系下的真实几何面积（属性里的 面积公顷 是切分前的原值）
    import shapely
    area_km2 = shapely.area(ov.to_crs(4544).geometry.values) / 1e6
    grp = pd.DataFrame({"名称": ov["名称"].values, "km2": area_km2}).groupby("名称").agg(
        要素数=("km2", "size"), 面积km2=("km2", "sum")).round(2)
    print(grp.to_string())

    print("\n" + "=" * 60)
    print("全部几何算子执行完毕。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
