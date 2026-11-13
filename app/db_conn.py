"""数据库连接：连接描述、探测、表清单与取数（先支持 PostgreSQL / PostGIS）。

这个模块只做「纯逻辑」：**不 import Qt**，也不在导入时 import 驱动。
没有 psycopg2 的机器照样能启动、照样能读写连接配置，只是点「测试连接 /
浏览表」时会得到一句「需要先装 psycopg2-binary」的提示。

为什么要自己拼 SQL 取几何：本项目自带的 GDAL（pyogrio / rasterio 的 wheel）
没编译 PostgreSQL 驱动，`pyogrio.list_drivers()` 里没有 `PostgreSQL`。
所以走 psycopg2 直连，几何列用 `ST_AsBinary()` 取 WKB 再交给 shapely ——
这条路不依赖 GDAL，也不依赖服务端装 PostGIS 之外的东西（PostGIS 是必需的，
`geometry_columns` 视图就来自它）。
"""
from __future__ import annotations

import math

#: 支持的数据库类型。以后加 MySQL / Oracle / 达梦，只需要在这里补一项 +
#: `connect()` 里加一个分支；界面上的下拉框读的就是这里。
DB_KINDS = {"postgresql": "PostgreSQL / PostGIS"}
KIND_ORDER = ("postgresql",)
DEFAULT_PORTS = {"postgresql": 5432}
#: PostgreSQL 的默认 schema；不是 public 的表要带上 schema 才不会撞名。
DEFAULT_SCHEMA = "public"
#: 连接配置的条数上限（settings.json 里不该无限长）。
MAX_CONNECTIONS = 30
#: 单张表默认最多读多少要素。0 = 不限（大表慎用，几十万行会吃掉几百 MB）。
DEFAULT_LIMIT = 200_000
#: 网络探测的默认超时（秒）：连不上要尽快报错，别让界面像卡死。
TIMEOUT = 6.0
#: 服务端游标的批大小：一次 2 万行，够摊薄往返，又不至于一口气吃满内存。
BATCH = 20_000

SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


class DbError(ValueError):
    """连接配置或数据库操作失败：消息直接给用户看。"""


class DbCancelled(Exception):
    """用户中途取消（关窗口 / 点取消）：和失败分开，不弹错误框。"""


# ---------------------------------------------------------------- 连接描述
def empty_conn(kind: str = "postgresql") -> dict:
    """新建连接的初值：端口按类型给默认值，其余留空。"""
    kind = kind if kind in DB_KINDS else "postgresql"
    return {
        "kind": kind,
        "name": "",
        "host": "",
        "port": DEFAULT_PORTS.get(kind, 5432),
        "dbname": "",
        "user": "",
        "password": "",
        "schema": DEFAULT_SCHEMA,
        "sslmode": "prefer",
    }


def _text(value, limit: int = 120) -> str:
    """去首尾空白、把连续空白压成一个空格，再按需截断。"""
    s = " ".join(str(value or "").split())
    return s[:limit]


def normalize_conn(raw: dict | None) -> dict:
    """把一条连接配置整理成固定字段顺序的 dict，坏值一律回落到默认值。"""
    out = empty_conn()
    if not isinstance(raw, dict):
        raw = {}
    kind = _text(raw.get("kind"), 24).lower()
    if kind in DB_KINDS:
        out["kind"] = kind
    out["name"] = _text(raw.get("name"), 40)
    # 主机名里允许出现空格以外的字符；用户常从别处粘出 " 10.0.0.5 " 这种
    out["host"] = _text(raw.get("host"), 200)
    default_port = DEFAULT_PORTS.get(out["kind"], 5432)
    try:
        port = int(raw.get("port") or default_port)
    except (TypeError, ValueError):
        port = default_port
    out["port"] = port if 1 <= port <= 65535 else default_port
    out["dbname"] = _text(raw.get("dbname"), 120)
    out["user"] = _text(raw.get("user"), 120)
    # 密码原样保留（可以有空格），只去掉误粘进来的换行
    out["password"] = str(raw.get("password") or "").strip("\r\n")
    out["schema"] = _text(raw.get("schema"), 120) or DEFAULT_SCHEMA
    ssl = _text(raw.get("sslmode"), 24).lower()
    out["sslmode"] = ssl if ssl in SSLMODES else "prefer"
    return out


