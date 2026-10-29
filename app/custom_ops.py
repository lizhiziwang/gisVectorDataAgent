"""自定义算子：由自然语言算法描述生成、校验、落盘、执行。

设计取舍
--------
· **契约极简**：生成物就是一个 `def op(gdf, args) -> (GeoDataFrame|None, str)` 函数，
  模型不用理解本项目内部结构，也不碰 Store/Layer；
· **沙箱**：AST 静态检查（禁止 import / while / 双下划线属性 / 危险内建），
  运行时只注入 pandas / numpy / geopandas / shapely 和两个坐标辅助函数；
· **可审计**：算子源码以 JSON（含元信息）存到 `data/operators/`，
  界面上生成后先给用户看代码再决定是否入库，随时可在算子库里删。
"""
from __future__ import annotations

import ast
import builtins
import json
import re
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = ROOT / "data" / "operators"

# 生成代码里允许出现的名字（其余一律拒绝）
_ALLOWED_BUILTINS = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float",
    "format", "frozenset", "int", "isinstance", "iter", "len", "list", "map", "max",
    "min", "next", "pow", "range", "reversed", "round", "set", "slice", "sorted",
    "str", "sum", "tuple", "zip", "print", "True", "False", "None",
)
_BANNED_NODES = (ast.Import, ast.ImportFrom, ast.While, ast.ClassDef,
                 ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith, ast.Global,
                 ast.Nonlocal)
_BANNED_NAMES = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
                 "exit", "quit", "globals", "locals", "vars", "dir", "help", "type",
                 "getattr", "setattr", "delattr", "memoryview", "super", "object"}


class OperatorError(ValueError):
    """自定义算子的语法/安全检查/执行错误。"""


# --------------------------------------------------------------------------- 校验
def check_code(code: str) -> None:
    """静态检查生成的代码，不合格就抛 OperatorError（附原因）。"""
    if not isinstance(code, str) or not code.strip():
        raise OperatorError("算子代码为空。")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise OperatorError(f"代码有语法错误：第 {e.lineno} 行 {e.msg}")

    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if not any(f.name == "op" for f in funcs):
        raise OperatorError("缺少入口函数 `def op(gdf, args):`。")

    for node in ast.walk(tree):
        if isinstance(node, _BANNED_NODES):
            what = {ast.Import: "import 导入", ast.ImportFrom: "import 导入",
                    ast.While: "while 循环（可能死循环）", ast.ClassDef: "class 定义",
                    ast.AsyncFunctionDef: "async 函数", ast.AsyncFor: "async 循环",
                    ast.AsyncWith: "async with", ast.Global: "global 声明",
                    ast.Nonlocal: "nonlocal 声明"}.get(type(node), type(node).__name__)
            raise OperatorError(f"不允许使用 {what}。")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise OperatorError(f"不允许访问 `{node.attr}` 这类内部属性。")
        if isinstance(node, ast.Name):
            if node.id in _BANNED_NAMES:
                raise OperatorError(f"不允许使用 `{node.id}`。")
            if node.id.startswith("__"):
                raise OperatorError(f"不允许使用 `{node.id}`。")


def _safe_env() -> dict:
    """执行环境：只给四件套 + 两个坐标辅助 + 白名单内建函数。"""
    env = {name: getattr(builtins, name) for name in _ALLOWED_BUILTINS
           if hasattr(builtins, name)}
    env.update({"__builtins__": env, "pd": pd, "np": np,
                "gpd": gpd, "shapely": shapely})
    env["metric"] = _metric
    env["to_wgs"] = _to_wgs
    return env


def _metric(gdf):
    """把数据转到适合算长度/面积的投影坐标系（不改变入参）。"""
    from .engine import metric_crs
    try:
        if gdf.crs is not None and not gdf.crs.is_geographic:
            return gdf
        code, _ = metric_crs(gdf)
        return gdf.to_crs(code)
    except Exception:
        return gdf


def _to_wgs(gdf, like):
    """把结果转回参考图层 `like` 的坐标系。"""
    try:
        return gdf.to_crs(like.crs)
    except Exception:
        return gdf


