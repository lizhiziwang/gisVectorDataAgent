"""图层要素标注：取当前视野内要素的锚点坐标和字段文本。

职责边界：
· 这里只负责「拿数据」—— 锚点（世界坐标）+ 文本，不碰 QPainter；
· 真正画字在 `native/mapview.py::_paint_labels`，因为那里才有当前视口的
  平移/缩放，能把世界坐标换成屏幕坐标。

为什么按视口取，而不是一律整层预计算：
· 标注只在屏幕上看得见时才有意义，视野外的几百公里不用管；
· 有空间索引的源文件（GPKG/FileGDB/FlatGeobuf/带 .qix 的 Shapefile）按 bbox 读
  本来就是毫秒级，`iter_bbox_attrs` 按块读、取够就停，内存和耗时只跟「视野里取多少」有关；
· 没有索引的源文件（GeoParquet、无 .qix 的 Shapefile）按 bbox 也要顺扫整个文件，
  那就反过来整层读一次建「锚点表」（`build_label_table`），之后视口切换只在内存里筛。

和 QGIS 标注引擎的对应关系：
· QGIS 是 `QgsLabelFeature`（候选位置）→ `QgsLabelSolver` 冲突消解 → 能放多少放多少；
  它不设「每层最多 N 个」，靠 `QgsLabelThinningSettings` 和屏幕碰撞自然收敛。
· 这里没有 C++ 求解器，用两步近似：先按 **16×16 网格在视野里均匀取样**（等价于
  QGIS 的候选分布），再把候选全交给 `mapview` 的碰撞避让去筛。取样上限只是
  安全阀（防止整层 46 万要素全塞给绘制），不是「只标 1000 个」。
· 线要素对齐 QGIS 默认的「沿线上放置」：锚点取**线中点**（不是包围盒中心），
  并附带该处切线方向，绘制时把字旋转到与道路平行；非线要素角度为 0。
"""
from __future__ import annotations

import math

import numpy as np

#: 候选锚点上限（安全阀）。真正决定画多少个的是屏幕碰撞，不是这个数。
LABEL_CAP = 1000

#: 收集阶段先多收几倍，再按网格均匀挑；只取前 N 个会只标到文件顺序靠前的一角。
COLLECT_FACTOR = 4

#: 均匀取样用的网格边长（16×16 = 256 格）。
_GRID = 16

#: 单个标注文本的最大长度（字符）。太长的字段值会横跨半个屏幕。
MAX_CHARS = 40

#: 「整层锚点表」的要素数上限：源文件图层一次读完整层、把锚点留在内存，
#: 之后任何视口都只做 numpy 筛选（`build_label_table`）。
#: 21 万个面要素约 25MB；再大就仍按视口取，免得为标注把几十万条线的
#: 逐要素求中点也算一遍。
LABEL_TABLE_MAX = 200_000


def clean_text(value) -> str:
    """字段值 → 可显示的短文本；空值/NaN/NA 一律返回空串。"""
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:  # noqa: BLE001
        pass
    try:
        s = str(value).strip()
    except Exception:  # noqa: BLE001
        return ""
    if not s or s.lower() in ("nan", "none", "nat", "<na>", "null"):
        return ""
    if len(s) > MAX_CHARS:
        s = s[:MAX_CHARS - 1] + "…"
    return s


def project_xy(lon, lat):
    """经纬度数组 → 归一化 Web Mercator（和 `native.proj.project` 同一公式）。"""
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    x = (lon + 180.0) / 360.0
    s = np.sin(np.clip(lat, -85.05, 85.05) * math.pi / 180.0)
    y = 0.5 - np.log((1.0 + s) / (1.0 - s)) / (4.0 * math.pi)
    return x, y


def _rect_mask(b: np.ndarray, rect: tuple) -> np.ndarray:
    x0, y0, x1, y1 = (float(v) for v in rect)
    ok = np.isfinite(b).all(axis=1)
    m = ((b[:, 2] >= x0) & (b[:, 0] <= x1)
         & (b[:, 3] >= y0) & (b[:, 1] <= y1))
    return m & ok


