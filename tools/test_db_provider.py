"""PostGIS「按视野取数」的回归测试（不联网、不装库）。

覆盖：
  1. bbox 换算（4326 / 投影 / 无 SRID）与 SQL 形状（空间过滤 + 排序 + LIMIT）；
  2. 分批：网络往返固定 1 万行一批，画的时候再按调用方粒度切；
  3. 探测 `probe_table`：估算范围、退到 ST_Extent、几何列为空、排序键；
  4. `PostGISProvider`：元信息、WGS84 转换、标注锚点、属性表、拾取、整表缓存；
  5. `Layer(db_spec=...)` / `Store.add_db_source`：加图层不读数据；
  6. 会话描述：只存连接名（不含密码），恢复时按名字找回连接；
  7. MapView：DB 图层走流式渲染，但不建「整层 LOD 缓存」（那等于全表读）。

用法：python tools/test_db_provider.py
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

import shapely
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)

from app import db_conn
from app.db_provider import PostGISProvider
from app.engine import Layer, Store

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

#: 假库里的要素总数（够跑出 3 个网络批次：10000 + 10000 + 5000）
TOTAL = 25_000
LINE_WKB = shapely.to_wkb(shapely.LineString([(0.0, 0.0), (1.0, 1.0)]))
#: 投影坐标系里的同一条线（x 是米，换到 4326 大约是东经 0~2°）
MERC_WKB = shapely.to_wkb(shapely.LineString([(111_319.49, 0.0),
                                             (222_638.98, 0.0)]))


# ---------------------------------------------------------------- 假驱动
class _Cursor:
    def __init__(self, db, name=None):
        self.db = db
        self.name = name
        self.sql = ""
        self.params = ()
        self.itersize = 1
        self._rows: list = []
        self._i = 0
        self.closed = False

    def execute(self, sql, params=None):
        self.sql = " ".join(str(sql).split())
        self.params = tuple(params or ())
        self.db.log.append((self.sql, self.params))
        self._rows = self.db.answer(self.sql, self.params)
        self._i = 0

    def fetchmany(self, n):
        out = self._rows[self._i:self._i + int(n)]
        self._i += len(out)
        self.db.fetches.append(int(n))
        return out

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    def fetchall(self):
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class _Conn:
    def __init__(self, db):
        self.db = db
        self.closed = False

    def cursor(self, name=None):
        return _Cursor(self.db, name)

    def rollback(self):
        pass

    def close(self):
        self.closed = True
        self.db.closed += 1


class FakeDb:
    """够用的假 psycopg2：按 SQL 关键字回固定结果，并记下每次查询/取批。"""

    def __init__(self, total: int = TOTAL, no_est: bool = False,
                 relkind: str = "r", pkey: str | None = "gid",
                 empty_geom: bool = False, wkb: bytes = LINE_WKB,
                 no_name_col: bool = False, memview: bool = False,
                 null_geom: bool = False):
        self.total = int(total)
        self.no_est = no_est
        self.relkind = relkind
        self.pkey = pkey
        self.empty_geom = empty_geom
        self.wkb = wkb
        self.no_name_col = no_name_col
        #: psycopg2 的 ST_AsBinary() 给的是 memoryview（shapely 只认 bytes）
        self.memview = bool(memview)
        #: 表里混着 NULL 几何
        self.null_geom = bool(null_geom)
        self.log: list = []
        self.fetches: list = []
        self.closed = 0
        self.kwargs: list = []

    # -- 假驱动要回答的各种 SQL
    def answer(self, sql: str, params: tuple):
        low = sql.lower()
        if "information_schema.columns" in low:
            cols = [("gid", "integer", "int4", "NO"),
                    ("name", "text", "text", "NO")]
            if not self.no_name_col:
                cols.append(("lanes", "integer", "int4", "YES"))
            cols.append(("geom", "USER-DEFINED", "geometry", "YES"))
            return cols
        if "pg_index" in low:
            return [(self.pkey,)] if self.pkey else []
        if "relkind" in low:
            return [(self.relkind,)]
        if "st_estimatedextent" in low:
            if self.no_est:
                raise RuntimeError("no statistics")
            return [("BOX(0 0,1000 1000)",)]
        if "st_extent(" in low:
            return [("BOX(0 0,1000 1000)",)]
        if "st_geometrytype" in low:
            if self.empty_geom:
                return []
            return [(self.wkb, "ST_LineString")]
        if "st_asbinary" in low:
            cols = _select_cols(sql, before="st_asbinary")
            if "offset" in low:
                return [self._row(cols, 0)]
            return [self._row(cols, i) for i in range(self._count(sql))]
        if low.startswith("select "):
            cols = _select_cols(sql)
            if not cols:
                return []
            return [tuple(self._value(c, i) for c in cols)
                    for i in range(self._count(sql))]
        return []

    def _count(self, sql: str) -> int:
        limit = _sql_limit(sql)
        return self.total if limit is None else min(self.total, int(limit))

    def _value(self, name: str, i: int):
        if name == "gid":
            return i
        if name == "name":
            return f"r{i}"
        if name == "lanes":
            return i % 3
        return None

    def _row(self, cols: list, i: int) -> tuple:
        geom = self.wkb
        if self.null_geom and i % 3 == 1:
            geom = None
        if geom is not None and self.memview:
            geom = memoryview(geom)
        return tuple(self._value(c, i) for c in cols) + (geom,)


def _sql_limit(sql: str):
    up = sql.upper()
    if "LIMIT " not in up:
        return None
    try:
        tail = up.rsplit("LIMIT ", 1)[1].split()[0]
        return int(tail)
    except (IndexError, ValueError):
        return None


def _select_cols(sql: str, before: str | None = None) -> list:
    """从 SQL 里取出选中的列名（`before` 限定只看它之前的部分）。"""
    import re
    head = sql.split(" FROM ")[0] if " FROM " in sql else sql
    if before:
        low = head.lower()
        pos = low.find(before)
        if pos >= 0:
            head = head[:pos]
    return re.findall(r'"([^"]+)"', head)


def install_fake(**kw):
    """把假 psycopg2 塞进 sys.modules，返回 FakeDb（每次调用都是新库）。"""
    db = FakeDb(**kw)

    def _connect(**kwargs):
        db.kwargs.append(dict(kwargs))
        return _Conn(db)

    mod = types.ModuleType("psycopg2")
    mod.connect = _connect
    mod.Error = Exception
    sys.modules["psycopg2"] = mod
    return db


def spec_for(db=None, **over) -> dict:
    spec = {
        "conn": dict(CONN), "schema": "public", "table": "roads",
        "geom_column": "geom", "srid": 4326, "geom_type": "LineString",
        "fields": ["gid", "name"], "count": TOTAL, "order_by": "gid",
        "max_features": 200_000,
        "bounds": [0.0, 0.0, 1000.0, 1000.0],
        "wgs_bounds": [0.0, 0.0, 1.0, 1.0],
    }
    spec.update(over)
    return spec


# ---------------------------------------------------------------- 1 bbox / SQL
print("\n[1] bbox 换算与 SQL 形状")
check("4326 原样通过",
      db_conn.to_source_bbox((114.0, 30.0, 115.0, 31.0), 4326)
      == (114.0, 30.0, 115.0, 31.0))
same = db_conn.to_source_bbox((114.0, 30.0, 115.0, 31.0), 0)
check("没有 SRID 的表也不硬转", same == (114.0, 30.0, 115.0, 31.0), same)
proj = db_conn.to_source_bbox((114.0, 30.0, 115.0, 31.0), 3857)
check("投影坐标系转成米", 12_000_000 < proj[0] < 13_500_000 and
      (proj[2] - proj[0]) > 90_000, proj)

db = install_fake()
prov = PostGISProvider(spec_for(srid=3857))
list(prov.iter_bbox((114.0, 30.0, 115.0, 31.0)))
sql, params = db.log[-1]
check("SQL 用空间过滤", "ST_MakeEnvelope" in sql and "&&" in sql, sql[:120])
check("过滤框是投影坐标（服务端算）",
      12_000_000 < float(params[0]) < 13_500_000, params)
check("带宽上限写进 SQL", "LIMIT 200000" in sql, sql[-80:])
check("按稳定键排序", 'ORDER BY "gid"' in sql, sql[-120:])

db = install_fake()
prov = PostGISProvider(spec_for())
list(prov.iter_bbox((0.0, 0.0, 1.0, 1.0), simplify_tol=0.5))
sql = db.log[-1][0]
check("宽视野先服务端简化（省带宽）",
      "ST_SimplifyPreserveTopology" in sql, sql[:160])
tol_param = db.log[-1][1][0]
check("4326 的容差按度传（0.5）", abs(float(tol_param) - 0.5) < 1e-9, tol_param)

db = install_fake()
prov = PostGISProvider(spec_for(srid=3857))
list(prov.iter_bbox((114.0, 30.0, 115.0, 31.0), simplify_tol=0.5))
check("投影坐标系按米传（0.5° ≈ 55 km）",
      50_000 < float(db.log[-1][1][0]) < 60_000, db.log[-1][1][0])

db = install_fake()
prov = PostGISProvider(spec_for())
list(prov.iter_bbox((0.0, 0.0, 1.0, 1.0), simplify_tol=0.00001))
check("近景不做服务端简化（细节不丢）",
      "ST_SimplifyPreserveTopology" not in db.log[-1][0], db.log[-1][0][:160])

# ---------------------------------------------------------------- 2 分批
print("\n[2] 1 万行一批 / 取消 / 连接释放")
db = install_fake(total=TOTAL)
prov = PostGISProvider(spec_for())
sizes = [len(g) for g in prov.iter_bbox((0.0, 0.0, 1.0, 1.0), batch_size=4096)]
check("画的时候按调用方粒度切", sizes[:2] == [4096, 4096] and sum(sizes) == TOTAL,
      sizes[:4])
check("网络往返固定 1 万行",
      max(db.fetches) == 10_000 and db.fetches.count(10_000) >= 2, db.fetches[:6])
check("拉完就把连接关掉", db.closed == 1, db.closed)

db = install_fake()
prov = PostGISProvider(spec_for())
gen = prov.iter_bbox((0.0, 0.0, 1.0, 1.0))
next(gen)
gen.close()
check("中途取消也关连接", db.closed == 1, db.closed)

db = install_fake()
try:
    for _ in db_conn.iter_bbox(CONN, "public", "roads", "geom", 4326,
                               (0.0, 0.0, 1.0, 1.0), cancel=lambda: True):
        pass
    check("取消抛 DbCancelled", False)
except db_conn.DbCancelled:
    check("取消抛 DbCancelled", True)

# ---------------------------------------------------------------- 3 探测
print("\n[3] 加图层前的探测（不读整表）")
db = install_fake()
info = db_conn.probe_table(CONN, "public", "roads", geom_column="geom",
                           srid=4326, est_features=690)
check("探测成功", info.get("ok") is True, info)
check("范围解析出来了", info.get("bounds") == [0.0, 0.0, 1000.0, 1000.0],
      info.get("bounds"))
check("4326 的范围就是 WGS84 范围",
      info.get("wgs_bounds") == [0.0, 0.0, 1000.0, 1000.0], info.get("wgs_bounds"))
check("字段不含几何列", info.get("fields") == ["gid", "name", "lanes"],
      info.get("fields"))
check("几何类型读到了", info.get("geom_type") == "LineString", info.get("geom_type"))
check("行数用估计值（不 count(*)）", info.get("count") == 690, info.get("count"))
check("排序键是主键", info.get("order_by") == "gid", info.get("order_by"))
check("没有 count(*) 这种全表扫描",
      not any("count(*)" in s.lower() for s, _ in db.log),
      [s[:60] for s, _ in db.log])

db = install_fake(no_est=True)
info = db_conn.probe_table(CONN, "public", "roads", geom_column="geom", srid=4326)
check("没统计信息时退到 ST_Extent", info.get("bounds") == [0.0, 0.0, 1000.0, 1000.0],
      info.get("bounds"))
check("ST_Extent 挂了语句超时",
      any("statement_timeout" in s.lower() for s, _ in db.log),
      [s[:40] for s, _ in db.log])

db = install_fake(empty_geom=True)
info = db_conn.probe_table(CONN, "public", "roads", geom_column="geom", srid=4326)
check("表里没有非空几何要报清楚",
      info.get("ok") is False and "没有非空几何" in info.get("message", ""), info)

db = install_fake(pkey=None)
check("没主键就退回 ctid", db_conn.order_key(CONN, "public", "roads") == "ctid")
db = install_fake(relkind="v")
check("视图不排序（没有 ctid）", db_conn.order_key(CONN, "public", "roads") == "")

# 面板把探测结果包成 spec 时不能把 "" 兜底成 ctid（视图没有 ctid）
from native import dbpanel  # noqa: E402

worker = dbpanel._LoadWorker(CONN, "public", "roads", geom_column="geom",
                             srid=4326)
out = {}
worker.done.connect(lambda spec, err: out.update(spec=spec, err=err))
worker.run()
check("视图图层不带排序键", out.get("spec", {}).get("order_by") == "",
      out.get("spec", {}).get("order_by"))
worker2 = dbpanel._LoadWorker(CONN, "public", "roads", geom_column="geom",
                              srid=4326, est_features=-1)
out2 = {}
worker2.done.connect(lambda spec, err: out2.update(spec=spec, err=err))
worker2.run()
check("从没 analyze 的表不写负数要素（reltuples = -1）",
      out2.get("spec", {}).get("count") == 0,
      out2.get("spec", {}).get("count"))

# ---------------------------------------------------------------- 4 provider
print("\n[4] provider 元信息 / WGS84 转换")
db = install_fake()
prov = PostGISProvider(spec_for(srid=3857, bounds=[0, 0, 1000, 1000],
                                wgs_bounds=[0, 0, 0.01, 0.01]))
meta = prov.meta()
check("要素数来自估计值", meta["count"] == TOTAL, meta["count"])
check("CRS 按 SRID 标", meta["crs"] == "EPSG:3857", meta["crs"])
check("字段清单对", meta["fields"] == ["gid", "name"], meta["fields"])
check("几何类型对", meta["geomType"] == "LineString", meta["geomType"])
check("元信息不碰数据库", not db.log, db.log[:2])
check("live 标记打开（禁止整层 LOD）", prov.live is True)
check("范围回缓存值", prov.wgs_bounds == (0.0, 0.0, 0.01, 0.01), prov.wgs_bounds)

db = install_fake(wkb=MERC_WKB)
geoms = next(iter(PostGISProvider(spec_for(srid=3857)).iter_bbox(
    (0.0, 0.0, 1.0, 1.0))))
xy = None if geoms is None else shapely.get_coordinates(geoms[0])
check("投影坐标转成了 WGS84",
      xy is not None and len(xy) and abs(float(xy[0][0]) - 1.0) < 0.01,
      None if xy is None else str(xy[:1]))

db = install_fake(total=5, memview=True)
geoms = list(PostGISProvider(spec_for()).iter_bbox((0.0, 0.0, 1.0, 1.0)))
check("驱动回来的 memoryview 也能解出来（否则整屏空白）",
      sum(len(g) for g in geoms) == 5, [len(g) for g in geoms])

db = install_fake(total=9, null_geom=True)
geoms = list(PostGISProvider(spec_for()).iter_bbox((0.0, 0.0, 1.0, 1.0)))
check("NULL 几何按行剔掉，不影响其余要素",
      sum(len(g) for g in geoms) == 6, [len(g) for g in geoms])

# ---------------------------------------------------------------- 5 属性/拾取
print("\n[5] 属性表 / 拾取 / 标注")
db = install_fake(total=TOTAL)
prov = PostGISProvider(spec_for())
df = prov.attrs_frame()
check("属性表读得到", len(df) == TOTAL and list(df.columns) == ["gid", "name"],
      list(df.columns))
n_attrs = len([s for s, _ in db.log if s.lower().startswith('select "gid"')])
prov.attrs_frame()
check("属性表只读一次（缓存）",
      len([s for s, _ in db.log if s.lower().startswith('select "gid"')]) == n_attrs,
      n_attrs)
check("按行取属性", prov.attrs_at(3).get("name") == "r3", prov.attrs_at(3))

prov = PostGISProvider(spec_for())
batches = list(prov.iter_label_anchors(["name"], (0.0, 0.0, 1.0, 1.0)))
check("标注锚点批次数", len(batches) >= 1, len(batches))
lons, lats, ang, attrs = batches[0]
check("锚点数量与属性对齐",
      len(lons) == len(lats) == len(ang) == len(attrs.get("name", [])), len(lons))

db = install_fake(total=10)
prov = PostGISProvider(spec_for())
hit = prov.hit_test(0.5, 0.5, 0.01)
check("拾取命中", hit is not None and hit[1].get("name") == "r0", hit)
check("拾取行号是属性表行号", hit is not None and hit[0] == 0, hit)
geom = prov.feature_geom(hit[0])
check("命中几何走缓存（不再查库）",
      geom is not None and len(db.log) == 1, len(db.log))
geom7 = prov.feature_geom(7)
check("按行号取几何", geom7 is not None and geom7.geom_type == "LineString", geom7)
check("按行号取几何用 OFFSET",
      any("OFFSET 7" in s for s, _ in db.log), [s[-60:] for s, _ in db.log][-1:])
check("点远处拾不到", prov.hit_test(50.0, 50.0, 0.001) is None)

# ---------------------------------------------------------------- 6 Layer
print("\n[6] Layer / Store：加图层不读数据")
db = install_fake()
lay = Layer(id="D1", name="roads", gdf=None, db_spec=spec_for())
check("图层拿到 PostGIS provider", isinstance(lay.provider, PostGISProvider),
      type(lay.provider))
m = lay.meta()
check("图层元信息齐", m["count"] == TOTAL and m["geomType"] == "LineString", m)
check("图层几何类型", lay.geom_type == "LineString", lay.geom_type)
check("图层 WGS84 范围", lay.wgs_bounds == (0.0, 0.0, 1.0, 1.0), lay.wgs_bounds)
check("没有本地文件（渲染按视野查）",
      lay.source_path is None and lay.cache_path is None)
check("元信息全程没连库", not db.log, db.log[:2])

from native.mapview import _stream_enabled  # noqa: E402
check("DB 图层走流式渲染", _stream_enabled(lay) is True)

tmpdir = Path(tempfile.mkdtemp(prefix="dbprov_"))
store = Store(tmpdir)
lay1 = store.add_db_source(spec_for(), "roads")
lay2 = store.add_db_source(spec_for(), "roads")
check("图层 id 前缀 D", lay1.id.startswith("D") and lay2.id.startswith("D"), lay1.id)
check("同名自动加后缀", lay2.name == "roads_2", lay2.name)
check("store 里两道图层", len(store.layers) == 2, len(store.layers))
check("加图层没有写缓存文件", not list(tmpdir.glob("*.parquet")),
      list(tmpdir.iterdir()))

# ---------------------------------------------------------------- 7 LOD
print("\n[7] 数据库图层不建整层 LOD 缓存")


class _Worker:
    def __init__(self):
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)


class _View:
    def __init__(self):
        self.visible = {lay1.id: True}
        self._lod_worker = _Worker()

    def _view_need(self, c):
        return (0.0, 0.0, 1.0, 1.0)

    def mpp(self):
        return 100.0


class _Cache:
    def __init__(self, provider):
        self.stream = True
        self.provider = provider
        self.lod_state = ""
        self.total = 10_000_000
        self.extent = (0.0, 0.0, 1.0, 1.0)


from native.mapview import MapView  # noqa: E402

view = _View()
MapView._maybe_start_lod(view, lay1, _Cache(lay1.provider))
check("DB 图层不排 LOD 任务（否则=全表读）", not view._lod_worker.jobs,
      view._lod_worker.jobs)

# ---------------------------------------------------------------- 8 会话
print("\n[8] 会话描述：只存连接名，不存密码")
import json  # noqa: E402

import native.app as app_mod  # noqa: E402

store2 = Store(tmpdir)
store2.add_db_source(spec_for(), "roads")
stub = app_mod.MainWindow.__new__(app_mod.MainWindow)
stub.store = store2
stub.agent_db = None
descs = app_mod.MainWindow._layer_descriptors(stub)
check("会话里带 db 描述", bool(descs and descs[0].get("db")), descs[:1])
text = json.dumps(descs)
check("会话里没有密码", "s3cret" not in text)
check("会话里记了连接名", descs[0]["db"]["conn_name"] == "测试库")
check("会话里记了表与范围",
      descs[0]["db"]["table"] == "roads"
      and descs[0]["db"]["wgs_bounds"] == [0.0, 0.0, 1.0, 1.0], descs[0]["db"])
check("会话里 DB 图层没有缓存路径", not descs[0].get("cache_path"),
      descs[0].get("cache_path"))

app_mod.load_cfg = lambda: {"db_connections": [dict(CONN)]}
spec = app_mod.MainWindow._db_spec_from_descriptor(stub, descs[0]["db"], "roads")
check("恢复时按名字找回连接", bool(spec) and spec["conn"]["password"] == "s3cret",
      None if not spec else spec["conn"]["name"])
check("恢复出的 spec 完整",
      spec and spec["table"] == "roads" and spec["srid"] == 4326
      and spec["wgs_bounds"] == [0.0, 0.0, 1.0, 1.0], spec)
missing = app_mod.MainWindow._db_spec_from_descriptor(
    stub, dict(descs[0]["db"], conn_name="已删掉的连接", target=""), "roads")
check("连接没了就恢复不出来（上层报警）", missing is None, missing)

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
print(f"通过 {ok_count} / {ok_count + fail_count}")
print("结论: " + ("PASS" if fail_count == 0 else f"FAIL（{fail_count} 项）"))
sys.exit(0 if fail_count == 0 else 1)
