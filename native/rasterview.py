"""栅格渲染管线：独立于矢量管线的一条完整渲染链。

为什么独立
----------
矢量管线的整套机制（渲染集 `_RenderSet`、容差简化、亚像素降点、QPainterPath 构建、
按要素包围盒筛选）全都围绕「要素 = 几何」这一前提。栅格没有要素，这些结构一个都用不上。
把它塞进 `_LayerJob` 只会让那条已经被调优到 0.35s/帧的路径多出一堆分支判断。

所以这里是一条**平行的管子**：自己的线程、自己的缓存、自己的世代号，
只对外暴露 `set_layers / invalidate / shutdown` 和两个信号。
`MapView._compose()` 只是多画一次 `raster_image()` —— 矢量那边一行没动。

对照 QGIS 源码
--------------
| 这里 | QGIS |
| --- | --- |
| `RasterViewPort` | `QgsRasterViewPort`（mTopLeftPoint / mBottomRightPoint / mWidth / mHeight） |
| `_render_layer()` | `QgsRasterLayerRenderer::render()` |
| `app.raster.read_window()` | `QgsRasterIterator::nextPart()` + `readBlock()`（按目标分辨率抽取） |
| `RESAMPLING` | `QgsRasterResampleFilter` 的重采样方式 |
| `_stretch_band()` | `QgsContrastEnhancement`（累计裁剪拉伸） |
| `_RasterJob` / `_RasterWorker` | `QgsMapRendererJob` / `ParallelJob` 的栅格分支 |
| `_img_cache` | `QgsMapRendererCache` |
| `RasterSourceCache` | `QgsRasterBlockCache`（overview 层固定源块 + LRU） |
| `RasterTileCache` | `QgsMapRendererCache` 的屏幕 tile 层 |

关键取舍：**每层渲染成一张整视口大小的透明图**（和矢量 `_LayerJob` 完全一致的语义），
这样合成端就是一句 `drawImage(0, 0, img)`，不需要在合成时再做坐标变换。
代价是一张小栅格也要占一张视口图（1440×900 约 5 MB），换来的是合成逻辑零分叉。
"""
from __future__ import annotations

import math
import os
import queue
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future

import numpy as np

from PySide6.QtCore import QRectF, QThread, Signal
from PySide6.QtGui import QImage, QPainter, Qt

from app.online_map import (append_query_params, build_tile_url, build_wms_url,
                            fetch_bytes, prepare_config, world_to_mercator)
from app.raster import (RasterCancelled, RasterError, RasterSource,
                        _cancel_requested, _check_cancel, percentile_stretch,
                        read_window, sample_stats, select_overview_index)

#: 单层栅格渲染缓存最多留几份（来回缩放直接命中，对应 QGIS 的 cache 容量）
RASTER_CACHE_MAX = 6

# ---------------------------------------------------------------------------
# 屏幕 tile cache（QgsMapRendererCache 的 tile 层；不是源 block cache）
#
# 为什么不是缓存「整张视口图」：整图缓存键含 tx/ty，平移一个像素就失效。
# 这里把视口切成世界坐标系的固定网格 tile（逻辑 256px），tile 键只跟
# scale + 世界网格 (ix, iy) + 样式有关；同一比例尺下平移，大部分 tile
# 直接命中，只重读边缘进出的那几块。
# ---------------------------------------------------------------------------
TILE_LOGICAL = 256
#: tile 四周多读 2 个逻辑像素，后画的 tile 覆盖重叠带，消掉重采样接缝。
TILE_MARGIN = 2
#: 视口最多切多少块；超过就走整窗兜底，避免一次请求把缓存打爆
MAX_TILES_PER_FRAME = 320
#: 缓存预算：按 QImage 实际字节数控制，默认 128 MB
TILE_CACHE_MAX_BYTES = 64 * 1024 * 1024
TILE_CACHE_MAX_ITEMS = 512


#: 小图整窗读一次就完了，切 tile 只会多出边界误差；只有源影像足够大，
#: 同比例尺平移复用 tile 才划算。测试/调试可用 GIS_AGENT_RASTER_TILES=1 强制。
TILE_MIN_SOURCE_PIXELS = 2_000_000


def _tiles_enabled(layer=None) -> bool:
    env = os.environ.get("GIS_AGENT_RASTER_TILES", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    if layer is None:
        return True
    try:
        info = layer.info
        return int(info.width) * int(info.height) >= TILE_MIN_SOURCE_PIXELS
    except Exception:
        return True


class RasterTileCache:
    """LRU tile cache；线程安全（RasterWorker + 单测都可能调用）。"""

    def __init__(self, max_bytes: int = TILE_CACHE_MAX_BYTES,
                 max_items: int = TILE_CACHE_MAX_ITEMS) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple, QImage] = OrderedDict()
        self._bytes = 0
        self._max_bytes = int(max_bytes)
        self._max_items = int(max_items)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _nbytes(img: QImage) -> int:
        try:
            return int(img.sizeInBytes())
        except Exception:
            return int(img.width() * img.height() * 4)

    def contains(self, key: tuple) -> bool:
        with self._lock:
            return key in self._items

    def get(self, key: tuple) -> QImage | None:
        with self._lock:
            img = self._items.get(key)
            if img is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return img

    def put(self, key: tuple, img: QImage) -> None:
        if img is None:
            return
        n = self._nbytes(img)
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= self._nbytes(old)
            self._items[key] = img
            self._bytes += n
            while (self._items and (self._bytes > self._max_bytes
                                    or len(self._items) > self._max_items)):
                _, ev = self._items.popitem(last=False)
                self._bytes -= self._nbytes(ev)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def clear_layer(self, lid: str) -> None:
        with self._lock:
            for key in list(self._items):
                if key and key[0] == lid:
                    self._bytes -= self._nbytes(self._items.pop(key))
            # 新 key 都是 (lid, ...)，这里只做前缀清理

    def has_tiles(self, lid: str, rev: int, style_key: tuple,
                  scale: float, dpr: float) -> bool:
        with self._lock:
            for key in self._items:
                if (key[0] == lid and key[1] == rev and key[2] == style_key
                        and key[3] == round(float(scale), 9)
                        and key[4] == round(float(dpr), 3)):
                    return True
            return False

    def stats(self) -> dict:
        with self._lock:
            return {"tiles": len(self._items), "bytes": self._bytes,
                    "hits": self.hits, "misses": self.misses}


TILE_CACHE = RasterTileCache()


# ---------------------------------------------------------------------------
# 源 block cache（对应 QGIS 的 QgsRasterBlockCache）
#
# 和上面 TILE_CACHE 的区别：
# · TILE_CACHE 的键含 scale / tx / ty，缩放一变就整片失效；
# · 这里缓存的是**源 overview 层的固定像素块**，键不含视口。平移、放大、
#   缩小到同一 overview 层时都能复用，且块尺寸固定，LRU 预算可算。
# ---------------------------------------------------------------------------
SOURCE_BLOCK = 256
#: 块四周多读 1 个源像素，后画的块覆盖重叠带，消除重采样接缝。
SOURCE_BLOCK_MARGIN = 1
#: 单帧最多从源块拼多少块；超过就退回 read_window 的整窗/目标块路径。
MAX_SOURCE_BLOCKS_PER_FRAME = 640
SOURCE_CACHE_MAX_BYTES = 64 * 1024 * 1024
SOURCE_CACHE_MAX_ITEMS = 512


