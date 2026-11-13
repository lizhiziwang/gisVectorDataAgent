"""栅格算子：把「看图」升级成可执行的通用栅格工具。

设计原则和 `app/raster.py` 一致：
· rasterio / GDAL **只在真正执行算子时导入**，应用启动、纯矢量流程不加载它；
· 输入图层一律走 `Store.resolve`，但先校验 `is_raster`，避免拿矢量去跑栅格算子；
· 大影像不整幅读进内存：裁剪按窗口、重投影按块、栅格计算按块、分区统计按要素窗口；
· 每个算子产出新的 GeoTIFF，登记成 `RasterLayer`，原始文件只读不改。

算子清单
--------
raster_info       栅格体检（尺寸/波段/CRS/统计）
raster_clip       按 bbox 或面图层裁剪
raster_reproject  重投影
raster_resample   重采样（缩放）
raster_calc       栅格计算器（b1…bN + 四则/函数，NDVI 等）
zonal_stats       分区统计（面内像元统计 → 给面图层加字段）
raster_merge      多栅格镶嵌
raster_to_vector  栅格转矢量（polygonize）
"""
from __future__ import annotations

import ast
import json
import math
import operator
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- 通用

#: 允许的重采样方式（与 rasterio.enums.Resampling 同名）
RESAMPLING_NAMES = ("nearest", "bilinear", "cubic", "average", "mode", "lanczos")

#: 栅格计算器允许的函数。只放确定性、逐像元的 numpy 函数，不开放任意 Python。
_CALC_FUNCS = {
    "abs": np.abs,
    "sqrt": np.sqrt,
    "log": np.log,
    "log10": np.log10,
    "exp": np.exp,
    "where": np.where,
    "clip": np.clip,
    "minimum": np.minimum,
    "maximum": np.maximum,
}

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_CMP_OPS = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}


def _ok(message: str, layer=None, warnings=None, data=None):
    """延迟导入 Result，避免 engine ↔ raster_ops 的循环 import。"""
    from .engine import Result
    return Result(True, message, layer, warnings or [], data or {})


def _raster_layer(store, ref):
    lay = store.resolve(ref)
    if not getattr(lay, "is_raster", False):
        raise ValueError(f"「{lay.name}」是矢量图层，不是栅格；请选择 .tif 等栅格图层。")
    return lay


def _vector_layer(store, ref):
    lay = store.resolve(ref)
    if getattr(lay, "is_raster", False):
        raise ValueError(f"「{lay.name}」是栅格图层，这个参数需要矢量图层。")
    return lay


def _resampling(name: str):
    from rasterio.enums import Resampling
    key = str(name or "bilinear").strip().lower()
    if key not in RESAMPLING_NAMES:
        raise ValueError(f"重采样方式只能是 {'/'.join(RESAMPLING_NAMES)}，收到「{name}」")
    return getattr(Resampling, key)