def validate_conn(raw: dict | None) -> dict:
    """校验收件：必填项缺失时抛 `DbError`，消息里点名缺哪一项。"""
    conn = normalize_conn(raw)
    missing = []
    if not conn["name"]:
        missing.append("连接名称")
    if not conn["host"]:
        missing.append("主机")
    if not conn["dbname"]:
        missing.append("数据库")
    if not conn["user"]:
        missing.append("用户名")
    if missing:
        raise DbError("请填写：" + "、".join(missing))
    return conn


def target_text(conn: dict) -> str:
    """一行「user@host:port/db」摘要；**不带密码**，可以放进界面/日志。"""
    c = normalize_conn(conn)
    user = c["user"] or "?"
    host = c["host"] or "?"
    return f"{user}@{host}:{c['port']}/{c['dbname'] or '?'}"


def describe(conn: dict) -> str:
    """`target_text` + 非默认 schema，用于列表行的第二行。"""
    c = normalize_conn(conn)
    text = target_text(c)
    if c["schema"] and c["schema"] != DEFAULT_SCHEMA:
        text += f" · {c['schema']}"
    if c["sslmode"] and c["sslmode"] != "prefer":
        text += f" · ssl={c['sslmode']}"
    return text


def normalize_list(items) -> list[dict]:
    """连接列表整体整理：丢掉没名字/没主机的、重名的只留第一个、限条数。"""
    out: list[dict] = []
    names: set[str] = set()
    for raw in items or []:
        try:
            conn = normalize_conn(raw)
        except Exception:      # noqa: BLE001  坏数据不该让启动失败
            continue
        if not conn["name"] or not conn["host"] or conn["name"] in names:
            continue
        names.add(conn["name"])
        out.append(conn)
        if len(out) >= MAX_CONNECTIONS:
            break
    return out


# ---------------------------------------------------------------- 驱动
def driver():
    """返回 psycopg2 模块；没装返回 None（不抛异常，界面要能降级）。"""
    try:
        import psycopg2            # noqa: PLC0415  可选依赖，导入失败要能兜住
        return psycopg2
    except Exception:              # noqa: BLE001
        return None


def driver_hint() -> str:
    return ("连接 PostgreSQL 需要 psycopg2-binary，先装一下：\n"
            "  .venv\\Scripts\\python.exe -m pip install psycopg2-binary")


def require_driver():
    mod = driver()
    if mod is None:
        raise DbError(driver_hint())
    return mod


def available() -> bool:
    """驱动在不在（界面据此决定要不要提示安装）。"""
    return driver() is not None


# ---------------------------------------------------------------- 连接
def _friendly(err: Exception, conn: dict) -> str:
    """把 psycopg2 的英文异常翻成一句能照着修的中文。"""
    raw = str(err).strip() or err.__class__.__name__
    low = raw.lower()
    if "password authentication failed" in low or "认证失败" in raw:
        return f"用户名或密码不对（{conn['user']}@{conn['host']}）。\n{raw}"
    if "does not exist" in low and "database" in low:
        return f"数据库「{conn['dbname']}」不存在。\n{raw}"
    if "does not exist" in low and "role" in low:
        return f"用户「{conn['user']}」不存在。\n{raw}"
    if ("connection refused" in low or "could not connect" in low
            or "timeout" in low or "timed out" in low):
        return (f"连不上 {conn['host']}:{conn['port']}：端口不通或被防火墙挡住。\n{raw}")
    if "no pg_hba.conf entry" in low:
        return f"服务端的 pg_hba.conf 不允许本机连接。\n{raw}"
    if "server does not support ssl" in low:
        return f"服务端不支持 SSL，把「SSL 模式」改成 disable 再试。\n{raw}"
    return raw


