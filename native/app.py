"""主窗口：三栏工作台（图层 / 地图 / 对话），纯 Qt 控件，无 Web 引擎。"""
from __future__ import annotations

import shutil
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QIcon, QKeySequence, QPainter, QPen, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QToolTip, QVBoxLayout, QWidget)

from app import custom_ops
from app.agent import Planner, run_plan
from app.config import explain, is_ready, load_cfg, save_cfg
from app.engine import Store
from app.io_utils import export_layer as write_layer_file
from app.io_utils import extract_zip, file_info, load_any
from app.samples import ensure_samples

from .chat import ChatPanel, Worker
from .dialogs import ImportDialog, OperatorDialog, SettingsDialog, TableDialog
from .format import fmt_num
from .mapview import MapView
from .theme import QSS

FILE_FILTER = ("矢量数据 (*.zip *.shp *.geojson *.json *.gpkg *.kml *.gml *.csv);;"
               "Shapefile 压缩包 (*.zip);;GeoJSON (*.geojson *.json);;"
               "GeoPackage (*.gpkg);;CSV (*.csv);;所有文件 (*.*)")

TOOL_TEMPLATES = {
    "describe": "检查「{L}」的数据质量",
    "buffer": "对「{L}」做 500 米缓冲区",
    "clip": "用「{O}」裁剪「{L}」",
    "intersect": "「{L}」与「{O}」求交",
    "union": "「{L}」与「{O}」叠加合并",
    "erase": "从「{L}」中擦除「{O}」范围",
    "dissolve": "按字段融合「{L}」",
    "reproject": "把「{L}」投影到 EPSG:4547",
    "simplify": "简化「{L}」容差 10 米",
    "centroid": "取「{L}」的质心",
    "convex_hull": "生成「{L}」的凸包",
    "voronoi": "对「{L}」生成泰森多边形",
    "grid": "在「{L}」范围生成 1 公里渔网",
    "sjoin": "把「{O}」的属性空间连接到「{L}」",
    "nearest": "为「{L}」查找最近的「{O}」",
    "filter": "筛选「{L}」中容积率 > 3 的要素",
    "field_calc": "在「{L}」新增字段 密度 = 人口 / 面积",
    "area_stats": "按类型统计「{L}」的面积",
    "merge": "合并所有图层",
    "dedupe": "对「{L}」去重",
    "fix_geometry": "修复「{L}」的无效几何",
    "explode": "拆分「{L}」的多部件",
    "bbox_select": "框选「{L}」的一部分",
}


# --------------------------------------------------------------------------- 图标
# 图标语言：陶土橙圆角底 + 一个「面要素」轮廓 + 三个顶点句柄。
# 一眼能读出「矢量面 + 可编辑节点」，也和小尺寸下最稳（没有细线、没有多层嵌套）。
_ICON_BG = "#d97757"
_ICON_SHAPE = [(15, 24), (29, 16), (50, 21), (47, 42), (27, 50), (13, 41)]


def _icon_pixmap(size: int) -> QPixmap:
    """按 64×64 的设计稿等比画到指定尺寸。

    小尺寸要「减笔画」：16/20 px 下双层轮廓 + 句柄会糊成一团，
    那里只留一个实心面；24~40 画轮廓 + 句柄；再大才加后层叠置。
    """
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPolygonF

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 64.0, size / 64.0)

    p.setPen(Qt.NoPen)
    p.setBrush(QColor(_ICON_BG))
    p.drawRoundedRect(1.5, 1.5, 61.0, 61.0, 15.0, 15.0)

    # 主体整体左移 1.5、下移 1.5，让右上角的「后层」有安全边距
    dx0, dy0 = -1.5, 1.5
    pts = [(x + dx0, y + dy0) for x, y in _ICON_SHAPE]
    shape = QPolygonF([QPointF(x, y) for x, y in pts])

    detail = 0 if size <= 24 else (1 if size <= 40 else 2)
    if detail == 0:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawPolygon(shape)
    else:
        if detail == 2:
            pen = QPen(QColor(255, 255, 255, 84))
            pen.setWidthF(2.0)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(QPolygonF([QPointF(x + 3.4, y - 4.2) for x, y in pts]))

        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(3.0 if detail == 1 else 2.6)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(QColor(255, 255, 255, 52) if detail == 2 else Qt.NoBrush)
        p.drawPolygon(shape)

        # 句柄必须落在顶点上（用同一份 pts，别再手算偏移）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        for x, y in (pts[1], pts[3], pts[5]):
            p.drawRoundedRect(x - 2.8, y - 2.8, 5.6, 5.6, 1.5, 1.5)
    p.end()
    return pm


# 算子库的分组（只影响展示顺序；不在这里的算子会落到「其它 / 我的算子」）
TOOL_GROUPS = [
    ("概览", ["describe"]),
    ("几何构造", ["buffer", "centroid", "convex_hull", "min_rect", "voronoi", "grid"]),
    ("叠加分析", ["clip", "intersect", "union", "erase"]),
    ("邻近与连接", ["sjoin", "nearest", "bbox_select"]),
    ("属性与统计", ["filter", "field_calc", "area_stats", "shape_metrics"]),
    ("整理", ["dissolve", "merge", "dedupe", "simplify", "reproject", "fix_geometry", "explode"]),
]
TOOL_SPEC_BY_NAME: dict[str, str] = {}


def make_icon() -> QIcon:
    """多尺寸应用图标（任务栏 16/24、窗口 32/48、关于页 128 都清晰）。"""
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(_icon_pixmap(s))
    return ic



