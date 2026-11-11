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

import os
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


class RasterCancelled(Exception):
    """渲染被更新的视图取代；静默丢弃，不当作错误提示。"""


def _cancel_requested(cancel) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception:  # noqa: BLE001
        return False


def _check_cancel(cancel) -> None:
    if _cancel_requested(cancel):
        raise RasterCancelled()


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
                 "nodata", "bounds", "res", "band_desc", "driver", "overviews")

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
                overviews=tuple(int(x) for x in (ds.overviews(1) or ())
                                if int(x) > 1),
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
                 created_at: float | None = None, style: dict | None = None,
                 online: dict | None = None) -> None:
        self.id = id
        self.name = name
        self.color = color
        self.source = source
        self.parent = parent
        self.op = op
        self.created_at = created_at or time.time()
        self.info = info
        #: 在线底图配置（method/url/name...）；普通本地栅格为 None。
        #: 渲染器看到它就走瓦片/WMS 分支，绝不读 info.path。
        self.online = dict(online) if isinstance(online, dict) and online else None
        #: 渲染参数（用户在「图层样式」里改；默认值给常用配置）
        self.style: dict = {
            "mode": GRAY if info.bands == 1 else "rgb",
            "bands": [1, 2, 3] if info.bands >= 3 else [1],
            "resampling": "bilinear" if info.bands >= 3 else "nearest",
            "stretch": "percentile",   # none / minmax / percentile
            "gamma": 1.0,
            "opacity": 1.0,
        }
        if isinstance(style, dict):
            self.style.update({k: v for k, v in style.items() if not k.startswith("_")})
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
                "overviews": list(getattr(self.info, "overviews", ()) or ()),
                "online": dict(self.online) if self.online else None,
            },
            "online": dict(self.online) if self.online else None,
        }


# --------------------------------------------------------------------------- 读


def _resampling_enum(name: str):
    from rasterio.enums import Resampling
    return getattr(Resampling, RESAMPLING.get(name, "bilinear"), Resampling.bilinear)


#: 源窗口超过这个像元数就分块读。rasterio/GDAL 的 `read(out_shape=...)`
#: 在某些驱动上仍会先分配整幅源窗口，2 万×3 万的 TIFF 会一次吃掉 1~2 GB；
#: 分块后每次只解码 2048×2048（约 12 MB / 3 波段）。
MAX_DIRECT_SOURCE_PIXELS = 4_000_000
#: 分块读取按「目标图像块」切，而不是按源像元块切。这样每个块的源窗口
#: 边界落在全局输出网格的整数边界上，重采样相位不会在块与块之间错开。
READ_BLOCK_PIXELS = 256
#: 目标块外扩几个像素给 bilinear/cubic 核留余量，合成时只取核心区。
READ_BLOCK_MARGIN = 2
#: GDAL 块缓存上限（MB）。默认值不设时 GDAL 会按物理内存比例放大，
#: 大影像渲染时看到的是 GB 级峰值；64~256 MB 足够分块读取。
try:
    GDAL_CACHE_MB = max(16, int(os.environ.get("GIS_AGENT_GDAL_CACHE_MB", "128")))
except ValueError:
    GDAL_CACHE_MB = 128