def connect(conn: dict, timeout: float = TIMEOUT):
    """建一条连接（调用方负责 close）。失败抛 `DbError`，消息可直接展示。"""
    psycopg2 = require_driver()
    c = normalize_conn(conn)
    kwargs = {
        "host": c["host"],
        "port": c["port"],
        "dbname": c["dbname"],
        "user": c["user"],
        "connect_timeout": max(1, int(math.ceil(float(timeout or TIMEOUT)))),
        "application_name": "gis-agent",
    }
    if c["password"]:
        kwargs["password"] = c["password"]
    if c["sslmode"]:
        kwargs["sslmode"] = c["sslmode"]
    try:
        return psycopg2.connect(**kwargs)
    except DbError:
        raise
    except Exception as e:         # noqa: BLE001
        raise DbError(_friendly(e, c)) from None


def test_connection(conn: dict, timeout: float = TIMEOUT) -> dict:
    """探测连接：返回 {ok, message, version, postgis}；失败也返回 ok=False。

    不抛异常，界面可以直接把 `message` 显示出来。
    """
    c = normalize_conn(conn)
    out = {"ok": False, "message": "", "version": "", "postgis": "",
           "target": target_text(c)}
    try:
        db = connect(c, timeout=timeout)
    except DbError as e:
        out["message"] = str(e)
        return out
    try:
        with db.cursor() as cur:
            cur.execute("SHOW server_version")
            row = cur.fetchone()
            version = str(row[0]) if row else ""
            postgis = ""
            try:
                cur.execute("SELECT postgis_lib_version()")
                row = cur.fetchone()
                postgis = str(row[0]) if row else ""
            except Exception:      # noqa: BLE001  没有 PostGIS 不算连接失败
                db.rollback()
        out.update(ok=True, version=version, postgis=postgis)
        bits = [f"PostgreSQL {version}".strip()]
        if postgis:
            bits.append(f"PostGIS {postgis}")
        out["message"] = "已连接：" + " · ".join(b for b in bits if b)
    except Exception as e:         # noqa: BLE001
        out["message"] = _friendly(e, c)
    finally:
        try:
            db.close()
        except Exception:          # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------- 表清单
#: 所有能当表看的关系（普通表 / 分区表 / 视图 / 物化视图），LEFT JOIN 出几何信息
_TABLE_SQL = """
SELECT n.nspname, c.relname, c.relkind::text,
       COALESCE(gc.f_geometry_column, ''), COALESCE(gc.srid, 0),
       COALESCE(gc.type, ''), COALESCE(c.reltuples, -1)::bigint AS est
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN geometry_columns gc
       ON gc.f_table_schema = n.nspname AND gc.f_table_name = c.relname
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'topology', 'tiger')
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND n.nspname NOT LIKE 'pg\\_temp%'
ORDER BY n.nspname, c.relname
"""

#: 没装 PostGIS 时的退路：照样列出表，只是都没有几何列（浏览得到、加不到地图）
_TABLE_SQL_NO_POSTGIS = """
SELECT n.nspname, c.relname, c.relkind::text,
       '', 0, '', COALESCE(c.reltuples, -1)::bigint AS est
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'topology', 'tiger')
  AND n.nspname NOT LIKE 'pg\\_toast%'
  AND n.nspname NOT LIKE 'pg\\_temp%'
ORDER BY n.nspname, c.relname
"""

#: relkind → 中文（树里显示用）
RELKIND_CN = {"r": "表", "p": "分区表", "v": "视图", "m": "物化视图"}


