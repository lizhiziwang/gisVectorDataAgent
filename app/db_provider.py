"""PostGIS 图层的数据源：**按视野**向数据库要数据。

和 `VectorProvider`（读本地文件 / GeoParquet 快照）的区别
---------------------------------------------------------
以前双击一张表，是把整张表读成一份 GeoParquet 快照：20 万行要几十秒，
缓存文件几百 MB，而且快照是死数据 —— 库里改了要重新双击一次。

现在图层只记「连接 + schema.表」，渲染时：

1. 地图把**当前视野**的 bbox（EPSG:4326）交给 provider；
2. provider 用 `geom && ST_MakeEnvelope(...)` 在**服务端**做空间过滤
   （走 GiST 索引，不是把整表拉回来再筛）；
3. 结果按 `BATCH_ROWS`（1 万行）一批批取，边取边画 —— 大表也是秒级出图，
   而且平移到哪儿就取哪儿，不会「只在加载时那一块有数据」。

属性表 / 导出 / 算子仍然需要整份数据，那时才整表读（同样有「最多读取」
上限），并且只读一次留在内存里。两条路互不拖累：看图不碰整表，整表读
只在用户明确要求时发生。

对应 QGIS 里的 `QgsPostgresProvider` + `QgsFeatureIterator`(filterRect)。
"""

from __future__ import annotations

import threading

from . import db_conn
from .db_conn import (BATCH_ROWS, DEFAULT_LIMIT, SERVER_SIMPLIFY_MIN_M,
                      WORLD_4326, DbError, _q, normalize_conn)

#: 服务端简化门槛先统一在这里：小于这个像素当量的细节不用往客户端传
SIMPLIFY_DEG_TO_M = 111_320.0


