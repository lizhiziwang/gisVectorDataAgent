"""在线地图（XYZ / WMTS REST / WMS）的配置、鉴权与 URL 构造。

只做**纯逻辑**：校验表单、解析 Token/自定义查询参数/自定义请求头，
把瓦片坐标或视口范围拼成请求 URL、返回合成用的 `RasterInfo`。
真正下载和画图在 `native/rasterview.py` 的栅格渲染线程里做，
这样网络慢不会卡住 Qt 主线程。

在线底图不是 GDAL 数据源，不能走 `rasterio.open()`。图层仍然复用
`RasterLayer`（面板、显隐、缩放定位都能用），但 `RasterLayer.online` 非空时
渲染器走这里的瓦片/单张 WMS 分支，不访问 `info.path`。
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

#: 加载方式 → 界面中文名。顺序就是弹窗下拉框的顺序。
METHOD_LABELS: dict[str, str] = {
    "xyz": "XYZ 瓦片",
    "wmts": "WMTS（瓦片模板）",
    "wms": "WMS",
}

#: Token 参数名的常见写法（下拉框可编辑，不限于这些）。
TOKEN_KEY_PRESETS = ("token", "access_token", "key", "tk", "apikey")

#: 一次请求最多读多少字节，防止服务器返回一整个文件把渲染线程拖死。
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
#: 网络超时（秒）。瓦片正常几十毫秒；6 秒已经足够区分“没网/被墙”。
FETCH_TIMEOUT = 6.0

#: 墨卡托半周长（EPSG:3857，米）
MERCATOR_R = 20037508.342789244


class OnlineMapError(ValueError):
    """表单/配置错误，消息可以直接显示给用户。"""


def normalize_method(method: str) -> str:
    m = str(method or "").strip().lower()
    if m in ("xyz", "tile", "tiles"):
        return "xyz"
    if m == "wmts":
        return "wmts"
    if m in ("wms", "wms-t", "getmap"):
        return "wms"
    raise OnlineMapError("加载方式只支持 XYZ 瓦片、WMTS（瓦片模板）或 WMS。")


def parse_params(value) -> dict[str, str]:
    """解析自定义查询参数。

    支持三种输入：`key=value` 多行文本、`dict`、`[(k, v), ...]`。
    文本里也可以用 `&` 连接；空行和 `#` 注释会被忽略。
    """
    if not value:
        return {}
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for k, v in value.items():
            key = str(k or "").strip()
            if key:
                out[key] = str(v if v is not None else "")
        return out
    if isinstance(value, (list, tuple)):
        out = {}
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                key, val = str(item[0] or "").strip(), str(item[1] or "")
                if key:
                    out[key] = val
            elif isinstance(item, str) and "=" in item:
                key, val = item.split("=", 1)
                key = key.strip()
                if key:
                    out[key] = val.strip()
        return out

    out = {}
    for raw_line in str(value).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("?"):
            line = line[1:]
        for chunk in line.split("&"):
            item = chunk.strip()
            if not item:
                continue
            if "=" not in item:
                raise OnlineMapError(
                    f"自定义参数「{item}」缺少等号，格式应为 key=value。")
            key, val = item.split("=", 1)
            key = key.strip()
            if not key:
                raise OnlineMapError("自定义参数名不能为空。")
            out[key] = val.strip()
    return out


def parse_headers(value) -> dict[str, str]:
    """解析自定义 HTTP 请求头，格式为每行 `Name: value`。"""
    if not value:
        return {}
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for k, v in value.items():
            key = str(k or "").strip()
            if key:
                val = str(v if v is not None else "")
                if "\r" in val or "\n" in val:
                    raise OnlineMapError(f"请求头「{key}」的值不能包含换行。")
                out[key] = val
        return out
    if isinstance(value, (list, tuple)):
        out = {}
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                key, val = str(item[0] or "").strip(), str(item[1] or "")
                if key:
                    if "\r" in val or "\n" in val:
                        raise OnlineMapError(f"请求头「{key}」的值不能包含换行。")
                    out[key] = val
        return out

    out = {}
    for raw_line in str(value).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise OnlineMapError(
                f"请求头「{line}」缺少冒号，格式应为 Name: value。")
        key, val = line.split(":", 1)
        key = key.strip()
        if not key:
            raise OnlineMapError("请求头名称不能为空。")
        out[key] = val.strip()
    return out


def _subst_token(value, token: str) -> str:
    return str(value if value is not None else "").replace("{token}", token)


def prepare_config(cfg: dict) -> dict:
    """把配置展开成一次渲染可直接使用的形态。

    · 地址/参数/请求头里的 `{token}` 替换成 Token 值；
    · 没有用 `{token}` 引用时，Token 才按 `token_key` 追加成查询参数；
    · 参数和请求头统一成 dict，兼容旧会话里的字符串/空值。
    """
    out = dict(cfg or {})
    token = str(out.get("token") or "").strip()
    token_key = str(out.get("token_key") if out.get("token_key") is not None
                    else "token").strip()
    raw_url = str(out.get("url") or "")
    params = parse_params(out.get("params") or {})
    headers = parse_headers(out.get("headers") or {})
    # Token 已经在地址/参数/请求头里以 {token} 引用时，不再额外追加查询参数。
    # 这样「Authorization: Bearer {token}」不会把同一个 Token 又写进 URL
    # （URL 更容易进代理/服务端访问日志）；需要两者都发时在自定义参数里显式写。
    token_referenced = (
        "{token}" in raw_url
        or any("{token}" in str(k) or "{token}" in str(v)
               for k, v in params.items())
        or any("{token}" in str(k) or "{token}" in str(v)
               for k, v in headers.items())
    )

    out["url"] = _subst_token(raw_url, token)
    params = {_subst_token(k, token): _subst_token(v, token)
              for k, v in params.items()}
    headers = {_subst_token(k, token): _subst_token(v, token)
               for k, v in headers.items()}
    if token and token_key and not token_referenced:
        params[token_key] = token

    out["params"] = params
    out["headers"] = headers
    out["token"] = token
    out["token_key"] = token_key
    return out


def is_tianditu_host(hostname: str) -> bool:
    h = str(hostname or "").lower().rstrip(".")
    return bool(h) and (h == "tianditu.gov.cn" or h.endswith(".tianditu.gov.cn"))


def _zoom_defaults(hostname: str) -> tuple[int, int, str | None]:
    """不同底图服务的层级范围/子域习惯。

    天地图是 KVP WMTS，`TILEMATRIX` 从 1 开始（没有 0 级），子域是
    `t0`…`t7` 而不是 OSM 的 `a/b/c`。识别到天地图域名时直接按它的规则来，
    避免首屏请求一个不存在的 0 级瓦片。
    """
    if is_tianditu_host(hostname):
        return 1, 18, "01234567"
    return 0, 19, None


def _validate_tianditu_url(url: str, token: str, token_key: str) -> None:
    """天地图不是 REST 路径模板，必须带 WMTS KVP 参数。

    这个校验专门拦住“https://t0.tianditu.gov.cn/img_c/wmts/{z}/{x}/{y}”
    这类看起来像瓦片地址、实际服务器会返回 404/空内容的写法；否则图层
    静默空白，很难看出是地址问题。
    """
    parts = urlsplit(str(url))
    path = parts.path.lower()
    query = {k.upper(): v for k, v in
             parse_qsl(parts.query, keep_blank_values=True)}

    if "/wmts/" in path and re.search(r"/wmts/\{z\}/\{x\}/\{y\}/?$", path):
        raise OnlineMapError(
            "天地图不是 REST 路径模板，不能用 /wmts/{z}/{x}/{y}。\n"
            "请用 KVP 地址，例如：\n"
            "https://t{s}.tianditu.gov.cn/vec_w/wmts?SERVICE=WMTS"
            "&REQUEST=GetTile&VERSION=1.0.0&LAYER=vec&STYLE=default"
            "&TILEMATRIXSET=w&FORMAT=tiles&TILEMATRIX={z}"
            "&TILEROW={y}&TILECOL={x}&tk={token}")

    if re.search(r"/[a-z]+_c/", path) or query.get("TILEMATRIXSET", "").strip().lower() == "c":
        raise OnlineMapError(
            "当前在线渲染使用 Web Mercator（EPSG:3857），天地图请用 _w 系列"
            "（如 img_w/vec_w）且 TILEMATRIXSET=w，不要用 _c/TILEMATRIXSET=c。")

    required = ("SERVICE", "REQUEST", "LAYER", "TILEMATRIX",
                "TILEROW", "TILECOL", "TILEMATRIXSET")
    missing = [k for k in required if k not in query]
    if missing:
        raise OnlineMapError(
            "天地图缺少 WMTS 参数：" + "、".join(missing) + "。\n"
            "必须是 KVP 地址，不能只写 /wmts/{z}/{x}/{y}；"
            "可参考天地图 WMTS 模板（TILEMATRIX={z}&TILEROW={y}&TILECOL={x}）。")

    if query.get("SERVICE", "").strip().upper() != "WMTS" or \
            query.get("REQUEST", "").strip().upper() != "GETTILE":
        raise OnlineMapError("天地图地址的 SERVICE 必须是 WMTS，REQUEST 必须是 GetTile。")
    if query.get("TILEMATRIXSET", "").strip().lower() != "w":
        raise OnlineMapError("天地图请使用 TILEMATRIXSET=w（Web Mercator）。")

    for key, placeholder in (("TILEMATRIX", "{z}"), ("TILEROW", "{y}"),
                             ("TILECOL", "{x}")):
        value = str(query.get(key) or "")
        if placeholder not in value and not value.isdigit():
            raise OnlineMapError(
                f"天地图地址的 {key} 要写成 {placeholder} 占位符，当前是「{value}」。")

    tk_present = bool(str(query.get("TK") or "").strip())
    if not tk_present and not token:
        raise OnlineMapError("天地图地址需要 tk 参数（在弹窗的 Token 里填天地图 Key）。")
    if not tk_present and token and token_key.lower() != "tk":
        raise OnlineMapError("天地图的 Token 参数名要填 tk，不能是「"
                             f"{token_key or '空'}」。")


def validate_config(method: str, url: str, name: str, token: str = "",
                    token_key: str = "token", params="", headers="") -> dict:
    """校验并返回规范化的配置；失败抛 `OnlineMapError`。"""
    m = normalize_method(method)
    u = str(url or "").strip()
    n = str(name or "").strip()
    token = str(token or "").strip()
    if "\r" in token or "\n" in token:
        raise OnlineMapError("Token 不能包含换行。")
    token_key = str(token_key if token_key is not None else "token").strip()
    p = parse_params(params)
    h = parse_headers(headers)
    if not n:
        raise OnlineMapError("请填写在线地图名称。")
    if len(n) > 80:
        raise OnlineMapError("在线地图名称太长（最多 80 个字符）。")
    if not re.match(r"^https?://", u, re.IGNORECASE):
        raise OnlineMapError("在线地图地址必须以 http:// 或 https:// 开头。")
    if "\r" in u or "\n" in u:
        raise OnlineMapError("在线地图地址不能包含换行。")
    try:
        parsed = urlsplit(u.replace("{token}", token or "token"))
        host = parsed.netloc
        hostname = parsed.hostname or ""
    except Exception as exc:  # noqa: BLE001
        raise OnlineMapError("在线地图地址格式不正确。") from exc
    if not host:
        raise OnlineMapError("在线地图地址缺少主机名（例如 tile.openstreetmap.org）。")
    min_zoom, max_zoom, subdomains = _zoom_defaults(hostname)

    token_used = (
        "{token}" in u
        or any("{token}" in str(k) or "{token}" in str(v)
               for k, v in p.items())
        or any("{token}" in str(k) or "{token}" in str(v)
               for k, v in h.items())
    )
    if token_used and not token:
        raise OnlineMapError("地址/参数/请求头里用了 {token}，请填写 Token。")
    if token and not token_key and not token_used:
        raise OnlineMapError(
            "Token 参数名为空，且没有在地址/参数/请求头里写 {token}；"
            "Token 将不会被发送。")

    if m in ("xyz", "wmts") and is_tianditu_host(hostname):
        _validate_tianditu_url(u, token, token_key)

    if m in ("xyz", "wmts"):
        has_z = "{z}" in u or "{TileMatrix}" in u
        has_x = "{x}" in u or "{TileCol}" in u
        has_y = "{y}" in u or "{-y}" in u or "{TileRow}" in u
        if not (has_z and has_x and has_y):
            raise OnlineMapError(
                "瓦片地址要包含 {z}/{x}/{y}（或 WMTS 的 "
                "{TileMatrix}/{TileCol}/{TileRow}）。\n"
                "例如：https://tile.openstreetmap.org/{z}/{x}/{y}.png")
    else:
        # WMS 地址可以完整带 LAYERS/STYLES，也可以只给服务地址；渲染时
        # 自动补 SERVICE/REQUEST/WIDTH/HEIGHT/BBOX 等参数。
        allowed = {"{bbox}", "{width}", "{height}", "{srs}", "{crs}", "{token}"}
        placeholders = set(re.findall(r"\{[^{}]+\}", u))
        unknown = placeholders - allowed
        if unknown:
            raise OnlineMapError(
                "WMS 地址里的花括号只支持 {bbox}、{width}、{height}、"
                "{srs}/{crs}、{token}；不支持：" + "、".join(sorted(unknown)))
        try:
            keys = {k.upper() for k, _ in parse_qsl(urlsplit(u).query,
                                                    keep_blank_values=True)}
        except Exception:  # noqa: BLE001
            keys = set()
        keys.update(k.upper() for k in p)
        if "LAYERS" not in keys:
            raise OnlineMapError(
                "WMS 地址或自定义参数里要带 LAYERS，例如 LAYERS=workspace:layer。")

    out = {
        "method": m,
        "url": u,
        "name": n,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "token": token,
        "token_key": token_key,
        "params": p,
        "headers": h,
    }
    if subdomains:
        out["subdomains"] = subdomains
    return out


def method_label(method: str) -> str:
    try:
        return METHOD_LABELS[normalize_method(method)]
    except Exception:
        return str(method or "在线地图")


def method_short(method: str) -> str:
    """图层行/图例用的短名，避免 240px 的面板里把文字挤出去。"""
    try:
        m = normalize_method(method)
    except Exception:
        return str(method or "在线地图")
    return {"xyz": "XYZ", "wmts": "WMTS", "wms": "WMS"}.get(m, m.upper())


def build_tile_url(template: str, z: int, x: int, y: int,
                   subdomains: str = "abc") -> str:
    """把模板替换成一次瓦片请求。

    同时兼容常见写法：
    · `{z}/{x}/{y}`；
    · TMS 的 `{-y}`（Y 轴从南往北）；
    · WMTS REST 的 `{TileMatrix}/{TileCol}/{TileRow}`；
    · `{s}` 子域轮询。
    """
    u = str(template)
    z = int(z)
    x = int(x)
    y = int(y)
    # {-y} 必须先替换，避免被后面的 {y} 规则干扰（两者不是同一个占位符，
    # 但保持在所有替换前更直观）。
    u = u.replace("{-y}", str((1 << z) - 1 - y))
    subs = str(subdomains or "abc")
    if "{s}" in u and subs:
        u = u.replace("{s}", subs[(x + y) % len(subs)])
    u = (u.replace("{z}", str(z))
          .replace("{x}", str(x))
          .replace("{y}", str(y))
          .replace("{TileMatrix}", str(z))
          .replace("{TileCol}", str(x))
          .replace("{TileRow}", str(y)))
    return u


def world_to_mercator(world: tuple[float, float, float, float]) -> tuple[float, ...]:
    """Web Mercator 归一化世界矩形 → EPSG:3857 米。"""
    x0, y0, x1, y1 = (float(v) for v in world)
    # 归一化 y 向下增大；EPSG:3857 的 y 向上增大，所以上下翻转。
    left = max(-MERCATOR_R, min(MERCATOR_R, x0 * 2 * MERCATOR_R - MERCATOR_R))
    right = max(-MERCATOR_R, min(MERCATOR_R, x1 * 2 * MERCATOR_R - MERCATOR_R))
    top = max(-MERCATOR_R, min(MERCATOR_R, MERCATOR_R - y0 * 2 * MERCATOR_R))
    bottom = max(-MERCATOR_R, min(MERCATOR_R, MERCATOR_R - y1 * 2 * MERCATOR_R))
    return (min(left, right), min(bottom, top),
            max(left, right), max(bottom, top))


def _set_param(pairs: list[tuple[str, str]], key: str, value: str) -> None:
    """按参数名（不区分大小写）替换现有项，没有就追加。"""
    for i, (k, _v) in enumerate(pairs):
        if k.upper() == key.upper():
            pairs[i] = (k, value)
            return
    pairs.append((key, value))


def _replace_param(pairs: list[tuple[str, str]], key: str) -> None:
    pairs[:] = [(k, v) for k, v in pairs if k.upper() != key.upper()]


def append_query_params(url: str, params) -> str:
    """把自定义参数合并进 URL 查询串（同名参数覆盖地址里的值）。"""
    extra = parse_params(params)
    if not extra:
        return str(url)
    parts = urlsplit(str(url))
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    for k, v in extra.items():
        _set_param(pairs, k, v)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(pairs, doseq=True), parts.fragment))


def build_wms_url(template: str, bbox_mercator: tuple[float, ...],
                  width: int, height: int, extra_params=None) -> str:
    """为当前视口构造 WMS GetMap URL。

    用户可以把 LAYERS/STYLES 直接写在地址里，也可以放进 `extra_params`
    （弹窗里的「自定义参数」）；这里只覆盖每次视图都会变的
    BBOX/WIDTH/HEIGHT/SRS(CRS)，保留其他参数。也支持显式的
    `{bbox}` / `{width}` / `{height}` / `{srs}` 占位符。
    """
    bbox = (float(bbox_mercator[0]), float(bbox_mercator[1]),
            float(bbox_mercator[2]), float(bbox_mercator[3]))
    w = max(1, min(2048, int(width)))
    h = max(1, min(2048, int(height)))
    bbox_s = ",".join(f"{v:.6f}" for v in bbox)

    u = str(template or "")
    has_placeholders = any(k in u for k in ("{bbox}", "{width}", "{height}",
                                            "{srs}", "{crs}"))
    if has_placeholders:
        u = (u.replace("{bbox}", bbox_s)
              .replace("{width}", str(w))
              .replace("{height}", str(h))
              .replace("{srs}", "EPSG:3857")
              .replace("{crs}", "EPSG:3857"))
    parts = urlsplit(u)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    # 自定义参数放在地址参数之后：同名时以弹窗里填写的值为准。
    for k, v in parse_params(extra_params).items():
        _set_param(pairs, k, v)

    # 默认参数只在用户没写时补；REQUEST/SERVICE 明确写成 GetMap，避免
    # 用户贴的是 GetCapabilities 地址。
    defaults = {
        "SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.1.1",
        "FORMAT": "image/png", "TRANSPARENT": "TRUE", "STYLES": "",
    }
    present = {k.upper() for k, _ in pairs}
    for k, v in defaults.items():
        if k not in present:
            pairs.append((k, v))
    # 即使用户贴的是 GetCapabilities 地址，这里要的也是地图图片。
    _set_param(pairs, "SERVICE", "WMS")
    _set_param(pairs, "REQUEST", "GetMap")

    version = next((v for k, v in pairs if k.upper() == "VERSION"), "1.1.1")
    _set_param(pairs, "BBOX", bbox_s)
    _set_param(pairs, "WIDTH", str(w))
    _set_param(pairs, "HEIGHT", str(h))
    if str(version).strip() == "1.3.0":
        _set_param(pairs, "CRS", "EPSG:3857")
        _replace_param(pairs, "SRS")
    else:
        _set_param(pairs, "SRS", "EPSG:3857")
        _replace_param(pairs, "CRS")
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(pairs, doseq=True), parts.fragment))


def fetch_bytes(url: str, timeout: float = FETCH_TIMEOUT,
                max_bytes: int = MAX_RESPONSE_BYTES,
                headers: dict | None = None) -> bytes | None:
    """后台线程用的同步 HTTP GET；失败返回 None，不把异常抛到渲染线程。

    `headers` 是用户在弹窗里填的自定义请求头（Token 可写在 Authorization 里），
    会覆盖同名的默认 UA/Referer。
    """
    try:
        parts = urlsplit(str(url))
        referer = (f"{parts.scheme}://{parts.netloc}/"
                   if parts.scheme and parts.netloc else "")
        req_headers = {
            # 有些瓦片服务默认拒绝 Python-urllib；带一个常见 UA 和同源 Referer。
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36",
            "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
        }
        if referer:
            req_headers["Referer"] = referer
        for k, v in parse_headers(headers or {}).items():
            # HTTP 头名不区分大小写：先删掉同名旧项，再写用户的值。
            for old in [x for x in req_headers if x.lower() == k.lower()]:
                req_headers.pop(old, None)
            req_headers[k] = v
        req = Request(str(url), headers=req_headers)
        with urlopen(req, timeout=float(timeout)) as resp:
            data = resp.read(int(max_bytes) + 1)
        if not data or len(data) > int(max_bytes):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def online_raster_info(cfg: dict):
    """给在线底图造一个不会读盘的 `RasterInfo`。

    宽高为 0 是有意的：在线底图没有固定像元数，面板会显示「在线地图」而不是
    `0×0`；真正渲染由 `online` 分支按视口取瓦片。
    """
    from .raster import RasterInfo

    bounds = (-180.0, -85.05112878, 180.0, 85.05112878)
    return RasterInfo(
        path=str(cfg.get("url") or ""),
        width=0, height=0, bands=3,
        dtypes=["uint8"], crs="EPSG:3857",
        crs_name="Web Mercator (EPSG:3857)",
        nodata=None, bounds=bounds, res=(0.0, 0.0),
        band_desc=["", "", ""],
        driver=method_label(cfg.get("method") or "xyz"),
        overviews=(),
    )