def list_tables(conn: dict, timeout: float = TIMEOUT) -> list[dict]:
    """列出库里所有能当表看的关系（Navicat 那样的树用它）。

    每项：{schema, table, kind, has_geom, geom_column, srid, geom_type, est_features}。
    有几何列的（`has_geom`）才可以加到地图；没有的也列出来 —— 浏览表清单时
    看见「哪些不是空间表」比干脆看不见有用。

    `est_features` 是 `pg_class.reltuples` 的估计值（-1 = 还没统计过），
    只是给用户一个量级，不为了精确去 `count(*)`（大表会跑很久）。
    """
    c = normalize_conn(conn)
    db = connect(c, timeout=timeout)
    rows = []
    try:
        with db.cursor() as cur:
            try:
                cur.execute(_TABLE_SQL)
                rows = cur.fetchall() or []
            except Exception as e:                  # noqa: BLE001
                db.rollback()
                if "geometry_columns" not in str(e).lower():
                    raise DbError(_friendly(e, c)) from None
                # 没装 PostGIS：照样把表列出来，只是都没几何列
                cur.execute(_TABLE_SQL_NO_POSTGIS)
                rows = cur.fetchall() or []
    finally:
        try:
            db.close()
        except Exception:          # noqa: BLE001
            pass
    out = []
    for schema, table, kind, geom_col, srid, geom_type, est in rows:
        try:
            est = int(est)
        except (TypeError, ValueError):
            est = -1
        if est <= 0:
            # reltuples = 0 多半是「没 analyze 过」，不是真空表 —— 别显示成 0 要素
            est = -1
        out.append({
            "schema": str(schema),
            "table": str(table),
            "kind": RELKIND_CN.get(str(kind or "r"), "表"),
            "has_geom": bool(geom_col),
            "geom_column": str(geom_col or ""),
            "srid": int(srid or 0),
            "geom_type": str(geom_type or ""),
            "est_features": est,
        })
    return out


_COLUMNS_SQL = """
SELECT column_name, data_type, udt_name, is_nullable
FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s
ORDER BY ordinal_position
"""


def list_columns(conn: dict, schema: str, table: str,
                 timeout: float = TIMEOUT) -> list[dict]:
    """一张表的字段清单（展开表节点时看字段，Navicat 那样）。

    每项：{name, type, is_geom}。`is_geom` 认 geometry / geography 两种。
    """
    c = normalize_conn(conn)
    db = connect(c, timeout=timeout)
    try:
        with db.cursor() as cur:
            cur.execute(_COLUMNS_SQL, (str(schema), str(table)))
            rows = cur.fetchall() or []
    except Exception as e:                      # noqa: BLE001
        raise DbError(_friendly(e, c)) from None
    finally:
        try:
            db.close()
        except Exception:          # noqa: BLE001
            pass
    out = []
    for name, dtype, udt, _nullable in rows:
        udt = str(udt or "")
        out.append({
            "name": str(name),
            "type": str(dtype or udt),
            "is_geom": udt.lower() in ("geometry", "geography"),
        })
    return out


# ---------------------------------------------------------------- 取数
def _q(name: str) -> str:
    """SQL 标识符引号：表名/列名里可能有大写、空格、中文。"""
    return '"' + str(name).replace('"', '""') + '"'


def _check_cancel(cancel) -> None:
    if cancel is None:
        return
    try:
        if callable(cancel):
            stop = bool(cancel())
        else:
            stop = bool(getattr(cancel, "is_set", lambda: False)())
    except Exception:              # noqa: BLE001
        stop = False
    if stop:
        raise DbCancelled()


def _columns(db, schema: str, table: str) -> list[str]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position", (schema, table))
        return [str(r[0]) for r in (cur.fetchall() or [])]


