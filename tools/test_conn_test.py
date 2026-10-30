"""模型连通性测试的回归：各种失败情形要给**人话**而不是原始异常。

全程离线：urlopen 被替换成可控的假实现。
"""
from __future__ import annotations

import io
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import conn_test

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


class FakeResponse:
    def __init__(self, payload: bytes):
        self._buf = io.BytesIO(payload)

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_http_error(code: int, body: str):
    return urllib.error.HTTPError(
        "http://x", code, "err", {}, io.BytesIO(body.encode()))


# ---------------------------------------------------------------- 假 urlopen
_state = {"handler": None}
_real_urlopen = urllib.request.urlopen


def fake_urlopen(req, timeout=None):
    h = _state["handler"]
    if h is None:
        raise AssertionError("测试里不该发真实请求")
    return h(req, timeout)


urllib.request.urlopen = fake_urlopen

CFG = {"base_url": "https://api.example.com/v1", "model": "m1",
       "api_key": "k1", "api_format": "openai_chat", "api_overrides": {}}


print("[1] 连接成功")
_state["handler"] = lambda req, t: FakeResponse(
    json.dumps({"choices": [{"message": {"content": "好"}}]}).encode())
r = conn_test.test_connection(CFG)
check("成功", r.ok, True)
check("标题", r.message, "连接成功")
check("带上了模型回复", r.detail, "好")

print("[2] 401 鉴权失败")
_state["handler"] = lambda req, t: (_ for _ in ()).throw(
    make_http_error(401, json.dumps({"error": {"message": "invalid api key"}})))
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("标题说鉴权", "鉴权" in r.message, True)
check("标题不含状态码堆栈", "Traceback" not in r.message, True)
check("给了建议", "Key" in r.detail or "key" in r.detail, True)

print("[3] 403 无模型权限 —— 要点明是模型的问题")
_state["handler"] = lambda req, t: (_ for _ in ()).throw(make_http_error(
    403, json.dumps({"error": {"code": "", "message": "该令牌无权访问模型 MiniMax"}})))
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("标题提到模型", "模型" in r.message, True)
check("原文带上了", "MiniMax" in r.detail, True)

print("[4] 404 地址不对")
_state["handler"] = lambda req, t: (_ for _ in ()).throw(
    make_http_error(404, json.dumps({"error": {"message": "not found"}})))
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("标题提到地址", "地址" in r.message, True)

print("[5] 429 限流")
_state["handler"] = lambda req, t: (_ for _ in ()).throw(
    make_http_error(429, "{}"))
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("标题提到频繁/额度", "频繁" in r.message or "额度" in r.message, True)

print("[6] 500 服务端错 —— 不是用户填错")
_state["handler"] = lambda req, t: (_ for _ in ()).throw(
    make_http_error(500, "{}"))
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("说清是对方的问题", "对方" in r.detail or "服务端" in r.message, True)

print("[7] 连不上（域名解析失败）")
_state["handler"] = lambda req, t: (_ for _ in ()).throw(
    urllib.error.URLError(socket.gaierror(-2, "Name or service not known")))
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("说人话", "域名" in r.message, True)
check("不是原始异常", "gaierror" not in r.message, True)

print("[8] 连接被拒")
_state["handler"] = lambda req, t: (_ for _ in ()).throw(
    urllib.error.URLError(ConnectionRefusedError("Connection refused")))
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("说清是端口没人听", "拒绝" in r.message or "端口" in r.message, True)

print("[9] SSL 握手失败")
_state["handler"] = lambda req, t: (_ for _ in ()).throw(
    urllib.error.URLError("EOF occurred in violation of protocol"))
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("提到 HTTPS", "HTTPS" in r.message or "握手" in r.message, True)

print("[10] 超时")
_state["handler"] = lambda req, t: (_ for _ in ()).throw(TimeoutError())
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("提到超时", "超时" in r.message, True)

print("[11] 返回的不是 JSON")
_state["handler"] = lambda req, t: FakeResponse(b"<html>hello</html>")
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("说清不是接口", "JSON" in r.message, True)

print("[12] 连上了但响应结构不对（格式选错）")
_state["handler"] = lambda req, t: FakeResponse(json.dumps({"foo": "bar"}).encode())
r = conn_test.test_connection(CFG)
check("失败", r.ok, False)
check("说清是格式问题", "格式" in r.detail or "结构" in r.message, True)

print("[13] 配置没填全就别发请求")
for miss, expect in (("base_url", "地址"), ("api_key", "Key"), ("model", "模型")):
    cfg = dict(CFG)
    cfg[miss] = ""
    called = {"n": 0}

    def boom(req, t, _c=called):
        _c["n"] += 1
        return FakeResponse(b"{}")

    _state["handler"] = boom
    r = conn_test.test_connection(cfg)
    check(f"缺 {miss} 时不发请求", called["n"], 0)
    check(f"缺 {miss} 的提示", expect in r.message, True)

print("[14] Ollama 不用 key")
cfg = dict(CFG)
cfg.update({"api_format": "ollama", "base_url": "http://localhost:11434", "api_key": ""})
_state["handler"] = lambda req, t: FakeResponse(
    json.dumps({"message": {"content": "好"}}).encode())
r = conn_test.test_connection(cfg)
check("没 key 也能测", r.ok, True)

print("[15] 错误详情抽取")
check("OpenAI 风格", conn_test._extract_error_detail(
    '{"error":{"message":"boom"}}'), "boom")
check("message 风格", conn_test._extract_error_detail('{"message":"bang"}'), "bang")
check("纯文本", conn_test._extract_error_detail("just text"), "just text")
check("HTML", "网页" in conn_test._extract_error_detail("<html>x</html>"), True)
check("空", conn_test._extract_error_detail(""), "")

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
