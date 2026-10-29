"""格式化小工具（数值、距离、步骤字段中文名）。"""
from __future__ import annotations

LABELS = {
    "distance_m": "缓冲距离", "features_in": "输入要素", "features_out": "输出要素",
    "tolerance_m": "容差", "node_reduction_pct": "节点减少", "cells": "网格数",
    "points": "点数", "mean_distance_m": "平均距离", "removed": "移除要素",
    "repaired": "修复几何", "empty_removed": "清理空几何", "from": "原坐标系",
    "to": "目标坐标系", "expression": "表达式", "predicate": "空间关系",
    "total": "合计", "area_m2": "总面积", "length_m": "总长度", "meta": "概况",
}
HIDDEN = {"features_in", "features_out", "table", "meta", "unit", "total"}


def fmt_num(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        if v.is_integer():
            return f"{int(v):,}"
        return f"{v:,.2f}" if abs(v) >= 1000 else f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def fmt_meters(m) -> str:
    if m is None:
        return "—"
    try:
        m = float(m)
    except Exception:
        return str(m)
    if abs(m) >= 1000:
        return f"{m / 1000:.{0 if abs(m) >= 10000 else 2}f} km"
    return f"{m:.{1 if abs(m) < 10 else 0}f} m"


def kv_text(data: dict) -> str:
    """把步骤的 data 拼成一行可读摘要。"""
    if not isinstance(data, dict):
        return ""
    parts = []
    if "features_in" in data and "features_out" in data:
        parts.append(f"要素 {fmt_num(data['features_in'])} → {fmt_num(data['features_out'])}")
    for k, v in data.items():
        if k in HIDDEN or v is None or isinstance(v, (dict, list)):
            continue
        label = LABELS.get(k, k)
        val = fmt_meters(v) if (k.endswith("_m") or k.endswith("_m2")) else fmt_num(v)
        parts.append(f"{label} {val}")
    return " · ".join(parts)