def _parse_bbox(value):
    parts = [p.strip() for p in str(value).replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError("bbox 需要 4 个数字：x0,y0,x1,y1（经纬度）")
    try:
        x0, y0, x1, y1 = (float(p) for p in parts)
    except ValueError:
        raise ValueError("bbox 的 4 个值都必须是数字，例如 104.0,30.5,104.1,30.6")
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    if not (-180 <= x0 <= 180 and -180 <= x1 <= 180 and -90 <= y0 <= 90 and -90 <= y1 <= 90):
        raise ValueError("bbox 必须是经纬度（经度 -180~180，纬度 -90~90）")
    return x0, y0, x1, y1


def _out_path(store, tag: str) -> Path:
    base = getattr(store, "cache_dir", None)
    if base is None:
        base = Path(tempfile.mkdtemp(prefix="gis_raster_"))
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    return base / f"R{uuid.uuid4().hex[:8]}_{tag}.tif"


def _profile(src, count: int, dtype, width: int, height: int,
             transform, nodata, crs=None) -> dict:
    """生成输出 GeoTIFF 的 profile。不复制源 profile 的 photometric/interleave，
    免得 3 波段源生成的单波段结果带着非法的 RGB 标记。"""
    return {
        "driver": "GTiff",
        "width": int(width),
        "height": int(height),
        "count": int(count),
        "dtype": np.dtype(dtype).name,
        "crs": src.crs if crs is None else crs,
        "transform": transform,
        "nodata": nodata,
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "compress": "deflate",
        "BIGTIFF": "IF_SAFER",
    }


def _fill_nodata(src, nodata, dtype):
    """输出 nodata 的默认值：优先用户传的，其次源 nodata，最后按 dtype 选择。"""
    if nodata is not None:
        return nodata
    if src.nodata is not None:
        return src.nodata
    return np.nan if np.dtype(dtype).kind == "f" else 0


def _register_output(store, path: Path, name: str, source: str, op: str):
    from .raster import read_info
    info = read_info(path)
    lay = store.add_raster(info, name, source=source, op=op)
    # 让 Store.remove/clear 能清掉这些派生文件；原始文件不会被设置这个字段
    lay.cache_path = str(path)
    # 处理结果紧贴在源图层的上面：渲染顺序 = 图层面板顺序，否则结果会
    # 藏在源栅格底下（以前栅格有固定的「结果最后画」顺序，现在没有了）。
    if source in store.layers:
        ids = [lid for lid in store.layers if lid != lay.id]
        at = ids.index(source) + 1
        store.reorder(ids[:at] + [lay.id] + ids[at:])
    return lay


def _fmt_num(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except Exception:
        return str(v)
    if not math.isfinite(f):
        return "—"
    if abs(f) >= 1000:
        return f"{f:,.1f}"
    if abs(f) >= 1:
        return f"{f:.3f}".rstrip("0").rstrip(".")
    return f"{f:.5f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------- 体检


def _sample_stats(lay, max_side: int = 1024) -> dict:
    """抽样统计：最多读 1024 见方，靠 GDAL 的 overview/decimation，不整幅解码。"""
    import rasterio
    from rasterio.enums import Resampling

    with rasterio.open(lay.info.path) as ds:
        k = min(1.0, max_side / float(max(ds.width, ds.height)))
        w = max(1, int(round(ds.width * k)))
        h = max(1, int(round(ds.height * k)))
        arr = ds.read(out_shape=(ds.count, h, w), resampling=Resampling.nearest)
        nodata = ds.nodata
        out = {}
        for i in range(ds.count):
            a = arr[i].astype(np.float64, copy=False)
            valid = np.isfinite(a)
            if nodata is not None:
                valid &= a != nodata
            if not valid.any():
                out[i + 1] = {"count": 0}
                continue
            v = a[valid]
            out[i + 1] = {
                "count": int(v.size),
                "min": float(v.min()),
                "max": float(v.max()),
                "mean": float(v.mean()),
                "std": float(v.std()),
                "p2": float(np.percentile(v, 2)),
                "p98": float(np.percentile(v, 98)),
            }
        return {"sampled_width": w, "sampled_height": h, "bands": out}


def op_raster_info(store, layer=None, name=None) -> object:
    """栅格体检：元信息 + 抽样统计，不产生新图层。"""
    lay = _raster_layer(store, layer)
    info = lay.info
    stats = _sample_stats(lay)
    band_bits = []
    for b, s in stats["bands"].items():
        if not s.get("count"):
            band_bits.append(f"波段 {b}：全为 nodata")
            continue
        band_bits.append(
            f"波段 {b}：{_fmt_num(s['min'])} ~ {_fmt_num(s['max'])}"
            f"（均值 {_fmt_num(s['mean'])}，P2–P98 {_fmt_num(s['p2'])} ~ {_fmt_num(s['p98'])}）")
    b = info.bounds or (0, 0, 0, 0)
    res = info.res or (0.0, 0.0)
    msg = (f"栅格「{lay.name}」：{info.width}×{info.height} 像元，{info.bands} 波段，"
           f"类型 {'/'.join(info.dtypes or []) or '未知'}，坐标系 {info.crs_name or '未定义'}，"
           f"分辨率 {res[0]:.6g}×{res[1]:.6g}，nodata {info.nodata}。\n"
           f"范围：{b[0]:.5f}, {b[1]:.5f} → {b[2]:.5f}, {b[3]:.5f}。\n"
           + "\n".join(band_bits)
           + (f"\n（统计基于 {stats['sampled_width']}×{stats['sampled_height']} 抽样）"
              if (stats["sampled_width"], stats["sampled_height"]) != (info.width, info.height) else ""))
    data = {"raster": info.as_dict(), "stats": stats["bands"]}
    return _ok(msg, None, [], data)


# --------------------------------------------------------------------------- 裁剪


def op_raster_clip(store, layer=None, mask=None, bbox=None, all_touched=False,
                   nodata=None, name=None) -> object:
    """按经纬度范围或面图层裁剪栅格，输出新的 GeoTIFF。

    裁剪走独立子进程：rasterio/GDAL 的读、几何栅格化、Deflate 压缩都可能
    长时间不释放 GIL，放在 Agent 工作线程里会让 Qt 主线程“未响应”。
    子进程只负责写 GeoTIFF，主进程负责注册图层。
    """
    lay = _raster_layer(store, layer)
    if mask and bbox:
        raise ValueError("mask 和 bbox 只能二选一：要么用面图层裁，要么用经纬度范围裁。")
    if not mask and not bbox:
        raise ValueError("请提供 mask（面图层）或 bbox（x0,y0,x1,y1）之一。")

    tmp_paths: list[Path] = []
    out_path = _out_path(store, "clip")
    cfg: dict = {
        "raster": str(lay.info.path),
        "out": str(out_path),
        "all_touched": bool(all_touched),
        "nodata": nodata,
        "block": 1024,
    }
    try:
        if mask:
            mk = _vector_layer(store, mask)
            geom_type = getattr(mk, "geom_type", "")
            if "Polygon" not in geom_type:
                raise ValueError(f"裁剪面「{mk.name}」不是面图层（{geom_type}），无法用于栅格裁剪。")
            gdf = mk.gdf
            if gdf.crs is None:
                raise ValueError(f"裁剪面「{mk.name}」没有坐标系，无法和栅格对齐。")
            mf_fd, mf_name = tempfile.mkstemp(suffix=".geojson")
            os.close(mf_fd)
            mf = Path(mf_name)
            tmp_paths.append(mf)
            gdf.to_file(mf, driver="GeoJSON")
            cfg["mask_geojson"] = str(mf)
        else:
            cfg["bbox"] = [float(v) for v in _parse_bbox(bbox)]
        cf_fd, cf_name = tempfile.mkstemp(suffix=".json")
        os.close(cf_fd)
        cf = Path(cf_name)
        tmp_paths.append(cf)
        rf_fd, rf_name = tempfile.mkstemp(suffix=".result.json")
        os.close(rf_fd)
        rf = Path(rf_name)
        tmp_paths.append(rf)
        cfg["result"] = str(rf)
        cf.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "app.raster_clip_worker", str(cf)],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1800,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = {}
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if proc.returncode != 0 or not data.get("ok"):
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            err = str(data.get("error") or "").strip()
            raise ValueError("按面裁剪失败：" + (err or "；".join(tail)
                                                or f"子进程退出 {proc.returncode}"))
        width = int(data.get("width") or 0)
        height = int(data.get("height") or 0)
        if not out_path.is_file() or width < 1 or height < 1:
            raise ValueError("裁剪结果为空。")
        out = _register_output(store, out_path, name or f"{lay.name}_裁剪",
                               lay.id, "raster_clip")
    except Exception:
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        for p in tmp_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    msg = (f"裁剪「{lay.name}」：{lay.info.width}×{lay.info.height} → {width}×{height}，"
           f"输出「{out.name}」。")
    return _ok(msg, out, [], {"width": width, "height": height})


# --------------------------------------------------------------------------- 重投影


def op_raster_reproject(store, layer=None, epsg=None, resampling="bilinear",
                        resolution=None, name=None) -> object:
    """把栅格重投影到目标 EPSG；按输出块写，避免整幅数组常驻内存。"""
    import rasterio
    from rasterio.crs import CRS
    from rasterio.warp import calculate_default_transform, reproject, transform_bounds

    lay = _raster_layer(store, layer)
    code = str(epsg).strip().upper().replace("EPSG:", "").replace("EPSG", "")
    try:
        dst_crs = CRS.from_epsg(int(code))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"目标 EPSG 不合法：{epsg}（示例：4326、3857、4544）") from e

    with rasterio.open(lay.info.path) as src:
        if src.crs is None:
            raise ValueError("栅格没有坐标系，无法重投影。")
        res = float(resolution) if resolution not in (None, "") else None
        if res is not None and res <= 0:
            raise ValueError("resolution 必须大于 0。")
        if res is None:
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds)
        else:
            left, bottom, right, top = transform_bounds(
                src.crs, dst_crs, *src.bounds, densify_pts=21)
            width = max(1, int(math.ceil((right - left) / res)))
            height = max(1, int(math.ceil((top - bottom) / res)))
            from affine import Affine
            transform = Affine(res, 0.0, left, 0.0, -res, top)
        if width * height > 500_000_000:
            raise ValueError(f"目标尺寸 {width}×{height} 太大（超过 5 亿像元），"
                             "请调大 resolution 或先裁剪。")
        dtype = np.dtype(src.dtypes[0])
        nd = _fill_nodata(src, None, dtype)
        path = _out_path(store, "reproject")
        prof = _profile(src, src.count, dtype, width, height, transform, nd, crs=dst_crs)
        with rasterio.open(path, "w", **prof) as dst:
            for b in range(1, src.count + 1):
                for _, win in dst.block_windows(1):
                    dest = np.full((win.height, win.width), nd, dtype=dtype)
                    reproject(
                        source=rasterio.band(src, b),
                        destination=dest,
                        src_transform=src.transform, src_crs=src.crs,
                        src_nodata=src.nodata,
                        dst_transform=dst.window_transform(win), dst_crs=dst_crs,
                        dst_nodata=nd, resampling=_resampling(resampling),
                    )
                    dst.write(dest, b, window=win)
        out = _register_output(store, path, name or f"{lay.name}_EPSG{code}",
                               lay.id, "raster_reproject")

    msg = (f"重投影「{lay.name}」：{lay.info.crs_name} → EPSG:{code}，"
           f"输出 {width}×{height}，图层「{out.name}」。")
    return _ok(msg, out, [], {"width": width, "height": height, "epsg": int(code)})


