"""工具库的分类清单（左栏「工具库」与 Agent 设置 →「工具」共用）。

分组常量原来写在 native/app.py 里，设置页也要用；放到 app/ 下落一份，
两个界面只负责渲染，不各写一套分类逻辑。顺序即展示顺序。

这里只依赖 `app.agent` 的运行时表（`OPERATIONS` / `TOOL_GROUP`），
不 import 界面层，测试可以直接调。
"""
from __future__ import annotations

from . import tool_prefs
from .tool_prefs import is_enabled

#: 一级「矢量」下的功能分组。自定义矢量工具按 TOOL_GROUP 归位。
TOOL_GROUPS = [
    ("概览", ["describe", "highlight"]),
    ("几何构造", ["buffer", "centroid", "convex_hull", "min_rect", "voronoi", "grid"]),
    ("叠加分析", ["clip", "intersect", "union", "erase"]),
    ("邻近与连接", ["sjoin", "nearest", "bbox_select"]),
    ("属性与统计", ["filter", "field_calc", "area_stats", "shape_metrics"]),
    ("整理", ["dissolve", "merge", "dedupe", "simplify", "reproject",
              "fix_geometry", "explode"]),
]

#: 一级「栅格」下的功能分组。栅格算子不和矢量混在一个平铺列表里。
RASTER_TOOL_GROUPS = [
    ("概览", ["raster_info"]),
    ("处理", ["raster_clip", "raster_reproject", "raster_resample", "raster_merge"]),
    ("分析", ["raster_calc", "zonal_stats"]),
    ("转换", ["raster_to_vector"]),
]


def is_custom(name: str) -> bool:
    """是不是自定义算子（生成后落盘的那些）。

    靠函数名 `custom_` 前缀判断，和工具库原来的口径一致：自定义算子的
    `OPERATIONS[name]` 是 `make_custom_operator()` 造出来的包装函数。
    """
    from .agent import OPERATIONS
    op = OPERATIONS.get(name)
    return bool(op is not None and getattr(op, "__name__", "").startswith("custom_"))


def tool_tree(include_disabled: bool = False) -> list[tuple[str, list[tuple[str, list[str]]]]]:
    """返回两级工具分类：[("矢量", [("概览", [...]), ...]), ("栅格", [...])]。

    ``include_disabled=False``（默认，给左栏工具库用）时按运行时状态过滤掉
    停用的工具；设置页要显示所有工具（包括停用的，好让用户重新勾上），
    传 True。

    一级按数据类型，二级按功能。自定义工具都是矢量算子，按它新建时选的
    功能分组归位；分组不合法 / 选成栅格分组的，落到「我的工具」。
    """
    from .agent import OPERATIONS, TOOL_GROUP
    from .tool_registry import CUSTOM_GROUP

    def keep(name: str) -> bool:
        return include_disabled or is_enabled(name)

    vector: list[tuple[str, list[str]]] = []
    for title, names in TOOL_GROUPS:
        present = [n for n in names if n in OPERATIONS and keep(n)]
        if present:
            vector.append((title, present))
    grouped = {n for _, names in vector for n in names}

    custom = [n for n in OPERATIONS
              if n not in grouped and is_custom(n) and keep(n)]
    names_of_group = dict(TOOL_GROUPS)
    # 用户「＋ 新建分组…」建的分组：id 就是组名，和内置分组共用一套归位逻辑
    user_groups = tool_prefs.custom_groups()
    by_group: dict[str, list[str]] = {}
    mine: list[str] = []
    for n in custom:
        g = (TOOL_GROUP.get(n) or "").strip()
        if g and (g in names_of_group or g in user_groups) and n not in names_of_group.get(g, ()):
            by_group.setdefault(g, []).append(n)
        else:
            mine.append(n)
    if by_group:
        merged = {title: list(names) for title, names in vector}
        for title, names in by_group.items():
            if title in names_of_group or title in user_groups:
                merged.setdefault(title, []).extend(names)
        vector = [(title, names) for title, names in merged.items() if names]
    # 用户分组按创建顺序排在「我的工具」前面
    for g in user_groups:
        if by_group.get(g):
            vector.append((g, by_group[g]))
    if mine:
        vector.append((CUSTOM_GROUP, mine))

    raster: list[tuple[str, list[str]]] = []
    raster_names: set[str] = set()
    for title, names in RASTER_TOOL_GROUPS:
        present = [n for n in names if n in OPERATIONS and keep(n)]
        if present:
            raster.append((title, present))
            raster_names.update(present)
    # 以后新增的栅格算子即使忘了登记到 RASTER_TOOL_GROUPS，也不会掉进矢量组。
    extra_raster = [n for n in OPERATIONS
                    if (n.startswith("raster_") or n == "zonal_stats")
                    and n not in raster_names and keep(n)]
    if extra_raster:
        raster.append(("其它", extra_raster))
        raster_names.update(extra_raster)

    other = [n for n in OPERATIONS
             if n not in grouped and n not in custom and n not in raster_names
             and keep(n)]
    if other:
        vector.append(("其它", other))

    tree = [("矢量", vector), ("栅格", raster)]
    return [(kind, sections) for kind, sections in tree if sections]