def compile_op(code: str):
    """校验并编译成可调用对象 `op(gdf, args)`。"""
    check_code(code)
    env = _safe_env()
    exec(compile(code, "<custom-op>", "exec"), env)   # noqa: S102 - 已做 AST 白名单
    fn = env.get("op")
    if not callable(fn):
        raise OperatorError("代码里没有可调用的 op 函数。")
    return fn


def run_op(spec: dict, gdf, args: dict):
    """执行顺序：编译 → 调用 → 校验返回值。返回 (GeoDataFrame|None, 说明)。"""
    fn = compile_op(spec.get("code") or "")
    try:
        ret = fn(gdf, dict(args or {}))
    except OperatorError:
        raise
    except Exception as e:
        raise OperatorError(f"算子执行出错：{e}")
    if isinstance(ret, tuple) and len(ret) == 2:
        out, msg = ret
    elif isinstance(ret, gpd.GeoDataFrame):
        out, msg = ret, ""
    else:
        raise OperatorError("算子必须返回 (GeoDataFrame 或 None, 说明文字)。")
    if out is not None:
        if not isinstance(out, gpd.GeoDataFrame):
            raise OperatorError("算子返回的第一个值不是 GeoDataFrame。")
        if out.geometry.name not in out.columns:
            raise OperatorError("返回的 GeoDataFrame 里没有几何列。")
        if len(out) == 0:
            raise OperatorError("算子返回了空图层（0 个要素），检查一下筛选条件。")
    return out, str(msg or "")


# --------------------------------------------------------------------------- 落盘
def _safe_name(name: str) -> str:
    n = re.sub(r"[^0-9a-zA-Z_]+", "_", str(name or "")).strip("_").lower()
    if not n:
        n = f"custom_{int(time.time())}"
    if n[0].isdigit():
        n = "op_" + n
    return n[:40]


def save(spec: dict) -> dict:
    """保存到 data/operators/<name>.json，返回补全后的 spec。"""
    spec = dict(spec or {})
    spec["name"] = _safe_name(spec.get("name") or spec.get("title"))
    spec["title"] = (spec.get("title") or spec["name"]).strip()[:24]
    spec["desc"] = (spec.get("desc") or "").strip()
    spec["params"] = spec.get("params") or {}
    spec["code"] = spec.get("code") or ""
    spec["created"] = spec.get("created") or time.strftime("%Y-%m-%d %H:%M:%S")
    check_code(spec["code"])
    OPS_DIR.mkdir(parents=True, exist_ok=True)
    (OPS_DIR / f"{spec['name']}.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return spec


def load_all() -> list[dict]:
    """读回所有自定义算子（坏文件跳过，不让它拖垮启动）。"""
    out = []
    if not OPS_DIR.exists():
        return out
    for f in sorted(OPS_DIR.glob("*.json")):
        try:
            spec = json.loads(f.read_text(encoding="utf-8"))
            check_code(spec.get("code") or "")
            out.append(spec)
        except Exception:
            continue
    return out


def install(spec: dict) -> dict:
    """注册到运行期：引擎的 OPERATIONS + 规划器的工具表。

    这样自定义算子和内置算子完全等价：规则引擎点得到、大模型看得到、
    算子库按钮里也有它。返回补全后的 spec。
    """
    from .agent import register_tool_spec
    from .engine import OPERATIONS, make_custom_operator

    spec = dict(spec)
    spec["name"] = _safe_name(spec.get("name") or spec.get("title"))
    OPERATIONS[spec["name"]] = make_custom_operator(spec)
    register_tool_spec(spec["name"], spec.get("title") or spec["name"],
                       spec.get("desc") or "自定义算子",
                       spec.get("params") or {"layer": "图层名"})
    return spec


def install_all() -> list[dict]:
    """启动时把磁盘上的自定义算子全部装回去。"""
    out = []
    for spec in load_all():
        try:
            out.append(install(spec))
        except Exception:
            continue
    return out


def remove(name: str) -> bool:
    f = OPS_DIR / f"{name}.json"
    if f.exists():
        try:
            f.unlink()
            return True
        except Exception:
            return False
    return False