# --------------------------------------------------------------------------- 重采样


def op_raster_resample(store, layer=None, scale=0.5, resolution=None,
                       resampling="bilinear", name=None) -> object:
    """按比例或目标分辨率重采样（0.5 = 宽高各减半；resolution = 目标像元大小）。"""
    import rasterio
    from affine import Affine

    lay = _raster_layer(store, layer)
    res = None
    if resolution not in (None, ""):
        try:
            res = float(resolution)
        except (TypeError, ValueError):
            raise ValueError(f"resolution 必须是数字，收到 {resolution!r}")
        if res <= 0:
            raise ValueError("resolution 必须大于 0。")
    try:
        factor = float(scale)
    except (TypeError, ValueError):
        raise ValueError(f"scale 必须是数字，收到 {scale!r}")
    if res is None and factor <= 0:
        raise ValueError("scale 必须大于 0；0.5 表示宽高各减半，2 表示放大一倍。")
    with rasterio.open(lay.info.path) as src:
        if res is not None:
            new_w = max(1, int(round(src.width * abs(src.res[0]) / res)))
            new_h = max(1, int(round(src.height * abs(src.res[1]) / res)))
        else:
            new_w = max(1, int(round(src.width * factor)))
            new_h = max(1, int(round(src.height * factor)))
        if new_w * new_h > 300_000_000:
            raise ValueError(f"目标尺寸 {new_w}×{new_h} 太大，请用更小的 scale 或更大的 resolution。")
        arr = src.read(out_shape=(src.count, new_h, new_w),
                       resampling=_resampling(resampling))
        transform = src.transform * Affine.scale(src.width / new_w, src.height / new_h)
        dtype = np.dtype(src.dtypes[0])
        nd = _fill_nodata(src, None, dtype)
        path = _out_path(store, "resample")
        prof = _profile(src, src.count, dtype, new_w, new_h, transform, nd)
        with rasterio.open(path, "w", **prof) as dst:
            dst.write(arr.astype(dtype, copy=False))
        out = _register_output(store, path, name or f"{lay.name}_重采样",
                               lay.id, "raster_resample")

    how = f"resolution={res:g}" if res is not None else f"scale={factor:g}"
    msg = (f"重采样「{lay.name}」：{lay.info.width}×{lay.info.height} → {new_w}×{new_h}"
           f"（{how}，{resampling}），输出「{out.name}」。")
    return _ok(msg, out, [], {"width": new_w, "height": new_h,
                              "scale": factor, "resolution": res})