def read_table(conn: dict, schema: str, table: str,
               geom_column: str = "", max_features: int = DEFAULT_LIMIT,
               on_progress=None, cancel=None,
               timeout: float = TIMEOUT):
    """把一张表读成 GeoDataFrame（几何列转 shapely，CRS 用表的 SRID）。

    · `max_features` > 0 时在 SQL 里加 LIMIT（少传数据，不是读回来再截断）；
    · `on_progress(已读, 总量或 -1)` 每批回调一次，界面据此更新状态；
    · `cancel` 可以是 callable 或 threading.Event，取消时抛 `DbCancelled`。
    """
    import geopandas as gpd            # noqa: PLC0415  取数时才用，启动不拖慢
    import pandas as pd                # noqa: PLC0415
    import shapely                     # noqa: PLC0415

    c = normalize_conn(conn)
    tables = None
    srid = 0
    if not geom_column:
        # 没指定几何列：从表清单里找这张表自带的那个几何列
        tables = [t for t in list_tables(c, timeout=timeout)
                  if t["schema"] == schema and t["table"] == table]
        if not tables:
            raise DbError(f"库里找不到表 {schema}.{table}。")
        if not tables[0].get("has_geom"):
            raise DbError(f"表 {schema}.{table} 里没有几何列（不是空间表），"
                          "不能加到地图。")
        geom_column = tables[0]["geom_column"]
        srid = tables[0]["srid"]
    else:
        try:
            tables = [t for t in list_tables(c, timeout=timeout)
                      if t["schema"] == schema and t["table"] == table]
            srid = int((tables[0]["srid"] if tables else 0) or 0)
        except DbError:
            srid = 0

    db = connect(c, timeout=timeout)
    try:
        cols = [x for x in _columns(db, schema, table)
                if x not in (geom_column,)]
        if not cols:
            raise DbError(f"表 {schema}.{table} 里除了几何列没有别的字段。")
        select = ", ".join(_q(x) for x in cols)
        sql = (f"SELECT {select}, ST_AsBinary({_q(geom_column)}) "
               f"FROM {_q(schema)}.{_q(table)}")
        limit = int(max_features or 0)
        if limit > 0:
            sql += f" LIMIT {limit}"
        # 服务端游标：大表不会一次性把结果全塞进内存
        cur = db.cursor(name="gis_agent_read")
        cur.itersize = BATCH
        cur.execute(sql)
        rows: list[tuple] = []
        total = -1
        for t in tables or []:
            try:
                total = int(t.get("est_features") or -1)
            except (TypeError, ValueError):
                total = -1
        while True:
            _check_cancel(cancel)
            batch = cur.fetchmany(BATCH)
            if not batch:
                break
            rows.extend(batch)
            if on_progress is not None:
                try:
                    on_progress(len(rows), total)
                except Exception:   # noqa: BLE001  回调出错不该中断取数
                    pass
        if not rows:
            raise DbError(f"表 {schema}.{table} 里没有要素。")
        df = pd.DataFrame([r[:len(cols)] for r in rows], columns=cols)
        wkbs = [bytes(r[len(cols)]) if r[len(cols)] is not None else None
                for r in rows]
        geoms = shapely.from_wkb(wkbs)
        crs = f"EPSG:{srid}" if srid and srid > 0 else None
        gdf = gpd.GeoDataFrame(df, geometry=list(geoms), crs=crs)
        return gdf
    except DbCancelled:
        raise
    except DbError:
        raise
    except Exception as e:          # noqa: BLE001
        raise DbError(_friendly(e, c)) from None
    finally:
        try:
            db.close()
        except Exception:          # noqa: BLE001
            pass


def table_layer_name(conn: dict, schema: str, table: str) -> str:
    """图层名：public 下的表用表名，其它 schema 带上前缀避免撞名。"""
    c = normalize_conn(conn)
    if schema and schema != DEFAULT_SCHEMA:
        return f"{schema}.{table}"
    return str(table)


# ---------------------------------------------------------------- 按视野取数
#: 一次往返拉多少行。用户要的「数据量大就 1 万一批」：服务端游标 + fetchmany，
#: 不会因为表有 500 万行就把内存撑爆，也不会一次等几十秒。
BATCH_ROWS = 10_000
#: 4326 的「全球」范围：拿不到真实范围时用它兜底，保证图层照样能按视野渲染
WORLD_4326 = (-180.0, -85.05, 180.0, 85.05)
#: 服务端简化门槛（米）：窄到这个程度以下时，亚米级细节已经看不见，先简化再传
SERVER_SIMPLIFY_MIN_M = 5.0
#: ST_GeometryType 的返回值 → 图层元信息里的写法（和 OGR 的命名保持一致）
_GEOM_TYPES = {
    "Point": "Point", "LineString": "LineString", "Polygon": "Polygon",
    "MultiPoint": "MultiPoint", "MultiLineString": "MultiLineString",
    "MultiPolygon": "MultiPolygon", "GeometryCollection": "GeometryCollection",
}