def _read_window_blocks(ds, win, idxs: list[int], out_w: int, out_h: int,
                        resampling, cancel=None) -> np.ndarray:
    """按目标图像块读源窗口（QGIS `QgsRasterIterator` 的目标尺寸版）。

    关键不是“源块 2048”，而是每个块的源窗口边界必须落在**全局输出网格**
    的整数边界上。按源块切时，每个源块自己的 `out_shape` 比例并不等于
    全局比例，nearest 会在块边界整段错位；按目标块切并传浮点 window，
    GDAL 的像元中心映射和整窗读取完全一致。

    目标块 256×256，外扩 2 px 给双线性核留余量；源窗口大小仍由当前
    缩放比例决定，所以峰值内存和“输出 256×256 对应的源区域”成正比。
    """
    from rasterio.windows import Window

    out_w = max(1, int(out_w))
    out_h = max(1, int(out_h))
    dtype = np.result_type(*[np.dtype(ds.dtypes[i - 1]) for i in idxs])
    out = np.zeros((len(idxs), out_h, out_w), dtype=dtype)
    if win.width <= 0 or win.height <= 0:
        return out

    step = max(32, int(READ_BLOCK_PIXELS))
    margin = max(0, int(READ_BLOCK_MARGIN))

    def src_span(t0: int, t1: int, off: float, span: float, n: int) -> tuple[float, float]:
        return (off + (t0 / n) * span, off + (t1 / n) * span)

    for ty0 in range(0, out_h, step):
        _check_cancel(cancel)
        ty1 = min(out_h, ty0 + step)
        ey0 = max(0, ty0 - margin)
        ey1 = min(out_h, ty1 + margin)
        sy0, sy1 = src_span(ey0, ey1, win.row_off, win.height, out_h)
        for tx0 in range(0, out_w, step):
            tx1 = min(out_w, tx0 + step)
            ex0 = max(0, tx0 - margin)
            ex1 = min(out_w, tx1 + margin)
            sx0, sx1 = src_span(ex0, ex1, win.col_off, win.width, out_w)
            block = ds.read(
                indexes=idxs,
                window=Window(sx0, sy0, sx1 - sx0, sy1 - sy0),
                out_shape=(len(idxs), ey1 - ey0, ex1 - ex0),
                resampling=resampling,
                boundless=False,
            )
            # 丢掉外扩的边，只写回本目标块的核心区
            out[:, ty0:ty1, tx0:tx1] = block[
                :, ty0 - ey0:ty1 - ey0, tx0 - ex0:tx1 - ex0]
    return out


def select_overview_index(overviews, decimation: float) -> int | None:
    """按目标抽取倍率挑最合适的 overview 层，返回 `overview_level` 索引。

    QGIS 的 `QgsRasterDataProvider::bestOverviewForScale()` 做的是同一件事：
    只选**不粗于**目标分辨率的层。例如抽取倍率 6 用 factor 4，不用 8，
    否则屏幕像素比源像素还多，白白损失清晰度。
    """
    if not overviews or decimation <= 1.0:
        return None
    best = None
    for i, f in enumerate(overviews):
        try:
            f = int(f)
        except (TypeError, ValueError):
            continue
        if f <= 1:
            continue
        if float(f) <= float(decimation) * 1.10:
            best = i
    return best


