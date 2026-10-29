"""本地配置（大模型接口）读写，Web 版与桌面版共用。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

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
}


def _fix_base(base: str) -> str:
    """把用户填的地址整理成「可直接拼 /chat/completions」的形式。

    · 末尾多余斜杠去掉；
    · 有人会把完整的 …/chat/completions 贴进来 → 去掉；
    · 只填了域名（https://api.deepseek.com）→ 补成 /v1，
      否则请求会打到 …/chat/completions（DeepSeek 要的是 …/v1/chat/completions）。
    """
    b = (base or "").strip().rstrip("/")
    if not b:
        return ""
    if b.endswith("/chat/completions"):
        b = b[: -len("/chat/completions")].rstrip("/")
    if re.fullmatch(r"https?://[^/]+", b):
        b += "/v1"
    return b


def normalize(cfg: dict) -> dict:
    """补全配置：有 key 但没填地址/模型时按 DeepSeek 默认值补齐。

    不补的话 `is_ready()` 判 false，界面一直显示「规则引擎」，
    用户会以为「key 配了但没用」——实际上一次请求都没发出去。
    """
    out = dict(cfg or {})
    key = (out.get("api_key") or "").strip()
    out["api_key"] = key
    out["base_url"] = _fix_base(out.get("base_url") or "")
    out["model"] = (out.get("model") or "").strip()
    if key:
        if not out["base_url"]:
            out["base_url"] = DEFAULT_BASE
        if not out["model"]:
            out["model"] = DEFAULT_MODEL
        # 存了 key 就当用户想用它（除非明确关掉）
        out.setdefault("enabled", True)
        if out.get("enabled") is None:
            out["enabled"] = True
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
    """保存配置。写盘前先补全，下次打开设置就能看到实际生效的地址。"""
    cfg = normalize(cfg)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def is_ready(cfg: dict) -> bool:
    """三项齐备才算真正可用，避免界面误显示"已接入"。"""
    cfg = normalize(cfg)
    return bool(cfg.get("enabled") and cfg.get("api_key") and cfg.get("base_url"))


def explain(cfg: dict) -> str:
    """一句话说明当前状态，用来做界面提示（不泄露 key）。"""
    cfg = normalize(cfg)
    if not cfg.get("api_key"):
        return "未配置 API Key"
    if not cfg.get("enabled"):
        return "已配置 Key，但「启用大模型规划」没勾选"
    host = re.sub(r"^https?://", "", cfg.get("base_url") or "").split("/")[0]
    return f"已接入 {cfg.get('model')} @ {host}"
