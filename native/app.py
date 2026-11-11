"""主窗口：三栏工作台（图层 / 地图 / 对话），纯 Qt 控件，无 Web 引擎。"""
from __future__ import annotations

import atexit
import os
import shutil
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QEvent, QEventLoop, QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QIcon, QKeySequence, QPainter, QPen, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
                               QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar,
                               QPushButton, QProgressDialog, QScrollArea, QSizeGrip,
                               QSizePolicy, QTabWidget, QToolTip, QVBoxLayout, QWidget)

from app import custom_ops, tool_prefs
from app.agent import Planner, run_agent, run_plan
from app.config import explain, is_ready, load_cfg, save_cfg
from app.engine import Layer, Store
from app.gdb import is_gdb_path, read_gdb_layers
from app.io_utils import export_layer as write_layer_file
from app.io_utils import extract_zip, file_info, load_any, read_layer
from app.raster import RASTER_EXTS, RasterError, is_raster_path, read_info
from app.models import LayerRecord, Message, Part
from app.memory import MemoryManager
from app.mentions import (ambiguity_question, mentions_note, not_found_note,
                          parse_mentions, resolve_mentions, strip_mentions)
from app.runtime import (AgentRuntime, PRIORITY_CHAT, PRIORITY_EXPORT, PRIORITY_IMPORT,
                         PRIORITY_OPGEN, TaskScheduler, recover_interrupted_runs)
from app.samples import ensure_samples
from app.storage import AgentStore
from app.tool_registry import GROUP_CHOICES, template_map
from app.tool_catalog import tool_tree

from .chat import ChatPanel, PlanCard, Worker
from .agent_settings import AgentSettingsDialog
from .compare import MapCompareDialog, MapCompareWidget
from .dialogs import (ImportDataDialog, ImportDialog, NewToolDialog, OnlineMapDialog,
                      OperatorDialog, SettingsDialog, TableDialog)
from .format import fmt_num
from .mapview import MapView
from .memory_panel import MemoryPanel
from .style_dialog import LayerStyleDialog
from .taskcenter import TaskNotice
from .theme import QSS
from .workspace import WorkspacePanel
from .pyramid import PyramidWorker, has_overviews, overview_factors
from .stepconfirm import StepConfirmDialog, StepConfirmGate

FILE_FILTER = ("矢量与栅格 (*.zip *.shp *.geojson *.json *.gpkg *.kml *.gml *.csv *.tif *.tiff *.img *.vrt *.jp2 *.gdb);;"
               "矢量数据 (*.zip *.shp *.geojson *.json *.gpkg *.kml *.gml *.csv *.gdb);;"
               "栅格数据 (*.tif *.tiff *.img *.vrt *.jp2);;"
               "Shapefile 压缩包 (*.zip);;GeoJSON (*.geojson *.json);;"
               "GeoPackage (*.gpkg);;CSV (*.csv);;所有文件 (*.*)")

#: 标准控件（QTextEdit / QLineEdit / QFileDialog…）的中文翻译器。
#: 不装它，右键菜单会是 Qt 内置的英文 Undo / Redo / Cut / Copy / Paste。
_QT_TRANSLATOR = None


def install_qt_translations(app=None) -> None:
    """加载 Qt 自带的中文翻译，让标准右键菜单 / 原生对话框显示中文。

    `QTranslator` 必须由 Python 侧持有引用，否则会被回收，菜单又变回英文。
    找不到翻译文件时静默跳过（不影响功能，只是菜单仍是 Qt 默认英文）。
    """
    global _QT_TRANSLATOR
    if _QT_TRANSLATOR is not None:
        return
    app = app or QApplication.instance()
    if app is None:
        return
    try:
        from PySide6.QtCore import QLibraryInfo, QTranslator
        path = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
        tr = QTranslator(app)
        if not tr.load("qtbase_zh_CN", path):
            return
        app.installTranslator(tr)
        _QT_TRANSLATOR = tr
    except Exception:
        return


TOOL_TEMPLATES = template_map()


def describe_layer(lay) -> str:
    """一句话描述图层（矢量说要素数，栅格说像元尺寸，在线地图说加载方式）。"""
    if getattr(lay, "online", None):
        from app.online_map import method_label
        return f"「{lay.name}」（在线地图 · {method_label(lay.online.get('method'))}）"
    if getattr(lay, "is_raster", False):
        i = lay.info
        return f"「{lay.name}」（{i.width}×{i.height} 像元 · {i.bands} 波段）"
    try:
        return f"「{lay.name}」（{lay.meta()['count']:,} 要素）"
    except Exception:
        return f"「{lay.name}」"


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


# 工具库的两级分类（只影响展示顺序；不在这里的算子会落到「其它 / 我的工具」）
# 分组表放在 app/tool_catalog.py：左栏工具库和 Agent 设置里的「工具」页共用一份，
# 否则设置页删/停用了工具、左栏还按旧清单显示，两边会打架。
TOOL_SPEC_BY_NAME: dict[str, str] = {}
#: 算子名 → 分组（自定义算子按新建时选的分组归位；没选就是「我的工具」）
TOOL_SPEC_GROUP: dict[str, str] = {}


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


class CollapsibleSection(QFrame):
    """工具库里的可收缩分类面板。

    两级都用它：
    · level="kind"  → 「矢量」「栅格」这种一级分类，body 里放下一级分组的 section；
    · level="group" → 「几何构造」「分析」这种功能分组，body 里放两列工具按钮。
    """

    def __init__(self, title: str, count: int = 0, level: str = "group",
                 collapsed: bool = False, parent=None):
        super().__init__(parent)
        self._title = title
        self._count = int(count)
        self._level = level
        self._chip_count = 0
        self.setObjectName("toolKindCard" if level == "kind" else "toolGroupCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header = QPushButton()
        self.header.setObjectName("toolKindHeader" if level == "kind" else "toolGroupHeader")
        self.header.setCheckable(True)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.setToolTip("点击展开 / 收起")
        root.addWidget(self.header)

        self.body = QWidget()
        self.body.setObjectName("toolKindBody" if level == "kind" else "toolGroupBody")
        if level == "kind":
            self.body_layout = QVBoxLayout(self.body)
            self.body_layout.setContentsMargins(3, 0, 3, 5)
            self.body_layout.setSpacing(0)
        else:
            self.body_layout = QGridLayout(self.body)
            self.body_layout.setContentsMargins(10, 0, 5, 4)
            self.body_layout.setHorizontalSpacing(4)
            self.body_layout.setVerticalSpacing(2)
            self.body_layout.setColumnStretch(0, 1)
            self.body_layout.setColumnStretch(1, 1)
        root.addWidget(self.body)

        # 先定初值再连信号：初始化时不应该被当成用户操作写进折叠状态
        self.header.setChecked(not collapsed)
        self._sync()
        self.header.toggled.connect(self._on_toggled)

    def _sync(self) -> None:
        expanded = self.header.isChecked()
        arrow = "▾" if expanded else "▸"
        self.header.setText(f"{arrow}  {self._title}    {self._count}")
        self.body.setVisible(expanded)

    def _on_toggled(self, expanded: bool) -> None:
        self._sync()

    def add_chip(self, button: QPushButton, span: int = 1) -> None:
        row, col = divmod(self._chip_count, 2)
        if span >= 2:
            # 单入口（例如「在线地图」）横跨两列，点击区更明显，也不像半空一行。
            self.body_layout.addWidget(button, row, 0, 1, 2)
            self._chip_count += 2
        else:
            self.body_layout.addWidget(button, row, col)
            self._chip_count += 1

    def add_section(self, section: "CollapsibleSection") -> None:
        self.body_layout.addWidget(section)

    def is_expanded(self) -> bool:
        return bool(self.header.isChecked())


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
        # 栅格的 meta["count"] 是像元数，显示成「要素」会误导（288,000 像元 ≠ 288,000 要素）
        ras = meta.get("raster")
        if ras and ras.get("online"):
            from app.online_map import method_short
            sub_text = f"在线地图 · {method_short(ras['online'].get('method'))}"
        elif ras:
            sub_text = (f"{ras['width']}×{ras['height']} · {ras['bands']} 波段"
                        f" · {meta['crs']}")
        else:
            sub_text = f"{fmt_num(meta['count'])} 要素 · {meta['geomType']} · {meta['crs']}"
        sub = QLabel(sub_text)
        sub.setObjectName("muted")
        vl.addWidget(sub)


class _TaskBridge(QObject):
    """把 TaskScheduler 的 worker 线程事件转成 Qt 主线程 signal。"""
    event = Signal(str, str, object)


class _TitleBar(QFrame):
    """无边框窗口的自绘标题栏：拖动 / 双击最大化。"""

    def __init__(self, win: "MainWindow", parent=None):
        super().__init__(parent)
        self._win = win
        self._press: QPoint | None = None
        self.setCursor(Qt.ArrowCursor)

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            h = self._win.windowHandle()
            try:
                if h is not None and h.startSystemMove():
                    self._press = None
                    e.accept()
                    return
            except Exception:
                pass
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._press is not None and e.buttons() & Qt.LeftButton:
            now = e.globalPosition().toPoint()
            delta = now - self._press
            self._win.move(self._win.pos() + delta)
            self._press = now
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._press = None
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._win.toggle_maximize()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)


class ResizeHandle(QFrame):
    """右栏左边缘的拖拽把手：左右拖动即可改对话面板宽度。

    对话面板以前是写死 412px 的，窄屏上不够用、宽屏上又浪费地方。
    这里给它一条 6px 的把手，行为和常见 IDE 的分隔条一致：
    · 按住左键左右拖 → 实时改宽度；
    · 双击 → 恢复默认宽度；
    · 松手 → 把宽度记到本地设置里，下次打开还在。
    """

    MIN_W = 340
    MAX_W = 640
    DEFAULT_W = 412

    def __init__(self, on_width=None, parent=None):
        super().__init__(parent)
        self.setObjectName("resizeHandle")
        self.setFixedWidth(6)
        # SizeHorCursor：左右双箭头，正是「横向调宽」的语义
        # （Qt 6 里已经没有 SplitCursor 这个形状了）
        self.setCursor(Qt.SizeHorCursor)
        self.setToolTip("拖动调整对话面板宽度 · 双击恢复默认")
        self._on_width = on_width
        self._panel_ref = None
        self._press_x = 0
        self._press_w = 0
        self._dragging = False

    # ------------------------------------------------------------------ 事件
    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._press_x = e.globalPosition().x()
            self._press_w = self._panel_width()
            self._dragging = True
            self.setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._dragging and e.buttons() & Qt.LeftButton:
            # 往左拖 = 面板变宽
            delta = self._press_x - e.globalPosition().x()
            self._set_width(self._press_w + delta)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self._dragging:
            self._dragging = False
            self.setCursor(Qt.SizeHorCursor)
            if self._on_width:
                self._on_width(self._panel_width())
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._set_width(self.DEFAULT_W)
            if self._on_width:
                self._on_width(self.DEFAULT_W)
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    # ------------------------------------------------------------------ 宽度
    def _panel_width(self) -> int:
        return self._panel_ref.width() if self._panel_ref is not None else self.DEFAULT_W

    def _set_width(self, width: int) -> None:
        w = max(self.MIN_W, min(self.MAX_W, int(width)))
        if self._panel_ref is not None:
            self._panel_ref.setFixedWidth(w)

    def set_panel(self, panel) -> None:
        self._panel_ref = panel