def _centers(b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return (b[:, 0] + b[:, 2]) * 0.5, (b[:, 1] + b[:, 3]) * 0.5


def _grid_pick(wx: np.ndarray, wy: np.ndarray, rect: tuple, cap: int) -> np.ndarray:
    """按网格均匀挑 `cap` 个锚点：每格轮流取，避免样本聚成一坨。

    QGIS 的候选分布是「每个要素一组候选位置」，不抽样；这里为了把 46 万要素
    压到可绘制的量级，退而求其次做**空间分层**——先让每个格子都有代表，
    再把剩余名额分给超额的格子。只按文件顺序取前 N 个会只标到地图一角。
    """
    n = len(wx)
    if n <= cap:
        return np.arange(n)
    x0, y0 = project_xy(rect[0], rect[3])   # 西北角：world 坐标最小
    x1, y1 = project_xy(rect[2], rect[1])   # 东南角：world 坐标最大
    w = max(1e-12, float(x1) - float(x0))
    h = max(1e-12, float(y1) - float(y0))
    gx = np.clip(((wx - x0) / w * _GRID).astype(np.int32), 0, _GRID - 1)
    gy = np.clip(((wy - y0) / h * _GRID).astype(np.int32), 0, _GRID - 1)
    key = gy * _GRID + gx
    order = np.argsort(key, kind="stable")
    sorted_key = key[order]
    # 每个点在它所在格子里的序号（0,1,2...）
    starts = np.searchsorted(sorted_key, sorted_key, side="left")
    rank = np.arange(len(order), dtype=np.int64) - starts
    per_cell = max(1, int(math.ceil(cap / float(_GRID * _GRID))))
    picked = order[rank < per_cell]
    if len(picked) > cap:
        picked = picked[np.linspace(0, len(picked) - 1, cap).astype(int)]
    return picked


def _line_anchor_angle(geom):
    """线要素 → `((lon, lat), 角度)`；非线/退化几何返回 `(None, 0.0)`。

    锚点取线中点（不是包围盒中心）：斜着走的道路，包围盒中心可能落在路外，
    字就飘在空地上。角度取中点附近 10% 线段的方向，等价于 QGIS 「沿线上放置」
    的局部切线；再归一化到 (-90, 90]，保证字不会倒着写。

    角度在经纬度上算和投影后等价：Web Mercator 是保角投影，局部 x/y 比例因子相同。
    """
    if geom is None or getattr(geom, "is_empty", True):
        return None, 0.0
    gt = getattr(geom, "geom_type", "")
    if gt == "LineString":
        line = geom
    elif gt == "MultiLineString":
        # 多段线取最长的一股：跨股取中点经常落到两段之间的空处
        try:
            line = max(geom.geoms, key=lambda g: g.length)
        except Exception:  # noqa: BLE001
            return None, 0.0
    else:
        return None, 0.0
    try:
        if line.length <= 0:
            return None, 0.0
        p_mid = line.interpolate(0.5, normalized=True)
        p1 = line.interpolate(0.45, normalized=True)
        p2 = line.interpolate(0.55, normalized=True)
        dx, dy = p2.x - p1.x, p2.y - p1.y
        if abs(dx) < 1e-12 and abs(dy) < 1e-12:
            coords = list(line.coords)
            dx = coords[-1][0] - coords[0][0]
            dy = coords[-1][1] - coords[0][1]
        if abs(dx) < 1e-12 and abs(dy) < 1e-12:
            return (p_mid.x, p_mid.y), 0.0
        ang = math.degrees(math.atan2(dy, dx))
        if ang > 90.0:
            ang -= 180.0
        elif ang <= -90.0:
            ang += 180.0
        return (p_mid.x, p_mid.y), ang
    except Exception:  # noqa: BLE001
        return None, 0.0


def _finish(wx, wy, texts: list[str], rect: tuple, cap: int, geoms=None):
    """丢掉空文本、按网格裁到上限，再给线要素补上沿线锚点和角度。

    返回 `(xy, texts, angles)`；`angles` 是度，0 表示水平。
    """
    keep = np.asarray([i for i, t in enumerate(texts) if t], dtype=np.int64)
    if len(keep) == 0:
        return None, [], np.zeros(0)
    wx = np.asarray(wx, dtype=float)[keep]
    wy = np.asarray(wy, dtype=float)[keep]
    if len(wx) > cap:
        pick = _grid_pick(wx, wy, rect, cap)
        wx, wy = wx[pick], wy[pick]
        keep = keep[pick]
    angles = np.zeros(len(keep), dtype=float)
    if geoms is not None and len(keep):
        garr = geoms
        for j, src in enumerate(keep.tolist()):
            try:
                geom = garr[src]
            except Exception:  # noqa: BLE001
                continue
            pt, ang = _line_anchor_angle(geom)
            if pt is None:
                continue
            gx, gy = project_xy(pt[0], pt[1])
            wx[j] = float(gx)
            wy[j] = float(gy)
            angles[j] = ang
    xy = np.column_stack([wx, wy])
    return xy, [texts[int(i)] for i in keep], angles


def build_label_data(layer, field: str, rect: tuple, cap: int = LABEL_CAP,
                     cancelled=None):
    """返回 `(world_xy, texts, angles)`：`n×2` 世界坐标 + 文本 + 旋转角（度）。

    `rect` 是 WGS84 视口 `(x0, y0, x1, y1)`；取不到数据时返回
    `(None, [], 空数组)`，调用方按「这层没有标注」处理，不要画空框。
    线要素的 `world_xy` 是「沿线的中点」而不是包围盒中心。
    `cancelled` 是个无参回调：换了世代就尽快退出，别让旧视口的整层扫描
    把新视口的任务堵在后面（614MB 的村界面图层一次要 4.5s，拖动时能积到 10s）。
    """
    field = str(field or "")
    if not field or layer is None or getattr(layer, "is_raster", False):
        return None, [], np.zeros(0)
    rect = tuple(rect)
    if rect[0] >= rect[2] or rect[1] >= rect[3]:
        return None, [], np.zeros(0)

    provider = None
    if getattr(layer, "_gdf", None) is None:
        try:
            provider = layer.provider
        except Exception:  # noqa: BLE001
            provider = None
    if provider is not None:
        return _from_provider(provider, field, rect, cap, cancelled=cancelled)
    return _from_frame(layer, field, rect, cap)


def _from_frame(layer, field: str, rect: tuple, cap: int):
    try:
        import shapely

        gdf = layer.gdf
    except Exception:  # noqa: BLE001
        return None, [], np.zeros(0)
    if gdf is None or len(gdf) == 0 or field not in gdf.columns:
        return None, [], np.zeros(0)
    try:
        geoms = gdf.geometry.values
        b = np.asarray(shapely.bounds(geoms), dtype=float)
    except Exception:  # noqa: BLE001
        return None, [], np.zeros(0)
    if len(b) == 0:
        return None, [], np.zeros(0)
    idx = np.nonzero(_rect_mask(b, rect))[0]
    if len(idx) == 0:
        return None, [], np.zeros(0)
    clon, clat = _centers(b[idx])
    wx, wy = project_xy(clon, clat)
    # 先分层取样到「收集上限」，再只对这些要素做字段转字符串：
    # 视野里可能有几十万要素，不能为一个标注扫全表。
    collect_cap = max(cap, int(cap * COLLECT_FACTOR))
    pick = _grid_pick(wx, wy, rect, collect_cap)
    values = gdf[field].to_numpy()
    picked_geoms = geoms[idx[pick]]
    texts = [clean_text(values[int(i)]) for i in idx[pick]]
    return _finish(wx[pick], wy[pick], texts, rect, cap, geoms=picked_geoms)


def _assemble(wx, wy, texts: list[str], angles, rect: tuple, cap: int):
    """空文本剔除 + 网格均匀抽样到 `cap`；角度跟着一起选。"""
    keep = np.asarray([i for i, t in enumerate(texts) if t], dtype=np.int64)
    if len(keep) == 0:
        return None, [], np.zeros(0)
    wx = np.asarray(wx, dtype=float)[keep]
    wy = np.asarray(wy, dtype=float)[keep]
    ang = (np.asarray(angles, dtype=float)[keep] if angles is not None
           and len(angles) else np.zeros(len(keep)))
    sel_texts = [texts[int(i)] for i in keep]
    if len(wx) > cap:
        pick = _grid_pick(wx, wy, rect, cap)
        wx, wy, ang = wx[pick], wy[pick], ang[pick]
        sel_texts = [sel_texts[int(i)] for i in pick]
    return np.column_stack([wx, wy]), sel_texts, ang


def _from_provider(provider, field: str, rect: tuple, cap: int,
                   cancelled=None):
    """流式图层：按 bbox 分块读 anchors（锚点已由 provider 转成 WGS84）。"""
    try:
        batches = provider.iter_label_anchors([field], rect, batch_size=2048)
    except Exception:  # noqa: BLE001
        return None, [], np.zeros(0)
    collect_cap = max(cap, int(cap * COLLECT_FACTOR))
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    angs: list[np.ndarray] = []
    texts: list[str] = []
    got = 0
    x0, y0, x1, y1 = (float(v) for v in rect)
    try:
        for lons, lats, ang, attrs in batches:
            if cancelled is not None and cancelled():
                return None, [], np.zeros(0)
            vals = attrs.get(field) if isinstance(attrs, dict) else None
            if vals is None:
                # 字段没读到（列名对不上/驱动不支持）：这层干脆不标
                return None, [], np.zeros(0)
            keep = ((lons >= x0) & (lons <= x1) & (lats >= y0) & (lats <= y1))
            if not keep.any():
                continue
            wx, wy = project_xy(lons[keep], lats[keep])
            xs.append(np.asarray(wx))
            ys.append(np.asarray(wy))
            angs.append(np.asarray(ang, dtype=float)[keep])
            texts.extend([clean_text(v) for v in np.asarray(vals)[keep]])
            got += int(keep.sum())
            if got >= collect_cap:
                break
    finally:
        close = getattr(batches, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    if not xs:
        return None, [], np.zeros(0)
    return _assemble(np.concatenate(xs), np.concatenate(ys), texts,
                     np.concatenate(angs), rect, cap)


def build_label_table(layer, field: str, cancelled=None,
                      max_features: int = LABEL_TABLE_MAX):
    """整层锚点表：一次读完整个源文件，之后任何视口都只在内存里筛。

    为什么需要这条路径
    ------------------
    按视口取（`build_label_data`）在 **614MB 的村界面 parquet** 上要 4.5s/次：
    parquet 行组没有可用的空间统计，为了取视野里八百个名字，仍要把整列几何
    解出来再按 bbox 过滤。拖动一次换一个视野就重来一次；旧任务又不能中断，
    新任务排在它后面，用户看到的就是「标注延后 10 秒」。
    整层只读一次（同样 4.5s，但在后台跑一次），把 `(锚点, 文本, 角度)` 留在
    内存里（2 万条约 2MB），之后平移/缩放只是 numpy 掩码，不再碰磁盘。

    返回 `(xy, texts, angles, rect)`；取消/要素数超过 `max_features`/读不到
    字段时返回 `(None, [], 空数组, None)`。
    """
    field = str(field or "")
    if not field or layer is None or getattr(layer, "is_raster", False):
        return None, [], np.zeros(0), None
    if getattr(layer, "_gdf", None) is not None:
        return None, [], np.zeros(0), None      # 内存图层走 _from_frame，不需要表
    try:
        provider = layer.provider
        batches = provider.iter_label_anchors([field], None, batch_size=2048)
    except Exception:  # noqa: BLE001
        return None, [], np.zeros(0), None

    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    angles: list[np.ndarray] = []
    texts: list[str] = []
    got = 0
    try:
        for lons, lats, ang, attrs in batches:
            if cancelled is not None and cancelled():
                return None, [], np.zeros(0), None
            vals = attrs.get(field) if isinstance(attrs, dict) else None
            if vals is None:
                return None, [], np.zeros(0), None
            if len(lons) == 0:
                continue
            got += len(lons)
            if got > max_features:
                return None, [], np.zeros(0), None
            wx, wy = project_xy(lons, lats)
            xs.append(np.asarray(wx, dtype=float))
            ys.append(np.asarray(wy, dtype=float))
            angles.append(np.asarray(ang, dtype=float))
            texts.extend([clean_text(v) for v in np.asarray(vals)])
    finally:
        close = getattr(batches, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    if not xs:
        return None, [], np.zeros(0), None
    wx = np.concatenate(xs)
    wy = np.concatenate(ys)
    ang = np.concatenate(angles) if angles else np.zeros(0)
    keep = np.asarray([i for i, t in enumerate(texts) if t], dtype=np.int64)
    if len(keep) == 0:
        return None, [], np.zeros(0), None
    wx, wy, ang = wx[keep], wy[keep], ang[keep]
    texts = [texts[int(i)] for i in keep]
    return np.column_stack([wx, wy]), texts, ang, None
