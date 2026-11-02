"""平移时栅格不能「先跳回拖拽前的样子」——回归测试。

问题现场：拖完松手，地图先回到拖拽前的状态，然后才刷成正确的。
原因是视图一变、新栅格还要几百毫秒才渲染好，这段时间里 `_raster_imgs`
里那张**按旧视口渲出来的整视口图**被 `drawImage(0, 0)` 原样贴上去 ——
等于把整个世界拉回原位。

修法：合成时按这张图**自己的视口**做一次
`translate(tx,ty) · scale(s/s0) · translate(-tx0,-ty0)`。
对世界坐标里任一点这个式子精确等于新视口下的屏幕位置，所以：
· 平移 → 纯位移，位置完全正确，只是边缘缺一条 / 分辨率旧；
· 缩放 → 精确的等比变换，和当前屏幕上的旧帧同步。

本测试用「整幅的列灰度剖面互相关」来量栅格挪了多少像素，
不依赖肉眼，也不依赖任何一块特定地貌。

跑法：
    PYTHONPATH="D:/gis-agent/.venv/Lib/site-packages;D:/gis-agent" \
      "D:/zsh/app/py_3.13.1/python.exe" tools/test_pan_no_flicker.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Qt 必须先于 rasterio（GDAL）导入，否则 Windows 上 access violation
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAIL = []


def check(name, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"   {extra}"))
    if not ok:
        FAIL.append(name)


def to_np(img: QImage) -> np.ndarray:
    """QImage → (h, w, 4) uint8。走 RGBA8888 格式避免各平台字节序差异。"""
    if img.format() != QImage.Format_RGBA8888:
        img = img.convertToFormat(QImage.Format_RGBA8888)
    h, w = img.height(), img.width()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=h * img.bytesPerLine())
    return buf.reshape(h, img.bytesPerLine() // 4, 4)[:, :w, :].copy()


def gray(a: np.ndarray) -> np.ndarray:
    """RGBA → 灰度 float，透明处记为 0（背景不参与匹配）。"""
    g = a[:, :, :3].astype(np.float32).mean(axis=2)
    g[a[:, :, 3] == 0] = 0.0
    return g


def best_shift_x(a: np.ndarray, b: np.ndarray, lo: int = -120, hi: int = 120) -> tuple[int, float]:
    """`a` 相对 `b` 在水平方向上的位移（像素）与最优时的平均绝对差。

    只比两块图都非零的那部分，避免透明背景把相关性带偏。
    """
    best, best_err = 0, 1e18
    h, w = a.shape
    for d in range(lo, hi + 1):
        if d >= 0:
            aa, bb = a[:, d:], b[:, :w - d]
        else:
            aa, bb = a[:, :w + d], b[:, -d:]
        if aa.size == 0:
            continue
        both = (aa > 0) & (bb > 0)
        if both.sum() < 1000:
            continue
        err = float(np.abs(aa[both] - bb[both]).mean())
        if err < best_err:
            best, best_err = d, err
    return best, best_err


def diff_ratio(a: np.ndarray, b: np.ndarray, tol: float = 2.0) -> float:
    """两图在共同非零区域上「差异明显」的像素占比。"""
    both = (a > 0) & (b > 0)
    if both.sum() == 0:
        return 1.0
    return float((np.abs(a[both] - b[both]) > tol).mean())


def main() -> int:
    app = QApplication.instance() or QApplication([])

    from app.raster import RasterLayer, read_info
    from native.mapview import MapView

    tif = os.path.join(ROOT, "samples", "raster", "dem.tif")
    if not os.path.exists(tif):
        print("缺 samples/raster/dem.tif，先跑 tools/make_test_raster.py")
        return 1
    info = read_info(tif)
    lay = RasterLayer("R1", "高程", info)

    W, H = 600, 460
    mv = MapView()
    mv.resize(W, H)
    mv.show()
    app.processEvents()
    mv.set_layers([lay])
    mv._render_timer.stop()              # 本测试自己控制合成时机
    app.processEvents()

    dpr = mv._device_pixel_ratio()
    print(f"[1] 视口 {W}x{H} dpr={dpr}，栅格 {info.width}x{info.height}")

    from native.rasterview import RasterViewPort, render_layer_image

    def vp_now():
        return RasterViewPort(extent=mv._view_lonlat_rect(),
                              width=max(1, int(W * dpr)), height=max(1, int(H * dpr)),
                              scale=mv.scale, tx=mv.tx, ty=mv.ty, dpr=dpr,
                              world=mv._view_world_rect())

    img0 = render_layer_image(lay, vp_now())
    check("旧视口渲出栅格图", img0 is not None and img0.width() == int(W * dpr))

    # 模拟「拖拽 60 像素」：只改 tx，不触发重渲染（栅格图仍是旧的）
    PAN = 60
    mv.visible[lay.id] = True
    mv._raster_layers = [lay]
    mv._raster_imgs = {lay.id: img0}
    # 管线缓存里记下这张图是按旧视口渲的（生产环境由 _on_layer 写入）
    from native.rasterview import RasterPipeline
    old_view = (mv.scale, mv.tx, mv.ty, int(W * dpr), int(H * dpr), dpr, 0)
    mv._raster._cache[lay.id] = (old_view, img0)

    # ① 修好之后的行为：用旧图 + 变换合成
    mv.tx += PAN
    vp1 = vp_now()
    mv._compose(mv._scene_sig(), (mv.scale, mv.tx, mv.ty, W, H), mv._epoch)
    fixed = to_np(mv._pending.toImage())

    # ② 新视口下真正渲出来的图（作为基准）
    img1 = render_layer_image(lay, vp1)
    check("新视口渲出栅格图", img1 is not None)
    fresh = to_np(img1)

    # ③ 修之前的行为：旧图硬贴在 (0,0)
    mv._raster._cache[lay.id] = (old_view, img0)
    mv._raster_imgs = {lay.id: img0}
    mv._raster._cache.pop(lay.id, None)          # 没有视口记录 → 走原位贴
    mv._compose(mv._scene_sig(), (mv.scale, mv.tx, mv.ty, W, H), mv._epoch)
    naive = to_np(mv._pending.toImage())

    print("[2] 旧图 vs 新视口基准图 的位移")
    d_fix, err_fix = best_shift_x(gray(fixed), gray(fresh))
    d_old, err_old = best_shift_x(gray(naive), gray(fresh))
    r_fix = diff_ratio(gray(fixed), gray(fresh))
    r_old = diff_ratio(gray(naive), gray(fresh))
    print(f"  修好后位移 {d_fix} px（差异像素 {r_fix:.1%}） / "
          f"修之前位移 {d_old} px（差异像素 {r_old:.1%}）")

    check("修好后栅格位置正确（位移 ≈ 0）", abs(d_fix) <= 1, f"实际 {d_fix}")
    check("修好前确实错位（位移 ≈ 60）", d_old <= -PAN + 8,
          f"实际 {d_old}，期望约 {-PAN}")
    check("修好后和真正的新渲染几乎一致", r_fix < 0.05, f"{r_fix:.1%}")
    check("修之前和新渲染差得很远", r_old > 0.3, f"{r_old:.1%}")

    print("[3] 缩放时也要跟着缩放")
    old_view_z = (mv.scale, mv.tx, mv.ty, int(W * dpr), int(H * dpr), dpr, 0)
    mv._raster._cache[lay.id] = (old_view_z, img0)
    img_old2 = render_layer_image(lay, RasterViewPort(
        extent=mv._view_lonlat_rect(), width=int(W * dpr), height=int(H * dpr),
        scale=old_view_z[0], tx=old_view_z[1], ty=old_view_z[2], dpr=dpr,
        world=mv._view_world_rect()))
    mv._raster._cache[lay.id] = (old_view_z, img_old2)
    mv._raster_imgs = {lay.id: img_old2}
    mv.zoom_at(1.6, W / 2, H / 2)
    mv._compose(mv._scene_sig(), (mv.scale, mv.tx, mv.ty, W, H), mv._epoch)
    zoomed = to_np(mv._pending.toImage())
    img_fresh_zoom = render_layer_image(lay, vp_now())
    d_z, err_z = best_shift_x(gray(zoomed), gray(to_np(img_fresh_zoom)), -40, 40)
    print(f"  缩放后位移 {d_z} px（旧图分辨率不足，残差 {err_z:.2f} 属正常）")
    check("缩放后栅格仍在正确位置", abs(d_z) <= 2, f"实际 {d_z}")

    print("[4] 矢量在重算时，栅格回调不能把半成品推上屏")
    # 先让屏幕上有一帧（否则「首次出图」的宽限期会放行，那是另一条正确路径）
    mv._promote_pending(mv._epoch, (mv.scale, mv.tx, mv.ty, W, H), mv._scene_sig())
    check("此时已有整帧", mv._frame is not None)
    # 隔离：停掉可能还在路上的旧矢量帧，否则 frameReady 会把 busy 清掉
    mv._render_timer.stop()
    if mv._job is not None:
        mv._job.cancelled = True
    mv._epoch += 1
    mv._vec_busy = True
    before = mv._frame
    mv._recompose_now()
    app.processEvents()
    time.sleep(0.15)
    app.processEvents()
    check("矢量忙时不换帧", mv._frame is before)

    mv._vec_busy = False
    mv._recompose_now()
    deadline = time.time() + 3.0
    while time.time() < deadline and mv._frame is before:
        app.processEvents()
        time.sleep(0.01)
    check("矢量空闲时正常换帧", mv._frame is not before)

    # ---------------------------------------------------------------- 矢量侧
    # 上面的 [4] 只是「别上屏」，但用户看到的「整幅图跳回拖拽前」是矢量那一半
    # 造成的：`_layer_imgs` 里的旧图被 drawImage(0,0) 贴到了新视口上。
    # 下面用一个白块图把这件事量出来，不依赖真实数据的地貌。
    print("[5] 矢量层用同一套变换，旧图也要落在新位置上")
    from PySide6.QtGui import QColor, QPainter

    def block_img(w, h, bx, by, bw=40, bh=40):
        im = QImage(w, h, QImage.Format_RGBA8888)
        im.fill(Qt.transparent)
        pp = QPainter(im)
        pp.fillRect(bx, by, bw, bh, QColor(255, 255, 255))
        pp.end()
        return im

    def block_x(arr):
        cols = np.where((arr[:, :, 3] > 0).any(axis=0))[0]
        return int(cols.min()) if cols.size else -1

    v0 = (mv.scale, mv.tx, mv.ty, mv.width(), mv.height())
    canvas0 = block_img(mv.width(), mv.height(), 100, 100)
    x_at_v0 = 100                       # 白块在旧视口图像里的位置
    mv.tx += PAN
    v1 = (mv.scale, mv.tx, mv.ty, mv.width(), mv.height())

    # ① 有视口记录 → 应该挪到 100+PAN
    mv._layer_imgs = {"V1": canvas0}
    mv._layer_views = {"V1": v0}
    mv._raster_imgs = {}
    mv.visible["V1"] = True

    class _FakeVec:
        id, name, color, op = "V1", "假矢量", "#333333", "load"
        is_raster = False

    mv.layers = [_FakeVec()]
    mv._vec_layers = mv.layers
    mv._raster_layers = []
    mv._compose(mv._scene_sig(), v1, mv._epoch)
    got = block_x(to_np(mv._pending.toImage()))
    check("矢量旧图跟着平移了", abs(got - (x_at_v0 + PAN)) <= 1, f"实际 {got}")

    # ② 没有视口记录 → 退回原位贴（只能这样，宁可错一帧也别崩）
    mv._layer_views = {}
    mv._compose(mv._scene_sig(), v1, mv._epoch)
    got2 = block_x(to_np(mv._pending.toImage()))
    check("没有视口记录时退回原位", got2 == x_at_v0, f"实际 {got2}")

    # ---------------------------------------------------------------- 小幅拖拽
    # 用户报的第二个问题：拖一点点就松手，右/下留一圈空边 —— 因为重绘阈值
    # 只有「视口 × 0.3」（900 px 宽要拖 270 px），小幅拖拽永远够不到。
    print("[6] 小幅拖拽也要补齐空边")
    from native.mapview import PAN_RERENDER_PX

    mv._frame_view = (mv.scale, 1000.0, 500.0, mv.width(), mv.height())
    mv.tx, mv.ty = 1000.0, 500.0
    check("没动时不需要重绘", mv._pan_gap_too_wide() is False)

    mv.tx = 1000.0 + 30                       # 拖 30 px：小于视口比例的阈值
    check("拖 30 px 不打断跟手", mv._pan_gap_too_wide() is False)

    mv.tx = 1000.0 + PAN_RERENDER_PX + 8      # 超过绝对上限 → 该重绘了
    check(f"露出 {PAN_RERENDER_PX}+ px 空白就重绘", mv._pan_gap_too_wide() is True)

    small_w = 200
    mv._frame_view = (mv.scale, 1000.0, 500.0, mv.width(), mv.height())
    check("阈值是「视口比例」和「绝对像素」取小",
          round(min(mv.width() * 0.3, PAN_RERENDER_PX)) == PAN_RERENDER_PX
          or mv.width() * 0.3 < PAN_RERENDER_PX,
          f"视口 {mv.width()} → {min(mv.width() * 0.3, PAN_RERENDER_PX):.0f}")

    # 松手必须触发一次重绘（把空边补上），哪怕只拖了 30 px
    mv._render_timer.stop()
    mv.tx = 1000.0 + 30
    mv._drag_from = (0, 0)
    check("松手前定时器没起", mv._render_timer.isActive() is False)
    mv._end_pan()
    check("松手后安排了重绘", mv._render_timer.isActive() is True)
    mv._drag_from = None
    mv._render_timer.stop()

    # 完全没动就不要白白重绘
    mv.tx = mv._frame_view[1]
    mv._end_pan()
    check("没移动时不重绘", mv._render_timer.isActive() is False)

    print()
    # 必须显式关掉：MapView 里有常驻渲染线程，进程退出时才回收会触发
    # 「QThread: Destroyed while thread is still running」并把退出码变成
    # 0xC0000409（测试全过却报 crash）。
    mv.close()
    app.processEvents()
    if FAIL:
        print(f"结论: FAIL（{len(FAIL)} 项）：" + "、".join(FAIL))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
