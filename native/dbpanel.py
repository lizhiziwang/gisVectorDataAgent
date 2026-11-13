"""左栏「数据库」页签：PostgreSQL / PostGIS 连接管理 + 表清单。

这个页签和「工具库」并排挂在左栏下半块（`QTabWidget#dataTabs`）。
长相按 Navicat 的路子来：一棵树 连接 → schema → 表 → 字段，展开才去查，
**双击空间表直接把表加成图层**；普通表（没有几何列）也会列出来，只是加不到地图。

双击时**不读整表**：后台只探测几毫秒的元信息（范围、字段、行数估计），
图层登记成「连接 + schema.表」。渲染时地图把当前视野交给 provider，
用 `geom && ST_MakeEnvelope(...)` 在服务端空间过滤、每批 1 万行地拉回来
边拉边画（`app/db_provider.py`）。属性表/导出/算子要整份数据时才整表读，
同样受「一次最多 20 万要素」的限制（`DbConnectionsPanel.max_features`）。

线程：测试连接 `_TestWorker`、列表清单 `_TablesWorker`、读字段 `_ColsWorker`、
加图层的探测 `_LoadWorker` 都是 QThread，结果通过信号回 GUI 线程；关窗口时
`stop()` 会置取消标志。
"""
from __future__ import annotations

import uuid
from typing import Callable

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPushButton, QSpinBox, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from app import db_conn

from .icons import db_icon, grid_icon, layer_icon, schema_icon, search_icon


# ---------------------------------------------------------------- 后台任务
class _TestWorker(QThread):
    """在后台探一次连接，别让界面卡在 TCP 超时上。"""

    result = Signal(dict)

    def __init__(self, conn: dict, parent=None) -> None:
        super().__init__(parent)
        self.conn = db_conn.normalize_conn(conn)

    def run(self) -> None:      # noqa: D401 - QThread 入口
        try:
            res = db_conn.test_connection(self.conn)
        except Exception as e:  # noqa: BLE001  后台线程不能把异常抛出去
            res = {"ok": False, "message": str(e), "version": "", "postgis": "",
                   "target": db_conn.target_text(self.conn)}
        self.result.emit(res)


class _TablesWorker(QThread):
    """列出一张库里的空间表。"""

    result = Signal(list, str)      # (tables, error)

    def __init__(self, conn: dict, parent=None) -> None:
        super().__init__(parent)
        self.conn = db_conn.normalize_conn(conn)

    def run(self) -> None:      # noqa: D401
        try:
            self.result.emit(db_conn.list_tables(self.conn), "")
        except Exception as e:  # noqa: BLE001
            self.result.emit([], str(e))


class _LoadWorker(QThread):
    """加图层前的**探测**：取范围 + 排序键 + 字段，不读整表。

    以前这一路是「把整张表读成 GeoParquet 快照」：20 万行几十秒、几百 MB。
    现在图层是活的 —— 渲染时按视野向库里查（空间过滤 + 1 万一批），
    所以这里只需要几毫秒的元信息：范围、字段、行数估计。
    """

    done = Signal(dict, str)         # (图层 spec, 错误信息)
    #: 保留给关窗口时调 stop()（探测本身很快，不强杀）
    cancel = None

    def __init__(self, conn: dict, schema: str, table: str,
                 geom_column: str = "", srid: int = 0,
                 est_features: int = 0, max_features: int = 0,
                 parent=None) -> None:
        super().__init__(parent)
        self.conn = db_conn.normalize_conn(conn)
        self.schema = str(schema)
        self.table = str(table)
        self.geom_column = str(geom_column or "")
        self.srid = int(srid or 0)
        self.est_features = int(est_features or 0)
        self.max_features = int(max_features or 0)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:      # noqa: D401
        try:
            info = db_conn.probe_table(
                self.conn, self.schema, self.table,
                geom_column=self.geom_column, srid=self.srid,
                est_features=self.est_features)
            spec = {
                "conn": self.conn,
                "schema": self.schema,
                "table": self.table,
                "geom_column": self.geom_column,
                "srid": int(info.get("srid") or self.srid or 0),
                "geom_type": str(info.get("geom_type") or ""),
                "fields": list(info.get("fields") or []),
                "count": max(0, int(info.get("count") or self.est_features or 0)),
                # 视图没有 ctid，order_key() 会回 ""，别在这儿兜底成 ctid
                "order_by": str(info.get("order_by", "ctid")),
                "max_features": int(self.max_features or 0),
                "bounds": info.get("bounds"),
                "wgs_bounds": info.get("wgs_bounds"),
            }
            self.done.emit(spec, "")
        except Exception as e:  # noqa: BLE001  后台线程不能把异常抛出去
            self.done.emit({}, str(e))