class MainWindow(QMainWindow):
    #: Agent 执行过程中的流事件： (run_uid, kind, payload)
    #: kind = "plan" / "step"；后台线程 emit，Qt 自动排队到主线程。
    agent_event = Signal(str, str, object)

    def __init__(self):
        super().__init__()
        # 标准控件（输入框右键菜单、文件对话框）用 Qt 中文翻译
        install_qt_translations()
        self.agent_event.connect(self._on_agent_event)
        self._live_plans: dict[str, object] = {}
        self._active_plan_uid = ""
        self._tool_rerun_inflight = False
        self._pyramid_asked: set[str] = set()
        self._overview_workers: dict[str, PyramidWorker] = {}
        self._pending_confirm = None          # (StepConfirmGate, StepConfirmDialog)
        self._confirm_dialog = None
        self.setWindowTitle("GIS 矢量数据 Agent")
        self.setWindowIcon(make_icon())
        # 去掉 Windows 自带标题栏，窗口控制按钮画进顶部栏
        self.setWindowFlag(Qt.FramelessWindowHint, True)
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
        self._tmp_roots: list[Path] = []     # 导入弹窗里多个压缩包解压出的目录
        self._cleanup_worker: Worker | None = None
        self._load_queue: list[Path] = []    # 待载入的文件队列（串行处理）

        # 持久化：正常桌面运行落 data/agent.db；offscreen 测试/无头环境默认关闭，
        # 避免测试脚本互相污染会话历史。GIS_AGENT_PERSIST=1 可强制开启（测试用）。
        persist_env = os.environ.get("GIS_AGENT_PERSIST")
        self.persist = (persist_env == "1") or (
            persist_env != "0" and os.environ.get("QT_QPA_PLATFORM", "") != "offscreen")
        self.agent_db: AgentStore | None = None
        self.memory: MemoryManager | None = None
        self.project_id = ""
        self.session_id = ""
        self._synced_layers: dict[str, tuple] = {}
        self._interrupted_runs: list[str] = []
        if self.persist:
            try:
                self.agent_db = AgentStore()
                project = self.agent_db.ensure_default_project()
                self.project_id = project.id
                session = self.agent_db.latest_session(project.id)
                if session is None:
                    session = self.agent_db.create_session(project.id, "新会话")
                self.session_id = session.id
                self.memory = MemoryManager(self.agent_db, self.project_id, self.session_id)
                self.history = self.agent_db.latest_messages_text(self.session_id, 20)
                saved = self.agent_db.session_state(self.session_id)
                mem = saved.get("planner_memory") if isinstance(saved, dict) else None
                if isinstance(mem, dict):
                    self.planner.memory.update(mem)
                    # 重启后图层没有恢复，指向旧图层 id 的指代会失效
                    self.planner.memory["last_layer"] = None
                try:
                    self.planner.llm_stats.update(saved.get("llm_stats") or {})
                except Exception:
                    pass
                # 上次退出时没跑完的任务 → 标记 interrupted（不自动重跑）
                try:
                    self._interrupted_runs = recover_interrupted_runs(self.agent_db)
                except Exception:
                    self._interrupted_runs = []
            except Exception:
                self.persist = False
                self.agent_db = None
                self.memory = None
                self.history = []

        # 多任务：聊天/导出/生成算子进优先级队列，单 writer 串行执行
        self._task_bridge = _TaskBridge(self)
        self._task_bridge.event.connect(self._on_task_event, Qt.QueuedConnection)
        self.runtime = AgentRuntime(
            self.agent_db,
            on_event=lambda ev, task, payload: self._task_bridge.event.emit(task.id, ev, payload))
        # tasks 是 runtime 里的调度器（保留旧名字，兼容既有调用点）
        self.tasks = self.runtime.scheduler
        if self.agent_db is not None:
            self.runtime.bind_session(self.project_id, self.session_id)
        self._task_handlers: dict[str, dict] = {}
        self._task_retry: dict[str, dict] = {}
        self._current_task_id: str | None = None
        self._last_run_id: str = ""
        #: 待澄清的上下文：{"question": ..., "options": [...], "origin": 原始需求}
        self._pending_clarify: dict | None = None
        #: 用户对上一轮澄清的回答（带结构化上下文，供规划器使用）
        self._clarify_answer: dict | None = None
        #: 工具库折叠状态：key = "矢量|整理" / "kind|栅格"。刷新工具库时保留。
        self._tool_collapsed: set[str] = set()
        # Agent 设置 →「工具」里停用的工具：启动先按磁盘配置恢复。
        # 工具库渲染、大模型候选、执行前检查都读 app.tool_prefs 这一份状态。
        try:
            tool_prefs.set_disabled(load_cfg().get("tools_disabled") or [])
        except Exception:
            pass

        self.setStyleSheet(QSS)
        self._build()
        self._shortcuts()
        self._register_exit()
        self._refresh_planner_pill()
        if self.persist and self.history:
            self._restore_chat()
        else:
            self._greet()
        # 会话图层：只恢复引用（共享缓存/原始栅格），不复制到会话目录
        if self.persist and self.agent_db is not None:
            self._restore_session_layers()
            self.workspace.set_current(self.session_id)
        # 把 data/operators/ 里存过的自定义算子重新装回运行时
        try:
            installed = custom_ops.install_all()
            if installed:
                self.chat.add_agent("已载入 " + str(len(installed)) + " 个自定义工具："
                                    + "、".join("「" + s["title"] + "」" for s in installed))
        except Exception:
            pass
        # 上次退出时没跑完的任务 → 提示可重试（不自动重跑）
        if self._interrupted_runs:
            n = len(self._interrupted_runs)
            self.chat.add_agent(
                f"上次有 {n} 个任务没跑完（已标记为「未完成」）。"
                "它们不会自动重跑——你可以直接再说一次同样的需求。")
            self.set_status(f"{n} 个任务上次未完成")
        QTimer.singleShot(120, self._refresh_tools)
        self._update_chat_metrics()

    def _register_exit(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._shutdown_threads)
        atexit.register(self._shutdown_threads)

    def _shutdown_threads(self) -> None:
        """停掉渲染/任务线程；正常关窗、解释器退出、测试脚本直接退出都要走这里。"""
        pending = getattr(self, "_pending_confirm", None)
        if pending is not None:
            try:
                gate, dlg = pending
                gate.resolve("cancel", "")
                dlg.reject()
            except Exception:
                pass
            self._pending_confirm = None
        try:
            self.map_compare.shutdown()
        except Exception:
            pass
        try:
            self.map.shutdown()
        except Exception:
            pass
        for name in ("_worker", "_cleanup_worker"):
            w = getattr(self, name, None)
            try:
                if w is not None and w.isRunning():
                    w.wait(5000)
            except Exception:
                pass
        try:
            if getattr(self, "tasks", None) is not None:
                self.tasks.shutdown(wait=False)
        except Exception:
            pass
        try:
            if getattr(self, "agent_db", None) is not None:
                self.agent_db.close()
        except Exception:
            pass

    def closeEvent(self, e):  # noqa: N802
        """关窗时先停掉地图的渲染线程。

        地图控件内部有一个常驻渲染线程（QThread）。窗口关闭时如果它还在跑，
        QThread 被销毁会导致进程崩溃（"QThread: Destroyed while thread is
        still running"）——所以必须在这里显式停下来并等待退出。
        """
        # 有任务在跑时先问一句：等它跑完、还是直接取消（设计文档 §5.5）
        if not self._confirm_close_with_tasks():
            e.ignore()
            return
        # 关窗前把当前会话图层引用/缓存落盘，下次打开会话才能同步恢复
        try:
            self._persist_state(force_cache=True)
        except Exception:
            pass
        for worker in list(self._overview_workers.values()):
            try:
                worker.cancel()
                worker.wait(2000)
            except Exception:  # noqa: BLE001
                pass
        self._overview_workers.clear()
        try:
            self.map_compare.shutdown()
        except Exception:
            pass
        try:
            self.map.shutdown()
        except Exception:
            pass
        try:
            if self._worker is not None and self._worker.isRunning():
                # 长算子可能跑几十秒；关窗时先等它收尾，避免 QThread 被销毁导致崩溃
                self._worker.wait(15000)
        except Exception:
            pass
        try:
            if self._cleanup_worker is not None and self._cleanup_worker.isRunning():
                self._cleanup_worker.wait(5000)
        except Exception:
            pass
        # 取消所有还在跑/排队的任务，让 worker 线程尽快退出
        try:
            for t in self.tasks.active_tasks + self.tasks.pending_tasks():
                self.tasks.cancel(t.id)
        except Exception:
            pass
        try:
            self.tasks.shutdown(wait=True)
        except Exception:
            pass
        super().closeEvent(e)

    def _confirm_close_with_tasks(self) -> bool:
        """关窗前确认：还有任务在跑时给用户「等待 / 取消」的选择。

        返回 True 表示可以关闭。
        """
        try:
            n_run = self.tasks.running_count
            n_pending = self.tasks.pending_count
        except Exception:
            return True
        if not n_run and not n_pending:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("还有任务在跑")
        box.setIcon(QMessageBox.Question)
        box.setText(f"还有 {n_run} 个任务正在执行、{n_pending} 个在排队。")
        box.setInformativeText("可以等它们跑完再关，也可以现在取消并关闭。")
        wait_btn = box.addButton("等待完成", QMessageBox.AcceptRole)
        cancel_btn = box.addButton("取消并关闭", QMessageBox.DestructiveRole)
        box.setDefaultButton(wait_btn)
        box.exec()
        if box.clickedButton() is cancel_btn:
            for t in self.tasks.active_tasks + self.tasks.pending_tasks():
                try:
                    self.tasks.cancel(t.id)
                except Exception:
                    pass
            return True
        return True

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
        self.left_panel = self._build_left()
        bl.addWidget(self.left_panel)
        bl.addWidget(self._build_map(), 1)
        bl.addWidget(self._build_right())
        root.addWidget(body, 1)

        self._build_statusbar()

    # ---------------------------------------------------------------- 顶栏
    def _build_topbar(self) -> QWidget:
        bar = _TitleBar(self)
        bar.setObjectName("topbar")
        bar.setFixedHeight(58)
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(14, 0, 10, 0)
        hl.setSpacing(10)

        logo = QLabel()
        logo.setPixmap(make_icon().pixmap(34, 34))
        logo.setFixedSize(34, 34)
        logo.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        hl.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        t1 = QLabel("GIS 矢量数据 Agent")
        t1.setObjectName("brandTitle")
        t1.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        t2 = QLabel("说一句话，完成空间分析")
        t2.setObjectName("brandSub")
        t2.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        titles.addWidget(t1)
        titles.addWidget(t2)
        hl.addLayout(titles)
        hl.addStretch(1)

        self.pill = QLabel("● 规则引擎")
        self.pill.setObjectName("pill")
        self.pill.setAttribute(Qt.WA_TransparentForMouseEvents, True)
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

        self.btn_memory = QPushButton("◈ 记忆")
        self.btn_memory.setObjectName("ghost")
        self.btn_memory.setToolTip("查看和编辑 Agent 记住的偏好、项目事实和工作流")
        self.btn_memory.clicked.connect(self.open_memory_panel)
        hl.addWidget(self.btn_memory)

        self.btn_upload = QPushButton("↑ 上传数据")
        self.btn_upload.setObjectName("primary")
        self.btn_upload.clicked.connect(lambda _=False: self.open_import_dialog())
        hl.addWidget(self.btn_upload)

        self.btn_gdb = QPushButton("＋ GDB")
        self.btn_gdb.setObjectName("ghost")
        self.btn_gdb.setToolTip("打开 Esri FileGDB（.gdb 文件夹），支持一次导入多个要素类")
        self.btn_gdb.clicked.connect(self.choose_gdb)
        hl.addWidget(self.btn_gdb)

        # 头部最右侧：Agent 自己的设置（技能、逐步确认），和模型接口设置分开
        self.btn_agent_settings = QPushButton("⚙ Agent 设置")
        self.btn_agent_settings.setObjectName("ghost")
        self.btn_agent_settings.setToolTip("技能、工具库管理、逐步确认和 Agent 行为配置")
        self.btn_agent_settings.clicked.connect(self.open_agent_settings)
        hl.addWidget(self.btn_agent_settings)

        # 无边框窗口：最小化 / 最大化 / 关闭放在 Agent 设置右侧
        hl.addSpacing(4)
        self.btn_min = QPushButton("—")
        self.btn_min.setObjectName("winCtrl")
        self.btn_min.setFixedSize(24, 22)
        self.btn_min.setToolTip("最小化")
        self.btn_min.setCursor(Qt.PointingHandCursor)
        self.btn_min.clicked.connect(self.showMinimized)
        hl.addWidget(self.btn_min)

        self.btn_max = QPushButton("□")
        self.btn_max.setObjectName("winCtrl")
        self.btn_max.setFixedSize(24, 22)
        self.btn_max.setToolTip("最大化")
        self.btn_max.setCursor(Qt.PointingHandCursor)
        self.btn_max.clicked.connect(self.toggle_maximize)
        hl.addWidget(self.btn_max)

        self.btn_close = QPushButton("×")
        self.btn_close.setObjectName("winClose")
        self.btn_close.setFixedSize(24, 22)
        self.btn_close.setToolTip("关闭")
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.clicked.connect(self.close)
        hl.addWidget(self.btn_close)
        # 状态胶囊和右侧按钮同高（按钮高度由主题里的 padding 决定，这里量出来对齐）
        self.pill.setFixedHeight(self.btn_samples.sizeHint().height())
        self._sync_window_buttons()
        return bar

    def toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._sync_window_buttons()

    def _sync_window_buttons(self) -> None:
        btn = getattr(self, "btn_max", None)
        if btn is None:
            return
        if self.isMaximized():
            btn.setText("❐")
            btn.setToolTip("还原")
        else:
            btn.setText("□")
            btn.setToolTip("最大化")

    def changeEvent(self, e):  # noqa: N802
        super().changeEvent(e)
        if e.type() == QEvent.WindowStateChange:
            self._sync_window_buttons()

    # ---------------------------------------------------------------- 左栏
    def _build_left(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        panel.setFixedWidth(272)
        vl = QVBoxLayout(panel)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # 左栏上方：图层 / 工作区 两个页签；工具库仍固定在下方。
        self.side_tabs = QTabWidget()
        self.side_tabs.setObjectName("sideTabs")
        self.side_tabs.setDocumentMode(True)
        # QTabBar 默认会在 tab 上方画一条系统底边（#a0a0a0），在暖白面板上
        # 看起来像一条突兀的黑线；关掉，交给 QSS 的下划线表示选中。
        self.side_tabs.tabBar().setDrawBase(False)

        layer_page = QWidget()
        self.layer_page = layer_page
        lv = QVBoxLayout(layer_page)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)

        head = QHBoxLayout()
        head.setContentsMargins(14, 8, 12, 4)
        title = QLabel("数据图层")
        title.setObjectName("panelTitle")
        head.addWidget(title)
        self.count_badge = QLabel("0")
        self.count_badge.setObjectName("badge")
        head.addWidget(self.count_badge)
        head.addStretch(1)
        lv.addLayout(head)

        dz = QFrame()
        dz.setObjectName("dropzone")
        dz.setFixedHeight(76)
        dz.setCursor(Qt.PointingHandCursor)
        dzl = QVBoxLayout(dz)
        dzl.setContentsMargins(8, 8, 8, 8)
        dzl.setSpacing(1)
        d1 = QLabel("↑")
        d1.setAlignment(Qt.AlignCenter)
        d1.setStyleSheet("font-size:14px;color:#c0b9a7;")
        d2 = QLabel("拖入文件 / .gdb 文件夹，或点击导入")
        d2.setAlignment(Qt.AlignCenter)
        d2.setStyleSheet("font-size:11px;color:#43413c;font-weight:600;")
        d3 = QLabel("shp · GeoJSON · GPKG · GDB · CSV · TIF")
        d3.setAlignment(Qt.AlignCenter)
        d3.setObjectName("tiny")
        for w in (d1, d2, d3):
            # 点击文本也要落到 dropzone 的 mousePressEvent，否则只有边缘能点开导入弹窗
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            dzl.addWidget(w)
        dz.mousePressEvent = lambda e: self.open_import_dialog()  # type: ignore[assignment]
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(10, 0, 10, 7)
        wl.addWidget(dz)
        lv.addWidget(wrap)

        self.layer_list = QListWidget()
        self.layer_list.setObjectName("layerList")
        self.layer_list.setSpacing(0)
        self.layer_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.layer_list.customContextMenuRequested.connect(self._layer_menu)
        self.layer_list.itemDoubleClicked.connect(
            lambda item: self.rename_layer(item.data(Qt.UserRole)))
        self.layer_list.currentItemChanged.connect(self._on_layer_selected)
        lv.addWidget(self.layer_list, 1)

        self.side_tabs.addTab(layer_page, "图层")
        self.workspace = WorkspacePanel(
            sessions_provider=self._workspace_sessions,
            workspace_name=Path.cwd().name or "gis-agent")
        self.workspace.open_session.connect(self.open_session)
        self.workspace.new_session.connect(self.new_session)
        self.side_tabs.addTab(self.workspace, "工作区")
        self.side_tabs.currentChanged.connect(
            lambda i: self.workspace.refresh() if i == 1 else None)
        vl.addWidget(self.side_tabs, 2)

        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet("background:#e7e3d8;")
        vl.addWidget(div)

        head2 = QHBoxLayout()
        head2.setContentsMargins(14, 10, 12, 6)
        t2 = QLabel("工具库")
        t2.setObjectName("panelTitle")
        head2.addWidget(t2)
        self.tool_badge = QLabel("0")
        self.tool_badge.setObjectName("badge")
        head2.addWidget(self.tool_badge)
        head2.addStretch(1)
        self.new_op_btn = QPushButton("＋ 新建工具")
        self.new_op_btn.setObjectName("chip")
        self.new_op_btn.setCursor(Qt.PointingHandCursor)
        self.new_op_btn.setToolTip("用一段文字描述算法，让大模型生成一个新工具并加入工具库")
        self.new_op_btn.clicked.connect(self.prompt_new_operator)
        head2.addWidget(self.new_op_btn)
        vl.addLayout(head2)

        self.tool_scroll = QScrollArea()
        self.tool_scroll.setWidgetResizable(True)
        self.tool_scroll.setMinimumHeight(230)
        self.tool_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tool_host = QWidget()
        self.tool_box = QVBoxLayout(self.tool_host)
        self.tool_box.setContentsMargins(12, 0, 12, 10)
        self.tool_box.setSpacing(6)
        self.tool_box.setAlignment(Qt.AlignTop)
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
        self.map.picked.connect(self._on_pick)
        self.map.viewChanged.connect(self._on_view)
        self.map.cursorMoved.connect(self._on_cursor)
        self.map.progress.connect(self._on_map_progress)
        self.map.selectionChanged.connect(self._on_selection_changed)
        self.map.mapMenu.connect(self._map_menu)
        self.map.overviewNeeded.connect(self._on_overview_needed)
        self.map_compare = MapCompareWidget(self.map, card)
        grid.addWidget(self.map_compare, 0, 0)

        # 任务通知：漂浮在地图顶部中央，不占右栏高度。
        # 保留 task_center 别名，兼容旧测试/外部调用（它不是右侧那个旧面板）。
        self.task_notice = TaskNotice(snapshot=self.tasks.snapshot, parent=self.map)
        self.task_notice.cancel_requested.connect(self._cancel_task)
        self.task_notice.retry_requested.connect(self._retry_task)
        self.task_notice.log_requested.connect(self._show_task_log)
        self.task_center = self.task_notice

        zoom = QWidget()
        zl = QVBoxLayout(zoom)
        zl.setContentsMargins(12, 12, 12, 12)
        zl.setSpacing(6)
        for text, tip, fn in (("+", "放大", lambda: self.map.zoom_by(1)),
                              ("−", "缩小", lambda: self.map.zoom_by(-1)),
                              ("⤢", "全图", self.map.fit_all)):
            b = QPushButton(text)
            b.setObjectName("mapCtrl")
            b.setFixedSize(30, 30)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            zl.addWidget(b)
        self.compare_btn = QPushButton("◫")
        self.compare_btn.setObjectName("mapCtrl")
        self.compare_btn.setFixedSize(30, 30)
        self.compare_btn.setCheckable(True)
        self.compare_btn.setToolTip("地图对比")
        self.compare_btn.setCursor(Qt.PointingHandCursor)
        self.compare_btn.clicked.connect(self.toggle_map_compare)
        zl.addWidget(self.compare_btn)
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

    def toggle_map_compare(self) -> None:
        """进入/退出地图对比。"""
        if getattr(self, "map_compare", None) is None:
            return
        if self.map_compare.active:
            self._exit_map_compare()
            return
        rasters = [l for l in self.store.layers.values()
                   if getattr(l, "is_raster", False)]
        if len(rasters) < 2:
            self.compare_btn.setChecked(False)
            QMessageBox.information(
                self, "地图对比",
                "至少需要两个栅格图层才能对比。\n"
                "请先载入两个底图（例如两期影像或 DEM）。")
            return
        cur = self.store.get(self.current_layer)
        left_id = cur.id if getattr(cur, "is_raster", False) else rasters[0].id
        right_id = next((r.id for r in rasters if r.id != left_id), rasters[1].id)
        dlg = MapCompareDialog(rasters, left_id, right_id, "horizontal", self)
        if dlg.exec() != QDialog.Accepted:
            self.compare_btn.setChecked(False)
            return
        left_id, right_id, mode = dlg.selection()
        try:
            self.map_compare.activate(left_id, right_id, mode,
                                      list(self.store.layers.values()), self.visible)
        except Exception as e:  # noqa: BLE001
            self.compare_btn.setChecked(False)
            self._error("地图对比", str(e))
            return
        self.compare_btn.setChecked(True)
        self.compare_btn.setToolTip("退出地图对比")
        left_name = self.store.get(left_id).name
        right_name = self.store.get(right_id).name
        self.set_status(f"地图对比：左「{left_name}」/ 右「{right_name}」")

    def _exit_map_compare(self) -> None:
        if getattr(self, "map_compare", None) is None:
            return
        self.map_compare.deactivate()
        self.map.visible = self.visible
        self.compare_btn.setChecked(False)
        self.compare_btn.setToolTip("地图对比")
        self.refresh_layers(fit=False)
        self.set_status("已退出地图对比")

    # ---------------------------------------------------------------- 右栏
    def _chat_width(self) -> int:
        """对话面板宽度：从本地设置读，夹在合理范围内。"""
        try:
            w = int(load_cfg().get("chat_panel_width") or 0)
        except Exception:
            w = 0
        if w <= 0:
            w = ResizeHandle.DEFAULT_W
        return max(ResizeHandle.MIN_W, min(ResizeHandle.MAX_W, w))

    def _save_chat_width(self, width: int) -> None:
        """记住对话面板宽度，下次打开还是这个宽度。"""
        try:
            cfg = load_cfg()
            cfg["chat_panel_width"] = int(width)
            save_cfg(cfg)
        except Exception:
            pass
        self._sync_min_window_width()

    def _sync_min_window_width(self) -> None:
        """窗口最小宽度要装得下左栏 + 地图 + 对话面板。"""
        try:
            chat_w = self.chat_panel.width() if getattr(self, "chat_panel", None) else 412
            need = 272 + 320 + chat_w + 60
            self.setMinimumSize(max(1080, need), 640)
        except Exception:
            pass

    def _build_right(self) -> QWidget:
        holder = QWidget()
        holder.setObjectName("chatArea")
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)

        # 左边一条拖拽把手：左右拖动改对话面板宽度（双击回默认）
        self.chat_handle = ResizeHandle(self._save_chat_width)
        hl.addWidget(self.chat_handle)

        panel = QFrame()
        panel.setObjectName("chatPanel")
        panel.setFixedWidth(self._chat_width())
        hl.addWidget(panel, 1)
        self.chat_panel = panel
        self.chat_handle.set_panel(panel)

        vl = QVBoxLayout(panel)
        vl.setContentsMargins(0, 0, 0, 0)
        self.chat = ChatPanel(self)
        self.chat.sent.connect(self.on_send)
        self.chat.clear_btn.clicked.connect(self.clear_all)
        vl.addWidget(self.chat, 1)
        self._sync_min_window_width()
        return holder

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
        # Agent 任务取消：只对当前 writer 任务生效，在 step 边界停止
        self.st_task_cancel = QPushButton("停止任务")
        self.st_task_cancel.setObjectName("tiny")
        self.st_task_cancel.setCursor(Qt.PointingHandCursor)
        self.st_task_cancel.setToolTip("停止当前 Agent 任务（当前步骤跑完后停止）")
        self.st_task_cancel.clicked.connect(lambda _checked=False: self._cancel_active_task())
        self.st_task_cancel.hide()
        sb.addPermanentWidget(self.st_task_cancel)
        sb.addPermanentWidget(self.st_prog)
        sb.addPermanentWidget(self.st_msg)
        # 无边框窗口没有系统边框，右下角放一个尺寸手柄负责缩放整窗
        self.size_grip = QSizeGrip(sb)
        self.size_grip.setObjectName("sizeGrip")
        self.size_grip.setFixedSize(14, 14)
        sb.addPermanentWidget(self.size_grip)

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
        if self.map_compare.active:
            self.map_compare.refresh_layers(layers, self.visible)
            if not self.map_compare.active:
                self.compare_btn.setChecked(False)
        if fit:
            self.map.fit_all()
        self._refresh_legend()
        self.count_badge.setText(str(len(layers)))
        try:
            self.side_tabs.setTabText(0, f"图层 {len(layers)}" if layers else "图层")
        except Exception:
            pass
        try:
            self.workspace.refresh()
        except Exception:
            pass
        # 用 meta 的缓存计数，不要 len(l.gdf)：后者会把磁盘换出的图层重新读回内存
        # 栅格不计入「要素」总数（它的 count 是像元数，混进去会让数字失去意义）
        total = sum(int(l.meta().get("count") or 0) for l in layers
                    if not getattr(l, "is_raster", False))
        n_ras = sum(1 for l in layers if getattr(l, "is_raster", False))
        self.st_feat.setText(f"要素 {total:,}" + (f" · 栅格 {n_ras}" if n_ras else ""))
        self._sync_layer_registry()

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
        if getattr(self, "map_compare", None) is not None and self.map_compare.active:
            self.map_compare.set_layer_visibility(layer_id, bool(visible))
        # 显隐也立刻持久化：否则用户关掉大图层后直接关窗/崩溃，下次启动
        # 仍按旧的 visible 重建一次大图层的 LOD，出现一次 1 GB 瞬时峰值。
        QTimer.singleShot(0, self._persist_state)
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
        menu.addAction("图层样式…", lambda: self.open_layer_style(lid))
        menu.addAction("重命名…", lambda: self.rename_layer(lid))
        menu.addAction("复制图层", lambda: self.duplicate_layer(lid))
        show = menu.addMenu("显示")
        show.addAction("只显示此层", lambda: self.show_only(lid))
        show.addAction("显示全部", self.show_all)
        ex = menu.addMenu("导出")
        self._add_export_actions(ex, lay, lid)
        menu.addSeparator()
        menu.addAction("删除图层", lambda: self.delete_layer(lid))
        menu.exec(self.layer_list.mapToGlobal(pos))

    def _add_export_actions(self, menu: QMenu, lay, lid: str) -> None:
        """按图层类型填导出菜单：栅格复制原文件，矢量才给 GeoJSON/GPKG/SHP/CSV。"""
        if getattr(lay, "is_raster", False):
            menu.addAction("导出栅格文件（复制原文件）…",
                           lambda _=False: self.export_layer(lid))
            return
        for label, fmt in (("GeoJSON", "geojson"), ("GeoPackage", "gpkg"),
                           ("Shapefile (zip)", "shp"), ("CSV 属性表", "csv")):
            menu.addAction(label, lambda _=False, f=fmt: self.export_layer(lid, f))

    def open_layer_style(self, layer_id: str) -> None:
        """图层右键 →「图层样式…」：改渲染符号 / 要素标注。"""
        lay = self.store.get(layer_id)
        if lay is None:
            return
        dlg = LayerStyleDialog(lay, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.apply_layer_style(layer_id, dlg.style())
        self.set_status(f"已更新「{lay.name}」的样式")

    def apply_layer_style(self, layer_id: str, style: dict) -> None:
        """把弹窗结果写回图层，并让地图立刻按新样式重画。"""
        lay = self.store.get(layer_id)
        if lay is None or not isinstance(style, dict):
            return
        from app.layer_style import geom_kind, normalize_raster_style, normalize_vector_style

        if getattr(lay, "is_raster", False):
            lay.style = normalize_raster_style(style, getattr(lay, "style", None))
            lay.rev = int(getattr(lay, "rev", 0) or 0) + 1
            lay.stats = None
        else:
            try:
                geom = str((lay.meta() or {}).get("geomType") or "")
            except Exception:  # noqa: BLE001
                geom = ""
            st = normalize_vector_style(style, getattr(lay, "op", "") or "",
                                        getattr(lay, "color", "#2f6fed"), geom)
            lay.style = st
            # 图例小块 / 图层行左侧色块跟着主色走，否则「改了样式但色块没变」很怪
            kind = geom_kind(geom)
            lay.color = (st["point_color"] if kind == "point"
                         else st["stroke_color"] if kind == "line"
                         else st["fill_color"])
        self.map.refresh_layer_style(layer_id)
        # 图例/图层行要按新颜色重画
        self.refresh_layers()
        QTimer.singleShot(0, self._persist_state)

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
        if getattr(lay, "is_raster", False):
            # 栅格复制等于再挂一份同一个文件，没有意义；直接说清楚
            self.chat.add_agent(f"「{lay.name}」是栅格图层，不支持复制图层。")
            return
        new = self.store.add(lay.gdf.copy(), f"{lay.name}_副本", source=lay.id, op="copy",
                             color=getattr(lay, "color", None))
        # 复制图层 = 复制显示效果；样式不跟过去的话用户还得再配一遍标注
        new.style = dict(getattr(lay, "style", None) or {})
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
        if getattr(self, "map_compare", None) is not None and self.map_compare.active:
            for lid, vis in self.visible.items():
                self.map_compare.set_layer_visibility(lid, bool(vis))
        QTimer.singleShot(0, self._persist_state)
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
        paths, _ = QFileDialog.getOpenFileNames(self, "选择矢量/栅格数据", "", FILE_FILTER)
        if paths:
            self.load_paths(paths)

    def open_import_dialog(self, paths: list[str] | None = None) -> None:
        """点击拖拽区/上传入口：打开自定义导入弹窗，先选数据源再选图层。"""
        dlg = ImportDataDialog(self, start_paths=paths or [])
        if dlg.exec() != QDialog.Accepted:
            return
        entries = dlg.selections()
        roots = dlg.take_tmp_roots()
        if roots:
            self._tmp_roots.extend(roots)
        if not entries:
            self._cleanup_tmp()
            return
        files: list[Path] = []
        selections: dict[str, list[str]] = {}
        for e in entries:
            p = Path(e["path"])
            if p not in files:
                files.append(p)
            if e.get("layer"):
                selections.setdefault(str(p), []).append(str(e["layer"]))
        self._load_files(files, source="导入数据", selections=selections)

    def choose_gdb(self) -> None:
        """FileGDB 是文件夹，文件对话框选不了；单独用目录选择。"""
        d = QFileDialog.getExistingDirectory(
            self, "选择 Esri FileGDB 文件夹（.gdb）", "", QFileDialog.ShowDirsOnly)
        if not d:
            return
        p = Path(d)
        if p.suffix.lower() != ".gdb":
            QMessageBox.information(
                self, "不是 FileGDB",
                "请选择以 .gdb 结尾的文件夹（例如 2024年国土.gdb）。")
            return
        self.set_status(f"正在读取 FileGDB：{p.name}…")
        self.load_paths([d])

    def load_paths(self, paths: list[str]) -> None:
        # 导入不再被「有任务在跑」直接拒绝：排到当前任务后面即可
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
        if suffix in RASTER_EXTS:
            # 栅格也走载入队列：进度、取消、错误提示都复用同一套
            self._load_files([path], source=path.name)
            return True
        if suffix == ".gdb":
            # FileGDB 是目录；一个 GDB 可能含多个要素类，交给 _load_files 批量建层
            self._load_files([path], source=path.name)
            return True
        if suffix not in (".zip", ".shp", ".geojson", ".json", ".gpkg", ".kml", ".gml", ".csv"):
            QMessageBox.warning(self, "不支持的格式", f"{path.suffix} 暂时不支持。\n"
                                                     "请上传 zip/shp/geojson/gpkg/kml/csv、tif 栅格，"
                                                     "或点「＋ GDB」选择 FileGDB 文件夹。")
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
                    if is_raster_path(f):
                        # 栅格没有「要素」，列尺寸和波段数；file_info 走 pyogrio，
                        # 对 GeoTIFF 只会返回空，用户会看到「0 要素 · 未知类型」。
                        try:
                            ri = read_info(f)
                            info = {"kind": "raster", "features": 0,
                                    "geom": "", "crs": ri.crs,
                                    "width": ri.width, "height": ri.height,
                                    "bands": ri.bands}
                        except Exception:
                            info = {"kind": "raster", "features": 0, "geom": "",
                                    "crs": "", "width": 0, "height": 0, "bands": 0}
                    else:
                        info = {"kind": "vector", **file_info(f)}
                    cands.append({"id": i, "name": f.stem, "ext": f.suffix,
                                  "size_mb": round(f.stat().st_size / 1e6, 3), **info})
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
        roots = [root] if root is not None else []
        roots.extend(self._tmp_roots)
        self._tmp_roots = []
        if not roots:
            return

        def _rm() -> None:
            for r in roots:
                shutil.rmtree(r, ignore_errors=True)

        try:
            w = Worker(_rm)
            w.done.connect(lambda _r: None)
            w.failed.connect(lambda _msg: None)
            w.start()
            self._cleanup_worker = w
        except Exception:
            _rm()

    def _maybe_offer_pyramids(self, files: list[Path]) -> None:
        """大栅格没有 overview 时，询问并生成外部 .ovr（带进度）。"""
        for f in files:
            try:
                if not is_raster_path(f) or not Path(f).is_file():
                    continue
                key = str(Path(f).resolve())
                if key in self._pyramid_asked:
                    continue
                if has_overviews(f):
                    continue
                info = read_info(f)
                if info.width * info.height < 4_000_000:
                    continue
                factors = overview_factors(info.width, info.height)
                if not factors:
                    continue
                self._pyramid_asked.add(key)
                box = QMessageBox(self)
                box.setWindowTitle("生成栅格金字塔")
                box.setText(f"「{Path(f).name}」没有栅格金字塔。")
                box.setInformativeText(
                    "生成金字塔后缩放/渲染会快很多，也能显著降低大栅格的内存峰值。"
                    "会在 TIFF 旁创建 .ovr 文件，不修改原始影像。")
                gen = box.addButton("生成金字塔", QMessageBox.AcceptRole)
                box.addButton("暂不", QMessageBox.RejectRole)
                box.exec()
                if box.clickedButton() is not gen:
                    continue
                self._generate_pyramids(Path(f), factors)
            except Exception:
                continue

    def _on_overview_needed(self, path: str) -> None:
        """可见大栅格没有 overview：后台生成，不阻塞地图交互。

        这是 overview 管理的“自动补层”：首屏/首次缩放发现缺层时启动一次
        `PyramidWorker`，状态栏显示进度；完成后刷新该栅格所有图层的
        `info.overviews` 并让源块缓存失效，下一次缩放就会选到缩减层。
        """
        try:
            p = Path(path)
            if not p.is_file():
                return
            key = str(p.resolve())
            if key in self._pyramid_asked or key in self._overview_workers:
                return
            info = read_info(p)
            factors = overview_factors(info.width, info.height)
            if not factors:
                return
            if info.overviews:
                # 文件其实已有 overview，只是会话里的旧元信息没刷新。
                self._apply_overview_info(key, info)
                return
            self._pyramid_asked.add(key)
            worker = PyramidWorker(p, factors, self)
            worker._overview_key = key
            worker._overview_path = p
            self._overview_workers[key] = worker
            # 绑定到 MainWindow 的实例方法，Qt 会把回调排队回 GUI 线程；
            # 不要用无上下文的 lambda，否则状态栏/图层更新可能在 worker 线程执行。
            worker.progress.connect(self._on_overview_progress)
            worker.done.connect(self._on_overview_worker_done)
            self.set_status(f"后台生成栅格概览：{p.name}")
            worker.start()
        except Exception:  # noqa: BLE001
            pass

    def _on_overview_progress(self, value: int) -> None:
        worker = self.sender()
        key = getattr(worker, "_overview_key", "")
        if key:
            self.set_status(f"后台生成栅格概览 {Path(key).name}：{int(value)}%")

    def _on_overview_worker_done(self, ok: bool, msg: str) -> None:
        worker = self.sender()
        key = getattr(worker, "_overview_key", "")
        path = getattr(worker, "_overview_path", None)
        if key and path is not None:
            self._on_overview_done(key, path, bool(ok), str(msg))

    def _on_overview_done(self, key: str, path: Path, ok: bool, msg: str) -> None:
        worker = self._overview_workers.pop(key, None)
        if worker is not None:
            try:
                worker.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        if not ok:
            if "取消" not in (msg or ""):
                self.set_status(f"栅格概览生成失败：{Path(key).name}")
            return
        try:
            new_info = read_info(path)
        except Exception:  # noqa: BLE001
            return
        self._apply_overview_info(key, new_info)

    def _apply_overview_info(self, key: str, new_info) -> int:
        """把新读到的 overview 列表刷到所有引用同一文件的栅格图层。"""
        updated = 0
        for lay in list(self.store.layers.values()):
            if not getattr(lay, "is_raster", False):
                continue
            try:
                same = str(Path(lay.info.path).resolve()) == key
            except Exception:
                same = False
            if not same:
                continue
            lay.info.overviews = tuple(new_info.overviews or ())
            lay.stats = None
            lay.rev = int(getattr(lay, "rev", 0)) + 1
            try:
                self.map._raster.bump_rev(lay.id)
            except Exception:  # noqa: BLE001
                pass
            updated += 1
        if updated:
            self.set_status(f"栅格概览已就绪：{Path(key).name}")
            try:
                self.map.invalidate()
            except Exception:  # noqa: BLE001
                pass
        return updated

    def _generate_pyramids(self, path: Path, factors: list[int]) -> None:
        """QThread 生成 .ovr，QProgressDialog 显示真实 GDAL 进度。"""
        dlg = QProgressDialog(
            f"正在生成栅格金字塔：{path.name}", "取消", 0, 100, self)
        dlg.setWindowTitle("生成栅格金字塔")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        dlg.show()
        worker = PyramidWorker(path, factors, self)
        result: dict = {"done": False, "ok": False, "msg": "已取消"}
        loop = QEventLoop()

        def finish(ok: bool, msg: str) -> None:
            result.update(done=True, ok=bool(ok), msg=str(msg or ""))
            try:
                dlg.close()
            except Exception:
                pass
            loop.quit()

        worker.progress.connect(dlg.setValue)
        worker.done.connect(finish)
        dlg.canceled.connect(worker.cancel)
        self.set_status(f"正在生成栅格金字塔：{path.name}")
        worker.start()
        loop.exec()
        worker.wait(5000)
        try:
            worker.deleteLater()
        except Exception:
            pass
        if result["ok"]:
            self.set_status(f"金字塔已生成：{path.name}")
        elif "取消" not in result["msg"]:
            QMessageBox.warning(self, "生成金字塔失败",
                                result["msg"] or "GDAL 未生成金字塔。")
            self.set_status("生成金字塔失败")

    def _load_files(self, files: list[Path], source: str,
                    selections: dict[str, list[str]] | None = None) -> None:
        # 大栅格先问金字塔，避免首次渲染慢到像卡死
        self._maybe_offer_pyramids(files)
        self._set_busy(True)
        self._tick_load(files[0].name if files else source)
        # 只有解析数据时才暂停几何构建：gpd.read_file 在后台线程里会长时间
        # 占着 GIL，两边一起跑会互相饿死（实测 4 个大图层：11s 的活抢成 50s）。
        self.map.set_loading(True)
        gdb_files = [f for f in files if is_gdb_path(f)]
        if gdb_files:
            self.chat.add_agent(
                "正在读取 FileGDB：" + "、".join(f.name for f in gdb_files[:3])
                + ("…" if len(gdb_files) > 3 else "")
                + "（大库需要几秒到几十秒，读完会逐个建层）")

        def job(token):
            out, errors = [], []

            def tmp_source(f) -> bool:
                """压缩包解压目录在导入完成后会被清理，不能留给 provider 引用。"""
                roots = [self._tmp_root, *self._tmp_roots]
                try:
                    fp = Path(f).resolve()
                except Exception:
                    return False
                for root in roots:
                    if not root:
                        continue
                    try:
                        fp.relative_to(Path(root).resolve())
                        return True
                    except ValueError:
                        continue
                return False

            def add_vector(f, name, layer_name=None):
                """优先按 provider 登记（不读全量），失败再退回整份读取。

                ZIP 内的临时文件不能走 provider：导入结束 `_cleanup_tmp()` 会删目录。
                这类数据回退为整份读取，保证图层生命周期不依赖临时目录。
                """
                if tmp_source(f):
                    if layer_name:
                        gdf = read_layer(f, layer_name)
                    else:
                        gdf = load_any(f)
                    return self.store.add(gdf, name, source=source, op="load")
                try:
                    return self.store.add_source(
                        f, name, layer=layer_name, source=source, op="load")
                except Exception:
                    if layer_name:
                        gdf = read_layer(f, layer_name)
                    else:
                        gdf = load_any(f)
                    return self.store.add(gdf, name, source=source, op="load")

            for f in files:
                token.raise_if_cancelled()
                try:
                    # 栅格只读头信息；矢量优先 provider（只读文件头，渲染按 bbox 取），
                    # CSV / FileGDB 等没有 provider 的格式才退回整份读取
                    if is_raster_path(f):
                        info = read_info(f)
                        lay = self.store.add_raster(info, f.stem, source=source, op="load")
                        out.append(lay)
                    elif is_gdb_path(f):
                        # 从导入弹窗进来时只读用户勾选的要素类；
                        # 拖拽/直接载入没给 selections，就整库导入。
                        wanted = (selections or {}).get(str(f))
                        def on_progress(i, total, name):
                            try:
                                self.map.progress.emit(
                                    f"读取 FileGDB {i}/{total} · {name}")
                            except Exception:
                                pass

                        for layer_name, gdf in read_gdb_layers(
                                f, layer_names=wanted, on_progress=on_progress):
                            token.raise_if_cancelled()
                            lay = self.store.add(
                                gdf, f"{f.stem}/{layer_name}", source=source, op="load")
                            out.append(lay)
                    elif f.suffix.lower() == ".gpkg" and (selections or {}).get(str(f)):
                        # GPKG 和 GDB 一样是「一个文件多个图层」
                        for layer_name in selections[str(f)]:
                            token.raise_if_cancelled()
                            try:
                                self.map.progress.emit(f"读取 {f.name} · {layer_name}")
                            except Exception:
                                pass
                            lay = add_vector(
                                f, f"{f.stem}/{layer_name}", layer_name=layer_name)
                            out.append(lay)
                    else:
                        lay = add_vector(f, f.stem)
                        out.append(lay)
                except Exception as e:
                    errors.append(f"{f.name}：{e}")
            return out, errors

        # 导入也走调度器：排在 Agent 任务之后，不再直接拒绝
        task = self.runtime.submit_run(job, kind="import",
                                       label=f"导入 {files[0].name if files else source}",
                                       priority=PRIORITY_IMPORT)
        self._task_handlers[task.id] = {"kind": "import", "user": source}
        self._update_task_ui()

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
                describe_layer(l) for l in layers))
            # 会话图层引用入库（数据仍在共享缓存/原文件，不复制到会话目录）
            self._persist_state()
        if errors:
            detail = "\n".join(errors[:6])
            QMessageBox.warning(self, "部分文件未载入", detail)
            # 弹窗可能被主窗口挡住 / 用户点得快；对话区留一份可回溯的失败原因。
            self.chat.add_agent("这些文件没有载入成功：\n" + detail)
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

        def job(token):
            import geopandas as gpd
            out = []
            for f in ensure_samples():
                token.raise_if_cancelled()
                gdf = gpd.read_file(f)
                if gdf.crs is None:
                    gdf = gdf.set_crs(4326)
                out.append(self.store.add(gdf, f.stem, source=f.name, op="sample"))
            return out, []

        task = self.runtime.submit_run(job, kind="import", label="生成示例数据",
                                       priority=PRIORITY_IMPORT)
        self._task_handlers[task.id] = {"kind": "import", "user": "示例数据"}
        self._update_task_ui()

    def clear_all(self) -> None:
        if self.tasks.running_count or self.tasks.pending_count:
            QMessageBox.information(self, "正在执行", "还有 Agent 任务在跑，先等它结束或停止任务。")
            return
        # 保留旧会话（DB 里可追溯），当前窗口开一个新会话
        if self.persist and self.agent_db is not None:
            self._persist_state(force_cache=True)
            try:
                session = self.agent_db.create_session(self.project_id, "新会话")
                self.session_id = session.id
                if self.memory is not None:
                    self.memory.session_id = self.session_id
                # 新会话要重新绑定 runtime，否则 run 会挂到旧 session 上
                self.runtime.bind_session(self.project_id, self.session_id)
            except Exception:
                pass
        self.store.clear(preserve_cache=True)
        self.visible.clear()
        self.current_layer = None
        self.history.clear()
        self.planner.memory.update({"last_tool": "", "last_args": {}, "last_layer": None,
                                    "last_plan": [], "last_lines": []})
        try:
            self.planner.llm_stats.update({"calls": 0, "prompt_tokens": 0,
                                           "completion_tokens": 0,
                                           "cache_hit_tokens": 0, "seconds": 0.0})
        except Exception:
            pass
        self._pending_clarify = None
        self._live_plans.clear()
        self._active_plan_uid = ""
        self.map.drop_cache()
        self.chat.clear()
        self._persist_state()
        self.refresh_layers()
        self.workspace.refresh()
        self.workspace.set_current(self.session_id)
        self._greet()
        self._update_chat_metrics()
        self.set_status("已清空")

    # ================================================================== 对话
    def _restore_chat(self) -> None:
        """把上次会话的消息回填到对话面板。

        文本之外还要重建 `clarify` / `plan` / `steps` 三类 part：用户截图里的
        问题卡和工具调用计划卡，重启后如果只剩一行普通文字，等于上下文丢了。
        旧会话没有这些 part（当时只存了 text），仍按纯文本恢复。
        """
        if not self.persist or self.agent_db is None:
            self._greet()
            return
        try:
            msgs = self.agent_db.list_messages(self.session_id, limit=20)
        except Exception:
            msgs = []
        if not msgs:
            self._greet()
            return
        restored = 0
        for m in msgs:
            if m.role == "user":
                txt = m.text()
                if txt:
                    self.chat.add_user(txt)
                    restored += 1
            elif m.role == "assistant":
                self._restore_assistant(m)
                restored += 1
        self.set_status(f"已恢复上次会话（{restored} 条消息）")

    @staticmethod
    def _part(m, kind: str) -> Part | None:
        for p in (m.parts or []):
            if p.type == kind:
                return p
        return None

    def _restore_assistant(self, m: Message) -> None:
        """恢复一条 assistant 消息，按 part 顺序重建卡片和文本。"""
        clarify = self._part(m, "clarify")
        if clarify is not None:
            content = clarify.content or {}
            opts = [str(o) for o in (content.get("options") or []) if str(o).strip()]
            try:
                card = self.chat.add_clarify(str(content.get("question") or "需要确认一下："),
                                            opts, lambda _text: None)
                self.chat.restore_clarify(
                    card, str(content.get("chosen") or "") or None,
                    str(content.get("state") or "open"),
                    str(content.get("note") or ""))
                # 问题卡的题干就是这一条文本，不要再在下面重复一个气泡
                return
            except Exception:
                # 卡片坏了也至少把文本恢复出来，不能整条消息消失
                pass

        plan = self._part(m, "plan")
        if plan is not None:
            content = plan.content or {}
            plan_rows = content.get("plan") or []
            if plan_rows:
                try:
                    card = PlanCard(plan_rows, str(content.get("source") or ""), self)
                    for i, st in enumerate(plan_rows):
                        card.update_step(i, st if isinstance(st, dict) else {})
                    card.finish(failed=any(
                        str(st.get("status") or "") == "error"
                        for st in plan_rows if isinstance(st, dict)))
                    self.chat.add_plan(card)
                except Exception:
                    pass

        steps_part = self._part(m, "steps")
        steps = []
        if steps_part is not None:
            steps = [s for s in (steps_part.content or {}).get("steps") or []
                     if isinstance(s, dict)]
        txt = m.text()
        if txt and steps:
            self.chat.add_agent(txt, steps)
        elif txt:
            self.chat.add_agent(txt)
        elif steps:
            self.chat.add_steps(steps)

    def _persist_state(self, force_cache: bool = False) -> None:
        """保存会话状态。

        图层数据不按会话复制：矢量统一落在共享 `data/cache/`，这里只存
        图层 id、缓存路径/栅格信息、显隐和当前选中；切会话/关窗时才强制
        把所有矢量层落盘（`force_cache=True`），平时只更新引用。
        """
        if not self.persist or self.agent_db is None:
            return
        try:
            state = {
                "planner_memory": self.planner.memory,
                "llm_stats": getattr(self.planner, "llm_stats", {}),
                "layers": self._layer_descriptors(force_cache),
                "visible": {lid: bool(v) for lid, v in self.visible.items()},
                "current_layer": self.current_layer or "",
            }
            self.agent_db.save_session_state(self.session_id, state)
            if getattr(self, "workspace", None) is not None:
                self.workspace.refresh()
        except Exception:
            pass

    def _layer_descriptors(self, force_cache: bool = False) -> list[dict]:
        out: list[dict] = []
        for lay in self.store.layers.values():
            try:
                meta = lay.meta()
            except Exception:
                meta = {}
            desc = {
                "id": lay.id, "name": lay.name,
                "color": getattr(lay, "color", ""),
                "source": getattr(lay, "source", ""),
                "parent": getattr(lay, "parent", ""),
                "op": getattr(lay, "op", ""),
                "created_at": getattr(lay, "created_at", 0),
                "kind": "raster" if getattr(lay, "is_raster", False) else "vector",
                "count": meta.get("count") or 0,
                # 样式（颜色/线宽/标注字段）：随会话保存，切换/重开都还在
                "style": dict(getattr(lay, "style", None) or {}),
                "source_path": getattr(lay, "source_path", "") or "",
                "source_layer": getattr(lay, "source_layer", "") or "",
                # 在线地图：URL/加载方式随会话保存，恢复时重新取瓦片，不缓存图片
                "online": dict(getattr(lay, "online", None) or {}) or None,
            }
            if desc["kind"] == "raster":
                try:
                    desc["info"] = lay.info.as_dict()
                except Exception:
                    desc["info"] = {}
                desc["path"] = (desc["info"] or {}).get("path") or ""
                if desc["online"]:
                    # 在线图层没有本地文件；info 里的 path 只是显示用的 URL，
                    # 以当前 online.url 为准，避免修复/编辑配置后仍保存旧地址。
                    online_url = str(desc["online"].get("url") or "")
                    if online_url:
                        desc["path"] = online_url
                        desc["info"]["path"] = online_url
            else:
                path = getattr(lay, "cache_path", "") or ""
                if (force_cache and not (path and Path(path).exists())
                        and not desc.get("source_path")):
                    path = self.store.cache_layer(lay) or path
                desc["cache_path"] = path
            out.append(desc)
        return out

    def _restore_session_layers(self) -> None:
        """从 session_state 恢复图层；只引用共享缓存，不复制文件。"""
        if not self.persist or self.agent_db is None:
            return
        try:
            state = self.agent_db.session_state(self.session_id) or {}
        except Exception:
            state = {}
        descs = state.get("layers") or []
        if not descs:
            return
        self.store.clear(preserve_cache=True)
        errors: list[str] = []
        for d in descs:
            try:
                lid = str(d.get("id") or "")
                name = str(d.get("name") or "图层")
                if not lid:
                    continue
                if str(d.get("kind") or "vector") == "raster":
                    from app.raster import RasterInfo, RasterLayer
                    from app.raster import read_info
                    info_dict = d.get("info") or {}
                    path = str(d.get("path") or info_dict.get("path") or "")
                    if path and Path(path).is_file():
                        try:
                            info = read_info(path)
                        except Exception:
                            info = RasterInfo(**info_dict)
                    else:
                        info = RasterInfo(**info_dict)
                    lay = RasterLayer(
                        id=lid, name=name, info=info,
                        color=str(d.get("color") or "#8d897e"),
                        source=str(d.get("source") or ""),
                        parent=str(d.get("parent") or ""),
                        op=str(d.get("op") or "raster"),
                        created_at=float(d.get("created_at") or time.time()),
                        style=d.get("style"),
                        online=d.get("online"))
                else:
                    source_path = str(d.get("source_path") or "")
                    cache = str(d.get("cache_path") or "")
                    if source_path and Path(source_path).exists():
                        # provider 恢复：只记源文件，不依赖全量缓存
                        lay = Layer(
                            id=lid, name=name, gdf=None,
                            color=str(d.get("color") or "#2f6fed"),
                            source=str(d.get("source") or ""),
                            parent=str(d.get("parent") or ""),
                            op=str(d.get("op") or ""),
                            created_at=float(d.get("created_at") or time.time()),
                            source_path=source_path,
                            source_layer=str(d.get("source_layer") or "") or None,
                            style=d.get("style"))
                    elif cache and Path(cache).is_file():
                        lay = Layer(
                            id=lid, name=name, gdf=None,
                            color=str(d.get("color") or "#2f6fed"),
                            source=str(d.get("source") or ""),
                            parent=str(d.get("parent") or ""),
                            op=str(d.get("op") or ""),
                            created_at=float(d.get("created_at") or time.time()),
                            cache_path=cache,
                            style=d.get("style"))
                    else:
                        errors.append(f"{name}（源文件和缓存都不存在）")
                        continue
                if not getattr(lay, "is_raster", False):
                    # 关窗前的会话把矢量元信息也存了 count；恢复时先用
                    # provider 文件头/parquet 元数据填 `_meta`，绝不要为了
                    # 图层列表里的 “21,217 个要素” 去 `gpd.read_parquet()`，
                    # 那种缓存（几千万顶点）会把内存直接推到 GB 级。
                    pm: dict = {}
                    try:
                        if lay.provider is not None:
                            pm = lay.provider.meta()
                    except Exception:
                        pm = {}
                    lay._meta = {
                        "id": lid,
                        "name": name,
                        "color": lay.color,
                        "count": int(d.get("count") or pm.get("count") or 0),
                        "geomType": str(pm.get("geomType") or "Unknown"),
                        "crs": str(pm.get("crs") or "未定义"),
                        "crsName": str(pm.get("crsName") or pm.get("crs") or "未定义"),
                        "bounds": list(pm.get("bounds") or []),
                        "fields": list(pm.get("fields") or []),
                        "source": lay.source,
                        "parent": lay.parent,
                        "op": lay.op,
                    }
                self.store.layers[lid] = lay
                self.store._n += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{d.get('name') or '?'}：{e}")
        # 注意必须在原 dict 上更新：MapView.visible 持有的是这个对象的引用，
        # 重新赋值会让地图继续读旧 dict，复选框关了图层地图还在画。
        self.visible.clear()
        self.visible.update(
            {lid: bool((state.get("visible") or {}).get(lid, True))
             for lid in self.store.layers})
        cur = str(state.get("current_layer") or "")
        self.current_layer = cur if cur in self.store.layers else (
            next(reversed(self.store.layers), None) if self.store.layers else None)
        self.refresh_layers(fit=True)
        if errors:
            self.chat.add_agent("这些图层未能恢复：\n" + "\n".join(errors[:6]))

    def _workspace_sessions(self) -> list[dict]:
        """工作区的会话列表（含每个会话的图层引用）。"""
        now = time.time()
        if not self.persist or self.agent_db is None:
            return [{
                "id": self.session_id or "local",
                "title": "当前会话",
                "updated_at": now,
                "layer_count": len(self.store.layers),
                "layer_names": [l.name for l in self.store.layers.values()],
            }]
        out = []
        try:
            sessions = self.agent_db.list_sessions(self.project_id, limit=100)
        except Exception:
            sessions = []
        for s in sessions:
            try:
                st = self.agent_db.session_state(s.id) or {}
            except Exception:
                st = {}
            descs = st.get("layers") or []
            names = [str(d.get("name") or "") for d in descs if d.get("name")]
            if s.id == self.session_id:
                names = [l.name for l in self.store.layers.values()] or names
            out.append({
                "id": s.id,
                "title": str(s.title or "新会话"),
                "updated_at": s.updated_at,
                "layer_count": len(names),
                "layer_names": names,
            })
        return out

    def open_session(self, session_id: str) -> None:
        """打开工作区里的一个历史会话：恢复消息 + 同步恢复图层。"""
        if not self.persist or self.agent_db is None:
            return
        session_id = str(session_id or "")
        if not session_id or session_id == self.session_id:
            return
        if self.tasks.running_count or self.tasks.pending_count:
            QMessageBox.information(self, "任务执行中", "先等当前任务结束，再切换会话。")
            return
        sess = self.agent_db.get_session(session_id)
        if sess is None:
            return
        # 先把当前会话的图层状态和缓存落盘
        self._persist_state(force_cache=True)
        self.session_id = session_id
        if self.memory is not None:
            self.memory.session_id = session_id
        self.runtime.bind_session(self.project_id, session_id)
        try:
            state = self.agent_db.session_state(session_id) or {}
        except Exception:
            state = {}
        self.history = self.agent_db.latest_messages_text(session_id, 20)
        self.planner.memory.update(state.get("planner_memory") or {
            "last_tool": "", "last_args": {}, "last_layer": None,
            "last_plan": [], "last_lines": []})
        try:
            self.planner.llm_stats.update(state.get("llm_stats") or {})
        except Exception:
            pass
        # 会话里的大栅格如果没有金字塔，切过去前也询问一次
        raster_paths = [Path(str(d.get("path") or ""))
                        for d in (state.get("layers") or [])
                        if str(d.get("kind") or "") == "raster"
                        and d.get("path")]
        if raster_paths:
            self._maybe_offer_pyramids(raster_paths)
        self._pending_clarify = None
        self._clarify_answer = None
        self._tool_rerun_inflight = False
        self._last_run_id = ""
        self._live_plans.clear()
        self._active_plan_uid = ""
        self.store.clear(preserve_cache=True)
        self.visible.clear()
        self.current_layer = None
        self.map.drop_cache()
        self.chat.clear()
        self._restore_chat()
        self._restore_session_layers()
        self.workspace.set_current(self.session_id)
        self.workspace.refresh()
        self._update_chat_metrics()
        self.set_status(f"已打开会话「{sess.title or '新会话'}」")

    def new_session(self) -> None:
        """新建会话；旧会话保留在工作区，图层缓存不删。"""
        if self.tasks.running_count or self.tasks.pending_count:
            QMessageBox.information(self, "任务执行中", "先等当前任务结束，再新建会话。")
            return
        if not self.persist or self.agent_db is None:
            self.clear_all()
            return
        self._persist_state(force_cache=True)
        try:
            sess = self.agent_db.create_session(self.project_id, "新会话")
        except Exception as e:  # noqa: BLE001
            self._error("新建会话失败", str(e))
            return
        self.session_id = sess.id
        if self.memory is not None:
            self.memory.session_id = sess.id
        self.runtime.bind_session(self.project_id, sess.id)
        self.history = []
        self.planner.memory.update({"last_tool": "", "last_args": {}, "last_layer": None,
                                    "last_plan": [], "last_lines": []})
        try:
            self.planner.llm_stats.update({"calls": 0, "prompt_tokens": 0,
                                           "completion_tokens": 0,
                                           "cache_hit_tokens": 0, "seconds": 0.0})
        except Exception:
            pass
        self._pending_clarify = None
        self._clarify_answer = None
        self._tool_rerun_inflight = False
        self._last_run_id = ""
        self._live_plans.clear()
        self._active_plan_uid = ""
        self.store.clear(preserve_cache=True)
        self.visible.clear()
        self.current_layer = None
        self.map.drop_cache()
        self.chat.clear()
        self._greet()
        self.refresh_layers()
        self.workspace.refresh()
        self.workspace.set_current(self.session_id)
        self._update_chat_metrics()
        self.set_status("已新建会话")

    def _sync_layer_registry(self) -> None:
        """把当前图层的元信息写入 project 层表；只写有变化的图层。"""
        if not self.persist or self.agent_db is None:
            return
        current = set(self.store.layers)
        for lid, lay in self.store.layers.items():
            try:
                meta = lay.meta()
            except Exception:
                continue
            sig = (lay.name, meta.get("color"), meta.get("count"), meta.get("crs"),
                   tuple(meta.get("fields") or []), getattr(lay, "cache_path", ""))
            if self._synced_layers.get(lid) == sig:
                continue
            try:
                self.agent_db.upsert_layer(LayerRecord(
                    id=lid, project_id=self.project_id, name=lay.name,
                    parent_id=getattr(lay, "parent", "") or "",
                    op=getattr(lay, "op", "") or "",
                    crs=str(meta.get("crs") or ""),
                    geom_type=str(meta.get("geomType") or ""),
                    feature_count=int(meta.get("count") or 0),
                    bounds=list(meta.get("bounds") or []),
                    fields=list(meta.get("fields") or []),
                    cache_path=getattr(lay, "cache_path", "") or "",
                    content_hash="",
                    created_by_run="",
                    created_at=float(getattr(lay, "created_at", time.time()))))
                self._synced_layers[lid] = sig
            except Exception:
                pass
        for lid in list(self._synced_layers):
            if lid not in current:
                try:
                    self.agent_db.mark_layer_deleted(lid)
                except Exception:
                    pass
                self._synced_layers.pop(lid, None)

    def _greet(self) -> None:
        self.chat.add_agent(
            "我是你的空间分析助手。数据拖进来后，直接用中文说需求就行，例如：\n"
            "· 对「地块」做 500 米缓冲区，按用地类型融合\n"
            "· 用「行政区」裁剪「地块」，再统计各类面积\n"
            "· 给「学校」做 1 公里覆盖圈，看覆盖了哪些地块\n\n"
            "每一步运算都会生成新图层，原数据只读不改。")

    def _refresh_tools(self) -> None:
        """工具库：先分「矢量 / 栅格」，再按功能分组，每组可收缩。

        一级分类（矢量 / 栅格）和二级功能分组都是 `CollapsibleSection`；
        组内工具仍两列排开。折叠状态存在 `self._tool_collapsed`，刷新
        （载入自定义工具、新建工具后）不会把用户展开的面板重新弹开。
        """
        while self.tool_box.count():
            it = self.tool_box.takeAt(0)
            w = it.widget()
            if w is not None:
                # 立即摘掉旧 widget：只 deleteLater 的话，它到下一轮 DeferredDelete
                # 之前仍会作为子控件画在旧坐标上，刷新瞬间出现「残影」。
                w.hide()
                w.setParent(None)
                w.deleteLater()

        from app.agent import TOOL_GROUP, TOOL_LABEL, TOOL_SPECS
        TOOL_SPEC_BY_NAME.clear()
        TOOL_SPEC_BY_NAME.update({t["name"]: t.get("desc", "") for t in TOOL_SPECS})
        TOOL_SPEC_GROUP.clear()
        TOOL_SPEC_GROUP.update(TOOL_GROUP)

        self.tool_box.setContentsMargins(12, 0, 12, 10)

        # 在线地图不是 Agent 算子（不需要大模型、也不经过 OPERATIONS），
        # 单独放一个固定入口；放在最上面，找底图时不用先展开矢量/栅格。
        online_key = "base|在线底图"
        online_sec = CollapsibleSection(
            "底图", 1, level="group",
            collapsed=online_key in self._tool_collapsed)
        online_btn = QPushButton("＋ 在线地图")
        online_btn.setObjectName("chip")
        online_btn.setCursor(Qt.PointingHandCursor)
        online_btn.setToolTip(
            "添加 XYZ / WMTS / WMS 在线底图（含天地图；支持 Token、自定义参数和请求头）")
        online_btn.clicked.connect(self.open_online_map_dialog)
        online_sec.add_chip(online_btn, span=2)
        online_sec.header.toggled.connect(
            lambda checked, k=online_key: self._remember_tool_section(k, checked))
        self.tool_box.addWidget(online_sec)

        total = 0
        for kind, sections in self._tool_tree():
            sections = [(title, names) for title, names in sections if names]
            if not sections:
                continue
            kind_count = sum(len(names) for _, names in sections)
            kind_key = f"kind|{kind}"
            kind_sec = CollapsibleSection(
                kind, kind_count, level="kind",
                collapsed=kind_key in self._tool_collapsed)
            for title, names in sections:
                key = f"{kind}|{title}"
                sec = CollapsibleSection(
                    title, len(names), level="group",
                    collapsed=key in self._tool_collapsed)
                for name in names:
                    b = QPushButton(TOOL_LABEL.get(name, name))
                    b.setObjectName("toolChip")
                    b.setCursor(Qt.PointingHandCursor)
                    b.setToolTip(TOOL_SPEC_BY_NAME.get(name, "") or name)
                    b.clicked.connect(lambda _=False, n=name: self._insert_template(n))
                    sec.add_chip(b)
                sec.header.toggled.connect(
                    lambda checked, k=key: self._remember_tool_section(k, checked))
                kind_sec.add_section(sec)
            kind_sec.header.toggled.connect(
                lambda checked, k=kind_key: self._remember_tool_section(k, checked))
            self.tool_box.addWidget(kind_sec)
            total += kind_count
        self.tool_badge.setText(str(total))
        # 停用的工具不显示，但要让用户知道它们去哪了，否则会以为工具「丢了」。
        from app.engine import OPERATIONS
        hidden = len([n for n in tool_prefs.disabled_names() if n in OPERATIONS])
        self.tool_badge.setToolTip(
            f"共 {total + hidden} 个工具" +
            (f"，其中 {hidden} 个已停用（Agent 设置 →「工具」）" if hidden else "") +
            "；底图「在线地图」是独立入口，不计入算子数")
        # 让 QScrollArea 的 host 立刻按新内容算高度；否则首次刷新后
        # 面板会停留在视口高度，折叠/展开也不跟手。
        self.tool_box.activate()
        self.tool_host.adjustSize()

    def _remember_tool_section(self, key: str, expanded: bool) -> None:
        if expanded:
            self._tool_collapsed.discard(key)
        else:
            self._tool_collapsed.add(key)
        # 隐藏/显示 body 后重新计算滚动内容高度，避免出现一大片空白或内容被裁掉。
        self.tool_box.activate()
        self.tool_host.adjustSize()

    @staticmethod
    def _tool_tree(include_disabled: bool = False) -> list[tuple[str, list[tuple[str, list[str]]]]]:
        """返回两级工具分类：[("矢量", [("概览", [...]), ...]), ("栅格", [...])]。

        具体分类在 `app.tool_catalog.tool_tree()`，和 Agent 设置里的「工具」页
        共用同一份，避免两边显示的工具集合不一致。默认隐藏被停用的工具。
        """
        return tool_tree(include_disabled=include_disabled)

    @staticmethod
    def _tool_groups() -> list[tuple[str, list[str]]]:
        """扁平版分类（兼容旧调用/测试）：栅格分组加「栅格 · 」前缀区分。"""
        out: list[tuple[str, list[str]]] = []
        for kind, sections in MainWindow._tool_tree():
            for title, names in sections:
                label = title if kind == "矢量" else f"{kind} · {title}"
                out.append((label, names))
        return out

    def _insert_template(self, tool: str) -> None:
        cur = self.store.get(self.current_layer)
        lname = cur.name if cur else (next(iter(self.store.layers.values())).name
                                      if self.store.layers else "图层")
        other = next((l.name for l in self.store.layers.values() if l.name != lname), "边界")
        tpl = TOOL_TEMPLATES.get(tool, TOOL_LABEL_FALLBACK(tool))
        self.chat.input.setPlainText(tpl.replace("{L}", lname).replace("{O}", other))
        self.chat.input.setFocus()

    def open_online_map_dialog(self) -> None:
        """工具库 →「底图 / 在线地图」：填地址、加载方式、名称后加入图层。"""
        dlg = OnlineMapDialog(self)
        accepted = dlg.exec() == QDialog.Accepted
        if not accepted:
            dlg.deleteLater()
            self.set_status("已取消添加在线地图")
            return
        try:
            cfg = dlg.config()
        except Exception as e:  # noqa: BLE001
            dlg.deleteLater()
            self._error("在线地图", str(e))
            return
        dlg.deleteLater()
        self.add_online_map(cfg)

    def add_online_map(self, cfg: dict) -> None:
        """登记一个在线底图图层，并放在图层栈最底部（底图不压住矢量）。"""
        from app.online_map import method_label, online_raster_info

        cfg = dict(cfg or {})
        info = online_raster_info(cfg)
        had_layers = bool(self.store.layers)
        lay = self.store.add_raster(
            info, str(cfg.get("name") or "在线地图"),
            source=str(cfg.get("url") or ""), op="online", online=cfg)
        # 新图层默认追加在列表尾部（图层面板里显示在最上面）。底图应该排在
        # 图层栈最下面，否则会盖住已有的地块/道路；这里把字典顺序调整一下。
        items = [(lid, l) for lid, l in self.store.layers.items() if lid != lay.id]
        items.insert(0, (lay.id, lay))
        self.store.layers = dict(items)
        self.visible[lay.id] = True
        self.current_layer = lay.id
        self._push_undo({"kind": "add", "lids": [lay.id], "layers": [(lay.id, lay)]})
        self.refresh_layers(fit=not had_layers)
        self.chat.add_agent(
            f"已加载在线地图「{lay.name}」（{method_label(cfg.get('method'))}）。"
            "它在图层面板里可以像普通底图一样显隐、缩放定位和删除。")
        self.set_status(f"已加载在线地图「{lay.name}」")
        self._persist_state()

    def on_send(self, text: str, display: str | None = None,
                _auto_tool_retry: bool = False) -> None:
        """处理一条用户输入。

        ``display`` 是给界面看的文本（默认跟 text 一样）。
        澄清场景下：发给模型的是「原始需求 + 问题的完整描述」，
        但界面上只需要显示用户点的那个选项，读起来才自然。
        """
        if not _auto_tool_retry:
            self._tool_rerun_inflight = False
        # 用户没点选项、直接在输入框回答：把旧问题卡落成“已回答”，
        # 下次恢复时不再是一张能点的过期待办。
        if self._pending_clarify is not None and self._clarify_answer is None:
            pending = self._pending_clarify
            self._save_clarify_state(pending, "answered", None,
                                     "用户在输入框里回答了这个问题。")
            self._pending_clarify = None
        self.chat.add_user(display if display is not None else text)
        sel_note = self._apply_selection_as_layer()
        cfg = load_cfg()
        current = self.current_layer
        if sel_note:
            # 让规划器/大模型知道现在有一层「只含选中的要素」
            text = f"（当前图层只包含我选中的 {sel_note} 个要素，下面的操作都作用于它）{text}"

        # ---------------------------------------------------------- @ 图层提及
        # 「对 @小区 做缓冲区」：@ 是**显式**指定，优先于当前选中图层。
        # 名字写错或指代不明 → 直接问用户，别猜。
        mention_ctx = ""
        mentions = resolve_mentions(parse_mentions(text), self.store)
        if mentions:
            amb = ambiguity_question(mentions)
            if amb is not None:
                # 有歧义 → 走澄清，让用户点选项
                self._pending_clarify = {
                    "question": amb["question"],
                    "options": amb["options"],
                    "origin": text,
                }
                self.chat.add_clarify(amb["question"], amb["options"],
                                      self._on_clarify_picked)
                return
            good = [m for m in mentions if m.resolved]
            if good:
                # 明确指定的图层优先当「当前图层」
                current = good[0].layer_id
                # @ 标记要去掉（模型看到 @ 会当成特殊 token），
                # 但名字要留着 —— 规则引擎还得靠名字认图层。
                text = strip_mentions(text, mentions)
                # 单独告诉模型「用户点名了哪些图层」，它才不会换成相近的图层
                mention_ctx = mentions_note(mentions)
            else:
                # 全都没解析到：告诉用户，然后继续按原文走（可能是别的话题）
                text = strip_mentions(text, mentions)
                bad = not_found_note(mentions)
                if bad:
                    self.chat.add_agent(bad)
                    return
        # 澄清轮次：把上一轮的问题和这次的回答一起带给规划器，
        # 否则模型看到「道路」两个字，不知道用户到底想干什么。
        clarify_ctx = self._clarify_answer
        self._clarify_answer = None
        mem_text = self.memory.context(text) if self.memory is not None else ""
        run_uid = f"run-{time.time_ns()}"

        def job(token):
            self.planner.store = self.store
            # 「写一个算子：……」→ 走生成流程，不进规划器
            if self.planner.wants_new_operator(text):
                brief = self.planner.extract_operator_brief(text)
                res = {"op_gen": self.planner.gen_operator(brief, current, cfg),
                       "brief": brief, "group": "", "user": text,
                       "steps": [], "lines": [], "source": "gen", "note": ""}
                res["_run_uid"] = run_uid
                return res

            def on_event(kind: str, payload: dict) -> None:
                self.agent_event.emit(run_uid, kind, payload)

            # 统一走 Agent 闭环：闲聊/规划/执行/修复/澄清都在 run_agent 里
            confirm_enabled = (os.environ.get("GIS_AGENT_CONFIRM_STEPS", "1") != "0"
                               and cfg.get("confirm_steps", True) is not False)

            def confirm_step(info: dict):
                """后台执行线程等待主线程的确认弹窗。"""
                if token.is_set():
                    return ("cancel", "")
                gate = StepConfirmGate()
                self.agent_event.emit(run_uid, "confirm",
                                      {"gate": gate, "info": dict(info or {})})
                while not gate.wait(0.25):
                    if token.is_set():
                        return ("cancel", "")
                return (gate.action, gate.instruction)

            res = run_agent(self.store, self.planner, text, current, cfg,
                            self.history, use_llm=is_ready(cfg),
                            cancel_token=token, memory_context=mem_text,
                            clarify_context=clarify_ctx,
                            mention_context=mention_ctx,
                            on_event=on_event,
                            confirm_step=confirm_step if confirm_enabled else None)
            res["user"] = text
            res["_run_uid"] = run_uid
            res["source"] = res.get("planner") or "rule"
            # 模型请求写新算子 → 真的去写，然后把代码交给用户确认
            brief = res.get("new_operator")
            if brief:
                res["op_gen"] = self.planner.gen_operator(brief, current, cfg)
                res["brief"] = brief
            return res

        task = self.runtime.submit_run(job, kind="agent", label=text[:48],
                                       priority=PRIORITY_CHAT)
        self._task_handlers[task.id] = {"kind": "agent", "user": text}
        self._task_retry[task.id] = {"kind": "agent", "user": text}
        self._update_task_ui()

    def _on_task_event(self, task_id: str, event: str, payload) -> None:
        """主线程处理 TaskScheduler 事件。"""
        info = self._task_handlers.get(task_id) or {}
        label = info.get("user") or ""
        task = self.tasks.get(task_id)
        if task is not None and task.run_id:
            self._last_run_id = task.run_id
        if event == "queued":
            self.set_status(f"任务排队中 · {label[:30]}")
        elif event == "started":
            self._exec_t0 = time.time()
            self._exec_timer.start()
            if info.get("kind") in ("agent", "opgen"):
                self.chat.add_typing()
                self.chat.set_typing_text("正在理解需求并生成计划…")
            self.set_status(f"正在执行 · {label[:30]}")
            self._update_task_ui()
        elif event == "cancel_requested":
            self.set_status("正在取消…（当前步骤跑完后停止）")
        elif event == "done":
            self._task_handlers.pop(task_id, None)
            kind = info.get("kind")
            if kind in ("agent", "opgen"):
                res = payload if isinstance(payload, dict) else {}
                self._on_plan_done(res)
            elif kind == "export":
                try:
                    self._on_exported(Path(str(payload)))
                except Exception:
                    pass
            elif kind == "import":
                try:
                    self._on_loaded(payload)
                except Exception:
                    pass
            self._update_task_ui()
        elif event == "failed":
            self._task_handlers.pop(task_id, None)
            kind = info.get("kind")
            if kind in ("agent", "opgen"):
                self._on_plan_failed(str(payload or "任务失败"))
            elif kind == "import":
                self._on_load_failed(str(payload or "载入失败"))
            else:
                self._set_busy(False)
                self._error("任务失败", str(payload or ""))
            self._update_task_ui()
        elif event == "cancelled":
            self._task_handlers.pop(task_id, None)
            self.chat.remove_typing()
            self.chat.set_tool_activity("", False)
            uid = self._active_plan_uid
            if uid:
                card = self._live_plans.pop(uid, None)
                if card is not None:
                    try:
                        card.fail("已取消")
                    except Exception:
                        pass
                self._active_plan_uid = ""
            kind = info.get("kind")
            if kind in ("agent", "opgen"):
                self.chat.add_agent("已取消当前任务。已生成的图层会保留。")
            self.set_status("已取消")
            self._update_task_ui()

    def _update_task_ui(self) -> None:
        active = self.tasks.active_task
        pending = self.tasks.pending_count
        if active or pending:
            self.st_task_cancel.show()
            self.st_task_cancel.setEnabled(bool(active))
            extra = f"（排队 {pending}）" if pending else ""
            if active is not None:
                self.st_msg.setText(f"执行中{extra} · {active.label[:28]}")
            else:
                self.st_msg.setText(f"排队中{extra}")
        else:
            self.st_task_cancel.hide()
        try:
            self.chat.set_background_tasks(
                int(self.tasks.running_count), int(self.tasks.pending_count))
        except Exception:
            pass
        # 「有没有后台任务在跑」统一由调度器状态决定：
        # 队列里还有任务时不能把 busy 清掉，否则老代码（载入/清空/工具栏）
        # 会在任务还没跑完时就解除防护。
        busy = bool(active or pending)
        if busy != self._busy:
            self._set_busy(busy)
        # 地图顶部的任务通知同步刷新
        try:
            if getattr(self, "task_notice", None) is not None:
                self.task_notice.refresh()
        except Exception:
            pass
        if not busy:
            try:
                if getattr(self, "workspace", None) is not None:
                    self.workspace.refresh()
            except Exception:
                pass

    def _cancel_task(self, task_id: str) -> None:
        """任务中心的「取消」。"""
        task = self.tasks.get(task_id)
        if task is None:
            return
        pending = self._pending_confirm
        if pending is not None:
            gate, dlg = pending
            try:
                gate.resolve("cancel", "")
            except Exception:
                pass
            try:
                dlg.reject()
            except Exception:
                pass
            self._pending_confirm = None
        if task.status == "running":
            self.set_status("正在取消：当前步骤跑完后停止")
        self.runtime.cancel(task_id)
        self._update_task_ui()

    def _retry_task(self, task_id: str) -> None:
        """任务中心的「重试」：用同样的输入重新排一次。

        任务结束后 ``_task_handlers`` 会被清掉，所以重试所需的输入
        单独记在 ``_task_retry`` 里，直到用户主动重试或关窗。
        """
        info = self._task_retry.get(task_id) or self._task_handlers.get(task_id) or {}
        kind = info.get("kind")
        user = info.get("user") or ""
        self._task_handlers.pop(task_id, None)
        self._task_retry.pop(task_id, None)
        if kind == "agent" and user:
            self.on_send(user)
        elif kind == "opgen":
            self._run_operator_gen(user or "生成工具")
        else:
            self.set_status("这个任务不能直接重试，请重新操作一次")

    def _show_task_log(self, task_id: str) -> None:
        """任务中心的「日志」：展示这次 run 的完整事件流。"""
        if self.agent_db is None:
            self.set_status("这次运行没有可回放的日志")
            return
        run_id = ""
        task = self.tasks.get(task_id)
        if task is not None:
            run_id = task.run_id or ""
        if not run_id:
            # 任务已经不在内存里了（重启后），退而求其次找最近一次运行
            runs = self.agent_db.list_recent_runs(self.session_id, limit=50)
            run_id = runs[0].id if runs else ""
        if not run_id:
            self.set_status("这次运行没有可回放的日志")
            return
        try:
            events = self.agent_db.list_events(run_id)
            steps = self.agent_db.list_steps(run_id)
        except Exception:
            events, steps = [], []
        if not events and not steps:
            self.set_status("这次运行没有可回放的日志")
            return
        lines: list[str] = []
        if steps:
            lines.append("— 步骤 —")
            for s in steps:
                lines.append(f"{s.ordinal + 1}. {s.tool} [{s.status}] {s.message}")
        if events:
            lines.append("— 事件 —")
            for e in events:
                payload = e.payload or {}
                extra = payload.get("error") or payload.get("tool") or ""
                lines.append(f"{e.seq}. {e.type}" + (f"  {extra}" if extra else ""))
        dlg = QDialog(self)
        dlg.setWindowTitle("运行日志")
        dlg.resize(640, 460)
        vl = QVBoxLayout(dlg)
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText("\n".join(lines))
        vl.addWidget(edit)
        btn = QPushButton("关闭")
        btn.clicked.connect(dlg.accept)
        vl.addWidget(btn, 0, Qt.AlignRight)
        dlg.exec()

    def _cancel_active_task(self) -> None:
        task = self.tasks.active_task
        if task is None:
            return
        self.tasks.cancel(task.id)
        self.st_task_cancel.setEnabled(False)
        self.st_task_cancel.setText("正在停止…")
        self.set_status("正在取消：当前步骤跑完后停止")
        QTimer.singleShot(3000, lambda: self.st_task_cancel.setText("停止任务"))

    def _handle_step_confirm(self, payload: dict) -> None:
        """在主线程弹窗确认一步，后台 run_plan 线程在 gate 上等待。"""
        gate = payload.get("gate")
        info = payload.get("info") or {}
        if not isinstance(gate, StepConfirmGate) or gate.event.is_set():
            return
        if self._confirm_dialog is not None:
            gate.resolve("cancel", "")
            return
        idx = int(info.get("index") or 0)
        label = str(info.get("label") or info.get("tool") or "")
        self.set_status(f"等待确认 · 第 {idx + 1} 步 · {label}")
        dlg = StepConfirmDialog(info, self)
        self._confirm_dialog = dlg
        self._pending_confirm = (gate, dlg)
        try:
            accepted = dlg.exec()
        finally:
            self._confirm_dialog = None
            self._pending_confirm = None
        if gate.event.is_set():
            return
        if accepted == QDialog.Accepted:
            gate.resolve(dlg.action, dlg.instruction())
        else:
            gate.resolve("cancel", "")

    def _on_agent_event(self, uid: str, kind: str, payload) -> None:
        """后台 Agent 的规划/步骤流事件（主线程执行）。"""
        payload = payload if isinstance(payload, dict) else {}
        if kind == "confirm":
            self._handle_step_confirm(payload)
            return
        if kind == "plan":
            plan = payload.get("plan") or []
            if not plan:
                return
            card = PlanCard(plan, str(payload.get("source") or ""), self)
            self._live_plans[uid] = card
            self._active_plan_uid = uid
            self.chat.remove_typing()
            self.chat.add_plan(card)
            self.chat.add_trace(
                "规划完成 · " + " → ".join(str(s.get("label") or s.get("tool") or "")
                                           for s in plan))
            self.set_status(f"计划已生成 · {len(plan)} 步，开始执行")
            self._update_chat_metrics()
        elif kind == "step":
            card = self._live_plans.get(uid)
            if card is not None:
                try:
                    card.update_step(int(payload.get("index") or 0), payload)
                except Exception:
                    pass
            status = str(payload.get("status") or "")
            label = str(payload.get("label") or payload.get("tool") or "工具")
            if status == "running":
                self.chat.set_tool_activity(label, True)
                self.chat.add_trace(f"调用工具 · {label}")
            elif status == "confirm":
                self.chat.set_tool_activity(f"等待确认 · {label}", True)
                self.chat.add_trace(f"等待确认 · {label}")
            elif status in ("ok", "warn", "error", "skipped", "cancelled"):
                self.chat.set_tool_activity("", False)
                tail = str(payload.get("message") or "")
                verb = {"ok": "完成", "warn": "完成", "error": "失败",
                        "skipped": "跳过", "cancelled": "已取消"}.get(status, "完成")
                self.chat.add_trace(
                    f"{verb} · {label}"
                    + (f" · {tail[:120]}" if tail else ""))
                self._update_chat_metrics()

    def _update_chat_metrics(self) -> None:
        """Harness 底栏：轮数 / 步数 / tok/s / 总 token / 缓存命中 / 上下文占比。"""
        turns = steps = 0
        if self.persist and self.agent_db is not None and self.session_id:
            try:
                turns = self.agent_db.count_user_messages(self.session_id)
                steps = self.agent_db.count_session_steps(self.session_id)
            except Exception:
                pass
        else:
            turns = sum(1 for m in self.history if m.get("role") == "user")
        stats = getattr(self.planner, "llm_stats", {}) or {}
        pt = int(stats.get("prompt_tokens") or 0)
        ct = int(stats.get("completion_tokens") or 0)
        secs = float(stats.get("seconds") or 0)
        cache = int(stats.get("cache_hit_tokens") or 0)
        tok_s = (ct / secs) if secs > 0 else 0.0
        cache_pct = (cache / pt * 100.0) if pt > 0 else None
        ctx = min(100.0, (pt + ct) / 128000.0 * 100.0) if (pt + ct) else 0.0
        self.chat.set_stats(turns, steps, tok_s, pt + ct, cache_pct, ctx)

    def toggle_left_panel(self) -> None:
        panel = getattr(self, "left_panel", None)
        if panel is None:
            return
        panel.setVisible(not panel.isVisible())
        self.set_status("已显示左侧工作区" if panel.isVisible() else "已隐藏左侧工作区")


    def _finish_live_plan(self, uid: str, failed: bool = False,
                          cancelled: bool = False) -> None:
        card = self._live_plans.pop(uid, None)
        if card is not None:
            try:
                if cancelled:
                    card.fail("用户取消")
                else:
                    card.finish(failed=failed)
            except Exception:
                pass
        if self._active_plan_uid == uid:
            self._active_plan_uid = ""

    def _tick_exec(self) -> None:
        """每秒刷一次「正在执行… Ns」，让长算子看起来是在干活而不是卡死。"""
        if not self.tasks.running_count:
            self._exec_timer.stop()
            return
        sec = int(time.time() - self._exec_t0)
        self.chat.set_typing_text(f"正在理解需求并生成计划… {sec}s")
        self.set_status(f"Agent 正在执行… {sec}s")

    def _show_clarify(self, clarify: dict, origin: str, res: dict) -> None:
        """渲染澄清问题，让用户点选项回答，然后带着答案重新规划。

        这一步是「大模型负责理解自然语言」的关键闭环：
        模型发现图层/字段有歧义时不该硬猜，而是把选择权交回用户；
        用户点完，模型拿到「原始需求 + 用户的回答」重新出计划。
        """
        question = str(clarify.get("question") or "需要确认一下：")
        options = [str(o) for o in (clarify.get("options") or []) if str(o).strip()]
        self._pending_clarify = {"question": question, "options": options,
                                 "origin": origin}
        self.chat.add_clarify(question, options, self._on_clarify_picked)
        msg = self._remember(origin, question, clarify={
            "question": question, "options": options,
            "state": "open", "chosen": None, "note": "",
        })
        if msg is not None:
            self._pending_clarify["message_id"] = msg.id
        self.set_status("需要你确认一下")
        self._refresh_planner_pill(res.get("source"))

    def _save_clarify_state(self, pending: dict, state: str,
                            chosen: str | None = None, note: str = "") -> None:
        mid = str(pending.get("message_id") or "")
        if not (mid and self.persist and self.agent_db is not None):
            return
        try:
            self.agent_db.update_part(mid, "clarify", {
                "question": str(pending.get("question") or ""),
                "options": list(pending.get("options") or []),
                "state": state, "chosen": chosen, "note": note,
            })
        except Exception:
            pass

    def _on_clarify_picked(self, option: str) -> None:
        """用户点了某个澄清选项：把答案送回 Agent 重新规划。"""
        pending = self._pending_clarify or {}
        self._save_clarify_state(pending, "picked", option,
                                 "已选好，按这个答案继续。")
        self._pending_clarify = None
        origin = pending.get("origin") or ""
        question = pending.get("question") or ""
        # 答案本身要完整——只发「道路」模型不知道是在回答哪一个问题，
        # 所以把「原始需求 + 刚才问的 + 用户的回答」拼成一条完整指令，
        # 并把结构化的澄清上下文一起交给规划器。
        if origin:
            text = f"{origin}\n（对刚才的问题「{question}」，我的回答是：{option}）"
        else:
            text = option
        self._clarify_answer = {
            "question": question, "options": pending.get("options") or [],
            "origin": origin, "answer": option,
        }
        # 界面只显示用户点的那个选项；发给模型的是拼好的完整上下文。
        # （on_send 内部会 add_user，这里别重复 add。）
        self.on_send(text, display=option)

    def _on_plan_done(self, res: dict) -> None:
        self._exec_timer.stop()
        self.chat.remove_typing()
        self.chat.set_tool_activity("", False)
        # 注意：这里不直接 _set_busy(False)——队列里可能还有别的任务，
        # busy 状态统一由 _update_task_ui() 按调度器状态维护。
        user = res.get("user") or ""
        steps = res.get("steps") or []
        lines = res.get("lines") or []
        uid = str(res.get("_run_uid") or "")
        plan_state = None
        if uid:
            # 先把计划卡最终状态取下来，再 _finish_live_plan() 把它从 live 表里摘掉；
            # 否则重启后只剩结果文字，看不到「哪一步失败了」。
            live = self._live_plans.get(uid)
            if live is not None:
                try:
                    plan_state = live.to_state()
                except Exception:
                    plan_state = None
            self._finish_live_plan(
                uid, failed=any(s.get("status") == "error" for s in steps),
                cancelled=bool(res.get("cancelled")))

        # 把这次运行的步骤落库（可回放）
        self._record_run_steps(res, steps)

        # 先抽取记忆（偏好/图层事实/工作流），再展示结果
        if self.memory is not None and res.get("op_gen") is None:
            try:
                self.memory.observe_turn(user, res, session_id=self.session_id)
            except Exception:
                pass
            # 消息够了就压缩一次会话摘要
            try:
                self.memory.maybe_summarize(self.session_id)
            except Exception:
                pass

        # 生成算子：先给用户看代码，确认后才入库
        if res.get("op_gen") is not None:
            # 模型先解释了「为什么要新写算子」，把这段话也显示出来，
            # 否则用户只看到一个代码弹窗，不知道这是回应他的哪个诉求。
            why = (res.get("reply") or "").strip()
            if why:
                self.chat.add_agent(why)
            if res.get("rerun"):
                self.chat.add_agent(
                    "现有工具库没有能直接完成这个任务的工具；模型决定新增一个工具，"
                    "确认代码后会用它自动重跑原任务。")
            self._handle_operator_gen(
                res.get("op_gen") or {}, res.get("brief") or user,
                group=res.get("group") or "", rerun=res.get("rerun") or "")
            return

        # 纯对话/澄清：直接回一句，不进步骤卡片
        if res.get("chat"):
            reply = res["chat"]
            self.chat.add_agent(reply)
            self._remember(user, reply)
            self._refresh_planner_pill(res.get("source"))
            return

        # 澄清：把问题 + 可点选项渲染出来，用户点一下就带答案重新规划
        clarify = res.get("clarify")
        if clarify:
            self._show_clarify(clarify, user, res)
            return

        if not steps:
            reply = (res.get("reply") or res.get("note") or "").strip()
            # 大模型/解析器给了自然语言说明就用它，别再甩模板
            reply = reply or ("这句我还拆不出可执行的步骤。可以这样描述：\n"
                              "· 对「图层名」做 500 米缓冲区\n"
                              "· 用「边界」裁剪「地块」\n"
                              "· 按「类型」融合后统计各类面积")
            # 大模型**出过错**（超时/额度/返回不合法）但规则引擎兜住了：
            # 要如实告诉用户「这次走的是规则引擎」，否则用户会以为
            # 自然语言理解成功，下一次换个说法就又不灵了。
            # 注意：只能用 llm_error 判定 —— note 既可能是错误，
            # 也可能是模型给的正常回复，不能一概当成错误提示。
            # reply 里通常已经带着错误原文（run_agent 把 note 拼进去了），
            # 所以只在没包含时才补，避免同一句话说两遍。
            err = (res.get("llm_error") or "").strip()
            if err and res.get("source") != "llm" and err not in reply:
                reply = f"（{err}）\n{reply}"
            self.chat.add_agent(reply)
            self._remember(user, reply)
            self._refresh_planner_pill(res.get("source"))
            return
        err = sum(1 for s in steps if s.get("status") == "error")
        head = f"执行了 {len(steps)} 步" + (f"，其中 {err} 步失败" if err else "，全部成功") + "："
        body = "\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines))
        text = res.get("reply") or ((res.get("note") + "\n" if res.get("note") else "") + head + "\n" + body)
        self.chat.add_agent(text, steps)
        self._remember(user, text, steps=steps, plan=plan_state)

        created = [s["layer"] for s in steps if s.get("layer")]
        if created:
            acts = [(m["id"], self.store.get(m["id"])) for m in created]
            acts = [(lid, lay) for lid, lay in acts if lay is not None]
            if acts:
                self._push_undo({"kind": "add", "lids": [lid for lid, _ in acts], "layers": acts})
            self.current_layer = created[-1]["id"]
        self.refresh_layers()
        # highlight 算子只改 store 里的高亮集合，不产新图层 ——
        # 这里把集合同步到地图，让用户真的看到高亮。
        self._sync_highlight_to_map()
        if created:
            self.map.fit_layer(created[-1]["id"])
            self.set_status(f"完成 {len(steps)} 步，生成 {len(created)} 个图层")
        else:
            n = self.map.highlight_set_count()
            if n:
                self.set_status(f"完成 {len(steps)} 步，高亮 {n} 个要素")
            else:
                self.set_status(f"完成 {len(steps)} 步")
        self._refresh_planner_pill(res.get("source"))
        self._update_chat_metrics()

    def _sync_highlight_to_map(self) -> None:
        """把 store 里的高亮集合同步到地图视图。"""
        try:
            self.map.clear_highlight_set()
            for lid, (idx, color) in (self.store.highlights or {}).items():
                if idx:
                    self.map.set_highlight_set(lid, idx, color)
        except Exception:
            pass

    def _record_run_steps(self, res: dict, steps: list[dict]) -> None:
        """把这次运行的步骤写进 DB（steps 表 + step.finished 事件）。

        找不到对应 run 时静默跳过——持久化是加分项，不该因为它让主流程失败。
        """
        if self.agent_db is None or not steps:
            return
        run_id = res.get("run_id") or self._last_run_id
        if not run_id:
            return
        try:
            for s in steps:
                layer = s.get("layer") or {}
                self.runtime.record_step(run_id, {
                    "tool": s.get("tool") or "",
                    "args": s.get("args") or {},
                    "message": s.get("message") or "",
                    "warnings": s.get("warnings") or [],
                    "data": s.get("data") or {},
                    "layer_id": layer.get("id") if isinstance(layer, dict) else None,
                }, status=str(s.get("status") or "ok"))
        except Exception:
            pass

    def _on_plan_failed(self, msg: str) -> None:
        self._exec_timer.stop()
        self.chat.remove_typing()
        self.chat.set_tool_activity("", False)
        uid = self._active_plan_uid
        if uid:
            card = self._live_plans.pop(uid, None)
            if card is not None:
                try:
                    card.fail(msg)
                except Exception:
                    pass
            self._active_plan_uid = ""
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
        if getattr(lay, "is_raster", False):
            # 栅格没有属性表，但有元信息；给个直接的说明比弹空表好
            if getattr(lay, "online", None):
                from app.online_map import method_label
                cfg = lay.online
                extras = []
                params = cfg.get("params") or {}
                headers = cfg.get("headers") or {}
                if isinstance(params, dict) and params:
                    extras.append(f"自定义参数：{len(params)} 项")
                if isinstance(headers, dict) and headers:
                    extras.append(f"自定义请求头：{len(headers)} 项")
                if cfg.get("token"):
                    token_key = str(cfg.get("token_key") or "").strip()
                    extras.append("Token：已配置" +
                                  (f"（参数名 {token_key}）" if token_key else ""))
                extra_text = ("\n" + "\n".join(extras)) if extras else ""
                display_url = str(cfg.get("url") or "")
                token_value = str(cfg.get("token") or "")
                if len(token_value) >= 4:
                    # 用户若把 Token 直接写进地址，信息框里也不露出明文。
                    # 太短的值不做替换，避免把地址里的普通字符全打成星号。
                    display_url = display_url.replace(token_value, "***")
                QMessageBox.information(
                    self, f"在线地图 · {lay.name}",
                    f"加载方式：{method_label(cfg.get('method'))}\n"
                    f"地址：{display_url}\n"
                    f"坐标系：{lay.info.crs_name}{extra_text}\n\n"
                    "在线底图由网络瓦片/服务实时渲染，没有本地属性表。")
                return
            i = lay.info
            QMessageBox.information(
                self, f"栅格信息 · {lay.name}",
                f"尺寸：{i.width} × {i.height} 像元\n"
                f"波段：{i.bands}（{(i.dtypes or [''])[0]}）\n"
                f"坐标系：{i.crs_name}\n"
                f"范围：{tuple(round(v, 4) for v in (i.bounds or ()))}\n"
                f"驱动：{i.driver}\n"
                f"概览：{('、'.join(str(x) for x in (i.overviews or ())) or '无')}\n\n"
                "栅格没有属性表。")
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

    def _export_raster(self, lay) -> None:
        """栅格导出 = 复制原始文件（不支持重投影/裁剪导出）。"""
        if getattr(lay, "online", None):
            self.chat.add_agent(
                f"「{lay.name}」是在线地图，没有本地原文件可导出；"
                "可以用「导出当前视图」保存屏幕上的画面。")
            self.set_status("在线地图不支持导出原文件")
            return
        src = Path(lay.info.path)
        default = str(Path.home() / src.name)
        path, _ = QFileDialog.getSaveFileName(self, "导出栅格", default,
                                              f"*{src.suffix}")
        if not path:
            return
        dest = Path(path if path.lower().endswith(src.suffix.lower()) else path + src.suffix)
        try:
            shutil.copyfile(src, dest)
            # 金字塔是单独的外部文件；不一起复制，导出的图会丢失加速结构
            ovr = Path(str(src) + ".ovr")
            if ovr.is_file():
                shutil.copyfile(ovr, Path(str(dest) + ".ovr"))
        except Exception as e:  # noqa: BLE001
            self._error("导出失败", str(e))
            return
        self.set_status(f"已导出栅格：{dest.name}")
        self.chat.add_agent(f"栅格已导出到 {dest}")

    def export_layer(self, layer_id: str, fmt: str | None = None) -> None:
        lay = self.store.get(layer_id)
        if lay is None:
            return
        if getattr(lay, "is_raster", False):
            self._export_raster(lay)
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

        def job(token):
            # 只读任务：拿一份 gdf 快照，和 Agent 任务并发跑也不会互相污染
            gdf = lay.gdf
            token.raise_if_cancelled()
            return write_layer_file(gdf, lay.name, fmt, dest)

        # 导出走调度器：分析中也能排队，不会覆盖 _worker 导致线程崩溃
        task = self.runtime.submit_run(job, kind="export",
                                       label=f"导出 {lay.name}",
                                       priority=PRIORITY_EXPORT)
        self._task_handlers[task.id] = {"kind": "export", "user": lay.name}
        self._update_task_ui()

    def _on_exported(self, path: Path) -> None:
        self.set_status(f"已导出 {path.name}")
        QMessageBox.information(self, "导出完成", f"已保存到：\n{path}")

    def open_agent_settings(self) -> None:
        """头部最右侧的 Agent 设置：技能 + 工具 + 逐步确认 + 模型设置入口。"""
        dlg = AgentSettingsDialog(self)
        # 设置里删/改/停用工具后，左栏工具库要立刻跟着变；不用用户关掉窗口再刷新。
        dlg.tools_changed.connect(self._apply_tool_settings)
        accepted = dlg.exec() == QDialog.Accepted
        if accepted and dlg.new_tool_requested:
            # 生成工具有自己的弹窗流程，等设置窗口关掉再走，避免两层模态叠在一起。
            QTimer.singleShot(0, self.prompt_new_operator)

    def _apply_tool_settings(self) -> None:
        """把设置页保存的「停用工具」同步到运行时，并刷新左栏工具库。"""
        try:
            tool_prefs.set_disabled(load_cfg().get("tools_disabled") or [])
        except Exception:
            pass
        self._refresh_tools()

    def _enable_tool_after_install(self, name: str) -> None:
        """新建/重装工具后保证它是启用的。

        边界情况：用户曾停用某个自定义工具，删掉后大模型又生成了一个同名工具
        —— 如果只沿用停用列表，新工具会一出生就被隐藏，用户找不到还得再进设置翻。
        """
        tool_prefs.enable(name)
        try:
            cfg = load_cfg()
            old = list(cfg.get("tools_disabled") or [])
            keep = [n for n in old if n != name]
            if len(keep) != len(old):
                cfg["tools_disabled"] = keep
                save_cfg(cfg)
        except Exception:
            pass

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

    def open_memory_panel(self) -> None:
        """打开记忆管理面板。"""
        if self.memory is None:
            QMessageBox.information(
                self, "记忆管理",
                "当前没有启用持久化，记忆功能不可用。\n"
                "（正常启动桌面端时是开启的；测试环境默认关闭。）")
            return
        dlg = MemoryPanel(self.memory, self)
        dlg.exec()

    def _remember(self, user: str, reply: str, *, steps: list[dict] | None = None,
                  plan: dict | None = None,
                  clarify: dict | None = None) -> Message | None:
        """记下这一轮对话：内存 history 给模型，SQLite 给下次启动。

        ``plan`` / ``clarify`` 是结构化卡片数据，作为 assistant 消息的 part
        与文本一起落库；重启时由 `_restore_chat()` 按原顺序重建成卡片。
        只传文本的旧调用行为不变。
        """
        if user:
            self.history.append({"role": "user", "content": user})
        if reply:
            self.history.append({"role": "assistant", "content": reply})
        if len(self.history) > 20:
            del self.history[:-20]
        out: Message | None = None
        if self.persist and self.agent_db is not None:
            try:
                if user:
                    self.agent_db.append_text(self.session_id, "user", user)
                parts: list[Part] = []
                if plan:
                    parts.append(Part(type="plan", content=plan))
                if reply:
                    parts.append(Part(type="text", content={"text": reply}))
                if steps:
                    clean = [PlanCard._public_step(s) for s in steps
                             if isinstance(s, dict)]
                    if clean:
                        parts.append(Part(type="steps", content={"steps": clean}))
                if clarify:
                    parts.append(Part(type="clarify", content=clarify))
                if parts:
                    out = self.agent_db.append_message(
                        self.session_id, "assistant", parts)
                sess = self.agent_db.get_session(self.session_id)
                if user and sess is not None and sess.title in ("新会话", "默认会话"):
                    self.agent_db.touch_session(self.session_id, title=user[:30])
                else:
                    self.agent_db.touch_session(self.session_id)
                self._persist_state()
            except Exception:
                pass
        self._update_chat_metrics()
        return out

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
        try:
            self.chat._refresh_model_button()
        except Exception:
            pass

    # ================================================================== 自定义算子
    def prompt_new_operator(self) -> None:
        """工具库右上角「＋ 新建工具」：输入一段算法描述 + 选个分组。"""
        if not is_ready(load_cfg()):
            QMessageBox.information(
                self, "需要大模型",
                "按描述生成工具需要接入大模型：\n先到「模型设置」填好 API Key（默认用 DeepSeek）。")
            return
        dlg = NewToolDialog(self, group_choices=GROUP_CHOICES)
        if dlg.exec() != QDialog.Accepted:
            return
        text, group = dlg.value()
        if not text:
            return
        self.chat.add_user("生成工具：" + text)
        self._run_operator_gen(text, group=group)

    def _run_operator_gen(self, brief: str, group: str = "") -> None:
        """后台调用大模型生成算子，完成后弹预览。

        ``group`` 是用户在输入弹窗里选的分组，一路带到最终落盘。
        """
        def job(token):
            self.planner.store = self.store
            return {"op_gen": self.planner.gen_operator(brief, self.current_layer, load_cfg()),
                    "brief": brief, "group": group, "user": "", "steps": [], "lines": [],
                    "source": "gen", "note": ""}

        task = self.runtime.submit_run(job, kind="opgen", label="生成工具",
                                       priority=PRIORITY_OPGEN)
        self._task_handlers[task.id] = {"kind": "opgen", "user": "生成工具"}
        self._task_retry[task.id] = {"kind": "opgen", "user": brief}
        self._update_task_ui()

    def _handle_operator_gen(self, res: dict, brief: str, group: str = "",
                             rerun: str = "") -> None:
        """展示生成的工具：代码可改，确认后落盘并注册。"""
        spec = res.get("spec") or {}
        # 输入弹窗里选的分组作为预览弹窗的默认值（用户仍可在预览里改）
        if group:
            spec.setdefault("group", group)
        if res.get("error"):
            msg = res["error"]
            raw = res.get("raw")
            self.chat.add_agent("没能生成工具：" + msg + ("\n\n模型原始输出：\n" + raw if raw else ""))
            # 代码已经生成出来了，只是没过安全检查 —— 那就让用户看到并改，
            # 总比丢一句「没通过检查」就结束强。
            if not spec.get("code"):
                return
        elif not spec:
            self.chat.add_agent("没能生成工具：模型没有返回代码。")
            return
        dlg = OperatorDialog(spec, self, group_choices=GROUP_CHOICES)
        if dlg.exec() != QDialog.Accepted:
            self.chat.add_agent("已取消，工具没有保存。")
            return
        spec = dlg.value()
        try:
            spec = custom_ops.save(spec)
            custom_ops.install(spec)
        except Exception as e:
            self.chat.add_agent(f"保存失败：{e}")
            return
        self._enable_tool_after_install(spec["name"])
        self._refresh_tools()
        try:
            self.workspace.refresh()
        except Exception:
            pass
        params = "、".join(spec.get("params") or {}) or "layer"
        self.chat.add_agent(
            f"已加入工具库：「{spec['title']}」（{spec['name']}）。\n"
            f"参数：{params}。\n"
            f"直接说需求就能用它，比如「对地块做{spec['title']}」。")
        self.set_status(f"新工具已就绪：{spec['title']}")
        # 模型是因为「现有工具不足」才要求新建的：装上以后自动把原任务再跑一遍，
        # 而不是让用户自己复制原话重发。只自动重试一次，避免模型反复要求新工具。
        if rerun and not self._tool_rerun_inflight:
            self._tool_rerun_inflight = True
            self.chat.add_agent("工具库已补上，正在用它重新执行原任务…")
            QTimer.singleShot(
                0, lambda: self.on_send(rerun, display="↻ 重新执行原任务",
                                        _auto_tool_retry=True))



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
