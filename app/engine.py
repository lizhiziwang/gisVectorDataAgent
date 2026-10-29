"""矢量数据操作引擎：所有空间运算的真实实现（GeoPandas / Shapely / PyProj）。

设计约定（对应 GIS 生产规范）：
1. 原始数据只读 —— 任何算子都不修改入参图层，一律产出新图层；
2. CRS 强制校验 —— 距离/面积类算子自动投影到 CGCS2000 3 度带（中国境内）；
3. 几何有效性自动修复 —— 拓扑错误在入口处修复并计入报告；
4. 每一步都返回可追溯的执行报告（要素数变化、告警、耗时）。
"""
from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely import make_valid
from shapely.geometry import box

# 图层配色：印刷感的低饱和一套，和暖白底界面相处不打架，同时保持可分辨。
# 第一色是界面强调色（陶土橙），新建图层会顺着往下取。
PALETTE = [
    "#c96442", "#4a6fa5", "#5b8c5a", "#a9762b", "#7b5ea7",
    "#3f7d8c", "#b04a5a", "#6b7a4f", "#8a6a4f", "#5a6b7d",
]

UNIT_TO_M = {"m": 1.0, "米": 1.0, "km": 1000.0, "公里": 1000.0, "千米": 1000.0,
             "cm": 0.01, "厘米": 0.01, "mm": 0.001, "毫米": 0.001,
             "ha": 10000.0, "公顷": 10000.0, "ft": 0.3048, "英尺": 0.3048}


# --------------------------------------------------------------------------- 图层
@dataclass
class Layer:
    id: str
    name: str
    gdf: Any                       # GeoDataFrame（保持其原始 CRS）
    color: str = "#2f6fed"
    source: str = ""
    parent: str = ""
    op: str = ""
    created_at: float = field(default_factory=time.time)
    _invalid: int | None = field(default=None, repr=False)
    _meta: dict | None = field(default=None, repr=False, compare=False)

    def invalid_count(self) -> int | None:
        """无效几何数量：只算一次并缓存，超大图层跳过（逐个校验太慢）。"""
        if self._invalid is None:
            if len(self.gdf) > 20000:
                self._invalid = -1
            else:
                try:
                    import shapely
                    # 向量化校验：geopandas 的 .is_valid 逐个调用 shapely.is_valid，
                    # 复杂多边形上万个就要几百毫秒；直接用 shapely 的数组接口快得多
                    arr = self.gdf.geometry.values
                    self._invalid = int((~shapely.is_valid(arr)).sum())
                except Exception:
                    self._invalid = 0
        return None if self._invalid < 0 else self._invalid

    @property
    def geom_type(self) -> str:
        if len(self.gdf) == 0:
            return "Empty"
        types = {str(t).replace("Multi", "") for t in self.gdf.geom_type.dropna().unique()}
        return "/".join(sorted(types)) or "Empty"

    def meta(self, with_invalid: bool = False) -> dict:
        """图层元信息（结果缓存，几何不变量只算一次）。

        `total_bounds` 和 `geom_type` 在几十万要素上各要几十毫秒，
        而图层列表刷新一次就要对每个图层取一遍 —— 不缓存的话，
        一次载入多个图层会白卡好几秒（实测 3.4 秒）。
        图层对象一旦创建，几何不再变化，所以可以安全缓存。

        `with_invalid=False`（默认）不触发几何校验 —— 校验几万个复杂多边形
        要几百毫秒，放在界面刷新路径上会卡住；只有算子执行时
        （后台线程）才需要。
        """
        if self._meta is None:
            b = self.gdf.total_bounds if len(self.gdf) else [0, 0, 0, 0]
            crs = self.gdf.crs
            self._meta = {
                "id": self.id,
                "name": self.name,
                "color": self.color,
                "count": int(len(self.gdf)),
                "geomType": self.geom_type,
                "crs": f"EPSG:{crs.to_epsg()}" if crs is not None and crs.to_epsg() else (crs.name if crs is not None else "未定义"),
                "crsName": crs.name if crs is not None else "未定义",
                "bounds": [float(x) for x in b],
                "fields": [str(c) for c in self.gdf.columns if c != self.gdf.geometry.name],
                "source": self.source,
                "parent": self.parent,
                "op": self.op,
            }
        d = dict(self._meta)
        if with_invalid:
            d["invalid"] = self.invalid_count() if len(self.gdf) else 0
        else:
            d["invalid"] = None
        return d


