"""Web Mercator 投影（矢量管线与栅格管线共用）。

单独拆出来是为了让 `rasterview` 能复用同一套坐标变换——
两条管线必须落在**完全相同的世界坐标系**里，否则栅格和矢量叠加会错位。
如果各写一份 `project()`，将来改一处忘另一处，症状是「栅格比矢量偏了半个屏幕」，
而且极难查。
"""
from __future__ import annotations

import math


def project(lon: float, lat: float) -> tuple[float, float]:
    """经纬度 → 归一化 Web Mercator [0,1]。"""
    x = (lon + 180.0) / 360.0
    s = math.sin(max(-85.05, min(85.05, lat)) * math.pi / 180.0)
    y = 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)
    return x, y


def unproject(x: float, y: float) -> tuple[float, float]:
    """归一化 Web Mercator → 经纬度。"""
    lon = x * 360.0 - 180.0
    n = math.pi - 2 * math.pi * y
    lat = (180.0 / math.pi) * math.atan(0.5 * (math.exp(n) - math.exp(-n)))
    return lon, lat