class RasterSource:
    """按某个 overview 层打开源文件，供 source block cache 批量读取。

    一次渲染只 open 一次、读多块；比 `read_window()` 每块 open 一次便宜得多。
    打开的是 **overview 层本身**（`rasterio.open(..., overview_level=n)`），
    因此窗口、块号、像素尺寸都是该层坐标系，跨比例尺复用时键稳定。
    """

    def __init__(self, layer: RasterLayer, overview_index: int | None = None) -> None:
        self.layer = layer
        self.path = str(layer.info.path)
        self.overview_index = overview_index
        self.factor = 1
        if overview_index is not None:
            ov = list(getattr(layer.info, "overviews", ()) or ())
            if 0 <= overview_index < len(ov):
                self.factor = int(ov[overview_index])
        self.ds = None
        self.crs = None
        self._env = None

    def __enter__(self) -> "RasterSource":
        rasterio = _require_rasterio()
        self._env = rasterio.Env(GDAL_CACHEMAX=GDAL_CACHE_MB)
        self._env.__enter__()
        kw = {}
        if self.overview_index is not None:
            kw["overview_level"] = int(self.overview_index)
        try:
            self.ds = rasterio.open(self.path, **kw)
        except Exception:
            try:
                self._env.__exit__(None, None, None)
            finally:
                self._env = None
            raise
        self.crs = self.ds.crs
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.ds is not None:
                self.ds.close()
        finally:
            self.ds = None
            if self._env is not None:
                self._env.__exit__(exc_type, exc, tb)
                self._env = None
        return False

    @property
    def width(self) -> int:
        return int(self.ds.width) if self.ds is not None else 0

    @property
    def height(self) -> int:
        return int(self.ds.height) if self.ds is not None else 0

    def window_for_wgs(self, extent_wgs84: tuple):
        """WGS84 范围 → 当前 overview 层的像素窗口（已与全图求交）。"""
        rasterio = _require_rasterio()
        from rasterio.warp import transform_bounds
        from rasterio.windows import Window, from_bounds

        x0, y0, x1, y1 = extent_wgs84
        if x1 <= x0 or y1 <= y0:
            return None
        if self.crs is not None and self.crs.to_epsg() != 4326:
            try:
                rb = transform_bounds("EPSG:4326", self.crs, x0, y0, x1, y1,
                                      densify_pts=21)
            except Exception:
                rb = (x0, y0, x1, y1)
        else:
            rb = (x0, y0, x1, y1)
        try:
            win = from_bounds(rb[0], rb[1], rb[2], rb[3], transform=self.ds.transform)
            win = win.intersection(Window(0, 0, self.ds.width, self.ds.height))
        except Exception:
            return None
        if win.width <= 0 or win.height <= 0:
            return None
        return win

    def read(self, win, bands: list[int], out_shape: tuple[int, int] | None = None,
             resampling: str = "bilinear", cancel=None):
        _check_cancel(cancel)
        kwargs = {
            "indexes": bands,
            "window": win,
            "resampling": _resampling_enum(resampling),
            "boundless": False,
        }
        if out_shape is not None:
            kwargs["out_shape"] = (len(bands), int(out_shape[0]), int(out_shape[1]))
        return self.ds.read(**kwargs)

    def bounds_wgs(self, win) -> tuple:
        """窗口 → WGS84 范围；用于把 source block 画到屏幕正确位置。"""
        rasterio = _require_rasterio()
        from rasterio.warp import transform_bounds

        try:
            wb = rasterio.windows.bounds(win, self.ds.transform)
            if self.crs is not None and self.crs.to_epsg() != 4326:
                wb = transform_bounds(self.crs, "EPSG:4326", *wb, densify_pts=21)
            return (float(wb[0]), float(wb[1]), float(wb[2]), float(wb[3]))
        except Exception:
            return self.layer.info.bounds


def read_window(layer: RasterLayer, extent_wgs84: tuple, out_w: int, out_h: int,
                bands: list[int] | None = None, resampling: str = "bilinear",
                overview_index: int | None = None, cancel=None
                ) -> tuple[np.ndarray, tuple] | None:
    """按 WGS84 范围抽取栅格，缩放到 (out_h, out_w)。

    对应 QGIS `QgsRasterIterator::nextPart()` + `readBlock()` 的语义：
    **只读目标分辨率需要的像元数**，靠 `out_shape` 做抽取。
    返回 (数组 [bands, out_h, out_w], 实际覆盖的 WGS84 范围) 或 None（不相交）。
    """
    x0, y0, x1, y1 = extent_wgs84
    if x1 <= x0 or y1 <= y0:
        return None
    if out_w < 1 or out_h < 1:
        return None

    _check_cancel(cancel)
    # GDAL 默认块缓存按机器内存的百分比给（本机可到 1.5 GB+）。栅格渲染
    # 只需要屏幕那么大的输出，64 MB 足够覆盖分块读取的重叠，必须显式封顶。
    with RasterSource(layer, overview_index) as src:
        ds = src.ds
        win = src.window_for_wgs((x0, y0, x1, y1))
        if win is None:
            return None

        idxs = bands or (list(range(1, min(ds.count, 3) + 1)) if ds.count >= 3 else [1])
        rs = _resampling_enum(resampling)
        src_pixels = max(1.0, float(win.width) * float(win.height))
        try:
            # 已经显式打开 overview 层时，窗口本身就是缩减层，不再判断 overview。
            has_overviews = overview_index is not None
            if not has_overviews:
                try:
                    has_overviews = any(ds.overviews(i) for i in idxs)
                except Exception:
                    has_overviews = False
            if src_pixels > MAX_DIRECT_SOURCE_PIXELS and not has_overviews:
                # 有 overview 时 GDAL 直接读缩减层，内存和速度都最好，不要分块。
                arr = _read_window_blocks(
                    ds, win, idxs, max(1, int(out_w)), max(1, int(out_h)), rs,
                    cancel=cancel)
            else:
                _check_cancel(cancel)
                arr = ds.read(
                    indexes=idxs,
                    window=win,
                    out_shape=(len(idxs), max(1, int(out_h)), max(1, int(out_w))),
                    resampling=rs,
                    boundless=False,
                )
        except RasterCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            raise RasterError(f"读栅格失败：{e}") from e

        actual = src.bounds_wgs(win)
    return arr, actual


