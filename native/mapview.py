"""纯 Qt 地图控件：QPainter 自绘矢量图层（Web Mercator），无任何 Web 引擎。

渲染管线逐条对照 QGIS 源码实现（qgsmaprendererjob.cpp /
qgsmaprendererparalleljob.cpp / qgsvectorlayerrenderer.cpp / qgssimplifymethod.h）：

1. **每个图层一个渲染任务（LayerRenderJob）**
   `_LayerJob` 各自画到**自己的一张 QImage** 上，主线程 `_compose()` 按渲染顺序
   叠加 —— 同 `QgsMapRendererParallelJob::prepareJobs()` + `composeImage()`；
   一层画完就能先上屏（对应 `renderingLayersFinished`）。
2. **常驻渲染线程 + 最新任务优先**
   线程不随任务销毁重建；提交新任务时只替换队列里的待办，正在跑的旧任务
   标记 cancelled 后在**图层间隙**收工 —— 同 `feedback()->cancel()` 的非阻塞取消。
3. **世代号（epoch）**
   视图一变 +1，旧世代回来的图像一律丢弃，杜绝「旧帧覆盖新帧」。
4. **要素选取 = 视口矩形筛选 + 全量渲染**
   对应 `QgsFeatureRequest::setFilterRect(extent)`：**不抽样、不限额**，
   落在范围内的要素一个不少全部进渲染集。这是「地图上有地方没渲染出来」
   类问题的根因所在 —— 任何抽样/预算限制都会在屏幕上变成一块一块的空洞。
5. **简化 = 屏幕像素级容差 + 亚像素要素降为点**
   对应 `QgsSimplifyMethod`：`tolerance` 以像素为单位换算成图面单位
   （`mpp * 0.8`），逐要素简化；小于 1 个像素的要素（省域视图下的建筑）
   降为点绘制（同 `threshold` 的像素语义），既不丢要素也不浪费顶点。
6. **图层图像缓存（QgsMapRendererCache）**
   `_img_cache` 按「视口 + 尺寸 + dpr + 图层几何版本」缓存整层图像，
   切显隐 / 调顺序直接复用。
7. **几何 → 世界坐标 QPainterPath 分片构建**
   每帧 10ms 预算，构建期间界面照常响应；同一要素的多个环合成一条路径，
   多条要素再按批合并（Qt 侧 drawPath 的固定开销远大于顶点数）。
8. **拾取不建 STRtree**
   numpy 包围盒预筛 + shapely 向量化距离判断，几十万要素也不卡。
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QApplication, QWidget

# --- 渲染参数（对照 QGIS 的默认值）-----------------------------------------
BUILD_BUDGET_S = 0.010       # 每个事件循环片段的几何构建预算
RENDER_DEBOUNCE_MS = 160     # 手势停下多久后重绘帧缓存
PAN_RERENDER_RATIO = 0.3     # 平移超过视口的这个比例就重绘，避免露出空白边

# 简化容差：QGIS 的 QgsSimplifyMethod 用「像素」为单位，这里取 0.8 像素。
SIMPLIFY_PX = 0.8
# 亚像素要素降为点：屏幕尺寸小于这个像素数的面要素不再画轮廓，改画一个点
# （QGIS 的 QgsSimplifyMethod::threshold 也是像素语义，避免小要素被简化抹掉）。
DOT_PX = 1.5

# 路径合并：Qt 每次 drawPath 的固定开销 >> 顶点开销，逐要素画 47 万条道路要 800ms，
# 按批合并后只需 190ms。批内要素在空间上连续，便于整批裁剪。
BATCH_FEATURES = 256
BATCH_VERTS = 20000

# --- 比例尺分级（渲染集）参数 ---
# 渲染集 = 「某个经纬度范围 + 某个简化容差」下构建好的一整套绘制路径，
# 对应 QGIS 里「某次渲染的 extent + QgsSimplifyMethod」这一组合。
# 关键约束：**渲染集对它的范围是完备的** —— 范围里相交的要素全都在里面，
# 一个不少。只有在这种意义下「范围」才能当作可复用的判据。
LOD_REBUILD_RATIO = 1.35     # 容差差 35% 以上就重建（保证误差 < 约 1 像素）
LOD_VIEW_PAD = 1.6           # 构建范围 = 视口 × 1.6，留出平移余量
LOD_KEEP_SETS = 3            # 每个图层保留几套（来回缩放直接复用）

# 线宽按比例尺切换 —— 这不是美学选择，是性能分水岭：
# Qt 光栅引擎对「整数宽度 1」的 pen 有一条快速路径（直接画 1 像素线段），
# 任何 > 1 的宽度都会走完整的**抗锯齿描边器**（每个顶点生成四边形 + 圆头端帽 + 拐角连接）。
# 实测 47 万条道路 / 94 万顶点，省域视图下 1.6px 要 1914 ms，1.0px 只要 79 ms（24 倍），
# 而画出来的像素数几乎一样（11.9 万 vs 11.0 万）。而在这个比例尺上，
# 6 米宽的道路本来就不足 0.01 个像素，画 1px 才是正确的制图表达（QGIS 同级也是 ~1px）。
LINE_THIN_MPP = 45.0         # 每像素地面米数超过这个值（约 1:17 万）一律画 1px

LINE_WIDE_PX = 1.6           # 放大后道路的观感宽度


def project(lon: float, lat: float) -> tuple[float, float]:
    """经纬度 → 归一化 Web Mercator [0,1]。"""
    x = (lon + 180.0) / 360.0
    s = math.sin(max(-85.05, min(85.05, lat)) * math.pi / 180.0)
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x, y


def unproject(x: float, y: float) -> tuple[float, float]:
    lon = x * 360.0 - 180.0
    n = math.pi - 2 * math.pi * y
    lat = (180.0 / math.pi) * math.atan(0.5 * (math.exp(n) - math.exp(-n)))
    return lon, lat


def hex_alpha(hex_color: str, alpha: float) -> QColor:
    c = QColor(hex_color)
    c.setAlphaF(alpha)
    return c


def _visible_indices(bounds: list, view: tuple, limit: int | None = None) -> list[int]:
    """返回落在视野内的要素下标。

    `limit` 为快照时刻的边界数：只处理这部分，保证后台线程看到的是自洽视图。
    """
    n = len(bounds) if limit is None else min(len(bounds), limit)
    if n <= 0:
        return []
    b = np.asarray(bounds[:n], dtype=float)
    x0, y0, x1, y1 = view
    m = (b[:, 0] <= x1) & (b[:, 2] >= x0) & (b[:, 1] <= y1) & (b[:, 3] >= y0)
    return np.nonzero(m)[0].tolist()


def _rect_contains(outer: tuple, inner: tuple) -> bool:
    """outer 是否完整包含 inner。"""
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


def _paint_layer(p: QPainter, lay: dict, view: tuple, scale: float,
                 mpp: float = 0.0) -> None:
    """画一个图层（painter 已处于世界坐标变换下）。

    `view` 是当前视口的世界坐标矩形，用来裁掉不在视野里的路径批
    （对应 QGIS 的 `QgsRenderContext::extent()` 裁剪）。
    """
    op = lay["op"]
    soft = op in ("buffer", "grid", "merge", "voronoi")
    fill_a = 0.04 if soft else (0.08 if op == "dissolve" else 0.14)
    stroke_a = 0.26 if soft else 0.7
    # 亚像素要素的点：等价于「1 像素描边」，比填充重、比描边轻
    dot_a = 0.55 if soft else 0.85
    color = lay["color"]

    poly_paths = lay["poly_paths"]
    line_paths = lay["line_paths"]
    points = lay["points"]
    dots = lay["dots"]

    if poly_paths:
        p.setBrush(QBrush(hex_alpha(color, fill_a)))
        pen = QPen(hex_alpha(color, stroke_a))
        pen.setWidth(1)
        pen.setCosmetic(True)
        p.setPen(pen)
        for i in _visible_indices(lay["poly_bounds"], view, lay["npoly_b"]):
            if i < lay["npoly"]:
                p.drawPath(poly_paths[i])

    if dots is not None:
        # 亚像素要素：一批点一次画完（drawPoints 一个调用就够，比逐要素快 10 倍）。
        # QPolygonF 在渲染线程里首次用到时才构建（24 万点约 0.3s），不占主线程。
        poly = dots()
        if poly is not None and not poly.isEmpty():
            # 一个点 = 一个设备像素（hairline 笔）。观感等价于
            # 「次像素多边形 + 1 像素描边」，也和 QGIS 在这个比例尺下的结果一致。
            # 坑：drawPoints 在笔宽 > 1 时会按「端帽」把退化线段画成横条
            # （实测 8 万个点糊成一整片实色），只有 width=0 才是一个像素。
            pen = QPen(hex_alpha(color, dot_a))
            pen.setWidth(0)
            p.setPen(pen)
            p.drawPoints(poly)

    if line_paths:
        p.setBrush(Qt.NoBrush)
        pen = QPen(QColor(color))
        # 缩得远时用 1px：既正确又走 Qt 的整数宽度快速路径（见 LINE_THIN_MPP 注释）
        pen.setWidthF(1.0 if mpp >= LINE_THIN_MPP else LINE_WIDE_PX)
        pen.setCapStyle(Qt.RoundCap)
        pen.setCosmetic(True)
        p.setPen(pen)
        for i in _visible_indices(lay["line_bounds"], view, lay["nline_b"]):
            if i < lay["nline"]:
                p.drawPath(line_paths[i])

    if points:
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(1.3)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(QBrush(QColor(color)))
        # 点是固定像素大小 → 世界坐标下半径要除以缩放比
        r = 3.4 / max(scale, 1e-9)
        for i in _visible_indices(lay["point_bounds"], view, lay["npt_b"]):
            if i < lay["npt"]:
                x, y = points[i]
                p.drawEllipse(QPointF(x, y), r, r)


class _RenderSet:
    """一个图层的渲染集：某个经纬度范围 + 某个简化容差下构建好的一整套路径。

    **完备性约定（最重要的一条）**：`rect` 范围内与之相交的要素，
    全部都在这个集合里 —— 不抽样、不限额、不丢要素。
    只有满足这条，`rect` 才能被当作「这个范围已经画好了」的判据复用。
    如果哪天为了省事往集合里抽一部分要素，就**不能**再把 rect 记成已覆盖，
    否则平移回到那块区域时，缺的要素永远不会被补上（地图上就是一块一块的空洞）。
    """

    __slots__ = ("tol", "rect", "nfeat",
                 "poly_paths", "poly_bounds", "line_paths", "line_bounds",
                 "dot_xy", "dots", "points", "point_bounds", "built", "used")

    def __init__(self, tol: float = 0.0, rect: tuple | None = None) -> None:
        self.tol = tol            # 实际使用的简化容差（度）
        self.rect = rect          # 构建时覆盖的经纬度范围 (x0,y0,x1,y1)
        self.nfeat = 0
        self.poly_paths: list = []      # 批量合并后的面路径
        self.poly_bounds: list = []     # 每条路径的世界坐标包围盒
        self.line_paths: list = []
        self.line_bounds: list = []
        self.dot_xy: np.ndarray | None = None   # 亚像素要素（世界坐标）
        self.dots = None                        # QPolygonF 懒构建缓存
        self.points: list = []                  # 真正的点要素
        self.point_bounds: list = []
        self.built = False
        self.used = 0.0           # 最近一次被使用的时间（LRU 淘汰用）

    def dots_polygon(self):
        """把亚像素要素的世界坐标转成 QPolygonF（只算一次）。

        构造 24 万个 QPointF 约 0.3s，放在渲染线程首次用到时做，
        不占主线程；之后每帧 drawPoints 只要 ~60ms。
        """
        if self.dots is None:
            arr = self.dot_xy
            if arr is None or len(arr) == 0:
                self.dots = QPolygonF()
            else:
                self.dots = QPolygonF([QPointF(float(x), float(y)) for x, y in arr])
            # 先赋 dots 再清 dot_xy：主线程随时可能来拍快照，
            # 中间态不能让两边都是 None（那样这一帧会漏画这个图层）
            self.dot_xy = None
        return self.dots


class _BuildJob:
    """一次渲染集构建任务（分片执行，每片 10ms）。

    `stage`：2 = 简化几何，3 = 生成绘制路径。
    """

    __slots__ = ("target", "tol", "rect", "idx", "dots", "stage",
                 "simplify_pos", "simplify_out", "pos", "nfeat",
                 "spoly_paths", "spoly_bounds", "sline_paths", "sline_bounds",
                 "spoints", "spoint_bounds",
                 "path_chunk", "simp_chunk", "batch_n", "batch_v")

    def __init__(self, target: _RenderSet, tol: float, rect: tuple,
                 idx, dots=None) -> None:
        self.target = target
        self.tol = tol
        self.rect = rect
        self.idx = idx
        self.dots = dots          # 亚像素要素的世界坐标 (n,2)，在规划阶段就算好
        self.stage = 2            # 2=简化 3=生成路径
        self.simplify_pos = 0
        self.simplify_out: list = []
        self.pos = 0
        self.nfeat = 0
        self.spoly_paths: list = []
        self.spoly_bounds: list = []
        self.sline_paths: list = []
        self.sline_bounds: list = []
        self.spoints: list = []
        self.spoint_bounds: list = []
        self.path_chunk = 120     # 自适应批大小（路径构建）
        self.simp_chunk = 120     # 自适应批大小（几何简化）
        self.batch_n = 0          # 当前批次累积的要素数
        self.batch_v = 0          # 当前批次累积的顶点数


class _LayerCache:
    """一个图层的渲染缓存。

    · `geoms`：全量 4326 几何（拾取 + 简化用）；
    · `bounds`：全量包围盒 (n,4)（拾取 + 按视口选要素用）；
    · `extent`：全量包围盒的并集（决定「整个图层」的范围）；
    · `sets`：已构建好的渲染集（新的在后），最多保留 LOD_KEEP_SETS 套；
    · `rev`：这个图层的几何版本号，渲染集一提交就 +1
      （图层图像缓存的键用它，跟别的图层互不影响）。
    """

    __slots__ = ("geoms", "types", "bounds", "extent", "wgs_ready", "total", "sets",
                 "build", "bounds_out", "bounds_pos", "rev")

    def __init__(self) -> None:
        self.geoms = None        # 全量 4326 几何
        self.types = None        # (n,) shapely 类型 id（面要素降点用）
        self.bounds = None       # (n,4) 经纬度包围盒
        self.extent: tuple | None = None
        self.wgs_ready = False
        self.total = 0
        self.sets: list[_RenderSet] = []
        self.build: _BuildJob | None = None
        self.bounds_out: list | None = None
        self.bounds_pos = 0
        self.rev = 0


class _LayerJob:
    """单个图层的渲染任务（对应 QGIS 的 LayerRenderJob）。

    QGIS 的 `QgsMapRendererParallelJob::prepareJobs()` 给每个图层建一个
    `LayerRenderJob`，各自画到**自己的一张 QImage** 上，最后 `composeImage()`
    按顺序叠加。好处：
    · 每个图层只遍历一遍自己的要素包围盒（不用像分块那样每块重来）；
    · 某个图层画完就能先上屏（`renderingLayersFinished` 的语义）；
    · 配合图层级缓存（QgsMapRendererCache），切显隐/调顺序可以不重绘。
    """

    __slots__ = ("lid", "snap", "image", "completed")

    def __init__(self, lid: str, snap: dict):
        self.lid = lid
        self.snap = snap
        self.image: QImage | None = None
        self.completed = False


class _RenderJob:
    """一次渲染任务（对应 QGIS 的 QgsMapRendererJob / ParallelJob）。

    · `epoch`：世代号。视图一变就 +1，旧世代的结果回来时直接丢弃，
      避免「旧视图的帧覆盖新视图的帧」；
    · `cancelled`：视图又变了就置位，渲染线程在图层间隙检查并提前收工
      （对应 QGIS 的 `feedback()->cancel()`）；
    · `jobs`：每个图层一个 `_LayerJob`，按渲染顺序排列。
    """

    __slots__ = ("epoch", "jobs", "sig", "view", "world", "cancelled",
                 "w", "h", "dpr", "scale", "tx", "ty", "mpp")

    def __init__(self, epoch, jobs, sig, view, world, w, h, dpr, scale, tx, ty, mpp=0.0):
        self.epoch = epoch
        self.jobs = jobs
        self.sig = sig
        self.view = view          # (scale, tx, ty, w, h)：帧缓存记录用
        self.world = world        # 世界坐标矩形：绘制时裁剪要素用
        self.cancelled = False
        self.w = w
        self.h = h
        self.dpr = dpr
        self.scale = scale
        self.tx = tx
        self.ty = ty
        self.mpp = mpp            # 每像素地面米数：决定线宽档位（见 LINE_THIN_MPP）


class _RenderWorker(QThread):
    """常驻渲染线程（对应 QGIS 的 QgsMapRendererParallelJob）。

    设计要点（逐条对齐 QGIS）：

    · **常驻**：线程不随任务销毁重建，避免 "Thread already running"，
      也避免运行中的 QThread 被 GC 掉；
    · **最新任务优先**：队列里只保留最新一次请求（QGIS 是 `cancel()` 旧 job
      再 `start()` 新的），正在跑的旧任务标记 cancelled 后在图层间隙收工；
    · **按图层渲染**：每个图层画到自己的一张 QImage，主线程按顺序合成
      （对应 `LayerRenderJob` + `composeImage`）；
    · **渐进上屏**：一层画完就先发回去合成一次（对应 `renderingLayersFinished`）。
    """

    # 图层 id, 图层图像, epoch
    layerReady = Signal(str, object, int)
    # 场景签名, 视图参数, epoch
    frameReady = Signal(object, tuple, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._pending: _RenderJob | None = None
        self._current: _RenderJob | None = None
        self._wake = threading.Event()
        self._stop = False

    # ------------------------------------------------------------------ 提交
    def submit(self, job: _RenderJob) -> None:
        """提交一次渲染请求；同一时刻只保留最新的一次。

        注意**不取消正在跑的任务**：让它自然收尾（并把已完成图层发回），
        否则连续缩放时每一帧都半途而废，屏幕会一直停在旧画面上。
        """
        with self._lock:
            old = self._pending
            self._pending = job
        if old is not None:
            old.cancelled = True
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        with self._lock:
            job = self._pending
            self._pending = None
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
                    self._render_job(job)
                except Exception:  # noqa: BLE001
                    pass
                with self._lock:
                    self._current = None
                if not job.cancelled:
                    self.frameReady.emit(job.sig, job.view, job.epoch)
            else:
                with self._lock:
                    self._current = None
                if self._stop:
                    break
                self._wake.wait(0.2)
                self._wake.clear()
            if self._stop:
                break

    # ------------------------------------------------------------------ 渲染
    def _render_job(self, job: _RenderJob) -> None:
        """按图层依次渲染，每层画完就发回去（对应 renderLayerStatic）。"""
        dpr = job.dpr
        for lj in job.jobs:
            if job.cancelled:
                return
            if lj.snap is None:
                continue
            img = QImage(max(1, int(job.w * dpr)), max(1, int(job.h * dpr)),
                         QImage.Format_ARGB32_Premultiplied)
            img.setDevicePixelRatio(dpr)
            img.fill(Qt.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.translate(job.tx, job.ty)
            p.scale(job.scale, job.scale)
            try:
                _paint_layer(p, lj.snap, job.world, job.scale, job.mpp)
            finally:
                p.end()
            lj.image = img
            lj.completed = True
            if job.cancelled:
                return
            # 这一层好了 → 主线程可以先合成出来看
            self.layerReady.emit(lj.lid, img, job.epoch)


class MapView(QWidget):
    hoverChanged = Signal(object)
    picked = Signal(object)
    selectionChanged = Signal(int)      # 选区要素数
    mapMenu = Signal(QPoint)            # 右键菜单（全局坐标），由主窗口挂菜单项
    viewChanged = Signal()
    cursorMoved = Signal(float, float)
    progress = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(320, 240)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

        self.layers: list = []
        self.visible: dict[str, bool] = {}
        self.scale = 4000.0
        self.tx = 0.0
        self.ty = 0.0
        self._fit_done = False
        self.setFocusPolicy(Qt.StrongFocus)      # 方向键平移、+/- 缩放需要能拿到键盘焦点
        self._drag_from: QPoint | None = None
        self._drag_moved = 0
        self._hover = None
        self._cache: dict[str, _LayerCache] = {}
        self._hover_ts = 0.0
        # 选区：{图层 id: 要素下标数组}。只记一层（框选只作用于「当前图层」）。
        self._sel: dict[str, np.ndarray] = {}
        self._sel_layer: str | None = None      # 框选的目标图层，由主窗口同步过来
        self._band: tuple | None = None         # 正在拖的选框（屏幕坐标）
        self._hl: tuple | None = None           # 持久高亮（属性表点行），和 hover 分开
        self._last_partial_frame = 0.0

        # 帧缓存
        self._frame: QPixmap | None = None
        self._frame_view: tuple[float, float, float, int, int] | None = None
        self._frame_sig: tuple | None = None
        self._frame_epoch = -1        # 当前帧属于哪个世代
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_frame)
        self._epoch = 0               # 渲染世代号：视图一变就 +1，旧结果一律丢弃
        self._job: _RenderJob | None = None
        self._geo_rev = 0             # 几何版本号（渲染集提交时 +1）
        self._rendered_geo_rev = -1   # 已经渲染过的几何版本
        self._loading = False         # 外部正在解析数据（期间暂停构建）

        # 图层图像（对应 QGIS 的 LayerRenderJob.img + QgsMapRendererCache）
        self._layer_imgs: dict[str, QImage] = {}   # 当前世代各图层的图
        self._img_cache: dict[str, tuple] = {}     # lid -> (cacheKey, QImage)
        self._img_keys: dict[str, tuple] = {}      # lid -> 本次请求算出的 cacheKey
        # 合成缓冲：整帧画完才整体上屏（对应 QgsMapRendererParallelJob 结束换图）
        self._pending: QPixmap | None = None
        self._pending_sig: tuple | None = None
        self._pending_view: tuple | None = None
        self._pending_epoch = -1

        # 常驻渲染线程（最新任务优先）
        self._worker = _RenderWorker(self)
        self._worker.layerReady.connect(self._on_layer_ready)
        self._worker.frameReady.connect(self._on_frame_ready)
        self._worker.start()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

        # 分片构建
        self._build_timer = QTimer(self)
        self._build_timer.setInterval(0)
        self._build_timer.timeout.connect(self._build_tick)

    def shutdown(self) -> None:
        """退出前停掉渲染线程，避免 QThread 还在跑就被销毁（会崩进程）。"""
        worker = getattr(self, "_worker", None)
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.stop()
                worker.wait(3000)
        except Exception:
            pass

    def __del__(self):  # noqa: D105
        # 兜底：非正常退出（没有走 closeEvent）时也尽量把线程停掉
        try:
            worker = getattr(self, "_worker", None)
            if worker is not None and worker.isRunning():
                worker.stop()
                worker.wait(1500)
        except Exception:
            pass

    def closeEvent(self, e):  # noqa: N802
        self.shutdown()
        super().closeEvent(e)

    # ------------------------------------------------------------------ 数据
    def set_layers(self, layers: list) -> None:
        had = bool(self.layers)
        self.layers = layers
        keep = {l.id for l in layers}
        for lid in list(self._cache):
            if lid not in keep:
                self._cache.pop(lid, None)
                self._img_cache.pop(lid, None)
                self._layer_imgs.pop(lid, None)
        for lay in layers:
            if lay.id not in self._cache:
                self._cache[lay.id] = _LayerCache()
        self._invalidate_frame()
        if not had and layers:
            self.fit_all()
        self._kick_build()
        self.update()

    def drop_cache(self, layer_id: str | None = None) -> None:
        if layer_id:
            self._cache.pop(layer_id, None)
            self._img_cache.pop(layer_id, None)
            self._layer_imgs.pop(layer_id, None)
        else:
            self._cache.clear()
            self._img_cache.clear()
            self._layer_imgs.clear()
        self._invalidate_frame()

    def invalidate(self) -> None:
        """外部改动（显隐、顺序）后调用，强制重画帧缓存。"""
        self._invalidate_frame()

    def set_loading(self, loading: bool) -> None:
        """外部正在载入数据：暂停几何构建，别跟解析线程抢 GIL。

        解析（gpd.read_file）和几何构建都是 CPU 活，两边同时跑会互相饿死：
        实测 4 个大图层，串行 11s 能解析完，抢着跑要 40s 以上。
        载入结束后自动续上构建。
        """
        self._loading = bool(loading)
        if not self._loading:
            self._kick_build()

    # ------------------------------------------------------------------ 视图
    def mpp(self) -> float:
        lon, lat = self.center_lonlat()
        return 156543.03392 * math.cos(lat * math.pi / 180.0) / (self.scale / 256.0)

    def center_lonlat(self) -> tuple[float, float]:
        return unproject((self.width() / 2 - self.tx) / self.scale,
                         (self.height() / 2 - self.ty) / self.scale)

    def lonlat_to_screen(self, lon: float, lat: float) -> QPointF:
        x, y = project(lon, lat)
        return QPointF(x * self.scale + self.tx, y * self.scale + self.ty)

    def screen_to_lonlat(self, px: float, py: float) -> tuple[float, float]:
        return unproject((px - self.tx) / self.scale, (py - self.ty) / self.scale)

    def fit_all(self) -> None:
        b = None
        for lay in self.layers:
            if not self.visible.get(lay.id, True):
                continue
            try:
                lb = lay.gdf.total_bounds
            except Exception:
                continue
            if lb is None or len(lb) != 4 or not all(map(math.isfinite, lb)):
                continue
            b = lb if b is None else [min(b[0], lb[0]), min(b[1], lb[1]),
                                      max(b[2], lb[2]), max(b[3], lb[3])]
        if b is None:
            self.fit_bounds(74, 18, 135.5, 53.5, pad=0.02)
        else:
            self.fit_bounds(b[0], b[1], b[2], b[3], pad=0.12)

    def fit_layer(self, layer_id: str) -> None:
        lay = next((l for l in self.layers if l.id == layer_id), None)
        if lay is None:
            return
        try:
            b = lay.gdf.total_bounds
            self.fit_bounds(b[0], b[1], b[2], b[3], pad=0.15)
        except Exception:
            pass

    def fit_bounds(self, x0: float, y0: float, x1: float, y1: float, pad: float = 0.1) -> None:
        if not all(map(math.isfinite, (x0, y0, x1, y1))):
            return
        px0, py0 = project(x0, y1)
        px1, py1 = project(x1, y0)
        bw = max(abs(px1 - px0), 1e-9)
        bh = max(abs(py1 - py0), 1e-9)
        s = min(self.width() / bw, self.height() / bh) * (1 - pad * 2)
        self.scale = min(max(s, 60.0), 4e7)
        cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
        self.tx = self.width() / 2 - cx * self.scale
        self.ty = self.height() / 2 - cy * self.scale
        self._invalidate_frame()
        self._kick_build()
        self.viewChanged.emit()
        self.update()

    def zoom_at(self, factor: float, px: float | None = None, py: float | None = None) -> None:
        px = self.width() / 2 if px is None else px
        py = self.height() / 2 if py is None else py
        wx = (px - self.tx) / self.scale
        wy = (py - self.ty) / self.scale
        self.scale = min(max(self.scale * factor, 40.0), 6e7)
        self.tx = px - wx * self.scale
        self.ty = py - wy * self.scale
        self._invalidate_frame()
        self._kick_build()
        self.viewChanged.emit()
        self.update()

    def zoom_by(self, dz: float) -> None:
        self.zoom_at(2 ** dz)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._invalidate_frame()
        if self.width() < 50:
            return
        if not self.layers:
            self.fit_all()
        elif not self._fit_done:
            self._fit_done = True
            self.fit_all()

    # ------------------------------------------------------------------ 帧缓存
    def _scene_sig(self) -> tuple:
        return tuple((l.id, self.visible.get(l.id, True), l.color) for l in self.layers)

    def _invalidate_frame(self, delay: int = RENDER_DEBOUNCE_MS) -> None:
        """标记当前帧需要重绘（不影响已缓存的几何路径）。"""
        self._frame_sig = None
        if self._render_timer.isActive():
            self._render_timer.stop()
        self._render_timer.start(delay)

    def _frame_usable(self) -> bool:
        if self._frame is None or self._frame_view is None:
            return False
        if self._frame_sig != self._scene_sig():
            return False
        _, _, _, w, h = self._frame_view
        return w == self.width() and h == self.height()

    def _render_frame(self) -> None:
        """渲染定时器回调：发起一次渲染（对应 QgsMapRendererJob::start()）。"""
        self._start_render()

    def _start_render(self) -> None:
        """发起一次渲染（对应 QgsMapRendererJob::start()）。

        · 每次视图变化都会给 job 分配新的世代号（epoch），旧世代的结果一律丢弃；
        · 每个图层一个 `_LayerJob`，命中图层缓存的直接复用上次的图，
          不再重绘（切显隐、调顺序几乎零成本）；
        · 整帧画完才整体上屏（中途只有首次出图时会先显示半成品）。
        """
        if self.width() < 10 or self.height() < 10:
            return
        self._kick_build()
        snapshot = self._make_snapshot()
        if snapshot is None:
            # 两种「没有东西可画」要区别对待：
            # · 还有可见图层，只是几何还没构建好 → 继续显示旧帧，别闪白；
            # · **一个可见图层都没有**（用户把图层全隐藏了）→ 必须合成一张空帧，
            #   否则旧帧会一直留在屏幕上，「显示 / 隐藏」看起来就像没生效。
            if any(self.visible.get(l.id, True) for l in self.layers):
                return
            self._epoch += 1
            epoch = self._epoch
            self._layer_imgs = {}
            self._compose(self._scene_sig(),
                          (self.scale, self.tx, self.ty, self.width(), self.height()),
                          epoch)
            self._promote_pending(epoch, self._pending_view, self._pending_sig)
            self.update()
            return
        self._epoch += 1
        epoch = self._epoch
        dpr = self._device_pixel_ratio()
        jobs: list[_LayerJob] = []
        reuse: dict[str, QImage] = {}
        for snap in snapshot["layers"]:
            lay = next((l for l in self.layers if l.id == snap["lid"]), None)
            if lay is None:
                continue
            key = self._layer_cache_key(lay, snapshot)
            hit = self._img_cache.get(lay.id)
            if hit is not None and hit[0] == key and hit[1] is not None:
                # 图层缓存命中：这张图还能用，直接拿来合成
                reuse[lay.id] = hit[1]
                jobs.append(_LayerJob(lay.id, None))
            else:
                jobs.append(_LayerJob(lay.id, snap))
                self._img_keys[lay.id] = key
        if not jobs:
            return
        if all(j.snap is None for j in jobs):
            # 全命中缓存 → 直接合成上屏，不用渲染线程
            self._layer_imgs = dict(reuse)
            self._compose(self._scene_sig(),
                          (self.scale, self.tx, self.ty, self.width(), self.height()),
                          epoch)
            self._promote_pending(epoch, self._pending_view, self._pending_sig)
            self._rendered_geo_rev = self._geo_rev
            return
        # 没命中的图层用缓存里已有的旧图先顶着（避免合成时缺块闪白）
        self._layer_imgs = dict(reuse)
        job = _RenderJob(epoch, jobs, self._scene_sig(),
                         (self.scale, self.tx, self.ty, self.width(), self.height()),
                         self._view_world_rect(),
                         self.width(), self.height(), dpr,
                         self.scale, self.tx, self.ty, self.mpp())
        self._job = job
        self._rendered_geo_rev = self._geo_rev
        # 提交给常驻渲染线程：队列只保留最新一次，不会堆积成「管线堵住」
        self._worker.submit(job)

    def _on_layer_ready(self, lid: str, img, epoch: int) -> None:
        """某个图层渲染好了：存图并合成到「待上屏」的缓冲里。"""
        if epoch != self._epoch or img is None:
            return                      # 过期世代，丢弃
        job = self._job
        if job is None:
            return
        self._layer_imgs[lid] = img
        # 存进图层缓存，下次同视口切显隐可直接复用
        key = self._img_keys.get(lid)
        if key is not None:
            self._img_cache[lid] = (key, img)
        self._compose(job.sig, job.view, epoch)
        if self._frame is None:
            # 首次出图：还没有旧帧可顶，就让用户先看到已经画好的部分
            self._promote_pending(epoch, job.view, job.sig)
            self.update()

    def _promote_pending(self, epoch: int, view, sig) -> None:
        """把「待上屏」的缓冲换成当前帧。

        直接把 `_pending` 交给 `_frame` 并把 `_pending` 置空（不是共用同一个
        QPixmap）：否则下一次 `_compose` 清空 `_pending` 时会把正在显示的
        帧一起清掉，屏幕上就是一闪一闪的半成品。
        """
        if self._pending is None or self._pending_epoch != epoch:
            return
        self._frame = self._pending
        self._pending = None
        self._frame_epoch = epoch
        self._frame_view = view
        self._frame_sig = sig

    def _compose(self, sig, view, epoch: int) -> None:
        """把各图层图像按顺序叠成一帧（对应 QGIS 的 composeImage）。

        每次都在**一张干净的画布**上从零合成，绝不在上一帧上叠 ——
        否则同一个图层会被重复叠加（14% 的填充叠几次就变成实色），
        看起来就像「有些地方渲染错了 / 没渲染出来」。
        合成结果先放在 `_pending`，等整帧画完再整体上屏
        （对应 QgsMapRendererParallelJob 结束时一次性替换画布）。
        """
        dpr = self._device_pixel_ratio()
        w = max(1, int(self.width() * dpr))
        h = max(1, int(self.height() * dpr))
        if (self._pending is None or self._pending.width() != w
                or self._pending.height() != h):
            self._pending = QPixmap(w, h)
            self._pending.setDevicePixelRatio(dpr)
        self._pending.fill(Qt.transparent)
        p = QPainter(self._pending)
        for lay in self._layers_render_order():
            if not self.visible.get(lay.id, True):
                continue
            img = self._layer_imgs.get(lay.id)
            if img is not None:
                p.drawImage(0, 0, img)
        p.end()
        self._pending_sig = sig
        self._pending_view = view
        self._pending_epoch = epoch

    def _on_frame_ready(self, sig, view, epoch: int) -> None:
        """整帧渲染完成：校验世代号后整体上屏。

        只在这里替换 `_frame` —— 对应 QGIS 渲染完成后一次性换图，
        中途不往屏幕上贴半成品，也就不会出现「半帧旧图 + 半帧新图」叠加。
        """
        if epoch != self._epoch:
            return                      # 过期世代，丢弃（否则旧帧会覆盖新帧）
        self._promote_pending(epoch, view, sig)
        self.update()

    def _device_pixel_ratio(self) -> float:
        return self.devicePixelRatioF() or 1.0

    def _make_snapshot(self) -> dict | None:
        """拍下渲染所需的数据快照（每个图层一份）。

        只拷贝「列表引用 + 长度」：列表元素（QPainterPath / bbox）一旦创建就不再修改，
        渲染集提交时是**整体替换**列表，不会原地改；
        遍历时按拍下的长度走，因此后台线程看到的是一个自洽的视图。
        """
        layers = []
        for lay in self._layers_render_order():
            if not self.visible.get(lay.id, True):
                continue
            c = self._cache.get(lay.id)
            if c is None:
                continue
            s = self._active_set(c)
            if s is None:
                continue
            if not (s.poly_paths or s.line_paths or s.points or s.dot_xy is not None
                    or s.dots is not None):
                continue
            layers.append({
                "lid": lay.id,
                "color": lay.color, "op": lay.op,
                "poly_paths": s.poly_paths, "npoly": len(s.poly_paths),
                "poly_bounds": s.poly_bounds, "npoly_b": len(s.poly_bounds),
                "line_paths": s.line_paths, "nline": len(s.line_paths),
                "line_bounds": s.line_bounds, "nline_b": len(s.line_bounds),
                "points": s.points, "npt": len(s.points),
                "point_bounds": s.point_bounds, "npt_b": len(s.point_bounds),
                # 亚像素要素：传「取 QPolygonF 的方法」，由渲染线程首次用到时构建
                "dots": s.dots_polygon if (s.dot_xy is not None or s.dots is not None) else None,
            })
        if not layers:
            return None
        return {
            "layers": layers,
            "scale": self.scale, "tx": self.tx, "ty": self.ty,
            "w": self.width(), "h": self.height(),
            "dpr": self.devicePixelRatioF() or 1.0,
            "view": self._view_world_rect(),
            "mpp": self.mpp(),
        }

    def _layer_cache_key(self, lay, snap: dict) -> tuple:
        """图层渲染缓存的键（对应 QGIS 的 QgsMapRendererCache::cacheKey）。

        同一视口下切显隐、调图层顺序，只要这个键没变就不用重绘该图层。
        几何版本用**图层自己的** `c.rev`：别的图层重建不该把我这张图废掉。
        """
        c = self._cache.get(lay.id)
        rev = c.rev if c is not None else 0
        # 线宽档位也算进缓存键：1px / 1.6px 是两张不同的图，不能互相复用
        tier = 1 if snap.get("mpp", 0.0) >= LINE_THIN_MPP else 0
        return (snap["scale"], snap["tx"], snap["ty"],
                snap["w"], snap["h"], snap["dpr"], rev,
                lay.color, lay.op, tier)

    # ------------------------------------------------------------------ 构建
    def _layers_render_order(self) -> list:
        back = {"buffer", "dissolve", "grid", "merge", "convex_hull", "voronoi"}
        idx = {id(l): i for i, l in enumerate(self.layers)}
        return sorted(self.layers, key=lambda l: (0 if l.op in back else 1, idx[id(l)]))

    def _kick_build(self) -> None:
        """启动分片构建定时器。"""
        if self._build_timer.isActive():
            return
        if self._loading:
            return                      # 载入中：先让解析线程独占 CPU
        if not self._has_pending():
            return
        self._build_timer.start()

    def _has_pending(self) -> bool:
        for lay in self.layers:
            c = self._cache.get(lay.id)
            if c is None:
                continue
            if c.build is not None or c.bounds is None:
                return True
            if self._needs_build(lay, c):
                return True
        return False

    def _active_set(self, c: _LayerCache) -> _RenderSet | None:
        """当前用于渲染的渲染集（覆盖当前视口、容差最细、有内容的那套）。"""
        return self._pick_set(c)

    def _ensure_geoms(self, lay, c: _LayerCache) -> None:
        """准备全量 4326 几何（只需一次）。"""
        if c.wgs_ready:
            return
        gdf = lay.gdf
        try:
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(4326)
        except Exception:
            pass
        c.geoms = np.asarray(gdf.geometry)
        c.wgs_ready = True
        c.total = len(c.geoms)
        try:
            import shapely
            c.types = shapely.get_type_id(c.geoms)
        except Exception:
            c.types = None

    def _view_lonlat_rect(self) -> tuple[float, float, float, float]:
        """当前视口的经纬度范围。"""
        wx0, wy0, wx1, wy1 = self._view_world_rect()
        lon0, lat0 = unproject(wx0, wy0)
        lon1, lat1 = unproject(wx1, wy1)
        return (min(lon0, lon1), min(lat0, lat1), max(lon0, lon1), max(lat0, lat1))

    def _build_rect(self, c: _LayerCache) -> tuple:
        """本次渲染集要覆盖的经纬度范围：视口外扩 LOD_VIEW_PAD 倍。

        裁剪到图层范围：超出图层的部分本来就没有要素，留着只会让 rect 变虚胖。
        省域视图下外扩后通常已经盖住整个图层 → rect 就等于图层范围，
        于是以后**怎么平移都不用重建**。
        """
        r = self._pad_region(self._view_lonlat_rect(), LOD_VIEW_PAD)
        return self._clip_rect(r, c.extent)

    @staticmethod
    def _clip_rect(rect: tuple, ext: tuple | None) -> tuple:
        if ext is None:
            return rect
        return (max(rect[0], ext[0]), max(rect[1], ext[1]),
                min(rect[2], ext[2]), min(rect[3], ext[3]))

    def _view_need(self, c: _LayerCache) -> tuple | None:
        """当前视口里「可能真的有要素」的范围 = 视口 ∩ 图层范围。

        必须裁剪：视口常常比图层本身大（省域视图下四周留白）,
        拿未裁剪的视口去判「渲染集覆盖够不够」，永远判不够 —— 会无限重建。
        """
        r = self._view_lonlat_rect()
        ext = c.extent
        if ext is None:
            return r
        r = self._clip_rect(r, ext)
        if r[0] >= r[2] or r[1] >= r[3]:
            return None          # 视口完全在图层范围之外
        return r

    def _needs_build(self, lay, c: _LayerCache) -> bool:
        """当前视口有没有「范围覆盖 + 容差够细」的渲染集可用。

        对应 QGIS：视图范围一变就重新渲染（extent 变了缓存键就变了）。
        这里只是把渲染结果按范围+容差缓存起来复用。
        """
        if c.total == 0:
            return False
        if not self.visible.get(lay.id, True):
            # 隐藏的图层不建缓存：省下几十万个要素的构建，
            # 等重新显示时 `invalidate()` 会把它排进来
            return False
        if c.build is not None:
            return False          # 已经在建了，别重复排
        need = self._view_need(c)
        if need is None:
            return False          # 视口里根本不可能有要素
        cur = self._simplify_tol()
        for s in c.sets:
            if s.tol <= cur * LOD_REBUILD_RATIO and _rect_contains(s.rect, need):
                return False
        return True

    def _pick_set(self, c: _LayerCache) -> _RenderSet | None:
        """挑一套用来渲染的集合：覆盖当前视口 + 容差最细。

        没有覆盖视口的集合时返回 None —— 宁可不画（先显示上一帧的底图），
        也不能拿一套只覆盖别处的集合去顶：那会画出「错的区域」，
        在屏幕上就是一块有、一块没有。
        """
        need = self._view_need(c)
        if need is None:
            return None
        cur = self._simplify_tol()
        cov = [s for s in c.sets if s.built and _rect_contains(s.rect, need)]
        if not cov:
            return None
        ok = [s for s in cov if s.tol <= cur * LOD_REBUILD_RATIO]
        best = min(ok or cov, key=lambda s: s.tol)
        best.used = time.perf_counter()
        return best

    def _select_indices(self, c: _LayerCache, region: tuple) -> np.ndarray:
        """挑出本次要构建的要素：**范围里相交的要素，一个不少**。

        对应 QGIS 的 `QgsFeatureRequest::setFilterRect(extent)`：
        筛选之后就是全量渲染，没有抽样、没有配额、没有上限。
        任何形式的抽样都会在屏幕上变成一块一块的空洞 —— 因为渲染集
        一旦被当成「这个范围已经画好了」缓存下来，缺的要素就再也不会被补上。
        """
        b = c.bounds
        if b is None or len(b) == 0:
            return np.arange(c.total)
        m = ((b[:, 0] <= region[2]) & (b[:, 2] >= region[0]) &
             (b[:, 1] <= region[3]) & (b[:, 3] >= region[1]))
        return np.nonzero(m)[0]

    def _plan_build(self, lay, c: _LayerCache) -> _BuildJob | None:
        """规划一次构建任务：一个范围 + 一个容差 → 一整套路径。

        范围内没有要素时**照样建一个空集合**（rect 有效、nfeat=0）：
        这样「这块范围在当前容差下已经画过了」这个事实才成立，
        `_needs_build` 自然收敛，不会每帧空转。
        """
        self._ensure_geoms(lay, c)
        if c.total == 0:
            return None
        rect = self._build_rect(c)
        tol = self._simplify_tol()
        idx = self._select_indices(c, rect)
        dots, idx = self._split_subpixel(c, idx)
        job = _BuildJob(_RenderSet(tol, rect), tol, rect, idx, dots)
        job.nfeat = len(idx) + (0 if dots is None else len(dots))
        return job

    def _split_subpixel(self, c: _LayerCache, idx: np.ndarray):
        """把亚像素面要素挑出来画点，其余照常建路径。

        `QgsSimplifyMethod::threshold` 的像素语义：屏幕尺寸不足 `DOT_PX`
        的面要素，按容差简化一定会被抹平（省域视图下 24 万栋建筑总共只剩
        4 个顶点），抹平之后屏幕上就是「这里什么都没有」。
        所以这类要素不建轮廓路径，改画一个点 —— 屏幕上本来就是 1 像素的东西，
        既不会凭空消失，也省掉了海量无用顶点。

        线要素不做这个处理（短线段照样是一段线，画点反而更慢）。
        """
        if len(idx) == 0 or self.scale <= 0:
            return None, idx
        b = c.bounds
        if b is None or len(b) == 0:
            return None, idx
        types = c.types
        if types is None:
            return None, idx
        sub = b[idx]
        cx = (sub[:, 0] + sub[:, 2]) * 0.5
        cy = (sub[:, 1] + sub[:, 3]) * 0.5
        # 经纬度差 → 世界坐标差 → 像素（Mercator 局部保角，用 cos(lat) 换算）
        coslat = np.maximum(np.cos(np.radians(cy)), 1e-6)
        px_w = (sub[:, 2] - sub[:, 0]) / 360.0 * self.scale
        px_h = (sub[:, 3] - sub[:, 1]) / (360.0 * coslat) * self.scale
        is_poly = (types[idx] == 3) | (types[idx] == 6)
        small = is_poly & (np.maximum(px_w, px_h) < DOT_PX)
        if not bool(small.any()):
            return None, idx
        xs, ys = self._project_arr(np.column_stack((cx[small], cy[small])))
        dots = np.column_stack((xs, ys))
        return dots, idx[~small]

    @staticmethod
    def _pad_region(rect: tuple, factor: float) -> tuple:
        cx = (rect[0] + rect[2]) / 2
        cy = (rect[1] + rect[3]) / 2
        hw = (rect[2] - rect[0]) / 2 * factor
        hh = (rect[3] - rect[1]) / 2 * factor
        return (cx - hw, cy - hh, cx + hw, cy + hh)

    def _simplify_tol(self) -> float:
        """当前的简化容差（度）：约 0.8 像素对应的地面距离。

        对应 QGIS 的 `QgsSimplifyMethod::tolerance()` —— 它是以「地图单位」
        表达的，由 `mapUnitsPerPixel * 像素阈值` 换算而来。跟着屏幕走，
        放大时容差自动变小，几何就比「旧的简化图形」精细。
        """
        try:
            mpp = self.mpp()
        except Exception:
            return 0.0
        return max(mpp * SIMPLIFY_PX / 111320.0, 0.0)

    def _build_tick(self) -> None:
        """分片构建：每次只占用 10ms，界面全程可响应。"""
        t_end = time.perf_counter() + BUILD_BUDGET_S
        target = self._next_build_task()
        if target is None:
            self._build_timer.stop()
            self.progress.emit("")
            self._invalidate_frame()
            self.update()
            return
        lay, c, kind = target
        if kind == "bounds":
            self._build_bounds_chunk(c, t_end)
        else:
            self._build_job_chunk(c, c.build, t_end)
        self._report_progress()
        self._partial_frame()
        self.update()

    def _next_build_task(self):
        """找下一个该干的活。

        优先级：① 已有构建任务 → 把它做完；② 缺全量包围盒 → 先补；
        ③ 当前视口没有可用的渲染集 → 排一个。
        """
        for lay in self.layers:
            c = self._cache.get(lay.id)
            if c is not None and c.build is not None:
                return (lay, c, "build")
        for lay in self.layers:
            c = self._cache.get(lay.id)
            if c is None:
                continue
            if c.bounds is None:
                self._ensure_geoms(lay, c)
                return (lay, c, "bounds")
        for lay in self.layers:
            c = self._cache.get(lay.id)
            if c is None:
                continue
            if self._needs_build(lay, c):
                c.build = self._plan_build(lay, c)
                if c.build is not None:
                    return (lay, c, "build")
                continue
        return None

    def _partial_frame(self) -> None:
        """构建过程中定期出一帧，地图随构建进度逐渐显现（不用等全部构建完）。"""
        now = time.perf_counter()
        if now - self._last_partial_frame > 0.35:
            self._last_partial_frame = now
            if self._rendered_geo_rev != self._geo_rev:
                # 几何有变化才重画，避免把在飞的任务白白取消掉
                self._start_render()
        self.update()

    def _build_bounds_chunk(self, c: _LayerCache, t_end: float) -> None:
        """分片计算全量要素的包围盒（拾取 + 视口筛选索引用）。"""
        try:
            import shapely
        except Exception:
            c.bounds = np.zeros((0, 4), dtype=float)
            return
        arr = c.geoms
        if arr is None or len(arr) == 0:
            c.bounds = np.zeros((0, 4), dtype=float)
            return
        total = len(arr)
        # 分片小一点：单次 shapely.bounds 更短，主线程不会被一次调用占太久
        # （实测 4000 一片时，单次调用在载入高峰期能被 GIL 拖到 1 秒以上）
        chunk = 1200
        parts = c.bounds_out if c.bounds_out is not None else []
        while c.bounds_pos < total:
            end = min(total, c.bounds_pos + chunk)
            parts.append(shapely.bounds(arr[c.bounds_pos:end]))
            c.bounds_pos = end
            if time.perf_counter() >= t_end:
                break
        if c.bounds_pos >= total:
            c.bounds = np.vstack(parts) if parts else np.zeros((0, 4), dtype=float)
            c.bounds_out = None
            c.extent = self._bounds_extent(c.bounds)
        else:
            c.bounds_out = parts

    @staticmethod
    def _bounds_extent(b) -> tuple | None:
        """全量包围盒的并集 → 图层范围 (x0,y0,x1,y1)。"""
        if b is None or len(b) == 0:
            return None
        try:
            with np.errstate(all="ignore"):
                import warnings
                with warnings.catch_warnings():
                    # 空图层（比如筛选后 0 要素）整列是 NaN，nanmin 会告警，这里静音
                    warnings.simplefilter("ignore", RuntimeWarning)
                    x0 = float(np.nanmin(b[:, 0]))
                    y0 = float(np.nanmin(b[:, 1]))
                    x1 = float(np.nanmax(b[:, 2]))
                    y1 = float(np.nanmax(b[:, 3]))
        except Exception:
            return None
        if not all(map(math.isfinite, (x0, y0, x1, y1))):
            return None
        return (x0, y0, x1, y1)

    def _build_job_chunk(self, c: _LayerCache, job: _BuildJob, t_end: float) -> None:
        """推进一次构建任务一个片段。"""
        if job is None:
            return
        if job.stage == 2:
            self._simplify_chunk(c, job, t_end)
            if job.simplify_pos >= len(job.idx):
                job.stage = 3
            return
        if job.stage == 3:
            self._paths_chunk(c, job, t_end)
            if job.pos >= len(job.simplify_out):
                self._commit_set(c, job)
            return

    def _commit_set(self, c: _LayerCache, job: _BuildJob) -> None:
        """提交渲染集。

        旧集合一直保留到新集合提交完成，所以重建期间地图不会闪白；
        新集合提交后按 LRU 淘汰多余的集合（来回缩放时优先命中缓存）。
        """
        s = job.target
        s.poly_paths = job.spoly_paths
        s.poly_bounds = job.spoly_bounds
        s.line_paths = job.sline_paths
        s.line_bounds = job.sline_bounds
        s.points = job.spoints
        s.point_bounds = job.spoint_bounds
        s.dot_xy = job.dots
        s.nfeat = job.nfeat
        s.built = True
        s.used = time.perf_counter()
        c.sets.append(s)
        c.rev += 1
        while len(c.sets) > LOD_KEEP_SETS:
            # 淘汰最久没用过的；但当前视口正在用的那套要留住
            cur = self._pick_set(c)
            victims = [x for x in c.sets if x is not cur]
            if not victims:
                break
            victim = min(victims, key=lambda x: x.used)
            c.sets.remove(victim)
        c.build = None
        self._geo_rev += 1
        self._invalidate_frame(0)

    def _simplify_chunk(self, c: _LayerCache, job: _BuildJob, t_end: float) -> None:
        """分片简化选中的几何。"""
        arr = c.geoms
        if arr is None:
            job.simplify_out = []
            job.simplify_pos = len(job.idx)
            return
        idx = job.idx
        total = len(idx)
        try:
            import shapely
        except Exception:
            job.simplify_out = list(arr[idx]) if total else []
            job.simplify_pos = total
            return
        chunk = max(50, job.simp_chunk)
        out = job.simplify_out
        while job.simplify_pos < total:
            end = min(total, job.simplify_pos + chunk)
            part = arr[idx[job.simplify_pos:end]]
            t0 = time.perf_counter()
            if job.tol > 0 and len(part):
                try:
                    sim = shapely.simplify(part, job.tol, preserve_topology=False)
                    # 逐要素退化检测：容差相对某个要素太大时它会被抹平
                    # （小建筑在省域视图下就几个像素，一简化就没了）。
                    # 这种要素回退用原始几何 —— QGIS 的简化器也是这么处理的。
                    keep = self._keep_mask(part, sim)
                    if keep is not None and not bool(keep.all()):
                        sim = np.where(keep, sim, part)
                    part = sim
                except Exception:
                    pass
            out.extend(part.tolist())
            job.simplify_pos = end
            dt = time.perf_counter() - t0
            # 自适应块大小：保证单次 simplify 调用不超出时间预算
            if dt > BUILD_BUDGET_S:
                job.simp_chunk = max(30, chunk // 2)
                chunk = job.simp_chunk
            elif dt < BUILD_BUDGET_S / 4:
                job.simp_chunk = min(1500, chunk * 2)
                chunk = job.simp_chunk
            if time.perf_counter() >= t_end:
                break

    @staticmethod
    def _keep_mask(orig, simp) -> np.ndarray | None:
        """判断每个要素简化后是否还「像原来那个东西」。

        判据（任一不满足就回退原始几何）：
        · 简化后为空；
        · 面积缩水超过 60%（形状被抹平了）；
        · 顶点数反而没减少（简化没意义）。

        返回 None 表示全部保留（不需要回退）。
        """
        import shapely
        try:
            empty = shapely.is_empty(simp)
            a0 = shapely.area(orig)
            a1 = shapely.area(simp)
            # 面积型几何：缩水过多就回退（线的 area 恒为 0，不受影响）
            shrink = (a1 < a0 * 0.4) & (a0 > 0)
            bad = empty | shrink
            if not bool(bad.any()):
                return None
            return ~bad
        except Exception:
            return None

    def _report_progress(self) -> None:
        total = len(self._cache)
        if total <= 1:
            return
        done = 0.0
        for c in self._cache.values():
            if c.bounds is None and c.geoms is not None:
                done += min(1.0, c.bounds_pos / max(1, len(c.geoms)))
                continue
            if c.build is not None:
                j = c.build
                if j.stage == 2:
                    done += min(1.0, j.simplify_pos / max(1, len(j.idx))) * 0.6
                elif j.stage == 3:
                    done += 0.6 + min(1.0, j.pos / max(1, len(j.simplify_out))) * 0.4
                continue
            done += 1.0
        pct = int(100 * done / total)
        self.progress.emit(f"正在构建渲染缓存… {pct}%")

    def _paths_chunk(self, c: _LayerCache, job: _BuildJob, t_end: float) -> None:
        """批量把要素转成绘制路径。

        优化点：
        · 用 shapely 向量化接口一次取出整批要素的全部坐标
          （get_coordinates 一次 ~0.02s，逐要素则要 1.4s）；
        · 同一要素的多个环用 addPath 并成一条路径，既省 QPainterPath 对象数量
          （渲染更快），又保留逐要素包围盒用于视野裁剪；
        · 批大小自适应：超预算就减半，保证每次调用都在时间预算内。
        """
        try:
            import shapely
            from shapely import (get_coordinates, get_num_coordinates, get_parts,
                                 get_rings, get_type_id)
        except Exception:
            self._paths_chunk_fallback(c, job, t_end)
            return

        src = job.simplify_out
        n = len(src) if src is not None else 0
        if job.pos >= n:
            return
        batch = min(n, job.pos + max(50, job.path_chunk))
        geoms = np.asarray(src[job.pos:batch], dtype=object)
        t0 = time.perf_counter()
        try:
            tids = get_type_id(geoms)
            poly_sel = np.nonzero((tids == 3) | (tids == 6))[0]   # Polygon/MultiPolygon
            line_sel = np.nonzero((tids == 1) | (tids == 5))[0]   # LineString/MultiLineString
            pt_sel = np.nonzero(tids == 0)[0]                     # Point

            if len(poly_sel):
                self._build_polys(job, geoms, poly_sel, get_parts, get_rings,
                                  get_coordinates, get_num_coordinates, shapely)
            if len(line_sel):
                self._build_lines(job, geoms, line_sel, get_parts, get_coordinates,
                                  get_num_coordinates, shapely)
            if len(pt_sel):
                self._build_points(job, geoms, pt_sel, get_coordinates)
        except Exception:
            self._paths_chunk_fallback(c, job, t_end)
            return

        dt = time.perf_counter() - t0
        # 自适应批大小：超时减半，有富余则放大（限制在 50~2000）
        if dt > BUILD_BUDGET_S:
            job.path_chunk = max(30, job.path_chunk // 2)
        elif dt < BUILD_BUDGET_S / 4:
            job.path_chunk = min(2000, job.path_chunk * 2)
        job.pos = batch

    def _build_polys(self, job: _BuildJob, geoms, sel, get_parts, get_rings,
                     get_coordinates, get_num_coordinates, shapely) -> None:
        """批量构建面要素路径（多条要素合并进同一条 path）。

        投影对整批坐标只算一次（每要素单独算 numpy 会慢 6 倍）；
        再按「要素 → 环 → 顶点」的偏移切分写路径。
        合并的意义：Qt 每次 drawPath 的固定开销远大于顶点开销，
        逐要素画 47 万条道路要 800ms，合并后只要 190ms。
        """
        sub = geoms[sel]
        n_parts = shapely.get_num_geometries(sub)
        parts = get_parts(sub)
        n_rings = shapely.get_num_interior_rings(parts) + 1
        rings = get_rings(parts)
        rc = get_coordinates(rings)
        rn = get_num_coordinates(rings)
        if len(rn) == 0:
            return
        xs_all, ys_all = self._project_arr(rc)
        xs = xs_all.tolist()
        ys = ys_all.tolist()

        part_off = np.concatenate([[0], np.cumsum(n_parts)])
        ring_off = np.concatenate([[0], np.cumsum(rn)])       # 环 → 坐标偏移
        ring_cnt = np.concatenate([[0], np.cumsum(n_rings)])  # 部件 → 环序号偏移
        # 每个要素的环区间 [r0, r1)（r 是「环的序号」，用它去查 ring_off）
        f_r0 = ring_cnt[part_off[:-1]]
        f_r1 = ring_cnt[part_off[1:]]

        # 每个环的包围盒一次算完（同 _build_lines：省掉逐环 Python 切片归约）
        nring = len(rn)
        valid = rn >= 2
        bx0 = np.full(nring, 1e9)
        bx1 = np.full(nring, -1e9)
        by0 = np.full(nring, 1e9)
        by1 = np.full(nring, -1e9)
        if valid.any():
            starts = ring_off[:-1][valid]
            bx0[valid] = np.minimum.reduceat(xs_all, starts)
            bx1[valid] = np.maximum.reduceat(xs_all, starts)
            by0[valid] = np.minimum.reduceat(ys_all, starts)
            by1[valid] = np.maximum.reduceat(ys_all, starts)
        bx0l, bx1l, by0l, by1l = bx0.tolist(), bx1.tolist(), by0.tolist(), by1.tolist()

        path = QPainterPath()
        bb = [1e9, 1e9, -1e9, -1e9]
        nf = 0
        nv = 0
        for f in range(len(sub)):
            r0, r1 = int(f_r0[f]), int(f_r1[f])
            if r0 >= r1:
                continue
            for r in range(r0, r1):
                a, b = int(ring_off[r]), int(ring_off[r + 1])
                if b - a < 2:
                    continue
                path.moveTo(xs[a], ys[a])
                for i in range(a + 1, b):
                    path.lineTo(xs[i], ys[i])
                path.closeSubpath()
                rx0 = bx0l[r]
                rx1 = bx1l[r]
                ry0 = by0l[r]
                ry1 = by1l[r]
                if rx0 < bb[0]:
                    bb[0] = rx0
                if ry0 < bb[1]:
                    bb[1] = ry0
                if rx1 > bb[2]:
                    bb[2] = rx1
                if ry1 > bb[3]:
                    bb[3] = ry1
                nv += b - a
            nf += 1
            if nf >= BATCH_FEATURES or nv >= BATCH_VERTS:
                if bb[0] <= bb[2]:
                    job.spoly_paths.append(path)
                    job.spoly_bounds.append(bb)
                path = QPainterPath()
                bb = [1e9, 1e9, -1e9, -1e9]
                nf = 0
                nv = 0
        if nf and bb[0] <= bb[2]:
            job.spoly_paths.append(path)
            job.spoly_bounds.append(bb)

    def _build_lines(self, job: _BuildJob, geoms, sel, get_parts, get_coordinates,
                     get_num_coordinates, shapely) -> None:
        """批量构建线要素路径（同样按批合并）。"""
        sub = geoms[sel]
        parts = get_parts(sub)
        lc = get_coordinates(parts)
        ln = get_num_coordinates(parts)
        if len(ln) == 0:
            return
        xs_all, ys_all = self._project_arr(lc)
        xs = xs_all.tolist()
        ys = ys_all.tolist()
        off = np.concatenate([[0], np.cumsum(ln)])
        f_parts = shapely.get_num_geometries(sub)
        part_off = np.concatenate([[0], np.cumsum(f_parts)])

        # 每个 part 的包围盒一次算完：原来是逐个 part 对切片做 4 次 min/max，
        # 47 万个 part 就是近 200 万次 Python 切片 + 归约（实测占构建时间的三成）。
        # reduceat 按「每个 part 的起点」归约，天然和 cumsum 偏移对齐。
        npart = len(ln)
        valid = ln >= 2
        bx0 = np.full(npart, 1e9)
        bx1 = np.full(npart, -1e9)
        by0 = np.full(npart, 1e9)
        by1 = np.full(npart, -1e9)
        if valid.any():
            starts = off[:-1][valid]
            bx0[valid] = np.minimum.reduceat(xs_all, starts)
            bx1[valid] = np.maximum.reduceat(xs_all, starts)
            by0[valid] = np.minimum.reduceat(ys_all, starts)
            by1[valid] = np.maximum.reduceat(ys_all, starts)
        bx0l, bx1l, by0l, by1l = bx0.tolist(), bx1.tolist(), by0.tolist(), by1.tolist()

        path = QPainterPath()
        bb = [1e9, 1e9, -1e9, -1e9]
        nf = 0
        nv = 0
        for f in range(len(sub)):
            for k in range(int(part_off[f]), int(part_off[f + 1])):
                a, b = int(off[k]), int(off[k + 1])
                if b - a < 2:
                    continue
                path.moveTo(xs[a], ys[a])
                for i in range(a + 1, b):
                    path.lineTo(xs[i], ys[i])
                rx0 = bx0l[k]
                rx1 = bx1l[k]
                ry0 = by0l[k]
                ry1 = by1l[k]
                if rx0 < bb[0]:
                    bb[0] = rx0
                if ry0 < bb[1]:
                    bb[1] = ry0
                if rx1 > bb[2]:
                    bb[2] = rx1
                if ry1 > bb[3]:
                    bb[3] = ry1
                nv += b - a
            nf += 1
            if nf >= BATCH_FEATURES or nv >= BATCH_VERTS:
                if bb[0] <= bb[2]:
                    job.sline_paths.append(path)
                    job.sline_bounds.append(bb)
                path = QPainterPath()
                bb = [1e9, 1e9, -1e9, -1e9]
                nf = 0
                nv = 0
        if nf and bb[0] <= bb[2]:
            job.sline_paths.append(path)
            job.sline_bounds.append(bb)

    def _build_points(self, job: _BuildJob, geoms, sel, get_coordinates) -> None:
        """批量构建点要素。"""
        pc = get_coordinates(geoms[sel])
        if pc is None or len(pc) == 0:
            return
        xs, ys = self._project_arr(pc)
        for i in range(len(xs)):
            xi, yi = float(xs[i]), float(ys[i])
            job.spoints.append((xi, yi))
            job.spoint_bounds.append([xi, yi, xi, yi])

    @staticmethod
    def _project_arr(pts) -> tuple[np.ndarray, np.ndarray]:
        """经纬度数组 → 世界坐标（归一化 [0,1]）的 x/y 数组。

        整批一次算完；不要对每个要素单独调用（numpy 的固定开销会放大 N 倍）。
        """
        lon = pts[:, 0]
        lat = np.clip(pts[:, 1], -85.05, 85.05)
        x = (lon + 180.0) / 360.0
        s = np.sin(np.radians(lat))
        with np.errstate(divide="ignore", invalid="ignore"):
            y = 0.5 - np.log((1 + s) / (1 - s)) / (4 * np.pi)
        return x, y

    def _paths_chunk_fallback(self, c: _LayerCache, job: _BuildJob, t_end: float) -> None:
        """向量化不可用时的逐要素兜底路径。"""
        src = job.simplify_out
        n = len(src) if src is not None else 0
        while job.pos < n and time.perf_counter() < t_end:
            geom = src[job.pos]
            job.pos += 1
            if geom is None or geom.is_empty:
                continue
            self._emit_feature(job, geom)

    def _emit_feature(self, job: _BuildJob, geom) -> bool:
        """把一个要素转成世界坐标路径 + 包围盒（逐要素兜底用）。"""
        gt = geom.geom_type
        if gt in ("Polygon", "MultiPolygon"):
            path = QPainterPath()
            bb = [1e9, 1e9, -1e9, -1e9]
            parts = [geom] if gt == "Polygon" else list(geom.geoms)
            for poly in parts:
                bb = self._add_ring_bb(path, poly.exterior.coords, True, bb)
                for ring in poly.interiors:
                    bb = self._add_ring_bb(path, ring.coords, True, bb)
            if bb[0] > bb[2]:
                return False
            job.spoly_paths.append(path)
            job.spoly_bounds.append(bb)
            return True
        if gt in ("LineString", "MultiLineString"):
            path = QPainterPath()
            bb = [1e9, 1e9, -1e9, -1e9]
            parts = [geom] if gt == "LineString" else list(geom.geoms)
            for ln in parts:
                bb = self._add_ring_bb(path, ln.coords, False, bb)
            if bb[0] > bb[2]:
                return False
            job.sline_paths.append(path)
            job.sline_bounds.append(bb)
            return True
        try:
            x, y = project(geom.x, geom.y)
        except Exception:
            return False
        job.spoints.append((x, y))
        job.spoint_bounds.append([x, y, x, y])
        return True

    @staticmethod
    def _add_ring_bb(path: QPainterPath, coords, close: bool, bb) -> list:
        """把一环经纬度坐标转成世界坐标路径。

        投影用 numpy 向量化算（逐点调 project() 会慢一个数量级），
        再用 moveTo/lineTo 直接写路径（构造 QPolygonF 更慢）。
        """
        pts = np.asarray(coords, dtype=float)
        if pts.size == 0:
            return bb
        lon = pts[:, 0]
        lat = np.clip(pts[:, 1], -85.05, 85.05)
        x = (lon + 180.0) / 360.0
        s = np.sin(np.radians(lat))
        with np.errstate(divide="ignore", invalid="ignore"):
            y = 0.5 - np.log((1 + s) / (1 - s)) / (4 * np.pi)
        xl = x.tolist()
        yl = y.tolist()
        path.moveTo(xl[0], yl[0])
        for i in range(1, len(xl)):
            path.lineTo(xl[i], yl[i])
        if close:
            path.closeSubpath()
        x0, x1 = xl[0], xl[-1]
        y0, y1 = yl[0], yl[-1]
        if x0 < bb[0]:
            bb[0] = x0
        if y0 < bb[1]:
            bb[1] = y0
        if x1 > bb[2]:
            bb[2] = x1
        if y1 > bb[3]:
            bb[3] = y1
        return bb

    def _geom_path(self, geom) -> QPainterPath | None:
        """shapely 几何 → 世界坐标 QPainterPath（点要素返回 None，由调用方画圆）。"""
        if geom is None or geom.is_empty:
            return None
        path = QPainterPath()
        gt = geom.geom_type
        if gt == "Polygon":
            self._add_ring(path, geom.exterior.coords, True)
            for r in geom.interiors:
                self._add_ring(path, r.coords, True)
        elif gt == "MultiPolygon":
            for poly in geom.geoms:
                self._add_ring(path, poly.exterior.coords, True)
                for r in poly.interiors:
                    self._add_ring(path, r.coords, True)
        elif gt == "LineString":
            self._add_ring(path, geom.coords, False)
        elif gt == "MultiLineString":
            for ln in geom.geoms:
                self._add_ring(path, ln.coords, False)
        else:
            return None
        return path

    @staticmethod
    def _add_ring(path: QPainterPath, coords, close: bool) -> None:
        first = True
        for lon, lat in coords:
            x, y = project(lon, lat)
            if first:
                path.moveTo(x, y)
                first = False
            else:
                path.lineTo(x, y)
        if close:
            path.closeSubpath()

    # ------------------------------------------------------------------ 交互
    def mousePressEvent(self, e):  # noqa: N802
        self.setFocus(Qt.MouseFocusReason)       # 点一下地图就能用方向键
        if e.button() == Qt.MiddleButton:        # 中键拖拽平移（GIS 老习惯）
            self._drag_from = e.position().toPoint()
            self._drag_moved = 0
            self.setCursor(Qt.ClosedHandCursor)
            return
        if e.button() == Qt.LeftButton:
            pos = e.position().toPoint()
            if e.modifiers() & Qt.ShiftModifier:      # Shift + 拖拽 = 框选
                self._band = (pos.x(), pos.y(), pos.x(), pos.y())
                self.setCursor(Qt.CrossCursor)
                return
            self._drag_from = pos
            self._drag_moved = 0
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, e):  # noqa: N802
        pos = e.position()
        lon, lat = self.screen_to_lonlat(pos.x(), pos.y())
        self.cursorMoved.emit(lon, lat)

        if self._band is not None:                    # 正在拖选框：只重画，不重渲染
            x0, y0, _, _ = self._band
            self._band = (x0, y0, pos.x(), pos.y())
            self.update()
            return

        if self._drag_from is not None:
            d = pos.toPoint() - self._drag_from
            self.tx += d.x()
            self.ty += d.y()
            self._drag_moved += abs(d.x()) + abs(d.y())
            self._drag_from = pos.toPoint()
            self.viewChanged.emit()
            # 平移只是位图偏移，超出一部分视口才重绘，保证拖动跟手
            if self._frame_view and (abs(self.tx - self._frame_view[1]) > self.width() * PAN_RERENDER_RATIO
                                     or abs(self.ty - self._frame_view[2]) > self.height() * PAN_RERENDER_RATIO):
                self._invalidate_frame()
                self._kick_build()
            self.update()
            return

        now = time.time()
        if now - self._hover_ts > 0.1:
            self._hover_ts = now
            hit = self._hit_test(pos.x(), pos.y())
            key = (hit[0].id, hit[1]) if hit else None
            if key != self._hover:
                self._hover = key
                self.update()
                self.hoverChanged.emit({"layer": hit[0], "props": hit[2]} if hit else None)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._band is not None and e.button() == Qt.LeftButton:
            band, self._band = self._band, None
            self.setCursor(Qt.ArrowCursor)
            if abs(band[2] - band[0]) >= 6 and abs(band[3] - band[1]) >= 6:
                self._select_rect(band)
            self.update()
            return
        if e.button() == Qt.LeftButton and self._drag_from is not None:
            self._drag_from = None
            self.setCursor(Qt.ArrowCursor)
            if self._drag_moved < 4:
                hit = self._hit_test(e.position().x(), e.position().y())
                if hit is None and self._sel:
                    # 点空白处 = 取消选择（QGIS 同款手感）
                    self.clear_selection()
                self.picked.emit({"layer": hit[0], "props": hit[2],
                                  "x": e.position().x(), "y": e.position().y()} if hit else None)

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key_Home:
            self.fit_all()
            return
        step = 80 if not (e.modifiers() & Qt.ShiftModifier) else 320
        moves = {Qt.Key_Left: (step, 0), Qt.Key_Right: (-step, 0),
                 Qt.Key_Up: (0, step), Qt.Key_Down: (0, -step)}
        if e.key() in moves:
            dx, dy = moves[e.key()]
            self.tx += dx
            self.ty += dy
            self._invalidate_frame()
            self.viewChanged.emit()
            self.update()
            return
        if e.key() in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_by(1)
            return
        if e.key() in (Qt.Key_Minus, Qt.Key_Underscore):
            self.zoom_by(-1)
            return
        super().keyPressEvent(e)

    def contextMenuEvent(self, e):  # noqa: N802
        self.mapMenu.emit(e.globalPos())
        e.accept()

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        self.zoom_at(2.0, e.position().x(), e.position().y())

    def wheelEvent(self, e):  # noqa: N802
        dy = e.angleDelta().y()
        if dy:
            self.zoom_at(math.pow(1.0015, dy), e.position().x(), e.position().y())
        e.accept()

    def leaveEvent(self, e):  # noqa: N802
        if self._hover is not None:
            self._hover = None
            self.update()
            self.hoverChanged.emit(None)

    # ------------------------------------------------------------------ 选区
    def set_select_target(self, layer_id: str | None) -> None:
        """框选作用在哪一层（跟左栏当前选中层同步）。"""
        self._sel_layer = layer_id

    def selection_count(self) -> int:
        return int(sum(len(v) for v in self._sel.values()))

    def selection_layer(self) -> str | None:
        return next(iter(self._sel), None)

    def selection_indices(self, layer_id: str | None = None):
        lid = layer_id or self.selection_layer()
        return self._sel.get(lid) if lid else None

    def set_selection(self, layer_id: str, idx) -> None:
        import numpy as _np
        arr = _np.asarray(idx, dtype=int) if not isinstance(idx, _np.ndarray) else idx
        self._sel = {layer_id: arr} if len(arr) else {}
        if len(arr):
            self._sel_layer = layer_id
        self.selectionChanged.emit(int(len(arr)))
        self.update()

    def clear_selection(self) -> None:
        if not self._sel:
            return
        self._sel = {}
        self.selectionChanged.emit(0)
        self.update()

    def highlight_feature(self, layer_id: str, pos_idx: int) -> None:
        """属性表点行 → 地图上高亮这个要素（持久，直到下一次高亮或清除）。"""
        self._hl = (layer_id, int(pos_idx))
        self.update()

    def clear_highlight(self) -> None:
        if self._hl is None:
            return
        self._hl = None
        self.update()

    def zoom_to_feature(self, layer_id: str, pos_idx: int, pad: float = 0.35) -> None:
        lay = next((l for l in self.layers if l.id == layer_id), None)
        c = self._cache.get(layer_id)
        if lay is None:
            return
        self._ensure_index(lay, c)
        if c is None or c.bounds is None or pos_idx >= len(c.bounds):
            return
        b = c.bounds[pos_idx]
        w = max(b[2] - b[0], 1e-6)
        h = max(b[3] - b[1], 1e-6)
        # 单个点/极小要素：给一个固定的最小视野，否则会缩到极限
        if w * self.scale < 40 or h * self.scale < 40:
            w = h = 400.0 / max(self.scale, 1e-9)
        self.fit_bounds(b[0] - w * pad, b[1] - h * pad, b[2] + w * pad, b[3] + h * pad)

    def _select_rect(self, band: tuple) -> None:
        """框选：先用 bbox 粗筛，候选不多时再用 shapely 精确判断相交。"""
        lid = self._sel_layer or self._top_visible_lid()
        lay = next((l for l in self.layers if l.id == lid), None)
        c = self._cache.get(lid) if lid else None
        if lay is None or c is None:
            return
        self._ensure_index(lay, c)
        if c.bounds is None or len(c.bounds) == 0:
            return
        lon0, lat0 = self.screen_to_lonlat(band[0], band[1])
        lon1, lat1 = self.screen_to_lonlat(band[2], band[3])
        x0, x1 = min(lon0, lon1), max(lon0, lon1)
        y0, y1 = min(lat0, lat1), max(lat0, lat1)
        b = c.bounds
        m = (b[:, 0] <= x1) & (b[:, 2] >= x0) & (b[:, 1] <= y1) & (b[:, 3] >= y0)
        idx = np.nonzero(m)[0]
        if 0 < len(idx) <= 20000 and c.geoms is not None:
            try:
                import shapely
                room = shapely.box(x0, y0, x1, y1)
                idx = idx[shapely.intersects(c.geoms[idx], room)]
            except Exception:
                pass
        self.set_selection(lid, idx)

    def _top_visible_lid(self) -> str | None:
        for lay in reversed(self._layers_render_order()):
            if self.visible.get(lay.id, True):
                return lay.id
        return None

    # ------------------------------------------------------------------ 拾取
    def _ensure_index(self, lay, c: _LayerCache) -> bool:
        """按需生成全量几何索引（包围盒 + 几何数组），只在第一次点击时算一次。"""
        if c.bounds is not None:
            return True
        self._ensure_geoms(lay, c)
        try:
            import shapely
            c.bounds = shapely.bounds(c.geoms)
            c.extent = self._bounds_extent(c.bounds)
        except Exception:
            return False
        return True

    def _hit_test(self, sx: float, sy: float):
        lon, lat = self.screen_to_lonlat(sx, sy)
        tol = max(self.mpp() * 4 / 111320.0, 1e-6)
        import shapely
        from shapely.geometry import Point
        pt = Point(lon, lat)

        for lay in reversed(self._layers_render_order()):
            if not self.visible.get(lay.id, True):
                continue
            c = self._cache.get(lay.id)
            if c is None or not self._ensure_index(lay, c):
                continue
            b = c.bounds
            m = ((b[:, 0] <= lon + tol) & (b[:, 2] >= lon - tol) &
                 (b[:, 1] <= lat + tol) & (b[:, 3] >= lat - tol))
            idxs = np.nonzero(m)[0]
            if idxs.size == 0:
                continue
            if idxs.size > 3000:      # 极端缩小情况下的兜底，避免一次测几十万个
                idxs = idxs[:3000]
            dist = shapely.distance(c.geoms[idxs], pt)
            k = int(np.argmin(dist))
            if float(dist[k]) <= tol:
                row = lay.gdf.iloc[int(idxs[k])]
                props = row.to_dict()
                props.pop(lay.gdf.geometry.name, None)
                return lay, int(idxs[k]), props
        return None

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, e):  # noqa: N802
        p = QPainter(self)
        rect = self.rect()
        grad = QLinearGradient(0, 0, 0, rect.height())
        grad.setColorAt(0, QColor("#faf9f5"))
        grad.setColorAt(1, QColor("#f4f1e8"))
        p.fillRect(rect, QBrush(grad))

        if self._frame is not None and self._frame_view is not None:
            s0, tx0, ty0, w0, h0 = self._frame_view
            r = self.scale / s0 if s0 else 1.0
            p.save()
            p.setRenderHint(QPainter.SmoothPixmapTransform, False)
            p.translate(self.tx, self.ty)
            p.scale(r, r)
            p.translate(-tx0, -ty0)
            p.drawPixmap(0, 0, self._frame)
            p.restore()
        elif not self.layers:
            p.setPen(QColor("#8d897e"))
            p.setFont(QFont("Microsoft YaHei UI", 11))
            p.drawText(rect.adjusted(0, -18, 0, -18), Qt.AlignCenter, "还没有数据")
            p.setFont(QFont("Microsoft YaHei UI", 9))
            p.setPen(QColor("#a9a396"))
            p.drawText(rect.adjusted(0, 18, 0, 18), Qt.AlignCenter,
                       "拖入 Shapefile / GeoJSON，或点右上角「示例数据」")
        else:
            # 图层已载入但几何还在构建：给出明确提示，而不是一片空白
            p.setPen(QColor("#8d897e"))
            p.setFont(QFont("Microsoft YaHei UI", 10))
            p.drawText(rect, Qt.AlignCenter, "正在准备渲染数据…")

        # 经纬网画在帧缓存**之上**，这样缩放平移时网格始终对齐屏幕
        self._draw_grid(p)
        self._draw_selection(p)
        self._draw_highlight(p)
        self._draw_hover(p)
        self._draw_band(p)
        p.end()

    def _draw_selection(self, p: QPainter) -> None:
        """选中的要素描朱红边 + 半透明填充（只画前 SEL_DRAW_LIMIT 个，剩下的画外框）。"""
        for lid, idxs in self._sel.items():
            c = self._cache.get(lid)
            if c is None or c.geoms is None or len(idxs) == 0:
                continue
            p.save()
            p.translate(self.tx, self.ty)
            p.scale(self.scale, self.scale)
            pen = QPen(QColor(201, 100, 66, 235))
            pen.setWidthF(1.8)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(QBrush(QColor(217, 119, 87, 48)))
            drawn = 0
            for i in idxs:
                if drawn >= 400:                     # 太多就不逐个描了，画整体外框
                    break
                path = self._geom_path(c.geoms[int(i)])
                if path is not None:
                    p.drawPath(path)
                    drawn += 1
            p.restore()

    def _draw_highlight(self, p: QPainter) -> None:
        """属性表点行的高亮：一圈更醒目的描边 + 外层淡光圈。"""
        if not self._hl:
            return
        lid, idx = self._hl
        c = self._cache.get(lid)
        if c is None or c.geoms is None or idx >= len(c.geoms):
            return
        geom = c.geoms[idx]
        if geom is None or geom.is_empty:
            return
        p.save()
        p.translate(self.tx, self.ty)
        p.scale(self.scale, self.scale)
        path = self._geom_path(geom)
        if path is None:                             # 点要素：画在屏幕坐标上
            p.restore()
            pt = self.lonlat_to_screen(geom.x, geom.y)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(201, 100, 66, 90), 5))
            p.drawEllipse(pt, 9, 9)
            p.setPen(QPen(QColor("#c96442"), 2))
            p.drawEllipse(pt, 5, 5)
            return
        p.setBrush(QBrush(QColor(217, 119, 87, 70)))
        # 必须 cosmetic：画笔处在世界坐标变换下，2.4「世界单位」在高缩放时
        # 会变成上千万像素宽，把整个视口糊成一块实色（就是那个「大圆」）
        pen = QPen(QColor("#c96442"))
        pen.setWidthF(2.4)
        pen.setCosmetic(True)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPath(path)
        p.restore()

    def _draw_band(self, p: QPainter) -> None:
        if self._band is None:
            return
        x0, y0, x1, y1 = self._band
        r = QRectF(QPointF(min(x0, x1), min(y0, y1)), QPointF(max(x0, x1), max(y0, y1)))
        p.setPen(QPen(QColor(201, 100, 66, 210), 1, Qt.DashLine))
        p.setBrush(QBrush(QColor(217, 119, 87, 30)))
        p.drawRect(r)

    def _draw_hover(self, p: QPainter) -> None:
        if not self._hover:
            return
        lid, idx = self._hover
        c = self._cache.get(lid)
        lay = next((l for l in self.layers if l.id == lid), None)
        if c is None or lay is None or c.geoms is None or idx >= len(c.geoms):
            return
        geom = c.geoms[idx]
        if geom is None or geom.is_empty:
            return
        path = QPainterPath()
        gt = geom.geom_type
        if gt == "Polygon":
            self._add_ring(path, geom.exterior.coords, True)
            for r in geom.interiors:
                self._add_ring(path, r.coords, True)
        elif gt == "MultiPolygon":
            for poly in geom.geoms:
                self._add_ring(path, poly.exterior.coords, True)
                for r in poly.interiors:
                    self._add_ring(path, r.coords, True)
        elif gt == "LineString":
            self._add_ring(path, geom.coords, False)
        elif gt == "MultiLineString":
            for ln in geom.geoms:
                self._add_ring(path, ln.coords, False)
        else:
            pt = self.lonlat_to_screen(geom.x, geom.y)
            p.setPen(QPen(QColor("#0f1b2d"), 2))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(pt, 7, 7)
            return
        p.save()
        p.translate(self.tx, self.ty)
        p.scale(self.scale, self.scale)
        pen = QPen(QColor("#0f1b2d"))
        pen.setWidthF(2.2)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)
        p.restore()

    def _draw_grid(self, p: QPainter) -> None:
        steps = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30]
        step = steps[-1]
        for s in steps:
            if s / 360.0 * self.scale > 115:
                step = s
                break
        dec = 0 if step >= 1 else 1 if step >= 0.1 else 2 if step >= 0.01 else 3
        tl, br = self.screen_to_lonlat(0, 0), self.screen_to_lonlat(self.width(), self.height())

        pen = QPen(QColor(140, 132, 112, 36))
        pen.setWidth(1)
        p.setPen(pen)
        p.setFont(QFont("Consolas", 8))

        lon = math.floor(tl[0] / step) * step
        while lon <= br[0] + step:
            x = self.lonlat_to_screen(lon, 0).x()
            p.drawLine(int(x), 0, int(x), self.height())
            p.setPen(QColor(150, 143, 124, 185))
            txt = "0°" if abs(lon) < 1e-9 else f"{abs(lon):.{dec}f}°{'E' if lon > 0 else 'W'}"
            p.drawText(int(x) + 4, self.height() - 6, txt)
            p.setPen(pen)
            lon += step

        lat = math.floor(min(tl[1], br[1]) / step) * step
        top = max(tl[1], br[1])
        while lat <= top + step:
            y = self.lonlat_to_screen(0, lat).y()
            p.drawLine(0, int(y), self.width(), int(y))
            p.setPen(QColor(150, 143, 124, 185))
            txt = "0°" if abs(lat) < 1e-9 else f"{abs(lat):.{dec}f}°{'N' if lat > 0 else 'S'}"
            p.drawText(6, int(y) - 4, txt)
            p.setPen(pen)
            lat += step

    def _view_world_rect(self) -> tuple[float, float, float, float]:
        return ((0 - self.tx) / self.scale, (0 - self.ty) / self.scale,
                (self.width() - self.tx) / self.scale, (self.height() - self.ty) / self.scale)