class RasterSourceCache:
    """源块 RGBA 的 LRU；线程安全（RasterWorker + 单测都可能调用）。"""

    def __init__(self, max_bytes: int = SOURCE_CACHE_MAX_BYTES,
                 max_items: int = SOURCE_CACHE_MAX_ITEMS) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[tuple, QImage] = OrderedDict()
        self._bytes = 0
        self._max_bytes = int(max_bytes)
        self._max_items = int(max_items)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _nbytes(item) -> int:
        img = item[0] if isinstance(item, tuple) else item
        try:
            return int(img.sizeInBytes())
        except Exception:
            return int(img.width() * img.height() * 4)

    def get(self, key: tuple):
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return item

    def put(self, key: tuple, item) -> None:
        n = self._nbytes(item)
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= self._nbytes(old)
            self._items[key] = item
            self._bytes += n
            while self._items and (self._bytes > self._max_bytes
                                   or len(self._items) > self._max_items):
                _, old = self._items.popitem(last=False)
                self._bytes -= self._nbytes(old)

    def clear_layer(self, lid: str) -> None:
        with self._lock:
            for key in [k for k in self._items if k and k[0] == lid]:
                old = self._items.pop(key)
                self._bytes -= self._nbytes(old)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0

    def stats(self) -> dict:
        with self._lock:
            return {"blocks": len(self._items), "bytes": self._bytes,
                    "hits": self.hits, "misses": self.misses}


SOURCE_CACHE = RasterSourceCache()


# ---------------------------------------------------------------------------
# 在线地图缓存（XYZ 瓦片 / WMS 单张图）
#
# 这里缓存的是解码后的 QImage，不是原始字节：平移回来时直接画，不再解码。
# 键是最终请求 URL；同一 URL 的瓦片在缩放/平移时天然复用。上限按像素算，
# 256 张 256² RGBA 约 64 MB，和本地栅格的两层缓存保持同一量级。
# ---------------------------------------------------------------------------
ONLINE_CACHE_MAX_BYTES = 64 * 1024 * 1024
ONLINE_CACHE_MAX_ITEMS = 256
#: 单帧最多请求多少张在线瓦片；异常视口（比如整世界误算到 z15）直接放弃，
#: 避免一个图层把渲染线程拖进几十分钟的下载循环。
MAX_ONLINE_TILES = 1024


class _OnlineImageCache:
    def __init__(self, max_bytes: int = ONLINE_CACHE_MAX_BYTES,
                 max_items: int = ONLINE_CACHE_MAX_ITEMS) -> None:
        self._lock = threading.Lock()
        self._data: OrderedDict[str, QImage] = OrderedDict()
        self._bytes = 0
        self._max_bytes = int(max_bytes)
        self._max_items = int(max_items)

    @staticmethod
    def _size(img: QImage) -> int:
        try:
            return int(img.sizeInBytes())
        except Exception:  # noqa: BLE001
            return int(img.width() * img.height() * 4)

    def get(self, key: str) -> QImage | None:
        with self._lock:
            img = self._data.get(key)
            if img is None:
                return None
            self._data.move_to_end(key)
            return img

    def put(self, key: str, img: QImage) -> None:
        if img is None or img.isNull():
            return
        n = self._size(img)
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= self._size(old)
            self._data[key] = img
            self._bytes += n
            while self._data and (len(self._data) > self._max_items
                                  or self._bytes > self._max_bytes):
                _k, old = self._data.popitem(last=False)
                self._bytes -= self._size(old)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._bytes = 0


ONLINE_CACHE = _OnlineImageCache()


# --------------------------------------------------------------------------- 视口


class RasterViewPort:
    """一次栅格渲染的目标视口（对应 `QgsRasterViewPort`）。

    · `extent`：要画的 WGS84 范围（经纬度）
    · `width / height`：目标**设备像素**尺寸（已乘 dpr）
    · `scale / tx / ty`：世界坐标 → 屏幕的变换，和矢量管线同一套
    """

    __slots__ = ("extent", "width", "height", "scale", "tx", "ty", "dpr", "world")

    def __init__(self, extent, width, height, scale, tx, ty, dpr, world):
        # 全部转 Python 标量：调用方可能把 numpy.float64 带进来（gdf.total_bounds
        # 一路传到 fit_bounds），它流进 QPainter.setRenderHint 会变成 numpy.bool_，
        # 在合成时抛 TypeError。缓存键也会因此出现「数值相等但类型不同」的重复项。
        self.extent = tuple(float(v) for v in extent)
        self.width = int(width)
        self.height = int(height)
        self.scale = float(scale)
        self.tx = float(tx)
        self.ty = float(ty)
        self.dpr = float(dpr)
        self.world = tuple(float(v) for v in world)      # 世界坐标矩形 (x0, y0, x1, y1)


# --------------------------------------------------------------------------- 任务


class _RasterItem:
    """单个栅格图层的渲染任务。"""

    __slots__ = ("lid", "layer", "image", "completed")

    def __init__(self, lid: str, layer) -> None:
        self.lid = lid
        self.layer = layer
        self.image: QImage | None = None
        self.completed = False


class _RasterJob:
    """一次栅格渲染任务（对应 QGS 的渲染 job，栅格分支）。"""

    __slots__ = ("epoch", "items", "viewport", "view", "cancelled")

    def __init__(self, epoch: int, items: list[_RasterItem], viewport: RasterViewPort,
                 view: tuple) -> None:
        self.epoch = epoch
        self.items = items
        self.viewport = viewport
        self.view = view
        self.cancelled = False


# --------------------------------------------------------------------------- 数组 → QImage


def _to_qimage(rgba: np.ndarray) -> QImage:
    """(h, w, 4) uint8 → QImage。

    必须 `copy()`：QImage 用外部 buffer 构造时**不接管内存**，
    numpy 数组一被回收，图像就指向野指针（表现为花屏或直接崩溃）。
    """
    h, w = rgba.shape[:2]
    buf = np.ascontiguousarray(rgba)
    img = QImage(buf.data, w, h, 4 * w, QImage.Format_RGBA8888)
    return img.copy()


def _stretch_band(band: np.ndarray, lo: float, hi: float, gamma: float,
                  nodata) -> np.ndarray:
    """单波段 → 0..255（QgsContrastEnhancement + gamma）。"""
    a = band.astype(np.float32, copy=True)
    valid = np.isfinite(a)
    if nodata is not None:
        valid &= a != nodata
    span = float(hi - lo) or 1.0
    np.subtract(a, lo, out=a)
    np.divide(a, span, out=a)
    np.clip(a, 0.0, 1.0, out=a)
    if gamma and gamma != 1.0:
        np.power(a, 1.0 / float(gamma), out=a)
    a *= 255.0
    out = a.astype(np.uint8)
    out[~valid] = 0
    return out


def _build_rgba(layer, arr: np.ndarray) -> np.ndarray:
    """按图层的渲染样式把波段数组变成 RGBA。

    单波段 → 灰度铺成 RGB；三波段及以上 → 取前三波段当 RGB。
    nodata 一律 alpha=0（透明），这样栅格和下面的矢量叠加不会糊成一块黑。
    """
    style = layer.style or {}
    info = layer.info
    nodata = info.nodata
    n = arr.shape[0]
    h, w = arr.shape[1], arr.shape[2]
    rgba = np.zeros((h, w, 4), dtype=np.uint8)

    gamma = float(style.get("gamma") or 1.0)
    stretch = style.get("stretch") or "percentile"

    # 统计量算一次就缓存（大影像上 percentile 不便宜）
    stats = layer.stats
    if stats is None:
        stats = {}
        for i in range(n):
            if stretch == "none":
                stats[i] = (0.0, 255.0 if arr.dtype == np.uint8 else 1.0)
            elif stretch == "minmax":
                a = arr[i]
                v = a[np.isfinite(a)]
                stats[i] = (float(v.min()), float(v.max())) if v.size else (0.0, 1.0)
            else:
                stats[i] = percentile_stretch(arr[i], nodata=nodata)
        layer.stats = stats

    if n >= 3:
        for c in range(3):
            lo, hi = stats.get(c, (0.0, 1.0))
            rgba[:, :, c] = _stretch_band(arr[c], lo, hi, gamma, nodata)
        # alpha：任一波段为 nodata（或 NaN/Inf）就透明。
        # 注意 nodata 可能是 np.nan（栅格计算结果默认就是 NaN），
        # `arr == nan` 永远为 False，必须同时用 isfinite 判断。
        mask = np.zeros((h, w), dtype=bool)
        for c in range(3):
            mask |= ~np.isfinite(arr[c])
            if nodata is not None:
                mask |= arr[c] == nodata
        rgba[:, :, 3] = np.where(mask, 0, 255)
    else:
        lo, hi = stats.get(0, (0.0, 1.0))
        g = _stretch_band(arr[0], lo, hi, gamma, nodata)
        rgba[:, :, 0] = g
        rgba[:, :, 1] = g
        rgba[:, :, 2] = g
        rgba[:, :, 3] = 255
        mask = ~np.isfinite(arr[0])
        if nodata is not None:
            mask |= arr[0] == nodata
        rgba[:, :, 3][mask] = 0
        # 单波段但极小值全为 0 时，别整幅变成黑块（高程图常见 -9999）
    return rgba