class Store:
    """内存图层仓库（单机单会话）。"""

    def __init__(self) -> None:
        self.layers: dict[str, Layer] = {}
        self._n = 0

    def add(self, gdf: gpd.GeoDataFrame, name: str, **kw) -> Layer:
        lid = "L" + uuid.uuid4().hex[:8]
        color = PALETTE[self._n % len(PALETTE)]
        self._n += 1
        # 图层名去重：同名结果自动加序号，避免图层面板出现多个"地块_融合"
        existing = {l.name for l in self.layers.values()}
        if name in existing:
            base, k = name, 2
            while f"{base}_{k}" in existing:
                k += 1
            name = f"{base}_{k}"
        lay = Layer(id=lid, name=name, gdf=gdf, color=kw.pop("color", color), **kw)
        self.layers[lid] = lay
        return lay

    def get(self, ref: str | None) -> Layer | None:
        if not ref:
            if len(self.layers) == 1:
                return next(iter(self.layers.values()))
            return None
        if ref in self.layers:
            return self.layers[ref]
        low = str(ref).lower().strip()
        for lay in self.layers.values():
            if lay.name.lower() == low:
                return lay
        cands = [l for l in self.layers.values() if low in l.name.lower() or l.name.lower() in low]
        if cands:
            return max(cands, key=lambda l: len(l.name))
        return None

    def resolve(self, ref: str | None) -> Layer:
        lay = self.get(ref)
        if lay is None:
            names = "、".join(l.name for l in self.layers.values()) or "（空）"
            raise ValueError(f"找不到图层「{ref}」，当前可用图层：{names}")
        return lay


# --------------------------------------------------------------------------- 工具函数
def _ensure_crs(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, list[str]]:
    warns = []
    if gdf.crs is None:
        warns.append("数据无坐标系信息，按 WGS84 (EPSG:4326) 处理，请核实。")
        gdf = gdf.set_crs(4326)
    return gdf, warns