# --------------------------------------------------------------------------- 栅格计算


def _normalize_expr(expr: str) -> str:
    return (str(expr or "").strip()
            .replace("（", "(").replace("）", ")")
            .replace("×", "*").replace("÷", "/")
            .replace("−", "-").replace("^", "**"))


def _eval_ast(node, env):
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, env)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        raise ValueError(f"未知变量 `{node.id}`；波段变量是 b1、b2…（对应第 1、2…波段）")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        with np.errstate(all="ignore"):
            return _BIN_OPS[type(node.op)](_eval_ast(node.left, env),
                                           _eval_ast(node.right, env))
    if isinstance(node, ast.UnaryOp):
        v = _eval_ast(node.operand, env)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return v
        raise ValueError("只支持正负号一元运算")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fn = _CALC_FUNCS.get(node.func.id)
        if fn is None:
            raise ValueError(f"不支持的函数 `{node.func.id}`；可用：{'、'.join(_CALC_FUNCS)}")
        if node.keywords:
            raise ValueError("函数不支持关键字参数")
        return fn(*[_eval_ast(a, env) for a in node.args])
    if isinstance(node, ast.Compare):
        left = _eval_ast(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            fn = _CMP_OPS.get(type(op))
            if fn is None:
                raise ValueError("不支持的比较运算")
            right = _eval_ast(comp, env)
            left = fn(left, right)
        return left
    if isinstance(node, ast.BoolOp):
        vals = [_eval_ast(v, env) for v in node.values]
        out = vals[0]
        for v in vals[1:]:
            out = np.logical_and(out, v) if isinstance(node.op, ast.And) else np.logical_or(out, v)
        return out
    if isinstance(node, ast.IfExp):
        return np.where(_eval_ast(node.test, env),
                        _eval_ast(node.body, env), _eval_ast(node.orelse, env))
    raise ValueError("表达式只能包含数字、b1…bN、+ - * / ** %、比较和 where/clip 等函数")


def _compile_expr(expression: str, band_count: int):
    expr = _normalize_expr(expression)
    if not expr:
        raise ValueError("expression 不能为空，例如 (b4 - b3) / (b4 + b3)")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误：{e.msg}") from e
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id.startswith("b")}
    for name in sorted(used):
        try:
            idx = int(name[1:])
        except ValueError:
            continue
        if idx < 1 or idx > band_count:
            raise ValueError(f"表达式引用了 {name}，但这个栅格只有 {band_count} 个波段")
    return tree, expr


