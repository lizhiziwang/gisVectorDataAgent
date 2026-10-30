"""模型连通性测试：发一个最小请求，判断配置能不能用。

和 `Planner._call_llm` 分开，因为目的不同：
· 正常调用要的是「拿到计划」，失败就回退规则引擎，用户不必知道细节；
· 连通性测试要的是「**明确告诉用户哪里错了**」——
  密钥没权限、模型名拼错、地址打不通、格式不对，各有不同的说法。

所以这里自己发请求、自己解释错误，不复用规划器那套「统一当失败处理」的逻辑。
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request

from . import api_formats

#: 连通性检查用的小请求：够短、够快，也不浪费额度
_PROBE = "回复「好」一个字"


class TestResult:
    """一次连通性测试的结果。"""

    def __init__(self, ok: bool, message: str, detail: str = "",
                 elapsed: float = 0.0, reply: str = ""):
        self.ok = ok
        self.message = message
        self.detail = detail
        self.elapsed = elapsed
        self.reply = reply

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TestResult ok={self.ok} msg={self.message!r}>"


def _extract_error_detail(raw: str) -> str:
    """把服务端返回的 JSON 错误抠成人能看懂的一句话。

    各家错误结构不一样：
    · OpenAI 系 {"error": {"message": "..."}}
    · Anthropic {"error": {"message": "..."}}
    · Gemini {"error": {"message": "..."}}
    · 一些网关 {"message": "..."} 或 {"msg": "..."} 或纯文本
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except Exception:
        # 不是 JSON，就截断原文（去掉可能的 HTML 错误页）
        if raw.startswith("<"):
            return "服务端返回了网页而不是接口数据（地址可能填错了）"
        return raw[:300]
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg") or err.get("code")
            if msg:
                code = err.get("code")
                return f"{msg}" + (f"（{code}）" if code and str(code) not in str(msg) else "")
        if isinstance(err, str):
            return err
        for k in ("message", "msg", "detail", "reason"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v[:300]
    return raw[:300]


def _network_reason(reason) -> str:
    """把底层网络异常翻译成人话。

    urllib 会把各种底层错误包成 URLError，reason 里可能是
    ssl.SSLError、socket.gaierror、ConnectionRefusedError……
    直接甩给用户看没人看得懂。

    注意 gaierror 的 str 形如 "(-2, 'Name or service not known')"，
    errno 在前、消息在后，所以要同时看 errno 和消息。
    """
    s = str(reason or "")
    low = s.lower()
    errno = getattr(reason, "errno", None)
    if errno in (-2, -3, -5) or "name or service not known" in low \
            or "nodename nor servname" in low or "getaddrinfo failed" in low:
        return "域名解析不了（主机名不存在或 DNS 有问题）"
    if errno == 111 or "connection refused" in low:
        return "连接被拒绝（地址能解析，但那个端口没人监听）"
    if "timed out" in low or "timeout" in low or errno == 110:
        return "连接超时（网络不通或被防火墙挡了）"
    if "ssl" in low or "certificate" in low or "unexpected_eof" in low \
            or "eof occurred" in low or "wrong_version" in low:
        return "HTTPS 握手失败（证书或协议对不上）"
    if "no route to host" in low or errno == 101:
        return "网络不通（没有到目标主机的路由）"
    if "reset by peer" in low or errno == 104:
        return "连接被对方断开"
    if "network is unreachable" in low or errno == 101:
        return "网络不可达"
    return f"连不上服务器：{s}" if s else "连不上服务器"


def _explain(status: int, detail: str, fmt: str) -> tuple[str, str]:
    """按状态码给出「问题是什么 + 怎么办」。

    返回 (标题, 建议)。标题直接给用户看，建议是排查方向。
    「建议」里要带上服务端原话 —— 用户看到「无权访问模型 MiniMax」才知道
    该改哪个模型名，光说「没权限」他不知道要去哪查。
    """
    d = (detail or "").strip()
    if status == 401:
        advice = "检查 API Key 是否复制完整（别带空格）、是否已过期、是否用错了厂商的 Key。"
        return ("鉴权失败：API Key 无效或没填对",
                advice + (f" 服务端说：{d}" if d else ""))
    if status == 403:
        if "model" in d.lower() or "模型" in d:
            return ("没有访问该模型的权限",
                    "这个 Key 没被授权访问当前模型。换一个有权限的模型名，"
                    "或问服务商开通。" + (f" 服务端说：{d}" if d else ""))
        return ("没有访问权限",
                "Key 有效但没有该接口/模型的权限。可以先换个模型名试试。"
                + (f" 服务端说：{d}" if d else ""))
    if status == 404:
        return ("接口地址不对（404）",
                "地址或端点路径拼错了。检查 Base URL，"
                "以及「接口格式」是否选对了（比如 Claude 要 /messages 不是 /chat/completions）。")
    if status == 429:
        return ("请求太频繁或额度用完（429）",
                "稍后再试，或检查账户余额/额度。")
    if status in (400, 422):
        return ("请求被拒绝（%d）" % status,
                "参数或格式不被接受。检查「接口格式」和模型名；"
                "自建网关可能不支持 JSON 模式。" + (f" 服务端说：{d}" if d else ""))
    if status >= 500:
        return ("服务端出错（%d）" % status,
                "这是对方服务的问题，不是你填错了。稍后再试。" + (f" 服务端说：{d}" if d else ""))
    if d:
        return (f"请求失败（HTTP {status}）", f"服务端说：{d}")
    return (f"请求失败（HTTP {status}）", "换个地址或格式再试。")


def test_connection(cfg: dict, timeout: int = 12) -> TestResult:
    """发一个最小请求，返回测试结果。**不会**改写 `_llm_fail_at` 之类的全局状态。

    连通性测试不该影响后续正常调用的退避逻辑 —— 测试失败不等于服务不可用，
    可能只是这次填错了。
    """
    t0 = time.time()
    fmt = (cfg.get("api_format") or api_formats.DEFAULT_FORMAT).strip() or api_formats.DEFAULT_FORMAT
    try:
        url, headers, payload = api_formats.build_request(
            cfg, [{"role": "user", "content": _PROBE}], False, 16)
    except Exception as e:  # noqa: BLE001 - 配置本身有问题
        return TestResult(False, "配置有误，请求根本没发出去", str(e),
                          time.time() - t0)

    # 缺地址 / 缺 key 这类问题要在发请求前就说清楚，别让用户等一个网络超时
    if not (cfg.get("base_url") or "").strip():
        return TestResult(False, "还没填接口地址", "Base URL 不能为空。",
                          time.time() - t0)
    if not (cfg.get("api_key") or "").strip() and fmt != "ollama":
        return TestResult(False, "还没填 API Key",
                          "除本地 Ollama 外都需要 Key。", time.time() - t0)
    if not (cfg.get("model") or "").strip():
        return TestResult(False, "还没填模型名称",
                          "模型名填错会返回 403/404。", time.time() - t0)

    body = api_formats.dumps(payload)
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        data = json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        detail = _extract_error_detail(raw)
        title, advice = _explain(int(e.code), detail, fmt)
        return TestResult(False, title, advice or detail, time.time() - t0)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return TestResult(False, _network_reason(reason),
                          "检查地址、端口、网络；本机服务要确认已经启动。",
                          time.time() - t0)
    except TimeoutError:
        return TestResult(False, f"超时（{timeout} 秒没响应）",
                          "地址可能指向了不可达的主机，或服务太慢。",
                          time.time() - t0)
    except ssl.SSLError as e:
        return TestResult(False, f"HTTPS 握手失败：{e.reason if hasattr(e, 'reason') else e}",
                          "证书或协议不匹配。自签名证书可以试试 http://，"
                          "或者确认地址里的 https:// 写对了。",
                          time.time() - t0)
    except json.JSONDecodeError:
        return TestResult(False, "服务端返回的不是 JSON",
                          "地址可能填成了网页地址；正确地址应该返回 JSON。",
                          time.time() - t0)
    except Exception as e:  # noqa: BLE001
        return TestResult(False, f"请求出错：{e}", "", time.time() - t0)

    # 通了还得看响应结构对不对 —— 有些网关会返回 200 + 错误对象
    try:
        content, _finish = api_formats.parse_response(fmt, data)
    except Exception as e:  # noqa: BLE001
        return TestResult(False, "连上了，但响应结构不对",
                          f"按「{api_formats.FORMAT_NAMES.get(fmt, fmt)}」解析不出来：{e}\n"
                          "接口格式可能选错了。", time.time() - t0)

    reply = (content or "").strip()
    if not reply:
        return TestResult(True, "连接成功，但模型返回了空内容",
                          "接口是通的，可以再试试正常发指令。", time.time() - t0)
    return TestResult(True, "连接成功", reply[:120], time.time() - t0, reply)
