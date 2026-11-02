"""Esri FileGDB（.gdb 文件夹数据库）读取。

依赖 `pyopenfilegdb`（纯 Python 的 OpenFileGDB 实现，不调 GDAL / fiona）。
它是一个**文件夹**，里面可以有多个要素类，所以这里返回的是
`[(图层名, GeoDataFrame), ...]`，由界面决定全部导入还是按需选择。

和 `io_utils.read_vector()` 的边界：
· 普通单文件矢量仍走 geopandas / pyogrio；
· `.gdb` 只走 pyopenfilegdb，避免 GDAL 版本差异和 FileGDB 驱动缺失。
"""
from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

try:  # Shapely 2.1+ 才有 ragged array 批量构造；旧版本回退逐个 shape()
    from shapely import GeometryType, from_ragged_array
except ImportError:  # pragma: no cover - 兼容 shapely 2.0
    GeometryType = None
    from_ragged_array = None
from shapely.geometry import shape

GDB_SUFFIX = ".gdb"

#: 一次转换多少个面要素。批量 1000~2000 能摊薄 Shapely 构造的固定开销，
#: 又不会把整层 4000 万个顶点同时复制进一个大数组。
_GDB_BATCH = 1000

#: pyopenfilegdb 的安装提示；只在真正打开 GDB 时出现，不拖慢普通矢量启动。
GDB_INSTALL_HINT = (
    "打开 FileGDB 需要 pyopenfilegdb（纯 Python，无需 GDAL）：\n"
    "  pip install \"pyopenfilegdb @ git+https://github.com/lizhiziwang/pyopengdb.git\""
)


def is_gdb_path(path) -> bool:
    """是不是一个 FileGDB 文件夹（.gdb 是目录，不是普通文件）。"""
    p = Path(str(path))
    return p.suffix.lower() == GDB_SUFFIX and p.is_dir()


def _open_gdb(path):
    try:
        from pyopenfilegdb import OpenFileGDB
    except Exception as e:  # noqa: BLE001
        raise ValueError(GDB_INSTALL_HINT) from e
    p = Path(path)
    if not is_gdb_path(p):
        raise ValueError(f"不是 FileGDB 文件夹：{p}（需要以 .gdb 结尾的目录）")
    return OpenFileGDB.open(str(p))


def gdb_layer_names(path) -> list[str]:
    """列出 GDB 里的要素类名；打不开时返回中文错误。"""
    try:
        with _open_gdb(path) as db:
            return [str(n) for n in db.list_feature_classes()]
    except Exception as e:  # noqa: BLE001
        if "pyopenfilegdb" in str(e):
            raise
        raise ValueError(f"读取 FileGDB 失败：{e}") from e


def gdb_layer_infos(path) -> list[dict]:
    """图层选择弹窗用的轻量元信息：只读头，不读要素。

    返回 [{"name", "features", "geom", "crs"}, ...]。
    """
    try:
        with _open_gdb(path) as db:
            out = []
            for name in db.list_feature_classes():
                layer = db.get_layer(name)
                gt = str(getattr(layer, "geometry_type", "") or "")
                if gt.lower() in ("", "null"):
                    continue
                crs = _gdb_crs(layer)
                out.append({
                    "name": str(name),
                    "features": int(getattr(layer, "record_count", 0) or 0),
                    "geom": gt,
                    "crs": crs.to_string() if crs is not None else "",
                })
            return out
    except Exception as e:  # noqa: BLE001
        if "pyopenfilegdb" in str(e):
            raise
        raise ValueError(f"读取 FileGDB 失败：{e}") from e


def _gdb_crs(layer):
    """GdbSpatialRef → pyproj.CRS。优先 WKT，WKID 只作兜底。"""
    sr = getattr(layer, "spatial_ref", None)
    if sr is None:
        return None
    from pyproj import CRS
    wkt = getattr(sr, "wkt", None)
    if wkt:
        try:
            return CRS.from_wkt(wkt)
        except Exception:
            pass
    wkid = getattr(sr, "effective_wkid", None)
    if wkid:
        try:
            return CRS.from_epsg(int(wkid))
        except Exception:
            pass
    return None


def _to_shapely(g):
    if g is None:
        return None
    try:
        if getattr(g, "is_empty", False):
            return None
        return shape(g.__geo_interface__)
    except Exception:
        return None


def _polygons_from_geoms(geoms) -> list:
    """一批 polygon → Shapely，用 ragged array 一次性构造。

    纯 Python 逐个 `shape(g.__geo_interface__)` 要先把 4000 万个顶点物化成
    Python 元组，再把元组交给 Shapely，单图层要 50~80 秒。这里直接拿
    pyopenfilegdb 的 flat `array('d')`（C 扩展解码的结果）拼成连续坐标数组，
    一次 `from_ragged_array` 构造整批，耗时降到几秒。
    """
    if from_ragged_array is None:
        raise RuntimeError("当前 Shapely 版本没有 from_ragged_array")
    parts = []
    ring_offsets = [0]
    geom_offsets = [0]
    pos = 0
    for g in geoms:
        xy = g.xy_parts
        if int(getattr(g, "part_count", 0)) == 1:
            groups = ((0, ()),)
        else:
            groups = g._ring_groups()
        if not groups:
            raise ValueError("面几何没有环")
        for shell, holes in groups:
            for idx in (shell, *tuple(holes or ())):
                a = xy[int(idx)]
                n = len(a) // 2
                if n < 3:
                    raise ValueError("面环少于 3 个点")
                parts.append(a)
                pos += n
                ring_offsets.append(pos)
        geom_offsets.append(len(ring_offsets) - 1)
    if pos <= 0:
        raise ValueError("空坐标")
    coords = np.empty((pos, 2), dtype=np.float64)
    p = 0
    for a in parts:
        n = len(a) // 2
        coords[p:p + n, 0] = a[0::2]
        coords[p:p + n, 1] = a[1::2]
        p += n
    arr = from_ragged_array(
        GeometryType.POLYGON, coords,
        (np.asarray(ring_offsets, dtype=np.int64),
         np.asarray(geom_offsets, dtype=np.int64)))
    if len(arr) != len(geoms):
        raise ValueError("批量构造的几何数量不一致")
    return list(arr)