# --------------------------------------------------------------------------- 渲染


def render_layer_image(layer, vp: RasterViewPort, cancel=None,
                       progress=None) -> QImage | None:
    """栅格图层 → 整视口透明图；优先走源块，再退屏幕 tile，最后整窗。

    · 源块：键不含视口，跨缩放复用；大图（有 overview 优先，native 小块也行）；
    · 屏幕 tile：同一比例尺平移复用；小图或源块超出预算时使用；
    · 整窗：小视口/异常兜底（以及 `GIS_AGENT_RASTER_TILES=0`）。

    `progress` 只用于在线瓦片：每贴上一张瓦片就回传一张**已复制**的当前
    视口图，主线程可以马上合成上屏；本地栅格仍是一层画完再回传。
    """
    # 在线底图不读栅格文件：XYZ/WMTS 取瓦片，WMS 取一张图。放在最前面，
    # 避免任何 rasterio / RasterSource / overview 逻辑碰到 URL。
    if getattr(layer, "online", None):
        return _render_online_layer(layer, vp, cancel=cancel,
                                    progress=progress)

    opacity = float((layer.style or {}).get("opacity") or 1.0)
    if _source_blocks_enabled(layer):
        try:
            img = _render_layer_image_source_blocks(layer, vp, cancel)
            if img is not None:
                return img
        except RasterCancelled:
            raise
        except RasterError:
            raise
        except Exception:  # noqa: BLE001
            pass
    if _tiles_enabled(layer) and opacity >= 0.999:
        bands, resampling, style_key = _tile_style_parts(layer)
        rev = int(getattr(layer, "rev", 0))
        have_tiles = TILE_CACHE.has_tiles(
            _layer_id(layer), rev, style_key, vp.scale, vp.dpr)
        if have_tiles:
            try:
                img = _render_layer_image_tiled(layer, vp, cancel=cancel)
                if img is not None:
                    return img
            except RasterCancelled:
                raise
            except RasterError:
                raise
            except Exception:  # noqa: BLE001
                # tile 合成失败不能让整个栅格图层消失：退回整窗读取。
                pass
        else:
            # 首帧仍用整窗读取：大栅格不会因为切成 N 个窗口反而更慢；
            # 画完再把结果裁成 tile，下一次平移/重绘直接命中。
            img = _render_layer_image_full(layer, vp, cancel=cancel)
            if img is not None:
                try:
                    _populate_tile_cache(layer, vp, img)
                except Exception:  # noqa: BLE001
                    pass
            return img
    return _render_layer_image_full(layer, vp, cancel=cancel)


def _online_cache_key(url: str, headers: dict | None) -> str:
    """网络图片缓存键。

    同一个 URL 如果带不同 Authorization/Token 请求头，可能是不同权限的图，
    不能共用缓存，所以把自定义请求头也压进键里。
    """
    headers = headers or {}
    if not headers:
        return url
    joined = "&".join(f"{str(k).lower()}={v}" for k, v in sorted(headers.items()))
    return f"{url}\n{joined}"


def _render_online_layer(layer, vp: RasterViewPort, cancel=None,
                         progress=None) -> QImage | None:
    """在线底图 → 整视口透明图（XYZ/WMTS 瓦片或 WMS 单张图）。"""
    cfg = prepare_config(getattr(layer, "online", None) or {})
    method = str(cfg.get("method") or "xyz").lower()
    opacity = float((layer.style or {}).get("opacity") or 1.0)

    def emit_partial(img: QImage) -> None:
        if progress is None:
            return
        if opacity < 0.999:
            img = _apply_opacity(img, opacity)
        progress(img)

    if method == "wms":
        img = _render_online_wms(layer, cfg, vp, cancel=cancel)
    else:
        img = _render_online_tiles(layer, cfg, vp, cancel=cancel,
                                   progress=emit_partial)
    if img is None:
        return None
    if opacity < 0.999:
        img = _apply_opacity(img, opacity)
    return img