def to_source_bbox(bbox4326, srid: int):
    """4326 的 bbox → 源 CRS 的 bbox（给 ST_MakeEnvelope 用）。

    `srid` 为 0（没登记坐标系）时原样返回 —— 那类表的几何本来就是「按经纬度看」
    的旧约定，硬转反而会把过滤框挪走。
    """
    x0, y0, x1, y1 = (float(v) for v in bbox4326)
    try:
        srid = int(srid or 0)
    except (TypeError, ValueError):
        srid = 0
    if srid in (0, 4326):
        return (x0, y0, x1, y1)
    try:
        from pyproj import Transformer          # noqa: PLC0415
        t = Transformer.from_crs(4326, srid, always_xy=True)
        return tuple(float(v) for v in
                     t.transform_bounds(x0, y0, x1, y1, densify_pts=21))
    except Exception:                            # noqa: BLE001  转不了就按原样过
        return (x0, y0, x1, y1)


def _bbox_where(env: tuple, srid: int, exact: bool) -> tuple[str, list]:
    """`geom && ST_MakeEnvelope(...)`（GiST 索引走这个）。

    `exact=True` 再叠加 `ST_Intersects`：过滤框是外接矩形，`&&` 会把
    「包围盒压在视野里、实际在视野外」的要素也算进来。渲染用 `&&` 就够
    （画出来会被裁掉，省一次拓扑判断），拾取/标注要精确。
    """
    if srid and int(srid) > 0:
        env_sql = "ST_MakeEnvelope(%s, %s, %s, %s, %s)"
        params = [float(env[0]), float(env[1]), float(env[2]), float(env[3]),
                  int(srid)]
    else:
        # 没有 SRID 的表：SRID 传 -1，让 PostGIS 不校验（几何的 srid 本来就是 0）
        env_sql = "ST_MakeEnvelope(%s, %s, %s, %s, -1)"
        params = [float(env[0]), float(env[1]), float(env[2]), float(env[3])]
    where = [f"{_q_geom_placeholder()} && {env_sql}"]
    if exact:
        where.append(f"ST_Intersects({_q_geom_placeholder()}, {env_sql})")
        params = params + params
    return " AND ".join(where), params


def iter_bbox(conn: dict, schema: str, table: str, geom_column: str,
              srid: int, bbox4326, batch: int = BATCH_ROWS, limit: int = 0,
              columns=None, simplify_m: float = 0.0, exact: bool = False,
              cancel=None, timeout: float = TIMEOUT, order_by: str = "ctid"):
    """按视野从服务端一批批拉几何（生成器，每批最多 `batch` 行）。

    行格式同 `read_table`：先是 `columns` 里的属性，最后一个是 WKB 字节串。
    `limit` 是本次视野的硬上限（「最多读取」），0 = 不限。

    为什么用服务端游标而不是 OFFSET 翻页：`OFFSET 100000` 要把前面十万行扫一遍，
    越翻越慢；游标是「边执行边取」，配合 `geom && envelope` 的 GiST 索引扫描，
    第 N 批和第 1 批一样快。
    """
    c = normalize_conn(conn)
    cols = [str(x) for x in (columns or [])]
    env = to_source_bbox(bbox4326, srid)
    geom = _q(geom_column)
    expr = geom
    if simplify_m and float(simplify_m) > 0:
        # 先简化再传：省域视图下几十万顶点的大面从 MB 级降到 KB 级
        expr = f"ST_SimplifyPreserveTopology({geom}, %s)"
    select = ", ".join([_q(x) for x in cols] + [f"ST_AsBinary({expr})"])
    where, params = _bbox_where(env, srid, exact)
    where = where.replace(_q_geom_placeholder(), geom)
    if simplify_m and float(simplify_m) > 0:
        params = [float(simplify_m)] + params
    sql = (f"SELECT {select} FROM {_q(schema)}.{_q(table)} WHERE {where}")
    if order_by:
        sql += f" ORDER BY {_q(order_by) if order_by != 'ctid' else 'ctid'}"
    if int(limit or 0) > 0:
        sql += f" LIMIT {int(limit)}"

    db = connect(c, timeout=timeout)
    cur = None
    try:
        cur = db.cursor(name="gis_agent_bbox")
        cur.itersize = max(1, int(batch))
        cur.execute(sql, params)
        while True:
            _check_cancel(cancel)
            rows = cur.fetchmany(max(1, int(batch)))
            if not rows:
                break
            yield rows
    except DbCancelled:
        raise
    except DbError:
        raise
    except Exception as e:                       # noqa: BLE001
        raise DbError(_friendly(e, c)) from None
    finally:
        for close in (getattr(cur, "close", None), getattr(db, "close", None)):
            try:
                if close is not None:
                    close()
            except Exception:                    # noqa: BLE001
                pass


