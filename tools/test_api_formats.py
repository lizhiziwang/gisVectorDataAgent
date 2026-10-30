"""API 格式适配层的回归：每种格式的请求怎么拼、响应怎么读。

离线，不联网。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import api_formats
from app.config import normalize

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


MSGS = [
    {"role": "system", "content": "你是 GIS 分析师"},
    {"role": "user", "content": "做缓冲区"},
]


def cfg(**kw):
    base = {"base_url": "https://api.example.com/v1", "model": "m1",
            "api_key": "k1", "api_format": "openai_chat", "api_overrides": {}}
    base.update(kw)
    return base


print("[1] OpenAI Chat Completions")
url, h, p = api_formats.build_request(cfg(), MSGS, True, 100)
check("URL", url, "https://api.example.com/v1/chat/completions")
check("Bearer 头", h.get("Authorization"), "Bearer k1")
check("messages 原样", p["messages"], MSGS)
check("JSON mode", p.get("response_format"), {"type": "json_object"})
check("max_tokens", p["max_tokens"], 100)
c, f = api_formats.parse_response("openai_chat", {
    "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}]})
check("取 content", c, "你好")
check("取 finish", f, "stop")

print("[2] OpenAI Responses")
url, h, p = api_formats.build_request(
    cfg(api_format="openai_response", base_url="https://api.example.com/v1"), MSGS, True, 100)
check("URL", url, "https://api.example.com/v1/responses")
check("system 提到 instructions", "GIS 分析师" in p.get("instructions", ""), True)
check("system 不在 input 里", all(m["role"] != "system" for m in p["input"]), True)
check("input 只留 user/assistant", len(p["input"]), 1)
check("max_output_tokens", p["max_output_tokens"], 100)
c, f = api_formats.parse_response("openai_response", {
    "output": [{"type": "message",
                "content": [{"type": "output_text", "text": "行"}]}],
    "status": "completed"})
check("取 content", c, "行")
check("取 finish", f, "stop")

print("[3] Anthropic Messages")
url, h, p = api_formats.build_request(
    cfg(api_format="anthropic", base_url="https://api.anthropic.com/v1"), MSGS, True, 100)
check("URL", url, "https://api.anthropic.com/v1/messages")
check("x-api-key 头", h.get("x-api-key"), "k1")
check("不带 Bearer", "Authorization" in h, False)
check("anthropic-version", h.get("anthropic-version"), "2023-06-01")
check("system 单独拎出来", "GIS 分析师" in p.get("system", ""), True)
check("messages 不含 system", all(m["role"] != "system" for m in p["messages"]), True)
c, f = api_formats.parse_response("anthropic", {
    "content": [{"type": "text", "text": "好的"}], "stop_reason": "end_turn"})
check("取 content", c, "好的")
check("结束原因映射", f, "stop")

print("[4] Gemini generateContent")
url, h, p = api_formats.build_request(
    cfg(api_format="gemini", base_url="https://generativelanguage.googleapis.com/v1beta"),
    MSGS, True, 100)
check("URL 带模型名", url,
      "https://generativelanguage.googleapis.com/v1beta/models/m1:generateContent")
check("x-goog-api-key 头", h.get("x-goog-api-key"), "k1")
check("contents 结构", p["contents"][0]["parts"][0]["text"], "做缓冲区")
check("systemInstruction", "GIS 分析师" in p["systemInstruction"]["parts"][0]["text"], True)
check("JSON mime", p["generationConfig"]["responseMimeType"], "application/json")
c, f = api_formats.parse_response("gemini", {
    "candidates": [{"content": {"parts": [{"text": "了解"}]}, "finishReason": "STOP"}]})
check("取 content", c, "了解")
check("结束原因映射", f, "stop")

print("[5] Ollama 原生")
url, h, p = api_formats.build_request(
    cfg(api_format="ollama", base_url="http://localhost:11434"), MSGS, True, 100)
check("URL", url, "http://localhost:11434/api/chat")
check("stream=false", p.get("stream"), False)
check("format=json", p.get("format"), "json")
check("messages 原样", p["messages"], MSGS)
c, f = api_formats.parse_response("ollama", {
    "message": {"content": "嗯"}, "done": True})
check("取 content", c, "嗯")
check("结束原因映射", f, "stop")

print("[6] 自定义覆盖：端点路径 + 附加请求头 + 请求体字段")
url, h, p = api_formats.build_request(cfg(
    api_overrides={"path": "/v2/invoke", "headers": {"X-Team": "gis"},
                   "body": {"top_p": 0.2}}), MSGS, False, 100)
check("自定义路径", url, "https://api.example.com/v1/v2/invoke")
check("附加请求头", h.get("X-Team"), "gis")
check("原有头还在", h.get("Authorization"), "Bearer k1")
check("自定义请求体字段", p.get("top_p"), 0.2)

print("[7] 自定义路径里的 {model} 占位")
url, _h, _p = api_formats.build_request(
    cfg(api_format="gemini", base_url="https://x.example.com/v1beta",
        api_overrides={"path": "/models/{model}:streamGenerateContent"}), MSGS, False, 100)
check("占位替换", url, "https://x.example.com/v1beta/models/m1:streamGenerateContent")

print("[8] 响应解析失败要报错，不能静默返回空串")
for fmt, bad in (("openai_chat", {}), ("openai_chat", {"choices": [{}]}),
                  ("anthropic", {}), ("gemini", {}), ("ollama", {})):
    try:
        api_formats.parse_response(fmt, bad)
        check(f"{fmt} 坏响应应抛错", "没抛", True)
    except ValueError:
        check(f"{fmt} 坏响应应抛错", True, True)
    except Exception as e:
        check(f"{fmt} 坏响应应抛错", f"抛了别的异常: {e}", True)

print("[9] 配置归一化")
n = normalize({"api_key": "k", "api_format": "anthropic",
               "base_url": "https://api.anthropic.com/v1/messages"})
check("anthropic 不砍 /v1", n["base_url"], "https://api.anthropic.com/v1")
n = normalize({"api_key": "k", "api_format": "openai_chat",
               "base_url": "https://api.deepseek.com/v1/chat/completions"})
check("openai 砍掉端点", n["base_url"], "https://api.deepseek.com/v1")
n = normalize({"api_key": "k", "api_format": "openai_chat", "base_url": "https://api.deepseek.com"})
check("只有域名补 /v1", n["base_url"], "https://api.deepseek.com/v1")
n = normalize({"api_key": "k", "api_format": "gemini",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent"})
check("gemini 砍掉模型段", n["base_url"], "https://generativelanguage.googleapis.com/v1beta")
n = normalize({"api_key": "k", "api_format": "不存在的格式"})
check("未知格式回落", n["api_format"], "openai_chat")
n = normalize({"api_key": "k", "api_overrides": {"path": "  ", "headers": {}, "junk": 1}})
check("空覆盖被清掉", n["api_overrides"], {})
n = normalize({"api_key": "k", "api_overrides": {"headers": {"X-A": "1"}}})
check("保留有效覆盖", n["api_overrides"], {"headers": {"X-A": "1"}})

print("[10] Ollama 不需要 key 也能用")
from app.config import is_ready
n = normalize({"api_format": "ollama", "base_url": "http://localhost:11434",
               "model": "qwen2.5:7b", "enabled": True})
check("没 key 也算就绪", is_ready(n), True)
n = normalize({"api_format": "ollama", "enabled": True})
check("自动补本地地址", n["base_url"], "http://localhost:11434")
check("自动补模型", n["model"], "qwen2.5:7b")
check("没 key 也算就绪", is_ready(n), True)
n = normalize({"api_format": "openai_chat", "base_url": "https://x/v1", "model": "m",
               "enabled": True})
check("其他格式没 key 不算就绪", is_ready(n), False)

print("[11] 规划器不会因为没 key 就跳过 Ollama")
from app.agent import Planner
from app.engine import Store
p = Planner(Store())
called = {"n": 0}


def fake_call(cfg, msgs, json_mode, max_tokens=1600):
    called["n"] += 1
    return '{"plan": [], "reply": "ok"}', "stop"


p._call_llm = fake_call
out = p.llm_plan("做点事", None,
                 {"base_url": "http://localhost:11434", "model": "q", "api_key": "",
                  "api_format": "ollama", "enabled": True}, [])
check("确实调了模型", called["n"], 1)
check("拿到回复", (out or {}).get("reply"), "ok")
out2 = p.llm_plan("做点事", None,
                   {"base_url": "https://x/v1", "model": "q", "api_key": "",
                    "api_format": "openai_chat", "enabled": True}, [])
check("OpenAI 格式没 key 直接跳过", out2, None)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