def _online_output_image(vp: RasterViewPort) -> QImage:
    img = QImage(max(1, int(vp.width)), max(1, int(vp.height)),
                 QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(float(vp.dpr))
    img.fill(Qt.transparent)
    return img


def _online_zoom(scale: float, cfg: dict) -> int:
    """当前比例尺对应的 Web 瓦片层级。

    一层瓦片是 256 逻辑像素；`round(log2(scale / 256))` 选最接近 256px 的层级，
    比 floor 少糊、比 ceil 少下一倍瓦片。范围夹在 min_zoom/max_zoom 内。
    """
    try:
        lo = int(cfg.get("min_zoom") if cfg.get("min_zoom") is not None else 0)
        hi = int(cfg.get("max_zoom") if cfg.get("max_zoom") is not None else 19)
    except Exception:  # noqa: BLE001
        lo, hi = 0, 19
    if hi < lo:
        lo, hi = hi, lo
    try:
        z = int(round(math.log2(max(float(scale), 1e-9) / 256.0)))
    except Exception:  # noqa: BLE001
        z = 0
    return max(lo, min(hi, z))


#: 瓦片并发下载的线程数。渲染线程只负责解码和贴图，网络等待摊到这几条线程。
#: 8 是天地图 / OSM 这类公共瓦片服务能稳定接受的同时连接数；再多容易被限速。
TILE_FETCH_WORKERS = 8


class _TileTask:
    """一张待下载的瓦片：URL 与结果 Future。"""

    __slots__ = ("url", "headers", "future")

    def __init__(self, url: str, headers: dict | None) -> None:
        self.url = url
        self.headers = headers or {}
        self.future = Future()


class _TileFetcher:
    """并发取瓦片字节的小线程池。

    为什么不用 `ThreadPoolExecutor`：它的工作线程不是 daemon，解释器退出时会
    join；某张瓦片卡在 6 s 超时里，关窗口就得陪着等。这里用 daemon 线程 +
    队列，进程退出不被拖住，和 `_online_worker` 的 cancel 语义也对得上。

    `submit()` 只入队，结果从 `future` 回来；贴图仍由渲染线程单线程完成
    （QPainter 不能跨线程）。
    """

    def __init__(self, workers: int = TILE_FETCH_WORKERS) -> None:
        self._workers = max(1, int(workers))
        self._queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    @property
    def workers(self) -> int:
        """线程数；调用方据此决定同时提交多少张。"""
        return self._workers

    def submit(self, url: str, headers: dict | None) -> _TileTask:
        task = _TileTask(url, headers)
        self._ensure_threads()
        self._queue.put(task)
        return task

    def _ensure_threads(self) -> None:
        with self._lock:
            while len(self._threads) < self._workers:
                name = "tile-fetch-%d" % (len(self._threads) + 1)
                t = threading.Thread(target=self._run, name=name, daemon=True)
                self._threads.append(t)
                t.start()

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                return
            fut = task.future
            # 视图已经切走：这张不发，队列里剩下的同理（cancel 后再判断）。
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                data = fetch_bytes(task.url, headers=task.headers)
            except BaseException:  # noqa: BLE001  后台线程不能把异常抛给解释器
                data = None
            try:
                fut.set_result(data)
            except Exception:  # noqa: BLE001  调用方已经 cancel 了这张
                pass


def _tile_workers() -> int:
    """并发数：默认 `TILE_FETCH_WORKERS`，`GIS_AGENT_TILE_THREADS` 可覆盖。

    服务端限流时把它调小（比如 2），网络很好时可以调到 16；上限夹 32，
    免得一条 `export` 把浏览器同源并发策略之外的压力全砸给瓦片服务器。
    """
    env = os.environ.get("GIS_AGENT_TILE_THREADS", "").strip()
    if env:
        try:
            return max(1, min(32, int(env)))
        except ValueError:
            pass
    return TILE_FETCH_WORKERS


_TILE_FETCHER: "_TileFetcher | None" = None
_TILE_FETCHER_LOCK = threading.Lock()


def tile_fetcher() -> _TileFetcher:
    """进程级单例：所有在线图层共用这几条下载线程。"""
    global _TILE_FETCHER
    with _TILE_FETCHER_LOCK:
        if _TILE_FETCHER is None:
            _TILE_FETCHER = _TileFetcher(_tile_workers())
        return _TILE_FETCHER


def _wait_tile(task: _TileTask, cancel=None) -> bytes | None:
    """等一张瓦片的结果，期间保持可取消（每 50 ms 看一次取消标志）。

    不能用无超时的 `future.result()`：视图切走后渲染线程必须马上让路，
    否则旧视图的慢瓦片会把新视图挡在后面。
    """
    fut = task.future
    while True:
        if _cancel_requested(cancel):
            fut.cancel()
            raise RasterCancelled()
        try:
            return fut.result(timeout=0.05)
        except TimeoutError:
            continue


def _render_online_tiles(layer, cfg: dict, vp: RasterViewPort,
                         cancel=None, progress=None) -> QImage | None:
    """XYZ / WMTS 瓦片：并发下载、按扫描顺序贴图，每贴一张就回传当前快照。

    网络瓦片是一张一张到的；如果等全部到齐才返回整图，图层会长时间空白，
    还会让整条栅格管线以为这一层没画完。`progress` 回调让调用方每拿到一张
    就合成一次，先出半成品，最后一张到齐后再用完整图替换。

    下载在 `_TileFetcher` 的几条线程上同时进行（一屏 12 张、每张 150 ms，
    串行要 1.8 s，8 路并发只要约 300 ms），贴图仍在本线程按顺序做 ——
    QPainter 不能跨线程，而瓦片之间不重叠，顺序贴出来的结果和串行一致。
    """
    template = str(cfg.get("url") or "")
    if not template:
        return None
    z = _online_zoom(vp.scale, cfg)
    n = 1 << z
    # 视口世界矩形（归一化 Mercator）→ 覆盖到的瓦片区。视口可能略超出
    # [0,1]（整世界视图的边缘），夹到合法范围即可。
    wx0, wy0, wx1, wy1 = (float(v) for v in vp.world)
    x0 = max(0, min(n - 1, int(math.floor(wx0 * n))))
    x1 = max(0, min(n - 1, int(math.floor(wx1 * n))))
    y0 = max(0, min(n - 1, int(math.floor(wy0 * n))))
    y1 = max(0, min(n - 1, int(math.floor(wy1 * n))))
    if (x1 - x0 + 1) * (y1 - y0 + 1) > MAX_ONLINE_TILES:
        return None
    span = float(vp.scale) / n
    if span <= 0:
        return None
    params = cfg.get("params") or {}
    headers = cfg.get("headers") or {}
    subdomains = str(cfg.get("subdomains") or "").strip()
    if not subdomains and "tianditu.gov.cn" in template.lower():
        # 兼容旧会话里没有 subdomains 字段的天地图配置：天地图是 t0…t7，
        # 不是 OSM 的 a/b/c。
        subdomains = "01234567"
    if not subdomains:
        subdomains = "abc"
    out = _online_output_image(vp)
    p = QPainter(out)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    drawn = 0
    # 先把视口覆盖到的瓦片列成有序清单（扫描顺序 = 左上到右下），缓存命中的
    # 直接拿图；没命中的入队并发下载。同时在飞的不超过 TILE_FETCH_WORKERS 张，
    # 免得一屏几十张同时打到服务器被限速。
    items: list[list] = []
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            url = append_query_params(
                build_tile_url(template, z, tx, ty, subdomains=subdomains),
                params)
            key = _online_cache_key(url, headers)
            items.append([tx, ty, key, url, ONLINE_CACHE.get(key)])

    fetcher = tile_fetcher()
    #: 在飞窗口 = 线程数：排队超过线程数只是把结果压在自己的队列里，没意义。
    window = max(1, min(TILE_FETCH_WORKERS, fetcher.workers))
    inflight: deque = deque()          # (清单下标, _TileTask)，按提交顺序
    pos = 0
    failed = False

    def pump() -> None:
        """把下载窗口补满（只提交未缓存、且线路还没出错的瓦片）。"""
        nonlocal pos
        while not failed and pos < len(items) and len(inflight) < window:
            it = items[pos]
            if it[4] is None:
                inflight.append((pos, fetcher.submit(it[3], headers)))
            pos += 1

    def drop_inflight() -> None:
        """放弃还没开始的下载（已经发出去的会自己结束，结果丢掉即可）。"""
        while inflight:
            _j, task = inflight.popleft()
            task.future.cancel()

    try:
        for tx, ty, key, _url, tile in items:
            if _cancel_requested(cancel):
                raise RasterCancelled()
            pump()
            if tile is None:
                # 清单按顺序提交，所以队头正好是当前这张。
                _j, task = inflight.popleft()
                data = _wait_tile(task, cancel)
                if not data:
                    # 一张瓦片失败通常意味着整条线路不可用：不再提交新的，
                    # 已经贴上的先留着。整张都没画出来时返回 None，不缓存空图，
                    # 下次视图变化还能重试。
                    failed = True
                    drop_inflight()
                    return out if drawn else None
                tile = QImage()
                if not tile.loadFromData(data):
                    failed = True
                    drop_inflight()
                    return out if drawn else None
                ONLINE_CACHE.put(key, tile)
            p.drawImage(QRectF(tx / n * vp.scale + vp.tx,
                               ty / n * vp.scale + vp.ty,
                               span, span), tile)
            drawn += 1
            if progress is not None:
                # 必须 copy：调用方要把这张图交给主线程显示，后台 painter
                # 还会继续往 out 上贴下一张瓦片。
                progress(out.copy())
    except RasterCancelled:
        drop_inflight()
        raise
    finally:
        p.end()
    return out if drawn else None


def _render_online_wms(layer, cfg: dict, vp: RasterViewPort,
                       cancel=None) -> QImage | None:
    """WMS GetMap：一次请求整张视口图。"""
    if _cancel_requested(cancel):
        raise RasterCancelled()
    bbox = world_to_mercator(vp.world)
    req_w = max(1, min(2048, int(vp.width)))
    req_h = max(1, min(2048, int(vp.height)))
    params = cfg.get("params") or {}
    headers = cfg.get("headers") or {}
    url = build_wms_url(str(cfg.get("url") or ""), bbox, req_w, req_h,
                        extra_params=params)
    key = _online_cache_key(url, headers)
    img = ONLINE_CACHE.get(key)
    if img is None:
        data = fetch_bytes(url, max_bytes=24 * 1024 * 1024, headers=headers)
        if not data:
            return None
        img = QImage()
        if not img.loadFromData(data):
            return None
        ONLINE_CACHE.put(key, img)
    out = _online_output_image(vp)
    p = QPainter(out)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    try:
        # QImage 已设 dpr，QPainter 用逻辑像素；请求图按设备像素拿，drawImage
        # 自动缩放回逻辑视口。
        p.drawImage(QRectF(0, 0, vp.width / max(vp.dpr, 1e-9),
                           vp.height / max(vp.dpr, 1e-9)), img)
    finally:
        p.end()
    return out


def _source_blocks_enabled(layer) -> bool:
    env = os.environ.get("GIS_AGENT_RASTER_BLOCKS", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        # 源块缓存按 overview 层组织；没有 overview 时直接读 native 块通常
        # 比 GDAL 一次降采样更慢，交给 overview 自动生成 + 屏幕 tile/整窗路径。
        return bool(getattr(layer.info, "overviews", ()))
    except Exception:
        return False


def _render_layer_image_source_blocks(layer, vp: RasterViewPort,
                                      cancel=None) -> QImage | None:
    """源 overview 块 → 整视口图；块缓存键不含视口，跨缩放复用。

    这是 QGIS `QgsRasterIterator` + `QgsRasterBlockCache` 的对应实现：
    先按目标抽取倍率选 overview 层，再按该层的固定 256×256 像素块读取。
    同一块在平移、放大/缩小到同一层时都命中；块图像是经过拉伸的 RGBA，
    合成时只做投影落位和双线性缩放。
    """
    info = layer.info
    b = getattr(info, "bounds", None)
    if not b or len(b) != 4:
        return None
    wx0, wy0, wx1, wy1 = (min(b[0], b[2]), min(b[1], b[3]),
                          max(b[0], b[2]), max(b[1], b[3]))
    vx0, vy0, vx1, vy1 = vp.extent
    ix0, iy0 = max(wx0, vx0), max(wy0, vy0)
    ix1, iy1 = min(wx1, vx1), min(wy1, vy1)
    if ix1 <= ix0 or iy1 <= iy0:
        return None

    from .proj import project

    px0, py0 = project(ix0, iy1)
    px1, py1 = project(ix1, iy0)
    sx0, sy0 = px0 * vp.scale + vp.tx, py0 * vp.scale + vp.ty
    sx1, sy1 = px1 * vp.scale + vp.tx, py1 * vp.scale + vp.ty
    if sx1 <= sx0 or sy1 <= sy0:
        return None
    dpr = float(vp.dpr)
    dst_w = max(1, int(round((sx1 - sx0) * dpr)))
    dst_h = max(1, int(round((sy1 - sy0) * dpr)))
    cap = 4096
    if dst_w > cap or dst_h > cap:
        k = cap / float(max(dst_w, dst_h))
        dst_w = max(1, int(dst_w * k))
        dst_h = max(1, int(dst_h * k))

    overviews = list(getattr(info, "overviews", ()) or ())
    # 先用 native 层算实际源窗口，决定抽取倍率；再按选定 overview 打开。
    try:
        with RasterSource(layer, None) as native:
            win0 = native.window_for_wgs((ix0, iy0, ix1, iy1))
            if win0 is None:
                return None
            decimation = max(float(win0.width) / dst_w,
                             float(win0.height) / dst_h)
    except RasterCancelled:
        raise
    except Exception:  # noqa: BLE001
        return None

    ov_idx = select_overview_index(overviews, decimation)
    if ov_idx is None:
        # 当前窗口接近 native；走 read_window/屏幕 tile，比逐 native 块快。
        return None
    try:
        src_ctx = RasterSource(layer, ov_idx)
        src = src_ctx.__enter__()
    except RasterCancelled:
        raise
    except Exception:  # noqa: BLE001
        return None
    try:
        from rasterio.windows import Window
        win = src.window_for_wgs((ix0, iy0, ix1, iy1))
        if win is None:
            return None
        bs = max(32, int(SOURCE_BLOCK))
        bx0 = int(math.floor(win.col_off / bs))
        bx1 = int(math.ceil((win.col_off + win.width) / bs))
        by0 = int(math.floor(win.row_off / bs))
        by1 = int(math.ceil((win.row_off + win.height) / bs))
        block_count = (bx1 - bx0 + 1) * (by1 - by0 + 1)
        if block_count > MAX_SOURCE_BLOCKS_PER_FRAME:
            return None
        # 源块路径只在 overview 层使用；块数超预算就回退整窗/目标块。


        bands, resampling, style_key = _tile_style_parts(layer)
        _ensure_stats(layer, (wx0, wy0, wx1, wy1), bands, resampling,
                      cancel=cancel)
        rev = int(getattr(layer, "rev", 0))
        lid = _layer_id(layer)
        margin = max(0, int(SOURCE_BLOCK_MARGIN))
        factor = int(src.factor)
        out = QImage(max(1, int(vp.width)), max(1, int(vp.height)),
                     QImage.Format_ARGB32_Premultiplied)
        out.setDevicePixelRatio(dpr)
        out.fill(Qt.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        try:
            for by in range(by0, by1 + 1):
                _check_cancel(cancel)
                for bx in range(bx0, bx1 + 1):
                    _check_cancel(cancel)
                    core_x, core_y = bx * bs, by * bs
                    ex0 = max(0, core_x - margin)
                    ey0 = max(0, core_y - margin)
                    ex1 = min(src.width, core_x + bs + margin)
                    ey1 = min(src.height, core_y + bs + margin)
                    if ex1 <= ex0 or ey1 <= ey0:
                        continue
                    key = (lid, rev, style_key, factor, bx, by, bs, margin)
                    cached = SOURCE_CACHE.get(key)
                    if cached is None:
                        try:
                            arr = src.read(
                                Window(ex0, ey0, ex1 - ex0, ey1 - ey0),
                                bands, out_shape=(ey1 - ey0, ex1 - ex0),
                                resampling=resampling, cancel=cancel)
                        except RasterCancelled:
                            raise
                        except Exception:  # noqa: BLE001
                            continue
                        if arr is None or arr.size == 0:
                            continue
                        img = _to_qimage(_build_rgba(layer, arr))
                        try:
                            actual = src.bounds_wgs(
                                Window(ex0, ey0, ex1 - ex0, ey1 - ey0))
                        except Exception:  # noqa: BLE001
                            actual = (ix0, iy0, ix1, iy1)
                        cached = (img, actual)
                        SOURCE_CACHE.put(key, cached)
                    try:
                        img, actual = cached
                        asx0, asy0, asx1, asy1 = _wgs_to_screen(actual, vp)
                        painter.drawImage(
                            QRectF(asx0, asy0, max(1.0, asx1 - asx0),
                                   max(1.0, asy1 - asy0)), img)
                    except RasterCancelled:
                        raise
                    except Exception:  # noqa: BLE001
                        continue
        finally:
            painter.end()
        opacity = float((layer.style or {}).get("opacity") or 1.0)
        if opacity < 0.999:
            out = _apply_opacity(out, opacity)
        return out
    finally:
        src_ctx.__exit__(None, None, None)


def _render_layer_image_full(layer, vp: RasterViewPort, cancel=None) -> QImage | None:
    """整窗读取（旧路径，作为小视口/异常兜底）。

    步骤和 QGIS 一致：
    1. 栅格范围 ∩ 视口范围 → 实际要画的经纬度矩形
    2. 该矩形按当前比例尺换算成设备像素 → 决定抽多少像元（不读全图）
    3. 读 + 重采样 + 拉伸 → RGBA
    4. 画到视口图上正确的位置
    """
    import math

    from .proj import project

    b = layer.info.bounds
    if not b:
        return None
    wx0, wy0, wx1, wy1 = (min(b[0], b[2]), min(b[1], b[3]),
                          max(b[0], b[2]), max(b[1], b[3]))
    vx0, vy0, vx1, vy1 = vp.extent
    ix0, iy0 = max(wx0, vx0), max(wy0, vy0)
    ix1, iy1 = min(wx1, vx1), min(wy1, vy1)
    if ix1 <= ix0 or iy1 <= iy0:
        return None                                  # 视口外，不用画

    # 交集在世界坐标里的屏幕矩形
    px0, py0 = project(ix0, iy1)                     # 左上角：经度小、纬度大
    px1, py1 = project(ix1, iy0)                     # 右下角
    sx0 = px0 * vp.scale + vp.tx
    sy0 = py0 * vp.scale + vp.ty
    sx1 = px1 * vp.scale + vp.tx
    sy1 = py1 * vp.scale + vp.ty
    if sx1 <= sx0 or sy1 <= sy0:
        return None

    dpr = vp.dpr
    # 目标像素尺寸：只要屏幕这么大，超出的部分读了也没用
    dst_w = max(1, int(round((sx1 - sx0) * dpr)))
    dst_h = max(1, int(round((sy1 - sy0) * dpr)))
    # 上限保护：极端缩放下别申请几千兆的数组
    cap = 4096
    if dst_w > cap or dst_h > cap:
        k = cap / float(max(dst_w, dst_h))
        dst_w = max(1, int(dst_w * k))
        dst_h = max(1, int(dst_h * k))

    style = layer.style or {}
    bands = style.get("bands") or ([1, 2, 3] if layer.info.bands >= 3 else [1])
    bands = [int(x) for x in bands if 1 <= int(x) <= layer.info.bands] or [1]
    resampling = style.get("resampling") or "bilinear"

    try:
        got = read_window(layer, (ix0, iy0, ix1, iy1), dst_w, dst_h,
                          bands=bands, resampling=resampling, cancel=cancel)
    except RasterError:
        raise
    if got is None:
        return None
    arr, _actual = got
    if arr.size == 0:
        return None

    rgba = _build_rgba(layer, arr)
    sub = _to_qimage(rgba)

    # 整视口透明图（和矢量 _LayerJob 同语义）
    img = QImage(max(1, int(vp.width)), max(1, int(vp.height)),
                 QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(Qt.transparent)
    p = QPainter(img)
    # 栅格放大用双线性，避免最近邻的锯齿；缩小同理
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    # QPainter 在设置了 dpr 的 QImage 上用的是**逻辑像素**（宽度 = vp.width/dpr）。
    # sx0/sy0 本来就是屏幕逻辑坐标，这里不能再乘 dpr，否则高 DPI 屏上栅格会
    # 画到 2 倍大小、偏出视口。target 尺寸仍是设备像素（dst_w/dst_h），
    # 由 drawImage 自动缩放。
    p.drawImage(_qrect(sx0, sy0, sx1 - sx0, sy1 - sy0), sub)
    p.end()
    opacity = float(style.get("opacity") or 1.0)
    if opacity < 0.999:
        img = _apply_opacity(img, opacity)
    return img


def _wgs_to_screen(extent: tuple, vp: RasterViewPort) -> tuple:
    """WGS84 矩形 → 屏幕逻辑坐标 (sx0, sy0, sx1, sy1)。"""
    from .proj import project

    lon0, lat0, lon1, lat1 = extent
    p0 = project(lon0, lat0)
    p1 = project(lon1, lat1)
    return (p0[0] * vp.scale + vp.tx,
            p1[1] * vp.scale + vp.ty,
            p1[0] * vp.scale + vp.tx,
            p0[1] * vp.scale + vp.ty)


def _layer_id(layer) -> str:
    """图层 id；测试里的鸭子类型层没有 id 时用对象地址兜底。"""
    lid = getattr(layer, "id", None)
    return str(lid) if lid else f"_layer_{id(layer)}"


def _tile_style_parts(layer) -> tuple[list[int], str, tuple]:
    """tile 键里跟绘制结果有关的样式部分。"""
    style = layer.style or {}
    bands = style.get("bands") or ([1, 2, 3] if layer.info.bands >= 3 else [1])
    bands = [int(x) for x in bands if 1 <= int(x) <= layer.info.bands] or [1]
    resampling = style.get("resampling") or "bilinear"
    key = (tuple(bands), str(resampling),
           str(style.get("stretch") or "percentile"),
           float(style.get("gamma") or 1.0))
    return bands, str(resampling), key


def _ensure_stats(layer, extent: tuple, bands: list[int], resampling: str,
                  cancel=None) -> None:
    """在切块前把拉伸统计算到图层级。

    如果让第一块自己算 stats，后面所有块都会用这一小块的分位数，拼接处会
    出现亮度跳变。QGIS 的 QgsContrastEnhancement 也是图层级统计；但统计
    必须**抽样**，不能为了 percentile 扫全图（那会让第一次缩放卡几十秒）。
    """
    if getattr(layer, "stats", None) is not None:
        return
    try:
        stats = sample_stats(layer, bands, resampling, cancel=cancel)
        if stats:
            layer.stats = stats
    except RasterCancelled:
        raise
    except Exception:  # noqa: BLE001
        pass


def _populate_tile_cache(layer, vp: RasterViewPort, img: QImage) -> None:
    """整窗首帧画完后，把它按 tile 网格裁进缓存。

    这样第一次打大图仍是整窗读取（不额外多读 N 个窗口），
    之后同一比例尺的平移直接复用这些 tile。
    """
    b = getattr(layer.info, "bounds", None)
    if not b or len(b) != 4:
        return
    bx0, by0, bx1, by1 = (min(b[0], b[2]), min(b[1], b[3]),
                          max(b[0], b[2]), max(b[1], b[3]))
    bands, resampling, style_key = _tile_style_parts(layer)
    scale = float(vp.scale)
    tx, ty = float(vp.tx), float(vp.ty)
    dpr = float(vp.dpr)
    tile_world = TILE_LOGICAL / scale
    vw = vp.world
    ix0 = int(math.floor(vw[0] / tile_world))
    ix1 = int(math.ceil(vw[2] / tile_world))
    iy0 = int(math.floor(vw[1] / tile_world))
    iy1 = int(math.ceil(vw[3] / tile_world))
    if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > MAX_TILES_PER_FRAME:
        return

    from .proj import unproject

    rev = int(getattr(layer, "rev", 0))
    tile_span = TILE_LOGICAL + 2 * TILE_MARGIN
    margin_world = TILE_MARGIN / scale
    tile_dev = max(1, int(round(tile_span * dpr)))

    for ix in range(ix0, ix1 + 1):
        for iy in range(iy0, iy1 + 1):
            wx0 = ix * tile_world - margin_world
            wy0 = iy * tile_world - margin_world
            wx1 = (ix + 1) * tile_world + margin_world
            wy1 = (iy + 1) * tile_world + margin_world
            try:
                lon0, lat_bottom = unproject(wx0, wy1)
                lon1, lat_top = unproject(wx1, wy0)
            except Exception:
                continue
            clon0 = max(lon0, bx0)
            clon1 = min(lon1, bx1)
            clat0 = max(lat_bottom, by0)
            clat1 = min(lat_top, by1)
            if clon1 <= clon0 or clat1 <= clat0:
                continue
            tsx = wx0 * scale + tx
            tsy = wy0 * scale + ty
            # 只缓存**完整落在视口内**的 tile：视口边缘的 tile 在整窗图里
            # 是残缺的，缓存了会让下一次平移露透明边；让 tile 路径按需补读。
            view_w = vp.width / dpr
            view_h = vp.height / dpr
            if (tsx < -0.5 or tsy < -0.5
                    or tsx + tile_span > view_w + 0.5
                    or tsy + tile_span > view_h + 0.5):
                continue
            key = (_layer_id(layer), rev, style_key, round(scale, 9),
                   round(dpr, 3), ix, iy, TILE_LOGICAL, TILE_MARGIN)
            if TILE_CACHE.contains(key):
                continue
            tile = QImage(tile_dev, tile_dev, QImage.Format_ARGB32_Premultiplied)
            tile.setDevicePixelRatio(dpr)
            tile.fill(Qt.transparent)
            tp = QPainter(tile)
            tp.setRenderHint(QPainter.SmoothPixmapTransform, True)
            try:
                tp.drawImage(QRectF(-tsx, -tsy,
                                    img.width() / dpr, img.height() / dpr), img)
            finally:
                tp.end()
            TILE_CACHE.put(key, tile)


def _render_layer_image_tiled(layer, vp: RasterViewPort, cancel=None) -> QImage | None:
    """把视口切成世界坐标固定网格的 tile，逐块读/缓存后合成。"""
    b = getattr(layer.info, "bounds", None)
    if not b or len(b) != 4:
        return None
    bx0, by0, bx1, by1 = (min(b[0], b[2]), min(b[1], b[3]),
                          max(b[0], b[2]), max(b[1], b[3]))
    bands, resampling, style_key = _tile_style_parts(layer)
    _ensure_stats(layer, (bx0, by0, bx1, by1), bands, resampling, cancel=cancel)

    scale = float(vp.scale)
    tx, ty = float(vp.tx), float(vp.ty)
    dpr = float(vp.dpr)
    tile_world = TILE_LOGICAL / scale
    vw = vp.world
    ix0 = int(math.floor(vw[0] / tile_world))
    ix1 = int(math.ceil(vw[2] / tile_world))
    iy0 = int(math.floor(vw[1] / tile_world))
    iy1 = int(math.ceil(vw[3] / tile_world))
    if (ix1 - ix0 + 1) * (iy1 - iy0 + 1) > MAX_TILES_PER_FRAME:
        return None

    from .proj import unproject

    rev = int(getattr(layer, "rev", 0))
    tile_span = TILE_LOGICAL + 2 * TILE_MARGIN
    margin_world = TILE_MARGIN / scale
    tile_dev = max(1, int(round(tile_span * dpr)))

    out = QImage(max(1, int(vp.width)), max(1, int(vp.height)),
                 QImage.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(dpr)
    out.fill(Qt.transparent)
    op = QPainter(out)
    op.setRenderHint(QPainter.SmoothPixmapTransform, True)
    try:
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                # 每个 tile 多读一圈，后画的覆盖重叠带，避免重采样在
                # tile 边界留下 1px 接缝。
                wx0 = ix * tile_world - margin_world
                wy0 = iy * tile_world - margin_world
                wx1 = (ix + 1) * tile_world + margin_world
                wy1 = (iy + 1) * tile_world + margin_world
                # 世界 bbox → WGS bbox（y 轴向下：wy1 是南边）
                try:
                    lon0, lat_bottom = unproject(wx0, wy1)
                    lon1, lat_top = unproject(wx1, wy0)
                except Exception:
                    continue
                clon0 = max(lon0, bx0)
                clon1 = min(lon1, bx1)
                clat0 = max(lat_bottom, by0)
                clat1 = min(lat_top, by1)
                if clon1 <= clon0 or clat1 <= clat0:
                    continue
                sx0, sy0, sx1, sy1 = _wgs_to_screen(
                    (clon0, clat0, clon1, clat1), vp)
                tsx = wx0 * scale + tx
                tsy = wy0 * scale + ty
                # 只保留 tile 与裁剪后范围的重叠部分
                lx0 = max(sx0, tsx)
                ly0 = max(sy0, tsy)
                lx1 = min(sx1, tsx + tile_span)
                ly1 = min(sy1, tsy + tile_span)
                if lx1 <= lx0 or ly1 <= ly0:
                    continue

                key = (_layer_id(layer), rev, style_key, round(scale, 9),
                       round(dpr, 3), ix, iy, TILE_LOGICAL, TILE_MARGIN)
                tile = TILE_CACHE.get(key)
                if tile is None:
                    _check_cancel(cancel)
                    ow = max(1, int(round((lx1 - lx0) * dpr)))
                    oh = max(1, int(round((ly1 - ly0) * dpr)))
                    got = read_window(layer, (clon0, clat0, clon1, clat1),
                                      ow, oh, bands=bands, resampling=resampling,
                                      cancel=cancel)
                    if got is None:
                        continue
                    arr, actual = got
                    if arr is None or arr.size == 0:
                        continue
                    rgba = _build_rgba(layer, arr)
                    sub = _to_qimage(rgba)
                    tile = QImage(tile_dev, tile_dev,
                                  QImage.Format_ARGB32_Premultiplied)
                    tile.setDevicePixelRatio(dpr)
                    tile.fill(Qt.transparent)
                    tp = QPainter(tile)
                    tp.setRenderHint(QPainter.SmoothPixmapTransform, True)
                    try:
                        asx0, asy0, asx1, asy1 = _wgs_to_screen(actual, vp)
                        tp.drawImage(
                            QRectF(asx0 - tsx, asy0 - tsy,
                                   max(1.0, asx1 - asx0),
                                   max(1.0, asy1 - asy0)), sub)
                    finally:
                        tp.end()
                    TILE_CACHE.put(key, tile)
                op.drawImage(QRectF(tsx, tsy, tile_span, tile_span), tile)
    finally:
        op.end()
    return out


def _qrect(x, y, w, h):
    from PySide6.QtCore import QRectF
    return QRectF(float(x), float(y), max(1.0, float(w)), max(1.0, float(h)))


def _apply_opacity(img: QImage, opacity: float) -> QImage:
    """整层透明度（对应 QGIS 的 layer opacity）。"""
    out = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(img.devicePixelRatio())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setOpacity(max(0.0, min(1.0, opacity)))
    p.drawImage(0, 0, img)
    p.end()
    return out


# --------------------------------------------------------------------------- 线程


class _RasterWorker(QThread):
    """常驻栅格渲染线程。

    和矢量 `_RenderWorker` 同一套语义：最新任务优先、epoch 校验、逐层回传。
    独立线程的意义在于——栅格读盘/解码很慢（大 GeoTIFF 一帧可能几百毫秒），
    它不能拖住矢量那条已经调优好的管子，反之亦然。
    """

    layerReady = Signal(str, object, int)      # 图层 id, 图像, epoch
    layerProgress = Signal(str, object, int)   # 在线瓦片中间结果, epoch
    frameDone = Signal(int)                    # epoch

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending: _RasterJob | None = None
        self._current: _RasterJob | None = None
        self._wake = threading.Event()
        self._stop = False

    def submit(self, job: _RasterJob, cancel_current: bool = False) -> None:
        with self._lock:
            old = self._pending
            cur = self._current
            self._pending = job
        if old is not None:
            old.cancelled = True
        # 在线瓦片在新视口到来时取消正在下载的旧视口，避免旧瓦片把新帧
        # 堵在队列里；本地栅格通常已经画到一半，不主动打断。
        if cancel_current and cur is not None:
            cur.cancelled = True
        self._wake.set()

    def cancel_current(self) -> None:
        """取消正在跑的当前任务（新视口到来时给在线瓦片用）。"""
        with self._lock:
            cur = self._current
            old = self._pending
        if cur is not None:
            cur.cancelled = True
        if old is not None:
            old.cancelled = True
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        with self._lock:
            job, self._pending = self._pending, None
            cur = self._current
        if job is not None:
            job.cancelled = True
        if cur is not None:
            cur.cancelled = True
        self._wake.set()

    def run(self) -> None:
        while True:
            with self._lock:
                job = self._pending
                self._pending = None
                self._current = job
            if job is not None and not job.cancelled:
                try:
                    self._render(job)
                except Exception:  # noqa: BLE001
                    pass
                with self._lock:
                    self._current = None
                if not job.cancelled:
                    self.frameDone.emit(job.epoch)
            else:
                with self._lock:
                    self._current = None
                if self._stop:
                    break
                self._wake.wait(0.2)
                self._wake.clear()
            if self._stop:
                break

    def _render(self, job: _RasterJob) -> None:
        for it in job.items:
            if job.cancelled:
                return
            progress = None
            if getattr(it.layer, "online", None):
                def progress(img, lid=it.lid, epoch=job.epoch):
                    if not job.cancelled:
                        self.layerProgress.emit(lid, img, epoch)
            try:
                img = render_layer_image(it.layer, job.viewport,
                                         cancel=lambda: job.cancelled,
                                         progress=progress)
            except RasterCancelled:
                return
            except Exception:  # noqa: BLE001
                img = None
            if job.cancelled:
                return
            it.image = img
            it.completed = True
            if img is not None:
                self.layerReady.emit(it.lid, img, job.epoch)


# --------------------------------------------------------------------------- 对外


class RasterPipeline:
    """栅格渲染管线的门面。`MapView` 只跟它打交道。

    用法（在 MapView 里）：
        self._raster = RasterPipeline(self)
        self._raster.layerImage.connect(self._on_raster_ready)
        self._raster.frameDone.connect(self._on_raster_done)
        ...
        self._raster.set_layers(raster_layers)
        self._raster.request(viewport)
    """

    def __init__(self, parent=None) -> None:
        self._worker = _RasterWorker(parent)
        self._worker.layerReady.connect(self._on_layer)
        self._worker.layerProgress.connect(self._on_partial)
        self._worker.frameDone.connect(self._on_frame)
        self._worker.start()
        # 在线瓦片单独一条线程：底图在栈底，如果和本地栅格共用一条线程，
        # 上面几层本地影像必须等所有瓦片下载完才开始画。分开后瓦片慢慢到，
        # 本地栅格照常先出；`_pending_jobs` 负责等两边都结束再报整帧完成。
        self._online_worker = _RasterWorker(parent)
        self._online_worker.layerReady.connect(self._on_layer)
        self._online_worker.layerProgress.connect(self._on_partial)
        self._online_worker.frameDone.connect(self._on_frame)
        self._online_worker.start()

        self.layers: list = []
        self.visible: dict[str, bool] = {}
        self._epoch = 0
        self._imgs: dict[str, QImage] = {}
        #: 图层 id → (缓存键, 图像)：跨帧复用（切显隐/调顺序不重读）
        self._cache: dict[str, tuple] = {}
        #: 在线图层的中间图属于哪个视口；中间图不能进 _cache，否则会被当成完整帧
        self._partial_views: dict[str, tuple] = {}
        self._req_view: tuple | None = None
        #: 当前 epoch 还有几条 worker 在画（本地 / 在线各一条）
        self._pending_jobs = 0

        #: 外部挂的信号处理（由 MapView 连接）
        self.on_layer = None       # (lid, img) -> None
        self.on_partial = None     # (lid, img) -> None
        self.on_frame = None       # () -> None

    # ------------------------------------------------------------------ 配置
    def set_layers(self, layers: list) -> None:
        old_ids = {l.id for l in self.layers}
        self.layers = list(layers)
        keep = {l.id for l in self.layers}
        for lid in old_ids - keep:
            self._cache.pop(lid, None)
            self._imgs.pop(lid, None)
            self._partial_views.pop(lid, None)
            TILE_CACHE.clear_layer(lid)
            SOURCE_CACHE.clear_layer(lid)
        # 没有在线图层了就把网瓦片缓存一起释放，别让 64 MB 跟着会话空转。
        if not any(getattr(l, "online", None) for l in self.layers):
            ONLINE_CACHE.clear()

    def set_visible(self, vis: dict[str, bool]) -> None:
        self.visible = dict(vis or {})

    def bump_rev(self, lid: str) -> None:
        """图层样式变了：丢掉它的缓存，下次重画。"""
        self._cache.pop(lid, None)
        self._imgs.pop(lid, None)
        self._partial_views.pop(lid, None)
        TILE_CACHE.clear_layer(lid)
        SOURCE_CACHE.clear_layer(lid)
        lay = next((l for l in self.layers if l.id == lid), None)
        if lay is not None:
            lay.stats = None

    def clear(self) -> None:
        self._cache.clear()
        self._imgs.clear()
        self._partial_views.clear()
        TILE_CACHE.clear()
        SOURCE_CACHE.clear()
        ONLINE_CACHE.clear()
        self._online_worker.cancel_current()
        self._pending_jobs = 0
        self._epoch += 1

    def image(self, lid: str) -> QImage | None:
        return self._imgs.get(lid)

    def view_of(self, lid: str) -> tuple | None:
        """这张图是**按哪个视口**渲出来的：(scale, tx, ty, w, h, dpr, rev)。

        合成端需要它：视图一动，手上这张图就过期了，但**过期不等于要扔掉**——
        只要知道它当初是按哪个 tx/ty/scale 画的，就能算出它现在应该画在哪儿。
        平移时那是纯位移，缩放时是一个以视口为基准的等比变换，两种都精确。
        扔掉它反而最糟：屏幕会先跳回拖拽前的样子，等新图回来才归位。
        """
        # 中间图优先于旧的完整缓存：平移/缩放后旧图还在屏上，但一旦第一张
        # 新瓦片到了，`_imgs[lid]` 已经换成新图，不能再按旧视口去贴它。
        part = self._partial_views.get(lid)
        if part is not None:
            return part
        hit = self._cache.get(lid)
        return hit[0] if hit else None

    def images(self) -> dict[str, QImage]:
        return self._imgs

    @property
    def epoch(self) -> int:
        return self._epoch

    def has_layers(self) -> bool:
        return any(self.visible.get(l.id, True) for l in self.layers)

    # ------------------------------------------------------------------ 请求
    def request(self, viewport: RasterViewPort) -> bool:
        """发起一次栅格渲染。全程命中缓存时返回 False（不用起线程）。"""
        vis = [l for l in self.layers if self.visible.get(l.id, True)]
        if not vis:
            self._epoch += 1
            self._imgs = {}
            self._pending_jobs = 0
            self._online_worker.cancel_current()
            if self.on_frame:
                self.on_frame()
            return False

        self._epoch += 1
        epoch = self._epoch
        self._req_view = (viewport.scale, viewport.tx, viewport.ty,
                          viewport.width, viewport.height, viewport.dpr)
        # 新视口一出现，旧在线瓦片立即让路；本地栅格不主动打断。
        self._online_worker.cancel_current()

        items: list[_RasterItem] = []
        reuse: dict[str, QImage] = {}
        for lay in vis:
            key = self._key(lay, viewport)
            hit = self._cache.get(lay.id)
            if hit is not None and hit[0] == key and hit[1] is not None:
                reuse[lay.id] = hit[1]
                it = _RasterItem(lay.id, lay)
                it.image = hit[1]
                it.completed = True
                items.append(it)
            else:
                items.append(_RasterItem(lay.id, lay))

        self._imgs = dict(reuse)
        if all(it.completed for it in items):
            self._pending_jobs = 0
            if self.on_frame:
                self.on_frame()
            return False

        todo = [it for it in items if not it.completed]
        local_items = [it for it in todo
                       if not getattr(it.layer, "online", None)]
        online_items = [it for it in todo
                        if getattr(it.layer, "online", None)]
        self._pending_jobs = int(bool(local_items)) + int(bool(online_items))
        if local_items:
            self._worker.submit(
                _RasterJob(epoch, local_items, viewport, self._req_view))
        if online_items:
            self._online_worker.submit(
                _RasterJob(epoch, online_items, viewport, self._req_view),
                cancel_current=True)
        return True

    def _key(self, lay, vp: RasterViewPort) -> tuple:
        return (float(vp.scale), float(vp.tx), float(vp.ty),
                int(vp.width), int(vp.height), float(vp.dpr),
                int(getattr(lay, "rev", 0)))

    # ------------------------------------------------------------------ 回调
    def _on_partial(self, lid: str, img, epoch: int) -> None:
        """在线瓦片中间图：只更新当前显示，不写完整帧缓存。"""
        if epoch != self._epoch or img is None:
            return
        self._imgs[lid] = img
        if self._req_view is not None:
            self._partial_views[lid] = self._req_view
        if self.on_partial:
            self.on_partial(lid, img)

    def _on_layer(self, lid: str, img, epoch: int) -> None:
        if epoch != self._epoch:
            return                       # 过期世代：视图已经变了
        self._imgs[lid] = img
        self._partial_views.pop(lid, None)
        lay = next((l for l in self.layers if l.id == lid), None)
        if lay is not None and self._req_view is not None:
            # 记进缓存：键用**这次请求**的视图，下次同视图直接复用
            self._cache[lid] = (self._key(lay, _VPShim(self._req_view)), img)
        if self.on_layer:
            self.on_layer(lid, img)

    def _on_frame(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        # 本地栅格和在线瓦片各有一条线程；只收到一条 frameDone 时还不能
        # 宣告整轮栅格完成，否则 MapView 会把在线中间帧当成 final。
        if self._pending_jobs > 0:
            self._pending_jobs -= 1
        if self._pending_jobs > 0:
            return
        if self.on_frame:
            self.on_frame()

    def shutdown(self) -> None:
        for worker in (self._worker, self._online_worker):
            try:
                worker.stop()
                worker.wait(1500)
            except Exception:  # noqa: BLE001
                pass
        TILE_CACHE.clear()
        SOURCE_CACHE.clear()
        ONLINE_CACHE.clear()


class _VPShim:
    """把缓存的视图元组伪装成 viewport，只为复用 `_key()`。"""

    __slots__ = ("scale", "tx", "ty", "width", "height", "dpr")

    def __init__(self, view: tuple) -> None:
        self.scale, self.tx, self.ty, self.width, self.height, self.dpr = view