def op_raster_calc(store, layer=None, expression=None, name=None) -> object:
    """逐像元栅格计算：b1…bN 是波段，输出单波段 float32（nodata=NaN）。

    按 1024 见方的块读写，避免把大影像整幅转成 float 数组。
    """
    import rasterio
    from rasterio.windows import Window

    lay = _raster_layer(store, layer)
    with rasterio.open(lay.info.path) as src:
        tree, expr = _compile_expr(expression, src.count)
        path = _out_path(store, "calc")
        prof = _profile(src, 1, np.float32, src.width, src.height,
                        src.transform, np.nan)
        block = 1024
        with rasterio.open(path, "w", **prof) as dst:
            for row in range(0, src.height, block):
                for col in range(0, src.width, block):
                    h = min(block, src.height - row)
                    w = min(block, src.width - col)
                    win = Window(col, row, w, h)
                    arr = src.read(window=win).astype(np.float32, copy=False)
                    if src.nodata is not None:
                        arr[arr == np.float32(src.nodata)] = np.nan
                    env = {f"b{i + 1}": arr[i] for i in range(src.count)}
                    with np.errstate(all="ignore"):
                        val = _eval_ast(tree, env)
                    val = np.asarray(val, dtype=np.float32)
                    if val.ndim == 0:
                        val = np.full((h, w), float(val), dtype=np.float32)
                    elif val.shape != (h, w):
                        val = np.broadcast_to(val, (h, w)).astype(np.float32, copy=False)
                    val = np.where(np.isfinite(val), val, np.nan).astype(np.float32, copy=False)
                    dst.write(val, 1, window=win)
        out = _register_output(store, path, name or f"{lay.name}_计算", lay.id, "raster_calc")

    msg = (f"栅格计算「{lay.name}」：`{expr}` → 单波段 float32，"
           f"输出 {lay.info.width}×{lay.info.height}，图层「{out.name}」。")
    return _ok(msg, out, [], {"expression": expr, "width": lay.info.width,
                              "height": lay.info.height})


