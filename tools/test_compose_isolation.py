"""合成层的两条硬约束（栅格 + 矢量同帧时必须成立）。

现场：载入真实大影像（3 波段、EPSG:4490、1.8 GB）后，只要平移/缩放，
控制台开始刷

    TypeError: 'numpy.bool' object cannot be interpreted as an integer
      native/mapview.py  p.setRenderHint(..., abs(r - 1.0) > 1e-9)
    QPainter::begin: A paint device can only be painted by one painter at a time.

屏幕上的矢量整片消失、只剩竖条纹。两个根因都在 `_compose()` 这条两条管线
**共用**的合成路径上：

1. `fit_bounds()` 直接吃 `gdf.total_bounds` 的 numpy.float64，于是
   `MapView.scale/tx/ty` 也成了 numpy 标量；它经 `RasterViewPort` /
   `_layer_views` 流进 `_blit_view_image()` 后，`abs(scale/s0 - 1) > 1e-9`
   得到的是 `numpy.bool_`。`QPainter.setRenderHint()` 只接受 bool/int，抛
   TypeError —— 栅格在合成顺序里先画，异常一抛，后面的矢量一层都画不到。
2. 异常发生在 `p.save()` 之后、`p.restore()` 之前，画笔没被收掉；后续合成
   全部报 "paint device can only be painted by one painter at a time"。

本测试锁住三件事：
· fit_bounds 必须把 numpy 标量归一成 Python float；
· 旧视口图（numpy 标量）合成时不能抛异常，且位置正确；
· 某一层合成失败时，其它层仍要画出来（不能整帧丢）。
· 另外顺手锁住 `render_layer_image()` 的高 DPI 坐标（dpr=2 时不能画成 2 倍大）。

跑法：
    QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe tools/test_compose_isolation.py
"""
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Qt 必须先于 rasterio（GDAL）导入；本测试其实不碰 rasterio，但保持同一顺序
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

import numpy as np

FAILS = []


def check(name, ok, extra=""):
    print(f"  {'OK ' if ok else '!! '} {name}: {ok!r}" + ("" if ok else f"  {extra}"))
    if not ok:
        FAILS.append(name)


def fake_layer(lid, color="#336699", raster=False):
    lay = types.SimpleNamespace(id=lid, name=lid, color=color, op="load")
    if raster:
        lay.is_raster = True
    return lay


def block_image(w, h, x, y, color, size=20):
    """在整视口透明图上画一个方块（用来量位置）。"""
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.fillRect(int(x), int(y), size, size, QColor(color))
    p.end()
    return img


def bbox_nonempty(img):
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 0:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def has_color_near(img, x, y, color, radius=2):
    """(x, y) 附近有没有指定颜色（容忍抗锯齿/取整）。"""
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            px, py = int(x) + dx, int(y) + dy
            if 0 <= px < img.width() and 0 <= py < img.height():
                if img.pixelColor(px, py) == QColor(color):
                    return True
    return False