def sample_stats(layer: RasterLayer, bands: list[int], resampling: str = "bilinear",
                 cancel=None, grid: int = 8, block_px: int = 256) -> dict:
    """采样估计整层拉伸区间（QGIS `QgsRasterBandStats` 的抽样路径）。

    这里**不能**为了算 percentile 把 6 亿像元读一遍：那正是缩放第一次卡住的
    原因。有 overview 时读 512×512 缩减层；没有 overview 时在图上均匀取
    `grid×grid` 个 256×256 源块，再合并采样值。统计只用于显示拉伸，抽样足够，
    且不会像“用当前视口算”那样在平移/缩放时改变亮度。
    """
    info = layer.info
    b = getattr(info, "bounds", None)
    if not b or len(b) != 4:
        return {}
    stretch = str((layer.style or {}).get("stretch") or "percentile")
    if stretch == "none":
        return {}

    arr = None
    overviews = list(getattr(info, "overviews", ()) or ())
    if overviews or int(info.width) * int(info.height) <= MAX_DIRECT_SOURCE_PIXELS:
        # 有 overview 时直接读缩减层；小图整窗读也不贵。
        idx = None
        if overviews:
            idx = select_overview_index(
                overviews, max(info.width, info.height) / 512.0)
        try:
            got = read_window(layer, tuple(b), 512, 512, bands=bands,
                              resampling=resampling, overview_index=idx,
                              cancel=cancel)
            if got is not None:
                arr = got[0]
        except RasterCancelled:
            raise
        except Exception:  # noqa: BLE001
            arr = None

    if arr is None:
        # 没有 overview 的大图：均匀抽 grid×grid 个源窗口，不扫全图。
        rasterio = _require_rasterio()
        from rasterio.windows import Window
        try:
            with RasterSource(layer, None) as src:
                ds = src.ds
                if ds.width <= 0 or ds.height <= 0:
                    return {}
                step_x = max(1, ds.width // max(1, grid))
                step_y = max(1, ds.height // max(1, grid))
                bw = max(1, min(block_px, step_x))
                bh = max(1, min(block_px, step_y))
                parts = []
                for gy in range(max(1, grid)):
                    _check_cancel(cancel)
                    for gx in range(max(1, grid)):
                        x = min(max(0, gx * step_x), max(0, ds.width - bw))
                        y = min(max(0, gy * step_y), max(0, ds.height - bh))
                        try:
                            chunk = src.read(
                                Window(x, y, bw, bh), bands,
                                out_shape=(64, 64), resampling="nearest",
                                cancel=cancel)
                        except RasterCancelled:
                            raise
                        except Exception:  # noqa: BLE001
                            continue
                        if chunk is not None and chunk.size:
                            parts.append(chunk.reshape(len(bands), -1))
                if not parts:
                    return {}
                arr = np.concatenate(parts, axis=1)
        except RasterCancelled:
            raise
        except Exception:  # noqa: BLE001
            return {}

    if arr is None or arr.size == 0:
        return {}
    nodata = info.nodata
    out = {}
    for i in range(min(len(bands), arr.shape[0])):
        a = arr[i]
        if stretch == "minmax":
            valid = a[np.isfinite(a)]
            if nodata is not None:
                valid = valid[valid != nodata]
            out[i] = ((float(valid.min()), float(valid.max()))
                      if valid.size else (0.0, 1.0))
        else:
            out[i] = percentile_stretch(a, nodata=nodata)
    return out


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