class PostGISProvider:
    """把一张 PostGIS 表包装成「按 bbox 取数」的 provider。

    `spec` 里的字段（由数据库面板探测出来，随会话保存）：

    ```
    {"conn": {...}, "schema": "sde", "table": "BHDFishnet",
     "geom_column": "shape", "srid": 4490, "geom_type": "Polygon",
     "fields": ["name", ...], "count": 690, "order_by": "objectid",
     "max_features": 200000, "wgs_bounds": [x0, y0, x1, y1]}
    ```

    `conn` 里带密码 —— 只在内存里用，写会话时只存连接名。
    """

    #: 告诉 MapView：**不要**给这一层建「整层 LOD 粗路径缓存」。
    #: 那条路会把整表按全世界 bbox 读一遍，正是这次要消灭的东西。
    live = True

    def __init__(self, spec: dict):
        self.spec = dict(spec or {})
        self.conn = normalize_conn(self.spec.get("conn") or {})
        self.schema = str(self.spec.get("schema") or db_conn.DEFAULT_SCHEMA)
        self.table = str(self.spec.get("table") or "")
        self.geom_column = str(self.spec.get("geom_column") or "")
        try:
            self.srid = int(self.spec.get("srid") or 0)
        except (TypeError, ValueError):
            self.srid = 0
        self.geom_type = str(self.spec.get("geom_type") or "Unknown")
        self.fields = [str(x) for x in (self.spec.get("fields") or [])]
        self.order_by = str(self.spec.get("order_by") or "ctid")
        self.layer_name = str(self.spec.get("layer_name")
                              or self.table)
        try:
            self.max_features = int(self.spec.get("max_features") or DEFAULT_LIMIT)
        except (TypeError, ValueError):
            self.max_features = DEFAULT_LIMIT
        self._wgs = None
        wb = self.spec.get("wgs_bounds")
        if wb and len(wb) == 4:
            try:
                self._wgs = tuple(float(v) for v in wb)
            except (TypeError, ValueError):
                self._wgs = None
        self._bounds = None
        b = self.spec.get("bounds")
        if b and len(b) == 4 and not self._wgs:
            self._bounds = tuple(float(v) for v in b)
        self._count = 0
        try:
            self._count = max(0, int(self.spec.get("count") or 0))
        except (TypeError, ValueError):
            self._count = 0
        self._lock = threading.Lock()
        self._attrs_df = None
        self._gdf = None
        self._last_hit = None            # (行号, 几何)：命中/高亮时复用
        self._transformer = self._make_transformer()

    # ------------------------------------------------------------------ 元信息
    def __repr__(self) -> str:                                 # pragma: no cover
        return f"<PostGISProvider {self.schema}.{self.table}>"

    @property
    def path(self) -> str:
        """给「数据源」这类通用显示用的字符串（不是文件路径）。"""
        return f"postgis://{self.conn.get('host')}/{self.schema}.{self.table}"

    @property
    def bounds(self):
        """源 CRS 的范围（加图层时探测到的那份；没探测到就 None）。"""
        return self._bounds

    @property
    def wgs_bounds(self):
        """WGS84 范围。

        只回缓存值，**绝不在这里查库**：MapView 会在 UI 线程上问这个属性，
        一次 `ST_Extent()` 就够卡一屏。真正的探测在加图层时（后台线程）做，
        探测不到就退回「全球」——渲染靠视野过滤，没有范围也能画。
        """
        return self._wgs

    @property
    def wgs_bounds_hint(self):
        return self._wgs

    @property
    def count(self) -> int:
        return int(self._count or 0)

    @property
    def crs(self):
        if self.srid <= 0:
            return None
        try:
            from pyproj import CRS          # noqa: PLC0415
            return CRS.from_epsg(self.srid)
        except Exception:                    # noqa: BLE001
            return None

    def meta(self) -> dict:
        crs = self.crs
        return {
            # reltuples = -1 表示这张表从没 analyze 过，别把 -1 画到界面上
            "count": max(0, int(self.count or 0)),
            "geomType": self.geom_type or "Unknown",
            "crs": f"EPSG:{self.srid}" if self.srid > 0 else "未定义",
            "crsName": (crs.name if crs is not None else
                        (f"EPSG:{self.srid}" if self.srid > 0 else "未定义")),
            "bounds": list(self._bounds or ()),
            "fields": list(self.fields),
        }

    # ------------------------------------------------------------------ 取数
    def iter_bbox(self, bbox4326, batch_size: int = BATCH_ROWS, columns=None,
                  simplify_tol: float = 0.0):
        """按视野从库里拉几何（每批最多 1 万行），产出 WGS84 的 shapely 数组。

        这是渲染线程唯一会调的方法：`columns=[]` 时只取几何列，属性一个字节
        都不传；`simplify_tol`（度）在服务端先简化，再转 WGS84。
        """
        cols = [str(c) for c in (columns or []) if str(c) in self.fields]
        # 网络往返按 1 万行一批（BATCH_ROWS），画的时候再按调用方要的粒度切
        # ——「一批一批地拉」和「一批一批地画」不是一个尺寸，别混着用。
        want = max(1, int(batch_size or 4096))
        rows = db_conn.iter_bbox(
            self.conn, self.schema, self.table, self.geom_column, self.srid,
            bbox4326, batch=BATCH_ROWS,
            limit=self.max_features, columns=cols,
            simplify_m=self._simplify_m(simplify_tol), order_by=self.order_by)
        try:
            gi = len(cols)
            buf: list = []

            def _decode(chunk):
                geoms, _keep = _wkb_geoms([r[gi] for r in chunk])
                if geoms is None:
                    return None
                return self._to_wgs(geoms)

            for batch in rows:
                buf.extend(batch)
                while len(buf) >= want:
                    chunk, buf = buf[:want], buf[want:]
                    geoms = _decode(chunk)
                    if geoms is not None and len(geoms):
                        yield geoms
            if buf:
                geoms = _decode(buf)
                if geoms is not None and len(geoms):
                    yield geoms
        finally:
            close = getattr(rows, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:            # noqa: BLE001
                    pass

    def iter_bbox_attrs(self, bbox4326, columns, batch_size: int = 2048):
        """按视野产出 `(几何数组, 属性 dict)`：拾取/标注之外的按需路径。"""
        import numpy as np                   # noqa: PLC0415

        cols = [str(c) for c in (columns or []) if str(c) in self.fields]
        rows = db_conn.iter_bbox(
            self.conn, self.schema, self.table, self.geom_column, self.srid,
            bbox4326, batch=BATCH_ROWS,
            limit=self.max_features, columns=cols, order_by=self.order_by)
        try:
            gi = len(cols)
            for batch in rows:
                geoms, keep = _wkb_geoms([r[gi] for r in batch])
                if geoms is None or len(geoms) == 0:
                    continue
                geoms = self._to_wgs(geoms)
                if geoms is None or len(geoms) == 0:
                    continue
                attrs = {c: np.asarray([batch[k][i] for k in keep], dtype=object)
                         for i, c in enumerate(cols)}
                yield geoms, attrs
        finally:
            close = getattr(rows, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:            # noqa: BLE001
                    pass

    def iter_label_anchors(self, columns, bbox4326=None, batch_size: int = 2048):
        """标注锚点：`(lons, lats, angles, attrs)`，WGS84。

        画布上的文字只能在 WGS84 里定位，所以这里和 `VectorProvider` 一样，
        一条要素只算一个点（包围盒中心；线用中点切线角），不把顶点全转一遍。
        """
        import numpy as np                   # noqa: PLC0415
        import shapely                       # noqa: PLC0415

        cols = [str(c) for c in (columns or []) if str(c)]
        if not cols:
            return
        bbox = tuple(bbox4326) if bbox4326 is not None else self._whole_bbox()
        is_line = "Line" in str(self.geom_type or "")
        for geoms, attrs in self.iter_bbox_attrs(bbox, cols, batch_size):
            if geoms is None or len(geoms) == 0:
                continue
            try:
                b = np.asarray(shapely.bounds(geoms), dtype=float)
            except Exception:                # noqa: BLE001
                continue
            ok = np.isfinite(b).all(axis=1)
            if not ok.all():
                b = b[ok]
                geoms = np.asarray(geoms, dtype=object)[ok]
                attrs = {k: np.asarray(v, dtype=object)[ok]
                         for k, v in attrs.items()}
            if len(b) == 0:
                continue
            xs = (b[:, 0] + b[:, 2]) * 0.5
            ys = (b[:, 1] + b[:, 3]) * 0.5
            ang = np.zeros(len(b), dtype=float)
            if is_line:
                for j in range(len(geoms)):
                    try:
                        pt, a = self._line_mid_angle(geoms[j])
                    except Exception:        # noqa: BLE001
                        continue
                    if pt is not None:
                        xs[j], ys[j], ang[j] = pt[0], pt[1], a
            yield (np.asarray(xs, dtype=float), np.asarray(ys, dtype=float),
                   ang, attrs)

    # ------------------------------------------------------------------ 整表
    def read_all(self):
        """整表读回内存（属性表/导出/算子用），有上限、只读一次。"""
        with self._lock:
            if self._gdf is not None:
                return self._gdf
            gdf = db_conn.read_table(
                self.conn, self.schema, self.table,
                geom_column=self.geom_column, max_features=self.max_features)
            self._gdf = gdf
            return gdf

    def read_bbox(self, bbox4326):
        """只把视野内的要素读成 GeoDataFrame（导出「当前视图」用）。"""
        import geopandas as gpd             # noqa: PLC0415
        import numpy as np                  # noqa: PLC0415
        import shapely                      # noqa: PLC0415

        geoms = []
        attrs: dict = {}
        for arr, a in self.iter_bbox_attrs(bbox4326, self.fields):
            geoms.append(np.asarray(arr, dtype=object))
            for k, v in a.items():
                attrs.setdefault(k, []).extend(list(v))
        if not geoms:
            crs = f"EPSG:{self.srid}" if self.srid > 0 else None
            return gpd.GeoDataFrame({c: [] for c in self.fields},
                                    geometry=[], crs=crs)
        crs = f"EPSG:{self.srid}" if self.srid > 0 else None
        return gpd.GeoDataFrame(attrs, geometry=list(np.concatenate(geoms)),
                                crs=crs)

    def attrs_frame(self):
        """属性表：只读非几何列（不传几何，比整表读快一个量级）。"""
        import pandas as pd                 # noqa: PLC0415

        with self._lock:
            if self._attrs_df is not None:
                return self._attrs_df
            if self._gdf is not None:
                gdf = self._gdf
                df = gdf.drop(columns=[gdf.geometry.name]).reset_index(drop=True)
            else:
                cols = list(self.fields)
                if not cols:
                    df = pd.DataFrame()
                else:
                    rows = db_conn.read_attrs(
                        self.conn, self.schema, self.table, cols,
                        limit=self.max_features, order_by=self.order_by)
                    df = pd.DataFrame(rows, columns=cols)
            self._attrs_df = df
            return df

    def attrs_at(self, row: int) -> dict:
        try:
            df = self.attrs_frame()
        except Exception:                    # noqa: BLE001
            return {}
        try:
            if 0 <= int(row) < len(df):
                return {k: _plain(v) for k, v in df.iloc[int(row)].to_dict().items()}
        except Exception:                    # noqa: BLE001
            pass
        return {}

    def feature_geom(self, row: int):
        """按行号取一条 WGS84 几何（属性表点行的高亮/定位用）。"""
        try:
            row = int(row)
        except (TypeError, ValueError):
            return None
        if row < 0:
            return None
        if self._last_hit is not None and self._last_hit[0] == row:
            return self._last_hit[1]
        import shapely                       # noqa: PLC0415

        sql = (f"SELECT ST_AsBinary({_q(self.geom_column)}) "
               f"FROM {_q(self.schema)}.{_q(self.table)}")
        if self.order_by:
            sql += f" ORDER BY {_q(self.order_by) if self.order_by != 'ctid' else 'ctid'}"
        sql += f" OFFSET {int(row)} LIMIT 1"
        db = None
        try:
            db = db_conn.connect(self.conn)
            with db.cursor() as cur:
                cur.execute(sql)
                r = cur.fetchone()
            if not r or not r[0]:
                return None
            geom = shapely.from_wkb(bytes(r[0]))
            geom = self._to_wgs_one(geom)
            self._last_hit = (row, geom)
            return geom
        except Exception:                    # noqa: BLE001
            return None
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:            # noqa: BLE001
                    pass

    def hit_test(self, lon: float, lat: float, tol: float):
        """点击拾取：只在点位附近的小框里查一次，返回 `(行号, 属性)`。

        行号是「属性表里的第几行」——属性表已经读进内存时按主键/ctid 精确算，
        没读时给 0（弹窗只用到属性，几何高亮用缓存的那一条）。
        """
        if tol <= 0:
            return None
        try:
            import shapely                   # noqa: PLC0415
            from shapely.geometry import Point   # noqa: PLC0415
        except Exception:                    # noqa: BLE001
            return None
        bbox = (float(lon) - tol, float(lat) - tol,
                float(lon) + tol, float(lat) + tol)
        cols = list(self.fields)
        try:
            rows = []
            for batch in db_conn.iter_bbox(
                    self.conn, self.schema, self.table, self.geom_column,
                    self.srid, bbox, batch=200, limit=200, columns=cols,
                    exact=True, order_by=self.order_by):
                rows.extend(batch)
                break
        except Exception:                    # noqa: BLE001
            return None
        if not rows:
            return None
        pt = Point(float(lon), float(lat))
        best = None
        for r in rows:
            try:
                geom = self._to_wgs_one(shapely.from_wkb(bytes(r[len(cols)])))
                if geom is None or geom.is_empty:
                    continue
                d = float(shapely.distance(geom, pt))
            except Exception:                # noqa: BLE001
                continue
            if best is None or d < best[0]:
                best = (d, r, geom)
        if best is None or best[0] > tol:
            return None
        _, r, geom = best
        props = {c: _plain(r[i]) for i, c in enumerate(cols)}
        idx = self._row_index(r, cols)
        self._last_hit = (idx, geom)
        return idx, props

    # ------------------------------------------------------------------ 内部
    def _row_index(self, row, cols) -> int:
        """命中行在属性表里的下标；算不出来给 0（不影响弹窗/高亮）。"""
        key = "ctid" if self.order_by == "ctid" else self.order_by
        if key not in cols or self._attrs_df is None:
            return 0
        try:
            val = row[cols.index(key)]
            hit = self._attrs_df.index[self._attrs_df[key] == val]
            return int(hit[0]) if len(hit) else 0
        except Exception:                    # noqa: BLE001
            return 0

    def _whole_bbox(self):
        """整层 bbox（标注整层锚点表时用）：有范围就用，没有就用「全球」。"""
        return tuple(self._wgs or WORLD_4326)

    def _make_transformer(self):
        if self.srid <= 0 or self.srid == 4326:
            return None
        try:
            from pyproj import Transformer   # noqa: PLC0415
            return Transformer.from_crs(self.srid, 4326, always_xy=True)
        except Exception:                    # noqa: BLE001
            return None

    def _to_wgs(self, geoms):
        if self._transformer is None or geoms is None or len(geoms) == 0:
            return geoms
        try:
            import shapely                   # noqa: PLC0415

            def fn(c):
                x, y = self._transformer.transform(c[:, 0], c[:, 1])
                out = c.copy()
                out[:, 0] = x
                out[:, 1] = y
                return out

            return shapely.transform(geoms, fn)
        except Exception:                    # noqa: BLE001
            return geoms

    def _to_wgs_one(self, geom):
        if self._transformer is None or geom is None:
            return geom
        try:
            import numpy as np               # noqa: PLC0415
            arr = self._to_wgs(np.asarray([geom], dtype=object))
            return arr[0] if arr is not None and len(arr) else geom
        except Exception:                    # noqa: BLE001
            return geom

    def _simplify_m(self, tol_wgs: float) -> float:
        """屏幕容差（度）→ **源 CRS 的单位**（投影是米，4326 还是度）。

        小于 5 米当量的容差不值得让服务端算（还容易把要素简化没了），
        那种情况返回 0，交给客户端的简化流程。
        """
        if not tol_wgs:
            return 0.0
        try:
            tol = float(tol_wgs)
        except (TypeError, ValueError):
            return 0.0
        if tol <= 0:
            return 0.0
        if self.srid in (0, 4326):
            return tol if tol >= SERVER_SIMPLIFY_MIN_M / SIMPLIFY_DEG_TO_M else 0.0
        tol_m = tol * SIMPLIFY_DEG_TO_M
        return tol_m if tol_m >= SERVER_SIMPLIFY_MIN_M else 0.0

    @staticmethod
    def _line_mid_angle(geom):
        """线和 `VectorProvider` 一样取中点切线角（文字顺着线走）。"""
        try:
            import numpy as np               # noqa: PLC0415
            if geom is None or geom.is_empty:
                return None, 0.0
            if geom.geom_type != "LineString":
                mid = geom.interpolate(0.5, normalized=True)
                return (mid.x, mid.y), 0.0
            cs = np.asarray(geom.coords, dtype=float)
            if len(cs) < 2:
                return None, 0.0
            mid = geom.interpolate(0.5, normalized=True)
            dist = np.hypot(cs[:, 0] - mid.x, cs[:, 1] - mid.y)
            k = int(np.argmin(dist))
            a = int(max(0, k - 1))
            b = int(min(len(cs) - 1, k + 1))
            ang = np.degrees(np.arctan2(cs[b, 1] - cs[a, 1],
                                        cs[b, 0] - cs[a, 0]))
            if ang > 90.0:
                ang -= 180.0
            elif ang < -90.0:
                ang += 180.0
            return (float(mid.x), float(mid.y)), float(ang)
        except Exception:                    # noqa: BLE001
            return None, 0.0


def _wkb_geoms(values):
    """WKB 列表 → `(shapely 数组, 保留下来的下标)`。

    两个坑都在这一个函数里堵住：

    · psycopg2 的 `ST_AsBinary()` 回来的是 **memoryview**，`shapely.from_wkb`
      只认 bytes —— 直接喂会抛 TypeError，而渲染线程会把这个异常吞掉，
      用户看到的就是「图层加进来了、屏幕上一片空白」；
    · 表里允许 NULL 几何，解出来是 `None`，画路径时会炸在更深的地方，
      这里按行剔除，并把下标回传，属性列才好跟着对齐。
    """
    try:
        import numpy as np               # noqa: PLC0415
        import shapely                   # noqa: PLC0415
    except Exception:                    # noqa: BLE001
        return None, []
    raw = [bytes(v) if isinstance(v, memoryview) else v for v in values]
    keep: list[int] = []
    buf: list = []
    for i, v in enumerate(raw):
        if not v:
            continue
        buf.append(v)
        keep.append(i)
    if not buf:
        return None, []
    try:
        geoms = np.asarray(shapely.from_wkb(buf), dtype=object)
    except Exception:                    # noqa: BLE001  坏 WKB 不该毁掉整层
        return None, keep
    if len(geoms) == 0:
        return None, []
    ok = np.asarray([g is not None for g in geoms], dtype=bool)
    if not ok.all():
        geoms = geoms[ok]
        keep = [k for k, good in zip(keep, ok) if good]
    if len(geoms) == 0:
        return None, []
    return geoms, keep


def _plain(v):
    """numpy 标量 → Python 原生类型：属性弹窗/JSON 里不能有 numpy 对象。"""
    try:
        if v is None or isinstance(v, (str, bool, int, float)):
            return v
        import numpy as np                   # noqa: PLC0415
        if isinstance(v, np.generic):
            return v.item()
        if isinstance(v, np.ndarray):
            return v.tolist()
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return v if isinstance(v, (list, dict)) else str(v)
    except Exception:                        # noqa: BLE001
        return str(v)


def provider_for(spec: dict) -> PostGISProvider:
    """工厂：打不开就抛 `DbError`，消息可以直接显示给用户。"""
    if not spec or not spec.get("table"):
        raise DbError("这个图层缺少「schema.表」信息，无法连接数据库。")
    if db_conn.driver() is None:
        raise DbError(db_conn.driver_hint())
    return PostGISProvider(spec)
