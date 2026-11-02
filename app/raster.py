"""栅格图层：元信息 + 读取后端（rasterio）。

为什么单独一个文件
------------------
矢量这边的一切都建立在「一个 GeoDataFrame = 一个图层」上（`engine.Layer`）：
算子拿 gdf 做 overlay、属性表读 gdf 的列、渲染把 gdf 的几何转成 QPainterPath。
栅格没有「要素」这个概念，硬塞进 `Layer` 会让那 26 个矢量算子全部面对一个空 gdf。

所以这里给栅格一个**鸭子类型**的 `RasterLayer`：字段形状和 `Layer` 一致
（id / name / color / op / source / parent），`meta()` 返回同样结构，
图层面板、缩放定位、显隐这些地方不用改就能用；但 `gdf` 一访问就报错，
把「拿栅格去做矢量运算」变成一个**清晰的中文错误**，而不是一段 None 崩溃。

读取参照 QGIS 的 provider 语义：
· `read_window()` 对应 `QgsRasterDataProvider::readBlock()` —— 按目标像素尺寸
  抽取（decimation），只读屏幕需要的那几个像元，不是把 1 万×1 万整幅读进内存；
· `resampling` 对应 `QgsRasterResampleFilter` 的重采样方式；
· `percentile_stretch()` 对应 `QgsContrastEnhancement` 的累计裁剪拉伸。
"""
from __future__ import annotations

import time

import numpy as np

#: 栅格格式（QGIS 里是 GDAL 驱动名，这里用扩展名简化）
RASTER_EXTS = (".tif", ".tiff", ".img", ".vrt", ".jp2")

#: 重采样方式（名字对齐 QGIS 的 QgsRasterResampleFilter 枚举）
RESAMPLING = {
    "nearest": "nearest",      # 最近邻：分类栅格必须用它，否则颜色会串
    "bilinear": "bilinear",    # 双线性：连续数据（高程/影像）默认
    "cubic": "cubic",
    "average": "average",      # 缩小取平均，避免噪点
}

#: 单波段渲染模式
GRAY = "gray"            # 灰度拉伸
PSEUDO = "pseudo"        # 伪彩色


class RasterError(Exception):
    """栅格读取/使用中的可展示错误。"""


def is_raster_path(path) -> bool:
    from pathlib import Path
    return Path(str(path)).suffix.lower() in RASTER_EXTS


def _require_rasterio():
    try:
        import rasterio  # noqa: F401
        return rasterio
    except Exception as e:  # noqa: BLE001
        raise RasterError(
            "打开栅格需要 rasterio（它自带 GDAL）。请先在运行环境里安装：pip install rasterio"
        ) from e


class RasterInfo:
    """栅格元信息（一次性读头，不加载像素）。"""

    __slots__ = ("path", "width", "height", "bands", "dtypes", "crs", "crs_name",
                 "nodata", "bounds", "res", "band_desc", "driver")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def count(self) -> int:
        """像元总数（面板里想显示「要素数」时用它）。"""
        return int(self.width or 0) * int(self.height or 0)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def read_info(path) -> RasterInfo:
    """只读头信息（QGIS 的 QgsRasterLayer::dataProvider() 初始化那一步）。"""
    rasterio = _require_rasterio()
    from rasterio.warp import transform_bounds

    try:
        with rasterio.open(str(path)) as ds:
            crs = ds.crs
            # 边界统一转 WGS84：地图是 Web Mercator，只认经纬度
            if crs is not None:
                try:
                    b = transform_bounds(crs, "EPSG:4326", *ds.bounds, densify_pts=21)
                except Exception:
                    b = ds.bounds
            else:
                b = ds.bounds
            return RasterInfo(
                path=str(path),
                width=int(ds.width), height=int(ds.height),
                bands=int(ds.count),
                dtypes=[str(t) for t in (ds.dtypes or ())],
                crs=crs.to_string() if crs is not None else None,
                crs_name=crs.to_string() if crs is not None else "未定义",
                nodata=ds.nodata,
                bounds=(float(b[0]), float(b[1]), float(b[2]), float(b[3])),
                res=(abs(float(ds.res[0])), abs(float(ds.res[1]))),
                band_desc=[str(d) if d else "" for d in (ds.descriptions or ())],
                driver=ds.driver,
            )
    except RasterError:
        raise
    except Exception as e:  # noqa: BLE001
        raise RasterError(f"读不了这个栅格：{e}") from e


