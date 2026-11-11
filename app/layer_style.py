"""图层渲染样式：矢量符号 + 要素标注的取值、默认值与规范化。

为什么要单独一个模块：同一份样式要被三处读 ——
· `native/mapview.py` 的渲染线程（`_paint_layer`）拿它决定颜色/线宽/点大小；
· `native/style_dialog.py` 的样式弹窗拿它做初值和预览；
· 会话恢复（`native/app.py::_restore_session_layers`）拿它把样式原样装回去。

默认值必须和改造前的硬编码完全一致（对照 `_paint_layer` 里的 op 分支）：
缓冲区/渔网/合并等「软」算子填充 4%、描边 26%；融合 8%；其余 14% / 70%；
线图层描边 100%（按比例尺 1px / 1.6px 两档，这个动态逻辑留在渲染器里）；
点半径 3.4px。
"""
from __future__ import annotations

import re

#: 这些算子的结果通常是大片派生面，默认画得更淡，避免盖住原始数据
SOFT_OPS = {"buffer", "grid", "merge", "voronoi"}

#: 矢量样式里认识的键。不在表里的键（老版本残留、手改的）一律丢掉。
VECTOR_KEYS = (
    "fill_color", "fill_opacity",
    "stroke_color", "stroke_opacity", "stroke_width",
    "point_color", "point_size",
    "label_field", "label_size", "label_color", "label_halo",
)

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def valid_color(value, fallback: str = "#2f6fed") -> str:
    """合法性检查：只收 `#rrggbb`。弹窗、会话文件、手改 JSON 都可能送来坏值。"""
    s = str(value or "").strip()
    if _HEX.match(s):
        return s.lower()
    return fallback


def geom_kind(geom_type: str | None) -> str:
    """把 GeoPandas 的几何类型归成三档。混层/未知返回空串（按面处理）。"""
    s = str(geom_type or "").lower()
    if "point" in s:
        return "point"
    if "line" in s:
        return "line"
    if "polygon" in s or "surface" in s:
        return "polygon"
    return ""


def default_vector_style(op: str = "", color: str = "#2f6fed",
                         geom_type: str = "") -> dict:
    """按算子/几何类型给出一套「和现在长得一样」的默认样式。"""
    color = valid_color(color)
    kind = geom_kind(geom_type)
    soft = op in SOFT_OPS
    stroke_opacity = 100 if kind == "line" else (26 if soft else 70)
    return {
        "fill_color": color,
        "fill_opacity": 4 if soft else (8 if op == "dissolve" else 14),
        "stroke_color": color,
        "stroke_opacity": stroke_opacity,
        # 线图层的动态宽度（1px / 1.6px）由渲染器按比例尺决定；
        # 这里的值只在用户真的改了线宽之后才生效（见 `_paint_layer`）。
        "stroke_width": 1.0,
        "point_color": color,
        "point_size": 6.8,
        "label_field": "",
        "label_size": 11,
        "label_color": "#2b2a26",
        "label_halo": True,
    }


def _clamp(value, lo: float, hi: float, fallback: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    if v != v:                       # NaN
        return fallback
    return max(lo, min(hi, v))


def normalize_vector_style(style: dict | None, op: str = "",
                           color: str = "#2f6fed", geom_type: str = "") -> dict:
    """补默认值 + 夹范围 + 丢掉不认识的键；结果可以安全地落盘和渲染。"""
    out = default_vector_style(op, color, geom_type)
    if not isinstance(style, dict):
        return out
    for key in VECTOR_KEYS:
        if key not in style:
            continue
        val = style[key]
        if key.endswith("_color"):
            out[key] = valid_color(val, out[key])
        elif key in ("fill_opacity", "stroke_opacity"):
            out[key] = int(round(_clamp(val, 0, 100, out[key])))
        elif key == "stroke_width":
            out[key] = round(_clamp(val, 0.5, 10.0, out[key]), 2)
        elif key == "point_size":
            out[key] = round(_clamp(val, 2.0, 40.0, out[key]), 1)
        elif key == "label_size":
            out[key] = int(round(_clamp(val, 8, 24, out[key])))
        elif key == "label_halo":
            out[key] = bool(val)
        elif key == "label_field":
            out[key] = str(val or "")
    return out


def resolve_vector_style(op: str = "", color: str = "#2f6fed",
                         style: dict | None = None, geom_type: str = "") -> dict:
    """图层当前实际生效的样式（默认值 + 用户覆盖）。"""
    return normalize_vector_style(style, op, color, geom_type)


def style_signature(style: dict | None) -> tuple:
    """渲染缓存键用的稳定签名：值全部转成可哈希的标量。"""
    if not isinstance(style, dict):
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in style.items()))


# ------------------------------------------------------------------ 栅格
RASTER_STRETCH = ("percentile", "minmax", "none")


def normalize_raster_style(style: dict | None, base: dict | None = None) -> dict:
    """栅格样式：只规范弹窗会改的三项（透明度/拉伸/gamma），其余原样保留。"""
    out = dict(base or {})
    if isinstance(style, dict):
        out.update({k: v for k, v in style.items() if not k.startswith("_")})
    out["opacity"] = round(_clamp(out.get("opacity"), 0.0, 1.0, 1.0), 3)
    stretch = str(out.get("stretch") or "percentile").lower()
    out["stretch"] = stretch if stretch in RASTER_STRETCH else "percentile"
    out["gamma"] = round(_clamp(out.get("gamma"), 0.2, 3.0, 1.0), 2)
    return out