def _q_geom_placeholder() -> str:
    """占位符，构造 SQL 时再替换成真正的几何列（引号规则只写一处）。"""
    return "{{GEOM}}"


def read_attrs(conn: dict, schema: str, table: str, columns, limit: int = 0,
               timeout: float = TIMEOUT, order_by: str = ""):
    """只读属性（不带几何）：属性表用，省掉几何传输。返回 list[tuple]。"""
    c = normalize_conn(conn)
    cols = [str(x) for x in (columns or [])]
    if not cols:
        return []
    sql = f"SELECT {', '.join(_q(x) for x in cols)} FROM {_q(schema)}.{_q(table)}"
    if order_by:
        sql += f" ORDER BY {_q(order_by) if order_by != 'ctid' else 'ctid'}"
    if int(limit or 0) > 0:
        sql += f" LIMIT {int(limit)}"
    db = connect(c, timeout=timeout)
    try:
        with db.cursor(name="gis_agent_attrs") as cur:
            cur.itersize = BATCH_ROWS
            cur.execute(sql)
            out: list[tuple] = []
            while True:
                rows = cur.fetchmany(BATCH_ROWS)
                if not rows:
                    break
                out.extend(rows)
        return out
    except Exception as e:                       # noqa: BLE001
        raise DbError(_friendly(e, c)) from None
    finally:
        try:
            db.close()
        except Exception:                        # noqa: BLE001
            pass


def _parse_box(text: str):
    """`BOX(1 2,3 4)` → (1, 2, 3, 4)；解析不了返回 None。"""
    if not text:
        return None
    try:
        body = str(text).strip()
        if body.upper().startswith("BOX"):
            body = body[body.index("(") + 1:body.rindex(")")]
        a, b = body.split(",")
        x0, y0 = (float(v) for v in a.split())
        x1, y1 = (float(v) for v in b.split())
        return (x0, y0, x1, y1)
    except Exception:                            # noqa: BLE001
        return None


def order_key(conn: dict, schema: str, table: str, timeout: float = TIMEOUT) -> str:
    """这张表的「稳定排序键」：单列主键优先，否则用 `ctid`。

    渲染/属性表/拾取都用同一个键排序，行号才对得上（`feature_geom(idx)`
    要靠 `OFFSET` 取第 idx 行）。
    """
    c = normalize_conn(conn)
    db = connect(c, timeout=timeout)
    try:
        reg = f"{_q(schema)}.{_q(table)}"
        with db.cursor() as cur:
            # 视图没有 ctid，也没有稳定行序：不排序，翻页/取单行按服务端顺序来
            cur.execute("SELECT relkind FROM pg_class WHERE oid = %s::regclass",
                        (reg,))
            row = cur.fetchone()
            kind = str(row[0]) if row else "r"
        if kind in ("v",):
            return ""
        with db.cursor() as cur:
            cur.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = %s::regclass AND i.indisprimary",
                (reg,))
            cols = [str(r[0]) for r in (cur.fetchall() or [])]
        return cols[0] if len(cols) == 1 else "ctid"
    except Exception:                            # noqa: BLE001  视图/分区表没有主键
        return "ctid"
    finally:
        try:
            db.close()
        except Exception:                        # noqa: BLE001
            pass


