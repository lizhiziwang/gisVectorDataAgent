"""本地配置（大模型接口）读写，Web 版与桌面版共用。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import api_formats

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CONFIG = DATA / "settings.json"

# 只填一个 key 时用的默认值：设置面板里的占位符也是 DeepSeek，
# 大多数人就是「贴个 key 点保存」，不该因为没填 Base URL 就静默不生效。
DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

DEFAULTS = {
    "base_url": os.environ.get("LLM_BASE_URL", ""),
    "api_key": os.environ.get("LLM_API_KEY", ""),
    "model": os.environ.get("LLM_MODEL", ""),
    "enabled": bool(os.environ.get("LLM_API_KEY")),
    # 接口格式：openai_chat / openai_response / anthropic / gemini / ollama
    "api_format": os.environ.get("LLM_API_FORMAT", "") or "openai_chat",
    # 自定义覆盖：{"path": "/v1/chat/completions", "headers": {...}, "body": {...}}
    "api_overrides": {},
    # Agent 设置：每步确认 / 技能总开关 / 工具库停用列表
    "confirm_steps": True,
    "skills_enabled": True,
    "skills_disabled": [],
    "tools_disabled": [],
    # 桌面端：对话面板宽度（用户拖出来的，记住下次还是这个宽）
    "chat_panel_width": 412,
}


def _fix_base(base: str, fmt: str = "openai_chat") -> str:
    """把用户填的地址整理成「可直接拼端点路径」的形式。

    · 末尾多余斜杠去掉；
    · 有人会把完整的 …/chat/completions 贴进来 → 去掉；
    · 只填了域名（https://api.deepseek.com）→ 补成 /v1，
      否则请求会打到 …/chat/completions（DeepSeek 要的是 …/v1/chat/completions）。

    端点路径因格式而异：OpenAI 系是 /chat/completions，Anthropic 是 /messages，
    Gemini 是 /models/{model}:generateContent，Ollama 是 /api/chat。
    按错格式去截断会把用户的地址改坏，所以这里按格式分别处理。
    """
    b = (base or "").strip().rstrip("/")
    if not b:
        return ""
    fmt = (fmt or "openai_chat").strip() or "openai_chat"
    if fmt == "openai_chat":
        if b.endswith("/chat/completions"):
            b = b[: -len("/chat/completions")].rstrip("/")
        if re.fullmatch(r"https?://[^/]+", b):
            b += "/v1"
    elif fmt == "openai_response":
        if b.endswith("/responses"):
            b = b[: -len("/responses")].rstrip("/")
    elif fmt == "anthropic":
        if b.endswith("/messages"):
            b = b[: -len("/messages")].rstrip("/")
    elif fmt == "gemini":
        # Gemini 的 base 通常是 https://generativelanguage.googleapis.com/v1beta
        b = re.sub(r"/models/[^/]+:generateContent$", "", b).rstrip("/")
    elif fmt == "ollama":
        if b.endswith("/api/chat"):
            b = b[: -len("/api/chat")].rstrip("/")
    return b


def normalize(cfg: dict) -> dict:
    """补全配置：有 key 但没填地址/模型时按 DeepSeek 默认值补齐。

    不补的话 `is_ready()` 判 false，界面一直显示「规则引擎」，
    用户会以为「key 配了但没用」——实际上一次请求都没发出去。
    """
    out = dict(cfg or {})
    key = (out.get("api_key") or "").strip()
    out["api_key"] = key
    fmt = (out.get("api_format") or "").strip() or "openai_chat"
    if fmt not in api_formats.FORMAT_NAMES:
        fmt = "openai_chat"
    out["api_format"] = fmt
    out["base_url"] = _fix_base(out.get("base_url") or "", fmt)
    out["model"] = (out.get("model") or "").strip()
    # 自定义覆盖：只留 dict，且把空值去掉
    ov = out.get("api_overrides")
    if not isinstance(ov, dict):
        ov = {}
    clean = {}
    path = str(ov.get("path") or "").strip()
    if path:
        clean["path"] = path
    for key2 in ("headers", "body"):
        v = ov.get(key2)
        if isinstance(v, dict) and v:
            clean[key2] = {str(k): val for k, val in v.items() if k}
    out["api_overrides"] = clean
    out["confirm_steps"] = bool(out.get("confirm_steps", True))
    out["skills_enabled"] = bool(out.get("skills_enabled", True))
    # 对话面板宽度：夹在合理范围内，别让坏值把界面挤没了
    try:
        out["chat_panel_width"] = int(out.get("chat_panel_width") or 412)
    except (TypeError, ValueError):
        out["chat_panel_width"] = 412
    out["chat_panel_width"] = max(340, min(640, out["chat_panel_width"]))
    # 左栏「图层 / 工具库」的上下高度（像素）：坏值就当没存过，交给界面用默认比例
    split = out.get("sidebar_split")
    clean = []
    if isinstance(split, (list, tuple)) and len(split) == 2:
        try:
            top, bottom = int(split[0]), int(split[1])
        except (TypeError, ValueError):
            top = bottom = 0
        if top >= 120 and bottom >= 120:
            clean = [top, bottom]
    if clean:
        out["sidebar_split"] = clean
    else:
        out.pop("sidebar_split", None)
    disabled = out.get("skills_disabled")
    if not isinstance(disabled, list):
        disabled = []
    out["skills_disabled"] = [str(x) for x in disabled if str(x).strip()]
    tdis = out.get("tools_disabled")
    if not isinstance(tdis, list):
        tdis = []
    out["tools_disabled"] = [str(x) for x in tdis if str(x).strip()]
    # 用户自建的工具分组：只留像样的名字（去空白、限长、去重、限个数）
    tg = out.get("tool_groups")
    if not isinstance(tg, list):
        tg = []
    groups: list[str] = []
    for raw in tg:
        name = " ".join(str(raw or "").split())
        if name and len(name) <= 12 and name not in groups:
            groups.append(name)
        if len(groups) >= 24:
            break
    if groups:
        out["tool_groups"] = groups
    else:
        out.pop("tool_groups", None)
    # 数据库连接（PostgreSQL / PostGIS）：密码和其他本地设置一样只存在这台机器上；
    # 字段校验交给 app/db_conn.py 一份实现，坏数据直接丢掉，别在界面上留点不动的空行。
    from .db_conn import normalize_list as _normalize_conns   # noqa: PLC0415
    conns = _normalize_conns(out.get("db_connections"))
    if conns:
        out["db_connections"] = conns
    else:
        out.pop("db_connections", None)
    # 左栏下半块当前页签：0 工具库 / 1 数据库（默认页不写盘，和 sidebar_split 一个规矩）
    try:
        tab = int(out.get("data_tab") or 0)
    except (TypeError, ValueError):
        tab = 0
    if tab == 1:
        out["data_tab"] = 1
    else:
        out.pop("data_tab", None)
    # 补默认值：
    # · 填了 key → 默认按 DeepSeek 补地址和模型（多数人只贴 key 就想能用）；
    # · Ollama 不需要 key → 只要选了 ollama 格式且没填地址，就给本地默认地址，
    #   别让用户以为「没填 key = 不可用」。
    if key:
        if not out["base_url"]:
            out["base_url"] = DEFAULT_BASE
        if not out["model"]:
            out["model"] = DEFAULT_MODEL
        # 存了 key 就当用户想用它（除非明确关掉）
        out.setdefault("enabled", True)
        if out.get("enabled") is None:
            out["enabled"] = True
    elif fmt == "ollama":
        if not out["base_url"]:
            out["base_url"] = "http://localhost:11434"
        if not out["model"]:
            out["model"] = "qwen2.5:7b"
    return out


def load_cfg() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text("utf-8")))
        except Exception:
            pass
    return normalize(cfg)


def save_cfg(cfg: dict) -> None:
    """保存配置。写盘前先补全，下次打开设置就能看到实际生效的地址。

    原子写 + 收紧权限：避免写到一半崩溃留下坏 JSON；密钥文件不该被同机
    其他用户随意读取（Windows 上 chmod 效果有限，但至少不影响正常使用）。
    """
    cfg = normalize(cfg)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)
    except Exception:
        pass


def is_ready(cfg: dict) -> bool:
    """配置齐备才算真正可用，避免界面误显示"已接入"。

    Ollama 这类本地服务不需要 API Key，所以对 ollama 格式只要求地址和模型。
    """
    cfg = normalize(cfg)
    if not (cfg.get("enabled") and cfg.get("base_url")):
        return False
    if cfg.get("api_format") == "ollama":
        return bool(cfg.get("model"))
    return bool(cfg.get("api_key"))


def explain(cfg: dict) -> str:
    """一句话说明当前状态，用来做界面提示（不泄露 key）。"""
    cfg = normalize(cfg)
    fmt = cfg.get("api_format") or "openai_chat"
    if not cfg.get("api_key") and fmt != "ollama":
        return "未配置 API Key"
    if not cfg.get("enabled"):
        return "已配置 Key，但「启用大模型规划」没勾选"
    host = re.sub(r"^https?://", "", cfg.get("base_url") or "").split("/")[0]
    name = api_formats.FORMAT_NAMES.get(fmt, "OpenAI")
    return f"已接入 {cfg.get('model')} @ {host}（{name}）"