class RasterLayer:
    """一个栅格图层。字段形状对齐 `engine.Layer`，好让 Store / 面板直接复用。"""

    #: 让 `engine.Store` 的内部逻辑（_memory_mb / _trim_memory）安全跳过它
    _gdf = None
    cache_path = None

    def __init__(self, id: str, name: str, info: RasterInfo, color: str = "#8d897e",
                 source: str = "", parent: str = "", op: str = "raster",
                 created_at: float | None = None) -> None:
        self.id = id
        self.name = name
        self.color = color
        self.source = source
        self.parent = parent
        self.op = op
        self.created_at = created_at or time.time()
        self.info = info
        #: 渲染参数（用户以后可以改；现在给合理默认）
        self.style: dict = {
            "mode": GRAY if info.bands == 1 else "rgb",
            "bands": [1, 2, 3] if info.bands >= 3 else [1],
            "resampling": "bilinear" if info.bands >= 3 else "nearest",
            "stretch": "percentile",   # none / minmax / percentile
            "gamma": 1.0,
            "opacity": 1.0,
        }
        #: 改渲染参数时 +1，用于让渲染缓存失效（对齐 Layer 的 rev 语义）
        self.rev = 0
        self._last_used = time.time()
        #: 拉伸用的统计量（首次渲染时算，之后复用）
        self.stats: dict | None = None

    # ------------------------------------------------------------------ 类型
    @property
    def is_raster(self) -> bool:
        return True

    @property
    def gdf(self):
        raise RasterError(
            f"「{self.name}」是栅格图层，不能做矢量运算。"
            "可用的操作：缩放定位、属性（波段/尺寸/坐标系）、删除、导出原文件。")

    @property
    def geom_type(self) -> str:
        return "Raster"

    def meta(self, with_invalid: bool = False) -> dict:
        """元信息。结构对齐 `engine.Layer.meta()`，面板不用分叉。"""
        b = self.info.bounds or (0, 0, 0, 0)
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "count": self.info.count,
            "geomType": "Raster",
            "crs": self.info.crs_name or "未定义",
            "crsName": self.info.crs_name or "未定义",
            "bounds": [float(x) for x in b],
            "fields": [],
            "source": self.source,
            "parent": self.parent,
            "op": self.op,
            "invalid": None,
            # 面板/工具提示用
            "raster": {
                "width": self.info.width,
                "height": self.info.height,
                "bands": self.info.bands,
                "dtype": (self.info.dtypes or [""])[0],
                "driver": self.info.driver,
            },
        }


# --------------------------------------------------------------------------- 读


def _resampling_enum(name: str):
    from rasterio.enums import Resampling
    return getattr(Resampling, RESAMPLING.get(name, "bilinear"), Resampling.bilinear)


def read_window(layer: RasterLayer, extent_wgs84: tuple, out_w: int, out_h: int,
                bands: list[int] | None = None, resampling: str = "bilinear"
                ) -> tuple[np.ndarray, tuple] | None:
    """按 WGS84 范围抽取栅格，缩放到 (out_h, out_w)。

    对应 QGIS `QgsRasterIterator::nextPart()` + `readBlock()` 的语义：
    **只读目标分辨率需要的像元数**，靠 `out_shape` 做抽取。
    返回 (数组 [bands, out_h, out_w], 实际覆盖的 WGS84 范围) 或 None（不相交）。
    """
    rasterio = _require_rasterio()
    from rasterio.warp import transform_bounds

    x0, y0, x1, y1 = extent_wgs84
    if x1 <= x0 or y1 <= y0:
        return None
    if out_w < 1 or out_h < 1:
        return None

    with rasterio.open(layer.info.path) as ds:
        crs = ds.crs
        # WGS84 范围 → 栅格自己的 CRS
        if crs is not None and crs.to_epsg() != 4326:
            try:
                rb = transform_bounds("EPSG:4326", crs, x0, y0, x1, y1, densify_pts=21)
            except Exception:
                rb = (x0, y0, x1, y1)
        else:
            rb = (x0, y0, x1, y1)

        from rasterio.windows import from_bounds
        try:
            win = from_bounds(rb[0], rb[1], rb[2], rb[3], transform=ds.transform)
        except Exception:
            return None
        # 和栅格本身求交（视口可能比栅格大）
        full = rasterio.windows.Window(0, 0, ds.width, ds.height)
        try:
            win = win.intersection(full)
        except Exception:
            return None
        if win.width <= 0 or win.height <= 0:
            return None

        idxs = bands or (list(range(1, min(ds.count, 3) + 1)) if ds.count >= 3 else [1])
        try:
            arr = ds.read(
                indexes=idxs,
                window=win,
                out_shape=(len(idxs), max(1, int(out_h)), max(1, int(out_w))),
                resampling=_resampling_enum(resampling),
                boundless=False,
            )
        except Exception as e:  # noqa: BLE001
            raise RasterError(f"读栅格失败：{e}") from e

        # 这块实际覆盖的经纬度范围（供叠加定位用）
        try:
            wb = rasterio.windows.bounds(win, ds.transform)
            if crs is not None and crs.to_epsg() != 4326:
                wb = transform_bounds(crs, "EPSG:4326", *wb, densify_pts=21)
        except Exception:
            wb = (x0, y0, x1, y1)

    return arr, (float(wb[0]), float(wb[1]), float(wb[2]), float(wb[3]))


def percentile_stretch(arr: np.ndarray, lo_pct: float = 2.0, hi_pct: float = 98.0,
                       nodata=None) -> tuple[float, float]:
    """累计裁剪拉伸的上下限（QgsContrastEnhancement 的 CumulativeCut）。

    永远不要用 min/max：影像里一个坏点就能把整幅压成灰片。
    """
    a = arr.astype(np.float64, copy=False)
    valid = np.isfinite(a)
    if nodata is not None:
        valid &= a != nodata
    if not valid.any():
        return 0.0, 1.0
    v = a[valid]
    if v.size > 400_000:                      # 大图抽样，够用了
        v = v[:: max(1, v.size // 400_000)]
    lo = float(np.percentile(v, lo_pct))
    hi = float(np.percentile(v, hi_pct))
    if hi <= lo:
        lo, hi = float(v.min()), float(v.max())
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi
