"""矢量 provider：只读元信息 + 按 bbox 从源文件取要素。

这是 `Layer` 的「不把整层读进内存」路径，对应 QGIS 的
`QgsVectorDataProvider::getFeatures(QgsFeatureRequest::setFilterRect(...))`：

- `read_info()` 只读文件头（要素数、范围、CRS、字段），打开大 Shapefile/GPKG
  不再先整份解析；
- `iter_bbox()` 走 `pyogrio.open_arrow()`，让 OGR 只返回与屏幕范围相交的要素，
  并且按 RecordBatch 惰性产出；渲染线程解一批、画一批、丢一批，不物化整个窗口；
- `read_bbox()` 保留给需要 GeoDataFrame 的调用方；
- `read_all()` 仍保留完整读取，供算子、导出、拾取等确实需要全量数据的路径。

注意：provider 不是数据源插件系统，只覆盖 `pyogrio` 能读的常见矢量格式；
CSV / FileGDB（走 pyopenfilegdb 的独立实现）仍回退为整份读取。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import geopandas as gpd
import numpy as np
from pyproj import CRS, Transformer

#: provider 支持的源文件扩展名。`.gdb` 是文件夹，交给 app.gdb 的专用实现。
#: `.parquet` 是应用自己的溢出缓存（GeoParquet），pyogrio 没有驱动，
#: 这里用 pyarrow 直接按 batch 读，避免把几十万/上千万顶点整份读回来。
VECTOR_PROVIDER_EXTS = (".shp", ".gpkg", ".geojson", ".json", ".kml", ".gml",
                        ".parquet", ".pq")


class VectorProvider:
    """一个源文件（或其中的一个图层）的只读 provider。"""

    def __init__(self, path: str | Path, layer: str | None = None):
        self.path = Path(path)
        self.layer = layer
        self._lock = threading.RLock()
        self._is_parquet = self.path.suffix.lower() in (".parquet", ".pq")
        self._geom_name = "geometry"
        self._info = self._read_info()
        self.crs = self._parse_crs(self._info.get("crs"))
        self.encoding = self._info.get("encoding") or None
        fields = self._info.get("fields")
        self.fields = [str(c) for c in fields] if fields is not None else []
        self.geom_type = str(self._info.get("geometry_type") or "Unknown")
        try:
            self.count = int(self._info.get("features") or 0)
        except Exception:
            self.count = 0
        try:
            tb = self._info.get("total_bounds")
            self.bounds = tuple(float(v) for v in tb) if tb is not None else ()
        except Exception:
            self.bounds = ()
        if len(self.bounds) != 4:
            self.bounds = ()
        self._wgs_bounds: tuple[float, float, float, float] | None = None
        # GeoParquet 的 `geo` 元数据不带包围盒；旁车文件记一次 WGS84 范围，
        # 否则每次恢复都要为了“这个图层在不在视口里”把 614 MB 几何扫一遍。
        if self._is_parquet:
            self._read_bounds_sidecar()
        #: 渲染用「简化后的 WGS84 几何」缓存：bucket 容差 -> (geoms, bounds)。
        #: 44M 顶点的图层每次平移都重新 decode/simplify/transform 要数秒；
        #: 同一比例尺内复用整层简化结果，平移只做 bbox 过滤。
        self._render_cache: dict[float, tuple] = {}
        self._render_cache_bytes = 0
        self._render_cache_limit = 96 * 1024 * 1024
        #: 只加载属性列的小表，供大图层点击拾取查属性；绝不带几何。
        self._attrs_table = None
        #: 属性表打开时缓存的 pandas DataFrame（只含非几何列）。
        self._attrs_df = None
        #: 单要素几何缓存：属性表点行时 highlight + zoom 会连查两次。
        self._last_feature: tuple[int, object] | None = None
        # Shapefile 没有内建空间索引：后台补 .qix，后续 bbox 查询才不用全表扫。
        # 只写旁车文件，不阻塞当前打开。
        if self.path.suffix.lower() == ".shp":
            try:
                from .spatial_index import build_shapefile_index
                build_shapefile_index(self.path)
            except Exception:
                pass

    # ------------------------------------------------------------------ 元信息
    def _read_info(self) -> dict:
        if self._is_parquet:
            return self._read_parquet_info()
        import pyogrio

        kwargs = {}
        if self.layer:
            kwargs["layer"] = self.layer
        else:
            # 多图层数据源（GPKG/FileGDB）不带 layer 时，pyogrio 会报错；
            # 沿用 gpd.read_file 的默认行为：取第一个可读图层。
            try:
                layers = pyogrio.list_layers(str(self.path))
                if len(layers) > 1:
                    self.layer = str(layers[0][0])
                    kwargs["layer"] = self.layer
                elif len(layers) == 1:
                    self.layer = str(layers[0][0])
            except Exception:
                pass
        info = pyogrio.read_info(str(self.path), **kwargs)
        # GML 等驱动的要素数是惰性的（-1）；强制数一次，否则无法判断空图层
        try:
            if int(info.get("features") or 0) < 0:
                info = pyogrio.read_info(
                    str(self.path), force_feature_count=True, **kwargs)
        except Exception:
            pass
        return dict(info)

    def _read_parquet_info(self) -> dict:
        """GeoParquet 只读文件元数据，不在打开时扫全部几何。

        矢量缓存是 `gpd.to_parquet()` 写出的；pyogrio 没有 parquet 驱动。
        直接读 `geo` 元数据拿几何列/CRS/字段，渲染时再按 batch 解码 WKB。
        """
        import json
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(self.path)
        md = pf.metadata.metadata or {}
        try:
            geo = json.loads(md.get(b"geo", b"{}") or b"{}")
        except Exception:
            geo = {}
        self._geom_name = str(geo.get("primary_column") or "geometry")
        col_meta = (geo.get("columns") or {}).get(self._geom_name) or {}
        crs = col_meta.get("crs")
        names = list(pf.schema_arrow.names)
        fields = [n for n in names if n != self._geom_name and n != "__index_level_0__"]
        return {
            "features": int(pf.metadata.num_rows),
            "fields": fields,
            "crs": crs,
            "total_bounds": None,
            "geometry_type": "Unknown",
            "driver": "Parquet",
            "encoding": None,
        }

    # ------------------------------------------------------------------ 范围旁车
    @staticmethod
    def _bounds_sidecar_path(path) -> "Path":
        return Path(str(path) + ".bounds.json")

    def _read_bounds_sidecar(self) -> None:
        """读 GeoParquet 旁车里的 WGS84 范围；没有就保持未知，绝不在这里扫几何。"""
        try:
            data = json.loads(
                self._bounds_sidecar_path(self.path).read_text(encoding="utf-8"))
        except Exception:
            return
        try:
            wb = data.get("wgs_bounds")
            if wb is not None and len(wb) == 4:
                self._wgs_bounds = tuple(float(v) for v in wb)
        except Exception:
            self._wgs_bounds = None

    def write_bounds_sidecar(self) -> None:
        """把已算出的 WGS84 范围写进旁车，后续恢复不再扫几何。"""
        if not self._is_parquet or self._wgs_bounds is None:
            return
        try:
            path = self._bounds_sidecar_path(self.path)
            tmp = path.with_suffix(path.suffix + ".tmp")
            import json as _json
            tmp.write_text(_json.dumps(
                {"wgs_bounds": [float(v) for v in self._wgs_bounds]}),
                encoding="utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            pass

    @property
    def wgs_bounds_hint(self) -> tuple[float, float, float, float] | None:
        """已缓存/文件头已知的 WGS84 范围；不触发 GeoParquet 全量几何扫描。"""
        return self._wgs_bounds

    @staticmethod
    def _release_arrow_memory() -> None:
        """把 PyArrow/mimalloc 已释放但未归还给 OS 的页交回去。

        `ParquetFile` 在关闭前会保留读过的 row group/column chunk；删掉它之后
        `release_unused()` 能把工作集从 ~0.8GB 拉回 ~0.1GB。
        """
        try:
            import pyarrow as pa
            pa.default_memory_pool().release_unused()
        except Exception:
            pass

    def _parquet_row_group_batches(self, columns, batch_size: int):
        """按 row group 逐个读，读完立即释放 ParquetFile 持有的 column chunk。

        同一个 `ParquetFile.iter_batches()` 扫多个 row group 时，PyArrow 会把
        读过的 column chunk 一直留在对象里；按组重开/销毁才能让峰值只跟单个
        row group 成正比。
        """
        import pyarrow.parquet as pq

        pf0 = None
        try:
            pf0 = pq.ParquetFile(self.path)
            nrg = int(pf0.metadata.num_row_groups)
        except Exception:
            return
        finally:
            try:
                del pf0
            except Exception:
                pass
            self._release_arrow_memory()

        for rg in range(nrg):
            pf = None
            try:
                pf = pq.ParquetFile(self.path)
                yield from pf.iter_batches(
                    batch_size=batch_size, row_groups=[rg], columns=columns)
            except Exception:
                continue
            finally:
                try:
                    del pf
                except Exception:
                    pass
                self._release_arrow_memory()

    def _ensure_parquet_bounds(self) -> None:
        """首次需要范围时才扫几何，仍按 batch 算 min/max，不物化整层。"""
        if self.bounds:
            return
        import numpy as np
        import shapely

        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        try:
            # 这个图层平均每个多边形 2000+ 顶点，一批 8192 个要素仅 shapely
            # 对象就要 1 GB+。范围扫描用更小的 batch，峰值和 batch 大小成正比。
            for batch in self._parquet_row_group_batches(
                    columns=[self._geom_name], batch_size=256):
                wkb = batch.column(0).to_numpy(zero_copy_only=False)
                geoms = shapely.from_wkb(wkb)
                geoms = np.asarray(geoms)
                if len(geoms) == 0:
                    continue
                b = shapely.bounds(geoms)
                if b is None or len(b) == 0:
                    continue
                valid = np.isfinite(b).all(axis=1)
                if not valid.any():
                    continue
                b = b[valid]
                x0 = min(x0, float(b[:, 0].min()))
                y0 = min(y0, float(b[:, 1].min()))
                x1 = max(x1, float(b[:, 2].max()))
                y1 = max(y1, float(b[:, 3].max()))
        except Exception:
            pass
        finally:
            self._release_arrow_memory()
        if x0 <= x1 and y0 <= y1:
            self.bounds = (x0, y0, x1, y1)

    @staticmethod
    def _parse_crs(value) -> CRS | None:
        if value is None or value == "":
            return None
        try:
            if isinstance(value, dict):
                return CRS.from_json_dict(value)
            return CRS.from_user_input(value)
        except Exception:
            return None

    @property
    def wgs_bounds(self) -> tuple[float, float, float, float] | None:
        """源文件范围转换到 WGS84；失败时退回源坐标范围。"""
        if self._wgs_bounds is not None:
            return self._wgs_bounds
        if not self.bounds and self._is_parquet:
            self._ensure_parquet_bounds()
        if not self.bounds:
            return None
        if self.crs is None:
            self._wgs_bounds = self.bounds  # 无 CRS：沿用现有「按经纬度看」的约定
            return self._wgs_bounds
        try:
            if self.crs.to_epsg() == 4326:
                self._wgs_bounds = self.bounds
            else:
                t = Transformer.from_crs(self.crs, 4326, always_xy=True)
                self._wgs_bounds = tuple(
                    float(v) for v in t.transform_bounds(*self.bounds, densify_pts=21))
        except Exception:
            self._wgs_bounds = self.bounds
        if self._is_parquet:
            self.write_bounds_sidecar()
        return self._wgs_bounds

    def meta(self) -> dict:
        crs_name = self.crs.name if self.crs is not None else "未定义"
        epsg = None
        if self.crs is not None:
            try:
                epsg = self.crs.to_epsg()
            except Exception:
                epsg = None
        return {
            "count": self.count,
            "geomType": self.geom_type,
            "crs": f"EPSG:{epsg}" if epsg else crs_name,
            "crsName": crs_name,
            "bounds": list(self.bounds),
            "fields": list(self.fields),
        }

    # ------------------------------------------------------------------ 读取
    def _bbox_source(self, bbox4326: tuple[float, float, float, float]):
        """WGS84 bbox → 源 CRS bbox；无 CRS 时原样返回。"""
        try:
            epsg = self.crs.to_epsg() if self.crs is not None else None
        except Exception:
            epsg = None
        if self.crs is None or epsg == 4326:
            return tuple(float(v) for v in bbox4326)
        try:
            t = Transformer.from_crs(4326, self.crs, always_xy=True)
            x0, y0, x1, y1 = (float(v) for v in bbox4326)
            # 四条边各取几个点：投影后的 bbox 不一定是矩形，只取角点会漏边
            xs = [x0 + (x1 - x0) * i / 8 for i in range(9)]
            ys = [y0 + (y1 - y0) * i / 8 for i in range(9)]
            pts = [(x, y0) for x in xs] + [(x, y1) for x in xs] + \
                  [(x0, y) for y in ys] + [(x1, y) for y in ys]
            tx, ty = t.transform([p[0] for p in pts], [p[1] for p in pts])
            return (min(tx), min(ty), max(tx), max(ty))
        except Exception:
            return tuple(float(v) for v in bbox4326)

    def read_bbox(self, bbox4326: tuple[float, float, float, float],
                  columns: list[str] | None = None) -> gpd.GeoDataFrame:
        """读取与 WGS84 bbox 相交的要素，统一返回 EPSG:4326。"""
        if self._is_parquet:
            parts = list(self.iter_bbox(bbox4326, columns=columns))
            if not parts:
                return gpd.GeoDataFrame({"geometry": []}, crs="EPSG:4326")
            geoms = np.concatenate(parts) if len(parts) > 1 else parts[0]
            return gpd.GeoDataFrame({"geometry": list(geoms)}, crs="EPSG:4326")
        bbox = self._bbox_source(bbox4326)
        with self._lock:
            gdf = self._read_bbox_raw(bbox, columns)
        if gdf is None or len(gdf) == 0:
            return gdf
        try:
            if gdf.crs is None and self.crs is not None:
                gdf = gdf.set_crs(self.crs)
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(4326)
        except Exception:
            pass
        return gdf

    def iter_bbox(self, bbox4326: tuple[float, float, float, float],
                  batch_size: int = 4096,
                  columns: list[str] | None = None,
                  simplify_tol: float = 0.0):
        """按批次惰性产出与 bbox 相交的几何（EPSG:4326）。

        `simplify_tol` 是渲染用的 WGS84 容差（度）。对投影坐标图层，会在
        **源 CRS 里先简化再投影**：直接投影 4400 万顶点要数秒，简化后通常
        只剩几万个；这是缩放/平移能及时恢复的关键。`read_bbox()` 不传这个
        参数，所以属性读取/空间查询拿到的仍是完整几何。

        这是渲染线程真正需要的接口：对应 QGIS 的
        `QgsFeatureIterator` + `QgsFeatureRequest::setFilterRect()`，
        不把整个窗口物化成 GeoDataFrame。

        实现走 `pyogrio.open_arrow()`：OGR 自己流式产出 RecordBatch，
        每批只把 WKB 解成 shapely 数组。GPU/CPU 侧没有“整层几何”驻留，
        取消也只需要在批边界丢掉生成器。

        pyarrow / open_arrow 不可用时退回 `read_bbox()` 的单批结果，
        保证老环境仍能渲染，只是没有逐批读取的内存优势。
        """
        if self._is_parquet:
            if simplify_tol > 0:
                yield from self._iter_cached_parquet_bbox(
                    bbox4326, batch_size, simplify_tol)
            else:
                yield from self._iter_parquet_bbox(bbox4326, batch_size)
            return
        try:
            import pyarrow as pa
            import pyogrio
            import shapely
        except Exception:
            gdf = self.read_bbox(bbox4326, columns=columns)
            if gdf is not None and len(gdf):
                yield np.asarray(gdf.geometry)
            return

        bbox = self._bbox_source(bbox4326)
        opts = {
            "bbox": tuple(bbox),
            "columns": list(columns) if columns is not None else [],
            "batch_size": max(1, int(batch_size)),
        }
        if self.layer:
            opts["layer"] = self.layer
        if self.encoding:
            opts["encoding"] = self.encoding
        transformer = self._wgs_transformer()
        yielded = False
        try:
            # 不在这里拿 self._lock：生成器可能被随时放弃，锁会跟着生成器
            # 的生命周期悬着。provider 是只读的，OGR 每次 open 都有自己的句柄。
            with pyogrio.open_arrow(str(self.path), **opts) as (meta, stream):
                reader = pa.RecordBatchReader.from_stream(stream)
                for batch in reader:
                    if batch.num_rows == 0:
                        continue
                    gi = self._geometry_index(batch.schema)
                    wkb = batch.column(gi).to_numpy(zero_copy_only=False)
                    geoms = shapely.from_wkb(wkb)
                    # OGR 路径（Shapefile/GDB）保持「先投影、后简化」，和整份
                    # 读取渲染一致；源 CRS 预简化只用于投影坐标的 GeoParquet
                    # 大图层，避免属性/水面等细节出现可见偏差。
                    if transformer is not None:
                        geoms = self._transform_geoms(geoms, transformer)
                    yielded = True
                    yield np.asarray(geoms)
        except Exception:
            # 某些驱动（老 GML/KML）不支持 Arrow 流式；只在还没产出任何批次时
            # 退回一次批量读取，避免中途失败造成重复要素。
            if yielded:
                return
            gdf = self.read_bbox(bbox4326, columns=columns)
            if gdf is not None and len(gdf):
                yield np.asarray(gdf.geometry)

    def iter_bbox_attrs(self, bbox4326: tuple[float, float, float, float],
                        columns: list[str], batch_size: int = 2048):
        """按批产出 `(geoms, {列名: ndarray})`，供标注这类「几何 + 属性」查询。

        和 `iter_bbox` 的区别只有一个：把请求的属性列一起读出来。它仍然
        按 bbox 流式产出 —— 标注只要视野里的前几百个要素，调用方取够就
        `break`，不会为了标几个名字把整个窗口的几何/属性都物化出来。
        """
        cols = [str(c) for c in (columns or []) if str(c)]
        if self._is_parquet:
            yield from self._iter_parquet_bbox_attrs(bbox4326, cols, batch_size)
            return
        try:
            import pyarrow as pa
            import pyogrio
            import shapely
        except Exception:
            gdf = self.read_bbox(bbox4326, columns=cols or None)
            if gdf is not None and len(gdf):
                attrs = {c: gdf[c].to_numpy() for c in cols if c in gdf.columns}
                yield np.asarray(gdf.geometry), attrs
            return

        bbox = self._bbox_source(bbox4326)
        opts = {
            "bbox": tuple(bbox),
            "columns": cols,
            "batch_size": max(1, int(batch_size)),
        }
        if self.layer:
            opts["layer"] = self.layer
        if self.encoding:
            opts["encoding"] = self.encoding
        transformer = self._wgs_transformer()
        yielded = False
        try:
            with pyogrio.open_arrow(str(self.path), **opts) as (meta, stream):
                reader = pa.RecordBatchReader.from_stream(stream)
                for batch in reader:
                    if batch.num_rows == 0:
                        continue
                    gi = self._geometry_index(batch.schema)
                    wkb = batch.column(gi).to_numpy(zero_copy_only=False)
                    geoms = np.asarray(shapely.from_wkb(wkb))
                    if transformer is not None:
                        geoms = self._transform_geoms(geoms, transformer)
                    attrs = {}
                    for cname in cols:
                        ci = batch.schema.get_field_index(cname)
                        if ci >= 0:
                            attrs[cname] = batch.column(ci).to_numpy(zero_copy_only=False)
                    yielded = True
                    yield geoms, attrs
        except Exception:
            if yielded:
                return
            gdf = self.read_bbox(bbox4326, columns=cols or None)
            if gdf is not None and len(gdf):
                attrs = {c: gdf[c].to_numpy() for c in cols if c in gdf.columns}
                yield np.asarray(gdf.geometry), attrs

    @staticmethod
    def _line_mid_angle(geom):
        """线要素 → `((x, y), 角度)`，源 CRS 里算；非线/退化几何返回 `(None, 0.0)`。

        锚点取线中点（不是包围盒中心），角度取中点附近线段方向；角度归一化到
        (-90, 90]，字不会倒着写。源 CRS 是投影坐标时（如 CGCS2000 高斯-
        克吕格）局部角度和 WGS84 里一致 —— 横轴墨卡托保角，不需要变换几何。
        """
        if geom is None or getattr(geom, "is_empty", True):
            return None, 0.0
        gt = getattr(geom, "geom_type", "")
        if gt == "LineString":
            line = geom
        elif gt == "MultiLineString":
            try:
                line = max(geom.geoms, key=lambda g: g.length)
            except Exception:  # noqa: BLE001
                return None, 0.0
        else:
            return None, 0.0
        try:
            if line.length <= 0:
                return None, 0.0
            p_mid = line.interpolate(0.5, normalized=True)
            p1 = line.interpolate(0.45, normalized=True)
            p2 = line.interpolate(0.55, normalized=True)
            dx, dy = p2.x - p1.x, p2.y - p1.y
            if abs(dx) < 1e-12 and abs(dy) < 1e-12:
                coords = list(line.coords)
                dx = coords[-1][0] - coords[0][0]
                dy = coords[-1][1] - coords[0][1]
            if abs(dx) < 1e-12 and abs(dy) < 1e-12:
                return (p_mid.x, p_mid.y), 0.0
            import math
            ang = math.degrees(math.atan2(dy, dx))
            if ang > 90.0:
                ang -= 180.0
            elif ang <= -90.0:
                ang += 180.0
            return (p_mid.x, p_mid.y), ang
        except Exception:  # noqa: BLE001
            return None, 0.0

    def _raw_attr_batches(self, columns: list[str], bbox=None,
                          batch_size: int = 2048):
        """按批产出**源 CRS** 的 `(geoms, attrs)`，不做坐标变换。

        `bbox` 是源 CRS 的 `(x0, y0, x1, y1)`，None 表示整层。和
        `iter_bbox_attrs` 的区别是不把整条几何的每个顶点都投影过去 ——
        标注只要锚点，投影整个几何在 614MB 的面上要 11 秒。
        """
        import numpy as np
        import shapely

        cols = [str(c) for c in (columns or []) if str(c)]
        if self._is_parquet:
            bs = min(max(1, int(batch_size)), 512)
            for batch in self._parquet_row_group_batches(
                    columns=[self._geom_name, *cols], batch_size=bs):
                if batch.num_rows == 0:
                    continue
                gi = batch.schema.get_field_index(self._geom_name)
                if gi < 0:
                    continue
                wkb = batch.column(gi).to_numpy(zero_copy_only=False)
                geoms = np.asarray(shapely.from_wkb(wkb))
                if len(geoms) == 0:
                    continue
                mask = None
                if bbox is not None:
                    try:
                        b = shapely.bounds(geoms)
                        mask = ((b[:, 2] >= bbox[0]) & (b[:, 0] <= bbox[2])
                                & (b[:, 3] >= bbox[1]) & (b[:, 1] <= bbox[3]))
                        if not mask.any():
                            continue
                    except Exception:  # noqa: BLE001
                        mask = None
                if mask is not None:
                    geoms = geoms[mask]
                attrs = {}
                for cname in cols:
                    ci = batch.schema.get_field_index(cname)
                    if ci < 0:
                        continue
                    arr = batch.column(ci).to_numpy(zero_copy_only=False)
                    attrs[cname] = arr[mask] if mask is not None else arr
                yield geoms, attrs
            return
        try:
            import pyarrow as pa
            import pyogrio
        except Exception:  # noqa: BLE001
            return
        opts = {"columns": cols, "batch_size": max(1, int(batch_size))}
        if bbox is not None:
            opts["bbox"] = tuple(float(v) for v in bbox)
        if self.layer:
            opts["layer"] = self.layer
        if self.encoding:
            opts["encoding"] = self.encoding
        try:
            with pyogrio.open_arrow(str(self.path), **opts) as (meta, stream):
                reader = pa.RecordBatchReader.from_stream(stream)
                for batch in reader:
                    if batch.num_rows == 0:
                        continue
                    gi = self._geometry_index(batch.schema)
                    wkb = batch.column(gi).to_numpy(zero_copy_only=False)
                    geoms = np.asarray(shapely.from_wkb(wkb))
                    attrs = {}
                    for cname in cols:
                        ci = batch.schema.get_field_index(cname)
                        if ci >= 0:
                            attrs[cname] = batch.column(ci).to_numpy(zero_copy_only=False)
                    yield geoms, attrs
        except Exception:  # noqa: BLE001
            return

    def iter_label_anchors(self, columns, bbox4326=None, batch_size: int = 2048):
        """按批产出 `(lons, lats, angles, attrs)`：标注锚点（WGS84）。

        锚点在**源 CRS** 里算（包围盒中心 / 线中点 + 切线角），最后只把
        「一条要素一个点」投影到 WGS84。以前为了标一个名字要把要素的每个
        顶点都转一遍，整层村界（614MB）一次 11.5s；现在只剩读 WKB + 求
        bounds 的成本。
        """
        import numpy as np
        import shapely

        cols = [str(c) for c in (columns or []) if str(c)]
        transformer = self._wgs_transformer()
        bbox = None
        if bbox4326 is not None:
            try:
                bbox = tuple(float(v) for v in self._bbox_source(tuple(bbox4326)))
            except Exception:  # noqa: BLE001
                bbox = None
        is_line = "Line" in str(self.geom_type or "")
        for geoms, attrs in self._raw_attr_batches(cols, bbox, batch_size):
            if geoms is None or len(geoms) == 0:
                continue
            try:
                b = np.asarray(shapely.bounds(geoms), dtype=float)
            except Exception:  # noqa: BLE001
                continue
            ok = np.isfinite(b).all(axis=1)
            if not ok.all():
                b = b[ok]
                geoms = np.asarray(geoms, dtype=object)[ok]
                attrs = {k: np.asarray(v)[ok] for k, v in attrs.items()}
            if len(b) == 0:
                continue
            xs = (b[:, 0] + b[:, 2]) * 0.5
            ys = (b[:, 1] + b[:, 3]) * 0.5
            ang = np.zeros(len(b), dtype=float)
            if is_line:
                garr = np.asarray(geoms, dtype=object)
                for j in range(len(garr)):
                    pt, a = self._line_mid_angle(garr[j])
                    if pt is None:
                        continue
                    xs[j], ys[j], ang[j] = pt[0], pt[1], a
            if transformer is not None:
                try:
                    xs, ys = transformer.transform(xs, ys)
                except Exception:  # noqa: BLE001
                    continue
            yield (np.asarray(xs, dtype=float), np.asarray(ys, dtype=float),
                   ang, attrs)

    def _iter_parquet_bbox_attrs(self, bbox4326, columns: list[str],
                                 batch_size: int = 2048):
        """GeoParquet 版的 `iter_bbox_attrs`：逐 row group 读几何 + 属性。"""
        import pyarrow.parquet as pq
        import shapely

        bbox = self._bbox_source(bbox4326)
        x0, y0, x1, y1 = (float(v) for v in bbox)
        transformer = self._wgs_transformer()
        try:
            bs = min(max(1, int(batch_size)), 512)
            for batch in self._parquet_row_group_batches(
                    columns=[self._geom_name, *columns], batch_size=bs):
                if batch.num_rows == 0:
                    continue
                gi = batch.schema.get_field_index(self._geom_name)
                if gi < 0:
                    continue
                wkb = batch.column(gi).to_numpy(zero_copy_only=False)
                geoms = np.asarray(shapely.from_wkb(wkb))
                if len(geoms) == 0:
                    continue
                mask = None
                try:
                    b = shapely.bounds(geoms)
                    mask = ((b[:, 2] >= x0) & (b[:, 0] <= x1)
                            & (b[:, 3] >= y0) & (b[:, 1] <= y1))
                    if not mask.any():
                        continue
                except Exception:
                    mask = None
                if mask is not None:
                    geoms = geoms[mask]
                if len(geoms) == 0:
                    continue
                if transformer is not None:
                    geoms = self._transform_geoms(geoms, transformer)
                attrs = {}
                for cname in columns:
                    ci = batch.schema.get_field_index(cname)
                    if ci < 0:
                        continue
                    arr = batch.column(ci).to_numpy(zero_copy_only=False)
                    attrs[cname] = arr[mask] if mask is not None else arr
                yield geoms, attrs
        except Exception:
            return

    @staticmethod
    def _tolerance_bucket(tol_wgs: float) -> float:
        """把连续的屏幕容差量化成 2 的幂次档位，平移/轻微缩放可复用缓存。"""
        try:
            import math
            t = max(float(tol_wgs), 1e-9)
            level = round(math.log2(t / 1e-6))
            return float(2 ** level * 1e-6)
        except Exception:
            return float(tol_wgs)

    def _iter_cached_parquet_bbox(self, bbox4326, batch_size, simplify_tol):
        """按简化档位缓存整层 WGS84 几何；同一档位的平移只做 bbox 过滤。

        缓存只存简化后的几何（44M 顶点降到十几万），不是原始全量；
        超过 `_render_cache_limit` 按最早写入淘汰。
        """
        import numpy as np
        import shapely

        bucket = self._tolerance_bucket(simplify_tol)
        with self._lock:
            use_key = self._pick_cached_bucket(bucket)
            entry = (self._render_cache.get(use_key)
                     if use_key is not None else None)
        if entry is not None:
            # 允许复用 2.5 倍容差以内的相邻档位：缩放一档时先用略粗的
            # 简化结果立即出图，等跨过 4 倍档位再重建，避免每级都等 4 秒。
            geoms, bounds, _indices = entry
            # 缓存里存的是简化后的几何，包围盒会比原始几何略向内缩；
            # 按简化容差外扩，避免视口边缘的要素被误过滤掉。
            yield from self._filter_cached(geoms, bounds, bbox4326, batch_size,
                                           pad=float(use_key) * 2.0)
            return

        # 缓存必须是整层范围，不能只缓存当前视口：否则平移到视口外就失效。
        full_bbox = None
        try:
            full_bbox = self.wgs_bounds
        except Exception:
            full_bbox = None
        if not full_bbox:
            full_bbox = tuple(bbox4326)

        geoms_out = []
        bounds_out = []
        indices_out = []
        nbytes = 0
        try:
            for batch, idxs in self._iter_parquet_bbox(
                    full_bbox, batch_size=min(max(1, int(batch_size)), 512),
                    simplify_tol=bucket, with_indices=True):
                if batch is None or len(batch) == 0:
                    continue
                try:
                    g = np.asarray(batch)
                    if g.dtype == object:
                        g = np.asarray(list(g), dtype=object)
                    b = shapely.bounds(g)
                except Exception:
                    continue
                geoms_out.append(g)
                bounds_out.append(b)
                indices_out.append(np.asarray(idxs, dtype=np.int64))
                try:
                    nbytes += int(shapely.get_num_coordinates(g).sum()) * 16
                except Exception:
                    nbytes += len(g) * 64
                yield from self._filter_cached(g, b, bbox4326, batch_size,
                                               pad=float(bucket) * 2.0)
        except Exception:
            pass

        if not geoms_out:
            return
        try:
            geoms = np.concatenate(geoms_out)
            bounds = np.concatenate(bounds_out)
            indices = np.concatenate(indices_out)
        except Exception:
            return
        with self._lock:
            if bucket in self._render_cache:
                return
            self._render_cache[bucket] = (geoms, bounds, indices)
            self._render_cache_bytes += int(nbytes)
            # 只保留当前比例尺附近的几个档位；超出按写入顺序淘汰。
            while (self._render_cache_bytes > self._render_cache_limit
                   and len(self._render_cache) > 1):
                old_key = next(iter(self._render_cache))
                if old_key == bucket:
                    old_key = next(reversed(self._render_cache))
                old_geoms, _old_bounds, _old_idx = self._render_cache.pop(old_key)
                try:
                    self._render_cache_bytes -= int(
                        shapely.get_num_coordinates(old_geoms).sum()) * 16
                except Exception:
                    self._render_cache_bytes -= 0
                self._render_cache_bytes = max(0, self._render_cache_bytes)

    def _pick_cached_bucket(self, target: float) -> float | None:
        """在已有简化档位里找一个与目标相差不超过 2.5 倍的缓存。

        优先选更粗（顶点更少、更快）的档位；没有就选更细的。这样平移、
        轻微滚轮和跨一级缩放都不会重新 decode 4400 万顶点。
        """
        best = None
        for key in self._render_cache:
            if target <= 0:
                continue
            ratio = float(key) / float(target)
            if not (1.0 / 2.5) <= ratio <= 2.5:
                continue
            # key 越大容差越粗、顶点越少。优先用最粗的可用档位；
            # 如果只有更细的可用，也取其中最接近目标（最大 key）的。
            if best is None or key > best:
                best = key
        return best

    @staticmethod
    def _filter_cached(geoms, bounds, bbox4326, batch_size, pad: float = 0.0):
        """从整层缓存里按 WGS84 bbox 取要素，按批 yield。

        `pad` 用于补偿简化造成的包围盒内缩（单位：度）。
        """
        import numpy as np

        if geoms is None or len(geoms) == 0:
            return
        x0, y0, x1, y1 = (float(v) for v in bbox4326)
        pad = max(0.0, float(pad))
        mask = ((bounds[:, 2] >= x0 - pad) & (bounds[:, 0] <= x1 + pad)
                & (bounds[:, 3] >= y0 - pad) & (bounds[:, 1] <= y1 + pad))
        if not bool(mask.any()):
            return
        sel = np.nonzero(mask)[0]
        step = max(1, int(batch_size))
        for i in range(0, len(sel), step):
            yield geoms[sel[i:i + step]]

    def _attr_columns(self) -> list[str]:
        return [str(f) for f in self.fields if str(f) != self._geom_name]

    def attrs_frame(self):
        """只读非几何列，返回 pandas DataFrame（行序 = 源文件行号）。

        这是属性表真正需要的接口：不能为了显示属性而调用 `Layer.gdf`，
        否则 GeoParquet 的 WKB 几何会被整份解码，21k 块 / 4480 万顶点的
        图层打开属性表就会多出数 GB。
        """
        if self._attrs_df is not None:
            return self._attrs_df
        cols = self._attr_columns()
        if not cols:
            import pandas as pd
            self._attrs_df = pd.DataFrame(index=range(max(0, int(self.count))))
            return self._attrs_df
        if self._is_parquet:
            import pyarrow.parquet as pq

            table = self._attrs_table
            if table is None:
                table = pq.read_table(self.path, columns=cols)
            df = table.to_pandas()
            # 同一份数据不要同时留 Arrow 和 pandas 两份；后面 attrs_at 走 df。
            self._attrs_table = None
            del table
            self._release_arrow_memory()
        else:
            import pyogrio

            opts = {"columns": cols, "read_geometry": False}
            if self.layer:
                opts["layer"] = self.layer
            if self.encoding:
                opts["encoding"] = self.encoding
            df = pyogrio.read_dataframe(self.path, **opts)
        df = df.reset_index(drop=True)
        self._attrs_df = df
        return df

    def feature_geom(self, row: int):
        """按源文件行号取单个 WGS84 几何；属性表点行的高亮/定位用。

        只解码包含这一行的 row group / 一个要素，不建整层索引。
        """
        try:
            row = int(row)
        except (TypeError, ValueError):
            return None
        if row < 0:
            return None
        if self._last_feature is not None and self._last_feature[0] == row:
            return self._last_feature[1]
        geom = None
        if self._is_parquet:
            import pyarrow.parquet as pq
            import shapely

            pf = None
            table = None
            try:
                pf = pq.ParquetFile(self.path)
                md = pf.metadata
                start = 0
                for rg in range(int(md.num_row_groups)):
                    n = int(md.row_group(rg).num_rows)
                    if start <= row < start + n:
                        table = pf.read_row_group(rg, columns=[self._geom_name])
                        wkb = table.column(0)[row - start].as_py()
                        if wkb is not None:
                            geom = shapely.from_wkb(wkb)
                        break
                    start += n
            finally:
                del table
                del pf
                self._release_arrow_memory()
        else:
            import pyogrio

            opts = {"read_geometry": True, "columns": [],
                    "skip_features": row, "max_features": 1}
            if self.layer:
                opts["layer"] = self.layer
            if self.encoding:
                opts["encoding"] = self.encoding
            try:
                gdf = pyogrio.read_dataframe(self.path, **opts)
                if gdf is not None and len(gdf):
                    geom = gdf.geometry.iloc[0]
            except Exception:
                geom = None
        if geom is None or getattr(geom, "is_empty", False):
            return None
        transformer = self._wgs_transformer()
        if transformer is not None:
            try:
                geom = self._transform_geoms(np.asarray([geom]), transformer)[0]
            except Exception:
                pass
        self._last_feature = (row, geom)
        return geom

    def attrs_at(self, row: int) -> dict:
        """按源文件行号取一行属性；只读非几何列，不加载几何。"""
        try:
            if self._attrs_df is not None:
                if row < 0 or row >= len(self._attrs_df):
                    return {}
                return self._attrs_df.iloc[int(row)].to_dict()
        except Exception:
            pass
        if not self._is_parquet:
            return {}
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq

            if self._attrs_table is None:
                cols = self._attr_columns()
                if not cols:
                    return {}
                self._attrs_table = pq.read_table(self.path, columns=cols)
            if row < 0 or row >= self._attrs_table.num_rows:
                return {}
            take = self._attrs_table.take(pa.array([int(row)]))
            vals = take.to_pylist()
            return dict(vals[0]) if vals else {}
        except Exception:
            return {}

    def hit_test(self, lon: float, lat: float, tol: float):
        """大图层点击命中的轻量查询。

        GeoParquet 用渲染 LOD 缓存里的简化几何找候选，再按源文件行号只读
        **属性列**；非 GeoParquet 走 provider 的小 bbox 读取。绝不调用
        `Layer.gdf`，否则 21k 块 / 4480 万顶点会瞬间把内存推到数 GB。
        返回 `(行号, 属性)` 或 None。
        """
        if tol <= 0:
            return None
        try:
            import numpy as np
            import shapely
            from shapely.geometry import Point
        except Exception:
            return None
        pt = Point(float(lon), float(lat))

        if self._is_parquet:
            with self._lock:
                if not self._render_cache:
                    return None
                # 用最细的一档做命中判断，误差最小
                key = min(self._render_cache)
                geoms, bounds, indices = self._render_cache[key]
            if geoms is None or len(geoms) == 0:
                return None
            pad = max(float(key) * 2.0, float(tol))
            mask = ((bounds[:, 0] <= lon + pad) & (bounds[:, 2] >= lon - pad)
                    & (bounds[:, 1] <= lat + pad) & (bounds[:, 3] >= lat - pad))
            sel = np.nonzero(mask)[0]
            if sel.size == 0:
                return None
            if sel.size > 3000:
                sel = sel[:3000]
            dist = shapely.distance(geoms[sel], pt)
            k = int(np.argmin(dist))
            if float(dist[k]) <= tol:
                row = int(indices[sel[k]])
                return row, self.attrs_at(row)
            return None

        # Shapefile / GPKG 等：只读光标周围的小 bbox，属性随小表返回
        try:
            gdf = self.read_bbox((lon - tol, lat - tol, lon + tol, lat + tol))
            if gdf is None or len(gdf) == 0:
                return None
            geoms = np.asarray(gdf.geometry)
            dist = shapely.distance(geoms, pt)
            k = int(np.argmin(dist))
            if float(dist[k]) > tol:
                return None
            row = gdf.iloc[k]
            props = row.to_dict()
            props.pop(gdf.geometry.name, None)
            return int(k), props
        except Exception:
            return None

    def _iter_parquet_bbox(self, bbox4326: tuple[float, float, float, float],
                           batch_size: int = 4096, simplify_tol: float = 0.0,
                           with_indices: bool = False):
        """包裹真实扫描，结束时把 ParquetFile 的 column chunk 内存还给 OS。"""
        try:
            yield from self._iter_parquet_bbox_inner(
                bbox4326, batch_size=batch_size, simplify_tol=simplify_tol,
                with_indices=with_indices)
        finally:
            self._release_arrow_memory()

    def _iter_parquet_bbox_inner(self, bbox4326: tuple[float, float, float, float],
                                 batch_size: int = 4096, simplify_tol: float = 0.0,
                                 with_indices: bool = False):
        """GeoParquet 的按 batch 渲染路径。

        没有空间索引时只能扫 batch，但每次只让一个 batch 的 WKB/几何驻留；
        这比 `gpd.read_parquet()` 把 2 万块、几千万顶点整体读进来低一个量级。
        """
        import numpy as np
        import shapely

        bbox = self._bbox_source(bbox4326)
        x0, y0, x1, y1 = (float(v) for v in bbox)
        transformer = self._wgs_transformer()
        # 大多边形图层不能按 4096 个要素解码：单个 batch 的 shapely 对象
        # 就能到几百 MB。按 batch 解码后仍然逐批产出，屏幕渲染不需要更大。
        bs = min(max(1, int(batch_size)), 512)
        row0 = 0
        # 渲染只吃几何；不要把属性列也读进 Arrow batch。
        for batch in self._parquet_row_group_batches(
                columns=[self._geom_name], batch_size=bs):
            n = int(batch.num_rows)
            start = row0
            row0 += n
            if n == 0:
                continue
            try:
                gi = batch.schema.get_field_index(self._geom_name)
                if gi < 0:
                    return
                wkb = batch.column(gi).to_numpy(zero_copy_only=False)
                geoms = np.asarray(shapely.from_wkb(wkb))
            except Exception:
                continue
            if len(geoms) == 0:
                continue
            idx = np.arange(start, start + len(geoms), dtype=np.int64)
            # 先按几何包围盒筛掉屏幕外要素；对 44M 顶点的图层也只是
            # 当前 batch 的 bounds 数组，不累积。
            try:
                b = shapely.bounds(geoms)
                mask = ((b[:, 2] >= x0) & (b[:, 0] <= x1)
                        & (b[:, 3] >= y0) & (b[:, 1] <= y1))
                if not mask.any():
                    continue
                geoms = geoms[mask]
                idx = idx[mask]
            except Exception:
                pass
            if simplify_tol > 0:
                geoms = self._simplify_source(geoms, simplify_tol, bbox4326)
            if transformer is not None:
                geoms = self._transform_geoms(geoms, transformer)
            if with_indices:
                yield np.asarray(geoms), idx
            else:
                yield np.asarray(geoms)

    def _wgs_transformer(self):
        """源 CRS → WGS84 的 pyproj Transformer；已是 4326 时返回 None。"""
        if self.crs is None:
            return None
        try:
            if self.crs.to_epsg() == 4326:
                return None
        except Exception:
            pass
        try:
            from pyproj import Transformer
            return Transformer.from_crs(self.crs, 4326, always_xy=True)
        except Exception:
            return None

    @staticmethod
    def _geometry_index(schema) -> int:
        """Arrow schema 里找 geoarrow.wkb 字段；找不到就退回第 0 列。"""
        for i, field in enumerate(schema):
            md = field.metadata or {}
            name = md.get(b"ARROW:extension:name", b"")
            if b"geoarrow" in name or b"wkb" in name:
                return i
        return 0

    def _simplify_source(self, geoms, tol_wgs: float, bbox4326: tuple):
        """在源 CRS 里按等效的 WGS84 容差做一次简化，再交给投影。

        投影是逐顶点成本；先简化能把 4400 万顶点降到几万，缩放时省掉数秒。
        容差从「度」换算成源单位：投影坐标按平均纬度换算米，再除以 CRS
        单位系数（米/英尺等）；地理坐标直接用度。
        """
        if tol_wgs <= 0:
            return geoms
        try:
            import math
            import shapely

            unit = 1.0
            projected = False
            if self.crs is not None:
                try:
                    projected = bool(self.crs.is_projected)
                    if projected:
                        unit = float(self.crs.axis_info[0].unit_conversion_factor) or 1.0
                except Exception:
                    projected = False
                    unit = 1.0
            if projected:
                lat = (float(bbox4326[1]) + float(bbox4326[3])) * 0.5
                tol_src = tol_wgs * 111320.0 * max(math.cos(math.radians(lat)), 1e-6)
                tol_src /= max(unit, 1e-9)
            else:
                tol_src = float(tol_wgs)
            return shapely.simplify(geoms, tol_src, preserve_topology=False)
        except Exception:
            return geoms

    @staticmethod
    def _transform_geoms(geoms, transformer):
        """批量坐标变换：一次 get_coordinates / transform / set_coordinates。"""
        import numpy as np
        import shapely

        coords = shapely.get_coordinates(geoms)
        if coords is None or len(coords) == 0:
            return geoms
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return shapely.set_coordinates(geoms, np.column_stack([x, y]))

    def _read_bbox_raw(self, bbox, columns=None) -> gpd.GeoDataFrame:
        import pyogrio

        opts = {"bbox": tuple(bbox)}
        if self.layer:
            opts["layer"] = self.layer
        if self.encoding:
            opts["encoding"] = self.encoding
        # 注意 columns=[] 是有效值（只要几何、不要属性）；不能用真值判断
        if columns is not None:
            opts["columns"] = list(columns)
        try:
            return pyogrio.read_dataframe(str(self.path), **opts)
        except Exception as first:
            # 部分驱动对 encoding/bbox 组合挑剔，退回 geopandas 的通用入口
            try:
                return gpd.read_file(self.path, **opts)
            except Exception:
                try:
                    return gpd.read_file(self.path, engine="fiona", **opts)
                except Exception:
                    raise ValueError(f"读取 {self.path.name} 失败：{first}") from first

    def read_all(self) -> gpd.GeoDataFrame:
        """完整读取（算子/导出/拾取等需要全量数据的路径）。"""
        if self._is_parquet:
            with self._lock:
                return gpd.read_parquet(self.path)

        from .io_utils import load_any, read_layer

        with self._lock:
            if self.layer:
                return read_layer(self.path, self.layer)
            return load_any(self.path)


def provider_for(path: str | Path, layer: str | None = None) -> VectorProvider | None:
    """按扩展名尝试创建 provider；不支持/打不开返回 None。"""
    p = Path(path)
    if p.suffix.lower() not in VECTOR_PROVIDER_EXTS:
        return None
    try:
        return VectorProvider(p, layer=layer)
    except Exception:
        return None
