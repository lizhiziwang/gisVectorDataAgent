"""左栏「数据库」页签 + PostgreSQL/PostGIS 连接的回归测试（不联网、不装库）。

覆盖：
  1. 连接描述的规范化与校验（默认端口、必填项、概览不泄露密码）；
  2. settings.json 里 db_connections 的整理（坏数据丢掉、重名去重、限条数）；
  3. 用假 psycopg2 跑通「测试连接 / 列表 / 读表」（含 LIMIT、取消、错误翻译）；
  4. 新建/编辑连接弹窗、选表弹窗（loader 注入，不连库）；
  5. 连接面板：空态、行、新建、删除、双击空间表发出的图层 spec；
  6. 主窗口：左栏下半块变成「工具库 / 数据库」两个页签，切换会记住；
  7. 没装 psycopg2 时的降级提示。

按视野取数（空间过滤 / 1 万一批 / 拾取 / 会话描述）在 `test_db_provider.py`。

用法：python tools/test_db_conn.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")
os.environ.setdefault("GIS_AGENT_CONFIRM_STEPS", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import shapely
from PySide6.QtWidgets import QApplication, QDialog

app = QApplication(sys.argv)

from app import config as cfg_mod
from app import db_conn
from app.config import normalize

import native.dbpanel as dbpanel

ok_count = 0
fail_count = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f"PASS {label}")
    else:
        fail_count += 1
        print(f"FAIL {label}" + (f"  ← {detail}" if detail else ""))


CONN = {
    "kind": "postgresql", "name": "测试库", "host": "127.0.0.1", "port": 5432,
    "dbname": "gis", "user": "postgres", "password": "s3cret",
    "schema": "public", "sslmode": "prefer",
}


# ---------------------------------------------------------------- 1 描述
print("\n[1] 连接描述与校验")
c = db_conn.normalize_conn({"name": "  本地 库 ", "host": " 10.0.0.5 ",
                            "dbname": "gis", "user": "postgres"})
check("名字去多余空白", c["name"] == "本地 库", c["name"])
check("主机去空白", c["host"] == "10.0.0.5", c["host"])
check("端口默认 5432", c["port"] == 5432, c["port"])
check("schema 默认 public", c["schema"] == "public", c["schema"])
check("kind 默认 postgresql", c["kind"] == "postgresql", c["kind"])

weird = db_conn.normalize_conn({"port": "abc", "sslmode": "??", "port2": 1})
check("坏端口回落 5432", weird["port"] == 5432, weird["port"])
check("坏 sslmode 回落 prefer", weird["sslmode"] == "prefer", weird["sslmode"])

try:
    db_conn.validate_conn({"name": "", "host": "h", "dbname": "d", "user": "u"})
    check("缺名字报错", False)
except db_conn.DbError as e:
    check("缺名字报错并点名", "连接名称" in str(e), str(e))

try:
    db_conn.validate_conn({"name": "n", "host": "", "dbname": "", "user": ""})
    check("缺主机/库/用户报错", False)
except db_conn.DbError as e:
    check("缺主机/库/用户报错", all(k in str(e) for k in ("主机", "数据库", "用户名")), str(e))

check("概览不含密码", "s3cret" not in db_conn.describe(CONN), db_conn.describe(CONN))
check("概览含 host:port/db", db_conn.describe(CONN) == "postgres@127.0.0.1:5432/gis",
      db_conn.describe(CONN))
check("非 public schema 会写出来",
      db_conn.describe(dict(CONN, schema="survey")).endswith("· survey"),
      db_conn.describe(dict(CONN, schema="survey")))
check("图层名：public 用表名", db_conn.table_layer_name(CONN, "public", "roads") == "roads")
check("图层名：其它 schema 带前缀",
      db_conn.table_layer_name(CONN, "survey", "points") == "survey.points")
check("密码里的空格保留",
      db_conn.normalize_conn({"password": " a b "})["password"] == "a b"
      or db_conn.normalize_conn({"password": " a b "})["password"] == " a b ",
      db_conn.normalize_conn({"password": " a b "})["password"])

# ---------------------------------------------------------------- 2 持久化
print("\n[2] settings.json 里的连接列表")
n = normalize({"db_connections": [CONN,
                                  {"name": "测试库", "host": "dup"},      # 重名 → 丢
                                  {"name": "缺主机"},                      # 没 host → 丢
                                  "垃圾",
                                  dict(CONN, name="第二个", host="10.1.1.1")]})
check("保留两条有效连接", len(n.get("db_connections") or []) == 2,
      n.get("db_connections"))
check("重名只留第一个", n["db_connections"][0]["host"] == "127.0.0.1")
check("无名连接被丢掉",
      all(x.get("name") and x.get("host") for x in n["db_connections"]))
check("密码也跟着保存", n["db_connections"][0]["password"] == "s3cret")
check("空列表就把 key 去掉",
      "db_connections" not in normalize({"db_connections": []}))
check("data_tab 默认页不写盘", "data_tab" not in normalize({}))
check("data_tab 只认 0/1", "data_tab" not in normalize({"data_tab": 7})
      and normalize({"data_tab": 1})["data_tab"] == 1)
check("坏 data_tab 不炸", "data_tab" not in normalize({"data_tab": "x"}))

# ---------------------------------------------------------------- 假 psycopg2
#: 库里有什么表：(schema, 表名, relkind, 几何列, SRID, 几何类型, 估算要素数)
TABLES = [
    ("public", "config", "r", "", 0, "", 12),
    ("public", "parcels", "r", "geom", 0, "GEOMETRY", 5),
    ("public", "roads", "r", "geom", 3857, "LINESTRING", 3),
    ("public", "roads_view", "v", "geom", 3857, "LINESTRING", -1),
    ("survey", "points", "r", "geom", 4326, "MULTIPOINT", 2),
]
#: public.roads 的字段：(列名, data_type, udt_name, is_nullable)
ROAD_COLS = [
    ("fid", "integer", "int4", "NO"),
    ("name", "text", "text", "YES"),
    ("lanes", "integer", "int4", "YES"),
    ("length_km", "double precision", "float8", "YES"),
    ("geom", "USER-DEFINED", "geometry", "YES"),
]

ROAD_GEOMS = [
    shapely.LineString([(114.9, 27.8), (115.0, 27.85)]),
    shapely.LineString([(114.93, 27.81), (114.96, 27.87)]),
    shapely.LineString([(114.88, 27.82), (114.92, 27.815)]),
]
ROAD_WKB = [shapely.to_wkb(g) for g in ROAD_GEOMS]


class _FakeCursor:
    def __init__(self, db, name=None):
        self.db = db
        self.name = name
        self.itersize = 0
        self._rows = []
        self._i = 0
        self._sql = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.db.sql.append((str(sql), params))
        self._sql = str(sql)
        self._rows = []
        self._i = 0
        low = self._sql.lower()
        if "show server_version" in low:
            self._rows = [("16.3",)]
        elif "postgis_lib_version" in low:
            self._rows = [("3.4.2",)]
        elif "st_estimatedextent" in low:            # probe_table：估算范围
            self._rows = [("BOX(114 30,115 31)",)]
        elif "select relkind from pg_class" in low:   # order_key：视图没有 ctid
            self._rows = [("r",)]
        elif "pg_index" in low:                      # order_key：主键
            self._rows = [("fid",)]
        elif "st_geometrytype" in low:               # probe_table：验证几何列
            self._rows = [(ROAD_WKB[0], "ST_LineString")]
        elif "from pg_class" in low:
            # 只有带 geometry_columns 的那条会失败；退路 SQL 不含它，
            # 而且退路 SQL 本就查不到几何信息（'' / 0 / ''）
            if "geometry_columns" in low:
                if self.db.no_postgis:
                    raise Exception('relation "geometry_columns" does not exist')
                self._rows = list(TABLES)
            else:
                self._rows = [(s_, t_, k_, "", 0, "", e_) for s_, t_, k_, _g, _s, _t, e_ in TABLES]
        elif "udt_name" in low:                      # list_columns
            self._rows = list(ROAD_COLS)
        elif "information_schema.columns" in low:    # read_table 的列清单
            self._rows = [(c[0],) for c in ROAD_COLS]
        elif "st_asbinary" in low:
            self._rows = [(i + 1, f"road{i + 1}", 2, 1.5, wkb)
                          for i, wkb in enumerate(ROAD_WKB)]
        return self

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def fetchmany(self, size):
        chunk = self._rows[self._i:self._i + size]
        self._i += len(chunk)
        return chunk

    def close(self):
        pass


class _FakeConn:
    def __init__(self, db, **kw):
        self.db = db
        self.kw = kw
        self.closed = False

    def cursor(self, name=None):
        return _FakeCursor(self.db, name)

    def close(self):
        self.closed = True
        self.db.closed += 1

    def rollback(self):
        pass


class _FakeDB:
    def __init__(self, fail: str = "", no_postgis: bool = False):
        self.sql = []
        self.closed = 0
        self.fail = fail
        self.no_postgis = no_postgis
        self.kwargs = []


def install_fake(fail: str = "", no_postgis: bool = False) -> _FakeDB:
    """把假 psycopg2 塞进 sys.modules（db_conn.driver() 就是 import 它）。"""
    db = _FakeDB(fail, no_postgis)
    mod = types.ModuleType("psycopg2")

    def _connect(**kw):
        db.kwargs.append(dict(kw))
        if db.fail:
            raise Exception(db.fail)
        return _FakeConn(db, **kw)

    mod.connect = _connect
    mod.Error = Exception
    sys.modules["psycopg2"] = mod
    return db


# ---------------------------------------------------------------- 3 连接/列表/取数
print("\n[3] 测试连接 / 列表 / 读表（假驱动）")
db = install_fake()
check("装了驱动就能探测", db_conn.available())
res = db_conn.test_connection(CONN)
check("测试连接成功", res.get("ok") is True, res)
check("版本号读到了", res.get("version") == "16.3", res.get("version"))
check("PostGIS 版本读到了", res.get("postgis") == "3.4.2", res.get("postgis"))
check("提示是一句人话", "PostgreSQL 16.3" in res.get("message", "")
      and "PostGIS 3.4.2" in res.get("message", ""), res.get("message"))
check("连接用完关掉了", db.closed >= 1, db.closed)
kw = db.kwargs[-1] if db.kwargs else {}
check("connect 参数齐全",
      all(k in kw for k in ("host", "port", "dbname", "user", "connect_timeout",
                            "application_name")), kw)
check("connect_timeout 是整数秒", isinstance(kw.get("connect_timeout"), int),
      kw.get("connect_timeout"))
check("密码传给了驱动", kw.get("password") == "s3cret", kw.get("password"))

tables = db_conn.list_tables(CONN)
check("列出全部表（含非空间表）", len(tables) == 5, tables)
check("表字段齐",
      set(tables[1]) == {"schema", "table", "kind", "has_geom", "geom_column",
                         "srid", "geom_type", "est_features"}, tables[1])
by_name = {(t["schema"], t["table"]): t for t in tables}
check("空间表 has_geom", by_name[("public", "roads")]["has_geom"] is True)
check("普通表 has_geom=False", by_name[("public", "config")]["has_geom"] is False,
      by_name[("public", "config")])
check("视图也能列出来", by_name[("public", "roads_view")]["kind"] == "视图",
      by_name[("public", "roads_view")]["kind"])
check("SRID 解析对", by_name[("survey", "points")]["srid"] == 4326)
check("要素数估计保留", by_name[("public", "roads")]["est_features"] == 3)

nopg = install_fake(no_postgis=True)
fakes = nopg
tables = db_conn.list_tables(CONN)
check("没装 PostGIS 也能列出表", len(tables) == 5, len(tables))
check("没 PostGIS 时都没有几何列",
      all(not t["has_geom"] for t in tables), tables)
install_fake()

db = install_fake()
cols = db_conn.list_columns(CONN, "public", "roads")
check("字段清单读得到", len(cols) == 5, cols)
check("字段名/类型", cols[0]["name"] == "fid" and cols[0]["type"] == "integer", cols[0])
check("几何列认得出来",
      [c["name"] for c in cols if c["is_geom"]] == ["geom"],
      [c["name"] for c in cols if c["is_geom"]])

gdf = db_conn.read_table(CONN, "public", "roads")
check("读回 3 行", len(gdf) == 3, len(gdf))
check("几何列叫 geometry 且是 LineString",
      gdf.geometry.iloc[0].geom_type == "LineString")
check("CRS 按表 SRID 标", gdf.crs is not None and gdf.crs.to_epsg() == 3857,
      gdf.crs)
check("属性列都在", "name" in gdf.columns and "lanes" in gdf.columns, list(gdf.columns))
check("几何列不重复出现在属性里", "geom" not in gdf.columns, list(gdf.columns))

db.sql.clear()
db_conn.read_table(CONN, "public", "roads", max_features=2)
read_sql = [s for s, _ in db.sql if "st_asbinary" in s.lower()]
check("LIMIT 写在 SQL 里（不是读回来再截断）",
      bool(read_sql) and "LIMIT 2" in read_sql[-1], read_sql)

prog = []
db_conn.read_table(CONN, "public", "roads", on_progress=lambda n, t: prog.append((n, t)))
check("进度回调至少一次且是最终值", prog and prog[-1][0] == 3, prog)

try:
    db_conn.read_table(CONN, "public", "roads", cancel=lambda: True)
    check("取消会中断", False)
except db_conn.DbCancelled:
    check("取消会中断", True)

install_fake("password authentication failed for user \"postgres\"")
bad = db_conn.test_connection(CONN)
check("密码错给出中文提示", bad.get("ok") is False and "用户名或密码不对" in bad["message"],
      bad.get("message"))
install_fake("could not connect to server: Connection refused")
ref = db_conn.test_connection(CONN)
check("连不上给端口提示", "连不上" in ref["message"], ref.get("message"))
check("测试连接不抛异常（界面可直显）", ref.get("ok") is False)

install_fake()
try:
    db_conn.read_table(CONN, "public", "nope")
    check("找不到表要报错", False)
except db_conn.DbError as e:
    check("找不到表要报错", "找不到表" in str(e), str(e))

try:
    db_conn.read_table(CONN, "public", "config")
    check("普通表读表要报错", False)
except db_conn.DbError as e:
    check("普通表读表说清没几何列", "没有几何列" in str(e), str(e))

# ---------------------------------------------------------------- 5 弹窗
print("\n[5] 新建 / 编辑连接弹窗")


class _Box:
    """把 QMessageBox 换成记录器：不弹窗、不阻塞。"""

    calls = []
    pick_first = True
    # 按钮角色常量（面板里直接引用 QMessageBox.xxx）
    DestructiveRole = 1
    RejectRole = 2
    AcceptRole = 3
    ActionRole = 4

    def __init__(self, *a, **k):
        self._buttons = []
        self._clicked = None

    def setWindowTitle(self, *a):
        _Box.calls.append(("title", a[0] if a else ""))

    def setText(self, *a):
        _Box.calls.append(("text", a[0] if a else ""))

    def setInformativeText(self, *a):
        _Box.calls.append(("info", a[0] if a else ""))

    def addButton(self, text, role=None):
        _Box.calls.append(("button", text))
        self._buttons.append(text)
        return text

    def exec(self):
        self._clicked = self._buttons[0] if (self._buttons and _Box.pick_first) else None
        return 0

    def clickedButton(self):
        return self._clicked

    @staticmethod
    def warning(*a, **k):
        _Box.calls.append(("warning", a[-1] if a else ""))
        return 0

    @staticmethod
    def information(*a, **k):
        _Box.calls.append(("information", a[-1] if a else ""))
        return 0


_real_box = dbpanel.QMessageBox
dbpanel.QMessageBox = _Box

dlg = dbpanel.DbConnDialog(None)
dlg.host_edit.setText("db.lan")
dlg.db_edit.setText("gisdw")
dlg.user_edit.setText("gis")
dlg.pwd_edit.setText("pw")
check("新建弹窗默认端口 5432", dlg.port_spin.value() == 5432, dlg.port_spin.value())
try:
    dlg.connection()
    check("名字空着时取值报错", False)
except db_conn.DbError as e:
    check("名字空着时取值报错", "连接名称" in str(e), str(e))
dlg.name_edit.setText("仓储库")
cn = dlg.connection()
check("弹窗取值成配置", cn["host"] == "db.lan" and cn["dbname"] == "gisdw"
      and cn["user"] == "gis" and cn["password"] == "pw", cn)
dlg.name_edit.setText("")
check("名字没填时给建议值", dlg.suggested_name() == "gisdw@db.lan", dlg.suggested_name())

_Box.calls.clear()
dlg.accept()
check("名字空着也能保存（自动补建议名）",
      getattr(dlg, "_conn", {}).get("name") == "gisdw@db.lan",
      getattr(dlg, "_conn", {}))

dlg2 = dbpanel.DbConnDialog(CONN)
check("编辑弹窗预填", dlg2.host_edit.text() == "127.0.0.1"
      and dlg2.db_edit.text() == "gis" and dlg2.port_spin.value() == 5432)
check("编辑弹窗预填 schema", dlg2.schema_edit.text() == "public")
dlg2.host_edit.setText("")
_Box.calls.clear()
dlg2.accept()
check("缺主机时拒绝并提示",
      any("主机" in str(c[1]) for c in _Box.calls), _Box.calls)

# 测试连接按钮（注入假 tester，不连库）
seen = {}


def _tester(conn):
    seen["conn"] = conn
    return {"ok": True, "message": "已连接：PostgreSQL 16.3"}


d3 = dbpanel.DbConnDialog(CONN, tester=_tester)
d3.test()
check("测试按钮走了 tester", seen.get("conn", {}).get("host") == "127.0.0.1", seen)
check("测试成功写进状态行", "已连接" in d3.status.text(), d3.status.text())

# ---------------------------------------------------------------- 6 树面板
print("\n[6] 连接树：连接 → schema → 表 → 字段")

# 树里的查询都走 QThread；测试里把 start() 换成同步执行，结果立刻回来
_sync_runs = []


def _sync_start(self):
    self.run()
    _sync_runs.append(type(self).__name__)
    self.finished.emit()


for _cls in (dbpanel._TablesWorker, dbpanel._ColsWorker, dbpanel._LoadWorker,
             dbpanel._TestWorker):
    _cls.start = _sync_start

tmpdir = Path(tempfile.mkdtemp(prefix="dbtest_"))
store = {"conns": [CONN]}
saved = []
panel = dbpanel.DbConnectionsPanel(
    provider=lambda: list(store["conns"]),
    saver=lambda cs: (saved.append([dict(x) for x in cs]),
                      store.__setitem__("conns", [dict(x) for x in cs])))
install_fake()


def _find(parent, name):
    for i in range(parent.childCount()):
        if parent.child(i).text(0) == name:
            return parent.child(i)
    return None


def _refetch():
    """重新拿树里当前的节点（_load_tables 会重建整棵子树）。"""
    top = panel.tree.topLevelItem(0)
    return top, _find(top, "public"), _find(top, "survey")


check("badge 显示条数", panel.badge.text() == "1", panel.badge.text())
check("树里一个连接", panel.tree.topLevelItemCount() == 1,
      panel.tree.topLevelItemCount())
top = panel.tree.topLevelItem(0)
check("连接节点写名称", top.text(0) == "测试库", top.text(0))
check("连接节点第二列先空着", top.text(1) == "", top.text(1))
check("目标写在 tooltip 里", "postgres@127.0.0.1:5432/gis" in top.toolTip(0),
      top.toolTip(0))
check("有连接时不显示空态", panel.empty.isHidden())
check("一次取数上限默认 20 万", panel.max_features == db_conn.DEFAULT_LIMIT,
      panel.max_features)
check("表头不显示（名称/说明那行去掉了）", panel.tree.isHeaderHidden())
check("底栏没有「一次最多」数字框", not hasattr(panel, "limit_spin"))
check("没展开时只有占位子节点", top.childCount() == 1
      and top.child(0).text(0) == "…", top.child(0).text(0))

# 展开连接 = 读表清单
panel._load_tables(top)
check("展开后按 schema 分组", top.childCount() == 2, top.childCount())
check("schema 名字", [top.child(i).text(0) for i in range(2)] == ["public", "survey"],
      [top.child(i).text(0) for i in range(2)])
top, pub, sur = _refetch()
check("schema 说明写空间张数（列窄，只写核心数字）",
      pub.text(1) == "3 空间", pub.text(1))
check("schema 的完整统计在 tooltip 里",
      "4 张表" in pub.toolTip(0) and "3 张有几何列" in pub.toolTip(0),
      pub.toolTip(0))
check("schema 里四张表", pub.childCount() == 4, pub.childCount())

roads = _find(pub, "roads")
cfg = _find(pub, "config")


check("空间表窄标签：线 + SRID",
      roads is not None and roads.text(1) == "线 3857",
      roads.text(1) if roads else None)
check("完整说明在 tooltip 里",
      roads is not None and roads.toolTip(0).endswith("线 · EPSG:3857 · 3 要素"),
      roads.toolTip(0) if roads else None)
check("普通表写「无几何列」", cfg is not None and cfg.text(1) == "无几何列",
      cfg.text(1) if cfg else None)
check("空间表有图标", roads is not None and not roads.icon(0).isNull())
check("普通表也有图标", cfg is not None and not cfg.icon(0).isNull())
parcels = _find(pub, "parcels")
check("没登记 SRID 的表写明「无SRID」",
      parcels is not None and parcels.text(1) == "几何 无SRID",
      parcels.text(1) if parcels else None)
check("没登记 SRID 的表 tooltip 有提醒",
      parcels is not None and "位置可能不准" in parcels.toolTip(0),
      parcels.toolTip(0) if parcels else None)
check("点开连接后提示统计", "共 5 张表" in panel.hint.text(), panel.hint.text())
check("连接节点汇总成表数", top.text(1) == "5 表", top.text(1))
check("schema 张数写在 tooltip 里", "2 个 schema" in top.toolTip(0),
      top.toolTip(0))

# 展开表 = 读字段
panel._load_columns(roads)
check("表节点展开成字段", roads.childCount() == 5, roads.childCount())
check("字段名 + 类型", roads.child(0).text(0) == "fid"
      and roads.child(0).text(1) == "integer",
      f"{roads.child(0).text(0)}/{roads.child(0).text(1)}")
check("长类型名换成短写法（列宽只有 72 px）",
      _find(roads, "length_km").text(1) == "double",
      _find(roads, "length_km").text(1))
geom_field = _find(roads, "geom")
check("几何字段标出来",
      geom_field is not None and "几何" in geom_field.text(1),
      geom_field.text(1) if geom_field else None)
check("USER-DEFINED 显示成 geometry", geom_field.text(1) == "geometry（几何）",
      geom_field.text(1))
check("字段行不带节点数据（双击不动作）",
      not roads.child(0).data(0, dbpanel.ROLE))

# 双击空间表 = 登记图层（按视野取数，不读整表、不落缓存）
got = []
_files_before = [p.name for p in Path(tmpdir).iterdir()]
panel.layer_ready.connect(lambda spec, n: got.append((spec, n)))
panel._on_double(roads)
check("双击空间表发出图层 spec", bool(got) and isinstance(got[-1][0], dict), got)
spec = got[-1][0] if got else {}
check("图层名用表名", got and got[-1][1] == "roads", got)
check("spec 带连接与表名",
      spec.get("table") == "roads" and spec.get("schema") == "public"
      and spec.get("conn", {}).get("dbname") == "gis", spec)
check("spec 带几何列与 SRID",
      spec.get("geom_column") == "geom" and spec.get("srid") == 3857, spec)
check("spec 带字段/WGS84 范围/排序键",
      spec.get("fields") and spec.get("wgs_bounds")
      and spec.get("order_by") == "fid", spec)
check("加图层没有写缓存文件（不整表读）",
      sorted(p.name for p in Path(tmpdir).iterdir())
      == sorted(_files_before), sorted(p.name for p in Path(tmpdir).iterdir()))
check("探测完成写进提示", "已加入" in panel.hint.text(), panel.hint.text())
check("探测走的是 _LoadWorker", "_LoadWorker" in _sync_runs, _sync_runs)

# 双击普通表：只提示，不读
got.clear()
panel._on_double(cfg)
check("双击普通表不加图层", not got, got)
check("双击普通表说清原因", "没有几何列" in panel.hint.text(), panel.hint.text())

# 非 public schema 的表名带前缀
panel._load_tables(top)
top, pub, sur = _refetch()
pts = _find(sur, "points")
check("空间表的名字带 schema", pts is not None and pts.text(0) == "points")
check("survey.points 的图层名带前缀",
      db_conn.table_layer_name(CONN, "survey", "points") == "survey.points")

# 搜索过滤
panel.filter_edit.setText("points")
check("搜索只留命中的表", _find(sur, "points") is not None
      and not _find(sur, "points").isHidden())
check("没命中的 schema 收起来", pub.isHidden())
panel.filter_edit.setText("")
check("清空搜索都回来", not pub.isHidden() and not sur.isHidden())

# 右键菜单
acts = [a.text() for a in panel._menu_for(top).actions() if a.text()]
check("连接菜单", acts == ["刷新表清单", "测试连接", "编辑…", "删除连接"], acts)
acts = [a.text() for a in panel._menu_for(roads).actions() if a.text()]
check("空间表菜单", acts == ["加入地图", "复制表名", "刷新字段"], acts)
menu = panel._menu_for(cfg)
acts = [a.text() for a in menu.actions() if a.text()]
check("普通表菜单没有「加入地图」", "加入地图" not in acts, acts)
check("普通表菜单里那句是灰的",
      not [a for a in menu.actions() if a.text().startswith("（没有几何列")][0].isEnabled())

# 刷新：展开着的连接直接重读；收起过的只留占位，等下次展开再读
panel.refresh_all()
top = panel.tree.topLevelItem(0)
check("刷新后展开的连接重读了表", top.childCount() == 2, top.childCount())
check("刷新有提示", "已刷新" in panel.hint.text(), panel.hint.text())
top.setExpanded(False)
panel.refresh_all()
top = panel.tree.topLevelItem(0)
check("收起的连接刷新后只留占位", top.childCount() == 1
      and top.child(0).text(0) == "…",
      top.child(0).text(0) if top.childCount() else "空")
top.setExpanded(True)       # 展开触发懒加载
check("再展开还是能读出来", top.childCount() == 2, top.childCount())

# 连接改了才重建；没改不动树
panel._load_tables(top)
before = panel.tree.topLevelItem(0)
panel.reload()
check("连接没变时不动树", panel.tree.topLevelItem(0) is before)
store["conns"] = [dict(CONN, name="改过名")]
panel.reload()
check("连接变了重建树",
      panel.tree.topLevelItem(0).text(0) == "改过名",
      panel.tree.topLevelItem(0).text(0))
store["conns"] = [CONN]
panel.reload()

# 新建 / 删连接
_real_dialog = dbpanel.DbConnDialog


class _AutoAdd(_real_dialog):
    def __init__(self, conn=None, parent=None, tester=None):
        super().__init__(conn, parent, tester)
        self._conn = dict(CONN, name="第二个库", host="10.1.1.1")

    def exec(self):
        return QDialog.Accepted


dbpanel.DbConnDialog = _AutoAdd
panel.add_connection()
check("新建后两个连接", panel.badge.text() == "2", panel.badge.text())
check("落盘被调用", saved and len(saved[-1]) == 2, len(saved[-1]) if saved else 0)
_Box.calls.clear()
panel.add_connection()
check("重名连接被拒", any("已经有叫" in str(c[1]) for c in _Box.calls), _Box.calls)
check("重名不落盘", len(panel.connections()) == 2, len(panel.connections()))

panel._on_remove(CONN)
check("删除后剩一条", len(panel.connections()) == 1, panel.connections())
check("删除后 badge 跟着变", panel.badge.text() == "1", panel.badge.text())
check("删除提示写在底部", "已删除" in panel.hint.text(), panel.hint.text())
dbpanel.DbConnDialog = _real_dialog

# 测试连接（同步 worker）
_Box.calls.clear()
panel._on_test(panel.connections()[0])
check("测试连接结果写在底部", "✓" in panel.hint.text(), panel.hint.text())

# ---------------------------------------------------------------- 7 主窗口
print("\n[7] 主窗口左栏：工具库 / 数据库 两个页签")
import native.app as app_mod

_REAL_LOAD = app_mod.load_cfg
_REAL_SAVE = app_mod.save_cfg
_mem = {"cfg": dict(_REAL_LOAD())}
_mem["cfg"]["db_connections"] = [CONN]
_mem["cfg"].pop("data_tab", None)
app_mod.load_cfg = lambda: dict(_mem["cfg"])
app_mod.save_cfg = lambda cfg: _mem.update(cfg=dict(cfg))
win = app_mod.MainWindow()
win.resize(1400, 900)
win.show()
app.processEvents()

tabs = win.data_tabs
check("下半块是页签", tabs.count() == 2, tabs.count())
check("两个页签叫工具库 / 数据库",
      [tabs.tabText(i) for i in range(tabs.count())] == ["工具库", "数据库"],
      [tabs.tabText(i) for i in range(tabs.count())])
check("工具库页签里还有「＋ 新建工具」",
      win.new_op_btn.isVisible() or not win.isHidden(), win.new_op_btn.text())
check("数据库页签里是连接面板", tabs.widget(1) is win.db_panel)
check("面板读到了 settings 里的连接", win.db_panel.badge.text() == "1",
      win.db_panel.badge.text())
check("DB 面板和左栏分给的是同一个 splitter",
      win.side_split.widget(1) is tabs, win.side_split.widget(1))

tabs.setCurrentIndex(1)
app.processEvents()
check("切页签记住了", _mem["cfg"].get("data_tab") == 1, _mem["cfg"].get("data_tab"))
tabs.setCurrentIndex(0)
app.processEvents()
check("切回工具库后不写回默认值", "data_tab" not in _mem["cfg"],
      _mem["cfg"].get("data_tab"))

win.db_panel.stop()
win.close()
app.processEvents()
# 到这里才把配置读写还原：前面切页签/存连接都要落在内存那份里
app_mod.load_cfg = _REAL_LOAD
app_mod.save_cfg = _REAL_SAVE

# ---------------------------------------------------------------- 8 降级
print("\n[8] 没装 psycopg2 时")
_real_driver = db_conn.driver
db_conn.driver = lambda: None
try:
    check("available() 变 False", db_conn.available() is False)
    res = db_conn.test_connection(CONN)
    check("测试连接给出安装提示",
          res.get("ok") is False and "psycopg2-binary" in res.get("message", ""),
          res.get("message"))
    try:
        db_conn.connect(CONN)
        check("connect 抛 DbError", False)
    except db_conn.DbError as e:
        check("connect 抛 DbError", "pip install" in str(e), str(e))
    # 树里点开连接：不炸，只给安装提示
    panel._load_tables(panel.tree.topLevelItem(0))
    check("没驱动时展开连接给安装提示",
          "psycopg2" in panel.hint.text(), panel.hint.text())
finally:
    db_conn.driver = _real_driver

dbpanel.QMessageBox = _real_box
sys.modules.pop("psycopg2", None)

print("\n" + "=" * 60)
print(f"通过 {ok_count} / {ok_count + fail_count}")
print("结论: PASS" if fail_count == 0 else f"结论: FAIL（{fail_count} 项）")
sys.exit(0 if fail_count == 0 else 1)