class ElideLabel(QLabel):
    """单行、按宽度自动省略的标签。

    图层名一长（`gis_osm_landuse_a_free_1_修复`）就会被行宽硬裁成
    `gis_osm_landuse_a_free_1`，和原图层**看起来一模一样**，用户根本分不清哪行是派生层。
    这里用省略号代替硬裁，并且从中间省（文件名/图层名的区分度常在尾部后缀）。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(24)
        self.setToolTip(text)
        self._apply()

    def setText(self, text: str) -> None:      # noqa: N802 (Qt 命名)
        self._full = text
        self.setToolTip(text)
        self._apply()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, e) -> None:          # noqa: N802
        super().resizeEvent(e)
        self._apply()

    def _apply(self) -> None:
        fm = self.fontMetrics()
        avail = max(24, self.width() - 2)
        super().setText(fm.elidedText(self._full, Qt.ElideMiddle, avail))


class LayerRow(QWidget):
    """图层列表里的一行：色块 + 名称 + 元信息 + 显隐按钮。"""

    def __init__(self, layer, visible: bool, on_toggle, parent=None):
        super().__init__(parent)
        self.layer = layer
        vl = QVBoxLayout(self)
        vl.setContentsMargins(9, 6, 6, 6)
        vl.setSpacing(1)

        top = QHBoxLayout()
        top.setSpacing(7)
        sw = QLabel()
        sw.setFixedSize(9, 9)
        sw.setStyleSheet(f"background:{layer.color};border-radius:2px;")
        top.addWidget(sw, 0, Qt.AlignTop)
        self.name = ElideLabel(layer.name)
        self.name.setStyleSheet("font-size:12px;font-weight:600;")
        top.addWidget(self.name, 1)

        self.eye = QCheckBox()
        self.eye.setChecked(visible)
        self.eye.setFixedSize(16, 16)
        self.eye.setCursor(Qt.PointingHandCursor)
        self.eye.setToolTip("显示 / 隐藏该图层")
        self.eye.setStyleSheet("QCheckBox::indicator{width:13px;height:13px;}")
        self.eye.toggled.connect(lambda checked: on_toggle(layer.id, checked))
        top.addWidget(self.eye, 0, Qt.AlignTop)
        vl.addLayout(top)

        meta = layer.meta()
        sub = QLabel(f"{fmt_num(meta['count'])} 要素 · {meta['geomType']} · {meta['crs']}")
        sub.setObjectName("muted")
        vl.addWidget(sub)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GIS 矢量数据 Agent")
        self.setWindowIcon(make_icon())
        self.resize(1520, 920)
        self.setMinimumSize(1080, 640)
        self.setAcceptDrops(True)

        self.store = Store()
        self.planner = Planner(self.store)
        self.current_layer: str | None = None
        self.visible: dict[str, bool] = {}
        self.history: list[dict] = []
        self._undo_stack: list[dict] = []      # 只记「加图层 / 删图层」这两类，够用且不会误伤
        self._redo_stack: list[dict] = []
        self._worker: Worker | None = None
        self._busy = False
        # 任务执行时的秒表：几十秒的算子不至于让界面看起来「卡死」
        self._exec_t0 = 0.0
        self._cursor_pushed = False
        self._exec_timer = QTimer(self)
        self._exec_timer.setInterval(1000)
        self._exec_timer.timeout.connect(self._tick_exec)
        self._tmp_root: Path | None = None   # 压缩包解压出的临时目录，导入完就删
        self._cleanup_worker: Worker | None = None
        self._load_queue: list[Path] = []    # 待载入的文件队列（串行处理）

        self.setStyleSheet(QSS)
        self._build()
        self._shortcuts()
        self._refresh_planner_pill()
        self._greet()
        # 把 data/operators/ 里存过的自定义算子重新装回运行时
        try:
            installed = custom_ops.install_all()
            if installed:
                self.chat.add_agent("已载入 " + str(len(installed)) + " 个自定义算子："
                                    + "、".join("「" + s["title"] + "」" for s in installed))
        except Exception:
            pass
        QTimer.singleShot(120, self._refresh_tools)

    def closeEvent(self, e):  # noqa: N802
        """关窗时先停掉地图的渲染线程。

        地图控件内部有一个常驻渲染线程（QThread）。窗口关闭时如果它还在跑，
        QThread 被销毁会导致进程崩溃（"QThread: Destroyed while thread is
        still running"）——所以必须在这里显式停下来并等待退出。
        """
        try:
            self.map.shutdown()
        except Exception:
            pass
        try:
            if self._worker is not None and self._worker.isRunning():
                self._worker.wait(2000)
        except Exception:
            pass
        super().closeEvent(e)

    # ================================================================== 布局
    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_topbar())

        body = QWidget()
        bl = QHBoxLayout(body)
        bl.setContentsMargins(10, 10, 10, 10)
        bl.setSpacing(10)
        bl.addWidget(self._build_left())
        bl.addWidget(self._build_map(), 1)
        bl.addWidget(self._build_right())
        root.addWidget(body, 1)

        self._build_statusbar()

    # ---------------------------------------------------------------- 顶栏
    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(58)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(14, 0, 14, 0)
        hl.setSpacing(10)

        logo = QLabel()
        logo.setPixmap(make_icon().pixmap(34, 34))
        logo.setFixedSize(34, 34)
        hl.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        t1 = QLabel("GIS 矢量数据 Agent")
        t1.setObjectName("brandTitle")
        t2 = QLabel("说一句话，完成空间分析")
        t2.setObjectName("brandSub")
        titles.addWidget(t1)
        titles.addWidget(t2)
        hl.addLayout(titles)
        hl.addStretch(1)

        self.pill = QLabel("● 规则引擎")
        self.pill.setObjectName("pill")
        # QLabel 默认会被布局纵向拉满（顶栏 58px 高，它就变成 58px 高的一块），
        # 这里固定成和右边按钮一样的高度，并在下面按按钮实测高度对齐
        self.pill.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.pill.setAlignment(Qt.AlignCenter)
        hl.addWidget(self.pill, 0, Qt.AlignVCenter)

        self.btn_samples = QPushButton("↓ 示例数据")
        self.btn_samples.setObjectName("ghost")
        self.btn_samples.clicked.connect(self.load_samples)
        hl.addWidget(self.btn_samples)

        self.btn_settings = QPushButton("⚙ 模型设置")
        self.btn_settings.setObjectName("ghost")
        self.btn_settings.clicked.connect(self.open_settings)
        hl.addWidget(self.btn_settings)

        self.btn_upload = QPushButton("↑ 上传矢量数据")
        self.btn_upload.setObjectName("primary")
        self.btn_upload.clicked.connect(self.choose_files)
        hl.addWidget(self.btn_upload)
        # 状态胶囊和右侧按钮同高（按钮高度由主题里的 padding 决定，这里量出来对齐）
        self.pill.setFixedHeight(self.btn_samples.sizeHint().height())
        return bar

    # ---------------------------------------------------------------- 左栏
    def _build_left(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setFixedWidth(272)
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(14, 12, 12, 6)
        title = QLabel("数据图层")
        title.setObjectName("panelTitle")
        head.addWidget(title)
        self.count_badge = QLabel("0")
        self.count_badge.setObjectName("badge")
        head.addWidget(self.count_badge)
        head.addStretch(1)
        vl.addLayout(head)

        dz = QFrame()
        dz.setObjectName("dropzone")
        dz.setFixedHeight(84)
        dz.setCursor(Qt.PointingHandCursor)
        dzl = QVBoxLayout(dz)
        dzl.setContentsMargins(8, 10, 8, 10)
        dzl.setSpacing(2)
        d1 = QLabel("↑")
        d1.setAlignment(Qt.AlignCenter)
        d1.setStyleSheet("font-size:15px;color:#c0b9a7;")
        d2 = QLabel("拖入文件或点击上传")
        d2.setAlignment(Qt.AlignCenter)
        d2.setStyleSheet("font-size:12px;color:#43413c;font-weight:600;")
        d3 = QLabel("shp(zip) · GeoJSON · GPKG · KML · CSV")
        d3.setAlignment(Qt.AlignCenter)
        d3.setObjectName("tiny")
        for w in (d1, d2, d3):
            dzl.addWidget(w)
        dz.mousePressEvent = lambda e: self.choose_files()  # type: ignore[assignment]
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(12, 0, 12, 10)
        wl.addWidget(dz)
        vl.addWidget(wrap)

        self.layer_list = QListWidget()
        self.layer_list.setObjectName("layerList")
        self.layer_list.setSpacing(0)
        self.layer_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.layer_list.customContextMenuRequested.connect(self._layer_menu)
        self.layer_list.itemDoubleClicked.connect(
            lambda item: self.rename_layer(item.data(Qt.UserRole)))
        self.layer_list.currentItemChanged.connect(self._on_layer_selected)
        vl.addWidget(self.layer_list, 2)

        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet("background:#e7e3d8;")
        vl.addWidget(div)

        head2 = QHBoxLayout()
        head2.setContentsMargins(14, 10, 12, 6)
        t2 = QLabel("算子库")
        t2.setObjectName("panelTitle")
        head2.addWidget(t2)
        self.tool_badge = QLabel("22")
        self.tool_badge.setObjectName("badge")
        head2.addWidget(self.tool_badge)
        head2.addStretch(1)
        self.new_op_btn = QPushButton("＋ 新算子")
        self.new_op_btn.setObjectName("chip")
        self.new_op_btn.setCursor(Qt.PointingHandCursor)
        self.new_op_btn.setToolTip("用一段文字描述算法，让大模型生成一个新算子并加入算子库")
        self.new_op_btn.clicked.connect(self.prompt_new_operator)
        head2.addWidget(self.new_op_btn)
        vl.addLayout(head2)

        self.tool_scroll = QScrollArea()
        self.tool_scroll.setWidgetResizable(True)
        self.tool_scroll.setMinimumHeight(230)
        self.tool_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tool_host = QWidget()
        self.tool_grid = QGridLayout(self.tool_host)
        self.tool_grid.setContentsMargins(12, 0, 12, 10)
        self.tool_grid.setSpacing(6)
        self.tool_scroll.setWidget(self.tool_host)
        vl.addWidget(self.tool_scroll, 3)
        return panel

    # ---------------------------------------------------------------- 地图
    def _build_map(self) -> QWidget:
        card = QFrame()
        card.setObjectName("panel")
        self.map_card = card          # 导出「当前视图」时抓它（地图 + 图例 + 缩放键）
        grid = QGridLayout(card)
        grid.setContentsMargins(0, 0, 0, 0)

        self.map = MapView()
        self.map.visible = self.visible
        self.map.hoverChanged.connect(self._on_hover)
        self.map.picked.connect(self._on_pick)
        self.map.viewChanged.connect(self._on_view)
        self.map.cursorMoved.connect(self._on_cursor)
        self.map.progress.connect(self._on_map_progress)
        self.map.selectionChanged.connect(self._on_selection_changed)
        self.map.mapMenu.connect(self._map_menu)
        grid.addWidget(self.map, 0, 0)

        zoom = QWidget()
        zl = QVBoxLayout(zoom)
        zl.setContentsMargins(12, 12, 12, 12)
        zl.setSpacing(6)
        for text, tip, fn in (("+", "放大", lambda: self.map.zoom_by(1)),
                              ("−", "缩小", lambda: self.map.zoom_by(-1)),
                              ("⤢", "全图", self.map.fit_all)):
            b = QPushButton(text)
            b.setObjectName("icon")
            b.setFixedSize(30, 30)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            zl.addWidget(b)
        zl.addStretch(1)
        grid.addWidget(zoom, 0, 0, Qt.AlignTop | Qt.AlignRight)

        self.legend = QFrame()
        self.legend.setObjectName("legendBox")
        self.legend_layout = QVBoxLayout(self.legend)
        self.legend_layout.setContentsMargins(9, 6, 9, 6)
        self.legend_layout.setSpacing(2)
        self.legend.setVisible(False)
        lwrap = QWidget()
        ll = QVBoxLayout(lwrap)
        ll.setContentsMargins(12, 12, 12, 12)
        ll.addWidget(self.legend)
        grid.addWidget(lwrap, 0, 0, Qt.AlignBottom | Qt.AlignLeft)

        self.map_info = QLabel("—")
        self.map_info.setObjectName("mapInfo")
        iwrap = QWidget()
        il = QVBoxLayout(iwrap)
        il.setContentsMargins(12, 12, 12, 12)
        il.addWidget(self.map_info)
        grid.addWidget(iwrap, 0, 0, Qt.AlignBottom | Qt.AlignRight)
        return card

    # ---------------------------------------------------------------- 右栏
    def _build_right(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("chatPanel")
        panel.setFixedWidth(412)
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(0, 0, 0, 0)
        self.chat = ChatPanel(self)
        self.chat.sent.connect(self.on_send)
        self.chat.clear_btn.clicked.connect(self.clear_all)
        vl.addWidget(self.chat)
        return panel

    # ---------------------------------------------------------------- 状态栏
    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.st_cursor = QLabel("光标 —")
        self.st_scale = QLabel("比例尺 —")
        self.st_feat = QLabel("要素 0")
        self.st_msg = QLabel("就绪")
        for w in (self.st_cursor, QLabel("·"), self.st_scale, QLabel("·"), self.st_feat):
            sb.addWidget(w)
        # 载入进度：一根很细的进度条 + 「取消」。默认隐藏，只在解析数据时出现。
        self.st_prog = QProgressBar()
        self.st_prog.setObjectName("loadProg")
        self.st_prog.setFixedSize(110, 4)
        self.st_prog.setTextVisible(False)
        self.st_prog.hide()
        self.st_cancel = QPushButton("取消")
        self.st_cancel.setObjectName("tiny")
        self.st_cancel.setCursor(Qt.PointingHandCursor)
        self.st_cancel.setToolTip("停止解析后面的文件（已经载完的图层会保留）")
        self.st_cancel.clicked.connect(lambda _checked=False: self.cancel_load())
        self.st_cancel.hide()
        sb.addPermanentWidget(self.st_cancel)
        sb.addPermanentWidget(self.st_prog)
        sb.addPermanentWidget(self.st_msg)

    def _shortcuts(self) -> None:
        for seq, fn in ((QKeySequence.Open, self.choose_files),
                        (QKeySequence("Ctrl+0"), self.map.fit_all),
                        (QKeySequence("Ctrl+F"), lambda: self.chat.input.setFocus()),
                        (QKeySequence.Undo, self.undo),
                        (QKeySequence.Redo, self.redo),
                        (QKeySequence("Ctrl+Y"), self.redo),
                        (QKeySequence("Ctrl+Shift+S"), self.export_view),
                        (QKeySequence("Esc"), self._escape)):
            sc = QShortcut(seq, self)
            sc.activated.connect(fn)

    # ================================================================== 图层
    def refresh_layers(self, fit: bool = False) -> None:
        layers = list(self.store.layers.values())
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for lay in reversed(layers):
            item = QListWidgetItem()
            row = LayerRow(lay, self.visible.get(lay.id, True), self.toggle_visible)
            item.setSizeHint(QSize(240, 58))
            item.setData(Qt.UserRole, lay.id)
            self.layer_list.addItem(item)
            self.layer_list.setItemWidget(item, row)
            if lay.id == self.current_layer:
                self.layer_list.setCurrentItem(item)
        self.layer_list.blockSignals(False)

        self.map.set_layers(layers)
        if fit:
            self.map.fit_all()
        self._refresh_legend()
        self.count_badge.setText(str(len(layers)))
        total = sum(len(l.gdf) for l in layers)
        self.st_feat.setText(f"要素 {total:,}")

    def _refresh_legend(self) -> None:
        while self.legend_layout.count():
            it = self.legend_layout.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        layers = [l for l in self.store.layers.values()]
        self.legend.setVisible(bool(layers))
        for lay in layers:
            row = QWidget()
            hl = QHBoxLayout(row)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(6)
            sw = QLabel()
            sw.setFixedSize(9, 9)
            alpha = "ff" if self.visible.get(lay.id, True) else "55"
            sw.setStyleSheet(f"background:{lay.color};border-radius:2px;")
            sw.setFixedSize(9, 9)
            hl.addWidget(sw)
            name = QLabel(lay.name)
            name.setObjectName("legendRow")
            if not self.visible.get(lay.id, True):
                name.setStyleSheet("color:#b3c0d1;text-decoration:line-through;")
            hl.addWidget(name)
            row.setCursor(Qt.PointingHandCursor)
            row.mousePressEvent = lambda e, lid=lay.id: self.toggle_visible(lid)  # type: ignore
            self.legend_layout.addWidget(row)

    def toggle_visible(self, layer_id: str, visible: bool | None = None) -> None:
        """切换图层显隐。

        `visible` 有值时以它为准（复选框的 toggled 会带状态过来），
        没值时按当前状态取反（图例行点击走这条）。
        只刷新图例，不重建整个图层列表 —— 重建会把正在发信号的复选框删掉，
        还会丢掉选中项和滚动位置。
        """
        if visible is None:
            visible = not self.visible.get(layer_id, True)
        self.visible[layer_id] = bool(visible)
        self._sync_eye(layer_id, bool(visible))
        self.map.invalidate()
        # 延后刷新图例：图例行自己也能触发这里，不能当场把它删掉
        QTimer.singleShot(0, self._refresh_legend)

    def _sync_eye(self, layer_id: str, visible: bool) -> None:
        """让图层行上的复选框跟上状态（图例行点击、程序调用都会走到）。

        不能靠重建整行来同步：那会丢掉选中项和滚动位置，
        而且可能在复选框自己的信号回调里把它删掉。
        """
        for i in range(self.layer_list.count()):
            item = self.layer_list.item(i)
            if item is None or item.data(Qt.UserRole) != layer_id:
                continue
            row = self.layer_list.itemWidget(item)
            if row is not None and hasattr(row, "eye"):
                row.eye.blockSignals(True)
                row.eye.setChecked(visible)
                row.eye.blockSignals(False)
            return

    def _on_layer_selected(self, cur, _prev) -> None:
        if cur is None:
            return
        lid = cur.data(Qt.UserRole)
        self.current_layer = lid
        self.map.set_select_target(lid)
        self.map.fit_layer(lid)
        lay = self.store.get(lid)
        if lay:
            self.set_status(f"已选中「{lay.name}」")

    def _layer_menu(self, pos) -> None:
        item = self.layer_list.itemAt(pos)
        if item is None:
            return
        lid = item.data(Qt.UserRole)
        lay = self.store.get(lid)
        if lay is None:
            return
        menu = QMenu(self)
        menu.addAction("缩放到图层", lambda: self.map.fit_layer(lid))
        menu.addAction("属性表", lambda: self.open_table(lid))
        menu.addAction("重命名…", lambda: self.rename_layer(lid))
        menu.addAction("复制图层", lambda: self.duplicate_layer(lid))
        show = menu.addMenu("显示")
        show.addAction("只显示此层", lambda: self.show_only(lid))
        show.addAction("显示全部", self.show_all)
        ex = menu.addMenu("导出")
        for label, fmt in (("GeoJSON", "geojson"), ("GeoPackage", "gpkg"),
                           ("Shapefile (zip)", "shp"), ("CSV 属性表", "csv")):
            ex.addAction(label, lambda _=False, f=fmt, i=lid: self.export_layer(i, f))
        menu.addSeparator()
        menu.addAction("删除图层", lambda: self.delete_layer(lid))
        menu.exec(self.layer_list.mapToGlobal(pos))

    def rename_layer(self, layer_id: str) -> None:
        lay = self.store.get(layer_id)
        if lay is None:
            return
        name, ok = QInputDialog.getText(self, "重命名图层", "新名称：", text=lay.name)
        name = (name or "").strip()
        if not ok or not name or name == lay.name:
            return
        # 重名自动加序号，避免后面「按名字找图层」时撞车
        existing = {l.name for l in self.store.layers.values() if l.id != layer_id}
        base, i = name, 2
        while name in existing:
            name = f"{base} ({i})"
            i += 1
        self._push_undo({"kind": "rename", "lid": layer_id, "old": lay.name, "new": name})
        lay.name = name
        self.refresh_layers()
        self.set_status(f"已重命名：「{name}」")

    def duplicate_layer(self, layer_id: str) -> None:
        lay = self.store.get(layer_id)
        if lay is None:
            return
        new = self.store.add(lay.gdf.copy(), f"{lay.name}_副本", source=lay.id, op="copy")
        self.visible[new.id] = True
        self.current_layer = new.id
        self._push_undo({"kind": "add", "lids": [new.id], "layers": [(new.id, new)]})
        self.refresh_layers()
        self.set_status(f"已复制出「{new.name}」")

    def show_only(self, layer_id: str) -> None:
        for lid in self.store.layers:
            self.visible[lid] = (lid == layer_id)
        self._apply_visibility()
        self.set_status(f"只显示「{self.store.get(layer_id).name}」")

    def show_all(self) -> None:
        for lid in self.store.layers:
            self.visible[lid] = True
        self._apply_visibility()
        self.set_status("已显示全部图层")

    def _apply_visibility(self) -> None:
        """显隐批量变了之后，把复选框和图例同步一遍，再触发重渲染。"""
        for i in range(self.layer_list.count()):
            item = self.layer_list.item(i)
            if item is None:
                continue
            lid = item.data(Qt.UserRole)
            row = self.layer_list.itemWidget(item)
            if row is not None and hasattr(row, "eye"):
                row.eye.blockSignals(True)
                row.eye.setChecked(bool(self.visible.get(lid, True)))
                row.eye.blockSignals(False)
        self.map.invalidate()
        self._refresh_legend()

    def export_view(self) -> None:
        """把当前视图（地图 + 图例 + 缩放键）存成 PNG，出报告直接用。"""
        if not self.store.layers:
            self.set_status("还没有数据")
            return
        from datetime import datetime
        default = str(Path.home() / "Desktop" /
                      f"地图视图_{datetime.now():%Y%m%d_%H%M}.png")
        path, _ = QFileDialog.getSaveFileName(self, "导出当前视图", default,
                                              "PNG 图片 (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        img = self.map_card.grab()
        if img.save(path):
            self.set_status(f"已导出视图 → {Path(path).name}")
            self.chat.add_agent(f"当前视图已存为图片：{path}")

    def delete_layer(self, layer_id: str, record: bool = True) -> None:
        lay = self.store.get(layer_id)
        if lay is None:
            return
        if record:
            self._push_undo({"kind": "del", "lid": layer_id, "layer": lay,
                             "index": list(self.store.layers).index(layer_id),
                             "visible": self.visible.get(layer_id, True)})
        self._drop_layers([layer_id])

    def _drop_layers(self, lids: list[str]) -> None:
        if self.map.selection_layer() in lids:
            self.map.clear_selection()
        for lid in lids:
            self.store.layers.pop(lid, None)
            self.visible.pop(lid, None)
            self.map.drop_cache(lid)
            if self.current_layer == lid:
                self.current_layer = None
        self.refresh_layers()

    # ---------------------------------------------------------------- 撤销 / 重做
    def _push_undo(self, action: dict) -> None:
        self._undo_stack.append(action)
        if len(self._undo_stack) > 20:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

    def _restore_layer(self, act: dict) -> None:
        """把删掉的图层按原来的顺序放回去（id、颜色、父子关系都不变）。"""
        items = list(self.store.layers.items())
        idx = max(0, min(act["index"], len(items)))
        items.insert(idx, (act["lid"], act["layer"]))
        self.store.layers = dict(items)
        self.visible[act["lid"]] = act.get("visible", True)
        self.current_layer = act["lid"]
        self.refresh_layers()

    def undo(self) -> None:
        if not self._undo_stack:
            self.set_status("没有可撤销的操作")
            return
        act = self._undo_stack.pop()
        if act["kind"] == "add":
            self._drop_layers(act["lids"])
            self.set_status(f"已撤销：移除 {len(act['lids'])} 个新图层")
        elif act["kind"] == "del":
            self._restore_layer(act)
            self.set_status(f"已撤销删除：「{act['layer'].name}」")
        elif act["kind"] == "rename":
            lay = self.store.get(act["lid"])
            if lay is not None:
                lay.name = act["old"]
                self.refresh_layers()
            self.set_status(f"已撤销重命名：「{act['old']}」")
        self._redo_stack.append(act)

    def redo(self) -> None:
        if not self._redo_stack:
            self.set_status("没有可重做的操作")
            return
        act = self._redo_stack.pop()
        if act["kind"] == "add":
            # 重做「新增」：撤销时只是从 store 里摘下来，对象还在，原样放回去即可
            for lid, lay in act.get("layers", []):
                self.store.layers[lid] = lay
                self.visible[lid] = True
            self.refresh_layers()
            self.set_status(f"已重做：恢复 {len(act.get('layers', []))} 个图层")
        elif act["kind"] == "del":
            self._push_undo_no_clear({"kind": "del", "lid": act["lid"], "layer": act["layer"],
                                      "index": list(self.store.layers).index(act["lid"]),
                                      "visible": self.visible.get(act["lid"], True)})
            self._drop_layers([act["lid"]])
            self.set_status(f"已重做：删除「{act['layer'].name}」")
        elif act["kind"] == "rename":
            lay = self.store.get(act["lid"])
            if lay is not None:
                lay.name = act["new"]
                self.refresh_layers()
            self.set_status(f"已重做重命名：「{act['new']}」")
        self._undo_stack.append(act)

    def _push_undo_no_clear(self, action: dict) -> None:
        self._undo_stack.append(action)
        if len(self._undo_stack) > 20:
            self._undo_stack.pop(0)

    def _escape(self) -> None:
        """Esc：先清选区，再清高亮。"""
        if self.map.selection_count():
            self.map.clear_selection()
            self.set_status("已清除选区")
        else:
            self.map.clear_highlight()

    # ================================================================== 地图事件
    def _on_view(self) -> None:
        mpp = self.map.mpp()
        self.map_info.setText(f"缩放 {mpp:.0f} m/px" if mpp >= 1 else f"缩放 {mpp:.2f} m/px")
        self.st_scale.setText(f"比例尺 ≈ {mpp * 120 / 1000:.2f} km / 120px"
                              if mpp * 120 >= 1000 else f"比例尺 ≈ {mpp * 120:.0f} m / 120px")

    def _on_cursor(self, lon: float, lat: float) -> None:
        self.st_cursor.setText(f"光标 {lon:.5f}°E, {lat:.5f}°N")

    @staticmethod
    def _clean_props(props: dict) -> dict:
        out = {}
        for k, v in (props or {}).items():
            s = str(v)
            if v is None or s == "nan" or s == "None":
                continue
            out[str(k)] = s[:60]
        return out

    def _on_hover(self, info) -> None:
        if not info:
            QToolTip.hideText()
            return
        props = self._clean_props(info.get("props"))
        lines = [f"<b>{k}</b> {v}" for k, v in list(props.items())[:3]]
        html = f"<div style='color:#9fc2ff'>{info['layer'].name}</div>" + "<br>".join(lines)
        QToolTip.showText(QCursor.pos(), html, self.map)

    def _on_pick(self, info) -> None:
        """点击要素：用浮动提示展示属性，不弹模态框打断操作。"""
        if not info:
            return
        props = self._clean_props(info.get("props"))
        rows = "".join(f"<tr><td style='color:#9fc2ff;padding-right:8px'>{k}</td>"
                       f"<td>{v}</td></tr>" for k, v in list(props.items())[:10])
        html = f"<b>{info['layer'].name}</b><table cellspacing='0'>{rows}</table>"
        QToolTip.showText(QCursor.pos(), html, self.map, self.map.rect(), 6000)

    def set_status(self, text: str) -> None:
        self.st_msg.setText(text)

    # ---------------------------------------------------------------- 载入进度
    def _begin_load(self, total: int) -> None:
        self._load_total = max(1, total)
        self._load_done = 0
        self._load_cancel = False
        self.st_prog.setRange(0, self._load_total)
        self.st_prog.setValue(0)
        self.st_prog.show()
        self.st_cancel.setText("取消")
        self.st_cancel.setEnabled(True)
        self.st_cancel.show()

    def _tick_load(self, name: str) -> None:
        """开始解析某个文件时刷一次（`_load_done` 已完成数）。"""
        if not getattr(self, "_load_total", 0):
            return
        self.st_prog.setValue(self._load_done)
        i = min(self._load_done + 1, self._load_total)
        self.set_status(f"正在解析 {i}/{self._load_total} · {name}")

    def _finish_load(self, msg: str = "") -> None:
        self.st_prog.hide()
        self.st_cancel.hide()
        self._load_total = 0
        self._load_done = 0
        self._load_cancel = False
        if msg:
            self.set_status(msg)

    def cancel_load(self) -> None:
        """取消：解析调用本身没法打断，当前文件读完就停，已载完的图层保留。"""
        if not getattr(self, "_load_total", 0):
            return
        self._load_cancel = True
        self.st_cancel.setEnabled(False)
        self.st_cancel.setText("正在停止…")
        self.set_status("正在停止：当前文件读完后不再继续")

    def _apply_selection_as_layer(self) -> int:
        """把当前选区单独成一层，后续算子作用于它。返回选中的要素数（0 = 没选区）。"""
        lid = self.map.selection_layer()
        idx = self.map.selection_indices()
        src = self.store.get(lid) if lid else None
        if src is None or idx is None or len(idx) == 0:
            return 0
        try:
            sub = src.gdf.iloc[idx].copy()
        except Exception:
            return 0
        n = len(sub)
        lay = self.store.add(sub, f"{src.name}_选中{n}", source=src.id, op="select")
        self.visible[lay.id] = True
        self.current_layer = lay.id
        self._push_undo({"kind": "add", "lids": [lay.id], "layers": [(lay.id, lay)]})
        self.map.clear_selection()
        self.refresh_layers()
        self.chat.add_agent(f"已把选中的 {n} 个要素单独成一层「{lay.name}」，接下来的操作作用于它。")
        return n

    def _map_menu(self, pos) -> None:
        self._build_map_menu().exec(pos)

    def _build_map_menu(self) -> QMenu:
        """地图右键菜单：视图、选区、导出（单独拆出来，方便测试）。"""
        menu = QMenu(self)
        menu.addAction("全图（Ctrl+0）", self.map.fit_all)
        cur = self.store.get(self.current_layer)
        if cur is not None:
            menu.addAction(f"缩放到「{cur.name}」", lambda: self.map.fit_layer(cur.id))
        n_sel = self.map.selection_count()
        if n_sel:
            menu.addAction(f"把选中的 {n_sel} 个要素存成图层",
                           lambda: self._apply_selection_as_layer())
            menu.addAction("清除选区（Esc）", self.map.clear_selection)
        menu.addSeparator()
        menu.addAction("导出当前视图…（Ctrl+Shift+S）", self.export_view)
        if cur is not None:
            menu.addAction("导出当前图层…", lambda: self.export_layer(cur.id))
        return menu

    def _on_selection_changed(self, n: int) -> None:
        if n:
            lay = self.store.get(self.map.selection_layer() or "")
            name = f"「{lay.name}」" if lay else ""
            self.set_status(f"{name}已选 {n} 个要素 · 说个操作，Esc 清除")
        elif not self._busy:
            self.set_status("已清除选区")

    def _on_map_progress(self, text: str) -> None:
        if text:
            self.st_msg.setText(text)
        elif self.st_msg.text().startswith("正在构建"):
            self.st_msg.setText("就绪")

    # ================================================================== 数据载入
    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择矢量数据", "", FILE_FILTER)
        if paths:
            self.load_paths(paths)

    def load_paths(self, paths: list[str]) -> None:
        if self._busy:
            QMessageBox.information(self, "正在执行", "当前还有任务在跑，请稍候。")
            return
        # 排队串行载入：一次只解析一个文件。
        # 并发解析看似更快，但 gpd.read_file 在后台线程里长时间持有 GIL，
        # 3 个线程一起跑会把主线程饿死，界面一卡就是好几秒；
        # 串行反而更稳，而且每载完一个就能立刻刷新界面。
        queued, missing = [], []
        for p in paths:
            (queued if Path(p).exists() else missing).append(Path(p))
        if missing:
            names = "、".join(p.name for p in missing[:4])
            self.chat.add_agent(f"这些文件找不到了，已跳过：{names}"
                                + ("…" if len(missing) > 4 else ""))
            self.set_status(f"{len(missing)} 个文件不存在，已跳过")
        if not queued:
            self._finish_load()
            return
        self._load_queue = queued
        self._begin_load(len(self._load_queue))
        self._load_next()

    def _load_next(self) -> None:
        """从队列里取下一个文件开始载入。"""
        if getattr(self, "_load_cancel", False):
            self._load_queue = []
            self.map.set_loading(False)
            self._set_busy(False)
            self._finish_load(f"已取消，保留已载入的 {len(self.store.layers)} 个图层")
            return
        while self._load_queue:
            path = self._load_queue.pop(0)
            self._tick_load(path.name)
            if self._load_one(path):
                return          # 载入已启动，完成后会自动续下一个
            # 这个文件不支持/被跳过 → 直接看下一个
            self._load_done += 1
        self._load_queue = []
        self.map.set_loading(False)
        self._set_busy(False)
        self._finish_load()

    def _load_one(self, path: Path) -> bool:
        """开始载入一个文件，返回是否真的启动了后台任务。"""
        suffix = path.suffix.lower()
        if suffix not in (".zip", ".shp", ".geojson", ".json", ".gpkg", ".kml", ".gml", ".csv"):
            QMessageBox.warning(self, "不支持的格式", f"{path.suffix} 暂时不支持。\n"
                                                     "请上传 zip/shp/geojson/gpkg/kml/csv。")
            return False
        if suffix == ".zip":
            try:
                root, files = extract_zip(path)
            except Exception as e:
                QMessageBox.warning(self, "读取失败", str(e))
                return False
            if len(files) > 1:
                cands = []
                for i, f in enumerate(files):
                    info = file_info(f)
                    cands.append({"id": i, "name": f.stem, "ext": f.suffix,
                                  "size_mb": round(f.stat().st_size / 1e6, 1), **info})
                dlg = ImportDialog(cands, path.name, self)
                if dlg.exec() != ImportDialog.Accepted or not dlg.picks():
                    shutil.rmtree(root, ignore_errors=True)
                    self.set_status("已取消导入")
                    return False
                chosen = [files[i] for i in dlg.picks()]
            else:
                chosen = files
            self._tmp_root = root
            self._load_files(chosen, source=path.name)
            return True
        self._load_files([path], source=path.name)
        return True

    def _cleanup_tmp(self) -> None:
        """清理压缩包解压出的临时目录。

        删除 30 万个文件的目录要几百毫秒，放后台线程做，别卡住界面。
        """
        root = self._tmp_root
        self._tmp_root = None
        if root is None:
            return
        try:
            w = Worker(lambda: shutil.rmtree(root, ignore_errors=True))
            w.done.connect(lambda _r: None)
            w.failed.connect(lambda _msg: None)
            w.start()
            self._cleanup_worker = w
        except Exception:
            shutil.rmtree(root, ignore_errors=True)

    def _load_files(self, files: list[Path], source: str) -> None:
        self._set_busy(True)
        self._tick_load(files[0].name if files else source)
        # 只有解析数据时才暂停几何构建：gpd.read_file 在后台线程里会长时间
        # 占着 GIL，两边一起跑会互相饿死（实测 4 个大图层：11s 的活抢成 50s）。
        self.map.set_loading(True)

        def job():
            out, errors = [], []
            for f in files:
                try:
                    gdf = load_any(f)
                    lay = self.store.add(gdf, f.stem, source=source, op="load")
                    out.append(lay)
                except Exception as e:
                    errors.append(f"{f.name}：{e}")
            return out, errors

        self._worker = Worker(job)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(self._on_load_failed)
        self._worker.start()

    def _on_load_failed(self, msg: str) -> None:
        self.map.set_loading(False)
        self._set_busy(False)
        self._error("载入失败", msg)

    def _on_loaded(self, payload) -> None:
        layers, errors = payload
        # 队列里还有文件 → 保持 busy / 保持暂停构建，否则地图会在这段时间
        # 开始建缓存，和下一个文件的解析抢 GIL，两边都慢
        if getattr(self, "_load_total", 0):
            self._load_done = min(self._load_done + 1, self._load_total)
            self.st_prog.setValue(self._load_done)
        cancelled = getattr(self, "_load_cancel", False)
        if cancelled:
            self._load_queue = []
        remaining = bool(getattr(self, "_load_queue", None))
        self._set_busy(remaining)
        self.map.set_loading(remaining)
        self._cleanup_tmp()
        for lay in layers:
            self.visible[lay.id] = True
        if layers:
            self.current_layer = layers[-1].id
            # 队列里还有文件 → 先不 fit：每 fit 一次视口就变，
            # 所有图层的渲染集都要重建一遍（多个大图层会白白重建好几轮）。
            # 等最后一个文件载完再统一 fit 一次。
            self.refresh_layers(fit=not getattr(self, "_load_queue", None))
            self.set_status(f"已载入 {len(layers)} 个图层")
            self.chat.add_agent("已载入：" + "、".join(
                f"「{l.name}」（{len(l.gdf):,} 要素）" for l in layers))
        if errors:
            QMessageBox.warning(self, "部分文件未载入", "\n".join(errors[:6]))
        # 队列里还有文件 → 接着载入下一个
        if getattr(self, "_load_queue", None):
            QTimer.singleShot(0, self._load_next)
        elif cancelled:
            self._finish_load(f"已取消，保留已载入的 {len(self.store.layers)} 个图层")
        else:
            self._finish_load(f"载入完成，共 {len(self.store.layers)} 个图层"
                              " · Shift+拖拽可框选要素")

    def load_samples(self) -> None:
        self._set_busy(True, "正在生成示例数据…")

        def job():
            import geopandas as gpd
            out = []
            for f in ensure_samples():
                gdf = gpd.read_file(f)
                if gdf.crs is None:
                    gdf = gdf.set_crs(4326)
                out.append(self.store.add(gdf, f.stem, source=f.name, op="sample"))
            return out, []

        self._worker = Worker(job)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(lambda msg: (self._set_busy(False), self._error("载入失败", msg)))
        self._worker.start()

    def clear_all(self) -> None:
        self.store.layers.clear()
        self.store._n = 0
        self.visible.clear()
        self.current_layer = None
        self.history.clear()
        self.map.drop_cache()
        self.chat.clear()
        self.refresh_layers()
        self._greet()
        self.set_status("已清空")

    # ================================================================== 对话
    def _greet(self) -> None:
        self.chat.add_agent(
            "我是你的空间分析助手。数据拖进来后，直接用中文说需求就行，例如：\n"
            "· 对「地块」做 500 米缓冲区，按用地类型融合\n"
            "· 用「行政区」裁剪「地块」，再统计各类面积\n"
            "· 给「学校」做 1 公里覆盖圈，看覆盖了哪些地块\n\n"
            "每一步运算都会生成新图层，原数据只读不改。")
        self._refresh_chips()

    def _refresh_chips(self) -> None:
        from app.agent import SUGGESTIONS
        self.chat.set_suggestions(SUGGESTIONS)

    def _refresh_tools(self) -> None:
        """算子库：按用途分组展示（同一组两列排开）。

        25 个算子平铺在一个网格里很难扫读，按「概览 / 几何构造 / 叠加分析 /
        邻近与连接 / 属性与统计 / 整理 / 我的算子」分组，组内仍可两列并排。
        """
        while self.tool_grid.count():
            it = self.tool_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

        from app.agent import TOOL_LABEL, TOOL_SPECS
        TOOL_SPEC_BY_NAME.clear()
        TOOL_SPEC_BY_NAME.update({t["name"]: t.get("desc", "") for t in TOOL_SPECS})
        groups = self._tool_groups()
        row = 0
        total = 0
        first = True
        for title, names in groups:
            if not names:
                continue
            lab = QLabel(f"{title}<span style='color:#c6c1b2;font-size:9px'>"
                         f"  {len(names)}</span>")
            lab.setObjectName("toolGroup")
            if first:
                lab.setProperty("first", "1")   # 首组不画上分隔线
                first = False
            self.tool_grid.addWidget(lab, row, 0, 1, 2)
            row += 1
            for i, name in enumerate(names):
                b = QPushButton(TOOL_LABEL.get(name, name))
                b.setObjectName("toolChip")
                b.setCursor(Qt.PointingHandCursor)
                b.setToolTip(TOOL_SPEC_BY_NAME.get(name, "") or name)
                b.clicked.connect(lambda _=False, n=name: self._insert_template(n))
                self.tool_grid.addWidget(b, row + i // 2, i % 2)
            row += (len(names) + 1) // 2
            total += len(names)
        self.tool_badge.setText(str(total))

    @staticmethod
    def _tool_groups() -> list[tuple[str, list[str]]]:
        """把当前可用的算子按用途归类；自定义算子单独成组。"""
        from app.agent import OPERATIONS
        groups = [(title, [n for n in names if n in OPERATIONS])
                  for title, names in TOOL_GROUPS]
        grouped = {n for _, names in groups for n in names}
        custom = [n for n in OPERATIONS
                  if n not in grouped and getattr(OPERATIONS[n], "__name__", "").startswith("custom_")]
        other = [n for n in OPERATIONS if n not in grouped and n not in custom]
        groups.append(("我的算子", custom))
        if other:
            groups.append(("其它", other))
        return [(t, ns) for t, ns in groups if ns]

    def _insert_template(self, tool: str) -> None:
        cur = self.store.get(self.current_layer)
        lname = cur.name if cur else (next(iter(self.store.layers.values())).name
                                      if self.store.layers else "图层")
        other = next((l.name for l in self.store.layers.values() if l.name != lname), "边界")
        tpl = TOOL_TEMPLATES.get(tool, TOOL_LABEL_FALLBACK(tool))
        self.chat.input.setPlainText(tpl.replace("{L}", lname).replace("{O}", other))
        self.chat.input.setFocus()

    def on_send(self, text: str) -> None:
        if not self.store.layers:
            self.chat.add_user(text)
            # 没数据时也可能是「你好」——先让闲聊逻辑接手，别一上来就催数据
            small = self.planner.chit_chat(text, has_data=False)
            self.chat.add_agent(small or (
                "还没有数据。先把矢量文件拖进来（shp 压缩包 / GeoJSON / GPKG / KML / CSV），"
                "或者点右上角「示例数据」先试一遍。"))
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": small or ""})
            return
        self.chat.add_user(text)
        self.chat.add_typing()
        sel_note = self._apply_selection_as_layer()
        self._set_busy(True, "Agent 正在执行…", cursor=False)
        self._exec_t0 = time.time()
        self._exec_timer.start()
        cfg = load_cfg()
        current = self.current_layer
        if sel_note:
            # 让规划器/大模型知道现在有一层「只含选中的要素」
            text = f"（当前图层只包含我选中的 {sel_note} 个要素，下面的操作都作用于它）{text}"

        def job():
            self.planner.store = self.store
            # 「写一个算子：……」→ 走生成流程，不进规划器
            if self.planner.wants_new_operator(text):
                brief = self.planner.extract_operator_brief(text)
                return {"op_gen": self.planner.gen_operator(brief, current, cfg),
                        "brief": brief, "user": text,
                        "steps": [], "lines": [], "source": "gen", "note": ""}
            # 纯对话（问候 / 道谢 / 问能做什么）先兜住，别送进规划器
            small = self.planner.chit_chat(text, has_data=True)
            if small:
                return {"chat": small, "steps": [], "lines": [], "source": "chat",
                        "note": "", "user": text}
            plan, source, note = None, "rule", ""
            if is_ready(cfg):
                try:
                    out = self.planner.llm_plan(text, current, cfg, self.history)
                except Exception:
                    out = None
                if out and out.get("plan"):
                    plan, source = out["plan"], "llm"
                    note = out.get("reply") or ""
                elif out and out.get("error"):
                    note = out["error"]
                elif out and out.get("reply") and not out.get("plan"):
                    # 大模型判断这是闲聊/问询，给了自然语言回复 → 直接用
                    return {"chat": out["reply"], "steps": [], "lines": [],
                            "source": "llm", "note": "", "user": text}
            if not plan:
                plan = self.planner.rule_plan(text, current)

            exports = [p for p in plan if p.get("tool") == "__export__"]
            plan = [p for p in plan if p.get("tool") != "__export__"]
            steps, lines, new_layers = run_plan(self.store, plan) if plan else ([], [], [])
            for ex in exports:
                args = ex.get("args") or {}
                lid = args.get("layer")
                lay = self.store.get(lid)
                fmt = args.get("format", "geojson")
                if lay:
                    steps.append({"tool": "export", "label": "导出",
                                  "args": {"layer": lay.name, "format": fmt},
                                  "status": "ok", "message": f"生成 {fmt.upper()}",
                                  "warnings": [], "elapsed": 0,
                                  "data": {"layer_id": lay.id, "format": fmt}})
                    lines.append(f"已准备「{lay.name}」的 {fmt.upper()} 导出。")
            return {"steps": steps, "lines": lines, "source": source, "note": note,
                    "user": text}

        self._worker = Worker(job)
        self._worker.done.connect(self._on_plan_done)
        self._worker.failed.connect(self._on_plan_failed)
        self._worker.start()

    def _tick_exec(self) -> None:
        """每秒刷一次「正在执行… Ns」，让长算子看起来是在干活而不是卡死。"""
        if not self._busy:
            self._exec_timer.stop()
            return
        sec = int(time.time() - self._exec_t0)
        self.chat.set_typing_text(f"正在规划并执行… {sec}s")
        self.set_status(f"Agent 正在执行… {sec}s")

    def _on_plan_done(self, res: dict) -> None:
        self._exec_timer.stop()
        self.chat.remove_typing()
        self._set_busy(False)
        user = res.get("user") or ""
        steps = res.get("steps") or []
        lines = res.get("lines") or []

        # 生成算子：先给用户看代码，确认后才入库
        if res.get("op_gen") is not None:
            self._handle_operator_gen(res.get("op_gen") or {}, res.get("brief") or user)
            return

        # 纯对话：直接回一句，不进步骤卡片
        if res.get("chat"):
            reply = res["chat"]
            self.chat.add_agent(reply)
            self._remember(user, reply)
            self._refresh_planner_pill(res.get("source"))
            return

        if not steps:
            note = (res.get("note") or "").strip()
            # 大模型/解析器给了自然语言说明就用它，别再甩模板
            reply = note or ("这句我还拆不出可执行的步骤。可以这样描述：\n"
                             "· 对「图层名」做 500 米缓冲区\n"
                             "· 用「边界」裁剪「地块」\n"
                             "· 按「类型」融合后统计各类面积")
            self.chat.add_agent(reply)
            self._remember(user, reply)
            return
        err = sum(1 for s in steps if s.get("status") == "error")
        head = f"执行了 {len(steps)} 步" + (f"，其中 {err} 步失败" if err else "，全部成功") + "："
        body = "\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines))
        text = (res.get("note") + "\n" if res.get("note") else "") + head + "\n" + body
        self.chat.add_agent(text, steps)
        self._remember(user, text)

        created = [s["layer"] for s in steps if s.get("layer")]
        if created:
            acts = [(m["id"], self.store.get(m["id"])) for m in created]
            acts = [(lid, lay) for lid, lay in acts if lay is not None]
            if acts:
                self._push_undo({"kind": "add", "lids": [lid for lid, _ in acts], "layers": acts})
            self.current_layer = created[-1]["id"]
        self.refresh_layers()
        if created:
            self.map.fit_layer(created[-1]["id"])
            self.set_status(f"完成 {len(steps)} 步，生成 {len(created)} 个图层")
        else:
            self.set_status(f"完成 {len(steps)} 步")
        self._refresh_planner_pill(res.get("source"))

    def _on_plan_failed(self, msg: str) -> None:
        self._exec_timer.stop()
        self.chat.remove_typing()
        self._set_busy(False)
        self.chat.add_agent(f"执行出错：{msg}")
        self.set_status("出错")

    # ================================================================== 其它动作
    def locate_layer(self, layer_id: str) -> None:
        self.current_layer = layer_id
        self.refresh_layers()
        self.map.fit_layer(layer_id)

    def open_table(self, layer_id: str) -> None:
        lay = self.store.get(layer_id)
        if lay is None:
            return

        def pick(pos_idx: int) -> None:
            # 点表格一行 → 地图上高亮该要素并缩放到它
            self.map.set_select_target(layer_id)
            self.map.highlight_feature(layer_id, pos_idx)
            self.map.zoom_to_feature(layer_id, pos_idx)
            self.set_status(f"「{lay.name}」第 {pos_idx + 1} 个要素")

        self._table_dlg = TableDialog(lay, self, on_pick=pick)
        self._table_dlg.exec()
        self._table_dlg = None

    def export_layer(self, layer_id: str, fmt: str | None = None) -> None:
        lay = self.store.get(layer_id)
        if lay is None:
            return
        if fmt is None:
            menu = QMenu(self)
            for label, f in (("GeoJSON (.geojson)", "geojson"), ("GeoPackage (.gpkg)", "gpkg"),
                             ("Shapefile (.zip)", "shp"), ("CSV 属性表 (.csv)", "csv")):
                menu.addAction(label, lambda _=False, ff=f: self.export_layer(layer_id, ff))
            menu.exec(self.map.mapToGlobal(self.map.rect().center()))
            return
        ext = {"geojson": ".geojson", "gpkg": ".gpkg", "shp": ".zip", "csv": ".csv"}[fmt]
        default = str(Path.home() / f"{lay.name}{ext}")
        path, _ = QFileDialog.getSaveFileName(self, "导出图层", default,
                                              f"*{ext}")
        if not path:
            return
        dest = Path(path if path.lower().endswith(ext) else path + ext)
        self._set_busy(True, "正在导出…")

        def job():
            return write_layer_file(lay.gdf, lay.name, fmt, dest)

        self._worker = Worker(job)
        self._worker.done.connect(lambda p: self._on_exported(Path(p)))
        self._worker.failed.connect(lambda msg: (self._set_busy(False), self._error("导出失败", msg)))
        self._worker.start()

    def _on_exported(self, path: Path) -> None:
        self._set_busy(False)
        self.set_status(f"已导出 {path.name}")
        QMessageBox.information(self, "导出完成", f"已保存到：\n{path}")

    def open_settings(self) -> None:
        cfg = load_cfg()
        dlg = SettingsDialog(cfg, self)
        if dlg.exec() != SettingsDialog.Accepted:
            return
        value = dlg.value()
        cfg.update(value)
        save_cfg(cfg)
        self._refresh_planner_pill()
        cfg = load_cfg()          # 读回来的是补全后的（含默认 Base URL / 模型）
        if is_ready(cfg):
            QMessageBox.information(
                self, "已保存",
                f"已启用大模型规划。\n{explain(cfg)}\n"
                "下次发消息会先交给大模型理解，接口异常时自动回退规则引擎。")
        elif value.get("enabled"):
            QMessageBox.information(self, "已保存", f"暂未生效：{explain(cfg)}。")
        else:
            self.set_status("设置已保存")

    def _remember(self, user: str, reply: str) -> None:
        """记下这一轮对话，给大模型做多轮上下文（之前只读不写，等于没有记忆）。"""
        if user:
            self.history.append({"role": "user", "content": user})
        if reply:
            self.history.append({"role": "assistant", "content": reply})
        if len(self.history) > 20:
            del self.history[:-20]

    def _refresh_planner_pill(self, source: str | None = None) -> None:
        cfg = load_cfg()
        ready = is_ready(cfg)
        state = explain(cfg)
        if source == "llm" or ready:
            self.pill.setText("● 大模型规划")
            self.pill.setObjectName("pillOn")
            self.chat.set_planner("大模型规划 · 已接入", True)
        else:
            self.pill.setText("● 规则引擎")
            self.pill.setObjectName("pill")
            self.chat.set_planner("规则引擎 · 离线可用", False)
        # 把「为什么没走大模型」写在悬浮提示里，省得用户以为 key 白配了
        self.pill.setToolTip(state)
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)
        self._sync_pill_height()

    # ================================================================== 自定义算子
    def prompt_new_operator(self) -> None:
        """算子库右上角「＋ 新算子」：输入一段算法描述。"""
        if not is_ready(load_cfg()):
            QMessageBox.information(
                self, "需要大模型",
                "按描述生成算子需要接入大模型：\n先到「模型设置」填好 API Key（默认用 DeepSeek）。")
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "用文字描述一个算子",
            "说明要算什么、参数是什么。例如：\n"
            "统计每个面内道路的总长度和条数，一个新图层输出这些字段。",
            "")
        if not ok or not text.strip():
            return
        self.chat.add_user("生成算子：" + text.strip())
        self._run_operator_gen(text.strip())

    def _run_operator_gen(self, brief: str) -> None:
        """后台调用大模型生成算子，完成后弹预览。"""
        self.chat.add_typing()
        self._set_busy(True, "正在生成算子…", cursor=False)
        self._exec_t0 = time.time()
        self._exec_timer.start()

        def job():
            self.planner.store = self.store
            return {"op_gen": self.planner.gen_operator(brief, self.current_layer, load_cfg()),
                    "brief": brief, "user": "", "steps": [], "lines": [],
                    "source": "gen", "note": ""}

        self._worker = Worker(job)
        self._worker.done.connect(self._on_plan_done)
        self._worker.failed.connect(self._on_plan_failed)
        self._worker.start()

    def _handle_operator_gen(self, res: dict, brief: str) -> None:
        """展示生成的算子：代码可改，确认后落盘并注册。"""
        if res.get("error"):
            msg = res["error"]
            raw = res.get("raw")
            self.chat.add_agent("没能生成算子：" + msg + ("\n\n模型原始输出：\n" + raw if raw else ""))
            return
        spec = res.get("spec") or {}
        if not spec:
            self.chat.add_agent("没能生成算子：模型没有返回代码。")
            return
        dlg = OperatorDialog(spec, self)
        if dlg.exec() != QDialog.Accepted:
            self.chat.add_agent("已取消，算子没有保存。")
            return
        spec = dlg.value()
        try:
            spec = custom_ops.save(spec)
            custom_ops.install(spec)
        except Exception as e:
            self.chat.add_agent(f"保存失败：{e}")
            return
        self._refresh_tools()
        params = "、".join(spec.get("params") or {}) or "layer"
        self.chat.add_agent(
            f"已加入算子库：「{spec['title']}」（{spec['name']}）。\n"
            f"参数：{params}。\n"
            f"直接说需求就能用它，比如「对地块做{spec['title']}」。")
        self.set_status(f"新算子已就绪：{spec['title']}")



    def _set_busy(self, busy: bool, text: str = "", cursor: bool = True) -> None:
        """只表示「界面忙」：状态文字 + 忙碌光标。

        **不要**在这里暂停地图渲染/构建：Agent 执行、导出也走这个函数，
        暂停了地图就会在任务期间冻住（用户说的「执行任务阻塞渲染」就是这个）。
        只有真正在解析文件的 `_load_files` 才会显式调用 `map.set_loading()`。

        `cursor=False` 用于「后台在算、界面照常可用」的场景（Agent 执行）：
        给整窗套一个转圈光标，会让人以为程序卡死了。
        """
        self._busy = busy
        self.chat.set_busy(busy)
        if busy:
            if cursor and not self._cursor_pushed:
                QApplication.setOverrideCursor(Qt.BusyCursor)
                self._cursor_pushed = True
        elif self._cursor_pushed:
            QApplication.restoreOverrideCursor()
            self._cursor_pushed = False
        if text:
            self.set_status(text)
        if not busy:
            self.set_status("就绪")

    def _error(self, title: str, msg: str) -> None:
        self.set_status("出错")
        QMessageBox.critical(self, title, msg)

    # ================================================================== 拖放
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        QTimer.singleShot(60, self.map.fit_all)
        # 首次显示后再对齐一次：样式表是懒应用的，建界面时按钮还没按 QSS 定型，
        # 那个时刻量出来的高度（20）比最终高度（24）小
        QTimer.singleShot(0, self._sync_pill_height)

    def _sync_pill_height(self) -> None:
        """让状态胶囊和右侧按钮同高。"""
        try:
            h = self.btn_samples.height()
            if h <= 0:
                h = self.btn_samples.sizeHint().height()
            if h and self.pill.height() != h:
                self.pill.setFixedHeight(h)
        except Exception:
            pass

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.load_paths(paths)


def TOOL_LABEL_FALLBACK(tool: str) -> str:
    """没有预置模板时的说法（自定义算子走这里）。"""
    from app.agent import TOOL_LABEL
    return f"对「{{L}}」做{TOOL_LABEL.get(tool, tool)}"
