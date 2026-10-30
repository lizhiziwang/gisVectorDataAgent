"""算子契约的唯一事实源。

为什么要有这个文件
------------------
之前同一个算子要在五个地方各写一遍：执行函数（engine.py）、给大模型的说明
（agent.TOOL_SPECS）、中文名（TOOL_LABEL）、桌面模板、网页模板、README。
只要有一处忘了改，就会出现「说明写 bbox、实现要 xmin/ymin/xmax/ymax」这类
运行期才暴露的契约漂移。

这里用 `ToolSpec` 把「名字 / 中文名 / 分组 / 参数类型 / 必填 / 枚举 / 默认值 /
模板 / 示例」集中描述，然后由它生成：

· 大模型 prompt 里的算子表（`prompt_specs()`）
· `validate_args()` 的参数校验（执行前就能拒绝非法参数）
· `/api/tools` 与界面里的算子卡片/模板
· README/测试可以引用同一份定义

设计约束
--------
· 不 import engine/agent，避免循环依赖；
· 参数只做「类型/范围/枚举」这种通用校验，图层是否存在、几何类型是否匹配
  由规划器/执行器在拿到 Store 后再检查；
· 校验失败返回中文错误信息，直接可以展示给用户。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 单位别名：模型/用户经常写中文，统一成引擎认的短单位
UNIT_ALIASES = {
    "m": "m", "米": "m", "meter": "m", "meters": "m",
    "km": "km", "公里": "km", "千米": "km", "kilometer": "km", "kilometers": "km",
    "cm": "cm", "厘米": "cm", "mm": "mm", "毫米": "mm",
    "ha": "ha", "公顷": "ha", "ft": "ft", "英尺": "ft",
}

PREDICATES = ("intersects", "contains", "within", "overlaps", "touches", "crosses",
              "covers", "covered_by")
JOIN_HOWS = ("inner", "left", "right")


def _default_prompt(p: "Param") -> str:
    """把参数说明拼成一句给模型看的话。"""
    bits = []
    if p.type != "string":
        bits.append(p.type)
    if p.enum:
        bits.append("可选：" + "/".join(map(str, p.enum)))
    if p.minimum is not None or p.maximum is not None:
        lo = "" if p.minimum is None else str(p.minimum)
        hi = "" if p.maximum is None else str(p.maximum)
        bits.append(f"范围 {lo}~{hi}")
    if p.has_default:
        bits.append(f"默认 {p.default}")
    desc = p.desc or p.name
    return f"{desc}（{'；'.join(bits)}）" if bits else desc


@dataclass(frozen=True)
class Param:
    name: str
    type: str = "string"          # string / number / integer / boolean / array / object
    desc: str = ""
    required: bool = False
    default: Any = None
    has_default: bool = False
    enum: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    aliases: tuple = ()

    def prompt(self) -> str:
        return _default_prompt(self)

    def schema(self) -> dict:
        s: dict = {"type": self.type if self.type not in ("array",) else "array",
                   "description": self.desc or self.name}
        if self.type == "array":
            s["items"] = {"type": "string"}
        if self.enum:
            s["enum"] = list(self.enum)
        if self.minimum is not None:
            s["minimum"] = self.minimum
        if self.maximum is not None:
            s["maximum"] = self.maximum
        if self.has_default:
            s["default"] = self.default
        return s


@dataclass
class ToolSpec:
    name: str
    title: str
    group: str
    desc: str
    params: dict[str, Param] = field(default_factory=dict)
    template: str = ""
    examples: tuple[str, ...] = ()
    output: str = "layer"          # layer / none / table / export

    def prompt_spec(self) -> dict:
        # 同时给 name / tool / args / params：兼容旧代码与模型习惯
        return {"name": self.name, "tool": self.name, "desc": self.desc,
                "args": {k: v.prompt() for k, v in self.params.items()},
                "params": {k: v.prompt() for k, v in self.params.items()}}

    def json_schema(self) -> dict:
        props = {k: v.schema() for k, v in self.params.items()}
        # 所有算子都允许显式命名结果图层
        props.setdefault("name", {"type": "string", "description": "结果图层名（可选）"})
        required = [k for k, v in self.params.items() if v.required]
        return {"type": "object", "properties": props, "required": required,
                "additionalProperties": False}

    def validate(self, args: dict) -> tuple[dict, list[str]]:
        """校验并做类型归一。返回 (清洗后的参数, 错误列表)。"""
        raw = dict(args or {})
        errors: list[str] = []
        out: dict = {}
        known = set(self.params) | {"name"}
        for k in list(raw):
            if k not in known:
                errors.append(f"「{self.title}」不认识参数 `{k}`，可用参数：{'、'.join(sorted(known))}")
                raw.pop(k, None)
        for name, p in self.params.items():
            if name not in raw or raw[name] in (None, ""):
                if p.required:
                    errors.append(f"「{self.title}」缺少必填参数 `{name}`（{p.desc or name}）")
                elif p.has_default:
                    out[name] = p.default
                continue
            v = raw[name]
            try:
                out[name] = _coerce(p, v)
            except ValueError as e:
                errors.append(f"「{self.title}」参数 `{name}` 不合法：{e}")
        if isinstance(raw.get("name"), str) and raw["name"].strip():
            out["name"] = raw["name"].strip()
        # unit 别名归一
        if "unit" in out and isinstance(out["unit"], str):
            u = out["unit"].strip().lower()
            if u in UNIT_ALIASES:
                out["unit"] = UNIT_ALIASES[u]
        return out, errors


def _coerce(p: Param, v: Any) -> Any:
    t = p.type
    if t == "number":
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"需要数字，收到 {v!r}")
        if p.minimum is not None and f < p.minimum:
            raise ValueError(f"不能小于 {p.minimum}")
        if p.maximum is not None and f > p.maximum:
            raise ValueError(f"不能大于 {p.maximum}")
        return f
    if t == "integer":
        try:
            i = int(float(v))
        except (TypeError, ValueError):
            raise ValueError(f"需要整数，收到 {v!r}")
        if p.minimum is not None and i < p.minimum:
            raise ValueError(f"不能小于 {int(p.minimum)}")
        if p.maximum is not None and i > p.maximum:
            raise ValueError(f"不能大于 {int(p.maximum)}")
        return i
    if t == "boolean":
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "y", "是", "对", "需要", "要")
        return bool(v)
    if t == "array":
        if isinstance(v, str):
            return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
        if isinstance(v, (list, tuple, set)):
            return list(v)
        return [v]
    if t == "object":
        if isinstance(v, dict):
            return v
        raise ValueError(f"需要对象，收到 {v!r}")
    s = str(v).strip()
    if p.enum and s not in p.enum:
        # unit 先做别名归一，再判断枚举
        alt = UNIT_ALIASES.get(s.lower()) if p.name == "unit" else None
        if alt in p.enum:
            return alt
        raise ValueError(f"只能是 {'/'.join(map(str, p.enum))} 之一，收到 {v!r}")
    return s


# --------------------------------------------------------------------------- 内置算子
def _p(name: str, type_: str = "string", desc: str = "", required: bool = False,
       default: Any = None, enum: tuple = (), minimum=None, maximum=None) -> Param:
    return Param(name=name, type=type_, desc=desc, required=required,
                 default=default, has_default=default is not None,
                 enum=tuple(enum), minimum=minimum, maximum=maximum)


def _layer(desc: str = "图层名") -> Param:
    return Param(name="layer", type="string", desc=desc, required=True)


def _build_builtins() -> dict[str, ToolSpec]:
    specs = [
        ToolSpec("highlight", "高亮显示", "概览",
                  "在地图上高亮显示要素：不改数据，只画强调样式。"
                  "expression 为空表示高亮全部要素；给了表达式就高亮满足条件的要素",
                  {"layer": _layer(),
                   "expression": _p("expression", "string", "筛选表达式，可为空（高亮全部）", False),
                   "color": _p("color", "string", "高亮颜色", False, "#d97757")},
                  "在地图上高亮显示「{L}」中面积大于 10000 的要素",
                  output="none"),
        ToolSpec("describe", "数据体检", "概览",
                 "数据体检：要素数、几何类型、坐标系、字段清单、总面积/长度",
                 {"layer": _layer()},
                 "检查「{L}」的数据质量", output="none"),
        ToolSpec("buffer", "缓冲区", "几何构造",
                 "缓冲区分析，按距离外扩；dissolve=true 时融合为整体",
                 {"layer": _layer(), "distance": _p("distance", "number", "缓冲距离", True, minimum=0.000001),
                  "unit": _p("unit", "string", "距离单位", False, "m", ("m", "km", "cm", "mm", "ha", "ft")),
                  "dissolve": _p("dissolve", "boolean", "是否融合为整体", False, False),
                  "quad_segs": _p("quad_segs", "integer", "圆弧分段", False, 16, minimum=1, maximum=128)},
                 "对「{L}」做 500 米缓冲区"),
        ToolSpec("clip", "裁剪", "叠加分析",
                 "用面图层裁剪目标图层（叠加裁剪）",
                 {"layer": _layer("被裁剪图层"), "mask": Param("mask", "string", "裁剪面图层", True)},
                 "用「{O}」裁剪「{L}」"),
        ToolSpec("intersect", "求交", "叠加分析",
                 "两图层求交（保留公共部分及双方属性）",
                 {"layer": _layer("图层 A"), "other": Param("other", "string", "图层 B", True)},
                 "「{L}」与「{O}」求交"),
        ToolSpec("union", "叠加合并", "叠加分析",
                 "两图层叠加合并",
                 {"layer": _layer("图层 A"), "other": Param("other", "string", "图层 B", True)},
                 "「{L}」与「{O}」叠加合并"),
        ToolSpec("erase", "擦除", "叠加分析",
                 "从 A 中擦除 B 的范围",
                 {"layer": _layer("图层 A"), "mask": Param("mask", "string", "擦除范围图层 B", True)},
                 "从「{L}」中擦除「{O}」范围"),
        ToolSpec("dissolve", "融合", "整理",
                 "融合：按字段合并同类要素，或整体融合；agg 可指定字段聚合方式",
                 {"layer": _layer(),
                  "by": _p("by", "array", "分组字段，可为空（整体融合）"),
                  "agg": _p("agg", "object", "字段聚合方式，如 {\"人口\":\"sum\"}")},
                 "按字段融合「{L}」"),
        ToolSpec("reproject", "投影转换", "整理",
                 "坐标系转换；epsg 传编号，如 4326/4544",
                 {"layer": _layer(), "epsg": _p("epsg", "string", "目标 EPSG 编号", True)},
                 "把「{L}」投影到 EPSG:4544"),
        ToolSpec("simplify", "几何简化", "整理",
                 "几何简化（抽稀节点，保留拓扑）",
                 {"layer": _layer(), "tolerance": _p("tolerance", "number", "容差", True, minimum=0.000001),
                  "unit": _p("unit", "string", "容差单位", False, "m", ("m", "km", "cm", "mm", "ft"))},
                 "简化「{L}」容差 10 米"),
        ToolSpec("centroid", "要素转点", "几何构造",
                 "面/线转要素中心点（代表点，保证落在要素内部）",
                 {"layer": _layer()}, "取「{L}」的质心"),
        ToolSpec("convex_hull", "凸包", "几何构造",
                 "生成整体凸包",
                 {"layer": _layer()}, "生成「{L}」的凸包"),
        ToolSpec("min_rect", "最小外接矩形", "几何构造",
                 "最小外接矩形面：rotated=最小面积旋转矩形，envelope=轴对齐包围盒；逐要素输出，附带矩形面积/周长",
                 {"layer": _layer(),
                  "mode": _p("mode", "string", "矩形类型", False, "rotated", ("rotated", "envelope"))},
                 "生成「{L}」的最小外接矩形"),
        ToolSpec("shape_metrics", "形状指标", "属性与统计",
                 "新增形状指标字段：面积 area_m2、周长 perim_m、最小外接矩形周长 rect_perim_m、周长比 rect_ratio、圆度 roundness",
                 {"layer": _layer()}, "计算「{L}」的形状指标"),
        ToolSpec("bbox_select", "范围框选", "邻近与连接",
                 "按经纬度范围框选要素（矩形范围过滤）",
                 {"layer": _layer(),
                  "bbox": Param("bbox", "string", "x0,y0,x1,y1（经纬度）", True)},
                 "框选「{L}」经度 104~104.05、纬度 30.58~30.62 的范围"),
        ToolSpec("voronoi", "泰森多边形", "几何构造",
                 "由点生成泰森多边形（Voronoi）",
                 {"layer": _layer("点图层名")}, "对「{L}」生成泰森多边形"),
        ToolSpec("grid", "生成渔网", "几何构造",
                 "生成渔网网格",
                 {"layer": _layer("参考范围图层"),
                  "cell": _p("cell", "number", "网格尺寸", True, minimum=0.000001),
                  "unit": _p("unit", "string", "尺寸单位", False, "m", ("m", "km", "cm", "mm", "ft"))},
                 "在「{L}」范围生成 1 公里渔网"),
        ToolSpec("sjoin", "空间连接", "邻近与连接",
                 "空间连接：把 B 的属性按空间关系挂到 A 上；同名字段会自动加后缀保留",
                 {"layer": _layer("目标 A"), "other": Param("other", "string", "来源 B", True),
                  "predicate": _p("predicate", "string", "空间关系", False, "intersects", PREDICATES),
                  "how": _p("how", "string", "连接方式", False, "inner", JOIN_HOWS)},
                 "把「{O}」的属性空间连接到「{L}」"),
        ToolSpec("nearest", "最近邻", "邻近与连接",
                 "最近邻连接，计算距离；k>1 返回前 k 个最近要素",
                 {"layer": _layer("图层 A"), "other": Param("other", "string", "图层 B", True),
                  "k": _p("k", "integer", "返回数量", False, 1, minimum=1, maximum=20)},
                 "为「{L}」查找最近的「{O}」"),
        ToolSpec("filter", "属性筛选", "属性与统计",
                 "属性筛选，pandas query 表达式",
                 {"layer": _layer(), "expression": _p("expression", "string", "筛选表达式", True)},
                 "筛选「{L}」中面积大于 10000 的要素"),
        ToolSpec("field_calc", "字段计算", "属性与统计",
                 "字段计算/新增；表达式只能用字段名和四则运算",
                 {"layer": _layer(), "field": _p("field", "string", "新字段名", True),
                  "expression": _p("expression", "string", "计算表达式", True)},
                 "在「{L}」新增字段 密度 = 人口 / 面积"),
        ToolSpec("area_stats", "面积统计", "属性与统计",
                 "面积/长度统计，可按字段分组；线图层按长度统计",
                 {"layer": _layer(), "group_by": _p("group_by", "string", "分组字段，可为空")},
                 "按用地类型统计「{L}」的面积", output="table"),
        ToolSpec("merge", "图层合并", "整理",
                 "多图层合并为一个",
                 {"layers": _p("layers", "array", "图层名数组", True)},
                 "合并所有图层"),
        ToolSpec("dedupe", "去重", "整理",
                 "删除重复要素；给了 subset 就按字段判重，否则按几何判重",
                 {"layer": _layer(), "subset": _p("subset", "array", "判重字段数组，可为空"),
                  "by_geometry": _p("by_geometry", "boolean", "是否按几何判重", False, True)},
                 "对「{L}」去重"),
        ToolSpec("fix_geometry", "几何修复", "整理",
                 "修复无效几何、清理空几何",
                 {"layer": _layer()}, "修复「{L}」的无效几何"),
        ToolSpec("explode", "拆分多部件", "整理",
                 "多部件几何拆分为单部件",
                 {"layer": _layer()}, "拆分「{L}」的多部件"),
    ]
    return {s.name: s for s in specs}


REGISTRY: dict[str, ToolSpec] = _build_builtins()
_ORDER = list(REGISTRY)


def get(name: str) -> ToolSpec | None:
    return REGISTRY.get((name or "").strip().lower())


def register(spec: ToolSpec) -> ToolSpec:
    REGISTRY[spec.name] = spec
    if spec.name not in _ORDER:
        _ORDER.append(spec.name)
    return spec


def prompt_specs() -> list[dict]:
    return [REGISTRY[n].prompt_spec() for n in _ORDER if n in REGISTRY]


def label_map() -> dict:
    return {n: REGISTRY[n].title for n in _ORDER if n in REGISTRY}


def template_map() -> dict:
    return {n: REGISTRY[n].template for n in _ORDER if n in REGISTRY and REGISTRY[n].template}


def groups() -> list[tuple[str, list[str]]]:
    order = ["概览", "几何构造", "叠加分析", "邻近与连接", "属性与统计", "整理", "我的算子"]
    out: dict[str, list[str]] = {g: [] for g in order}
    for n in _ORDER:
        s = REGISTRY.get(n)
        if s is None:
            continue
        out.setdefault(s.group, []).append(n)
    return [(g, names) for g, names in out.items() if names]


def validate_args(tool: str, args: dict) -> tuple[dict, list[str]]:
    spec = get(tool)
    if spec is None:
        return {}, [f"不支持的算子：{tool}"]
    return spec.validate(args or {})