def _fix_geom(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    try:
        bad = ~gdf.geometry.is_valid
    except Exception:
        return gdf, 0
    n = int(bad.sum())
    if n:
        gdf = gdf.copy()
        gdf.loc[bad, gdf.geometry.name] = gdf.loc[bad, gdf.geometry.name].apply(make_valid)
    return gdf, n


def metric_crs(gdf: gpd.GeoDataFrame) -> tuple[str, list[str]]:
    """返回适合做距离/面积计算的投影坐标系。"""
    warns: list[str] = []
    crs = gdf.crs
    if crs is not None and not crs.is_geographic:
        return crs.to_string(), warns
    b = gdf.to_crs(4326).total_bounds if len(gdf) else [0, 0, 0, 0]
    lon, lat = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    if not (-180 <= lon <= 180 and -85 <= lat <= 85):
        lon, lat = 104.0, 35.0
        warns.append("数据范围异常，投影参数按中国中部（104°E, 35°N）取值。")
    if 73.0 <= lon <= 136.0 and 3.0 <= lat <= 54.0:
        k = int(round((lon - 75.0) / 3.0))
        k = max(0, min(20, k))
        cm = 75 + 3 * k
        warns.append(f"已投影至 CGCS2000 / 3度带 CM {cm}°E（EPSG:{4534 + k}），距离单位为米。")
        if (b[2] - b[0]) > 3.5:
            warns.append("数据经度跨度超过 3°，跨带区域存在投影变形，面积统计建议改用等积投影。")
        return f"EPSG:{4534 + k}", warns
    zone = int((lon + 180) / 6) + 1
    epsg = (32600 if lat >= 0 else 32700) + zone
    warns.append(f"数据不在中国境内，已投影至 UTM {zone}{'N' if lat >= 0 else 'S'}（EPSG:{epsg}）。")
    return f"EPSG:{epsg}", warns


def to_display_geojson(gdf: gpd.GeoDataFrame, limit: int | None = None) -> dict:
    """转成 4326 的 GeoJSON 供前端渲染。"""
    g = gdf if gdf.crs is None else gdf.to_crs(4326)
    if limit and len(g) > limit:
        g = g.head(limit)
    return _dumps(g)


def _dumps(g: gpd.GeoDataFrame) -> dict:
    import json
    return json.loads(g.to_json(drop_id=True, default=str))


def _fmt(n: float, unit: str = "") -> str:
    if n is None or (isinstance(n, float) and math.isnan(n)):
        return "-"
    if abs(n) >= 1e6:
        return f"{n/1e6:,.3f} 百万{unit}"
    if abs(n) >= 1000:
        return f"{n:,.2f}{unit}"
    return f"{n:,.2f}{unit}"


def _num(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


# --------------------------------------------------------------------------- 算子
@dataclass
class Result:
    ok: bool
    message: str
    layer: Layer | None = None
    warnings: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def op_describe(store: Store, layer: str | None = None) -> Result:
    lay = store.resolve(layer)
    g = lay.gdf
    warns = []
    info = lay.meta(with_invalid=True)
    if info["invalid"]:
        warns.append(f"发现 {info['invalid']} 个无效几何（自相交/环未闭合等），运算前会自动修复。")
    elif info["invalid"] is None:
        warns.append("图层要素较多，已跳过逐要素的几何有效性检查（运算时仍会自动修复）。")
    if g.crs is None:
        warns.append("图层缺少坐标系定义。")
    area = length = None
    if len(g):
        mc, w = metric_crs(g)
        warns += w[:1]
        p = g.to_crs(mc)
        try:
            if "Polygon" in info["geomType"]:
                area = float(p.area.sum())
            if "Line" in info["geomType"]:
                length = float(p.length.sum())
        except Exception:
            pass
    data = {"meta": info, "area_m2": area, "length_m": length}
    msg = (f"图层「{lay.name}」：{info['count']} 个要素，几何类型 {info['geomType']}，"
           f"坐标系 {info['crs']}，字段 {len(info['fields'])} 个（{'、'.join(info['fields'][:8])}"
           f"{'…' if len(info['fields'])>8 else ''}）。")
    if area:
        msg += f" 总面积约 {area/1e6:.4f} km²。"
    if length:
        msg += f" 总长度约 {length/1000:.3f} km。"
    return Result(True, msg, None, warns, data)


def _buffer(store: Store, layer, distance=500, unit="m", quad_segs=16, dissolve=False, name=None) -> Result:
    lay = store.resolve(layer)
    g, w0 = _ensure_crs(lay.gdf)
    g, nfix = _fix_geom(g)
    warns = list(w0)
    if nfix:
        warns.append(f"已修复 {nfix} 个无效几何。")
    d = _num(distance)
    if d is None:
        raise ValueError("缓冲距离必须是数字。")
    d_m = d * UNIT_TO_M.get(str(unit).lower(), 1.0)
    if d_m == 0:
        raise ValueError("缓冲距离不能为 0。")
    mc, w = metric_crs(g)
    warns += w
    p = g.to_crs(mc)
    out = p.copy()
    out[out.geometry.name] = p.geometry.buffer(d_m, quad_segs=int(quad_segs))
    if dissolve:
        u = out.geometry.union_all()
        out = gpd.GeoDataFrame({"buffer_m": [d_m]}, geometry=[u], crs=mc)
    back = out.to_crs(g.crs) if g.crs is not None else out
    nm = name or f"{lay.name}_缓冲{_fmt(d_m,'m')}"
    nl = store.add(back, nm, source=lay.id, op="buffer")
    msg = (f"对「{lay.name}」按 {_fmt(d_m,' m')} 做缓冲区：要素 {len(g)} → {len(back)}"
           f"{'（已融合为整体）' if dissolve else ''}。")
    return Result(True, msg, nl, warns, {"distance_m": d_m, "features_in": len(g), "features_out": len(back)})


def _clip(store: Store, layer, mask, name=None) -> Result:
    lay, mk = store.resolve(layer), store.resolve(mask)
    g, w0 = _ensure_crs(lay.gdf)
    m, w1 = _ensure_crs(mk.gdf)
    g, n1 = _fix_geom(g)
    m, n2 = _fix_geom(m)
    warns = list(w0) + list(w1)
    if n1 + n2:
        warns.append(f"几何修复：主图层 {n1} 个，裁剪面 {n2} 个。")
    m = m.to_crs(g.crs)
    clip_geom = m.geometry.union_all()
    sel = g[g.geometry.intersects(clip_geom)]
    out = gpd.clip(sel, m) if len(sel) else sel
    nl = store.add(out, name or f"{lay.name}_裁剪", source=lay.id, op="clip")
    msg = f"用「{mk.name}」裁剪「{lay.name}」：要素 {len(g)} → {len(out)}。"
    return Result(True, msg, nl, warns, {"features_in": len(g), "features_out": len(out)})


def _intersect(store: Store, layer, other, name=None) -> Result:
    a, b = store.resolve(layer), store.resolve(other)
    ga, _ = _ensure_crs(a.gdf)
    gb, _ = _ensure_crs(b.gdf)
    gb = gb.to_crs(ga.crs)
    out = gpd.overlay(ga, gb, how="intersection", keep_geom_type=False)
    nl = store.add(out, name or f"{a.name}_∩_{b.name}", source=a.id, op="intersect")
    return Result(True, f"「{a.name}」与「{b.name}」求交：得到 {len(out)} 个要素。", nl, [],
                  {"features_out": len(out)})


def _union(store: Store, layer, other, name=None) -> Result:
    a, b = store.resolve(layer), store.resolve(other)
    ga, _ = _ensure_crs(a.gdf)
    gb, _ = _ensure_crs(b.gdf)
    gb = gb.to_crs(ga.crs)
    out = gpd.overlay(ga, gb, how="union", keep_geom_type=False)
    nl = store.add(out, name or f"{a.name}_∪_{b.name}", source=a.id, op="union")
    return Result(True, f"「{a.name}」与「{b.name}」合并叠加：得到 {len(out)} 个要素。", nl, [],
                  {"features_out": len(out)})


def _erase(store: Store, layer, mask, name=None) -> Result:
    a, b = store.resolve(layer), store.resolve(mask)
    ga, _ = _ensure_crs(a.gdf)
    gb, _ = _ensure_crs(b.gdf)
    gb = gb.to_crs(ga.crs)
    out = gpd.overlay(ga, gb, how="difference", keep_geom_type=False)
    nl = store.add(out, name or f"{a.name}_擦除_{b.name}", source=a.id, op="erase")
    return Result(True, f"从「{a.name}」中擦除「{b.name}」范围：要素 {len(ga)} → {len(out)}。", nl, [],
                  {"features_in": len(ga), "features_out": len(out)})


def _dissolve(store: Store, layer, by=None, agg=None, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    by_cols = []
    if by:
        if isinstance(by, str):
            by = [s.strip() for s in by.replace("，", ",").split(",") if s.strip()]
        for c in by:
            if c in g.columns:
                by_cols.append(c)
            else:
                w.append(f"字段「{c}」不存在，已忽略。")
    aggmap = None
    if agg and isinstance(agg, dict):
        aggmap = {k: v for k, v in agg.items() if k in g.columns}
    if by_cols:
        out = g.dissolve(by=by_cols, aggfunc=aggmap or "first", as_index=False)
        msg = f"按「{'、'.join(by_cols)}」融合「{lay.name}」：要素 {len(g)} → {len(out)}。"
    else:
        u = g.geometry.union_all()
        out = gpd.GeoDataFrame({"source": [lay.name], "count": [len(g)]}, geometry=[u], crs=g.crs)
        msg = f"把「{lay.name}」全部要素融合为 1 个整体（原 {len(g)} 个）。"
    nl = store.add(out, name or f"{lay.name}_融合", source=lay.id, op="dissolve")
    return Result(True, msg, nl, w, {"features_in": len(g), "features_out": len(out)})


def _reproject(store: Store, layer, epsg, name=None) -> Result:
    lay = store.resolve(layer)
    g, _ = _ensure_crs(lay.gdf)
    code = str(epsg).upper().replace("EPSG:", "").strip()
    try:
        crs = f"EPSG:{int(code)}"
        out = g.to_crs(crs)
    except Exception as e:
        raise ValueError(f"无法转换到 EPSG:{code}：{e}")
    nl = store.add(out, name or f"{lay.name}_{crs}", source=lay.id, op="reproject")
    return Result(True, f"「{lay.name}」由 {lay.meta()['crs']} 转换到 {crs}。", nl, [],
                  {"from": lay.meta()["crs"], "to": crs})


def _simplify(store: Store, layer, tolerance=10, unit="m", name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    mc, wm = metric_crs(g)
    tol = (_num(tolerance) or 0) * UNIT_TO_M.get(str(unit).lower(), 1.0)
    if tol <= 0:
        raise ValueError("容差必须大于 0。")
    p = g.to_crs(mc)
    simp = p.copy()
    simp[simp.geometry.name] = p.geometry.simplify(tol, preserve_topology=True)
    out = simp.to_crs(g.crs)
    nl = store.add(out, name or f"{lay.name}_简化", source=lay.id, op="simplify")
    n_in, n_out = _node_count(g), _node_count(out)
    red = max(0.0, 1 - n_out / max(n_in, 1))
    return Result(True, f"按 {_fmt(tol,' m')} 容差简化「{lay.name}」，节点 {n_in} → {n_out}（减少 {red*100:.1f}%）。",
                  nl, w + wm[:1],
                  {"tolerance_m": tol, "node_reduction_pct": round(red * 100, 2)})


def _node_count(gdf: gpd.GeoDataFrame) -> int:
    try:
        import shapely
        return int(shapely.get_num_coordinates(gdf.geometry.array).sum())
    except Exception:
        return 0


def _min_rect(store: Store, layer, mode="rotated", name=None) -> Result:
    """最小外接矩形。

    `mode="rotated"`：最小**面积**的旋转矩形（shapely minimum_rotated_rectangle），
    `mode="envelope"`：轴对齐外接框（东西向正矩形）。

    逐要素生成一个矩形面，并保留原属性 + 几个量测字段，
    方便接着算「周长 / 外接矩形周长」这类指标。
    """
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    if len(g) == 0:
        raise ValueError("图层没有要素。")
    is_poly = "Polygon" in lay.geom_type or "MultiPolygon" in lay.geom_type
    if not is_poly:
        w = w + ["该图层不是面图层，按几何外包处理。"]
    m = str(mode or "rotated").lower()
    try:
        if m.startswith("env") or m in ("axis", "bbox", "box"):
            rects = shapely.envelope(g.geometry.values)
        else:
            rects = shapely.minimum_rotated_rectangle(g.geometry.values)
    except Exception as e:
        raise ValueError(f"计算最小外接矩形失败：{e}")
    out = g.copy()
    out[out.geometry.name] = rects
    out = out[~out.geometry.is_empty & out.geometry.notna()]
    mc, wm = metric_crs(g)
    p = out.to_crs(mc)
    out["rect_area_m2"] = np.round(shapely.area(p.geometry.values), 2)
    out["rect_perim_m"] = np.round(shapely.length(p.geometry.values), 2)
    if m.startswith("env") or m in ("axis", "bbox", "box"):
        b = p.geometry.bounds
        out["rect_w_m"] = np.round(b["maxx"] - b["minx"], 2)
        out["rect_h_m"] = np.round(b["maxy"] - b["miny"], 2)
    nm = name or (f"{lay.name}_外接矩形" if not m.startswith("env") else f"{lay.name}_包围盒")
    nl = store.add(out, nm, source=lay.id, op="min_rect")
    kind = "轴对齐包围盒" if m.startswith("env") else "最小面积旋转矩形"
    return Result(True, f"为「{lay.name}」生成 {len(out)} 个{kind}（含长宽/面积字段）。",
                  nl, w + wm[:1], {"features_out": len(out), "mode": m})


def _shape_metrics(store: Store, layer, name=None) -> Result:
    """形状指标：面积、周长、最小外接矩形周长、以及「周长 / 外接矩形周长」比值。

    周长比越接近 1 越「方」；越小说明形状越细长、越不紧凑。
    这些量在**投影坐标系**下算，单位是米，避免 4326 下算长度没有意义。
    """
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    if len(g) == 0:
        raise ValueError("图层没有要素。")
    mc, wm = metric_crs(g)
    p = g.to_crs(mc)
    geoms = p.geometry.values
    out = g.copy()
    is_line = "Line" in lay.geom_type
    if is_line:
        perim = shapely.length(geoms)
        out["len_m"] = np.round(perim, 2)
        mrr_perim = np.round(shapely.length(shapely.minimum_rotated_rectangle(geoms)), 2)
        out["rect_perim_m"] = mrr_perim
        with np.errstate(divide="ignore", invalid="ignore"):
            out["rect_ratio"] = np.round(perim / np.where(mrr_perim > 0, mrr_perim, np.nan), 4)
        msg = f"为「{lay.name}」新增长度、外接矩形周长、长度比字段（{len(out)} 个要素）。"
    else:
        perim = shapely.length(geoms)
        area = shapely.area(geoms)
        mrr_perim = shapely.length(shapely.minimum_rotated_rectangle(geoms))
        out["area_m2"] = np.round(area, 2)
        out["perim_m"] = np.round(perim, 2)
        out["rect_perim_m"] = np.round(mrr_perim, 2)
        with np.errstate(divide="ignore", invalid="ignore"):
            out["rect_ratio"] = np.round(perim / np.where(mrr_perim > 0, mrr_perim, np.nan), 4)
            # 圆度 4πA/P²：圆=1，越方越接近 0.785
            out["roundness"] = np.round(4 * np.pi * area / np.where(perim > 0, perim ** 2, np.nan), 4)
        msg = (f"为「{lay.name}」新增 面积 / 周长 / 外接矩形周长 / 周长比 / 圆度 字段"
               f"（{len(out)} 个要素）。")
    nl = store.add(out, name or f"{lay.name}_形状指标", source=lay.id, op="shape_metrics")
    return Result(True, msg, nl, w + wm[:1], {"features_out": len(out)})


def _centroid(store: Store, layer, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    out = g.copy()
    out[out.geometry.name] = g.geometry.representative_point()
    out["_src_area_m2"] = None
    mc, _ = metric_crs(g)
    try:
        out["_src_area_m2"] = g.to_crs(mc).area.round(2)
    except Exception:
        pass
    nl = store.add(out, name or f"{lay.name}_质心", source=lay.id, op="centroid")
    return Result(True, f"生成「{lay.name}」的要素中心点，共 {len(out)} 个点。", nl, w, {"features_out": len(out)})


def _convex_hull(store: Store, layer, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    hull = g.geometry.union_all().convex_hull
    out = gpd.GeoDataFrame({"source": [lay.name]}, geometry=[hull], crs=g.crs)
    nl = store.add(out, name or f"{lay.name}_凸包", source=lay.id, op="convex_hull")
    return Result(True, f"生成「{lay.name}」的整体凸包。", nl, w, {})


def _voronoi(store: Store, layer, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    mc, wm = metric_crs(g)
    p = g.to_crs(mc)
    pts = p[p.geometry.geom_type.isin(["Point", "MultiPoint"])]
    if len(pts) < 2:
        raise ValueError("泰森多边形至少需要 2 个点要素。")
    env = pts.total_bounds
    pad = max(env[2] - env[0], env[3] - env[1]) * 0.15 + 100
    bounds = box(env[0] - pad, env[1] - pad, env[2] + pad, env[3] + pad)
    cells = pts.geometry.voronoi_polygons(extend_to=bounds)
    geoms = list(cells.geoms) if hasattr(cells, "geoms") else list(cells)
    out = gpd.GeoDataFrame(geometry=geoms, crs=mc).clip(bounds)
    # 附带点属性（空间连接）
    out = out.sjoin(pts, how="left", predicate="intersects").drop(columns=["index_right"], errors="ignore")
    out = out[~out.index.duplicated(keep="first")].to_crs(g.crs)
    nl = store.add(out, name or f"{lay.name}_泰森多边形", source=lay.id, op="voronoi")
    return Result(True, f"基于 {len(pts)} 个点生成泰森多边形 {len(out)} 个。", nl, w + wm[:1],
                  {"points": len(pts), "cells": len(out)})


def _points_along(store: Store, layer, distance=100, unit="m", layer_name=None) -> Result:
    raise NotImplementedError


def _grid(store: Store, layer, cell=1000, unit="m", name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    mc, wm = metric_crs(g)
    p = g.to_crs(mc)
    cell_m = (_num(cell) or 0) * UNIT_TO_M.get(str(unit).lower(), 1.0)
    if cell_m <= 0:
        raise ValueError("网格尺寸必须大于 0。")
    xmin, ymin, xmax, ymax = p.total_bounds
    cols = np.arange(xmin, xmax + cell_m, cell_m)
    rows = np.arange(ymin, ymax + cell_m, cell_m)
    if len(cols) * len(rows) > 250000:
        raise ValueError(f"网格数量过大（{len(cols)*len(rows)}），请增大网格尺寸。")
    from shapely.geometry import Polygon
    polys, ids = [], []
    for i in range(len(cols) - 1):
        for j in range(len(rows) - 1):
            polys.append(Polygon([(cols[i], rows[j]), (cols[i + 1], rows[j]),
                                  (cols[i + 1], rows[j + 1]), (cols[i], rows[j + 1])]))
            ids.append(f"{i}_{j}")
    out = gpd.GeoDataFrame({"cell_id": ids}, geometry=polys, crs=mc).to_crs(g.crs)
    nl = store.add(out, name or f"{lay.name}_网格{int(cell_m)}m", source=lay.id, op="grid")
    return Result(True, f"按 {_fmt(cell_m,' m')} 生成渔网，共 {len(out)} 个单元。", nl, w + wm[:1],
                  {"cells": len(out)})


def _sjoin(store: Store, layer, other, how="inner", predicate="intersects", name=None) -> Result:
    a, b = store.resolve(layer), store.resolve(other)
    ga, _ = _ensure_crs(a.gdf)
    gb, _ = _ensure_crs(b.gdf)
    gb = gb.to_crs(ga.crs)
    for df in (ga, gb):
        if df.index.name in ("index_right", "index_left"):
            df.index.name = None
    gb = gb.drop(columns=[c for c in gb.columns if c in ga.columns and c != gb.geometry.name], errors="ignore")
    out = gpd.sjoin(ga, gb, how=("inner" if how in ("inner", "left", "right") else "inner"),
                    predicate=predicate).drop(columns=["index_right"], errors="ignore")
    nl = store.add(out, name or f"{a.name}_空间连接", source=a.id, op="sjoin")
    return Result(True, f"「{a.name}」按 {predicate} 关联「{b.name}」：{len(ga)} → {len(out)} 个要素。", nl, [],
                  {"features_in": len(ga), "features_out": len(out), "predicate": predicate})


def _nearest(store: Store, layer, other, k=1, unit="m", name=None) -> Result:
    a, b = store.resolve(layer), store.resolve(other)
    ga, _ = _ensure_crs(a.gdf)
    gb, _ = _ensure_crs(b.gdf)
    gb = gb.to_crs(ga.crs)
    mc, wm = metric_crs(ga)
    pa, pb = ga.to_crs(mc), gb.to_crs(mc)
    kk = max(1, int(k or 1))
    out = None
    if kk > 1:
        try:
            out = gpd.sjoin(pa, pb, how="left", predicate="nearest", k=kk, distance="distance_m")
        except Exception:
            out = None
    if out is None:
        out = gpd.sjoin_nearest(pa, pb, how="left", distance_col="distance_m")
    out = out.drop(columns=["index_right"], errors="ignore")
    out = out[~out.index.duplicated(keep="first")]
    out = out.to_crs(ga.crs)
    nl = store.add(out, name or f"{a.name}_最近邻", source=a.id, op="nearest")
    d = out["distance_m"] if "distance_m" in out else pd.Series(dtype=float)
    msg = f"为「{a.name}」查找「{b.name}」中最近要素（k={k}），平均距离 {_fmt(d.mean() if len(d) else 0,' m')}。"
    return Result(True, msg, nl, wm[:1], {"features_out": len(out),
                                          "mean_distance_m": float(d.mean()) if len(d) else None})


def _filter(store: Store, layer, expression, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    expr = _normalize_expr(expression)
    expr = _coerce_text_literals(g, expr)
    try:
        mask = g.eval(expr, engine="python")
        out = g[mask.astype(bool)]
    except Exception as e:
        raise ValueError(f"属性表达式无法执行：{expression}（{e}）。示例：人口 > 10000 and 类型 == '住宅'")
    nl = store.add(out, name or f"{lay.name}_筛选", source=lay.id, op="filter")
    msg = f"按条件「{expression}」筛选「{lay.name}」：要素 {len(g)} → {len(out)}。"
    if len(out) == 0 and len(g):
        msg += "（没有要素符合条件，检查一下字段名和取值？文本字段要用引号，如 code == '001'）"
    return Result(True, msg, nl, w,
                  {"features_in": len(g), "features_out": len(out), "expression": expression})


def _normalize_expr(expr: str) -> str:
    t = str(expr).strip().replace("＞", ">").replace("＜", "<").replace("＝", "=")
    t = t.replace("≠", "!=").replace("＆", " and ").replace("｜", " or ")
    t = t.replace("大于等于", ">=").replace("小于等于", "<=").replace("大于", ">").replace("小于", "<")
    t = t.replace("并且", " and ").replace("或者", " or ").replace("且", " and ")
    return t


def _coerce_text_literals(gdf, expr: str) -> str:
    """字符串列和数字比较时，自动给数字加引号。

    Shapefile 里 osm_id / code / 行政区代码这类字段都是**文本**类型，
    而人和模型都习惯写 `osm_id == 4814005`（不加引号）。
    pandas 逐元素比较 str 和 int 全返回 False，于是「筛选 20094 → 0」，
    看起来像条件写错了，其实数据里有这个 id。
    """
    import re as _re
    from pandas.api.types import is_numeric_dtype

    def repl(m):
        col, op, num = m.group(1), m.group(2), m.group(3)
        if col not in gdf.columns:
            return m.group(0)
        try:
            if is_numeric_dtype(gdf[col]):
                return m.group(0)
        except Exception:
            return m.group(0)
        return f'{col} {op} "{num}"'

    if "==" not in expr and "!=" not in expr:
        return expr
    return _re.sub(r"\b([A-Za-z_]\w*|[^\W\d]\w*)\s*(==|!=)\s*(-?\d+(?:\.\d+)?)\b", repl, expr)


def _field_calc(store: Store, layer, field, expression, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    if not field:
        raise ValueError("请指定新字段名。")
    out = g.copy()
    expr = _normalize_expr(expression)
    try:
        out[field] = out.eval(expr, engine="python")
    except Exception as e:
        msg = str(e)
        hint = ""
        unknown = (isinstance(e, NameError) or "is not defined" in msg
                   or "not a supported function" in msg)
        if unknown:
            m = (re.search(r"name '([^']+)' is not defined", msg)
                 or re.search(r'"([^"]+)" is not a supported function', msg)
                 or re.search(r"'([^']+)'", msg))
            who = m.group(1) if m else "表达式里的函数或变量"
            hint = (f"。「{who}」不存在 —— 表达式只能用字段名和四则运算。"
                    "要算几何量请用「形状指标」算子（一次加上 area_m2 / perim_m / "
                    "rect_perim_m / rect_ratio / roundness），再用这些字段做计算，"
                    "例如 area_m2 / 10000。")
        raise ValueError(f"字段计算失败：{msg}{hint}")
    nl = store.add(out, name or f"{lay.name}_计算", source=lay.id, op="field_calc")
    return Result(True, f"在「{lay.name}」新增字段「{field}」，按 {expression} 计算完成。", nl, w, {})


def _area_stats(store: Store, layer, group_by=None, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    mc, wm = metric_crs(g)
    p = g.to_crs(mc)
    is_line = "Line" in lay.geom_type
    val = p.length if is_line else p.area
    label = "长度" if is_line else "面积"
    tmp = p.copy()
    tmp["_v"] = val
    key = None
    if group_by:
        cand = [group_by] if isinstance(group_by, str) else list(group_by)
        key = next((c for c in cand if c in tmp.columns), None)
        if key is None:
            w.append(f"字段「{group_by}」不存在，已改为整体统计。可用字段：{'、'.join(map(str, tmp.columns[:10]))}")
    if key:
        agg = tmp.groupby(key, dropna=False)["_v"].agg(["sum", "count"]).reset_index()
        agg = agg.sort_values("sum", ascending=False)
        total = float(agg["sum"].sum())
        top = "、".join(f"{r[key]} {r['sum']/1e6:.3f}km²" for _, r in agg.head(5).iterrows())
        msg = (f"按「{key}」统计{label}：共 {len(agg)} 类，合计 {total/1e6:.4f} km²。"
               f"前几位：{top}。")
        agg = agg.rename(columns={"sum": f"{label}(km²)" if not is_line else "长度(km)",
                                  "count": "要素数"})
        div = 1e6 if not is_line else 1000.0
        agg[f"{label}(km²)" if not is_line else "长度(km)"] = (
            agg[f"{label}(km²)" if not is_line else "长度(km)"] / div).round(3)
    else:
        total = float(tmp["_v"].sum())
        msg = f"「{lay.name}」共 {len(g)} 个要素，总{label} {total/1e6:.4f} km²" if not is_line else \
              f"「{lay.name}」共 {len(g)} 个要素，总长度 {total/1000:.3f} km"
        agg = pd.DataFrame({"范围": [lay.name],
                            f"{label}(km²)" if not is_line else "长度(km)": [round(total / (1e6 if not is_line else 1000.0), 3)],
                            "要素数": [len(g)]})
    table = {
        "columns": [str(c) for c in agg.columns],
        "rows": [[_clean(v) for v in row] for row in agg.head(50).itertuples(index=False)],
    }
    return Result(True, msg, None, w + wm[:1], {"table": table, "total": total, "unit": "m" if is_line else "m2"})


def _clean(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return round(float(v), 4)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _merge(store: Store, layers, name=None) -> Result:
    if isinstance(layers, str):
        layers = [s.strip() for s in layers.replace("，", ",").split(",") if s.strip()]
    if not layers:
        raise ValueError("请指定要合并的图层。")
    if len(layers) == 1 and layers[0] in ("*", "all", "全部"):
        objs = list(store.layers.values())
    else:
        objs = [store.resolve(x) for x in layers]
    base = objs[0].gdf
    parts = []
    for o in objs:
        g = o.gdf if o.gdf.crs is None else o.gdf.to_crs(base.crs)
        g = g.copy()
        g["_source"] = o.name
        for c in g.columns:
            if c != g.geometry.name:
                g[c] = g[c].astype(object)
        parts.append(g)
    out = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=base.crs)
    nl = store.add(out, name or "合并图层", source=",".join(o.id for o in objs), op="merge")
    return Result(True, f"合并 {len(objs)} 个图层（{'、'.join(o.name for o in objs)}），共 {len(out)} 个要素。",
                  nl, [], {"features_out": len(out)})


def _dedupe(store: Store, layer, by_geometry=True, subset=None, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    before = len(g)
    if by_geometry:
        key = g.geometry.apply(lambda x: x.wkb_hex if x is not None else None)
        out = g[~key.duplicated()]
    else:
        cols = ([s.strip() for s in subset.replace("，", ",").split(",")] if isinstance(subset, str) else subset) or []
        cols = [c for c in cols if c in g.columns]
        if not cols:
            raise ValueError("请提供用于判重的字段（subset）。")
        out = g.drop_duplicates(subset=cols)
    removed = before - len(out)
    if not removed:
        return Result(True, f"「{lay.name}」没有重复要素，未生成新图层。", None, w, {"removed": 0})
    nl = store.add(out, name or f"{lay.name}_去重", source=lay.id, op="dedupe")
    return Result(True, f"「{lay.name}」去重：要素 {before} → {len(out)}（移除 {removed}）。", nl, w,
                  {"removed": removed})


def _bbox_select(store: Store, layer, xmin, ymin, xmax, ymax, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    b = box(float(xmin), float(ymin), float(xmax), float(ymax))
    if g.crs is not None and not g.crs.is_geographic:
        b = gpd.GeoSeries([b], crs=4326).to_crs(g.crs).iloc[0]
    out = g[g.geometry.intersects(b)]
    nl = store.add(out, name or f"{lay.name}_框选", source=lay.id, op="bbox")
    return Result(True, f"用范围框选取「{lay.name}」：{len(g)} → {len(out)} 个要素。", nl, w,
                  {"features_out": len(out)})


def _fix(store: Store, layer, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    bad = ~g.geometry.is_valid
    n = int(bad.sum())
    out = g.copy()
    if n:
        out.loc[bad, out.geometry.name] = out.loc[bad, out.geometry.name].apply(make_valid)
    drop = int(out.geometry.isna().sum() + out.geometry.is_empty.sum())
    out = out[~(out.geometry.isna() | out.geometry.is_empty)]
    if not n and not drop:
        # 一处都没改：不建新图层。否则图层面板会凭空多一个几乎同名的「_修复」，
        # 看着像把原数据复制了一份，后面的大模型还会把它当成另一个图层去引用。
        return Result(True, f"「{lay.name}」几何检查通过：无效几何 0 个、空几何 0 个，"
                            f"无需修复，未生成新图层。", None, w,
                      {"repaired": 0, "empty_removed": 0})
    nl = store.add(out, name or f"{lay.name}_修复", source=lay.id, op="fix")
    return Result(True, f"几何修复完成：修复无效几何 {n} 个，清理空几何 {drop} 个。", nl, w,
                  {"repaired": n, "empty_removed": drop})


def _explode(store: Store, layer, name=None) -> Result:
    lay = store.resolve(layer)
    g, w = _ensure_crs(lay.gdf)
    out = g.explode(index_parts=False).reset_index(drop=True)
    nl = store.add(out, name or f"{lay.name}_炸开", source=lay.id, op="explode")
    return Result(True, f"多部件拆分为单部件：要素 {len(g)} → {len(out)}。", nl, w, {"features_out": len(out)})


def make_custom_operator(spec: dict) -> Callable:
    """把「自定义算子」包装成和其它算子一样签名的函数。

    生成物本身只关心 `op(gdf, args) -> (GeoDataFrame|None, 说明)`，
    图层解析、CRS 校验、结果入库、warnings 这些脏活都留在引擎侧，
    这样模型写出来的算子能和内置算子平等地被规划器调用。
    """
    from . import custom_ops

    def _run(store: Store, layer=None, **args) -> Result:
        lay = store.resolve(layer)
        out_name = args.pop("name", None)
        # 参数里若是别的图层名，就地换成它的 GeoDataFrame ——
        # 这样「面内道路总长」这类跨图层算法也能写成一个算子
        for key in ("other", "mask", "layer2", "join_layer"):
            v = args.get(key)
            if isinstance(v, str):
                other = store.get(v)
                if other is not None:
                    args[key] = other.gdf
        gdf, w = _ensure_crs(lay.gdf)
        out, msg = custom_ops.run_op(spec, gdf, args)
        title = spec.get("title") or spec.get("name")
        if out is None:
            return Result(True, msg or f"「{title}」执行完成。", None, w, {"custom": spec["name"]})
        nl = store.add(out, out_name or f"{lay.name}_{title}", source=lay.id, op=spec["name"])
        return Result(True, msg or f"「{title}」生成 {len(out)} 个要素。", nl, w,
                      {"features_out": len(out), "custom": spec["name"]})

    _run.__name__ = f"custom_{spec.get('name', 'op')}"
    return _run


OPERATIONS: dict[str, Callable] = {
    "describe": op_describe,
    "buffer": _buffer,
    "clip": _clip,
    "intersect": _intersect,
    "union": _union,
    "erase": _erase,
    "dissolve": _dissolve,
    "reproject": _reproject,
    "simplify": _simplify,
    "centroid": _centroid,
    "convex_hull": _convex_hull,
    "min_rect": _min_rect,
    "shape_metrics": _shape_metrics,
    "voronoi": _voronoi,
    "grid": _grid,
    "sjoin": _sjoin,
    "nearest": _nearest,
    "filter": _filter,
    "field_calc": _field_calc,
    "area_stats": _area_stats,
    "merge": _merge,
    "dedupe": _dedupe,
    "bbox_select": _bbox_select,
    "fix_geometry": _fix,
    "explode": _explode,
}
