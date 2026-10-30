"""大模型 API 格式适配层。

同一个「聊天 + JSON 输出」的语义，不同厂商的 HTTP 接口差别很大：
URL 路径、鉴权头、请求体结构、响应取值路径都不一样。
这个模块把这些差异收敛成一组适配器，让 `Planner._call_llm` 只管
「发请求 → 拿到 content 和 finish_reason」。

支持四种格式（设置窗口里可切）：

| key            | 说明                                    | 端点            |
|----------------|-----------------------------------------|-----------------|
| openai_chat    | OpenAI Chat Completions（默认，兼容面最广）| /chat/completions |
| openai_response| OpenAI Responses API                     | /responses      |
| anthropic      | Anthropic Messages                       | /messages       |
| gemini         | Google Gemini generateContent            | :generateContent |
| ollama         | Ollama 原生 /api/chat                    | /api/chat       |

每种格式还允许**自定义覆盖**（`cfg["api_overrides"]`）：
改端点路径、加自定义请求头、改请求体/响应的取值路径，
用来对接自建网关或私有代理。
"""
from __future__ import annotations

import json
from typing import Any

#: 设置窗口里展示的名称 → 格式 key
FORMATS: dict[str, str] = {
    "OpenAI Chat Completions（通用兼容）": "openai_chat",
    "OpenAI Responses": "openai_response",
    "Anthropic Messages（Claude）": "anthropic",
    "Google Gemini generateContent": "gemini",
    "Ollama 原生 /api/chat": "ollama",
}

#: 格式 key → 展示名称（用于回显当前选择）
FORMAT_NAMES: dict[str, str] = {v: k for k, v in FORMATS.items()}

DEFAULT_FORMAT = "openai_chat"


# --------------------------------------------------------------------------- 请求
def build_request(cfg: dict, msgs: list[dict], json_mode: bool,
                  max_tokens: int) -> tuple[str, dict, dict]:
    """把「对话消息」翻译成具体厂商的 HTTP 请求。

    返回 (url, headers, payload)。
    """
    fmt = (cfg.get("api_format") or DEFAULT_FORMAT).strip() or DEFAULT_FORMAT
    base = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or "deepseek-chat"
    ov = cfg.get("api_overrides") or {}

    if fmt == "anthropic":
        return _build_anthropic(cfg, msgs, json_mode, max_tokens, base, model, ov)
    if fmt == "gemini":
        return _build_gemini(cfg, msgs, json_mode, max_tokens, base, model, ov)
    if fmt == "ollama":
        return _build_ollama(cfg, msgs, json_mode, max_tokens, base, model, ov)
    if fmt == "openai_response":
        return _build_openai_responses(cfg, msgs, json_mode, max_tokens, base, model, ov)
    return _build_openai_chat(cfg, msgs, json_mode, max_tokens, base, model, ov)


def _auth_headers(cfg: dict, extra: dict | None = None) -> dict:
    """请求头：默认 Bearer 鉴权，可被自定义头覆盖。"""
    h = {"Content-Type": "application/json"}
    key = (cfg.get("api_key") or "").strip()
    if key:
        h["Authorization"] = f"Bearer {key}"
    for k, v in (extra or {}).items():
        if k and v:
            h[str(k)] = str(v)
    return h