# --------------------------------------------------------------------------- 分区统计


def _stat_values(values: np.ndarray, stats: list[str]) -> dict:
    out = {"count": int(values.size)}
    if values.size == 0:
        for s in stats:
            if s != "count":
                out[s] = None
        return out
    if "mean" in stats:
        out["mean"] = float(values.mean())
    if "min" in stats:
        out["min"] = float(values.min())
    if "max" in stats:
        out["max"] = float(values.max())
    if "std" in stats:
        out["std"] = float(values.std())
    if "sum" in stats:
        out["sum"] = float(values.sum())
    if "median" in stats:
        out["median"] = float(np.median(values))
    return out


def op_zonal_stats(store, layer=None, raster=None, band=1,
                   stats="mean,min,max,std,sum,count", prefix="r",
                   all_touched=False, name=None) -> object:
    """分区统计：把栅格值按面要素汇总，给面图层新增字段。

    对每个面用 `geometry_window` 只读它覆盖的像元块，不整幅读栅格。
    """
    import rasterio
    from rasterio.errors import WindowError
    from rasterio.features import geometry_mask, geometry_window

    poly = _vector_layer(store, layer)
    ras = _raster_layer(store, raster)
    geom_type = getattr(poly, "geom_type", "")
    if "Polygon" not in geom_type:
        raise ValueError(f"分区统计需要面图层，「{poly.name}」是 {geom_type}。")
    try:
        band_no = int(band)
    except (TypeError, ValueError):
        raise ValueError(f"band 必须是整数，收到 {band!r}")
    if band_no < 1 or band_no > ras.info.bands:
        raise ValueError(f"波段 {band_no} 超出范围（这个栅格有 {ras.info.bands} 个波段）")
    if isinstance(stats, (list, tuple, set)):
        stat_list = [str(s).strip().lower() for s in stats if str(s).strip()]
    else:
        stat_list = [s.strip().lower() for s in str(stats or "mean,min,max,std,sum,count")
                     .replace("，", ",").split(",") if s.strip()]
    allowed = {"mean", "min", "max", "std", "sum", "count", "median"}
    bad = [s for s in stat_list if s not in allowed]
    if bad:
        raise ValueError(f"不支持的统计项：{'、'.join(bad)}；可用：{'、'.join(sorted(allowed))}")
    stat_list = list(dict.fromkeys(stat_list))
    requested = list(stat_list)
    # count 是判断「这个面里到底有没有有效像元」的依据；用户没要也内部算一份，
    # 但不输出成字段。
    if "count" not in stat_list:
        stat_list.append("count")
    prefix = str(prefix or "").strip() or "r"

    gdf = poly.gdf
    if len(gdf) == 0:
        raise ValueError(f"「{poly.name}」没有面要素，无法做分区统计。")
    if gdf.crs is None:
        raise ValueError(f"「{poly.name}」没有坐标系，无法和栅格对齐。")
    if ras.info.crs is None:
        raise ValueError("栅格没有坐标系，无法做分区统计。")

    results = {s: [] for s in stat_list}
    with rasterio.open(ras.info.path) as ds:
        if gdf.crs.to_string() != ds.crs.to_string():
            gdf = gdf.to_crs(ds.crs.to_string())
        full = rasterio.windows.Window(0, 0, ds.width, ds.height)
        for geom in gdf.geometry:
            vals = np.empty(0, dtype=np.float64)
            if geom is not None and not geom.is_empty:
                try:
                    win = geometry_window(ds, [geom], pad_x=0, pad_y=0)
                    win = win.round_offsets().round_lengths().intersection(full)
                    if win.width >= 1 and win.height >= 1:
                        data = ds.read(band_no, window=win)
                        mask = geometry_mask(
                            [geom], out_shape=data.shape,
                            transform=ds.window_transform(win),
                            invert=True, all_touched=bool(all_touched))
                        vals = data[mask].astype(np.float64, copy=False)
                except (WindowError, ValueError):
                    vals = np.empty(0, dtype=np.float64)
            if ds.nodata is not None:
                vals = vals[vals != ds.nodata]
            vals = vals[np.isfinite(vals)]
            st = _stat_values(vals, stat_list)
            for s in stat_list:
                results[s].append(st.get(s))

    out = gdf.copy()
    existing = set(map(str, out.columns))
    fields = {}
    for s in requested:
        base = f"{prefix}_{s}"
        fld, i = base, 2
        while fld in existing:
            fld = f"{base}_{i}"
            i += 1
        existing.add(fld)
        out[fld] = results[s]
        fields[s] = fld
    nl = store.add(out, name or f"{poly.name}_分区统计", source=poly.id, op="zonal_stats")
    n_ok = sum(1 for v in results.get("count", []) if v)
    msg = (f"分区统计：用「{ras.name}」波段 {band_no} 统计「{poly.name}」的 "
           f"{len(out)} 个面（{n_ok} 个面内有有效像元），新增字段 "
           f"{'、'.join(fields.values())}。")
    return _ok(msg, nl, [], {"fields": fields, "features": len(out),
                             "features_with_data": n_ok})


