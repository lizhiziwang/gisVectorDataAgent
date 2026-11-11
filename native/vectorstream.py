"""屏幕范围内的矢量流式渲染（对照 QGIS 的矢量图层渲染路径）。

和 `mapview` 里那套「渲染集 + QPainterPath 缓存」不是一回事：

- 渲染集先把**整个视口范围**的要素转成路径，缓存起来，换视图时尽量复用；
- 这里不缓存路径，而是像 `QgsVectorLayerRenderer::render()` 一样，
  按 `QgsFeatureRequest::setFilterRect(extent)` 选出屏幕范围内的要素，
  分块取几何、简化、建路径、立即画进图层图。每块画完就丢，
  内存里不保留整层路径；块边界通过 `on_progress` 通知调用方发中间帧，
  所以屏幕上是渐进出现的，而不是整层完成后才换图。

源文件图层总是走这条路径（provider bbox）；内存图层超过
`mapview.STREAM_MIN_FEATURES` 才走，小图层用渲染集缓存更快。

几何构建仍然用 shapely 的向量化接口，不是逐要素 Python 循环。
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPolygonF

#: 每块取多少个要素。太大会让单次 chunk 卡住工作线程，太小会放大固定开销。
STREAM_CHUNK_FEATURES = 4096
#: 点图层的批次可以更大：没有路径构建，只有坐标投影 + 像素桶去重。
POINT_BATCH_FEATURES = 65536


def paint_stream_layer(p, snap: dict, view: tuple, scale: float, mpp: float,
                       cancelled=None, on_progress=None) -> None:
    """把一个图层的可见要素分块画进当前 painter。

    `snap` 由 `MapView._make_snapshot()` 生成，有两种来源：
    · 源文件图层：`provider` + `bbox`，渲染线程自己按 bbox 读窗口；
    · 内存大图层：`geoms` + `indices`，只取屏幕范围内已加载的几何。
    两种都带 `tol`（简化容差）和 `color` / `op`（样式）。
    """
    if snap.get("point_dots"):
        # 500 万点逐点 drawEllipse 是不可接受的。这里走「屏幕像素桶」：
        # 每个设备像素最多画一个点，视觉上等价于满密度点图，但把 500 万次
        # QPainter 调用压到「实际被占用的像素数」量级。它不是抽样：
        # 桶里有点就画，不会漏掉整片区域。
        provider = snap.get("provider")
        if provider is not None:
            try:
                batches = provider.iter_bbox(snap["bbox"],
                                             batch_size=POINT_BATCH_FEATURES,
                                             columns=[])
            except Exception:
                return
            try:
                _paint_point_batches(p, batches, snap, view, scale, cancelled, on_progress)
            except Exception:
                return
            finally:
                close = getattr(batches, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
            return
        geoms = snap.get("geoms")
        idx = snap.get("indices")
        if geoms is not None and idx is not None and len(idx):
            _paint_point_batches(
                p, (geoms[idx[i:i + POINT_BATCH_FEATURES]]
                    for i in range(0, len(idx), POINT_BATCH_FEATURES)),
                snap, view, scale, cancelled, on_progress)
        return

    tol = float(snap.get("tol") or 0.0)
    provider = snap.get("provider")
    if provider is not None:
        # 关键：不物化整个 bbox 窗口。provider 用 OGR Arrow 流按批产出几何，
        # 每批画完即丢——对应 QGIS 的 QgsFeatureIterator + filterRect。
        if cancelled is not None and cancelled():
            return
        try:
            batches = provider.iter_bbox(
                snap["bbox"], batch_size=STREAM_CHUNK_FEATURES, columns=[],
                simplify_tol=tol)
        except Exception:
            return
        try:
            for geoms in batches:
                if cancelled is not None and cancelled():
                    return
                if geoms is None or len(geoms) == 0:
                    continue
                _paint_chunk(p, geoms, snap, view, scale, mpp, tol)
                if on_progress is not None:
                    try:
                        on_progress()
                    except Exception:
                        pass
        except Exception:
            # 单批读失败不应该让整个渲染线程退出；已经画出的批次保留。
            return
        finally:
            close = getattr(batches, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        return

    geoms = snap.get("geoms")
    idx = snap.get("indices")
    if geoms is None or idx is None or len(idx) == 0:
        return

    for start in range(0, len(idx), STREAM_CHUNK_FEATURES):
        if cancelled is not None and cancelled():
            return
        chunk_idx = idx[start:start + STREAM_CHUNK_FEATURES]
        if len(chunk_idx) == 0:
            continue
        _paint_chunk(p, geoms[chunk_idx], snap, view, scale, mpp, tol)
        # 让主线程拿到「画到这里」的图层图。是否真的发出去由调用方按时间节流，
        # 这里只负责在块边界通知；一帧内不会把整层路径攒起来。
        if on_progress is not None:
            try:
                on_progress()
            except Exception:
                pass


def _paint_point_batches(p, batches, snap: dict, view: tuple, scale: float,
                         cancelled=None, on_progress=None) -> None:
    """高密度点：按屏幕像素桶去重后一次 drawPoints。

    逐点 `drawEllipse` 在 500 万点时会到几十秒；这里把世界坐标量化到
    1 个设备像素，同一个像素只保留一个点。对点图来说颜色/覆盖率完全不变，
    但 QPainter 的调用量从「点数」降到「被占用的像素数」。
    """
    import numpy as np
    import shapely
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPen, QPolygonF, Qt

    from .mapview import MapView, hex_alpha

    if p is None:
        return
    color = snap.get("color") or "#2f6fed"
    soft = (snap.get("op") or "") in ("buffer", "grid", "merge", "voronoi")
    dot_a = 0.55 if soft else 0.85
    pen = QPen(hex_alpha(color, dot_a))
    pen.setWidth(0)
    pen.setCosmetic(True)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    # 视口在世界坐标里的像素范围；先裁掉 padding 带进来的点。
    px0 = int(np.floor(float(view[0]) * scale))
    py0 = int(np.floor(float(view[1]) * scale))
    px1 = int(np.ceil(float(view[2]) * scale))
    py1 = int(np.ceil(float(view[3]) * scale))
    w = max(1, px1 - px0 + 1)
    h = max(1, py1 - py0 + 1)
    # 屏幕像素占用表。用 numpy 布尔数组而不是 Python set：
    # 500 万次 `k in seen` 的哈希查找本身就是秒级开销。
    seen = np.zeros(w * h, dtype=bool)

    for geoms in batches:
        if cancelled is not None and cancelled():
            return
        if geoms is None or len(geoms) == 0:
            continue
        try:
            valid = ~shapely.is_empty(geoms)
            if not bool(valid.all()):
                geoms = geoms[valid]
            if len(geoms) == 0:
                continue
            coords = shapely.get_coordinates(geoms)
        except Exception:
            continue
        if coords is None or len(coords) == 0:
            continue
        xs, ys = MapView._project_arr(coords)
        qx = np.rint(xs * scale).astype(np.int64)
        qy = np.rint(ys * scale).astype(np.int64)
        keep = (qx >= px0) & (qx <= px1) & (qy >= py0) & (qy <= py1)
        if not bool(keep.all()):
            qx = qx[keep]
            qy = qy[keep]
            if len(qx) == 0:
                continue
        gx = qx - px0
        gy = qy - py0
        keys = gy * w + gx
        uniq, first = np.unique(keys, return_index=True)
        fresh = ~seen[uniq]
        if bool(fresh.any()):
            seen[uniq] = True
            sel = first[fresh]
            poly = QPolygonF([QPointF(float(qx[i]) / scale, float(qy[i]) / scale)
                              for i in sel])
            if not poly.isEmpty():
                p.drawPoints(poly)
        if on_progress is not None:
            try:
                on_progress()
            except Exception:
                pass


def _paint_chunk(p, chunk, snap: dict, view: tuple, scale: float, mpp: float,
                 tol: float) -> None:
    """画一块要素：分类 → 亚像素降点 → 简化 → 建路径 → 交给 `_paint_layer`。"""
    if chunk is None or len(chunk) == 0:
        return
    try:
        import shapely
        from shapely import (get_coordinates, get_num_coordinates, get_parts,
                             get_rings, get_type_id)
    except Exception:
        return

    # 局部导入：避免 vectorstream ↔ mapview 的循环 import
    from .mapview import DOT_PX, MapView, _paint_layer

    # 空几何/缺失几何直接跳过，别让一个坏要素毁掉整块
    try:
        valid = ~shapely.is_empty(chunk)
        if not bool(valid.all()):
            chunk = chunk[valid]
        if len(chunk) == 0:
            return
    except Exception:
        pass

    types = get_type_id(chunk)
    poly_mask = (types == 3) | (types == 6)
    dots = None
    work = chunk

    # 亚像素面：不建轮廓，画一个点（和 _split_subpixel 的语义一致）
    if bool(poly_mask.any()):
        b = shapely.bounds(chunk)
        cx = (b[:, 0] + b[:, 2]) * 0.5
        cy = (b[:, 1] + b[:, 3]) * 0.5
        coslat = np.maximum(np.cos(np.radians(cy)), 1e-6)
        px_w = (b[:, 2] - b[:, 0]) / 360.0 * scale
        px_h = (b[:, 3] - b[:, 1]) / (360.0 * coslat) * scale
        small = poly_mask & (np.maximum(px_w, px_h) < DOT_PX)
        if bool(small.any()):
            xs, ys = MapView._project_arr(np.column_stack((cx[small], cy[small])))
            dots = QPolygonF([QPointF(float(x), float(y)) for x, y in zip(xs, ys)])
            work = chunk[~small]
            if len(work) == 0:
                _paint_layer(p, _style(snap, dots=dots), view, scale, mpp)
                return

    if tol > 0 and len(work):
        try:
            sim = shapely.simplify(work, tol, preserve_topology=False)
            keep = MapView._keep_mask(work, sim)
            if keep is not None and not bool(keep.all()):
                sim = np.where(keep, sim, work)
            work = sim
        except Exception:
            pass

    types = get_type_id(work)
    poly_sel = np.nonzero((types == 3) | (types == 6))[0]
    line_sel = np.nonzero((types == 1) | (types == 5))[0]
    pt_sel = np.nonzero(types == 0)[0]

    poly_paths, poly_bounds = _path_polys(work, poly_sel, get_parts, get_rings,
                                          get_coordinates, get_num_coordinates,
                                          MapView._project_arr)
    line_paths, line_bounds = _path_lines(work, line_sel, get_parts,
                                          get_coordinates, get_num_coordinates,
                                          MapView._project_arr)
    points, point_bounds = _path_points(work, pt_sel, get_coordinates,
                                        MapView._project_arr)

    _paint_layer(p, _style(snap, poly_paths, poly_bounds, line_paths, line_bounds,
                           points, point_bounds, dots), view, scale, mpp)


def _style(snap, poly_paths=None, poly_bounds=None, line_paths=None,
           line_bounds=None, points=None, point_bounds=None, dots=None) -> dict:
    """拼成 `_paint_layer` 认识的图层字典。"""
    poly_paths = poly_paths or []
    poly_bounds = poly_bounds or []
    line_paths = line_paths or []
    line_bounds = line_bounds or []
    points = points or []
    point_bounds = point_bounds or []
    return {
        "op": snap.get("op") or "",
        "color": snap.get("color") or "#2f6fed",
        "style": snap.get("style") or {},
        "geom": snap.get("geom") or "",
        "poly_paths": poly_paths, "npoly": len(poly_paths),
        "poly_bounds": poly_bounds, "npoly_b": len(poly_bounds),
        "line_paths": line_paths, "nline": len(line_paths),
        "line_bounds": line_bounds, "nline_b": len(line_bounds),
        "points": points, "npt": len(points),
        "point_bounds": point_bounds, "npt_b": len(point_bounds),
        # _paint_layer 调用 dots()，这里用 lambda 保持同一接口
        "dots": (lambda: dots) if dots is not None else None,
        "point_dots": bool(snap.get("point_dots")),
    }


def _path_polys(geoms, sel, get_parts, get_rings, get_coordinates,
                get_num_coordinates, project_arr):
    """面要素 → 批量合并的 QPainterPath 列表 + 每批包围盒。"""
    if len(sel) == 0:
        return [], []
    from PySide6.QtGui import QPainterPath

    import shapely as _shapely

    from .mapview import BATCH_FEATURES, BATCH_VERTS

    sub = geoms[sel]
    n_parts = _shapely.get_num_geometries(sub)
    parts = get_parts(sub)
    n_rings = _shapely.get_num_interior_rings(parts) + 1
    rings = get_rings(parts)
    rc = get_coordinates(rings)
    rn = get_num_coordinates(rings)
    if len(rn) == 0:
        return [], []
    xs_all, ys_all = project_arr(rc)
    xs = xs_all.tolist()
    ys = ys_all.tolist()

    part_off = np.concatenate([[0], np.cumsum(n_parts)])
    ring_off = np.concatenate([[0], np.cumsum(rn)])
    ring_cnt = np.concatenate([[0], np.cumsum(n_rings)])
    f_r0 = ring_cnt[part_off[:-1]]
    f_r1 = ring_cnt[part_off[1:]]

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

    paths, bounds = [], []
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
            rx0, rx1, ry0, ry1 = bx0l[r], bx1l[r], by0l[r], by1l[r]
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
        # 和工作线程里的批次大小保持一致；一块通常不会触发，
        # 但跨过 2 万顶点时仍拆批，避免单条路径过大。
        if nf >= BATCH_FEATURES or nv >= BATCH_VERTS:
            if bb[0] <= bb[2]:
                paths.append(path)
                bounds.append(bb)
            path = QPainterPath()
            bb = [1e9, 1e9, -1e9, -1e9]
            nf = 0
            nv = 0
    if nf and bb[0] <= bb[2]:
        paths.append(path)
        bounds.append(bb)
    return paths, bounds


def _path_lines(geoms, sel, get_parts, get_coordinates, get_num_coordinates,
                project_arr):
    """线要素 → 批量合并的 QPainterPath 列表 + 每批包围盒。"""
    from PySide6.QtGui import QPainterPath
    import shapely

    from .mapview import BATCH_FEATURES, BATCH_VERTS

    sub = geoms[sel]
    if len(sub) == 0:
        return [], []
    parts = get_parts(sub)
    lc = get_coordinates(parts)
    ln = get_num_coordinates(parts)
    if len(ln) == 0:
        return [], []
    xs_all, ys_all = project_arr(lc)
    xs = xs_all.tolist()
    ys = ys_all.tolist()
    off = np.concatenate([[0], np.cumsum(ln)])
    f_parts = shapely.get_num_geometries(sub)
    part_off = np.concatenate([[0], np.cumsum(f_parts)])

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

    paths, bounds = [], []
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
            rx0, rx1, ry0, ry1 = bx0l[k], bx1l[k], by0l[k], by1l[k]
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
                paths.append(path)
                bounds.append(bb)
            path = QPainterPath()
            bb = [1e9, 1e9, -1e9, -1e9]
            nf = 0
            nv = 0
    if nf and bb[0] <= bb[2]:
        paths.append(path)
        bounds.append(bb)
    return paths, bounds


def _path_points(geoms, sel, get_coordinates, project_arr):
    """点要素 → 世界坐标列表。"""
    if len(sel) == 0:
        return [], []
    pc = get_coordinates(geoms[sel])
    if pc is None or len(pc) == 0:
        return [], []
    xs, ys = project_arr(pc)
    points, bounds = [], []
    for x, y in zip(xs, ys):
        xf, yf = float(x), float(y)
        points.append((xf, yf))
        bounds.append([xf, yf, xf, yf])
    return points, bounds


def build_lod_paths(batches, tol: float, cancelled=None, on_batch=None,
                    max_verts: int | None = None):
    """把源文件图层的**全部**要素读成「整层一套」绘制路径（世界坐标）。

    为什么需要它
    ------------
    shapefile / GPKG 没有空间索引。省域这种「视口盖住整个图层」的渲染，
    `provider.iter_bbox()` 只能把文件从头读一遍 —— 实测 47 万条道路约
    3.5s，占整帧 6s 的六成。把它按一个固定的粗容差读**一次**、建好路径
    留在内存里，之后 `mpp >= LOD_MIN_MPP` 的宽视野直接画路径：不碰磁盘，
    只在内存里按每批包围盒裁掉视野外的部分。对应 QGIS 的
    「多比例尺预生成 + simplify」思路，只不过我们只做最粗的一级。

    和 `_paint_chunk` 的区别：那条路是「按屏幕 bbox 读窗口、画完即丢」，
    这里读的是整层、结果留给后续帧复用；容差固定，不跟当前视口走。

    返回 `(poly_paths, poly_bounds, line_paths, line_bounds, points,
    point_bounds, nfeat)`；被取消时返回 None。
    """
    try:
        import shapely
        from shapely import (get_coordinates, get_num_coordinates, get_parts,
                             get_rings, get_type_id)
    except Exception:  # noqa: BLE001
        return None

    from .mapview import MapView

    poly_paths: list = []
    poly_bounds: list = []
    line_paths: list = []
    line_bounds: list = []
    points: list = []
    point_bounds: list = []
    nfeat = 0
    nverts = 0

    for chunk in batches:
        if cancelled is not None and cancelled():
            return None
        if chunk is None or len(chunk) == 0:
            continue
        try:
            valid = ~shapely.is_empty(chunk)
            if not bool(valid.all()):
                chunk = chunk[valid]
        except Exception:  # noqa: BLE001
            pass
        if len(chunk) == 0:
            continue
        nfeat += len(chunk)
        if tol > 0:
            try:
                sim = shapely.simplify(chunk, tol, preserve_topology=False)
                # 和分片构建同一条规则：简化把要素抹没了就回退原几何，
                # 否则小湖泊/小建筑在省域视图下会整片消失。
                keep = MapView._keep_mask(chunk, sim)
                if keep is not None and not bool(keep.all()):
                    sim = np.where(keep, sim, chunk)
                chunk = sim
            except Exception:  # noqa: BLE001
                pass

        types = get_type_id(chunk)
        poly_sel = np.nonzero((types == 3) | (types == 6))[0]
        line_sel = np.nonzero((types == 1) | (types == 5))[0]
        pt_sel = np.nonzero(types == 0)[0]

        if max_verts is not None:
            try:
                nverts += int(get_num_coordinates(chunk).sum())
            except Exception:  # noqa: BLE001
                pass
            if nverts > max_verts:
                return None      # 太大（例如几百万个面的建筑层），不值得留在内存

        if len(poly_sel):
            pp, pb = _path_polys(chunk, poly_sel, get_parts, get_rings,
                                 get_coordinates, get_num_coordinates,
                                 MapView._project_arr)
            poly_paths.extend(pp)
            poly_bounds.extend(pb)
        if len(line_sel):
            lp, lb = _path_lines(chunk, line_sel, get_parts, get_coordinates,
                                 get_num_coordinates, MapView._project_arr)
            line_paths.extend(lp)
            line_bounds.extend(lb)
        if len(pt_sel):
            pts, ptb = _path_points(chunk, pt_sel, get_coordinates,
                                    MapView._project_arr)
            points.extend(pts)
            point_bounds.extend(ptb)
        if on_batch is not None:
            try:
                on_batch(nfeat)
            except Exception:  # noqa: BLE001
                pass

    return (poly_paths, poly_bounds, line_paths, line_bounds,
            points, point_bounds, nfeat)
