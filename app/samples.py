"""示例数据生成器：成都某片区的行政区 / 地块 / 道路 / 学校，共 4 个图层。

首次调用时生成 GeoJSON 到 samples/ 目录，之后直接复用，保证演示可复现。
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon, mapping, shape

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
SAMPLES.mkdir(parents=True, exist_ok=True)

# 成都市中心以南一块约 9km × 7km 的区域
LON0, LAT0 = 104.030, 30.585
DX, DY = 0.092, 0.062

SAMPLE_FILES = ["行政区.geojson", "地块.geojson", "道路.geojson", "学校.geojson"]

DISTRICTS = ["高新区", "锦江区", "武侯区", "双流区"]
LANDUSE = ["居住用地", "商业用地", "工业用地", "绿地广场", "公共服务"]


def _fc(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": features}


def _f(geom, props: dict) -> dict:
    return {"type": "Feature", "geometry": mapping(geom), "properties": props}


def _param_poly(x0, y0, x1, y1, rng, jitter=0.0012) -> Polygon:
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    outer = []
    for i, (x, y) in enumerate(pts):
        dx = rng.uniform(-jitter, jitter)
        dy = rng.uniform(-jitter, jitter)
        outer.append((x + dx, y + dy))
    p = Polygon(outer)
    return p if p.is_valid else p.buffer(0)


def build_districts(rng) -> list[dict]:
    """2x2 分区，使用共享角点的抖动格网，保证相邻边界严丝合缝（无缝隙/无重叠）。"""
    feats = []
    cols, rows = 2, 2
    pop = {"高新区": 96.4, "锦江区": 74.2, "武侯区": 128.7, "双流区": 152.3}
    # 3x3 角点格网
    lat_pts = [[(LON0 + DX / cols * i + rng.uniform(-0.0018, 0.0018),
                 LAT0 + DY / rows * j + rng.uniform(-0.0014, 0.0014)) for j in range(rows + 1)]
               for i in range(cols + 1)]
    for i in range(cols):
        for j in range(rows):
            name = DISTRICTS[i * rows + j]
            ring = [lat_pts[i][j], lat_pts[i + 1][j], lat_pts[i + 1][j + 1], lat_pts[i][j + 1]]
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            feats.append(_f(poly, {
                "名称": name,
                "等级": "市辖区",
                "人口万人": pop[name],
                "代码": f"5101{i}{j}",
            }))
    return feats


def _split_rect(rect, rng, depth=4, stop_area=1.1e-5):
    """把矩形递归二分（交替沿长边切），得到接近方形的宗地。"""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    if depth <= 0 or w * h < stop_area:
        return [rect]
    vertical = w >= h
    if rng.random() < 0.22:
        vertical = not vertical
    t = rng.uniform(0.38, 0.62)
    if vertical and w > 0:
        xm = x0 + w * t
        return _split_rect((x0, y0, xm, y1), rng, depth - 1, stop_area) + \
               _split_rect((xm, y0, x1, y1), rng, depth - 1, stop_area)
    if h > 0:
        ym = y0 + h * t
        return _split_rect((x0, y0, x1, ym), rng, depth - 1, stop_area) + \
               _split_rect((x0, ym, x1, y1), rng, depth - 1, stop_area)
    return [rect]


def build_parcels(rng) -> list[dict]:
    feats = []
    bx, by = 12, 8
    n = 1
    for i in range(bx):
        for j in range(by):
            x0 = LON0 + 0.002 + (DX - 0.006) / bx * i
            y0 = LAT0 + 0.002 + (DY - 0.006) / by * j
            w = (DX - 0.006) / bx
            h = (DY - 0.006) / by
            cells = _split_rect((x0, y0, x0 + w, y0 + h), rng)
            for (cx0, cy0, cx1, cy1) in cells:
                pad = 0.00009
                poly = _param_poly(cx0 + pad, cy0 + pad, cx1 - pad, cy1 - pad, rng, 0.00004)
                if poly.is_empty or poly.area <= 0:
                    continue
                use = rng.choice(LANDUSE)
                area_ha = round(poly.area * (111320 ** 2) * 0.87 / 10000, 2)  # 粗略换算公顷
                feats.append(_f(poly, {
                    "地块编号": f"D{n:04d}",
                    "用地类型": use,
                    "面积公顷": area_ha,
                    "容积率": round(rng.uniform(0.6, 4.2), 2),
                    "建筑密度": round(rng.uniform(0.15, 0.45), 2),
                }))
                n += 1
    return feats


def build_roads(rng) -> list[dict]:
    feats = []
    names = [
        ("天府大道", "快速路"), ("剑南大道", "主干路"), ("益州大道", "主干路"),
        ("锦城大道", "次干路"), ("府城大道", "次干路"), ("盛安街", "支路"),
    ]
    # 纵向 3 条
    for k, (name, grade) in enumerate(names[:3]):
        x = LON0 + DX * (0.22 + 0.28 * k) + rng.uniform(-0.004, 0.004)
        pts = [(x + rng.uniform(-0.002, 0.002), LAT0 + DY * t / 6) for t in range(7)]
        feats.append(_f(LineString(pts), {"道路名称": name, "等级": grade, "车道数": 8 if grade == "快速路" else 6}))
    # 横向 3 条
    for k, (name, grade) in enumerate(names[3:]):
        y = LAT0 + DY * (0.2 + 0.3 * k) + rng.uniform(-0.004, 0.004)
        pts = [(LON0 + DX * t / 6, y + rng.uniform(-0.0015, 0.0015)) for t in range(7)]
        feats.append(_f(LineString(pts), {"道路名称": name, "等级": grade, "车道数": 4}))
    return feats


def build_schools(rng, parcels: list[dict]) -> list[dict]:
    """学校点落在随机宗地内部，保证与地块图层有真实的空间关系。"""
    feats = []
    kinds = ["小学", "中学", "九年一贯制"]
    picks = rng.sample(range(len(parcels)), min(12, len(parcels)))
    for idx in picks:
        poly = shape(parcels[idx]["geometry"])
        pt = poly.representative_point()
        kind = rng.choice(kinds)
        feats.append(_f(pt, {
            "名称": f"{rng.choice(['高新','锦江','武侯','双流'])}{rng.choice(['第一','第二','实验','中心'])}{kind}",
            "类型": kind,
            "学生数": rng.randint(320, 2400),
            "教职工": rng.randint(35, 180),
        }))
    return feats


def ensure_samples() -> list[Path]:
    """确保示例数据存在，返回文件路径列表。"""
    rng = random.Random(20260623)
    paths = []
    district_fc = _fc(build_districts(rng))
    parcel_fc = _fc(build_parcels(rng))
    road_fc = _fc(build_roads(rng))
    school_fc = _fc(build_schools(rng, parcel_fc["features"]))
    for name, fc in [("行政区.geojson", district_fc), ("地块.geojson", parcel_fc),
                     ("道路.geojson", road_fc), ("学校.geojson", school_fc)]:
        p = SAMPLES / name
        if not p.exists():
            p.write_text(json.dumps(fc, ensure_ascii=False), "utf-8")
        paths.append(p)
    return paths


if __name__ == "__main__":
    for p in ensure_samples():
        data = json.loads(p.read_text("utf-8"))
        print(f"{p.name}: {len(data['features'])} 要素")