def _build_openai_chat(cfg, msgs, json_mode, max_tokens, base, model, ov) -> tuple[str, dict, dict]:
    path = ov.get("path") or "/chat/completions"
    url = base + path if path.startswith("/") else base + "/" + path
    payload: dict[str, Any] = {
        "model": model,
        "messages": msgs,
        "temperature": 0.1,
        "max_tokens": int(max_tokens),
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # 自定义请求体字段（浅层合并）
    for k, v in (ov.get("body") or {}).items():
        payload[k] = v
    return url, _auth_headers(cfg, ov.get("headers")), payload


def _build_openai_responses(cfg, msgs, json_mode, max_tokens, base, model, ov) -> tuple[str, dict, dict]:
    """OpenAI Responses API：输入用 input 数组，输出用 output_text。"""
    path = ov.get("path") or "/responses"
    url = base + path if path.startswith("/") else base + "/" + path
    # Responses API 没有 system role，system 消息要转成 instructions
    instructions = ""
    inputs = []
    for m in msgs:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            instructions += (content + "\n")
        elif role in ("user", "assistant"):
            inputs.append({"role": role, "content": content})
    payload: dict[str, Any] = {
        "model": model,
        "input": inputs,
        "max_output_tokens": int(max_tokens),
    }
    if instructions:
        payload["instructions"] = instructions.strip()
    if json_mode:
        payload["text"] = {"format": {"type": "json_object"}}
    for k, v in (ov.get("body") or {}).items():
        payload[k] = v
    return url, _auth_headers(cfg, ov.get("headers")), payload


def _build_anthropic(cfg, msgs, json_mode, max_tokens, base, model, ov) -> tuple[str, dict, dict]:
    """Anthropic Messages API：x-api-key 头 + system 单独拎出来。"""
    path = ov.get("path") or "/messages"
    url = base + path if path.startswith("/") else base + "/" + path
    system = "\n".join(m.get("content") or "" for m in msgs if m.get("role") == "system")
    convo = [{"role": m.get("role"), "content": m.get("content") or ""}
             for m in msgs if m.get("role") in ("user", "assistant")]
    payload: dict[str, Any] = {
        "model": model,
        "messages": convo,
        "max_tokens": int(max_tokens),
    }
    if system:
        payload["system"] = system
    if json_mode:
        # Anthropic 没有 response_format，靠提示词约束 + 解析兜底
        payload["temperature"] = 0.1
    for k, v in (ov.get("body") or {}).items():
        payload[k] = v
    headers = _auth_headers(cfg, ov.get("headers"))
    # Anthropic 用 x-api-key 鉴权，不是 Bearer。
    # 要把 Authorization 去掉 —— 否则两个头都在，有些网关会先看 Authorization。
    key = (cfg.get("api_key") or "").strip()
    if key:
        headers["x-api-key"] = key
    headers.pop("Authorization", None)
    headers.setdefault("anthropic-version", "2023-06-01")
    return url, headers, payload


def _build_gemini(cfg, msgs, json_mode, max_tokens, base, model, ov) -> tuple[str, dict, dict]:
    """Gemini generateContent：contents 数组 + x-goog-api-key 头。"""
    path = ov.get("path") or "/models/{model}:generateContent"
    if "{model}" in path:
        path = path.replace("{model}", model)
    else:
        path = path.replace(model, "{model}") if model in path else path
    url = base + path if path.startswith("/") else base + "/" + path
    system = "\n".join(m.get("content") or "" for m in msgs if m.get("role") == "system")
    contents = []
    for m in msgs:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        # Gemini 的角色只有 user / model
        contents.append({"role": "model" if role == "assistant" else "user",
                         "parts": [{"text": m.get("content") or ""}]})
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": int(max_tokens),
            "responseMimeType": "application/json" if json_mode else "text/plain",
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    for k, v in (ov.get("body") or {}).items():
        payload[k] = v
    headers = {"Content-Type": "application/json"}
    key = (cfg.get("api_key") or "").strip()
    if key:
        headers["x-goog-api-key"] = key
    for k, v in (ov.get("headers") or {}).items():
        if k and v:
            headers[str(k)] = str(v)
    return url, headers, payload


def _build_ollama(cfg, msgs, json_mode, max_tokens, base, model, ov) -> tuple[str, dict, dict]:
    """Ollama 原生 /api/chat：messages 格式 + stream=false。"""
    path = ov.get("path") or "/api/chat"
    url = base + path if path.startswith("/") else base + "/" + path
    payload: dict[str, Any] = {
        "model": model,
        "messages": msgs,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": int(max_tokens)},
    }
    if json_mode:
        payload["format"] = "json"
    for k, v in (ov.get("body") or {}).items():
        payload[k] = v
    headers = {"Content-Type": "application/json"}
    key = (cfg.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for k, v in (ov.get("headers") or {}).items():
        if k and v:
            headers[str(k)] = str(v)
    return url, headers, payload


# --------------------------------------------------------------------------- 响应
def parse_response(fmt: str, data: Any) -> tuple[str, str]:
    """从厂商响应里取出 (content, finish_reason)。

    解析不到就抛 ValueError —— 让调用方按「模型输出格式不对」处理，
    而不是静默返回空串（那会表现成「模型什么都没说」）。
    """
    fmt = (fmt or DEFAULT_FORMAT).strip() or DEFAULT_FORMAT
    if not isinstance(data, dict):
        raise ValueError("响应不是 JSON 对象")

    if fmt == "anthropic":
        blocks = data.get("content") or []
        parts = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") in (None, "text"):
                parts.append(str(b.get("text") or ""))
        content = "".join(parts)
        if not content and isinstance(data.get("completion"), str):
            content = data["completion"]
        if not content:
            raise ValueError("响应里取不到 content（content 数组为空或没有文本块）")
        finish = data.get("stop_reason") or ""
        return content, _map_finish(finish, "anthropic")

    if fmt == "gemini":
        cands = data.get("candidates") or []
        parts = []
        finish = ""
        for c in cands:
            if not isinstance(c, dict):
                continue
            finish = c.get("finishReason") or finish
            for p in ((c.get("content") or {}).get("parts") or []):
                if isinstance(p, dict) and p.get("text"):
                    parts.append(str(p["text"]))
        content = "".join(parts)
        if not content:
            raise ValueError("响应里取不到 content（candidates 为空或没有文本）")
        return content, _map_finish(finish, "gemini")

    if fmt == "ollama":
        msg = data.get("message") or {}
        content = str(msg.get("content") or "") if isinstance(msg, dict) else ""
        if not content and isinstance(data.get("response"), str):
            content = data["response"]
        if not content:
            raise ValueError("响应里取不到 content（message.content 为空）")
        done = "stop" if data.get("done") else ""
        return content, _map_finish(done, "ollama")

    if fmt == "openai_response":
        # Responses API：output 数组里找 message → content[].text
        parts = []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("text"):
                    parts.append(str(c["text"]))
        content = "".join(parts)
        if not content:
            ot = data.get("output_text")
            if isinstance(ot, str):
                content = ot
            elif isinstance(ot, list):
                content = "".join(str(x) for x in ot)
        status = data.get("status") or ""
        incomplete = data.get("incomplete_details") or {}
        finish = status or (incomplete.get("reason") if isinstance(incomplete, dict) else "")
        return content, _map_finish(finish, "openai_response")

    # openai_chat（默认）
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("响应里没有 choices")
    ch = choices[0] or {}
    msg = ch.get("message") or {}
    content = msg.get("content")
    if content is None:
        # 有些兼容接口返回 choices[0].text
        content = ch.get("text")
    if content is None:
        raise ValueError("响应里取不到 content")
    if isinstance(content, list):
        # 多模态格式：[{type: text, text: ...}, ...]
        content = "".join(str(p.get("text") or "") for p in content
                          if isinstance(p, dict))
    return str(content), _map_finish(ch.get("finish_reason") or "", "openai_chat")


#: 各家 finish_reason 的说法不一样，统一成 OpenAI 那套
_FINISH_MAP = {
    "anthropic": {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
                  "tool_use": "tool_calls"},
    "gemini": {"STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter",
               "RECITATION": "content_filter", "OTHER": "stop"},
    "ollama": {"stop": "stop", "length": "length"},
    "openai_response": {"completed": "stop", "incomplete": "length",
                        "max_output_tokens": "length"},
}


def _map_finish(reason: str, fmt: str) -> str:
    r = (reason or "").strip()
    if not r:
        return ""
    m = _FINISH_MAP.get(fmt) or {}
    return m.get(r, r)


def describe(fmt: str) -> str:
    """一句话说明这个格式会用什么样的请求（给设置窗口显示）。"""
    return {
        "openai_chat": "POST {base}/chat/completions · Bearer 鉴权 · 响应 choices[0].message.content",
        "openai_response": "POST {base}/responses · Bearer 鉴权 · 响应 output[].content[].text",
        "anthropic": "POST {base}/messages · x-api-key 鉴权 · 响应 content[].text",
        "gemini": "POST {base}/models/{model}:generateContent · x-goog-api-key 鉴权 · 响应 candidates[].content.parts[].text",
        "ollama": "POST {base}/api/chat · 可无鉴权 · 响应 message.content",
    }.get(fmt, "未知格式")


def format_needs_base_only(fmt: str) -> bool:
    """Gemini 的 base 里不含模型名，路径里带 {model}；其他格式同理。

    保留这个函数是为了设置窗口能提示用户 base 该怎么填。
    """
    return fmt == "gemini"


def dumps(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()