def probe_table(conn: dict, schema: str, table: str, geom_column: str = "",
                srid: int = 0, est_features: int = 0,
                timeout: float = TIMEOUT) -> dict:
    """加图层前的轻量探测：范围 + 排序键 + 字段，**不读整表**。

    · 范围先问 `ST_EstimatedExtent()`（走 pg_statistic，毫秒级）；
      它没有统计信息时再退到 `ST_Extent()`，并挂一个 3 秒的语句超时
      —— 一个 500 万行的表扫到底要几秒，不能卡住「加图层」这个动作。
    · 顺便 `LIMIT 1` 取一行几何：几何列写错、没权限、WKB 坏掉，
      在这里就报出来，而不是等渲染线程默默吞掉。
    """
    c = normalize_conn(conn)
    out = {"ok": False, "message": "", "bounds": None, "wgs_bounds": None,
           "count": int(est_features or 0), "srid": int(srid or 0),
           "geom_type": "", "fields": [], "order_by": "ctid"}
    db = connect(c, timeout=timeout)
    try:
        cols = [x for x in _columns(db, schema, table) if x != geom_column]
        out["fields"] = cols
        box = None
        if geom_column:
            try:
                with db.cursor() as cur:
                    cur.execute("SELECT ST_EstimatedExtent(%s, %s, %s)",
                                (str(schema), str(table), str(geom_column)))
                    row = cur.fetchone()
                box = _parse_box(row[0] if row else None)
            except Exception:                    # noqa: BLE001  没有统计信息
                db.rollback()
            if box is None:
                try:
                    with db.cursor() as cur:
                        cur.execute("SET statement_timeout = 3000")
                        cur.execute(f"SELECT ST_Extent({_q(geom_column)}) "
                                    f"FROM {_q(schema)}.{_q(table)}")
                        row = cur.fetchone()
                        cur.execute("SET statement_timeout = 0")
                    box = _parse_box(row[0] if row else None)
                except Exception:                # noqa: BLE001  超时就算了
                    try:
                        db.rollback()
                    except Exception:            # noqa: BLE001
                        pass
                    box = None
            # 取一行几何：验证几何列可读、WKB 能解出来
            with db.cursor() as cur:
                cur.execute(f"SELECT ST_AsBinary({_q(geom_column)}), "
                            f"ST_GeometryType({_q(geom_column)}) "
                            f"FROM {_q(schema)}.{_q(table)} "
                            f"WHERE {_q(geom_column)} IS NOT NULL LIMIT 1")
                row = cur.fetchone()
            if row is not None and row[0]:
                out["geom_type"] = _GEOM_TYPES.get(
                    str(row[1] or "").replace("ST_", ""),
                    str(row[1] or "").replace("ST_", ""))
            else:
                out["message"] = f"表 {schema}.{table} 里没有非空几何。"
                return out
        out["bounds"] = list(box) if box else None
        if box:
            try:
                from pyproj import Transformer      # noqa: PLC0415
                s = int(srid or 0)
                if s and s != 4326:
                    t = Transformer.from_crs(s, 4326, always_xy=True)
                    out["wgs_bounds"] = [
                        float(v) for v in t.transform_bounds(
                            *box, densify_pts=21)]
                else:
                    out["wgs_bounds"] = [float(v) for v in box]
            except Exception:                    # noqa: BLE001
                out["wgs_bounds"] = [float(v) for v in box]
        out["order_by"] = "ctid"
        out.update(ok=True, message="")
    except DbError:
        raise
    except Exception as e:                       # noqa: BLE001
        raise DbError(_friendly(e, c)) from None
    finally:
        try:
            db.close()
        except Exception:                        # noqa: BLE001
            pass
    try:
        out["order_by"] = order_key(c, schema, table, timeout=timeout)
    except Exception:                            # noqa: BLE001
        out["order_by"] = "ctid"
    return out