def _append_geoms(geoms: list, chunk: list) -> None:
    """把一批 feature.geometry 追加到 geoms；能批量就批量，不能就逐个兜底。"""
    if chunk and all(g is not None and getattr(g, "kind", "") == "polygon" for g in chunk):
        try:
            geoms.extend(_polygons_from_geoms(chunk))
            return
        except Exception:
            # 极少数脏几何/环形结构异常：退回逐个转换，不让整层导入失败。
            pass
    geoms.extend(_to_shapely(g) for g in chunk)


def _layer_to_gdf(layer):
    """一个 pyopenfilegdb 图层 → GeoDataFrame。

    属性里没有 OBJECTID（pyopenfilegdb 把它放在 feature.oid 上），
    字段清单以 `layer.fields` 为准，避免不同记录的键集合不一致。
    面要素按批走 ragged array 快速构造，点/线/复杂几何逐个兜底。
    """
    fields = [str(f.name) for f in getattr(layer, "fields", ())
              if not getattr(f, "is_oid", False) and not getattr(f, "is_geometry", False)]
    rows: list[dict] = []
    geoms = []
    chunk = []
    for feat in layer.read_features():
        attrs = getattr(feat, "attributes", None) or {}
        rows.append({name: attrs.get(name) for name in fields})
        chunk.append(getattr(feat, "geometry", None))
        if len(chunk) >= _GDB_BATCH:
            _append_geoms(geoms, chunk)
            chunk = []
    if chunk:
        _append_geoms(geoms, chunk)
    if not rows:
        return None

    crs = _gdb_crs(layer)
    if fields:
        df = pd.DataFrame(rows, columns=fields)
    else:
        df = pd.DataFrame(index=range(len(rows)))
    geom_col = "geometry" if "geometry" not in fields else "__gdb_geometry__"
    df[geom_col] = geoms
    return gpd.GeoDataFrame(df, geometry=geom_col, crs=crs)


def read_gdb_layers(path, layer_names: list[str] | None = None,
                    on_progress=None) -> list[tuple[str, gpd.GeoDataFrame]]:
    """读取 GDB 里的全部（或指定）要素类，返回 [(名称, GeoDataFrame)]。

    `on_progress(i, total, layer_name)` 在开始读每个要素类前调用，给界面刷进度。
    空要素类和独立属性表会被跳过；如果全部为空，抛中文错误。
    """
    try:
        with _open_gdb(path) as db:
            available = [str(n) for n in db.list_feature_classes()]
            wanted = list(layer_names) if layer_names else available
            missing = [n for n in wanted if n not in available]
            if missing:
                raise ValueError("GDB 里找不到图层：" + "、".join(missing)
                                 + "；可用：" + "、".join(available))
            out: list[tuple[str, gpd.GeoDataFrame]] = []
            for idx, name in enumerate(wanted, 1):
                if on_progress is not None:
                    try:
                        on_progress(idx, len(wanted), name)
                    except Exception:
                        pass
                layer = db.get_layer(name)
                # 独立属性表没有几何，导入后会变成不可绘制的空图层；跳过。
                gt = str(getattr(layer, "geometry_type", "") or "").lower()
                if gt in ("", "null"):
                    continue
                gdf = _layer_to_gdf(layer)
                if gdf is not None and len(gdf):
                    out.append((name, gdf))
            if not out:
                raise ValueError("GDB 里没有可载入的要素（所有要素类为空或没有几何）。")
            return out
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        if "pyopenfilegdb" in str(e):
            raise
        raise ValueError(f"读取 FileGDB 失败：{e}") from e


def gdb_info(path) -> dict:
    """给导入弹窗看的头信息：要素总数、图层数、坐标系。"""
    try:
        with _open_gdb(path) as db:
            names = [str(n) for n in db.list_feature_classes()]
            total = 0
            geom_types: set[str] = set()
            crs = None
            for name in names:
                layer = db.get_layer(name)
                total += int(getattr(layer, "record_count", 0) or 0)
                gt = str(getattr(layer, "geometry_type", "") or "")
                if gt:
                    geom_types.add(gt)
                if crs is None:
                    crs = _gdb_crs(layer)
            return {
                "features": total,
                "geom": (f"{len(names)} 图层 · " + "/".join(sorted(geom_types))
                         if names else "0 图层"),
                "crs": crs.to_string() if crs is not None else "",
                "layers": names,
            }
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"读取 FileGDB 失败：{e}") from e
