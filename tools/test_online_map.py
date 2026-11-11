"""在线地图工具：地址/加载方式/天地图/Token/参数/请求头、瓦片与 WMS 渲染、工具库入口、图层登记。

不访问真实网络：`fetch_bytes` 被替换成一张本地生成的 PNG，专门验证
「URL 拼得对 + 图像画得到 + 图层能进 store/持久化」。
"""
from __future__ import annotations

import os
import sys
import time
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["GIS_AGENT_PERSIST"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QBuffer, QByteArray, QEventLoop, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

FAIL = []


def check(name: str, actual, expected=True) -> None:
    ok = actual == expected
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print(f"     实际={actual!r} 期望={expected!r}")
        FAIL.append(name)


def png_bytes(color: str, w: int = 256, h: int = 256) -> bytes:
    img = QImage(w, h, QImage.Format_RGBA8888)
    img.fill(QColor(color))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


app = QApplication([])

# ---------------------------------------------------------------- 纯逻辑
from app.online_map import (TOKEN_KEY_PRESETS, append_query_params, build_tile_url,
                            build_wms_url, method_label, method_short,
                            online_raster_info, parse_headers, parse_params,
                            prepare_config, validate_config, world_to_mercator)

print("[1] 表单校验与 URL 构造")
cfg = validate_config("xyz", "https://tile.openstreetmap.org/{z}/{x}/{y}.png", "OSM")
check("XYZ 配置", cfg["method"] == "xyz" and cfg["name"] == "OSM")
check("XYZ 瓦片 URL", build_tile_url(cfg["url"], 3, 6, 2),
      "https://tile.openstreetmap.org/3/6/2.png")
check("TMS {-y}", build_tile_url("https://a/{z}/{x}/{-y}.png", 2, 1, 0),
      "https://a/2/1/3.png")
check("WMTS 模板", build_tile_url("https://x/{TileMatrix}/{TileCol}/{TileRow}.png",
                                  2, 1, 2),
      "https://x/2/1/2.png")
check("子域", build_tile_url("https://{s}.x/{z}/{x}/{y}.png", 1, 1, 0),
      "https://b.x/1/1/0.png")
check("加载方式中文名", method_label("wmts"), "WMTS（瓦片模板）")
check("加载方式短名", method_short("wmts"), "WMTS")

merc = world_to_mercator((0.0, 0.0, 1.0, 1.0))
check("世界范围墨卡托", (round(merc[0]), round(merc[1]), round(merc[2]), round(merc[3])),
      (-20037508, -20037508, 20037508, 20037508))
wms = build_wms_url("https://example.com/wms?LAYERS=x:y", merc, 1000, 600)
check("WMS 带 BBOX", "BBOX=" in wms)
check("WMS 带 WIDTH", "WIDTH=1000" in wms)
check("WMS 带 SRS", "SRS=EPSG%3A3857" in wms)
check("WMS 强制 GetMap", "REQUEST=GetMap" in wms)
check("WMS 保留 LAYERS", "LAYERS=x%3Ay" in wms)
wms13 = build_wms_url("https://example.com/wms?VERSION=1.3.0&LAYERS=x:y", merc, 800, 500)
check("WMS 1.3.0 用 CRS", ("CRS=EPSG%3A3857" in wms13) and "SRS=" not in wms13)

print("[1b] 天地图（KVP WMTS）")
tdt_url = ("https://t{s}.tianditu.gov.cn/vec_w/wmts?SERVICE=WMTS&REQUEST=GetTile"
           "&VERSION=1.0.0&LAYER=vec&STYLE=default&TILEMATRIXSET=w"
           "&FORMAT=tiles&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&tk={token}")
cfg_tdt = validate_config("wmts", tdt_url, "天地图", token="KEY",
                          token_key="tk")
check("天地图最小层级", cfg_tdt["min_zoom"], 1)
check("天地图最大层级", cfg_tdt["max_zoom"], 18)
check("天地图数字子域", cfg_tdt.get("subdomains"), "01234567")
prep_tdt = prepare_config(cfg_tdt)
tdt_tile = build_tile_url(prep_tdt["url"], 1, 1, 0,
                          subdomains=prep_tdt["subdomains"])
check("天地图子域 t1", "t1.tianditu.gov.cn" in tdt_tile)
check("天地图层级替换", "TILEMATRIX=1" in tdt_tile)
check("天地图行列替换",
      "TILEROW=0" in tdt_tile and "TILECOL=1" in tdt_tile)
check("天地图 Token 替换", "tk=KEY" in tdt_tile)

try:
    validate_config("wmts",
                    "https://t0.tianditu.gov.cn/img_c/wmts/{z}/{x}/{y}",
                    "错误天地图", token="KEY", token_key="tk")
    check("天地图路径模板被拒绝", False)
except Exception:
    check("天地图路径模板被拒绝", True)

bad_c = ("https://t0.tianditu.gov.cn/img_c/wmts?SERVICE=WMTS&REQUEST=GetTile"
         "&LAYER=img&TILEMATRIXSET=c&TILEMATRIX={z}&TILEROW={y}"
         "&TILECOL={x}&tk={token}")
try:
    validate_config("wmts", bad_c, "经纬度天地图", token="KEY", token_key="tk")
    check("天地图 _c 矩阵被拒绝", False)
except Exception:
    check("天地图 _c 矩阵被拒绝", True)

bad_tk = ("https://t0.tianditu.gov.cn/vec_w/wmts?SERVICE=WMTS"
          "&REQUEST=GetTile&LAYER=vec&TILEMATRIXSET=w"
          "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}")
try:
    validate_config("wmts", bad_tk, "缺 tk")
    check("天地图缺 tk 被拒绝", False)
except Exception:
    check("天地图缺 tk 被拒绝", True)

print("[1c] Token / 自定义参数 / 自定义请求头")
check("Token 参数名预置", "access_token" in TOKEN_KEY_PRESETS)
check("参数多行解析", parse_params("style=dark\nlang=zh"),
      {"style": "dark", "lang": "zh"})
check("参数 & 解析", parse_params("?a=1&b=2"), {"a": "1", "b": "2"})
check("请求头解析", parse_headers("Authorization: Bearer x\nX-Test: 1"),
      {"Authorization": "Bearer x", "X-Test": "1"})

cfg_auth = validate_config(
    "xyz", "https://t/{z}/{x}/{y}.png?token={token}", "鉴权底图",
    token="SECRET", token_key="tk", params="lang=zh&theme=dark",
    headers="Authorization: Bearer {token}\nX-Test: 1")
check("Token 落进配置", (cfg_auth["token"], cfg_auth["token_key"]),
      ("SECRET", "tk"))
check("自定义参数落进配置", cfg_auth["params"],
      {"lang": "zh", "theme": "dark"})
check("Token 在请求头", cfg_auth["headers"]["Authorization"],
      "Bearer {token}")
prep = prepare_config(cfg_auth)
check("Token 替换地址占位符", "{token}" not in prep["url"])
check("已引用 Token 不重复追加", "tk" not in prep["params"])
check("Token 替换请求头占位符", prep["headers"]["Authorization"],
      "Bearer SECRET")
tile_auth = append_query_params(build_tile_url(prep["url"], 3, 6, 2),
                                prep["params"])
check("瓦片带自定义参数", "lang=zh" in tile_auth and "theme=dark" in tile_auth)
check("瓦片不重复带 Token", "tk=SECRET" not in tile_auth)

cfg_auto = validate_config("xyz", "https://t/{z}/{x}/{y}.png", "自动附加",
                           token="SECRET", token_key="tk")
prep_auto = prepare_config(cfg_auto)
check("Token 自动追加查询参数", prep_auto["params"].get("tk"), "SECRET")
tile_auto = append_query_params(build_tile_url(prep_auto["url"], 3, 6, 2),
                                prep_auto["params"])
check("瓦片带自动追加的 Token", "tk=SECRET" in tile_auto)

wms_auth = build_wms_url("https://example.com/wms", merc, 320, 200,
                         extra_params={"LAYERS": "x:y", "token": "SECRET"})
check("WMS 从自定义参数补 LAYERS", "LAYERS=x%3Ay" in wms_auth)
check("WMS 带自定义 Token", "token=SECRET" in wms_auth)
check("WMS 无地址 LAYERS 也可校验",
      validate_config("wms", "https://example.com/wms", "WMS",
                      params="LAYERS=x:y")["method"], "wms")

try:
    validate_config("xyz", "https://t/{z}/{x}/{y}.png?token={token}", "x")
    check("引用 Token 但未填写被拒绝", False)
except Exception:
    check("引用 Token 但未填写被拒绝", True)

try:
    validate_config("wms", "https://example.com/wms", "WMS")
    check("WMS 缺 LAYERS 被拒绝", False)
except Exception:
    check("WMS 缺 LAYERS 被拒绝", True)

try:
    validate_config("xyz", "https://x/tile.png", "x")
    check("缺占位符被拒绝", False)
except Exception:
    check("缺占位符被拒绝", True)

info = online_raster_info(cfg)
check("在线栅格无固定尺寸", (info.width, info.height), (0, 0))
check("在线栅格坐标系", info.crs, "EPSG:3857")
check("在线栅格驱动名", info.driver, "XYZ 瓦片")

# ---------------------------------------------------------------- 渲染
print("[2] 离线瓦片 / WMS 渲染")
import native.rasterview as rv

old_fetch = rv.fetch_bytes
try:
    tile = png_bytes("#3366cc")
    rv.fetch_bytes = lambda *a, **k: tile

    from app.raster import RasterLayer

    cfg_xyz = validate_config("xyz", "https://t/{z}/{x}/{y}.png", "瓦片")
    layer = RasterLayer(id="Rtest", name="瓦片", info=online_raster_info(cfg_xyz),
                        online=cfg_xyz)
    vp = rv.RasterViewPort((0, 0, 1, 1), 320, 200, 320.0, 0.0, 0.0, 1.0,
                           (0.0, 0.0, 1.0, 1.0))
    out = rv.render_layer_image(layer, vp)
    check("瓦片渲染返回图像", out is not None)
    check("瓦片图像尺寸", (out.width(), out.height()), (320, 200))
    check("瓦片像素", out.pixelColor(160, 100).name(), "#3366cc")

    # scale=512 对应 2×2 瓦片；确认不是等 4 张都到齐才回调。
    vp_tiles = rv.RasterViewPort((0, 0, 1, 1), 320, 200, 512.0, 0.0, 0.0,
                                 1.0, (0.0, 0.0, 1.0, 1.0))
    seen = []
    out_tiles = rv.render_layer_image(layer, vp_tiles, progress=seen.append)
    check("瓦片逐张回调", len(seen) >= 2)
    check("首块瓦片已先上屏",
          bool(seen) and seen[0].pixelColor(300, 180).alpha() == 0
          and seen[-1].pixelColor(300, 180).alpha() > 0)
    check("逐张回调最终完整", out_tiles is not None
          and out_tiles.pixelColor(300, 180).alpha() > 0)

    # 管线层：中间图必须走 on_partial，且不能写进完整帧缓存。
    stream_layer = RasterLayer(id="Rstream", name="流式瓦片",
                               info=online_raster_info(cfg_xyz),
                               online=cfg_xyz)
    pipe = rv.RasterPipeline()
    pipe.set_layers([stream_layer])
    pipe.set_visible({stream_layer.id: True})
    partials = []
    partial_cached = []
    finals = []
    loop = QEventLoop()

    def on_partial(lid, im):
        partials.append(im)
        partial_cached.append(lid in pipe._cache)
        check_view = pipe.view_of(lid) is not None
        if not check_view:
            partial_cached.append(True)

    def on_final(lid, im):
        finals.append(im)

    pipe.on_partial = on_partial
    pipe.on_layer = on_final
    pipe.on_frame = loop.quit
    pipe.request(vp_tiles)
    QTimer.singleShot(5000, loop.quit)
    loop.exec()
    pipe.shutdown()
    check("栅格管线逐块回调", len(partials) >= 2)
    check("栅格管线只回传一次最终图", len(finals), 1)
    check("中间图不写完整帧缓存", not any(partial_cached))

    # 本地栅格和在线瓦片分两条线程：本地层不能等瓦片下载完才开始。
    old_render = rv.render_layer_image
    events = []
    try:
        def fake_render(layer, _vp, cancel=None, progress=None):
            if layer.id == "Llocal":
                events.append(("local_start", time.monotonic()))
                time.sleep(0.20)
                events.append(("local_end", time.monotonic()))
                im = QImage(320, 200, QImage.Format_ARGB32)
                im.fill(QColor("#884400"))
                return im
            events.append(("online_start", time.monotonic()))
            im = QImage(320, 200, QImage.Format_ARGB32)
            im.fill(QColor("#2255aa"))
            for i in range(3):
                time.sleep(0.04)
                if progress is not None:
                    progress(im.copy())
            events.append(("online_end", time.monotonic()))
            return im

        rv.render_layer_image = fake_render
        local = types.SimpleNamespace(id="Llocal", rev=0, online=None)
        online = types.SimpleNamespace(
            id="Lonline", rev=0, online={"method": "xyz"})
        pipe2 = rv.RasterPipeline()
        pipe2.set_layers([local, online])
        pipe2.set_visible({"Llocal": True, "Lonline": True})
        frames = []
        loop2 = QEventLoop()
        pipe2.on_frame = lambda: (frames.append(1), loop2.quit())
        pipe2.request(vp_tiles)
        QTimer.singleShot(5000, loop2.quit)
        loop2.exec()
        pipe2.shutdown()
        local_start = next(t for name, t in events if name == "local_start")
        online_end = next(t for name, t in events if name == "online_end")
        check("本地栅格不被在线瓦片阻塞", local_start < online_end)
        check("本地+在线都完成才报整帧", len(frames), 1)
    finally:
        rv.render_layer_image = old_render

    cfg_wms = validate_config("wms", "https://example.com/wms?LAYERS=x:y", "WMS")
    layer2 = RasterLayer(id="Rtest2", name="WMS", info=online_raster_info(cfg_wms),
                         online=cfg_wms)
    vp2 = rv.RasterViewPort((0, 0, 1, 1), 320, 200, 320.0, 0.0, 0.0, 2.0,
                            (0.0, 0.0, 1.0, 1.0))
    out2 = rv.render_layer_image(layer2, vp2)
    check("WMS 渲染返回图像", out2 is not None)
    check("WMS dpr 保留", out2.devicePixelRatio(), 2.0)
    check("WMS 像素", out2.pixelColor(80, 50).name(), "#3366cc")

    calls = []
    def fake_auth(url, **kwargs):
        calls.append((url, kwargs.get("headers") or {}))
        return tile
    rv.fetch_bytes = fake_auth
    cfg_xyz_auth = validate_config(
        "xyz", "https://t/{z}/{x}/{y}.png", "鉴权瓦片",
        token="SECRET", token_key="", params="lang=zh",
        headers="X-Test: {token}")
    layer3 = RasterLayer(id="Rtest3", name="鉴权瓦片",
                         info=online_raster_info(cfg_xyz_auth),
                         online=cfg_xyz_auth)
    out3 = rv.render_layer_image(layer3, vp)
    check("带鉴权瓦片渲染", out3 is not None)
    check("请求带自定义参数", any("lang=zh" in u for u, _ in calls))
    check("请求不重复带 Token 参数", all("tk=SECRET" not in u for u, _ in calls))
    check("请求带自定义请求头",
          any(h.get("X-Test") == "SECRET" for _, h in calls))

    cfg_xyz_auto = validate_config(
        "xyz", "https://t/{z}/{x}/{y}.png", "自动 Token",
        token="SECRET", token_key="tk")
    layer5 = RasterLayer(id="Rtest5", name="自动 Token",
                         info=online_raster_info(cfg_xyz_auto),
                         online=cfg_xyz_auto)
    out5 = rv.render_layer_image(layer5, vp)
    check("自动 Token 瓦片渲染", out5 is not None)
    check("请求带自动 Token 参数",
          any("tk=SECRET" in u for u, _ in calls))

    cfg_wms_auth = validate_config(
        "wms", "https://example.com/wms", "鉴权 WMS", token="SECRET",
        token_key="token", params="LAYERS=x:y&lang=zh")
    layer4 = RasterLayer(id="Rtest4", name="鉴权 WMS",
                         info=online_raster_info(cfg_wms_auth),
                         online=cfg_wms_auth)
    out4 = rv.render_layer_image(layer4, vp2)
    check("带鉴权 WMS 渲染", out4 is not None)
    last_url, _ = calls[-1]
    check("WMS 自定义参数生效",
          "LAYERS=x%3Ay" in last_url and "lang=zh" in last_url)
    check("WMS Token 生效", "token=SECRET" in last_url)

    # 天地图：视口 scale=320 对应 z=0，但天地图没有 0 级，配置层应夹到 1
    layer6 = RasterLayer(id="Rtest6", name="天地图",
                         info=online_raster_info(cfg_tdt), online=cfg_tdt)
    out6 = rv.render_layer_image(layer6, vp)
    check("天地图渲染返回图像", out6 is not None)
    check("天地图请求层级夹到 1",
          any("TILEMATRIX=1" in u for u, _ in calls))
    check("天地图请求带 tk",
          any("tk=KEY" in u for u, _ in calls))
    check("天地图不用字母子域",
          all(".tianditu.gov.cn" in u and "ta.tianditu" not in u
              for u, _ in calls if "tianditu" in u))
finally:
    rv.fetch_bytes = old_fetch

# ---------------------------------------------------------------- 弹窗 / 工具入口
print("[3] 工具库入口与弹窗")
from native.app import MainWindow
from native.dialogs import OnlineMapDialog

win = MainWindow()
win._refresh_tools()
sections = []
for i in range(win.tool_box.count()):
    w = win.tool_box.itemAt(i).widget()
    if w is not None and hasattr(w, "_title"):
        labels = [b.text() for b in w.findChildren(QPushButton)]
        sections.append((w._title, labels))
check("工具库有底图入口", any(t == "底图" and any("在线地图" in s for s in labels)
                         for t, labels in sections))

dlg = OnlineMapDialog(win)
labels = [x.text() for x in dlg.findChildren(QLabel)]
check("弹窗有地址字段", "在线地图地址" in labels)
check("弹窗有加载方式", "加载方式" in labels)
check("弹窗有名称字段", "在线地图名称" in labels)
check("弹窗有 Token 参数名", "Token 参数名" in labels)
check("弹窗有 Token", "Token" in labels)
check("弹窗有自定义参数", "自定义参数" in labels)
check("弹窗有自定义请求头", "自定义请求头" in labels)
check("Token 默认隐藏", dlg.token_edit.echoMode(), QLineEdit.Password)
check("加载方式有 3 项", dlg.method_combo.count(), 3)
dlg.method_combo.setCurrentIndex(dlg.method_combo.findData("wms"))
dlg.url_edit.setText("https://example.com/wms?LAYERS=x:y")
dlg.name_edit.setText("我的底图")
dlg.token_key.setCurrentText("access_token")
dlg.token_edit.setText("TOKEN123")
dlg.params_edit.setPlainText("lang=zh")
dlg.headers_edit.setPlainText("Authorization: Bearer {token}")
cfg_dlg = dlg.config()
check("弹窗配置可读取", cfg_dlg["method"], "wms")
check("弹窗名称可读取", cfg_dlg["name"], "我的底图")
check("弹窗 Token 可读取", cfg_dlg["token"], "TOKEN123")
check("弹窗参数可读取", cfg_dlg["params"], {"lang": "zh"})
check("弹窗请求头可读取", cfg_dlg["headers"]["Authorization"],
      "Bearer {token}")
dlg.method_combo.setCurrentIndex(dlg.method_combo.findData("wmts"))
dlg.token_key.setCurrentText("token")
dlg.url_edit.setText(
    "https://t{s}.tianditu.gov.cn/vec_w/wmts?SERVICE=WMTS&REQUEST=GetTile"
    "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&tk={token}")
check("天地图地址自动带出 tk", dlg.token_key.currentText(), "tk")
dlg.deleteLater()

# ---------------------------------------------------------------- 图层登记
print("[4] 在线图层进入 store / 持久化描述")
old_fetch = rv.fetch_bytes
try:
    rv.fetch_bytes = lambda *a, **k: png_bytes("#88aa66")
    win.add_online_map({
        "method": "xyz", "url": "https://t/{z}/{x}/{y}.png?token=SECRET_TOKEN",
        "name": "在线测试", "min_zoom": 0, "max_zoom": 19,
        "token": "SECRET_TOKEN", "token_key": "tk",
        "params": {"lang": "zh"}, "headers": {"X-Test": "1"}})
    layers = list(win.store.layers.values())
    check("只登记一个图层", len(layers), 1)
    lay = layers[0]
    check("是在线图层", bool(getattr(lay, "online", None)))
    check("在线图层在栈底", list(win.store.layers)[0], lay.id)
    check("图层可见", win.visible.get(lay.id), True)
    row = win.layer_list.itemWidget(win.layer_list.item(0))
    row_texts = [x.text() for x in row.findChildren(QLabel)]
    check("图层面板显示在线", any("在线地图" in t for t in row_texts))
    desc = win._layer_descriptors()[0]
    check("描述含 online", (desc.get("online") or {}).get("method"), "xyz")
    check("描述路径是 URL", desc.get("path"),
          "https://t/{z}/{x}/{y}.png?token=SECRET_TOKEN")
    check("描述含 Token", (desc.get("online") or {}).get("token"),
          "SECRET_TOKEN")
    check("描述含自定义参数",
          (desc.get("online") or {}).get("params"), {"lang": "zh"})
    check("描述含自定义请求头",
          (desc.get("online") or {}).get("headers"), {"X-Test": "1"})

    # 图层信息只显示“Token 已配置”，不能把值带进 UI/截图/聊天
    import native.app as appmod
    captured = []
    orig_info = appmod.QMessageBox.information
    appmod.QMessageBox.information = staticmethod(
        lambda *args: captured.append(args[2]))
    try:
        win.open_table(lay.id)
    finally:
        appmod.QMessageBox.information = orig_info
    info_text = captured[0] if captured else ""
    check("图层信息显示参数数", "自定义参数：1 项" in info_text, True)
    check("图层信息显示请求头数", "自定义请求头：1 项" in info_text, True)
    check("图层信息显示 Token 已配置", "Token：已配置" in info_text, True)
    check("图层信息不泄露 Token 值", "SECRET_TOKEN" not in info_text, True)
finally:
    rv.fetch_bytes = old_fetch
    win.close()

print()
if FAIL:
    print(f"失败 {len(FAIL)} 项：", FAIL)
    sys.stdout.flush()
    os._exit(1)
print("全部通过")
sys.stdout.flush()
os._exit(0)
