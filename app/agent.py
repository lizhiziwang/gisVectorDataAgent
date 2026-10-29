"""Agent 规划层：把自然语言指令解析成可执行的 GIS 算子链。

两级规划：
1. rule  —— 内置规则解析器，离线可用，覆盖中英文常用表达（默认）；
2. llm   —— 配置了任意 OpenAI 兼容接口（DeepSeek/通义/GLM/OpenAI…）后启用，
            由大模型输出结构化 JSON 计划，规则解析作为兜底。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
import urllib.error
from typing import Any

from .engine import OPERATIONS, Store, Result

# --------------------------------------------------------------------------- 工具说明
TOOL_SPECS = [
    {"name": "describe", "desc": "数据体检：要素数、几何类型、坐标系、字段清单、总面积/长度",
     "params": {"layer": "图层名"}},
    {"name": "buffer", "desc": "缓冲区分析，按距离外扩",
     "params": {"layer": "图层名", "distance": "数字", "unit": "m/km", "dissolve": "是否融合为整体 bool", "quad_segs": "圆弧分段，默认16"}},
    {"name": "clip", "desc": "用面图层裁剪目标图层（叠加裁剪）",
     "params": {"layer": "被裁剪图层", "mask": "裁剪面图层"}},
    {"name": "intersect", "desc": "两图层求交（保留公共部分及双方属性）",
     "params": {"layer": "图层A", "other": "图层B"}},
    {"name": "union", "desc": "两图层叠加合并",
     "params": {"layer": "A", "other": "B"}},
    {"name": "erase", "desc": "从A中擦除B的范围",
     "params": {"layer": "A", "mask": "B"}},
    {"name": "dissolve", "desc": "融合：按字段合并同类要素，或整体融合",
     "params": {"layer": "图层名", "by": "字段名或数组，可为空"}},
    {"name": "reproject", "desc": "坐标系转换",
     "params": {"layer": "图层名", "epsg": "EPSG 编号，如 4326/4547"}},
    {"name": "simplify", "desc": "几何简化（抽稀节点，保留拓扑）",
     "params": {"layer": "图层名", "tolerance": "数字", "unit": "m"}},
    {"name": "centroid", "desc": "面/线转要素中心点",
     "params": {"layer": "图层名"}},
    {"name": "convex_hull", "desc": "生成整体凸包",
     "params": {"layer": "图层名"}},
    {"name": "min_rect", "desc": "最小外接矩形面：rotated=最小面积旋转矩形，envelope=轴对齐包围盒；逐要素输出，附带矩形面积/周长",
     "params": {"layer": "图层名", "mode": "rotated 或 envelope，默认 rotated"}},
    {"name": "shape_metrics", "desc": "新增形状指标字段：面积 area_m2、周长 perim_m、最小外接矩形周长 rect_perim_m、周长比 rect_ratio、圆度 roundness",
     "params": {"layer": "图层名"}},
    {"name": "bbox_select", "desc": "按经纬度范围框选要素（矩形范围过滤）",
     "params": {"layer": "图层名", "bbox": "x0,y0,x1,y1（经纬度）", "name": "可选，结果图层名"}},
    {"name": "voronoi", "desc": "由点生成泰森多边形（Voronoi）",
     "params": {"layer": "点图层名"}},
    {"name": "grid", "desc": "生成渔网网格",
     "params": {"layer": "参考范围图层", "cell": "数字", "unit": "m/km"}},
    {"name": "sjoin", "desc": "空间连接：把B的属性按空间关系挂到A上",
     "params": {"layer": "目标A", "other": "来源B", "predicate": "intersects/contains/within/overlaps", "how": "inner/left"}},
    {"name": "nearest", "desc": "最近邻连接，计算距离",
     "params": {"layer": "A", "other": "B", "k": "数量，默认1"}},
    {"name": "filter", "desc": "属性筛选，pandas query 表达式",
     "params": {"layer": "图层名", "expression": "如 人口 > 10000 and 类型 == '住宅'"}},
    {"name": "field_calc", "desc": "字段计算/新增",
     "params": {"layer": "图层名", "field": "新字段名", "expression": "表达式"}},
    {"name": "area_stats", "desc": "面积/长度统计，可按字段分组",
     "params": {"layer": "图层名", "group_by": "字段名，可为空"}},
    {"name": "merge", "desc": "多图层合并为一个",
     "params": {"layers": "图层名数组"}},
    {"name": "dedupe", "desc": "删除重复要素",
     "params": {"layer": "图层名", "subset": "判重字段数组，可为空（按几何判重）"}},
    {"name": "fix_geometry", "desc": "修复无效几何、清理空几何",
     "params": {"layer": "图层名"}},
    {"name": "explode", "desc": "多部件几何拆分为单部件",
     "params": {"layer": "图层名"}},
]

TOOL_LABEL = {
    "describe": "数据体检", "buffer": "缓冲区", "clip": "裁剪", "intersect": "求交",
    "union": "叠加合并", "erase": "擦除", "dissolve": "融合", "reproject": "投影转换",
    "simplify": "几何简化", "centroid": "要素转点", "convex_hull": "凸包", "voronoi": "泰森多边形",
    "min_rect": "最小外接矩形", "shape_metrics": "形状指标",
    "grid": "生成渔网", "sjoin": "空间连接", "nearest": "最近邻", "filter": "属性筛选",
    "field_calc": "字段计算", "area_stats": "面积统计", "merge": "图层合并", "dedupe": "去重",
    "fix_geometry": "几何修复", "explode": "拆分多部件", "bbox_select": "范围框选",
}

def register_tool_spec(name: str, title: str, desc: str, params: dict | None = None) -> None:
    """把自定义算子登记进工具表（大模型提示词 + 中文名）。

    注册后它和内置算子完全同权：模型能规划它，界面算子库里也点得到。
    """
    if not name:
        return
    TOOL_LABEL[name] = title or name
    for s in TOOL_SPECS:
        if s.get("name") == name:
            s["desc"] = desc or s.get("desc", "")
            s["params"] = params or s.get("params") or {"layer": "图层名"}
            return
    TOOL_SPECS.append({"name": name, "desc": desc or "自定义算子",
                       "params": params or {"layer": "图层名"}})


NUM = r"(\d+(?:\.\d+)?)"
UNIT = r"\s*(公里|千米|km|KM|Km|米|m|M|厘米|cm|mm|毫米|公顷|ha)?"
UNIT_MAP = {"公里": "km", "千米": "km", "km": "km", "KM": "km", "Km": "km",
            "米": "m", "m": "m", "M": "m", "厘米": "cm", "cm": "cm",
            "mm": "mm", "毫米": "mm", "公顷": "ha", "ha": "ha"}


def _unit(u: str | None) -> str:
    if not u:
        return "m"
    return UNIT_MAP.get(u, "m")


def _split_clauses(text: str) -> list[str]:
    text = text.strip()
    parts = re.split(r"[。；;\n]|，(?=[^\d])|,\s*(?=[^\d])|然后|接着|之后|随后|再(?=[^\s])", text)
    out = [p.strip(" ，,、") for p in parts if p and p.strip(" ，,、")]
    return out or [text]


# --- 闲聊识别 ---------------------------------------------------------------
# 用户说「你好」不应该被当成一条解析失败的指令。这里先兜住纯对话，
# 只有「没有 GIS 关键词 + 整体就是一句寒暄」才走这条路，
# 「你好，帮我做个缓冲区」这种带需求的话仍然照常进规划器。

_PUNCT = set(" \t\r\n，。！？、,.!?~～…:：;；\"'“”‘’()（）[]【】<>《》")

# 只要出现这些词，就认为用户在提需求，不该当闲聊拦掉
_GIS_WORDS = (
    "缓冲", "裁剪", "融合", "相交", "叠加", "擦除", "联合", "统计", "面积", "长度",
    "简化", "网格", "渔网", "最近", "合并", "凸包", "泰森", "去重", "字段", "筛选",
    "导出", "投影", "质心", "转点", "拆分", "修复", "包络", "范围", "距离", "坐标",
    "要素", "图层", "地块", "道路", "水系", "建筑", "边界", "行政区",
    "矩形", "周长", "指标", "圆度", "外接",
)


def _norm_chat(text: str) -> str:
    return "".join(ch for ch in (text or "").strip() if ch not in _PUNCT).lower()


def _layer_brief(store, limit: int = 3) -> tuple[int, str, int]:
    """（图层数, 「A」「B」…, 总要素数）。"""
    layers = list(store.layers.values())
    names = [l.name for l in layers]
    brief = "、".join(f"「{n}」" for n in names[:limit])
    if len(names) > limit:
        brief += " 等"
    total = sum(len(l.gdf) for l in layers)
    return len(names), brief, total


def _iter_json_objects(text: str):
    """按括号配对扫出文本里的每个 JSON 对象（跳过字符串内的括号）。

    比 `re.search(r"\\{.*\\}")` 稳：模型常在 JSON 前后夹一句解释、
    或者同时给出两个对象，贪婪正则会把它们连一起导致解析失败。
    """
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:i + 1]
                    start = -1


def _parse_json_payload(text: str) -> dict | None:
    """从模型输出里抠出可用的 JSON 对象。"""
    raw = (text or "").strip()
    raw = re.sub(r"```[a-zA-Z]*", "", raw)          # 去掉 ```json / ``` 围栏
    raw = raw.replace("“", '"').replace("”", '"')
    for cand in _iter_json_objects(raw):
        for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                obj = json.loads(attempt)
            except Exception:
                continue
            if isinstance(obj, dict):
                return obj
            if isinstance(obj, list):
                return {"plan": obj}
    return None


# 模型可能用的字段名（不同模型习惯不同，全收）
_TOOL_KEYS = ("tool", "op", "name", "action", "operator", "func", "type")
_ARG_KEYS = ("args", "params", "arguments", "parameters", "options", "kwargs")
_TOOL_ALIAS = {
    "buffer_analysis": "buffer", "bufferanalysis": "buffer", "缓冲": "buffer",
    "attribute_filter": "filter", "attr_filter": "filter", "where": "filter",
    "spatial_join": "sjoin", "spatialjoin": "sjoin", "join": "sjoin",
    "intersection": "intersect", "intersects": "intersect",
    "difference": "erase", "clip_layer": "clip",
    "centroids": "centroid", "centroid_point": "centroid",
    "dissolve_layer": "dissolve", "convexhull": "convex_hull",
    "fix": "fix_geometry", "repair": "fix_geometry", "make_valid": "fix_geometry",
    "explode_multi": "explode", "deduplicate": "dedupe", "export_data": "export",
    "minimum_rotated_rectangle": "min_rect", "bounding_box": "min_rect", "envelope": "min_rect",
    "mrr": "min_rect", "shape_index": "shape_metrics", "metrics": "shape_metrics",
}


def normalize_plan(parsed: dict) -> tuple[list[dict], str]:
    """把模型给的步骤整理成 {tool, args} —— 这是执行器唯一认的格式。

    实测 DeepSeek 会用 `{"op": ..., "params": {...}}`，
    Gemini/GLM 之类偶尔用 `name`/`arguments`，甚至有把参数摊平在顶层的。
    这些都在这里归一，别让「模型答对了但字段名不对」白跑一趟。
    """
    if not isinstance(parsed, dict):
        return [], ""
    raw_steps = parsed.get("plan") or parsed.get("steps") or parsed.get("actions") or []
    if isinstance(raw_steps, dict):
        raw_steps = [raw_steps]
    reply = (parsed.get("reply") or parsed.get("message") or parsed.get("note")
             or parsed.get("answer") or "")
    out: list[dict] = []
    for st in raw_steps:
        if not isinstance(st, dict):
            continue
        tool = ""
        for k in _TOOL_KEYS:
            v = st.get(k)
            if isinstance(v, str) and v.strip():
                tool = v.strip()
                break
        if not tool:
            continue
        tool = _TOOL_ALIAS.get(tool.lower().replace("-", "_").replace(" ", "_"), tool.lower())
        args: dict = {}
        for k in _ARG_KEYS:
            v = st.get(k)
            if isinstance(v, dict):
                args = dict(v)
                break
        if not args:
            args = {k: v for k, v in st.items() if k not in _TOOL_KEYS + _ARG_KEYS}
        # 常见别名对齐执行器参数名
        if "layer_name" in args and "layer" not in args:
            args["layer"] = args.pop("layer_name")
        if "layerName" in args and "layer" not in args:
            args["layer"] = args.pop("layerName")
        if "where" in args and "expression" not in args:
            args["expression"] = args.pop("where")
        if "condition" in args and "expression" not in args:
            args["expression"] = args.pop("condition")
        if "radius" in args and "distance" not in args:
            args["distance"] = args.pop("radius")
        if tool == "buffer" and "distance" in args and "unit" not in args:
            args["unit"] = "m"
        out.append({"tool": tool, "args": args})
    return out, str(reply or "")


class Planner:
    def __init__(self, store: Store):
        self.store = store
        self._llm_fail_at = 0.0   # 上次大模型连接失败时间，2 分钟内不再重试

    # ---------------- 上下文
    def chit_chat(self, text: str, has_data: bool) -> str | None:
        """纯对话（问候 / 道谢 / 问能做什么 / 告别）→ 直接回一句人话。

        返回 None 表示「这不是闲聊」，交给规划器去解析。
        判据两条同时满足才认：① 整句就是一句寒暄；② 没有任何 GIS 关键词。
        这样「你好，帮我按类型统计面积」不会被误拦。
        """
        if not text:
            return None
        if any(w in text for w in _GIS_WORDS):
            return None
        s = _norm_chat(text)
        if not s or len(s) > 30:
            return None

        if re.fullmatch(r"(你好|您好|hi|hello|hey|嗨|哈喽|哈啰|在吗|在么|在不在)[呀啊哈呢嘛吧]*", s):
            if not has_data:
                return ("你好！我是空间分析助手。\n"
                        "先把矢量数据拖进来（shp 压缩包 / GeoJSON / GPKG / KML / CSV），"
                        "或者点右上角「示例数据」先试一遍。")
            n, brief, total = _layer_brief(self.store)
            return (f"你好！当前有 {n} 个图层（{brief}，共 {total:,} 要素）。\n"
                    "直接说需求就行，比如：\n"
                    "· 对「地块」做 500 米缓冲区\n"
                    "· 用「行政区」裁剪「地块」\n"
                    "· 按「类型」统计面积")

        if re.fullmatch(r"(谢谢|多谢|感谢|谢了|thanks|thankyou|thx|辛苦了?)[啦了哈你您]*", s):
            return "不客气。还想处理哪一步，直接说就行。"

        if re.fullmatch(r"(再见|拜拜|byebye|bye|晚安|下次聊)[啦了哈]*", s):
            return "好，随时叫我。"

        if re.fullmatch(r"(好的?|ok|okk|嗯+|收到|了解|明白了?|行|可以)[的啦了]*", s):
            return "好。需要我做什么，直接说需求。"

        if re.search(r"(你是谁|你叫什么|你是什么|你能做什么|你会什么|能干什么|能做啥|"
                     r"有什么功能|有哪些功能|怎么用|如何使用|使用说明|帮助|help|教程|介绍下自己)", s):
            n, brief, total = _layer_brief(self.store)
            body = ("我是空间分析助手：把中文需求翻译成空间算子，跑完后生成新图层。\n"
                    "常用的有：\n"
                    "· 缓冲区 / 裁剪 / 相交 / 融合 / 联合 / 擦除\n"
                    "· 面积、长度、分组统计\n"
                    "· 最近邻、凸包、质心、几何简化、渔网聚合\n"
                    "· 字段筛选、批量导出 GeoJSON / GPKG / Shapefile / CSV\n\n"
                    "原始图层只读不改，每一步的结果都是新图层。")
            if has_data:
                return body + f"\n\n当前可用图层：{brief}（共 {total:,} 要素）。"
            return body + "\n\n现在还没有数据，先把文件拖进来或点右上角「示例数据」。"

        return None

    def _mentioned(self, text: str) -> list[tuple[int, str]]:
        """按出现位置返回消息中提到的图层 (位置, 图层id)，长名优先。"""
        hits: list[tuple[int, str]] = []
        low = text.lower()
        for lay in sorted(self.store.layers.values(), key=lambda l: -len(l.name)):
            idx = low.find(lay.name.lower())
            if idx >= 0:
                hits.append((idx, lay.id))
        hits.sort()
        # 去掉重叠命名（子串被长名覆盖）
        ids, seen = [], set()
        for pos, lid in hits:
            if lid in seen:
                continue
            seen.add(lid)
            ids.append(lid)
        return ids

    def _resolve_target(self, ids: list[str], current: str | None) -> str | None:
        if ids:
            return ids[0]
        lay = self.store.get(current)
        if lay is not None:
            return lay.id
        if len(self.store.layers) == 1:
            return next(iter(self.store.layers))
        # 多个图层且未指定：取最近创建的一个，避免直接失败
        if self.store.layers:
            return list(self.store.layers)[-1]
        return None

    # ---------------- 规则解析
    # ---------------- 自定义算子生成
    @staticmethod
    def wants_new_operator(text: str) -> bool:
        """判断用户是不是在要求「造一个新算子」而不是用现成算子。"""
        if not text:
            return False
        t = text.strip()
        pats = (
            r"(写|做|造|生成|新建|新增|创建|实现|定义|加|添加)\s*(一|一个|个)?\s*(新)?\s*(的)?\s*(算子|operator)",
            r"自定义算子", r"自定义一个?(算子|工具|函数)",
            r"(帮我|给我)?(实现|写)(一)?个?(算法|函数|工具)",
            r"(把|将).{0,30}(写成|做成|封装成).{0,10}(算子|工具)",
        )
        return any(re.search(p, t, re.I) for p in pats)

    @staticmethod
    def extract_operator_brief(text: str) -> str:
        """从「写一个算子：xxx」里把算法描述抠出来，抠不到就用整句。"""
        t = (text or "").strip()
        m = re.search(r"[:：]\s*(.+)$", t, re.S)
        if m and len(m.group(1).strip()) >= 4:
            return m.group(1).strip()
        m = re.search(r"(?:算子|operator)\s*(?:叫|名为|名字是)?\s*[「\"'“]?([^」\"'”]{2,20})[」\"'”]?", t)
        return t

    def _schema_text(self, lay) -> str:
        """给模型看的数据说明（字段名 + 类型 + 几何 + 坐标系）。"""
        if lay is None:
            return "（当前没有加载图层）"
        m = lay.meta()
        try:
            fields = []
            for c in lay.gdf.columns:
                if c == lay.gdf.geometry.name:
                    continue
                fields.append(f"{c}({lay.gdf[c].dtype})")
        except Exception:
            fields = list(m.get("fields") or [])
        return (f"图层「{lay.name}」：几何 {m['geomType']}，坐标系 {m['crs']}，"
                f"{m['count']:,} 个要素；字段：{', '.join(fields[:30]) or '无'}")

    def gen_operator(self, description: str, current: str | None, cfg: dict) -> dict:
        """按算法描述生成一个算子：返回 {spec} 或 {error}。"""
        if not cfg.get("api_key") or not cfg.get("base_url"):
            return {"error": "生成算子需要接入大模型。先在「模型设置」里填上 API Key 和接口地址。"}
        lay = self.store.get(current) or next(iter(self.store.layers.values()), None)
        schema = self._schema_text(lay)
        sys_prompt = (
            "你是 GIS 算法工程师。用户用中文描述一个空间分析算法，你把它写成这个工具里的一个算子。\n"
            f"当前数据：{schema}\n"
            "算子契约（必须严格遵守）：\n"
            "def op(gdf, args):\n"
            "    # gdf: GeoDataFrame，可能已经是投影坐标系；\n"
            "    #      要算长度/面积先用 metric(gdf) 转成米制，算完用 to_wgs(结果, gdf) 转回原坐标系。\n"
            "    # args: dict，键与 params 一致，取值用 args.get(\"名\", 默认值)；\n"
            "    #      参数名用 other/mask/layer2 时，引擎会自动把它换成另一个图层的 GeoDataFrame，\n"
            "    #      可用来写「每个面内道路总长」这类跨图层算子。\n"
            "    # 返回 (新的 GeoDataFrame 或 None, 一句话中文说明)。\n"
            "可用：pd、np、gpd、shapely、metric(gdf)、to_wgs(gdf, like)。\n"
            "禁止：import、while 循环、文件/网络/系统调用、访问 __ 开头的属性。\n"
            "要求：\n"
            "1. 用向量化写法（shapely 数组接口、geopandas 列运算），不要逐要素 for 循环做大计算；\n"
            "2. 返回的 GeoDataFrame 要保留几何列，坐标系与入参一致；\n"
            "3. 结果为空时返回 (None, 说明) 或空图层并在说明里讲清楚，不要抛异常；\n"
            "4. 参数尽量少，默认值写进 args.get 的第二个参数。\n"
            "只输出 JSON：{\"name\":\"英文蛇形名\",\"title\":\"中文名(不超过8字)\","
            "\"desc\":\"一句话说明\",\"params\":{\"参数名\":\"说明\"},"
            "\"code\":\"完整的 python 代码\"}"
        )
        msgs = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": description}]
        out = self._call_llm(cfg, msgs, json_mode=True)
        if isinstance(out, dict):
            return {"error": f"大模型接口调用失败：{out.get('__error__', '')}"}
        content, finish = out
        parsed = _parse_json_payload(content)
        if not parsed or not (parsed.get("code") or "").strip():
            return {"error": "模型没有给出可用的算子代码。", "raw": (content or "")[:400]}
        spec = {
            "name": parsed.get("name") or parsed.get("title") or "custom_op",
            "title": parsed.get("title") or parsed.get("name") or "自定义算子",
            "desc": parsed.get("desc") or "",
            "params": parsed.get("params") or {"layer": "图层名"},
            "code": parsed.get("code") or "",
        }
        from . import custom_ops
        try:
            custom_ops.check_code(spec["code"])
        except custom_ops.OperatorError as e:
            return {"error": f"生成的代码没通过安全检查：{e}", "spec": spec}
        # 拿前若干个要素试跑一次，把可能的运行期错误提前暴露
        try:
            if lay is not None and len(lay.gdf):
                sample = lay.gdf.head(200)
                o, msg = custom_ops.run_op(spec, sample, {})
                spec["_trial"] = {"ok": True, "msg": msg or "",
                                  "rows": 0 if o is None else len(o)}
        except Exception as e:
            spec["_trial"] = {"ok": False, "msg": str(e)}
        return {"spec": spec}

    def rule_plan(self, text: str, current: str | None) -> list[dict]:
        """按分句解析，并把上一句产出自动接入下一句（流水线串联）。"""
        steps: list[dict] = []
        prev = False
        for clause in _split_clauses(text):
            ids = self._mentioned(clause)
            override = "$prev" if (not ids and prev) else None
            sub = self._parse_clause(clause, current, override)
            for s in sub:
                if s.get("tool") not in ("describe", "area_stats", "__export__"):
                    prev = True
            steps.extend(sub)
        return steps

    def _parse_clause(self, c: str, current: str | None, override: str | None = None) -> list[dict]:
        ids = self._mentioned(c)
        tgt = override or self._resolve_target(ids, current)
        low = c.lower()

        def S(tool: str, **args) -> list[dict]:
            return [{"tool": tool, "args": args}]

        # ---- 数据体检
        if re.search(r"体检|检查|看一下|看看|介绍|是什么数据|字段有哪些|坐标系|属性表|概况|描述", c):
            if not re.search(r"修复", c):
                return S("describe", layer=tgt)

        # ---- 投影转换
        m = re.search(r"(?:epsg\s*[:：]?\s*)(\d{4,5})", low)
        if m and re.search(r"转换|投影|转成|转为|变成|reproject", c):
            return S("reproject", layer=tgt, epsg=m.group(1))
        m = re.search(r"(?:转换|投影|转成|转为|变成)(?:到|为|成)?\s*(CGCS\s*2000|WGS\s*84|wgs84|cgcs2000)", c, re.I)
        if m:
            code = "4547" if "2000" in m.group(1) else "4326"
            return S("reproject", layer=tgt, epsg=code)

        # ---- 泰森
        if re.search(r"泰森|voronoi|势力范围|服务区", low):
            return S("voronoi", layer=tgt)

        # ---- 形状指标（要在「外接矩形」前面判断，否则「周长比」会被抢走）
        if re.search(r"周长比|形状指标|紧致|紧凑|圆度|shape_?metric", low):
            return S("shape_metrics", layer=tgt)

        # ---- 最小外接矩形 / 轴对齐包围盒
        if re.search(r"最小外接矩形|最小面积矩形|旋转矩形|min(?:imum)?[-_ ]?rotated", low):
            return S("min_rect", layer=tgt, mode="rotated")
        if re.search(r"外接矩形|包围盒|外包框|包络框|bounding[-_ ]?box|envelope", low):
            return S("min_rect", layer=tgt, mode="envelope")

        # ---- 凸包
        if re.search(r"凸包|外包络|hull", low):
            return S("convex_hull", layer=tgt)

        # ---- 质心
        if re.search(r"质心|中心点|代表点|转点|centroid", low):
            return S("centroid", layer=tgt)

        # ---- 修复
        if re.search(r"修复|拓扑错误|无效几何|make_?valid|自相交", low):
            return S("fix_geometry", layer=tgt)

        # ---- 拆分多部件
        if re.search(r"炸开|拆分|多部件|单部件|explode", low):
            return S("explode", layer=tgt)

        # ---- 去重
        if re.search(r"去重|重复要素|删除重复|dedup", low):
            return S("dedupe", layer=tgt)

        # ---- 简化
        m = re.search(r"(?:简化|抽稀|simplify)[^\d]{0,6}" + NUM + UNIT, c)
        if m:
            return S("simplify", layer=tgt, tolerance=float(m.group(1)), unit=_unit(m.group(2)))

        # ---- 渔网
        m = re.search(r"(?:渔网|网格|grid)[^\d]{0,8}" + NUM + UNIT, c) or \
            re.search(NUM + UNIT + r"[^\d]{0,6}(?:的)?(?:渔网|网格)", c)
        if m:
            return S("grid", layer=tgt, cell=float(m.group(1)), unit=_unit(m.group(2)))

        # ---- 最近邻
        if re.search(r"最近|就近|nearest", low):
            other = ids[1] if len(ids) > 1 else None
            if not other:
                rest = [l for l in self.store.layers if l != tgt]
                other = rest[0].id if rest else None
            return S("nearest", layer=tgt, other=other, k=1)

        # ---- 空间连接
        if re.search(r"空间连接|空间关联|挂接|关联|sjoin|连接属性", low):
            other = ids[1] if len(ids) > 1 else None
            pred = "intersects"
            if "包含" in c or "落在" in c:
                pred = "within"
            if "包含" in c and "被" not in c:
                pred = "contains"
            return S("sjoin", layer=tgt, other=other, predicate=pred, how="left" if "左" in c else "inner")

        # ---- 擦除
        if re.search(r"擦除|扣除|挖掉|减去|去掉.{0,6}范围|erase", low):
            mask = ids[1] if len(ids) > 1 else None
            return S("erase", layer=tgt, mask=mask)

        # ---- 裁剪
        if re.search(r"裁剪|裁切|clip", low):
            if len(ids) >= 2:
                first, second = ids[0], ids[1]
                mask = first if re.search(r"(用|以|按)", c) else second
                target = second if re.search(r"(用|以|按)", c) else first
                return S("clip", layer=target, mask=mask)
            if len(ids) == 1:
                if re.search(r"(用|以|按)", c) and tgt == ids[0]:
                    return S("clip", layer=current, mask=ids[0])
                rest = [l.id for l in self.store.layers.values() if l.id != tgt]
                return S("clip", layer=tgt, mask=rest[0] if rest else None)
            return S("clip", layer=tgt, mask=None)

        # ---- 求交
        if re.search(r"相交|交集|求交|intersect|公共部分", low):
            return S("intersect", layer=tgt, other=ids[1] if len(ids) > 1 else None)

        # ---- 叠加合并（union）
        if re.search(r"叠加|union|联合", low):
            return S("union", layer=tgt, other=ids[1] if len(ids) > 1 else None)

        # ---- 融合
        if re.search(r"融合|dissolve|合并同类|归并|按.{1,10}(汇总|合并)", low):
            by = None
            for f in self._fields_of(tgt):
                if f and f in c:
                    by = f
                    break
            return S("dissolve", layer=tgt, by=by)

        # ---- 多图层合并
        if re.search(r"合并|拼合|合到一起|merge|拼接|追加", low):
            return S("merge", layers=ids or ["all"])

        # ---- 属性筛选（必须早于"面积统计"，否则"筛选面积大于X"会被误判）
        if re.search(r"筛选|过滤|选出|找出|只要|保留|查询|filter", low):
            expr = self._build_expr(c, tgt)
            if expr:
                return S("filter", layer=tgt, expression=expr)

        # ---- 面积/长度统计
        if re.search(r"面积|长度|统计|汇总|多大|多少公顷|平方公里|km2|km²", low):
            by = None
            m = re.search(r"(?:按|按照|分|各|每个)\s*([\u4e00-\u9fa5A-Za-z_]{1,15}?)\s*(?:字段)?\s*(?:分组|分类|统计|汇总|的)", c)
            if m:
                by = m.group(1)
            else:
                cand = [f for f in self._fields_of(tgt) if f and f in c]
                if cand:
                    cand.sort(key=len, reverse=True)
                    by = cand[0]
            return S("area_stats", layer=tgt, group_by=by)

        # ---- 字段计算
        m = re.search(r"(?:新增|计算|添加)(?:字段)?\s*([\u4e00-\u9fa5A-Za-z_]\w{0,15})\s*(?:=|为|等于)\s*(.+)", c)
        if m and re.search(r"字段|计算", c):
            return S("field_calc", layer=tgt, field=m.group(1), expression=m.group(2).strip(" ，,。"))

        # ---- 属性筛选
        if re.search(r"筛选|过滤|选出|找出|只要|保留|查询|filter", low):
            expr = self._build_expr(c, tgt)
            if expr:
                return S("filter", layer=tgt, expression=expr)

        # ---- 缓冲区（放最后，避免误判）
        m = re.search(r"(?:缓冲区|缓冲|buffer|外扩|扩展)[^\d]{0,8}" + NUM + UNIT, c) or \
            re.search(NUM + UNIT + r"[^\d]{0,6}(?:的)?(?:缓冲区|缓冲)", c)
        if m:
            dist = float(m.group(1))
            unit = _unit(m.group(2))
            if unit == "m" and dist >= 1000 and re.search(r"公里|千米|km", c, re.I):
                unit = "km"
            dis = bool(re.search(r"融合|合并|整体|一起|union", c))
            return S("buffer", layer=tgt, distance=dist, unit=unit, dissolve=dis, quad_segs=16)

        # ---- 纯导出意图
        if re.search(r"导出|下载|另存|保存为|存成", low):
            fmt = "geojson"
            for k, v in {"shp": "shp", "shapefile": "shp", "gpkg": "gpkg", "geojson": "geojson", "csv": "csv", "kml": "kml"}.items():
                if k in low:
                    fmt = v
                    break
            return [{"tool": "__export__", "args": {"layer": tgt, "format": fmt}}]

        return []

    def _fields_of(self, lid: str | None) -> list[str]:
        lay = self._layer_of(lid)
        if not lay:
            return []
        return [str(c) for c in lay.gdf.columns if c != lay.gdf.geometry.name]

    def _layer_of(self, lid: str | None):
        if lid == "$prev":
            return list(self.store.layers.values())[-1] if self.store.layers else None
        return self.store.get(lid)

    def _is_categorical(self, lid: str | None, field: str) -> bool:
        """判断字段是否适合做分组：非数值型或取值数较少。"""
        lay = self._layer_of(lid)
        if not lay or field not in lay.gdf.columns:
            return False
        s = lay.gdf[field]
        try:
            if s.dtype.kind in "ifc":
                return s.nunique(dropna=True) <= 20
            return s.nunique(dropna=True) <= 50
        except Exception:
            return False

    def _build_expr(self, c: str, lid: str | None) -> str | None:
        fields = self._fields_of(lid)
        ops = [(r">=|大于等于|不小于|不少于", ">="), (r"<=|小于等于|不大于|不超过", "<="),
               (r">|大于|超过|高于|多于", ">"), (r"<|小于|低于|少于|不足", "<"),
               (r"==|＝|等于|是", "==")]
        for f in fields:
            if f not in c:
                continue
            tail = c.split(f, 1)[1]
            for pat, op in ops:
                m = re.match(r"\s*(?:" + pat + r")\s*['\"]?([\u4e00-\u9fa5A-Za-z0-9_.\-]+)['\"]?", tail)
                if m:
                    val = m.group(1)
                    q = f"'{val}'" if re.search(r"[\u4e00-\u9fa5]", val) else val
                    return f"{f} {op} {q}"
        # 直接给出表达式
        m = re.search(r"([\u4e00-\u9fa5A-Za-z_]\w*)\s*(>=|<=|>|<|==)\s*['\"]?([\w\u4e00-\u9fa5.]+)['\"]?", c)
        if m:
            return f"{m.group(1)} {m.group(2)} {m.group(3)}"
        return None

    # ---------------- LLM 解析
    def _call_llm(self, cfg: dict, msgs: list[dict], json_mode: bool) -> tuple[str, str] | None:
        """发一次请求，返回 (content, finish_reason)；网络失败返回 None。"""
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        payload = {"model": cfg.get("model") or "deepseek-chat", "messages": msgs,
                   "temperature": 0.1, "max_tokens": 1600}
        if json_mode:
            # DeepSeek / OpenAI 兼容接口都支持；不支持的服务端会 400，下面会退回普通模式
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            ch = data["choices"][0]
            return ch["message"]["content"] or "", ch.get("finish_reason") or ""
        except urllib.error.HTTPError as e:
            if json_mode and e.code in (400, 404, 422):
                return self._call_llm(cfg, msgs, json_mode=False)
            self._llm_fail_at = time.time()
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            return {"__error__": f"HTTP {e.code} {detail}"}  # type: ignore[return-value]
        except Exception as e:
            self._llm_fail_at = time.time()
            return {"__error__": str(e)}  # type: ignore[return-value]

    def llm_plan(self, text: str, current: str | None, cfg: dict, history: list[dict]) -> dict | None:
        if not cfg.get("api_key") or not cfg.get("base_url"):
            return None
        if time.time() - self._llm_fail_at < 60:
            return {"error": ""}   # 刚失败过，先安静地走规则引擎
        ctx = []
        for l in self.store.layers.values():
            m = l.meta()
            ctx.append({"id": l.id, "name": l.name, "geom": m["geomType"], "crs": m["crs"],
                        "count": m["count"], "fields": m["fields"][:25]})
        # 算子描述统一用 tool / args 这套词：之前给的是 name / params，
        # 模型照着抄成 {"op":…,"params":…}，执行器一个都不认。
        specs = [{"tool": s["name"], "desc": s["desc"], "args": s["params"]} for s in TOOL_SPECS]
        sys_prompt = (
            "你是 GIS 矢量数据分析师，负责把用户的中文指令翻译成结构化空间运算步骤。\n"
            f"当前已加载图层（JSON）：{json.dumps(ctx, ensure_ascii=False)}\n"
            f"当前选中图层：{current or '无'}\n"
            f"可用算子：{json.dumps(specs, ensure_ascii=False)}\n"
            "输出格式（务必照抄字段名，步骤对象只有 tool 和 args 两个字段，"
            "不要写成 op / params / name）：\n"
            '{"plan":[{"tool":"buffer","args":{"layer":"道路","distance":500,"unit":"m"}}],'
            '"reply":"一句话说明"}\n'
            "规则：\n"
            "1. 只使用上面列出的算子，args 里的图层用图层 name 传递；\n"
            "2. 距离单位用 m/km，务必把距离换算成数字 + unit；\n"
            "3. 若指令需要多步，按执行顺序输出，后一步可以用 \"$prev\" 表示上一步产出的图层；\n"
            "4. 若无法完成，plan 为空数组并在 reply 中说明缺什么；\n"
            "5. 若用户只是问候、道谢或问你能做什么，plan 为空数组，reply 用一两句自然回应，"
            "不要输出模板或用「拆不出步骤」这类机器人腔；\n"
            "6. 需要面积、周长、外接矩形、紧凑度这类几何量时，用 shape_metrics 直接生成字段，"
            "再用 field_calc 做四则运算；不要在 field_calc 里调用不存在的函数（只能用字段名和四则运算）；\n"
            "7. 只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字。"
        )
        base_msgs = [{"role": "system", "content": sys_prompt}]
        for h in history[-6:]:
            base_msgs.append({"role": h["role"], "content": h["content"][:600]})
        base_msgs.append({"role": "user", "content": text})

        last_err = ""
        for attempt in range(2):
            msgs = list(base_msgs)
            if attempt == 1:
                msgs.append({"role": "assistant", "content": last_err or "(上一条不是合法 JSON)"})
                msgs.append({"role": "user", "content":
                             "只输出那一个 JSON 对象，字段是 tool 和 args，不要任何其他文字。"})
            out = self._call_llm(cfg, msgs, json_mode=(attempt == 1))
            if isinstance(out, dict):           # 网络/接口错误
                return {"error": f"大模型接口调用失败，已回退规则引擎：{out.get('__error__', '')}"}
            content, finish = out
            if finish == "length":
                last_err = content
                continue                        # 被截断了：重试一次，提示它压缩输出
            parsed = _parse_json_payload(content)
            if parsed is not None:
                plan, reply = normalize_plan(parsed)
                if plan or reply:
                    return {"plan": plan, "reply": reply}
                last_err = content
                continue
            last_err = content

        # 两次都没拿到结构化结果：把模型原话当回复展示，总比一句“解析失败”有用
        words = (last_err or "").strip()
        if words and not words.startswith("{"):
            return {"plan": [], "reply": words[:400]}
        return {"error": "大模型返回的内容解析不出步骤，已回退规则引擎。"}


# --------------------------------------------------------------------------- 执行
def run_plan(store: Store, plan: list[dict]) -> tuple[list[dict], list[str], list[dict]]:
    """执行计划，返回 (步骤追踪, 汇总句子, 生成图层meta)。"""
    steps: list[dict] = []
    lines: list[str] = []
    new_layers: list[dict] = []
    last_id: str | None = None
    for raw in plan:
        tool = (raw or {}).get("tool")
        args = dict((raw or {}).get("args") or {})
        label = TOOL_LABEL.get(tool, tool or "未知算子")
        t0 = time.time()
        if tool not in OPERATIONS:
            steps.append({"tool": tool, "label": label, "args": args, "status": "error",
                          "message": f"不支持的算子：{tool}", "warnings": [], "data": {},
                          "elapsed": 0})
            lines.append(f"⚠️ 不支持的算子 {tool}")
            continue
        # 图层名 → id 解析（$prev 表示上一步产出的图层）
        for k in ("layer", "other", "mask"):
            v = args.get(k)
            if v == "$prev":
                if not last_id:
                    raise ValueError("上一步没有生成新图层，无法把结果传给下一步。")
                args[k] = last_id
            elif isinstance(v, str):
                lay = store.get(v)
                if lay is None and store.layers:
                    raise ValueError(f"找不到图层「{v}」，当前可用："
                                     + "、".join(l.name for l in store.layers.values()))
                if lay is not None:
                    args[k] = lay.id
        try:
            res: Result = OPERATIONS[tool](store, **args)
            el = round(time.time() - t0, 3)
            step = {"tool": tool, "label": label, "args": args, "status": "ok" if res.ok else "warn",
                    "message": res.message, "warnings": res.warnings, "data": res.data,
                    "elapsed": el, "layer": res.layer.meta() if res.layer else None}
            steps.append(step)
            lines.append(res.message)
            if res.layer:
                new_layers.append(res.layer.meta())
                last_id = res.layer.id
            else:
                # 本步没有产出新图层（检查类算子，或「修复/去重」发现零变化）。
                # 让 $prev 指向这一步的输入图层，下一步还能接着用，不必因为
                # 「没变化」而把整条链断掉。
                src = args.get("layer") or args.get("other") or args.get("mask")
                src_lay = store.get(src) if isinstance(src, str) else None
                if src_lay is not None:
                    last_id = src_lay.id
        except Exception as e:
            el = round(time.time() - t0, 3)
            steps.append({"tool": tool, "label": label, "args": args, "status": "error",
                          "message": str(e), "warnings": [], "data": {}, "elapsed": el})
            lines.append(f"✗ {label}失败：{e}")
            break
    return steps, lines, new_layers


SUGGESTIONS = [
    "对当前图层做 500 米缓冲区",
    "按字段融合后统计各类面积",
    "用边界图层裁剪当前图层",
    "检查数据质量，报告坐标系和无效几何",
    "生成 1 公里渔网并统计每个网格内的要素数",
    "把当前图层投影到 CGCS2000 3 度带",
    "筛选面积大于 10000 的要素",
    "取要素质心后生成泰森多边形",
]