# ---------------------------------------------------------------- 连接弹窗
class DbConnDialog(QDialog):
    """新建 / 编辑一个数据库连接。

    `conn` 传进来就是编辑（字段预填），不传就是新建。
    `tester` 可注入（回归测试用假连接，不真的建 TCP）。
    """

    def __init__(self, conn: dict | None = None, parent=None,
                 tester: Callable[[dict], dict] | None = None) -> None:
        super().__init__(parent)
        self._tester = tester
        editing = bool(conn)
        self.setWindowTitle("编辑数据库连接" if editing else "新建数据库连接")
        self.resize(520, 430)
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(9)

        head = QLabel("数据库连接")
        head.setStyleSheet("font-size:14px;font-weight:600;")
        root.addWidget(head)
        sub = QLabel("先支持 PostgreSQL / PostGIS：连上以后可以把库里的空间表加成图层。")
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        root.addWidget(sub)

        base = db_conn.normalize_conn(conn) if conn else db_conn.empty_conn()

        form = QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft)

        self.kind_combo = QComboBox()
        for key in db_conn.KIND_ORDER:
            self.kind_combo.addItem(db_conn.DB_KINDS[key], key)
        form.addRow("类型", self.kind_combo)

        self.name_edit = QLineEdit(base["name"])
        self.name_edit.setClearButtonEnabled(True)
        self.name_edit.setPlaceholderText("给自己看的名字，例如「生产库 · 道路」")
        form.addRow("连接名称", self.name_edit)

        self.host_edit = QLineEdit(base["host"])
        self.host_edit.setClearButtonEnabled(True)
        self.host_edit.setPlaceholderText("127.0.0.1 或 db.example.com")
        form.addRow("主机", self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(base["port"]))
        self.port_spin.setToolTip("PostgreSQL 默认 5432")
        form.addRow("端口", self.port_spin)

        self.db_edit = QLineEdit(base["dbname"])
        self.db_edit.setClearButtonEnabled(True)
        self.db_edit.setPlaceholderText("数据库名")
        form.addRow("数据库", self.db_edit)

        self.user_edit = QLineEdit(base["user"])
        self.user_edit.setClearButtonEnabled(True)
        self.user_edit.setPlaceholderText("用户名")
        form.addRow("用户名", self.user_edit)

        self.pwd_edit = QLineEdit(base["password"])
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        self.pwd_edit.setClearButtonEnabled(True)
        self.pwd_edit.setPlaceholderText("可留空（服务端 trust / 证书认证时）")
        self.show_pwd = QPushButton("显示")
        self.show_pwd.setObjectName("chip")
        self.show_pwd.setCheckable(True)
        self.show_pwd.setCursor(Qt.PointingHandCursor)
        self.show_pwd.toggled.connect(
            lambda on: self.pwd_edit.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password))
        pwd_row = QWidget()
        pwd_row.setSizePolicy(self.pwd_edit.sizePolicy())
        pwd_layout = QHBoxLayout(pwd_row)
        pwd_layout.setContentsMargins(0, 0, 0, 0)
        pwd_layout.setSpacing(6)
        pwd_layout.addWidget(self.pwd_edit, 1)
        pwd_layout.addWidget(self.show_pwd)
        pwd_row.setFixedHeight(self.pwd_edit.sizeHint().height())
        form.addRow("密码", pwd_row)

        self.schema_edit = QLineEdit(base["schema"])
        self.schema_edit.setClearButtonEnabled(True)
        self.schema_edit.setPlaceholderText("默认 public")
        form.addRow("默认 Schema", self.schema_edit)

        self.ssl_combo = QComboBox()
        for mode in db_conn.SSLMODES:
            self.ssl_combo.addItem(mode, mode)
        self.ssl_combo.setCurrentIndex(
            max(0, self.ssl_combo.findData(base["sslmode"])))
        self.ssl_combo.setToolTip(
            "prefer = 能加密就加密；服务端没开 SSL 时改 disable")
        form.addRow("SSL 模式", self.ssl_combo)
        root.addLayout(form)
        root.addStretch(1)

        self.status = QLabel("")
        self.status.setObjectName("tiny")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.test_btn = btns.addButton("测试连接", QDialogButtonBox.ActionRole)
        self.test_btn.setObjectName("chip")
        self.test_btn.clicked.connect(self.test)
        btns.button(QDialogButtonBox.Ok).setText("保存")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

        hint = QLabel("密码只存在本机 settings.json，不会写进日志或聊天记录。")
        hint.setObjectName("tiny")
        hint.setWordWrap(True)
        root.addWidget(hint)

        if not db_conn.available():
            self.status.setText(db_conn.driver_hint())
            self.test_btn.setEnabled(False)

        self._worker: QThread | None = None

    # -------------------------------------------------------------- 取值
    def connection(self, require_name: bool = True) -> dict:
        """按当前界面填的内容拼一条连接配置（校验失败抛 `DbError`）。

        `require_name=False` 用于「保存」：名字空着不算错，会用
        「库名@主机」补一个建议名，其余必填项照样要校验。
        """
        raw = {
            "kind": str(self.kind_combo.currentData() or "postgresql"),
            "name": self.name_edit.text(),
            "host": self.host_edit.text(),
            "port": self.port_spin.value(),
            "dbname": self.db_edit.text(),
            "user": self.user_edit.text(),
            "password": self.pwd_edit.text(),
            "schema": self.schema_edit.text(),
            "sslmode": str(self.ssl_combo.currentData() or "prefer"),
        }
        if require_name:
            return db_conn.validate_conn(raw)
        conn = db_conn.normalize_conn(raw)
        if not conn["name"]:
            conn["name"] = self.suggested_name()
        return db_conn.validate_conn(conn)

    def suggested_name(self) -> str:
        """没填名字时用「库名@主机」当建议值。"""
        db = self.db_edit.text().strip()
        host = self.host_edit.text().strip()
        return f"{db}@{host}" if db and host else (db or host or "PostgreSQL")

    def accept(self) -> None:       # noqa: D401 - Qt override
        try:
            conn = self.connection(require_name=False)
        except Exception as e:      # noqa: BLE001
            QMessageBox.warning(self, "数据库连接", str(e))
            return
        self._conn = conn
        super().accept()

    # -------------------------------------------------------------- 测试
    def test(self) -> None:
        try:
            conn = self.connection()
        except Exception as e:      # noqa: BLE001
            self.status.setText(str(e))
            return
        if self._tester is not None:
            try:
                res = self._tester(conn)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "message": str(e)}
            self._show_result(res)
            return
        self.test_btn.setEnabled(False)
        self.status.setText("正在连接…")
        worker = _TestWorker(conn, self)
        worker.result.connect(self._show_result)
        worker.finished.connect(self._on_test_finished)
        self._worker = worker
        worker.start()

    def _on_test_finished(self) -> None:
        self.test_btn.setEnabled(True)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    def _show_result(self, res: dict) -> None:
        ok = bool((res or {}).get("ok"))
        msg = str((res or {}).get("message") or "")
        self.status.setStyleSheet("color:#2f6b3d;" if ok else "color:#b0522f;")
        self.status.setText(("✓ " if ok else "✗ ") + msg)


