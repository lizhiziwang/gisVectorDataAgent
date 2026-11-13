"""纯 Qt 地图控件：QPainter 自绘矢量图层（Web Mercator），无任何 Web 引擎。

渲染管线逐条对照 QGIS 源码实现（qgsmaprendererjob.cpp /
qgsmaprendererparalleljob.cpp / qgsvectorlayerrenderer.cpp / qgssimplifymethod.h）：

1. **每个图层一个渲染任务（LayerRenderJob）**
   `_LayerJob` 各自画到**自己的一张 QImage** 上，常驻合成线程 `_ComposeWorker`
   按渲染顺序叠成整帧 —— 同 `QgsMapRendererParallelJob::prepareJobs()` + `composeImage()`；
   UI 线程只接整帧，不再逐层 drawImage。
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
9. **屏幕范围流式渲染**
   源文件图层无论大小都走 provider；内存图层超过 `STREAM_MIN_FEATURES`
   才走流式。两者都不预建整层路径，改为
   `QgsVectorLayerRenderer::render()` 的做法：按 `setFilterRect(extent)`
   取屏幕内要素，分块简化/建路径，立即画进图层 QImage，路径用完即丢；
   每块画完按时间节流发 `layerProgress`，主线程先合成 provisional 帧显示，
   整帧完成后再由 final 帧替换。小内存图层仍走渲染集缓存（平移/缩放复用更快）。
   可用 `GIS_AGENT_STREAM_VECTOR=1/0` 强制开/关。
"""
from __future__ import annotations

import math
import os
import threading
import time

import numpy as np

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF)
from PySide6.QtWidgets import QApplication, QWidget

from app.layer_style import SOFT_OPS, resolve_vector_style, style_signature

from .labels import (LABEL_CAP, LABEL_TABLE_MAX, _grid_pick, build_label_data,
                     build_label_table)
from .proj import project, unproject
from .rasterview import RasterPipeline, RasterViewPort

# --- 渲染参数（对照 QGIS 的默认值）-----------------------------------------
BUILD_BUDGET_S = 0.010       # 每个事件循环片段的几何构建预算
RENDER_DEBOUNCE_MS = 160     # 手势停下多久后重绘帧缓存（矢量帧构建较贵）
RASTER_RENDER_DEBOUNCE_MS = 60  # 纯栅格地图：源块命中快，少等一点更跟手
PAN_RERENDER_RATIO = 0.3     # 平移超过视口的这个比例就重绘，避免露出空白边
#: 但比例阈值在大窗口下太大了：900 px 宽的视口要拖 270 px 才重绘，
#: 小幅拖拽永远等不到，右/下就留下一圈空边。所以再加一个绝对上限：
#: 「露出的空白边超过这么多像素就重绘」，两者取小。
PAN_RERENDER_PX = 72

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

# --- 流式渲染（大图层）-----------------------------------------------------
# 小图层保留「渲染集」缓存：预建路径换来平移/缩放时的复用，更快。
# 大图层预建整层路径的内存代价通常高于收益，改为像 QGIS 的
# `QgsVectorLayerRenderer` 一样：按视口筛要素 → 分块简化/建路径 → 立即画，
# 路径用完即丢。可用 GIS_AGENT_STREAM_VECTOR=1/0 强制开/关。
STREAM_MIN_FEATURES = 250_000
# 流式图层中间帧的最小间隔：再密会拖慢合成，再疏就看不出「一块块出现」。
# 小窗口用更密的间隔，否则 30k 要素的图层只来得及出 1~2 帧，看起来仍像整层跳变；
# 大/高 DPI 窗口合成成本高，保持 0.12 s 一帧。
STREAM_PROGRESS_INTERVAL = 0.12
STREAM_PROGRESS_INTERVAL_SMALL = 0.06
STREAM_PROGRESS_SMALL_PIXELS = 1_500_000

# 点图层在当前视口里超过这个数量时，用 1px 的 drawPoints 代替逐点 drawEllipse。
# 500 万点逐点画 3.4px 圆会到几十秒；此时每点本来就不足一个像素，
# 1px 点才是正确的制图表达（和 QGIS 在小比例尺下的 marker 密度观感一致）。
POINT_DOT_THRESHOLD = 80_000

# --- 源文件图层的整层粗路径缓存（宽视野 LOD）-------------------------------
# shapefile / GPKG 没有空间索引：视口一旦盖住整个图层（省域、跨市），
# `iter_bbox()` 每次都要把文件从头读一遍。实测 47 万条道路的整层读取
# 3.2~3.6s，占整帧 6s 的六成；拖动/缩放时这类整帧要反复重来，就是
# 「卡住 5~8 秒」的来源。
# 这里在后台用固定粗容差把整层读**一次**建成一套路径（约 30MB / 47 万条），
# 之后 mpp >= LOD_MIN_MPP 的宽视野直接画路径缓存，不再碰磁盘；
# 更近的视野（城市级）本来就只有几万个要素，继续走按 bbox 读窗口的流式路径。
LOD_MIN_MPP = 45.0           # 每像素地面米数达到这个值就切到整层缓存（约 1:17 万）
LOD_TOL = LOD_MIN_MPP * SIMPLIFY_PX / 111320.0   # 建缓存用的经纬度容差（度）
LOD_MIN_FEATURES = 40_000    # 少于这么多要素的源文件层不值得建缓存（读一次本来也快）
LOD_READ_BATCH = 8192        # 建缓存时每次读多少要素
LOD_MAX_VERTS = 4_000_000    # 缓存顶点上限（QPainterPath 每顶点约 24 字节）：超了就放弃

# 并行图层 job（QgsMapRendererParallelJob）。单图层 5M 没有收益，但多图层
# 时每层各画各的 QImage；`GIS_AGENT_PARALLEL_LAYERS=0` 可退回串行对照。
PARALLEL_LAYERS = os.environ.get("GIS_AGENT_PARALLEL_LAYERS", "1").strip().lower() not in (
    "0", "false", "no", "off")
PARALLEL_MAX_WORKERS = max(1, min(4, os.cpu_count() or 1))


