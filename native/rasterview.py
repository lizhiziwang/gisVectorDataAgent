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

关键取舍：**每层渲染成一张整视口大小的透明图**（和矢量 `_LayerJob` 完全一致的语义），
这样合成端就是一句 `drawImage(0, 0, img)`，不需要在合成时再做坐标变换。
代价是一张小栅格也要占一张视口图（1440×900 约 5 MB），换来的是合成逻辑零分叉。
"""
from __future__ import annotations

import threading
import time

import numpy as np

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage, QPainter, Qt

from app.raster import RasterError, percentile_stretch, read_window

#: 单层栅格渲染缓存最多留几份（来回缩放直接命中，对应 QGIS 的 cache 容量）
RASTER_CACHE_MAX = 6


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


def render_layer_image(layer, vp: RasterViewPort) -> QImage | None:
    """把一个栅格图层渲染成整视口大小的透明图（核心，等价 render()）。

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
                          bands=bands, resampling=resampling)
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
    frameDone = Signal(int)                    # epoch

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending: _RasterJob | None = None
        self._current: _RasterJob | None = None
        self._wake = threading.Event()
        self._stop = False

    def submit(self, job: _RasterJob) -> None:
        with self._lock:
            old = self._pending
            self._pending = job
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
            try:
                img = render_layer_image(it.layer, job.viewport)
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
        self._worker.frameDone.connect(self._on_frame)
        self._worker.start()

        self.layers: list = []
        self.visible: dict[str, bool] = {}
        self._epoch = 0
        self._imgs: dict[str, QImage] = {}
        #: 图层 id → (缓存键, 图像)：跨帧复用（切显隐/调顺序不重读）
        self._cache: dict[str, tuple] = {}
        self._req_view: tuple | None = None

        #: 外部挂的信号处理（由 MapView 连接）
        self.on_layer = None       # (lid, img) -> None
        self.on_frame = None       # () -> None

    # ------------------------------------------------------------------ 配置
    def set_layers(self, layers: list) -> None:
        self.layers = list(layers)
        keep = {l.id for l in self.layers}
        for lid in list(self._cache):
            if lid not in keep:
                self._cache.pop(lid, None)
                self._imgs.pop(lid, None)

    def set_visible(self, vis: dict[str, bool]) -> None:
        self.visible = dict(vis or {})

    def bump_rev(self, lid: str) -> None:
        """图层样式变了：丢掉它的缓存，下次重画。"""
        self._cache.pop(lid, None)
        self._imgs.pop(lid, None)

    def clear(self) -> None:
        self._cache.clear()
        self._imgs.clear()
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
            if self.on_frame:
                self.on_frame()
            return False

        self._epoch += 1
        epoch = self._epoch
        self._req_view = (viewport.scale, viewport.tx, viewport.ty,
                          viewport.width, viewport.height, viewport.dpr)

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
            if self.on_frame:
                self.on_frame()
            return False

        job = _RasterJob(epoch, items, viewport, self._req_view)
        self._worker.submit(job)
        return True

    def _key(self, lay, vp: RasterViewPort) -> tuple:
        return (float(vp.scale), float(vp.tx), float(vp.ty),
                int(vp.width), int(vp.height), float(vp.dpr),
                int(getattr(lay, "rev", 0)))

    # ------------------------------------------------------------------ 回调
    def _on_layer(self, lid: str, img, epoch: int) -> None:
        if epoch != self._epoch:
            return                       # 过期世代：视图已经变了
        self._imgs[lid] = img
        lay = next((l for l in self.layers if l.id == lid), None)
        if lay is not None and self._req_view is not None:
            # 记进缓存：键用**这次请求**的视图，下次同视图直接复用
            self._cache[lid] = (self._key(lay, _VPShim(self._req_view)), img)
        if self.on_layer:
            self.on_layer(lid, img)

    def _on_frame(self, epoch: int) -> None:
        if epoch != self._epoch:
            return
        if self.on_frame:
            self.on_frame()

    def shutdown(self) -> None:
        try:
            self._worker.stop()
            self._worker.wait(1500)
        except Exception:  # noqa: BLE001
            pass


class _VPShim:
    """把缓存的视图元组伪装成 viewport，只为复用 `_key()`。"""

    __slots__ = ("scale", "tx", "ty", "width", "height", "dpr")

    def __init__(self, view: tuple) -> None:
        self.scale, self.tx, self.ty, self.width, self.height, self.dpr = view