# ---------------------------------------------------------------- 树
ROLE = Qt.UserRole

#: 几何类型 → 中文（树里「说明」列用）
#: 字段类型太长会把说明列撑爆，这里换成短写法（完整类型仍在 tooltip 里）
SHORT_TYPES = {
    "character varying": "varchar",
    "character": "char",
    "double precision": "double",
    "real": "float",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
    "USER-DEFINED": "geometry",
}

_GEOM_CN = {
    "POINT": "点", "LINESTRING": "线", "POLYGON": "面",
    "GEOMETRYCOLLECTION": "集合", "GEOMETRY": "几何",
    "CIRCULARSTRING": "曲线", "COMPOUNDCURVE": "曲线",
    "CURVEPOLYGON": "面", "MULTICURVE": "曲线", "MULTISURFACE": "面",
}


def _data(item) -> dict:
    return dict(item.data(0, ROLE) or {})


def geom_cn(geom_type: str) -> str:
    """`MULTILINESTRING` → 「线」。"""
    t = str(geom_type or "").upper()
    if t.startswith("MULTI"):
        t = t[5:]
    return _GEOM_CN.get(t, t or "几何")


def table_summary(t: dict) -> str:
    """表节点「说明」列：`线 · EPSG:4326 · 3 要素` / `无几何列`。"""
    if not t.get("has_geom"):
        return "无几何列"
    bits = [geom_cn(t.get("geom_type"))]
    srid = int(t.get("srid") or 0)
    # srid=0 的（ArcSDE 那类没登记 typmod 的列）得写出来：位置可能不准
    bits.append(f"EPSG:{srid}" if srid else "无 SRID")
    est = int(t.get("est_features") or -1)
    if est >= 0:
        bits.append(f"{est:,} 要素")
    return " · ".join(bits)


def table_badge(t: dict) -> str:
    """表节点第二列的窄标签：`线 3857` / `几何 无SRID` / `无几何列`。

    树在左栏里只有两百多像素宽，完整那句（`线 · EPSG:3857 · 3 要素`）塞不下，
    放进 tooltip。这里只留最要紧的：几何类型 + SRID。
    """
    if not t.get("has_geom"):
        return "无几何列"
    srid = int(t.get("srid") or 0)
    return f"{geom_cn(t.get('geom_type'))} {srid or '无SRID'}"


