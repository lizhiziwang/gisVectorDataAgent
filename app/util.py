"""JSON 安全化工具。

真实 GIS 数据（尤其 OSM / Shapefile）里空值极多：DBF 的 null 读进 pandas 后
往往变成 float('nan') 或 pd.NA / NaT。Starlette 的 JSONResponse 用
`json.dumps(..., allow_nan=False)`，遇到 NaN/Inf 会直接抛
`ValueError: Out of range float values are not JSON compliant`，表现为接口 500。
所以出口处统一做一次清洗：NaN/Inf → null，numpy 标量 → Python 原生类型，
时间 → ISO 字符串，其余未知类型 → str。
"""
from __future__ import annotations

import datetime as _dt
import json
import math
from typing import Any

import numpy as np
import pandas as pd


def json_safe(obj: Any) -> Any:
    """递归把任意对象转成可被严格 JSON 序列化的结构。"""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj
    if obj is True or obj is False:
        return obj
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.generic):
        return json_safe(obj.item())
    if isinstance(obj, np.ndarray):
        return [json_safe(v) for v in obj.tolist()]
    if isinstance(obj, (pd.Timestamp, _dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, _dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", "replace")
    try:
        miss = pd.isna(obj)
        if isinstance(miss, (bool, np.bool_)) and bool(miss):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(obj, (complex,)):
        return [obj.real, obj.imag]
    return str(obj)


def json_dumps_safe(obj: Any) -> str:
    return json.dumps(json_safe(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