def _stream_enabled(lay) -> bool:
    """这个图层是否走屏幕范围内的流式渲染。

    源文件图层（provider）一律按 bbox 流式读——只有这条路能避免首次
    打开时把整份数据读进内存；内存图层仍按要素数决定。
    """
    env = os.environ.get("GIS_AGENT_STREAM_VECTOR", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        p = lay.provider
        if p is not None and (getattr(lay, "source_path", None)
                              or getattr(lay, "cache_path", None)
                              or getattr(p, "live", False)):
            return True
    except Exception:
        pass
    try:
        count = int(lay.meta().get("count") or 0)
    except Exception:
        count = 0
    return count >= STREAM_MIN_FEATURES


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
    raw = lay.get("style") or {}
    st = resolve_vector_style(op, lay.get("color") or "#2f6fed", raw,
                              lay.get("geom") or "")
    soft = op in SOFT_OPS
    fill_a = st["fill_opacity"] / 100.0
    stroke_a = st["stroke_opacity"] / 100.0
    # 亚像素要素的透明度不跟填充透明度联动：缩远后整片要素突然变淡会像「丢了数据」
    dot_a = 0.55 if soft else 0.85
    color = st["fill_color"]
    stroke_color = st["stroke_color"]

    poly_paths = lay["poly_paths"]
    line_paths = lay["line_paths"]
    points = lay["points"]
    dots = lay["dots"]

    if poly_paths:
        p.setBrush(QBrush(hex_alpha(color, fill_a)))
        pen = QPen(hex_alpha(stroke_color, stroke_a))
        pen.setWidthF(float(st["stroke_width"]))
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
        # 线图层默认不透明（原来就是全色）；只有用户显式调过描边透明度才跟着变
        alpha = stroke_a if "stroke_opacity" in raw else 1.0
        pen = QPen(hex_alpha(stroke_color, alpha))
        # 缩得远时用 1px：既正确又走 Qt 的整数宽度快速路径（见 LINE_THIN_MPP 注释）
        width = (float(st["stroke_width"]) if "stroke_width" in raw
                 else (1.0 if mpp >= LINE_THIN_MPP else LINE_WIDE_PX))
        pen.setWidthF(width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setCosmetic(True)
        p.setPen(pen)
        for i in _visible_indices(lay["line_bounds"], view, lay["nline_b"]):
            if i < lay["nline"]:
                p.drawPath(line_paths[i])

    if points:
        if lay.get("point_dots"):
            # 高密度点：一个 drawPoints 批次画完，避免 500 万次 drawEllipse。
            # 这个比例尺下每个点本来就不足一个像素，1px 点是正确的表达。
            poly = QPolygonF([QPointF(float(x), float(y)) for x, y in points])
            if not poly.isEmpty():
                pen = QPen(hex_alpha(color, dot_a))
                pen.setWidth(0)
                pen.setCosmetic(True)
                p.setPen(pen)
                p.setBrush(Qt.NoBrush)
                p.drawPoints(poly)
        else:
            pen = QPen(QColor("#ffffff"))
            pen.setWidthF(1.3)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(QBrush(QColor(st["point_color"])))
            # 点是固定像素大小 → 世界坐标下半径要除以缩放比
            r = float(st["point_size"]) * 0.5 / max(scale, 1e-9)
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
      （图层图像缓存的键用它，跟别的图层互不影响）；
    · `stream`：大图层标记。为真时不建 `sets`，每次按屏幕范围分块现画；
      内存图层的 `geoms` / `bounds` 仍保留，供拾取、选区和范围筛选。
    · `provider`：源文件图层。此时不建 `geoms` / `bounds`，每次渲染只让
      provider 按 bbox 读窗口，内存里没有整层要素。
    """

    __slots__ = ("geoms", "types", "bounds", "extent", "wgs_ready", "total", "sets",
                 "build", "bounds_out", "bounds_pos", "rev", "stream", "provider",
                 "lod_state", "bounds_pending", "bounds_failed")

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
        self.stream = False      # 大图层：不建渲染集，屏幕范围内分块流式画
        self.provider = None     # 源文件 provider（有则优先按 bbox 读）
        # 源文件图层的「整层粗路径缓存」状态："" / "building" / "ready"
        self.lod_state = ""
        #: provider 的范围还在后台算：本帧先跳过，避免拿未知 extent 去扫全层
        self.bounds_pending = False
        #: 后台也算不出范围：不再重试，也不让这层退化成整层扫描
        self.bounds_failed = False


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


class _ComposeJob:
    """一次离屏合成请求（图层图 + 视口 + 显隐 + 顺序）。"""

    __slots__ = ("epoch", "sig", "view", "dpr", "w", "h",
                 "items", "mode")

    def __init__(self, epoch, sig, view, dpr, w, h, items,
                 mode: str):
        self.epoch = epoch
        self.sig = sig
        self.view = view
        self.dpr = dpr
        self.w = w
        self.h = h
        #: 自下而上的图层图：[(是否栅格, QImage, own_view)]
        #: 栅格和矢量在同一个列表里，顺序完全由图层列表决定（栅格不再强制垫底）
        self.items = items
        self.mode = mode                   # force / auto / first


def _compose_job_image(job: _ComposeJob) -> QImage | None:
    """在渲染线程里把已完成的图层图叠成一帧（不碰 QPixmap / 控件）。"""
    img = QImage(max(1, job.w), max(1, job.h), QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(job.dpr)
    img.fill(Qt.transparent)
    p = QPainter(img)
    try:
        for is_raster, limg, own in job.items:
            if limg is None:
                continue
            try:
                MapView._blit_view_image(p, limg, own, job.view, job.dpr,
                                         device_size=bool(is_raster))
            except Exception:  # noqa: BLE001
                pass
    finally:
        p.end()
    return img


class _ComposeWorker(QThread):
    """常驻合成线程：只负责把图层 QImage 叠成整帧，UI 线程只做一次贴图。

    合成往往要处理 2~3 张 3000×1800 的 ARGB 图；以前在 UI 线程里做，
    拖拽大栅格时会和事件循环抢时间。这里用 QImage（可跨线程绘制）离屏
    合成，完成后把整帧交回主线程。
    """

    ready = Signal(object, object)     # _ComposeJob, QImage

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._latest: _ComposeJob | None = None
        self._wake = threading.Event()
        self._stop = False

    def submit(self, job: _ComposeJob) -> None:
        with self._lock:
            self._latest = job
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def run(self) -> None:
        while True:
            self._wake.wait(0.2)
            if self._stop:
                return
            with self._lock:
                job = self._latest
                self._latest = None
            self._wake.clear()
            if job is None:
                continue
            img = _compose_job_image(job)
            if img is not None:
                self.ready.emit(job, img)


class _LabelJob:
    """一次要素标注取数任务（对应 QGIS 里 render job 内部的 labeling stage）。"""

    __slots__ = ("layer_id", "layer", "field", "rect", "gen", "kind")

    def __init__(self, layer_id, layer, field, rect, gen, kind="view"):
        self.layer_id = layer_id
        self.layer = layer
        self.field = field
        self.rect = rect
        self.gen = gen
        #: "view" = 按当前视口取；"table" = 整层读一次建锚点表
        #: （源文件图层在 614MB 村界面数据上 4.5s/次，整层只付一次）
        self.kind = kind


class _LabelWorker(QThread):
    """常驻标注线程：按视口取锚点，不阻塞 UI。

    为什么单独一条线程（而不是 `_rebuild_labels` 里直接算）：
    QGIS 的标注是渲染 job 的一部分，在后台线程里跑；我们这里取数虽然只取视野内
    的要素，但源文件图层（shapefile 无空间索引）在大范围下 `open_arrow` 本身就要
    一秒多——放在 GUI 线程里就是「缩放到全省界面卡住不动」。

    每个图层只保留最新一次任务（和渲染线程一样的最新优先），上一代结果回来时
    由 `gen` 判断并丢弃，避免旧视野的标注盖到新视野上。正在跑的旧任务也会在
    每批之间检查世代号并提前退出——否则拖动时新视野要等旧视野的整层扫描跑完
    （实测能积到 10s 以上）。
    """

    # 图层 id, 世代号, 任务类型, (field, rect, xy, texts, angles)
    ready = Signal(str, int, str, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._jobs: dict[str, _LabelJob] = {}
        self._latest: dict[str, int] = {}
        self._wake = threading.Event()
        self._stop = False

    def submit(self, job: _LabelJob) -> None:
        with self._lock:
            self._jobs[job.layer_id] = job
            self._latest[job.layer_id] = job.gen
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def _superseded(self, lid: str, gen: int) -> bool:
        with self._lock:
            return self._latest.get(lid, gen) != gen

    def run(self) -> None:
        while True:
            self._wake.wait(0.2)
            if self._stop:
                return
            self._wake.clear()
            with self._lock:
                if not self._jobs:
                    continue
                lid = next(iter(self._jobs))
                job = self._jobs.pop(lid, None)
            if job is None:
                continue
            dead = lambda: self._stop or self._superseded(job.layer_id, job.gen)  # noqa: E731
            try:
                if job.kind == "table":
                    xy, texts, angles, rect = build_label_table(
                        job.layer, job.field, cancelled=dead)
                else:
                    xy, texts, angles = build_label_data(
                        job.layer, job.field, job.rect, cancelled=dead)
                    rect = job.rect
            except Exception:  # noqa: BLE001
                xy, texts, angles, rect = None, [], None, job.rect
            self.ready.emit(job.layer_id, job.gen, job.kind,
                            (job.field, rect, xy, texts, angles))



class _LodJob:
    """一次「整层粗路径」构建任务（源文件图层的宽视野缓存）。"""

    __slots__ = ("layer_id", "provider", "rect", "tol")

    def __init__(self, layer_id, provider, rect, tol):
        self.layer_id = layer_id
        self.provider = provider
        self.rect = rect
        self.tol = tol


class _LodWorker(QThread):
    """后台把源文件图层整层读一遍，建成宽视野用的粗路径缓存。

    为什么要单独一条线程
    --------------------
    shapefile / GPKG 没有空间索引，整层读取实测 3.2~3.6s（47 万条道路）。
    放在 GUI 线程里就是打开图层时界面直接停住；放在渲染线程里则每次宽视野
    渲染都要付一遍。这里读一次、建成约 30MB 的路径缓存，之后宽视野的平移/
    缩放都只在内存里筛批。

    和 `_LabelWorker` 一样按图层「最新任务优先」；图层被删/换源时由
    `_on_lod_ready` 按 provider 身份丢弃过期结果。
    """

    # 图层 id, 构建结果, rect, 容差, provider（身份校验用）
    ready = Signal(str, object, tuple, float, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._jobs: dict[str, _LodJob] = {}
        self._wake = threading.Event()
        self._stop = False

    def submit(self, job: _LodJob) -> None:
        with self._lock:
            self._jobs[job.layer_id] = job
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def run(self) -> None:
        from .vectorstream import build_lod_paths
        while True:
            self._wake.wait(0.2)
            if self._stop:
                return
            self._wake.clear()
            with self._lock:
                if not self._jobs:
                    continue
                lid = next(iter(self._jobs))
                job = self._jobs.pop(lid, None)
            if job is None:
                continue
            payload = None
            batches = None
            try:
                batches = job.provider.iter_bbox(
                    job.rect, batch_size=LOD_READ_BATCH, columns=[])
                payload = build_lod_paths(batches, job.tol,
                                          cancelled=lambda: self._stop,
                                          max_verts=LOD_MAX_VERTS)
            except Exception:  # noqa: BLE001
                payload = None
            finally:
                close = getattr(batches, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
            if self._stop:
                return
            self.ready.emit(job.layer_id, payload, job.rect, job.tol, job.provider)


class _BoundsWorker(QThread):
    """后台补 GeoParquet 等 provider 的 WGS84 范围。

    旧缓存没有 `<cache>.bounds.json` 旁车时，`provider.bounds` 是空的；
    如果直接在 UI/渲染线程里算，21k 块 / 4480 万顶点要十几秒，而且这层
    可能根本在当前视口外。这里先标记 `bounds_pending`，本帧跳过该图层，
    后台算完写旁车后只失效一次，下帧就能像普通图层一样裁剪。
    """

    ready = Signal(str, object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._jobs: dict[str, object] = {}
        self._wake = threading.Event()
        self._stop = False

    def submit(self, lid: str, provider) -> None:
        with self._lock:
            self._jobs[lid] = provider
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    def run(self) -> None:
        while True:
            self._wake.wait(0.2)
            if self._stop:
                return
            self._wake.clear()
            with self._lock:
                if not self._jobs:
                    continue
                lid = next(iter(self._jobs))
                provider = self._jobs.pop(lid, None)
            if provider is None:
                continue
            bounds = None
            try:
                value = getattr(provider, "wgs_bounds", None)
                bounds = value() if callable(value) else value
            except Exception:  # noqa: BLE001
                bounds = None
            if self._stop:
                return
            self.ready.emit(lid, provider, bounds)


class _RenderJob:
    """一次渲染任务（对应 QGIS 的 QgsMapRendererJob / ParallelJob）。

    · `epoch`：世代号。视图一变就 +1，旧世代的结果回来时直接丢弃，
      避免「旧视图的帧覆盖新视图的帧」；
    · `cancelled`：视图又变了就置位，渲染线程在图层间隙、流式 batch/chunk
      边界检查并提前收工（对应 QGIS 的 `feedback()->cancel()`）；
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
      （对应 `LayerRenderJob` + `composeImage`）；多图层时用线程池并行画层，
      但仍按原始图层顺序发 `layerReady`；
    · **渐进上屏**：流式图层每画完一块就把当前图层图发一次，主线程合成出
       provisional 帧；整层/整帧完成后才发 final 帧。这样用户能看到要素
      一块块出现，而不是等所有图层都画完才一次性换图。
    """

    # 图层 id, 图层图像, epoch
    layerReady = Signal(str, object, int)
    # 流式图层的中间结果（同一张图层图在绘制中的副本）
    layerProgress = Signal(str, object, int)
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

        正在跑的任务也要标记取消：大矢量图层一帧可能画 3~5 秒，如果不取消，
        新的缩放/平移只能排队等它自然收尾，用户看到的就是「松手后卡住几秒」。
        取消是协作式的：流式层在每个 chunk 边界、并行层在 wait 循环里检查，
        已经画出的部分保留；旧帧在屏上按新视口平移，不会被清空。
        """
        with self._lock:
            old = self._pending
            cur = self._current
            self._pending = job
        if old is not None:
            old.cancelled = True
        if cur is not None:
            cur.cancelled = True
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
    def _paint_layer_job(self, job: _RenderJob, lj: "_LayerJob",
                         progress_gate=None) -> QImage | None:
        """画一个图层到独立 QImage（对应 QGIS 的 LayerRenderJob）。

        `progress_gate` 只在并行模式使用：`(lock, allowed, idx)`。并行时
        只允许「最下面的未完成层」发中间帧，避免上层先上屏、下层还没画，
        合成顺序看起来像图层顺序被打乱。
        """
        if lj.snap is None:
            return None
        dpr = job.dpr
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
            if lj.snap.get("stream"):
                from .vectorstream import paint_stream_layer

                last_emit = [0.0]

                def _progress() -> None:
                    # 只在中间块后面发：图层图还在同一张 QImage 上继续画，
                    # 必须 copy 一份给主线程，否则合成线程会读到半张图。
                    if job.cancelled:
                        return
                    if progress_gate is not None:
                        lock, allowed, idx = progress_gate
                        with lock:
                            if idx > allowed[0]:
                                return
                    interval = (STREAM_PROGRESS_INTERVAL_SMALL
                                if img.width() * img.height()
                                <= STREAM_PROGRESS_SMALL_PIXELS
                                else STREAM_PROGRESS_INTERVAL)
                    now = time.perf_counter()
                    if now - last_emit[0] < interval:
                        return
                    last_emit[0] = now
                    cp = img.copy()
                    cp.setDevicePixelRatio(dpr)
                    self.layerProgress.emit(lj.lid, cp, job.epoch)

                paint_stream_layer(p, lj.snap, job.world, job.scale, job.mpp,
                                   cancelled=lambda: job.cancelled,
                                   on_progress=_progress)
            else:
                _paint_layer(p, lj.snap, job.world, job.scale, job.mpp)
        finally:
            p.end()
        return img

    def _render_job(self, job: _RenderJob) -> None:
        """按图层渲染；流式层发中间帧，每层画完发 final 图层图。

        `GIS_AGENT_PARALLEL_LAYERS=0` 可退回严格串行，便于对照。
        并行路径对应 `QgsMapRendererParallelJob::prepareJobs()`：每个图层
        一个任务、各画各的 QImage，按原始图层顺序发 `layerReady` 后再合成。
        """
        if PARALLEL_LAYERS:
            self._render_job_parallel(job)
            return
        for lj in job.jobs:
            if job.cancelled:
                return
            img = self._paint_layer_job(job, lj)
            if img is None:
                continue
            lj.image = img
            lj.completed = True
            if job.cancelled:
                return
            # 这一层好了 → 主线程可以先合成出来看
            self.layerReady.emit(lj.lid, img, job.epoch)

    def _render_job_parallel(self, job: _RenderJob) -> None:
        """并行画层，但**按图层顺序**回传，保证合成顺序不变。

        任务本身只写自己的 QImage；`layerReady` 由这里在底层完成后再发。
        上层可以先画完等着，但不会先覆盖下层的中间帧。
        """
        jobs = [lj for lj in job.jobs if lj.snap is not None]
        if len(jobs) < 2:
            for lj in jobs:
                if job.cancelled:
                    return
                img = self._paint_layer_job(job, lj)
                if img is None:
                    continue
                lj.image = img
                lj.completed = True
                if job.cancelled:
                    return
                self.layerReady.emit(lj.lid, img, job.epoch)
            return

        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        n = min(PARALLEL_MAX_WORKERS, len(jobs))
        gate_lock = threading.Lock()
        allowed = [0]
        finished: set[int] = set()
        images: dict[int, QImage] = {}
        emitted: set[int] = set()

        def _work(i: int, lj: "_LayerJob"):
            try:
                img = self._paint_layer_job(job, lj, (gate_lock, allowed, i))
                return i, img
            except Exception:  # noqa: BLE001
                return i, None

        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="layer") as ex:
            futures = [ex.submit(_work, i, lj) for i, lj in enumerate(jobs)]
            pending = set(futures)
            while pending and not job.cancelled:
                done, pending = wait(pending, timeout=0.05,
                                     return_when=FIRST_COMPLETED)
                for f in done:
                    i, img = f.result()
                    finished.add(i)
                    if img is not None:
                        images[i] = img
                # 只按原始顺序释放已完成的层，避免上层先于下层上屏。
                while True:
                    nxt = len(emitted)
                    if nxt >= len(jobs) or nxt not in finished:
                        break
                    lj = jobs[nxt]
                    img = images.pop(nxt, None)
                    if img is not None:
                        lj.image = img
                        lj.completed = True
                        if not job.cancelled:
                            self.layerReady.emit(lj.lid, img, job.epoch)
                    emitted.add(nxt)
                    with gate_lock:
                        allowed[0] = nxt + 1
            if job.cancelled:
                for f in pending:
                    f.cancel()
                return
            # 收尾：正常情况下上面已经全部释放；这里兜住异常任务。
            for i, lj in enumerate(jobs):
                if i in emitted:
                    continue
                img = images.pop(i, None)
                if img is not None:
                    lj.image = img
                    lj.completed = True
                    self.layerReady.emit(lj.lid, img, job.epoch)
                emitted.add(i)


class MapView(QWidget):
    picked = Signal(object)
    selectionChanged = Signal(int)      # 选区要素数
    mapMenu = Signal(QPoint)            # 右键菜单（全局坐标），由主窗口挂菜单项
    viewChanged = Signal()
    cursorMoved = Signal(float, float)
    progress = Signal(str)
    #: 可见的大栅格没有 overview，提示上层可后台生成（只发路径，不阻塞渲染）
    overviewNeeded = Signal(str)

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
        self._fit_pending: str | None = None     # 等范围算完再缩放到指定图层
        self._fit_all_pending = False            # 等范围算完再重新 fit_all
        self.setFocusPolicy(Qt.StrongFocus)      # 方向键平移、+/- 缩放需要能拿到键盘焦点
        self._drag_from: QPoint | None = None
        self._drag_moved = 0
        self._cache: dict[str, _LayerCache] = {}
        # 选区：{图层 id: 要素下标数组}。只记一层（框选只作用于「当前图层」）。
        self._sel: dict[str, np.ndarray] = {}
        self._sel_layer: str | None = None      # 框选的目标图层，由主窗口同步过来
        self._band: tuple | None = None         # 正在拖的选框（屏幕坐标）
        self._hl: tuple | None = None           # 持久高亮（属性表点行），和点击拾取分开
        #: 算子高亮：{图层 id: (要素下标数组, 颜色)}。和 _hl 分开画，
        #: 因为 _hl 是「正在看的单个要素」，这个是「一批被高亮的要素」。
        self._hl_set: dict[str, tuple[np.ndarray, str]] = {}
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
        #: 图层 id → 这张图是**按哪个视口**渲的 (scale, tx, ty, w, h)。
        #: 合成时用它把过期图挪到正确位置，见 `_blit_view_image`。
        self._layer_views: dict[str, tuple] = {}
        self._img_cache: dict[str, tuple] = {}     # lid -> (cacheKey, QImage)
        self._img_keys: dict[str, tuple] = {}      # lid -> 本次请求算出的 cacheKey
        # 同步合成缓冲（测试/兼容路径）；生产渲染走 `_ComposeWorker` 异步出图。
        self._pending: QPixmap | None = None
        self._pending_sig: tuple | None = None
        self._pending_view: tuple | None = None
        self._pending_epoch = -1
        #: 矢量侧是否正在重算（已提交 job、还没拿到整帧）。
        #: 栅格管线是异步的，它完成时可能矢量 job 还在跑；那时候 `_layer_imgs`
        #: 只是一套「缓存命中的子集」，拿它合成会把正在重画的矢量层整个抹掉。
        #: 所以栅格回调能不能上屏，要先问这个标志。
        self._vec_busy = False

        # 常驻渲染线程（最新任务优先）
        self._worker = _RenderWorker(self)
        self._worker.layerReady.connect(self._on_layer_ready)
        self._worker.layerProgress.connect(self._on_layer_progress)
        self._worker.frameReady.connect(self._on_frame_ready)
        self._worker.start()

        # 离屏合成线程：UI 线程不再逐层 drawImage，只接整帧
        self._compose_worker = _ComposeWorker(self)
        self._compose_worker.ready.connect(self._on_compose_ready)
        self._compose_worker.start()

        # --- 栅格管线（完全独立的一条：自己的线程 / 缓存 / 世代号）
        # 见 native/rasterview.py。矢量管线这边只做两件事：
        #   1. 合成时先把栅格图铺在最底下；2. 视图变化时通知它重新请求。
        self._vec_layers: list = []          # 参与矢量渲染链的图层
        self._raster_layers: list = []       # 栅格图层（不进 _cache / 渲染集）
        self._raster_imgs: dict[str, QImage] = {}
        #: 当前 epoch 的栅格最终帧是否已经完成；防止迟到的瓦片中间帧把 final 降级
        self._raster_final_epoch = -1
        self._raster = RasterPipeline(self)
        self._raster.on_layer = self._on_raster_layer
        self._raster.on_partial = self._on_raster_progress
        self._raster.on_frame = self._on_raster_frame

        # --- 要素标注（样式弹窗里配的字段）
        #: 图层 id → {"field","sig","rect","xy","texts"}。按视口抽样缓存：
        #: 平移/缩放离开缓存范围才重取；重取在独立线程里做，不卡界面。
        self._labels: dict[str, dict] = {}
        self._labels_pending: set[str] = set()
        #: 图层 id → 世代号：旧任务的结果回来时按它丢弃
        self._labels_gen: dict[str, int] = {}
        #: 图层 id → 整层锚点表 {"field","xy","texts","angles","rect"}。
        #: 源文件图层整层读一次后，视口切换只在内存里筛（村界面 614MB
        #: 按视口取要 4.5s/次，拖动时就是「标注延后 10 秒」的根源）。
        self._labels_anchors: dict[str, dict] = {}
        #: 图层 id → 正在建的锚点表的世代号：建表期间不要重复提交，
        #: 否则每次重绘都会把上一个表任务作废，永远建不完。
        self._label_tables: dict[str, int] = {}
        self._label_timer = QTimer(self)
        self._label_timer.setSingleShot(True)
        self._label_timer.setInterval(140)
        self._label_timer.timeout.connect(self._rebuild_labels)
        self._label_worker = _LabelWorker(self)
        self._label_worker.ready.connect(self._on_labels_ready)
        self._label_worker.start()

        # 宽视野整层粗路径缓存：后台读一次源文件，之后不再碰磁盘
        self._lod_worker = _LodWorker(self)
        self._lod_worker.ready.connect(self._on_lod_ready)
        self._lod_worker.start()

        # 没有 bounds 旁车的旧 provider：先在后台补范围，本帧先跳过这层
        self._bounds_worker = _BoundsWorker(self)
        self._bounds_worker.ready.connect(self._on_bounds_ready)
        self._bounds_worker.start()

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

        # 分片构建
        self._build_timer = QTimer(self)
        self._build_timer.setInterval(0)
        self._build_timer.timeout.connect(self._build_tick)

    def shutdown(self) -> None:
        """退出前停掉渲染线程，避免 QThread 还在跑就被销毁（会崩进程）。"""
        raster = getattr(self, "_raster", None)
        if raster is not None:
            try:
                raster.shutdown()
            except Exception:
                pass
        worker = getattr(self, "_worker", None)
        if worker is not None:
            try:
                if worker.isRunning():
                    worker.stop()
                    worker.wait(3000)
            except Exception:
                pass
        compose = getattr(self, "_compose_worker", None)
        if compose is not None:
            try:
                if compose.isRunning():
                    compose.stop()
                    compose.wait(2000)
            except Exception:
                pass
        label_worker = getattr(self, "_label_worker", None)
        if label_worker is not None:
            try:
                if label_worker.isRunning():
                    label_worker.stop()
                    label_worker.wait(3000)
            except Exception:
                pass
        lod_worker = getattr(self, "_lod_worker", None)
        if lod_worker is not None:
            try:
                if lod_worker.isRunning():
                    lod_worker.stop()
                    lod_worker.wait(3000)
            except Exception:
                pass
        bounds_worker = getattr(self, "_bounds_worker", None)
        if bounds_worker is not None:
            try:
                if bounds_worker.isRunning():
                    bounds_worker.stop()
                    bounds_worker.wait(3000)
            except Exception:
                pass

    def __del__(self):  # noqa: D105
        # 兜底：非正常退出（没有走 closeEvent）时也尽量把线程停掉
        try:
            raster = getattr(self, "_raster", None)
            if raster is not None:
                raster.shutdown()
        except Exception:
            pass
        try:
            worker = getattr(self, "_worker", None)
            if worker is not None and worker.isRunning():
                worker.stop()
                worker.wait(1500)
        except Exception:
            pass
        try:
            compose = getattr(self, "_compose_worker", None)
            if compose is not None and compose.isRunning():
                compose.stop()
                compose.wait(1000)
        except Exception:
            pass
        try:
            lw = getattr(self, "_label_worker", None)
            if lw is not None and lw.isRunning():
                lw.stop()
                lw.wait(1000)
        except Exception:
            pass
        try:
            lod = getattr(self, "_lod_worker", None)
            if lod is not None and lod.isRunning():
                lod.stop()
                lod.wait(1000)
        except Exception:
            pass

    def closeEvent(self, e):  # noqa: N802
        self.shutdown()
        super().closeEvent(e)

    # ------------------------------------------------------------------ 数据
    def _use_provider(self, c: _LayerCache, lay) -> bool:
        """把源文件 provider 挂到图层缓存；返回模式是否变化。"""
        prov = None
        if c.stream:
            try:
                prov = getattr(lay, "provider", None)
            except Exception:
                prov = None
        changed = prov is not c.provider
        c.provider = prov
        if prov is not None:
            c.total = int(getattr(prov, "count", 0) or 0)
            # Parquet provider 打开时只有文件头、没有 bbox；不要为了给隐藏
            # 图层填 extent 去扫几十万顶点（那会把内存又推高）。真正 fit /
            # zoom-to-layer 时再按需调用 wgs_bounds。GeoParquet 的 WGS84
            # 范围由写入缓存时留下的旁车提供，打开时零扫描。
            hint = getattr(prov, "wgs_bounds_hint", None)
            if hint:
                c.extent = tuple(hint)
            elif getattr(prov, "bounds", None):
                b = getattr(prov, "wgs_bounds", None)
                if b is not None:
                    c.extent = tuple(b)
            if changed:
                # 不缓存整层几何/路径：provider 每次按屏幕 bbox 读取
                c.geoms = None
                c.types = None
                c.bounds = None
                c.wgs_ready = False
                c.sets.clear()
                c.build = None
                c.bounds_out = None
                c.bounds_pos = 0
                c.lod_state = ""
                c.bounds_pending = False
                c.bounds_failed = False
                # 换了数据源：旧锚点表/旧标注缓存都不再对应当前数据
                self._labels_anchors.pop(lay.id, None)
                self._labels.pop(lay.id, None)
                self._label_tables.pop(lay.id, None)
                self._labels_pending.discard(lay.id)
                self._labels_gen[lay.id] = self._labels_gen.get(lay.id, 0) + 1
            self._ensure_bounds_async(lay, c)
        return changed

    def _ensure_bounds_async(self, lay, c: _LayerCache, force: bool = False) -> None:
        """provider 没有范围时丢给后台算；本帧暂不画这一层。

        旧 GeoParquet 缓存只有文件头，没有 bbox；如果为了“判断在不在视口里”
        在 UI 线程扫 4480 万顶点，就会重现 13 秒卡顿。后台算完写旁车，
        之后恢复零扫描。
        """
        if (c.provider is None or c.extent is not None or c.bounds_pending
                or c.bounds_failed):
            return
        hint = getattr(c.provider, "wgs_bounds_hint", None)
        if hint:
            c.extent = tuple(hint)
            return
        if getattr(c.provider, "bounds", None):
            b = getattr(c.provider, "wgs_bounds", None)
            if b is not None:
                c.extent = tuple(b)
                return
        # 隐藏图层先不扫；显式「缩放到图层」或显示后再排
        if not force and not self.visible.get(lay.id, True):
            return
        c.bounds_pending = True
        self._bounds_worker.submit(lay.id, c.provider)

    def _on_bounds_ready(self, lid: str, provider, bounds) -> None:
        c = self._cache.get(lid)
        if c is None or c.provider is not provider:
            return                  # 图层删了/换源了
        c.bounds_pending = False
        if bounds is not None and len(bounds) == 4:
            c.extent = tuple(float(v) for v in bounds)
            if self._fit_pending == lid:
                self._fit_pending = None
                self.fit_bounds(*c.extent, pad=0.15)
                return
            if self._fit_all_pending:
                self._fit_all_pending = False
                self.fit_all()
                return
            if self._view_need(c) is None:
                return          # 范围外，范围回填不改变当前画面
            if not self._vec_busy:
                # 只重画矢量，不重新请求栅格：否则会把正在下载的在线瓦片
                # 取消并从头下一遍。
                self._start_render(request_raster=False)
        else:
            c.bounds_failed = True
            if self._fit_pending == lid:
                self._fit_pending = None
            if self._fit_all_pending:
                self._fit_all_pending = False
                self.fit_all()

    # ---------------------------------------------------- 宽视野整层粗路径缓存
    def _maybe_start_lod(self, lay, c: _LayerCache) -> None:
        """源文件图层够大就排一个后台任务，把整层读一次建成粗路径缓存。

        只做「宽视野会用到」的那些层：线/面、要素数够多、范围已知。
        点图层不建 —— 大点图层走 `point_dots` 的屏幕像素桶，本来就不建路径。
        """
        if not c.stream or c.provider is None or c.lod_state:
            return
        if getattr(c.provider, "live", False):
            # 数据库图层：整层 LOD 缓存 = 按「全世界」bbox 把整表读一遍，
            # 正是这次要消灭的动作。宽视野下数据库照样按视野过滤，不预读。
            return
        if not self.visible.get(lay.id, True):
            return
        try:
            if int(c.total or 0) < LOD_MIN_FEATURES:
                return
        except Exception:  # noqa: BLE001
            return
        if c.extent is None or self._view_need(c) is None:
            return
        if self.mpp() < LOD_MIN_MPP:
            return          # 城市级用不到整层粗路径；等缩到宽视野再建
        try:
            geom = str((lay.meta() or {}).get("geomType") or "")
        except Exception:  # noqa: BLE001
            geom = ""
        if "Point" in geom and "Poly" not in geom and "Line" not in geom:
            return
        ext = c.extent
        pad = LOD_TOL * 2.0
        rect = (ext[0] - pad, ext[1] - pad, ext[2] + pad, ext[3] + pad)
        c.lod_state = "building"
        self._lod_worker.submit(_LodJob(lay.id, c.provider, rect, LOD_TOL))

    def _maybe_prefetch_labels(self, lay, c: _LayerCache) -> None:
        """配了标注字段的源文件图层：图层一进来就在后台建锚点表。

        村界面这种 614MB 的 parquet，按视口取一次标注要 4.5s；等用户第一次
        平移再开始建，看起来就是「标注一直不出来」。放在打开图层时建，用户
        看完初始视图时表多半已经好了。
        """
        try:
            st = self._layer_paint_style(lay)
        except Exception:  # noqa: BLE001
            return
        field = st.get("label_field") or ""
        if not field:
            return
        if not self.visible.get(lay.id, True):
            return          # 隐藏的图层不预建；等用户打开时第一次重绘再建
        c = self._cache.get(lay.id)
        if c is not None and (c.bounds_pending or c.bounds_failed
                              or self._view_need(c) is None):
            return          # 视口外不建整层锚点表
        if self._label_table_ok(lay, field):
            return
        if lay.id in self._label_tables:
            return
        if not self._label_table_eligible(lay):
            return
        gen = self._labels_gen.get(lay.id, 0) + 1
        self._labels_gen[lay.id] = gen
        self._label_tables[lay.id] = gen
        self._label_worker.submit(_LabelJob(lay.id, lay, field, None, gen,
                                            kind="table"))


    def _lod_set(self, c: _LayerCache) -> _RenderSet | None:
        """当前视野可用的整层粗路径集合（不覆盖或太粗时返回 None）。

        「太粗」= 缓存的容差比当前视口需要的还大：这时候必须继续走流式
        读窗口，否则放大后道路会变成折线。
        """
        if not c.stream or c.provider is None or not c.sets:
            return None
        need = self._view_need(c)
        if need is None:
            return None
        cur = self._simplify_tol()
        for s in c.sets:
            if not s.built:
                continue
            if s.tol > cur * 1.02:
                continue
            if not _rect_contains(s.rect, need):
                continue
            s.used = time.perf_counter()
            return s
        return None

    def _on_lod_ready(self, lid: str, payload, rect: tuple, tol: float,
                      provider) -> None:
        """整层粗路径建好了：收进图层缓存，之后宽视野直接用。"""
        c = self._cache.get(lid)
        if c is None or c.provider is not provider:
            return                  # 图层删了，或者换了数据源
        if payload is None:
            c.lod_state = ""
            return
        poly_paths, poly_bounds, line_paths, line_bounds, points, point_bounds, nfeat = payload
        s = _RenderSet(tol, rect)
        s.poly_paths = poly_paths
        s.poly_bounds = poly_bounds
        s.line_paths = line_paths
        s.line_bounds = line_bounds
        s.points = points
        s.point_bounds = point_bounds
        s.nfeat = int(nfeat)
        s.built = True
        s.used = time.perf_counter()
        c.sets = [s]
        c.lod_state = "ready"
        c.rev += 1
        if not self._vec_busy:
            # 没有渲染在飞：立刻按新缓存重画一次（宽视野下 ~0.3s）。
            self._invalidate_frame(0)
            self.update()
        # 有渲染在飞就别打断它：这一帧是流式读出来的，细度不比缓存差；
        # 现在取消它再重画只是白费一次整层读。下一次视图变化自然会用上缓存。

    def set_layers(self, layers: list) -> None:
        had = bool(self.layers)
        self.layers = layers
        # 栅格和矢量分流：栅格**不**建 _LayerCache，于是矢量那套
        # 「渲染集 / 构建分片 / 视口筛要素」全部天然跳过它（它们的循环都
        # 以 `_cache.get(id) is None` 开头）。这条分界线是两条管线的接缝。
        self._vec_layers = [l for l in layers if not getattr(l, "is_raster", False)]
        self._raster_layers = [l for l in layers if getattr(l, "is_raster", False)]
        keep = {l.id for l in self._vec_layers}
        for lid in list(self._cache):
            if lid not in keep:
                self._cache.pop(lid, None)
                self._img_cache.pop(lid, None)
                self._layer_imgs.pop(lid, None)
                self._layer_views.pop(lid, None)
        for lay in self._vec_layers:
            if lay.id not in self._cache:
                c = _LayerCache()
                c.stream = _stream_enabled(lay)
                self._use_provider(c, lay)
                self._cache[lay.id] = c
            else:
                # 图层对象可能被替换（同名重建），重新判断一次阈值
                c = self._cache[lay.id]
                was = c.stream
                c.stream = _stream_enabled(lay)
                changed = self._use_provider(c, lay)
                if c.stream != was or changed:
                    # 切换渲染模式：旧的图层图和路径都不能再复用
                    self._img_cache.pop(lay.id, None)
                    if c.stream:
                        c.sets.clear()
                        c.build = None
            self._maybe_start_lod(lay, c)
            self._maybe_prefetch_labels(lay, c)
        # 栅格管线自己维护缓存：换图层集合时同步过去
        self._raster.set_layers(self._raster_layers)
        self._raster.set_visible(self.visible)
        alive = {l.id for l in self._raster_layers}
        for lid in list(self._raster_imgs):
            if lid not in alive:
                self._raster_imgs.pop(lid, None)
        # 标注缓存跟着图层集合走：删掉的图层不能再留锚点
        stale = set(self._labels) - {l.id for l in self._vec_layers}
        for lid in stale:
            self._labels.pop(lid, None)
            self._labels_pending.discard(lid)
            self._labels_anchors.pop(lid, None)
            self._label_tables.pop(lid, None)
            self._labels_gen[lid] = self._labels_gen.get(lid, 0) + 1
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
            self._layer_views.pop(layer_id, None)
            self._labels.pop(layer_id, None)
            self._labels_pending.discard(layer_id)
            self._labels_anchors.pop(layer_id, None)
            self._label_tables.pop(layer_id, None)
            self._labels_gen[layer_id] = self._labels_gen.get(layer_id, 0) + 1
        else:
            self._cache.clear()
            self._img_cache.clear()
            self._layer_imgs.clear()
            self._layer_views.clear()
            self._labels.clear()
            self._labels_pending.clear()
            self._labels_anchors.clear()
            self._label_tables.clear()
            self._labels_gen.clear()
        self._invalidate_frame()

    def refresh_layer_style(self, layer_id: str) -> None:
        """图层样式改了：作废这一层的图形缓存（几何路径可留），立刻重画。

        矢量：几何路径和样式无关，不用重建；只要丢掉按旧样式合成的图层图。
        栅格：透明度/拉伸在栅格管线里，交给它自己的 `bump_rev`。
        """
        self._img_cache.pop(layer_id, None)
        self._layer_imgs.pop(layer_id, None)
        self._layer_views.pop(layer_id, None)
        self._labels.pop(layer_id, None)
        self._labels_pending.discard(layer_id)
        self._labels_anchors.pop(layer_id, None)
        self._label_tables.pop(layer_id, None)
        # 世代 +1：正在跑的任务结果回来时会被丢弃，不会把旧样式字段的标注画上来
        self._labels_gen[layer_id] = self._labels_gen.get(layer_id, 0) + 1
        c = self._cache.get(layer_id)
        if c is not None:
            c.rev += 1
        try:
            self._raster.bump_rev(layer_id)
        except Exception:  # noqa: BLE001
            pass
        self._invalidate_frame(0)

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

    def _layer_bounds(self, lay):
        """图层的经纬度范围。矢量取已缓存的 extent 或 gdf，栅格取元信息。

        关键：provider 没有旁车/文件头范围时**返回 None**，绝不在 UI 线程
        调 `lay.wgs_bounds`（旧 GeoParquet 会扫 4480 万顶点，13 秒）。
        这种图层交给 `_BoundsWorker`，算完由 `_on_bounds_ready` 再 fit。
        """
        if getattr(lay, "is_raster", False):
            return lay.info.bounds
        c = self._cache.get(lay.id)
        if c is not None and c.extent is not None:
            return c.extent
        # provider 图层：只用文件头/旁车，不读全量几何
        try:
            if getattr(lay, "_gdf", None) is None:
                prov = getattr(lay, "provider", None)
                if prov is not None:
                    hint = getattr(prov, "wgs_bounds_hint", None)
                    if hint:
                        return tuple(hint)
                    if getattr(prov, "bounds", None):
                        wb = getattr(prov, "wgs_bounds", None)
                        if wb:
                            return tuple(wb)
                    return None
            gdf = lay.gdf
        except Exception:
            return None
        b = gdf.total_bounds
        # GDB / Shapefile 常常是投影坐标（如 CGCS2000 3 度带，单位米）。
        # fit_all() 要的是经纬度；直接拿米制 bounds 会让视口飞到经度几千万度。
        crs = getattr(gdf, "crs", None)
        try:
            if crs is not None and crs.to_epsg() != 4326:
                from pyproj import Transformer
                t = Transformer.from_crs(crs, 4326, always_xy=True)
                return t.transform_bounds(*b, densify_pts=21)
        except Exception:
            pass
        return b

    def fit_all(self) -> None:
        b = None
        need_bounds = False
        for lay in self.layers:
            if not self.visible.get(lay.id, True):
                continue
            c = self._cache.get(lay.id)
            if c is not None and c.provider is not None and c.extent is None:
                if not c.bounds_failed:
                    self._ensure_bounds_async(lay, c)
                    need_bounds = True
                continue
            try:
                lb = self._layer_bounds(lay)
            except Exception:
                continue
            if lb is None or len(lb) != 4 or not all(map(math.isfinite, lb)):
                continue
            b = lb if b is None else [min(b[0], lb[0]), min(b[1], lb[1]),
                                      max(b[2], lb[2]), max(b[3], lb[3])]
        if b is None:
            if need_bounds:
                # 先不跳到默认中国范围；等后台范围回来后再 fit_all 一次
                self._fit_all_pending = True
                return
            self.fit_bounds(74, 18, 135.5, 53.5, pad=0.02)
        else:
            self.fit_bounds(b[0], b[1], b[2], b[3], pad=0.12)
        if need_bounds:
            self._fit_all_pending = True

    def fit_layer(self, layer_id: str) -> None:
        lay = next((l for l in self.layers if l.id == layer_id), None)
        if lay is None:
            return
        c = self._cache.get(lay.id)
        if (c is not None and c.provider is not None and c.extent is None
                and not c.bounds_failed):
            # 旧缓存没有范围：后台算完再缩放到它，别让「缩放到图层」卡 13 秒
            self._fit_pending = lay.id
            self._ensure_bounds_async(lay, c, force=True)
            return
        try:
            b = self._layer_bounds(lay)
            self.fit_bounds(b[0], b[1], b[2], b[3], pad=0.15)
        except Exception:
            pass

    def fit_bounds(self, x0: float, y0: float, x1: float, y1: float, pad: float = 0.1) -> None:
        # 必须转成 Python float：调用方常直接传 gdf.total_bounds 里的
        # numpy.float64，算出来的 scale/tx/ty 也会是 numpy 标量。它一旦流进
        # `_blit_view_image`，`abs(s/s0 - 1) > 1e-9` 就是 numpy.bool_，
        # QPainter.setRenderHint() 不接受（见那个函数的注释）。
        x0, y0, x1, y1 = float(x0), float(y0), float(x1), float(y1)
        if not all(map(math.isfinite, (x0, y0, x1, y1))):
            return
        px0, py0 = project(x0, y1)
        px1, py1 = project(x1, y0)
        bw = max(abs(px1 - px0), 1e-9)
        bh = max(abs(py1 - py0), 1e-9)
        s = min(self.width() / bw, self.height() / bh) * (1 - pad * 2)
        self.scale = float(min(max(s, 60.0), 4e7))
        cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
        self.tx = float(self.width() / 2 - cx * self.scale)
        self.ty = float(self.height() / 2 - cy * self.scale)
        self._invalidate_frame()
        self._kick_build()
        self.viewChanged.emit()
        self.update()

    def zoom_at(self, factor: float, px: float | None = None, py: float | None = None) -> None:
        px = self.width() / 2 if px is None else float(px)
        py = self.height() / 2 if py is None else float(py)
        wx = (px - self.tx) / self.scale
        wy = (py - self.ty) / self.scale
        # 同样保持 Python float（原因见 fit_bounds）
        self.scale = float(min(max(self.scale * factor, 40.0), 6e7))
        self.tx = float(px - wx * self.scale)
        self.ty = float(py - wy * self.scale)
        self._invalidate_frame()
        self._kick_build()
        self.viewChanged.emit()
        self.update()

    def zoom_by(self, dz: float) -> None:
        self.zoom_at(2 ** dz)

    def set_view(self, scale: float, tx: float, ty: float) -> None:
        """把视图设成另一张地图的 scale/tx/ty（双视图同步用，不再套 fit）。"""
        try:
            s = float(scale)
            x = float(tx)
            y = float(ty)
        except (TypeError, ValueError):
            return
        if (abs(s - float(self.scale)) <= 1e-12
                and abs(x - float(self.tx)) <= 1e-9
                and abs(y - float(self.ty)) <= 1e-9):
            return
        self.scale = s
        self.tx = x
        self.ty = y
        self._invalidate_frame()
        self._kick_build()
        self.viewChanged.emit()
        self.update()

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

    def _invalidate_frame(self, delay: int | None = None) -> None:
        """标记当前帧需要重绘（不影响已缓存的几何路径）。"""
        if delay is None:
            # 有矢量时保留较长 debounce（避免边拖边重建路径）；纯栅格地图
            # 主要成本是源块读取，60ms 已经足够合并滚轮事件，手感更直接。
            delay = (RENDER_DEBOUNCE_MS if getattr(self, "_vec_layers", None)
                     else RASTER_RENDER_DEBOUNCE_MS)
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

    def _pan_gap_too_wide(self) -> bool:
        """拖拽中要不要重新渲染：判据是「露出的空白边有多宽」。

        平移期间画面是把整帧当位图偏移贴上去的（`paintEvent`），偏移多少就露多少
        空白。所以阈值按**像素**算，而不是只按视口比例：900 px 宽的窗口用
        `0.3 × 视口 = 270 px` 做阈值的话，小幅拖拽永远等不到重绘，右/下就留一圈
        空边。这里取 `min(视口 × 比例, PAN_RERENDER_PX)`，大窗口下也能在几十像素内
        补齐；小窗口下比例阈值本来就更严格，行为不变。
        """
        if self._frame_view is None:
            return False
        w_gap = min(self.width() * PAN_RERENDER_RATIO, PAN_RERENDER_PX)
        h_gap = min(self.height() * PAN_RERENDER_RATIO, PAN_RERENDER_PX)
        return (abs(self.tx - self._frame_view[1]) > w_gap
                or abs(self.ty - self._frame_view[2]) > h_gap)

    def _end_pan(self) -> None:
        """拖拽结束：不管移了几个像素，都按新视口重画一次。

        拖拽中为了跟手只做位图偏移，边缘会留下空白；松手后人不再动，
        正是补齐的最好时机。不加这条，小幅拖拽（没到重绘阈值）会一直留着空边，
        直到下一次缩放才消失。
        """
        if self._frame_view is None:
            return
        if (abs(self.tx - self._frame_view[1]) >= 0.5
                or abs(self.ty - self._frame_view[2]) >= 0.5
                or abs(self.scale - self._frame_view[0]) > 1e-9):
            self._invalidate_frame(0)

    def _render_frame(self) -> None:
        """渲染定时器回调：发起一次渲染（对应 QgsMapRendererJob::start()）。"""
        self._start_render()

    def _start_render(self, request_raster: bool = True) -> None:
        """发起一次渲染（对应 QgsMapRendererJob::start()）。

        · 每次视图变化都会给 job 分配新的世代号（epoch），旧世代的结果一律丢弃；
        · 每个图层一个 `_LayerJob`，命中图层缓存的直接复用上次的图，
          不再重绘（切显隐、调顺序几乎零成本）；
        · 流式图层在块边界发 `layerProgress`，主线程可先显示 provisional 帧；
          整帧画完后 `frameReady` 再发 final 帧整体替换。
        """
        if self.width() < 10 or self.height() < 10:
            return
        # 已经由当前视口发起渲染，就不要再让旧的 debounce 定时器补一枪：
        # 在线瓦片会被第二次 request 取消并从头下载，看起来像“加载两遍”。
        if self._render_timer.isActive():
            self._render_timer.stop()
        # 从这里到函数结束，矢量图都可能被换掉：期间禁止栅格回调上屏
        self._vec_busy = True
        # 栅格管线先走一步：它是独立的线程和缓存，成败都不影响下面的矢量流程。
        # 这里必须兜住异常 —— 「栅格坏了」绝不能变成「矢量也不画了」。
        # 矢量构建过程中的 partial 帧不重新请求栅格：视口没变，重发只会
        # 取消正在下载的在线瓦片，看起来像瓦片反复重下。
        if request_raster:
            try:
                self._request_raster()
            except Exception:  # noqa: BLE001
                pass
        # 旧 provider 缓存没有范围旁车：先在后台补，别为了判断可见性扫全层。
        for lay in self._vec_layers:
            if not self.visible.get(lay.id, True):
                continue
            c = self._cache.get(lay.id)
            if c is not None:
                self._ensure_bounds_async(lay, c)
                self._maybe_start_lod(lay, c)
        self._kick_build()
        snapshot = self._make_snapshot()
        if snapshot is None:
            # 三种「没有东西可画」要区别对待：
            # · 还有可见矢量图层，几何正在构建 → 继续显示旧帧，别闪白；
            # · **一个可见矢量图层都没有**（用户把图层全隐藏了，或者只有栅格）
            #   → 必须合成一张空帧，否则旧帧会一直留在屏幕上，
            #   「显示 / 隐藏」看起来就像没生效；
            # · 有可见矢量图层、但**没有在飞的构建、也没有可用渲染集**
            #   → 视口已经平移到数据范围之外（或者这一级缩放下筛不出要素），
            #     同样必须出空帧；否则用户把地图拖到没数据的地方，屏幕上还是
            #     原来那块内容，看着就像「拖不动」。
            #   注意这里只看**矢量**图层：栅格没有渲染集，混进来会误判成「还在准备」。
            if (any(self.visible.get(l.id, True) for l in self._vec_layers)
                    and self._build_pending()):
                return
            self._epoch += 1
            epoch = self._epoch
            self._layer_imgs = {}
            self._layer_views = {}
            self._vec_busy = False
            self._compose_async(
                self._scene_sig(),
                (self.scale, self.tx, self.ty, self.width(), self.height()),
                epoch, "force")
            return
        self._epoch += 1
        epoch = self._epoch
        dpr = self._device_pixel_ratio()
        view_now = (self.scale, self.tx, self.ty, self.width(), self.height())
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
            self._vec_busy = False
            return
        if all(j.snap is None for j in jobs):
            # 全命中缓存 → 直接合成上屏，不用渲染线程
            self._layer_imgs = dict(reuse)
            self._layer_views = {lid: view_now for lid in reuse}
            self._vec_busy = False
            self._compose_async(
                self._scene_sig(),
                (self.scale, self.tx, self.ty, self.width(), self.height()),
                epoch, "force")
            self._rendered_geo_rev = self._geo_rev
            return
        # 没命中的图层先保留上一帧的图（按当前视口平移/缩放贴上去），
        # 新图/中间图回来再逐层替换。不能直接清空：否则流式渲染刚起步时，
        # 还没轮到的图层会整片消失，看起来就是「闪白」。
        incoming = {j.lid for j in jobs}
        kept = {lid: img for lid, img in self._layer_imgs.items() if lid in incoming}
        kept.update(reuse)
        self._layer_imgs = kept
        for lid in reuse:
            self._layer_views[lid] = view_now
        job = _RenderJob(epoch, jobs, self._scene_sig(),
                         (self.scale, self.tx, self.ty, self.width(), self.height()),
                         self._view_world_rect(),
                         self.width(), self.height(), dpr,
                         self.scale, self.tx, self.ty, self.mpp())
        self._job = job
        self._rendered_geo_rev = self._geo_rev
        # 提交给常驻渲染线程：队列只保留最新一次，不会堆积成「管线堵住」
        self._worker.submit(job)

    # ------------------------------------------------------------- 栅格管线接线
    def _request_raster(self) -> None:
        """把当前视口交给栅格管线（对应 QgsRasterViewPort 的填充）。

        只做三件事：算视口经纬度范围、算设备像素尺寸、交出去。
        重采样/拉伸/读窗口全在 rasterview + app.raster 里，这边不掺和。
        """
        if not self._raster_layers:
            self._raster_imgs = {}
            return
        dpr = self._device_pixel_ratio()
        vp = RasterViewPort(
            extent=self._view_lonlat_rect(),
            width=max(1, int(self.width() * dpr)),
            height=max(1, int(self.height() * dpr)),
            scale=float(self.scale), tx=float(self.tx), ty=float(self.ty), dpr=dpr,
            world=self._view_world_rect(),
        )
        self._raster.set_visible(self.visible)
        # 大栅格没有 overview：通知上层后台生成，避免每次缩放都重读全图。
        # 只发一次判断，生成/去重由 MainWindow 负责；不阻塞当前渲染。
        for lay in self._raster_layers:
            if not self.visible.get(lay.id, True):
                continue
            try:
                info = lay.info
                b = getattr(info, "bounds", None)
                e = vp.extent
                if b is not None and len(b) == 4:
                    if (float(b[2]) < float(e[0]) or float(b[0]) > float(e[2])
                            or float(b[3]) < float(e[1]) or float(b[1]) > float(e[3])):
                        continue    # 视口外的栅格不必为它生成 overview
                if (not getattr(info, "overviews", ())
                        and int(info.width) * int(info.height) >= 4_000_000):
                    self.overviewNeeded.emit(str(info.path))
            except Exception:  # noqa: BLE001
                continue
        self._raster.request(vp)

    def _on_raster_layer(self, lid: str, img) -> None:
        """某个栅格图层画好了：存起来并立刻合成上屏（栅格可能比矢量先完成）。"""
        self._raster_imgs[lid] = img
        # 矢量还在整帧重算时，图层图只存着，等矢量帧完成后一起合成，
        # 避免每个栅格图层都触发一次全屏合成、和矢量抢 UI 时间。
        if self._vec_busy and self._frame is not None:
            return
        # 单层 final 不等于整轮栅格 final（在线瓦片和本地栅格现在并行）；
        # 在全部栅格 worker 收尾前仍按 provisional 合成，不登记整帧缓存。
        self._recompose_now(provisional=self._raster_final_epoch != self._epoch)

    def _on_raster_progress(self, lid: str, img) -> None:
        """在线瓦片到一张就存一张：先合成半成品，不写完整帧缓存。

        和矢量流式图的 `_on_layer_progress` 一样，用 provisional 模式上屏：
        `_frame_sig` 不登记，避免平移/缩放时把“只到了几块瓦片”的图当成
        完整帧复用。
        """
        if self._raster_final_epoch == self._epoch:
            return                      # final 已出，迟到的中间帧不能再降级
        self._raster_imgs[lid] = img
        if self._vec_busy and self._frame is not None:
            return
        self._recompose_now(provisional=True)

    def _on_raster_frame(self) -> None:
        # 先登记：即使矢量还在忙，这一轮栅格的 final 也已经到了；
        # 后续排队的瓦片 progress 不能再把它降级成 provisional。
        self._raster_final_epoch = self._epoch
        if self._vec_busy and self._frame is not None:
            return
        self._recompose_now()

    def _recompose_now(self, provisional: bool = False) -> None:
        """用当前视口参数重新合成一次（栅格管线的独立触发点）。

        不复用矢量 job 的 sig/view —— 栅格的完成时刻和矢量任务没有关系，
        可能矢量还在建几何、栅格已经画完了，这时也得让用户看到栅格。

        矢量 job 还在跑时，栅格回调先不驱动上屏（见 `_on_raster_layer`）：
        否则会和矢量中间帧抢合成线程；等矢量整帧的 provisional/final 一起出。
        """
        if self.width() < 10 or self.height() < 10:
            return
        view = (float(self.scale), float(self.tx), float(self.ty),
                self.width(), self.height())
        mode = "raster_progress" if provisional else "auto"
        self._compose_async(self._scene_sig(), view, self._epoch, mode)

    def _on_layer_progress(self, lid: str, img, epoch: int) -> None:
        """流式图层的中间结果：先把当前帧换成「画到这里」的 provisional 帧。

        provisional 帧只用于显示，不写图层缓存，也不设置 `_frame_sig`：
        `_frame_usable()` 会一直认为它不完整，直到 `_on_frame_ready()` 的
        final 帧回来。这样拖拽/缩放不会把半成品当成可复用的整帧。
        """
        if epoch != self._epoch or img is None:
            return
        job = self._job
        if job is None:
            return
        self._layer_imgs[lid] = img
        self._layer_views[lid] = job.view
        self._compose_async(job.sig, job.view, epoch, "progress")

    def _on_layer_ready(self, lid: str, img, epoch: int) -> None:
        """某个图层渲染好了：存图并合成到「待上屏」的缓冲里。"""
        if epoch != self._epoch or img is None:
            return                      # 过期世代，丢弃
        job = self._job
        if job is None:
            return
        self._layer_imgs[lid] = img
        self._layer_views[lid] = job.view        # 这张图属于 job.view 那个视口
        # 存进图层缓存，下次同视口切显隐可直接复用
        key = self._img_keys.get(lid)
        if key is not None:
            self._img_cache[lid] = (key, img)
        # 先显示已完成的图层；整帧最后一层完成时由 _on_frame_ready 再合成 final。
        mode = "first" if self._frame is None else "progress"
        self._compose_async(job.sig, job.view, epoch, mode)

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

    def _compose_async(self, sig, view, epoch: int, mode: str = "auto") -> None:
        """把合成交给常驻合成线程；主线程只等整帧结果。"""
        if self.width() < 10 or self.height() < 10:
            return
        dpr = self._device_pixel_ratio()
        w = max(1, int(self.width() * dpr))
        h = max(1, int(self.height() * dpr))
        # 自下而上叠：`self.layers` 的顺序就是面板顺序（最后一个压在最上面）。
        # 栅格和矢量混在同一个列表里，这样把一张栅格拖到矢量上面才真的有效果。
        items = []
        for lay in self.layers:
            if not self.visible.get(lay.id, True):
                continue
            if getattr(lay, "is_raster", False):
                rimg = self._raster_imgs.get(lay.id)
                if rimg is not None:
                    items.append((True, rimg, self._raster.view_of(lay.id)))
            else:
                vimg = self._layer_imgs.get(lay.id)
                if vimg is not None:
                    items.append((False, vimg, self._layer_views.get(lay.id)))
        job = _ComposeJob(epoch, sig, view, dpr, w, h, items, mode)
        try:
            self._compose_worker.submit(job)
        except Exception:
            pass

    def _on_compose_ready(self, job: _ComposeJob, img) -> None:
        """合成线程完成：校验世代/视口后整体替换当前帧。"""
        if job.epoch != self._epoch:
            return
        view_now = (float(self.scale), float(self.tx), float(self.ty),
                    self.width(), self.height())
        if tuple(job.view) != view_now:
            return
        if img is None or img.isNull():
            return
        promote = (job.mode == "force" and not self._vec_busy
                   or (job.mode == "auto"
                       and (self._frame is None or not self._vec_busy))
                   or (job.mode == "first" and self._frame is None)
                   # 只在本轮矢量任务还没结束时贴中间帧；final 之后迟到的
                   # progress 结果不能再把完整帧降级成半成品。
                   or (job.mode == "progress" and self._vec_busy)
                   # 在线瓦片先到一块就上屏；矢量同步完成时也允许贴 provisional。
                   # final 之后迟到的中间帧直接丢弃，避免把完整图降级。
                   or (job.mode == "raster_progress" and not self._vec_busy
                       and self._raster_final_epoch != job.epoch))
        if not promote:
            return
        try:
            pix = QPixmap.fromImage(img)
            pix.setDevicePixelRatio(job.dpr)
        except Exception:
            return
        self._frame = pix
        self._frame_epoch = job.epoch
        self._frame_view = job.view
        # provisional 帧故意不登记 sig：它只用于显示，不能被当成完整帧复用。
        # first 也是「先显示已完成的图层」，同样要等 force 的 final 帧才算完整。
        self._frame_sig = (None if job.mode in
                           ("progress", "first", "raster_progress")
                           else job.sig)
        if job.mode == "force":
            self._vec_busy = False
        self.update()

    def _compose(self, sig, view, epoch: int) -> None:
        """把各图层图像按顺序叠成一帧（对应 QGIS 的 composeImage）。

        每次都在**一张干净的画布**上从零合成，绝不在上一帧上叠 ——
        否则同一个图层会被重复叠加（14% 的填充叠几次就变成实色），
        看起来就像「有些地方渲染错了 / 没渲染出来」。
        这是兼容用的同步路径：结果先放 `_pending`，由调用方整体上屏；
        生产路径是 `_compose_async` + `_on_compose_ready`。
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
        # 合成是两条管线**唯一**共用的写画布路径，必须保证「一层画坏了不能
        # 连累别人」：以前这里任何一层抛异常都会跳过后面所有图层（栅格先画，
        # 于是矢量整片消失），并且 `p` 带着未配对的 save()/活跃画笔退出。
        # 现在逐层兜底 + finally 收笔，异常最多丢那一层，帧照常完整。
        try:
            # 自下而上逐层叠：栅格和矢量共用同一份图层顺序（面板顺序），
            # 所以把栅格拖到矢量上面、或者把矢量拖到栅格下面都能立刻生效。
            for lay in self.layers:
                if not self.visible.get(lay.id, True):
                    continue
                if getattr(lay, "is_raster", False):
                    rimg = self._raster_imgs.get(lay.id)
                    if rimg is None:
                        continue
                    try:
                        self._blit_view_image(p, rimg, self._raster.view_of(lay.id),
                                              view, dpr, device_size=True)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                img = self._layer_imgs.get(lay.id)
                if img is not None:
                    try:
                        self._blit_view_image(p, img, self._layer_views.get(lay.id),
                                              view, dpr, device_size=False)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            p.end()
        self._pending_sig = sig
        self._pending_view = view
        self._pending_epoch = epoch

    @staticmethod
    def _blit_view_image(p: QPainter, img, own, view, dpr: float,
                         device_size: bool) -> None:
        """把一张「按 `own` 视口渲出来的整视口图」画到 `view` 视口上。

        为什么不能直接 `drawImage(0, 0, img)`：视图一变（拖拽/滚轮），手上这张图
        就过期了，但新图还在另一个线程里渲。这时按 (0,0) 贴等于把**整个世界**
        拉回原处——用户看到的就是「松手后先跳回拖拽前的样子，过一会儿才归位」。
        过期不等于没用：只要知道它当初按哪个 tx/ty/scale 画的，就能算出它现在
        该画在哪儿：

            translate(tx, ty) · scale(s / s0) · translate(-tx0, -ty0)

        世界坐标里的点 w 在旧视口下是 `p0 = w·s0 + tx0`，代入上式得
        `tx + s/s0·(w·s0) = w·s + tx`，正好是它在**新**视口下的屏幕位置。
        所以纯平移是精确的整数位移，缩放是以视口为基准的精确等比变换；
        代价只有「旧图分辨率不够 / 边缘缺一条」，而位置始终正确。

        `own` / `view` 都是 `(scale, tx, ty, w, h[, ...])`：
        `device_size=True` 时 w/h 是设备像素（栅格管线），否则是逻辑像素。
        """
        if own is None:
            p.drawImage(0, 0, img)          # 不知道它属于哪个视口，只能原位贴
            return
        w_now = int(view[3] * dpr) if device_size else int(view[3])
        h_now = int(view[4] * dpr) if device_size else int(view[4])
        if own[3] != w_now or own[4] != h_now:
            # 窗口尺寸变了：图的构图都不一样，换算没有意义，原位贴上顶一帧
            p.drawImage(0, 0, img)
            return
        # 一律转 float：`own` 来自图层的渲染任务/缓存，可能带着 numpy.float64
        # （gdf.total_bounds 一路传进 fit_bounds）。numpy 标量参与比较会得到
        # numpy.bool_，而 QPainter.setRenderHint() 的第二个参数要求 bool/int，
        # 会抛 TypeError —— 那会中断整次合成，屏幕上的矢量跟着一起消失。
        s_now = float(view[0])
        s_own = float(own[0])
        r = (s_now / s_own) if s_own else 1.0
        if not math.isfinite(r) or r <= 0.0:
            p.drawImage(0, 0, img)
            return
        tx_now, ty_now = float(view[1]), float(view[2])
        tx_own, ty_own = float(own[1]), float(own[2])
        if abs(r - 1.0) <= 1e-9 and abs(tx_now - tx_own) < 0.5 \
                and abs(ty_now - ty_own) < 0.5:
            p.drawImage(0, 0, img)          # 视口没变（最常见），走最短路径
            return
        p.save()
        try:
            p.setRenderHint(QPainter.SmoothPixmapTransform, bool(abs(r - 1.0) > 1e-9))
            p.translate(tx_now, ty_now)
            if abs(r - 1.0) > 1e-9:
                p.scale(r, r)
            p.translate(-tx_own, -ty_own)
            p.drawImage(0, 0, img)
        finally:
            p.restore()

    def _on_frame_ready(self, sig, view, epoch: int) -> None:
        """整帧渲染完成：校验世代号后合成 final 帧。

        流式过程中 `_on_layer_progress` 已经贴过 provisional 帧（`_frame_sig=None`），
        这里再把所有图层图合成一次完整帧；`_frame_sig` 直到这一步才登记，
        所以缓存/拖拽不会把半成品当成完整帧。
        """
        if epoch != self._epoch:
            return                      # 过期世代，丢弃（否则旧帧会覆盖新帧）
        self._vec_busy = False          # 矢量侧这套图已经是完整的了
        # 在合成线程里把整帧叠好；UI 线程只接一次结果
        self._compose_async(sig, view, epoch, "force")

    def _device_pixel_ratio(self) -> float:
        return self.devicePixelRatioF() or 1.0

    def _point_dots_for(self, c: _LayerCache, lay, need: tuple,
                        visible_count: int | None = None) -> bool:
        """当前视口是否该把点降成 1px 点。

        不能用「图层总点数」一刀切：500 万点的图层放大到一条街时，
        视口里可能只有几十个点，应该保留 3.4px 的圆点。这里用
        「视口面积 / 图层范围面积」估算可见点数；内存流式图层已知
        可见下标数量，直接用它。
        """
        if os.environ.get("GIS_AGENT_POINT_DOTS", "1").strip().lower() in (
                "0", "false", "no", "off"):
            return False
        try:
            gt = str(getattr(lay, "geom_type", "") or "")
        except Exception:
            return False
        if "Point" not in gt:
            return False
        total = int(getattr(c, "total", 0) or 0)
        if total <= 0:
            return False
        if visible_count is not None:
            return int(visible_count) >= POINT_DOT_THRESHOLD
        if total < POINT_DOT_THRESHOLD:
            return False
        b = getattr(lay, "wgs_bounds", None) or getattr(c, "extent", None)
        if not b or len(b) != 4:
            return True
        area = max((float(b[2]) - float(b[0])) * (float(b[3]) - float(b[1])), 1e-12)
        need_area = max((float(need[2]) - float(need[0]))
                        * (float(need[3]) - float(need[1])), 0.0)
        frac = min(1.0, need_area / area)
        return total * frac >= POINT_DOT_THRESHOLD

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
            if c is None or c.bounds_pending or (c.bounds_failed and c.extent is None):
                continue
            style = getattr(lay, "style", None) or {}
            try:
                geom = str((lay.meta() or {}).get("geomType") or "")
            except Exception:  # noqa: BLE001
                geom = ""
            if c.stream and c.provider is not None:
                # 源文件图层：宽视野若已有「整层粗路径」缓存，就直接画缓存，
                # 不再按 bbox 读文件（省域整层读取是 5~8s 卡顿的主要来源）。
                lod = self._lod_set(c)
                if lod is not None:
                    layers.append(self._set_layer_entry(lay, lod, style, geom))
                    continue
                # 否则只传屏幕 bbox，渲染线程自己让 provider 读窗口。
                # 内存里没有整层 geoms/bounds，这是「provider 模式」的关键。
                need = self._view_need(c)
                if need is None:
                    continue
                layers.append({
                    "lid": lay.id,
                    "color": lay.color, "op": lay.op,
                    "style": style, "geom": geom,
                    "stream": True,
                    "provider": c.provider,
                    "bbox": self._pad_region(need, 1.02),
                    "tol": self._simplify_tol(),
                    "point_dots": self._point_dots_for(c, lay, need),
                })
                continue
            if c.stream:
                # 内存大图层：不传整层路径，只传「屏幕范围内」的要素下标。
                # 工作线程按块取几何、简化、建路径、立即画，路径不进缓存。
                if c.geoms is None or c.bounds is None:
                    continue
                need = self._view_need(c)
                if need is None:
                    continue
                idx = self._select_indices(c, self._pad_region(need, 1.02))
                layers.append({
                    "lid": lay.id,
                    "color": lay.color, "op": lay.op,
                    "style": style, "geom": geom,
                    "stream": True,
                    "geoms": c.geoms,
                    "indices": idx,
                    "tol": self._simplify_tol(),
                    "point_dots": self._point_dots_for(c, lay, need, len(idx)),
                })
                continue
            s = self._active_set(c)
            if s is None:
                continue
            if not (s.poly_paths or s.line_paths or s.points or s.dot_xy is not None
                    or s.dots is not None):
                continue
            layers.append(self._set_layer_entry(lay, s, style, geom))
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

    @staticmethod
    def _set_layer_entry(lay, s: _RenderSet, style: dict, geom: str) -> dict:
        """把一套渲染集（内存层的 / 源文件层的粗缓存）变成一个图层快照。"""
        return {
            "lid": lay.id,
            "color": lay.color, "op": lay.op,
            "style": style, "geom": geom,
            "poly_paths": s.poly_paths, "npoly": len(s.poly_paths),
            "poly_bounds": s.poly_bounds, "npoly_b": len(s.poly_bounds),
            "line_paths": s.line_paths, "nline": len(s.line_paths),
            "line_bounds": s.line_bounds, "nline_b": len(s.line_bounds),
            "points": s.points, "npt": len(s.points),
            "point_bounds": s.point_bounds, "npt_b": len(s.point_bounds),
            # 亚像素要素：传「取 QPolygonF 的方法」，由渲染线程首次用到时构建
            "dots": s.dots_polygon if (s.dot_xy is not None or s.dots is not None) else None,
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
                lay.color, lay.op, tier,
                style_signature(getattr(lay, "style", None)))

    # ------------------------------------------------------------------ 构建
    def _layers_render_order(self) -> list:
        """矢量图层的渲染顺序（自下而上）= 图层列表顺序。

        以前这里把 buffer / dissolve / grid 这类「背景算子」强制置底，以免
        一大块缓冲面盖住原图层；现在顺序由用户在图层面板里拖拽决定，
        背景算子改在 `Store.add()` 时插到最底下（默认效果不变），
        所以这里只需要照抄图层顺序，拖拽才不会有「拖了不生效」的层。
        """
        return list(self._vec_layers)

    def _build_pending(self) -> bool:
        """还有几何构建没做完吗（数据在解析、或者分片构建定时器还活着）。

        用来区分「还没准备好，先显示旧帧」和「确实没东西可画，该出空帧」。
        分片构建正常收尾时 `_build_tick` 会停掉定时器，所以定时器不活跃
        就等于构建已经结束。
        """
        return bool(self._loading or self._build_timer.isActive())

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
            if c.build is not None:
                return True
            if c.provider is not None:
                continue          # provider 图层不建全量包围盒，渲染时按 bbox 读
            if c.bounds is None:
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
        if c.stream:
            return False          # 流式图层不建渲染集，每次按视口现画
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
            if c.provider is not None:
                continue          # 源文件图层：不预读全量几何，渲染时走 bbox
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
                self._start_render(request_raster=False)
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
            # 平移只是位图偏移，露出的空白边够宽了才重绘，保证拖动跟手
            if self._pan_gap_too_wide():
                self._invalidate_frame()
                self._kick_build()
            self.update()
            return

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._band is not None and e.button() == Qt.LeftButton:
            band, self._band = self._band, None
            self.setCursor(Qt.ArrowCursor)
            if abs(band[2] - band[0]) >= 6 and abs(band[3] - band[1]) >= 6:
                self._select_rect(band)
            self.update()
            return
        if e.button() == Qt.MiddleButton and self._drag_from is not None:
            # 中键拖拽结束也要收尾：以前这里没有分支，`_drag_from` 一直留着，
            # 松手后鼠标一动地图还跟着跑，光标也卡在「抓手」上。
            self._drag_from = None
            self.setCursor(Qt.ArrowCursor)
            self._end_pan()
            self.update()
            return
        if e.button() == Qt.LeftButton and self._drag_from is not None:
            self._drag_from = None
            self.setCursor(Qt.ArrowCursor)
            self._end_pan()
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

    def set_highlight_set(self, layer_id: str, indices, color: str = "#d97757") -> None:
        """高亮一批要素（highlight 算子的结果）。

        只记下标，几何在绘制时现取 —— 免得复制一份几何占内存。
        """
        if indices is None:
            self._hl_set.pop(layer_id, None)
        else:
            idx = np.asarray(list(indices), dtype=np.int64)
            self._hl_set[layer_id] = (idx, color or "#d97757")
        self.update()

    def clear_highlight_set(self, layer_id: str | None = None) -> None:
        """清掉算子高亮；不给 layer_id 就全清。"""
        if layer_id is None:
            self._hl_set.clear()
        else:
            self._hl_set.pop(layer_id, None)
        self.update()

    def highlight_set_count(self) -> int:
        """当前高亮的要素总数（给状态栏显示）。"""
        return int(sum(len(v[0]) for v in self._hl_set.values()))

    def zoom_to_feature(self, layer_id: str, pos_idx: int, pad: float = 0.35) -> None:
        lay = next((l for l in self.layers if l.id == layer_id), None)
        c = self._cache.get(layer_id)
        if lay is None:
            return
        self._ensure_index(lay, c)
        b = None
        if c is not None and c.bounds is not None and pos_idx < len(c.bounds):
            b = c.bounds[pos_idx]
        elif c is not None and c.provider is not None:
            # provider 图层没有全量 bounds 索引：只读这一行的几何取范围。
            try:
                geom = c.provider.feature_geom(pos_idx)
                if geom is not None and not geom.is_empty:
                    b = geom.bounds
            except Exception:
                b = None
        if b is None:
            return
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
        """按需生成全量几何索引（包围盒 + 几何数组），只在第一次点击时算一次。

        provider 图层**不能**走这里：`_ensure_geoms()` 会 `lay.gdf` 全量加载，
        21k 块 / 4480 万顶点的图层会直接涨到数 GB（点击一次就触发）。
        流式图层的命中由 `provider.hit_test()` 只读小 bbox / 属性完成。
        """
        if c is None:
            return False
        if c.bounds is not None:
            return True
        if c.provider is not None and c.geoms is None:
            # 小图层（属性表/算子已经显式读过 gdf）可以照旧建索引；
            # 只有 gdf 还没进内存的源文件大图层才必须挡住全量加载。
            if getattr(lay, "_gdf", None) is None:
                return False
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
            if c is not None and c.extent is not None:
                e = c.extent
                if lon < e[0] or lon > e[2] or lat < e[1] or lat > e[3]:
                    continue        # 点在图层范围外，不必让 provider 扫文件
            if (c is not None and c.provider is not None
                    and c.geoms is None):
                # 大图层不要为了点击拾取把整层读进内存；provider 用简化
                # 缓存命中几何，再只读属性列。
                try:
                    hit = c.provider.hit_test(lon, lat, tol)
                except Exception:
                    hit = None
                if hit is not None:
                    return lay, int(hit[0]), hit[1]
                continue
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
                       "拖入 Shapefile / GeoJSON / GeoTIFF，或点左栏的导入区")
        else:
            # 图层已载入但几何还在构建：给出明确提示，而不是一片空白
            p.setPen(QColor("#8d897e"))
            p.setFont(QFont("Microsoft YaHei UI", 10))
            p.drawText(rect, Qt.AlignCenter, "正在准备渲染数据…")

        # 经纬网画在帧缓存**之上**，这样缩放平移时网格始终对齐屏幕；
        # 标注再压在网格之上，免得文字被网格线穿过。
        self._draw_grid(p)
        # 要素标注跟着当前视口实时画（不烘焙进帧缓存）：
        # 平移/缩放时文字位置立刻正确，不用等后台重渲染。
        self._paint_labels(p)
        self._draw_selection(p)
        self._draw_highlight_set(p)
        self._draw_highlight(p)
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
        geom = None
        if c is not None:
            if c.geoms is not None and idx < len(c.geoms):
                geom = c.geoms[idx]
            elif c.provider is not None:
                # provider 图层没有整层 geoms；只解码当前行，不能为了高亮建索引。
                try:
                    geom = c.provider.feature_geom(idx)
                except Exception:
                    geom = None
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

    def _draw_highlight_set(self, p: QPainter) -> None:
        """算子高亮：一批要素用强调色描边 + 淡填充画出来。

        和 _draw_highlight（属性表点行的单个要素）分开：
        那个更重（双圈），这个是「一批命中要素」的轻量样式。
        """
        if not self._hl_set:
            return
        for lid, (idx, color) in self._hl_set.items():
            if len(idx) == 0:
                continue
            c = self._cache.get(lid)
            if c is None or c.geoms is None:
                continue
            n = len(c.geoms)
            sel = idx[(idx >= 0) & (idx < n)]
            if len(sel) == 0:
                continue
            base = QColor(color or "#d97757")
            fill = QColor(base)
            fill.setAlpha(70)
            pen_color = QColor(base)
            pen_color.setAlpha(235)

            p.save()
            p.translate(self.tx, self.ty)
            p.scale(self.scale, self.scale)
            # cosmetic：画笔在世界坐标变换下，非 cosmetic 的宽度会
            # 随缩放变成上千万像素，把视口糊成一块实色。
            pen = QPen(pen_color)
            pen.setWidthF(2.2)
            pen.setCosmetic(True)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(QBrush(fill))
            point_pts = []
            for i in sel:
                geom = c.geoms[int(i)]
                if geom is None or geom.is_empty:
                    continue
                path = self._geom_path(geom)
                if path is None:          # 点要素：留到屏幕坐标下画
                    point_pts.append(geom)
                    continue
                p.drawPath(path)
            p.restore()
            # 点要素必须在屏幕坐标下画（不能套世界坐标变换）
            if point_pts:
                p.save()
                p.setBrush(QBrush(fill))
                p.setPen(QPen(pen_color, 1))
                for geom in point_pts:
                    pt = self.lonlat_to_screen(geom.x, geom.y)
                    p.drawEllipse(pt, 4.5, 4.5)
                p.restore()

    def _draw_band(self, p: QPainter) -> None:
        if self._band is None:
            return
        x0, y0, x1, y1 = self._band
        r = QRectF(QPointF(min(x0, x1), min(y0, y1)), QPointF(max(x0, x1), max(y0, y1)))
        p.setPen(QPen(QColor(201, 100, 66, 210), 1, Qt.DashLine))
        p.setBrush(QBrush(QColor(217, 119, 87, 30)))
        p.drawRect(r)

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

    # ------------------------------------------------------------- 要素标注
    def _layer_paint_style(self, lay) -> dict:
        """这个图层当前生效的矢量样式（含默认值）。"""
        try:
            geom = str((lay.meta() or {}).get("geomType") or "")
        except Exception:  # noqa: BLE001
            geom = ""
        return resolve_vector_style(
            getattr(lay, "op", "") or "", getattr(lay, "color", None) or "#2f6fed",
            getattr(lay, "style", None), geom)

    def _label_sig(self, lay) -> tuple:
        c = self._cache.get(lay.id)
        return (id(lay), getattr(c, "rev", 0) if c is not None else 0)

    def _label_cache_stale(self, lay, field: str, view: tuple) -> bool:
        if not field:
            return False
        cache = self._labels.get(lay.id)
        if cache is None or cache.get("field") != field:
            return True
        if cache.get("sig") != self._label_sig(lay):
            return True
        rect = cache.get("rect") or view
        if not _rect_contains(rect, view):
            return True
        # 缓存是「视野外扩 40%」取的。放大到缓存范围的 1/1.6 以下时，
        # 之前按大范围均匀抽的锚点在新视野里只剩寥寥几个——必须按新视野重取，
        # 否则就是「放大地图后标注几乎消失」。
        cw, ch = rect[2] - rect[0], rect[3] - rect[1]
        vw, vh = view[2] - view[0], view[3] - view[1]
        if vw <= 0 or vh <= 0:
            return True
        return cw > vw * 1.6 or ch > vh * 1.6

    def _label_table_ok(self, lay, field: str) -> bool:
        """这层是否已经有可用的「整层锚点表」（且字段一致）。"""
        a = self._labels_anchors.get(lay.id)
        return bool(a and a.get("field") == field)

    def _label_table_eligible(self, lay) -> bool:
        """这层值不值得建整层锚点表：按视口取会顺扫整个文件的源文件图层。

        · GeoParquet / GeoJSON、没有 `.qix` 的 Shapefile：按 bbox 取也要把
          整个文件解一遍（村界面 614MB 实测 4.5s/次），整层读一次更划算；
        · GPKG / FileGDB / FlatGeobuf / 有 `.qix` 的 Shapefile：按视口读本来
          就是毫秒级，别为标注把整层物化出来。
        """
        c = self._cache.get(lay.id)
        if c is None or c.provider is None or not c.stream:
            return False
        if getattr(lay, "_gdf", None) is not None:
            return False
        try:
            if int(c.total or 0) > LABEL_TABLE_MAX:
                return False
        except Exception:  # noqa: BLE001
            return False
        try:
            path = str(getattr(c.provider, "path", "") or "").lower()
        except Exception:  # noqa: BLE001
            return False
        if path.endswith((".parquet", ".geojson", ".json")):
            return True
        if path.endswith(".shp"):
            import os
            return not os.path.exists(path[:-4] + ".qix")
        return False

    def _derive_labels(self, lay, field: str, view: tuple):
        """用整层锚点表生成当前视口的标注缓存：纯 numpy 筛选，不读磁盘。

        锚点在 Web Mercator 世界坐标里，所以视口只要换算成世界矩形就能做掩码；
        拖动/缩放时每帧现算（2 万个锚点约 0.2ms），标注不会再落后于地图。
        """
        a = self._labels_anchors.get(lay.id)
        if not a or a.get("field") != field:
            return None
        pad = self._pad_region(view, 1.4)
        xy = a.get("xy")
        texts_all = a.get("texts") or []
        angles_all = a.get("angles")
        if xy is None or len(xy) == 0 or not texts_all:
            cache = {"field": field, "rect": pad, "sig": self._label_sig(lay),
                     "xy": None, "texts": [], "angles": None}
            self._labels[lay.id] = cache
            return None
        try:
            wx0, wy0 = project(float(view[0]), float(view[3]))
            wx1, wy1 = project(float(view[2]), float(view[1]))
            if wx0 > wx1:
                wx0, wx1 = wx1, wx0
            if wy0 > wy1:
                wy0, wy1 = wy1, wy0
            mask = ((xy[:, 0] >= wx0) & (xy[:, 0] <= wx1)
                    & (xy[:, 1] >= wy0) & (xy[:, 1] <= wy1))
        except Exception:  # noqa: BLE001
            mask = np.ones(len(xy), dtype=bool)
        sel = np.nonzero(mask)[0]
        if len(sel) == 0:
            cache = {"field": field, "rect": pad, "sig": self._label_sig(lay),
                     "xy": None, "texts": [], "angles": None}
            self._labels[lay.id] = cache
            return None
        if len(sel) > LABEL_CAP:
            sel = sel[_grid_pick(xy[sel, 0], xy[sel, 1], pad, LABEL_CAP)]
        cache = {
            "field": field, "rect": pad, "sig": self._label_sig(lay),
            "xy": np.asarray(xy[sel], dtype=float),
            "texts": [texts_all[int(i)] for i in sel],
            "angles": (np.asarray(angles_all, dtype=float)[sel]
                       if angles_all is not None
                       and len(angles_all) == len(xy) else None),
        }
        self._labels[lay.id] = cache
        return cache

    def _ensure_labels(self, lay, st: dict, view: tuple):
        """返回可画的标注缓存；过期就登记重建（防抖，不在 paint 里读数据）。"""
        field = st.get("label_field") or ""
        if not field:
            return None
        c = self._cache.get(lay.id)
        if c is not None and (c.bounds_pending or c.bounds_failed
                              or self._view_need(c) is None):
            return None          # 视口外/范围未知时不派发标注任务
        if self._label_table_ok(lay, field):
            # 整层锚点表已就绪：每帧现筛，拖动时标注跟着地图走，不读盘也不排队。
            return self._derive_labels(lay, field, view)
        cache = self._labels.get(lay.id)
        if lay.id in self._label_tables:
            # 整层表正在后台建（村界面约 4.5s，只付一次）；先画旧标注。
            return cache if cache is not None and cache.get("field") == field else None
        if self._vec_busy:
            # 渲染线程正在读同一个源文件（宽视野下那是一次 3s 级别的整层读）。
            # 标注也去读一遍只会让两边都变慢，先显示旧标注，等这一帧画完再重建。
            if cache is not None and cache.get("field") == field:
                return cache
            return None
        if self._label_cache_stale(lay, field, view):
            self._labels_pending.add(lay.id)
            if not self._label_timer.isActive():
                self._label_timer.start()
        if cache is not None and cache.get("field") == field:
            return cache
        return None

    def _rebuild_labels(self) -> None:
        """把过期图层的标注任务交给标注线程（防抖定时器回调，UI 线程只做派发）。"""
        pending, self._labels_pending = self._labels_pending, set()
        if not pending:
            return
        view = self._view_lonlat_rect()
        # 缓存范围比视口大一圈：小幅平移不用重新取数
        rect = self._pad_region(view, 1.4)
        by_id = {l.id: l for l in self._layers_render_order()}
        for lid in pending:
            lay = by_id.get(lid)
            if lay is None:
                continue
            c = self._cache.get(lid)
            if c is not None and (c.bounds_pending or c.bounds_failed
                                  or self._view_need(c) is None):
                continue
            st = self._layer_paint_style(lay)
            field = st.get("label_field") or ""
            if not field:
                continue
            if self._label_table_ok(lay, field):
                self._derive_labels(lay, field, view)
                continue
            if self._label_table_eligible(lay):
                if lid in self._label_tables:
                    continue        # 整层表正在建，别拿视口任务把它顶掉
                gen = self._labels_gen.get(lid, 0) + 1
                self._labels_gen[lid] = gen
                self._label_tables[lid] = gen
                self._label_worker.submit(
                    _LabelJob(lid, lay, field, None, gen, kind="table"))
                continue
            # 内存图层若被换出：先把几何读回主线程，别让标注线程去和渲染争同一个懒加载
            if getattr(lay, "_gdf", None) is None and getattr(lay, "provider", None) is None:
                try:
                    lay.gdf
                except Exception:  # noqa: BLE001
                    pass
            gen = self._labels_gen.get(lid, 0) + 1
            self._labels_gen[lid] = gen
            self._label_worker.submit(_LabelJob(lid, lay, field, rect, gen))

    def _on_labels_ready(self, lid: str, gen: int, kind: str, payload) -> None:
        """标注线程交回结果：世代/字段对不上就丢弃，否则缓存并重绘。"""
        if self._label_tables.get(lid) == gen:
            self._label_tables.pop(lid, None)
        if gen != self._labels_gen.get(lid, 0):
            return
        field, rect, xy, texts, angles = payload
        by_id = {l.id: l for l in self._layers_render_order()}
        lay = by_id.get(lid)
        if lay is None:
            return
        if (self._layer_paint_style(lay).get("label_field") or "") != field:
            return
        if kind == "table" and xy is not None:
            # 整层锚点表：之后所有视口都从它派生，不再触发磁盘读
            self._labels_anchors[lid] = {"field": field, "xy": xy,
                                         "texts": texts, "angles": angles,
                                         "rect": rect}
            self._labels.pop(lid, None)
            self._derive_labels(lay, field, self._view_lonlat_rect())
            self.update()
            return
        if kind == "table":
            # 表建失败（字段读不到/要素太多/被取消）：退回按视口取
            self._labels_anchors.pop(lid, None)
            self._labels_pending.add(lid)
            if not self._label_timer.isActive():
                self._label_timer.start()
            return
        self._labels[lid] = {"field": field, "rect": rect,
                             "sig": self._label_sig(lay),
                             "xy": xy, "texts": texts, "angles": angles}
        self.update()


    def _paint_labels(self, p: QPainter) -> None:
        """在帧缓存之上画要素标注（屏幕坐标，带简单避让 + 白色描边）。"""
        if self._frame is None or not self._vec_layers:
            return
        view = self._view_lonlat_rect()
        for lay in self._layers_render_order():
            if not self.visible.get(lay.id, True):
                continue
            st = self._layer_paint_style(lay)
            cache = self._ensure_labels(lay, st, view)
            if not cache:
                continue
            self._draw_label_set(p, cache, st)

    def _draw_label_set(self, p: QPainter, cache: dict, st: dict) -> None:
        xy = cache.get("xy")
        texts = cache.get("texts") or []
        if xy is None or len(texts) == 0:
            return
        try:
            pts = xy.tolist()
        except Exception:  # noqa: BLE001
            pts = list(xy)
        if not pts:
            return
        angles = cache.get("angles")
        if angles is None or len(angles) != len(pts):
            angles = [0.0] * len(pts)
        size = max(8, int(st.get("label_size") or 11))
        # 不用 "Microsoft YaHei UI"：某些精简字体环境/离屏渲染下该族名取不到字形，
        # 标注会整片消失。"Microsoft YaHei" 在 Windows 上一定有。
        font = QFont("Microsoft YaHei")
        font.setPixelSize(size)
        fm = QFontMetricsF(font)
        # 文本宽度只跟字号有关：建缓存时算一次，平移/缩放每帧不用再量一遍
        widths = cache.get("widths")
        if widths is None or cache.get("width_size") != size or len(widths) != len(texts):
            widths = [fm.horizontalAdvance(t) if t else 0.0 for t in texts]
            cache["widths"] = widths
            cache["width_size"] = size
        th = fm.height()
        ascent = fm.ascent()
        color = QColor(st.get("label_color") or "#2b2a26")
        halo = bool(st.get("label_halo", True))
        p.save()
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setFont(font)
        # 简单避让：按 120×26 像素的格子分桶，只和相邻 9 格里的框比较
        cell_w, cell_h = 120, 26
        placed: dict[tuple[int, int], list[QRectF]] = {}
        # 同名道路由很多短线段组成，不去重会把「机场路」在同一个路口标四遍。
        # QGIS 用「合并相连线」解决，这里退一步：同名文本在屏幕上相距太近就不再画。
        same_text_gap = 180.0
        text_spots: dict[str, list[tuple[float, float]]] = {}
        drawn = 0
        w, h = float(self.width()), float(self.height())
        for pos, text, tw, ang in zip(pts, texts, widths, angles):
            if not text:
                continue
            sx = float(pos[0]) * self.scale + self.tx
            sy = float(pos[1]) * self.scale + self.ty
            if sx < -40 or sy < -40 or sx > w + 40 or sy > h + 40:
                continue
            spots = text_spots.get(text)
            if spots is not None:
                too_close = False
                for px, py in spots:
                    if abs(px - sx) < same_text_gap and abs(py - sy) < same_text_gap:
                        too_close = True
                        break
                if too_close:
                    continue
            ang = float(ang)
            if not math.isfinite(ang):
                ang = 0.0
            # 碰撞用旋转后文字的外接矩形（轴对齐）：斜路上的字实际占的是斜条，
            # 用未旋转的宽度会和相邻标注误碰，用外接矩形则偏保守一点，和 QGIS
            # 的「旋转几何求交」是同量级的近似。
            if ang:
                rad = math.radians(ang)
                ca, sa = abs(math.cos(rad)), abs(math.sin(rad))
                bw = tw * ca + th * sa
                bh = tw * sa + th * ca
            else:
                bw, bh = tw, th
            box = QRectF(sx - bw / 2, sy - bh / 2, bw, bh)
            # 标注不能画到视口外：QGIS 也是整块放不下就不放，避免边上出现半截字
            if (box.left() < -2 or box.top() < -2
                    or box.right() > w + 2 or box.bottom() > h + 2):
                continue
            gi, gj = int(sx // cell_w), int(sy // cell_h)
            near: list[QRectF] = []
            for i in (gi - 1, gi, gi + 1):
                for j in (gj - 1, gj, gj + 1):
                    near.extend(placed.get((i, j), ()))
            if any(box.intersects(r) for r in near):
                continue
            placed.setdefault((gi, gj), []).append(box)
            if spots is None:
                text_spots[text] = [(sx, sy)]
            else:
                spots.append((sx, sy))
            p.save()
            p.translate(sx, sy)
            if ang:
                p.rotate(ang)      # 正角 = 顺时针，和屏幕 atan2 的方向一致
            bx = -tw / 2.0
            by = -th / 2.0 + ascent
            if halo:
                # 描边用「四个方向偏移画一遍」：比 QPainterPath 轮廓更快，
                # 也不依赖字体轮廓（offscreen/精简字体环境下轮廓可能取不到）。
                off = max(1.0, size / 12.0)
                p.setPen(QColor(255, 255, 255, 235))
                for dx, dy in ((-off, 0.0), (off, 0.0), (0.0, -off), (0.0, off)):
                    p.drawText(QPointF(bx + dx, by + dy), text)
            p.setPen(color)
            p.drawText(QPointF(bx, by), text)
            p.restore()
            drawn += 1
            if drawn >= LABEL_CAP:
                break
        p.restore()