#: 说明列（几何类型 / SRID / 字段类型）的字色：比名字淡一档
_MUTED = QBrush(QColor("#8f8a7d"))


class _ColsWorker(QThread):
    """后台读一张表的字段清单（展开表节点时用）。"""

    result = Signal(list, str)      # (columns, error)

    def __init__(self, conn: dict, schema: str, table: str, parent=None) -> None:
        super().__init__(parent)
        self.conn = db_conn.normalize_conn(conn)
        self.schema = str(schema)
        self.table = str(table)

    def run(self) -> None:      # noqa: D401 - QThread 入口
        try:
            self.result.emit(
                db_conn.list_columns(self.conn, self.schema, self.table), "")
        except Exception as e:  # noqa: BLE001
            self.result.emit([], str(e))


# ---------------------------------------------------------------- 面板
class DbConnectionsPanel(QFrame):
    """数据库连接管理面板（左栏「数据库」页签的内容）。

    长相按 Navicat 的路子来：一棵树，连接 → schema → 表 → 字段，
    展开才去查（懒加载），双击空间表把表登记成图层 —— 只探测范围/字段，
    数据留给渲染时按视野去库里取（`app/db_provider.py`）。

        kingbase          sde@192.168.17.4:54321/gisdata
        └─ sde            12 张表 · 3 张空间
           ├─ roads      线 · EPSG:4326 · 3 要素
           │  ├ fid       integer
           │  └ geom      geometry
           └─ config      无几何列
    """

    #: 加图层：图层 spec（连接 + schema.表 + 范围/字段）+ 建议的图层名
    layer_ready = Signal(dict, str)
    #: 连接列表变了（增删改），主窗口据此存盘
    changed = Signal()

    def __init__(self, provider: Callable[[], list[dict]] | None = None,
                 saver: Callable[[list[dict]], None] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("dbPanel")
        self._provider = provider or (lambda: [])
        self._saver = saver
        self._conns: list[dict] = []
        self._workers: list[QThread] = []
        self._load_worker: _LoadWorker | None = None
        #: 一次取数的上限：视野查询和属性表/导出的整表读都受它约束。
        #: 以前是底栏一个数字框，用户说那块不需要了，就固定成默认值。
        self.max_features = int(db_conn.DEFAULT_LIMIT)

        vl = QVBoxLayout(self)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(0)

        # ---- 标题行：数据库连接 [n]  ⟳  ＋ 新建连接
        head = QHBoxLayout()
        head.setContentsMargins(14, 10, 12, 6)
        head.setSpacing(6)
        title = QLabel("数据库连接")
        title.setObjectName("panelTitle")
        head.addWidget(title)
        self.badge = QLabel("0")
        self.badge.setObjectName("badge")
        head.addWidget(self.badge)
        head.addStretch(1)
        self.refresh_btn = QToolButton()
        self.refresh_btn.setObjectName("rowMenu")
        self.refresh_btn.setText("⟳")
        self.refresh_btn.setFixedSize(20, 20)
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setToolTip("重新读一遍表清单")
        self.refresh_btn.clicked.connect(self.refresh_all)
        head.addWidget(self.refresh_btn)
        self.new_btn = QPushButton("＋ 新建连接")
        self.new_btn.setObjectName("chip")
        self.new_btn.setCursor(Qt.PointingHandCursor)
        self.new_btn.setToolTip("添加一个 PostgreSQL / PostGIS 连接")
        self.new_btn.clicked.connect(self.add_connection)
        head.addWidget(self.new_btn)
        vl.addLayout(head)

        # ---- 搜索框（表多的时候按名字过滤）
        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("dbFilter")
        self.filter_edit.setPlaceholderText("搜索表名…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.addAction(search_icon(14), QLineEdit.LeadingPosition)
        self.filter_edit.textChanged.connect(self._on_filter)
        fwrap = QWidget()
        fl = QHBoxLayout(fwrap)
        fl.setContentsMargins(12, 0, 12, 6)
        fl.addWidget(self.filter_edit)
        vl.addWidget(fwrap)

        # ---- 树
        self.tree = QTreeWidget()
        self.tree.setObjectName("dbTree")
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["名称", "说明"])
        # 表头（名称 / 说明）不显示：左栏这块本来就窄，两行标题白占一条高，
        # 说明列的内容自己会说话（`面 4490`、`151 空间`）。
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(13)
        self.tree.setAlternatingRowColors(False)
        # 双击表 = 加到地图，别顺带展开（展开用左边的箭头或右键）
        self.tree.setExpandsOnDoubleClick(False)
        self.tree.setMinimumHeight(90)
        # 两列都按内容定宽，但说明列封顶 96 px、名字列封顶「剩下的宽度」：
        # 左栏只有两百多像素，让长表名和「无几何列」都能看见一部分
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 140)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemDoubleClicked.connect(self._on_double)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_menu)
        vl.addWidget(self.tree, 1)

        self.empty = QLabel(
            "还没有数据库连接。\n点右上角「＋ 新建连接」，先连一个 PostgreSQL / PostGIS。")
        self.empty.setObjectName("dbEmpty")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setContentsMargins(16, 18, 16, 18)
        vl.addWidget(self.empty, 1)

        # ---- 底栏：一句提示（一条细线和树分开，别让树看起来悬空）
        foot = QFrame()
        foot.setObjectName("dbFooter")
        fv = QVBoxLayout(foot)
        fv.setContentsMargins(14, 6, 12, 7)
        fv.setSpacing(2)

        self.hint = QLabel("点开连接浏览 schema 和表 · 双击空间表加入地图")
        self.hint.setObjectName("tiny")
        self.hint.setWordWrap(True)
        fv.addWidget(self.hint)
        vl.addWidget(foot)

        if not db_conn.available():
            self.hint.setText(db_conn.driver_hint().replace("\n", "  "))
        self.reload()

    # -------------------------------------------------------------- 数据
    def connections(self) -> list[dict]:
        return [db_conn.normalize_conn(c) for c in self._conns]

    @staticmethod
    def _sig(conns) -> list[tuple]:
        return [(c["name"], c["host"], c["port"], c["dbname"], c["user"],
                 c["schema"], c["password"]) for c in conns]

    def reload(self) -> None:
        """从 provider 重新读一遍连接列表；连接没变就不动树（别收起展开的节点）。"""
        try:
            conns = db_conn.normalize_list(self._provider())
        except Exception:       # noqa: BLE001
            conns = []
        if (self._sig(conns) == self._sig(self._conns)
                and self.tree.topLevelItemCount() == len(conns)):
            self._conns = conns
            return
        self._conns = conns
        self._rebuild()

    def _persist(self) -> None:
        if self._saver is not None:
            try:
                self._saver(self.connections())
            except Exception:   # noqa: BLE001  存盘失败不该让界面崩
                pass
        self.changed.emit()

    def _rebuild(self) -> None:
        self.tree.clear()
        self.badge.setText(str(len(self._conns)))
        has = bool(self._conns)
        self.empty.setVisible(not has)
        self.tree.setVisible(has)
        self.filter_edit.setVisible(has)
        for conn in self._conns:
            self.tree.addTopLevelItem(self._make_conn_item(conn))
        if has:
            self.hint.setText("点开连接浏览 schema 和表 · 双击空间表加入地图")

    # -------------------------------------------------------------- 造节点
    def _make_conn_item(self, conn: dict) -> QTreeWidgetItem:
        item = QTreeWidgetItem([conn["name"], ""])
        f = item.font(0)
        f.setBold(True)
        item.setFont(0, f)      # 连接是最粗的一级，和 schema/表拉开层次
        item.setIcon(0, db_icon(14))
        item.setForeground(1, _MUTED)
        item.setData(0, ROLE, {"kind": "conn", "conn": dict(conn),
                               "loaded": False, "loading": False})
        item.setToolTip(0, f"{conn['name']}\n{db_conn.describe(conn)}")
        item.setToolTip(1, "双击展开 · 右键测试 / 编辑 / 删除")
        self._placeholder(item, "…")
        return item

    def _make_table_item(self, conn: dict, t: dict) -> QTreeWidgetItem:
        has = bool(t.get("has_geom"))
        item = QTreeWidgetItem([str(t.get("table") or ""), table_badge(t)])
        item.setIcon(0, layer_icon(14) if has else grid_icon(14))
        item.setForeground(1, _MUTED)
        item.setData(0, ROLE, {
            "kind": "table", "conn": dict(conn),
            "schema": str(t.get("schema") or ""), "table": str(t.get("table") or ""),
            "has_geom": has, "srid": int(t.get("srid") or 0),
            "geom_column": str(t.get("geom_column") or ""),
            "geom_type": str(t.get("geom_type") or ""),
            "est_features": int(t.get("est_features") or 0),
            "loaded": False, "loading": False})
        if has and not int(t.get("srid") or 0):
            tip = (table_summary(t) + "\n这张表没登记 Spatial Reference"
                   "（geometry_columns.srid = 0），加到地图后位置可能不准")
        else:
            tip = table_summary(t) if has else "没有几何列，不能加到地图"
        item.setToolTip(0, f"{t.get('schema')}.{t.get('table')}\n" + tip)
        self._placeholder(item, "…")
        return item

    def _fit_columns(self) -> None:
        """列宽：名字尽量宽、说明列封顶 96 px（左栏放不下两列长文本）。"""
        tree = getattr(self, "tree", None)
        if tree is None:
            return
        avail = max(140, tree.viewport().width() - 16)
        tree.resizeColumnToContents(1)
        w1 = min(max(44, tree.columnWidth(1)), 72)
        tree.setColumnWidth(1, w1)
        tree.resizeColumnToContents(0)
        tree.setColumnWidth(0, max(120, min(tree.columnWidth(0), avail - w1)))

    def resizeEvent(self, ev) -> None:      # noqa: N802 (Qt 命名)
        super().resizeEvent(ev)
        if getattr(self, "tree", None) is not None:
            self._fit_columns()

    def _placeholder(self, item: QTreeWidgetItem, text: str) -> None:
        """挂一个灰掉的占位子节点（既能显示状态，又能让节点带展开箭头）。"""
        item.takeChildren()
        ph = QTreeWidgetItem([text, ""])
        ph.setFlags(Qt.NoItemFlags)     # 灰、不可选
        item.addChild(ph)

    # -------------------------------------------------------------- 懒加载
    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        d = _data(item)
        if d.get("kind") == "conn" and not d.get("loaded"):
            self._load_tables(item)
        elif d.get("kind") == "table" and not d.get("loaded"):
            self._load_columns(item)

    def _load_tables(self, item: QTreeWidgetItem) -> None:
        d = _data(item)
        if d.get("loading"):
            return
        conn = d.get("conn") or {}
        if not db_conn.available():
            self._placeholder(item, db_conn.driver_hint().replace("\n", " "))
            self.hint.setText(db_conn.driver_hint().replace("\n", "  "))
            return
        d["loading"] = True
        item.setData(0, ROLE, d)
        self._placeholder(item, "正在读取表清单…")
        worker = _TablesWorker(conn, self)
        worker.result.connect(
            lambda tables, err, it=item: self._fill_tables(it, tables, err))
        self._track(worker)
        worker.start()

    def _fill_tables(self, item: QTreeWidgetItem, tables: list,
                     error: str) -> None:
        d = _data(item)
        d["loading"] = False
        d["loaded"] = True
        item.setData(0, ROLE, d)
        item.takeChildren()
        if error:
            self._placeholder(item, str(error).replace("\n", " ")[:160])
            self.hint.setText(f"✗ 读表清单失败：{str(error).splitlines()[0]}")
            return
        if not tables:
            self._placeholder(item, "这个库里没有表")
            self.hint.setText("这个库里没有表（也可能当前用户没权限）。")
            return
        groups: dict[str, list[dict]] = {}
        for t in tables:
            groups.setdefault(str(t.get("schema") or ""), []).append(t)
        for schema, rows in groups.items():
            spatial = sum(1 for t in rows if t.get("has_geom"))
            s_item = QTreeWidgetItem([schema, f"{spatial} 空间"])
            s_item.setIcon(0, schema_icon(14))
            s_item.setForeground(1, _MUTED)
            s_item.setData(0, ROLE, {"kind": "schema", "conn": d.get("conn"),
                                     "schema": schema})
            s_item.setToolTip(0, f"{schema}（{len(rows)} 张表，其中 {spatial} 张有几何列）")
            for t in rows:
                s_item.addChild(self._make_table_item(d.get("conn") or {}, t))
            item.addChild(s_item)
            # 只有一个 schema 的时候直接摊开，省一次点击
            if len(groups) == 1:
                s_item.setExpanded(True)
        item.setExpanded(True)
        item.setText(1, f"{len(tables)} 表")
        item.setToolTip(0, f"{d.get('conn', {}).get('name', '')}\n"
                           f"{db_conn.describe(d.get('conn') or {})}\n"
                           f"{len(groups)} 个 schema · {len(tables)} 张表")
        self._fit_columns()
        self._on_filter(self.filter_edit.text())
        self.hint.setText(
            f"共 {len(tables)} 张表，其中 {sum(1 for t in tables if t.get('has_geom'))} "
            "张有几何列 · 双击加入地图")

    def _load_columns(self, item: QTreeWidgetItem) -> None:
        d = _data(item)
        if d.get("loading"):
            return
        if not db_conn.available():
            self._placeholder(item, "没有 psycopg2，读不了字段")
            return
        d["loading"] = True
        item.setData(0, ROLE, d)
        self._placeholder(item, "正在读字段…")
        worker = _ColsWorker(d.get("conn") or {}, d.get("schema") or "",
                             d.get("table") or "", self)
        worker.result.connect(
            lambda cols, err, it=item: self._fill_columns(it, cols, err))
        self._track(worker)
        worker.start()

    def _fill_columns(self, item: QTreeWidgetItem, cols: list,
                      error: str) -> None:
        d = _data(item)
        d["loading"] = False
        d["loaded"] = True
        item.setData(0, ROLE, d)
        item.takeChildren()
        if error:
            self._placeholder(item, str(error).replace("\n", " ")[:160])
            return
        if not cols:
            self._placeholder(item, "没有字段")
            return
        for c in cols:
            tip = str(c.get("type") or "")
            short = SHORT_TYPES.get(tip, tip)
            label = f"{short}（几何）" if c.get("is_geom") else short
            sub = QTreeWidgetItem([str(c.get("name") or ""), label])
            sub.setFlags(Qt.ItemIsEnabled)      # 只看看，不参与双击/重命名
            sub.setToolTip(0, f"{c.get('name')}: {tip}")
            sub.setForeground(1, _MUTED)
            item.addChild(sub)
        self._fit_columns()

    # -------------------------------------------------------------- 双击
    def _on_double(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        d = _data(item)
        kind = d.get("kind")
        if kind in ("conn", "schema"):
            item.setExpanded(not item.isExpanded())
            return
        if kind != "table":
            return
        if not d.get("has_geom"):
            self.hint.setText(
                f"{d.get('schema')}.{d.get('table')} 没有几何列，不能加到地图。")
            return
        self._start_load(d)

    def _start_load(self, d: dict) -> None:
        if not db_conn.available():
            QMessageBox.warning(self.window(), "数据库连接", db_conn.driver_hint())
            return
        if self._load_worker is not None and self._load_worker.isRunning():
            self.hint.setText("上一张表还在探测，等它结束再点。")
            return
        conn = d.get("conn") or {}
        schema = str(d.get("schema") or "")
        table = str(d.get("table") or "")
        limit = self.max_features
        name = db_conn.table_layer_name(conn, schema, table)
        self.hint.setText(f"正在连接 {schema}.{table} 取范围…")
        worker = _LoadWorker(conn, schema, table,
                             geom_column=str(d.get("geom_column") or ""),
                             srid=int(d.get("srid") or 0),
                             est_features=int(d.get("est_features") or 0),
                             max_features=limit, parent=self)
        worker.done.connect(lambda spec, err, nm=name: self._on_loaded(nm, spec, err))
        self._load_worker = worker
        self._track(worker)
        worker.start()

    def _on_loaded(self, name: str, spec: dict, error: str) -> None:
        if error:
            self.hint.setText(f"✗ 读「{name}」的表信息失败")
            QMessageBox.warning(self.window(), "读取失败", str(error))
            return
        if not spec:
            self.hint.setText("已取消")
            return
        self.hint.setText(f"✓ 已加入「{name}」：地图按当前视野取数")
        self.layer_ready.emit(spec, name)

    # -------------------------------------------------------------- 过滤
    def _on_filter(self, text: str) -> None:
        q = str(text or "").strip().lower()

        def walk(item: QTreeWidgetItem) -> bool:
            d = _data(item)
            kind = d.get("kind")
            if kind == "table":
                label = f"{d.get('schema')}.{d.get('table')}".lower()
                hit = (not q) or (q in label)
                item.setHidden(not hit)
                return hit
            hit_any = False
            for i in range(item.childCount()):
                child = item.child(i)
                if _data(child).get("kind") in ("schema", "table"):
                    hit_any = walk(child) or hit_any
            if kind == "schema":
                item.setHidden(bool(q) and not hit_any)
                if q and hit_any:
                    item.setExpanded(True)
            return hit_any

        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            d = _data(top)
            if not d.get("loaded"):
                continue            # 还没展开的连接，过滤等它加载完再说
            hit_any = False
            for j in range(top.childCount()):
                child = top.child(j)
                if _data(child).get("kind") == "schema":
                    hit_any = walk(child) or hit_any
            top.setHidden(bool(q) and not hit_any)
            if q and hit_any:
                top.setExpanded(True)

    # -------------------------------------------------------------- 菜单
    def _menu_for(self, item: QTreeWidgetItem | None) -> QMenu:
        """节点右键菜单（拆出来是为了能测：不发鼠标事件也能看菜单项）。"""
        menu = QMenu(self)
        d = _data(item) if item is not None else {}
        kind = d.get("kind")
        if kind == "conn":
            conn = d.get("conn") or {}
            menu.addAction("刷新表清单", lambda: self._load_tables(item))
            menu.addAction("测试连接", lambda: self._on_test(conn))
            menu.addSeparator()
            menu.addAction("编辑…", lambda: self._on_edit(conn))
            menu.addAction("删除连接", lambda: self._on_remove(conn))
        elif kind == "schema":
            conn = d.get("conn") or {}
            menu.addAction("刷新表清单", lambda: self._load_tables(
                item.parent() or item))
            menu.addAction("复制 schema 名", lambda: QApplication.clipboard().setText(
                str(d.get("schema") or "")))
            menu.addSeparator()
            menu.addAction("测试连接", lambda: self._on_test(conn))
        elif kind == "table":
            if d.get("has_geom"):
                menu.addAction("加入地图", lambda: self._start_load(d))
                menu.addSeparator()
            else:
                act = menu.addAction("（没有几何列，不能加入地图）")
                act.setEnabled(False)
                menu.addSeparator()
            menu.addAction("复制表名", lambda: QApplication.clipboard().setText(
                f"{d.get('schema')}.{d.get('table')}"))
            menu.addAction("刷新字段", lambda: self._load_columns(item))
        else:
            menu.addAction("新建连接…", self.add_connection)
            menu.addAction("刷新全部", self.refresh_all)
        return menu

    def _on_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is not None:
            self.tree.setCurrentItem(item)
        menu = self._menu_for(item)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def refresh_all(self) -> None:
        """重新读每个连接的表清单（展开着的节点保持展开）。"""
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            d = _data(item)
            d["loaded"] = False
            d["loading"] = False
            item.setData(0, ROLE, d)
            item.setText(1, "")
            item.setToolTip(0, f"{item.text(0)}\n"
                               f"{db_conn.describe(d.get('conn') or {})}")
            self._placeholder(item, "…")
            if item.isExpanded():
                self._load_tables(item)
        self.hint.setText("已刷新表清单")

    # -------------------------------------------------------------- 增删改
    def add_connection(self) -> None:
        dlg = DbConnDialog(None, self.window())
        if dlg.exec() != QDialog.Accepted:
            return
        conn = getattr(dlg, "_conn", None) or db_conn.empty_conn()
        names = {c["name"] for c in self._conns}
        if conn["name"] in names:
            QMessageBox.warning(self.window(), "数据库连接",
                                f"已经有叫「{conn['name']}」的连接了，换个名字。")
            return
        self._conns.append(conn)
        self._persist()
        self._rebuild()
        self.hint.setText(f"已添加「{conn['name']}」：{db_conn.describe(conn)}")

    def _on_edit(self, conn: dict) -> None:
        dlg = DbConnDialog(conn, self.window())
        if dlg.exec() != QDialog.Accepted:
            return
        new = getattr(dlg, "_conn", None) or conn
        others = {c["name"] for c in self._conns if c["name"] != conn["name"]}
        if new["name"] in others:
            QMessageBox.warning(self.window(), "数据库连接",
                                f"已经有叫「{new['name']}」的连接了，换个名字。")
            return
        self._conns = [new if c["name"] == conn["name"] else c for c in self._conns]
        self._persist()
        self._rebuild()
        self.hint.setText(f"已保存「{new['name']}」")

    def _on_remove(self, conn: dict) -> None:
        box = QMessageBox(self.window())
        box.setWindowTitle("删除数据库连接")
        box.setText(f"删除「{conn['name']}」？")
        box.setInformativeText("只删这条连接配置，库里的数据不受影响；已经加到地图的图层也还在。")
        ok = box.addButton("删除", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not ok:
            return
        self._conns = [c for c in self._conns if c["name"] != conn["name"]]
        self._persist()
        self._rebuild()
        self.hint.setText(f"已删除「{conn['name']}」")

    # -------------------------------------------------------------- 测试
    def _on_test(self, conn: dict) -> None:
        if not db_conn.available():
            QMessageBox.warning(self.window(), "数据库连接", db_conn.driver_hint())
            return
        self.hint.setText(f"正在测试「{conn['name']}」…")
        worker = _TestWorker(conn, self)
        worker.result.connect(lambda res, c=conn: self._show_test(c, res))
        self._track(worker)
        worker.start()

    def _show_test(self, conn: dict, res: dict) -> None:
        res = res or {}
        if res.get("ok"):
            self.hint.setText(f"✓ {conn['name']}：" + str(res.get("message") or "已连接"))
            return
        self.hint.setText(f"✗ {conn['name']}：连接失败")
        QMessageBox.warning(self.window(), "连接失败",
                            str(res.get("message") or "未知错误"))

    # -------------------------------------------------------------- 线程
    def _track(self, worker: QThread) -> None:
        self._workers.append(worker)
        worker.finished.connect(lambda w=worker: self._forget(w))

    def _forget(self, worker: QThread) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

    def stop(self) -> None:
        """关窗口：让还在读表的线程在下一批数据前退出。"""
        if self._load_worker is not None:
            self._load_worker.stop()