# --------------------------------------------------------------------------- 镶嵌


def op_raster_merge(store, layers=None, resampling="bilinear", nodata=None,
                    name=None) -> object:
    """把多个栅格镶嵌成一个；要求同 CRS、同波段数、同 dtype。"""
    import rasterio
    from rasterio.merge import merge as merge_raster

    refs = layers
    if isinstance(refs, str):
        refs = [x.strip() for x in refs.replace("，", ",").split(",") if x.strip()]
    refs = list(refs or [])
    if any(str(r) in ("*", "all", "全部") for r in refs):
        refs = [l.id for l in store.layers.values() if getattr(l, "is_raster", False)]
    if not refs:
        raise ValueError("请提供要镶嵌的栅格图层（layers 数组），或用 * 表示全部栅格。")
    rasters = [_raster_layer(store, r) for r in refs]
    if not rasters:
        raise ValueError("没有可镶嵌的栅格图层。")

    handles = []
    try:
        for r in rasters:
            handles.append(rasterio.open(r.info.path))
        first = handles[0]
        for h in handles[1:]:
            if h.crs != first.crs:
                raise ValueError("镶嵌要求所有栅格坐标系一致；请先分别重投影到同一 EPSG。")
            if h.count != first.count:
                raise ValueError(f"镶嵌要求波段数一致：{first.count} vs {h.count}。")
            if h.dtypes != first.dtypes:
                raise ValueError("镶嵌要求像元类型一致；请先统一 dtype。")
        total = sum(h.width * h.height for h in handles)
        if total > 400_000_000:
            raise ValueError("镶嵌结果超过 4 亿像元，内存风险太大；请先裁剪或重采样。")
        nd = nodata if nodata is not None else first.nodata
        arr, transform = merge_raster(handles, nodata=nd,
                                      resampling=_resampling(resampling))
        dtype = np.dtype(first.dtypes[0])
        path = _out_path(store, "merge")
        prof = _profile(first, first.count, dtype, arr.shape[2], arr.shape[1],
                        transform, nd)
        with rasterio.open(path, "w", **prof) as dst:
            dst.write(arr.astype(dtype, copy=False))
        out = _register_output(store, path, name or "栅格镶嵌", rasters[0].id,
                               "raster_merge")
    finally:
        for h in handles:
            try:
                h.close()
            except Exception:
                pass

    msg = (f"镶嵌 {len(rasters)} 个栅格：输出 {arr.shape[2]}×{arr.shape[1]}，"
           f"图层「{out.name}」。")
    return _ok(msg, out, [], {"source_layers": len(rasters),
                              "width": int(arr.shape[2]), "height": int(arr.shape[1])})