def main() -> int:
    app = QApplication.instance() or QApplication([])

    from native.mapview import MapView

    mv = MapView()
    mv.resize(400, 300)
    mv.setAttribute(Qt.WA_DontShowOnScreen, True)
    mv.show()
    app.processEvents()

    vec = fake_layer("V1", "#2050c0")
    ras = fake_layer("R1", "#c04020", raster=True)
    mv.layers = [vec, ras]
    mv._vec_layers = [vec]
    mv._raster_layers = [ras]
    mv.visible = {vec.id: True, ras.id: True}
    mv._raster.set_layers([ras])
    mv._raster.set_visible(mv.visible)
    dpr = mv._device_pixel_ratio()
    w, h = mv.width(), mv.height()

    # ---------------------------------------------------------------- 1. 类型
    print("[1] fit_bounds 必须把 numpy 标量归一成 Python float")
    try:
        mv.fit_bounds(np.float64(114.4), np.float64(27.5),
                      np.float64(115.4), np.float64(28.1), pad=0.1)
        types_ok = all(type(v) is float for v in (mv.scale, mv.tx, mv.ty))
    except Exception as e:  # noqa: BLE001
        types_ok = False
        print(f"      fit_bounds 抛了 {type(e).__name__}: {e}")
    check("scale/tx/ty 都是 Python float", types_ok,
          f"实际 {type(mv.scale).__name__}/{type(mv.tx).__name__}/{type(mv.ty).__name__}")
    mv._render_timer.stop()

    # ---------------------------------------------------------------- 2. 合成
    print("[2] 旧视口图 + numpy 标量：合成不能抛，位置要跟着平移")
    scale = np.float64(5000.0)
    tx0, ty0 = np.float64(1000.0), np.float64(800.0)
    mv.scale, mv.tx, mv.ty = np.float64(scale), np.float64(tx0), np.float64(ty0)
    old_view = (np.float64(scale), np.float64(tx0), np.float64(ty0), w, h)
    new_view = (np.float64(scale), np.float64(tx0 + 20), np.float64(ty0 + 10), w, h)

    rimg = block_image(int(w * dpr), int(h * dpr),
                       int(40 * dpr), int(30 * dpr), "#c04020", int(20 * dpr))
    vimg = block_image(int(w * dpr), int(h * dpr),
                       int(200 * dpr), int(150 * dpr), "#2050c0", int(20 * dpr))
    mv._raster_imgs = {ras.id: rimg}
    mv._raster._cache[ras.id] = (
        (np.float64(scale), np.float64(tx0), np.float64(ty0),
         int(w * dpr), int(h * dpr), float(dpr), 0), rimg)
    mv._layer_imgs = {vec.id: vimg}
    mv._layer_views = {vec.id: old_view}

    err = ""
    try:
        mv._compose(mv._scene_sig(), new_view, 1)
        out = mv._pending.toImage()
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        out = None
    check("合成没有抛异常", out is not None, err)
    if out is not None:
        # 两张图都该平移 (+20, +10) 逻辑像素
        check("栅格旧图跟着平移", has_color_near(out, (40 + 20) * dpr,
                                              (30 + 10) * dpr, "#c04020"),
              f"bbox={bbox_nonempty(out)}")
        check("矢量旧图跟着平移", has_color_near(out, (200 + 20) * dpr,
                                              (150 + 10) * dpr, "#2050c0"),
              f"bbox={bbox_nonempty(out)}")

    # ---------------------------------------------------------------- 3. 隔离
    print("[3] 某一层画失败，其它层仍要出现在这一帧里")
    mv._raster_imgs = {ras.id: rimg}
    mv._raster._cache[ras.id] = (
        (np.float64(scale), np.float64(tx0), np.float64(ty0),
         int(w * dpr), int(h * dpr), float(dpr), 0), rimg)
    mv._layer_imgs = {vec.id: vimg}
    mv._layer_views = {vec.id: old_view}
    same_view = (np.float64(scale), np.float64(tx0), np.float64(ty0), w, h)
    orig_blit = MapView._blit_view_image

    def flaky_blit(p, img, own, view, dpr_, device_size):
        if device_size:                       # 只让栅格那一层炸
            raise RuntimeError("模拟栅格读图画失败")
        return orig_blit(p, img, own, view, dpr_, device_size)

    mv._blit_view_image = flaky_blit
    err2 = ""
    try:
        mv._compose(mv._scene_sig(), same_view, 2)
        out2 = mv._pending.toImage()
    except Exception as e:  # noqa: BLE001
        err2 = f"{type(e).__name__}: {e}"
        out2 = None
    check("单层失败不向上抛", out2 is not None, err2)
    if out2 is not None:
        check("矢量层没有跟着丢", has_color_near(out2, 200 * dpr, 150 * dpr, "#2050c0"))
    # 画笔必须已经收掉：紧接着再合成一次要能正常开始
    mv._blit_view_image = orig_blit
    err3 = ""
    try:
        mv._compose(mv._scene_sig(), same_view, 3)
        out3 = mv._pending.toImage()
    except Exception as e:  # noqa: BLE001
        err3 = f"{type(e).__name__}: {e}"
        out3 = None
    check("下一次合成还能正常起笔", out3 is not None and
          has_color_near(out3, 200 * dpr, 150 * dpr, "#2050c0"), err3)

    # ---------------------------------------------------------------- 4. dpr
    print("[4] 高 DPI：栅格按逻辑像素落位，不能画成 dpr 倍大")
    from native import rasterview as rv
    from native.proj import project
    from native.rasterview import RasterViewPort, render_layer_image

    class _Info:
        bounds = (114.0, 27.0, 115.0, 28.0)
        bands = 1
        nodata = None

    class _RLayer:
        info = _Info()
        style = {"stretch": "none", "bands": [1], "gamma": 1.0, "opacity": 1.0}
        stats = {0: (0.0, 255.0)}
        rev = 0

    extent = (114.3, 27.3, 114.5, 27.5)
    vp_scale = 400000.0
    _px0, _py0 = project(extent[0], extent[3])
    vp_tx = 50.0 - _px0 * vp_scale          # 让栅格左上角落在逻辑 (50, 30)
    vp_ty = 30.0 - _py0 * vp_scale
    vp = RasterViewPort(extent=extent, width=800, height=600,
                        scale=vp_scale, tx=vp_tx, ty=vp_ty, dpr=2.0,
                        world=(0.0, 0.0, 1.0, 1.0))
    orig_read = rv.read_window

    def fake_read(layer, ext, out_w, out_h, bands=None, resampling="bilinear",
                  cancel=None):
        return np.full((1, max(1, out_h), max(1, out_w)), 200, dtype=np.uint8), ext

    rv.read_window = fake_read
    try:
        img = render_layer_image(_RLayer(), vp)
    finally:
        rv.read_window = orig_read
    check("dpr=2 渲出了栅格图", img is not None and img.width() == 800)
    if img is not None:
        px0, py0 = project(extent[0], extent[3])
        px1, py1 = project(extent[2], extent[1])
        exp = (round((px0 * vp.scale + vp.tx) * vp.dpr),
               round((py0 * vp.scale + vp.ty) * vp.dpr),
               round((px1 * vp.scale + vp.tx) * vp.dpr),
               round((py1 * vp.scale + vp.ty) * vp.dpr))
        got = bbox_nonempty(img)
        ok = got is not None and all(abs(a - b) <= 2 for a, b in zip(got, exp))
        check("栅格设备像素矩形等于视口换算值", ok, f"got={got} exp={exp}")

    mv.close()
    app.processEvents()

    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