# --------------------------------------------------------------------------- 转矢量


def op_raster_to_vector(store, layer=None, band=1, scale=1.0,
                        name=None) -> object:
    """把栅格按像元值转成面（polygonize）；scale<1 先降采样，控制要素数量。"""
    import geopandas as gpd
    import rasterio
    from affine import Affine
    from rasterio.features import shapes
    from shapely.geometry import shape

    lay = _raster_layer(store, layer)
    try:
        band_no = int(band)
        factor = float(scale)
    except (TypeError, ValueError):
        raise ValueError("band 必须是整数，scale 必须是数字")
    if band_no < 1 or band_no > lay.info.bands:
        raise ValueError(f"波段 {band_no} 超出范围（这个栅格有 {lay.info.bands} 个波段）")
    if not (0 < factor <= 1):
        raise ValueError("scale 必须在 (0, 1] 范围内（1 = 原始分辨率，0.5 = 先降采样一半）")

    with rasterio.open(lay.info.path) as src:
        new_w = max(1, int(round(src.width * factor)))
        new_h = max(1, int(round(src.height * factor)))
        if new_w * new_h > 30_000_000:
            raise ValueError(f"转矢量需要 {new_w}×{new_h} 像元，太大；请把 scale 调小。")
        arr = src.read(band_no, out_shape=(new_h, new_w),
                       resampling=rasterio.enums.Resampling.nearest)
        transform = src.transform * Affine.scale(src.width / new_w, src.height / new_h)
        valid = np.isfinite(arr)
        if src.nodata is not None:
            valid &= arr != src.nodata
        if not valid.any():
            raise ValueError("这一波段全是 nodata，没有可转矢量的像元。")
        geoms, values = [], []
        for geom, val in shapes(arr, mask=valid, transform=transform):
            geoms.append(shape(geom))
            values.append(val.item() if hasattr(val, "item") else val)
        crs = src.crs
    if not geoms:
        raise ValueError("没有提取到任何面。")
    out = gpd.GeoDataFrame({"value": values}, geometry=geoms, crs=crs)
    nl = store.add(out, name or f"{lay.name}_矢量", source=lay.id, op="raster_to_vector")
    msg = (f"栅格转矢量「{lay.name}」波段 {band_no}："
           f"{new_w}×{new_h} 像元 → {len(out)} 个面，输出「{nl.name}」。")
    return _ok(msg, nl, [], {"features_out": len(out), "width": new_w, "height": new_h})


# --------------------------------------------------------------------------- 注册表

OPERATIONS = {
    "raster_info": op_raster_info,
    "raster_clip": op_raster_clip,
    "raster_reproject": op_raster_reproject,
    "raster_resample": op_raster_resample,
    "raster_calc": op_raster_calc,
    "zonal_stats": op_zonal_stats,
    "raster_merge": op_raster_merge,
    "raster_to_vector": op_raster_to_vector,
}
