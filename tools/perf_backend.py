"""CPU 光栅 vs OpenGL：同一份渲染集、同一个视口，逐图层对比。

为什么要有这个脚本：网上/直觉都说「矢量地图上 GPU 快」，但这里实测的结论刚好相反 ——
GL 只在**线描边**上快，画面要素和点要素反而慢一到两个数量级。
要把渲染后端搬去 GL 之前，请先在这个机器上跑一遍它。

做法：真 windows 平台（需要真 GL 上下文）+ 离屏 FBO，用 QPainter 画同一批
QPainterPath（就是应用里那套渲染集），每层分别计时；顺手统计落笔像素，
防止「其实什么都没画」的假结果（第一次测就踩过：路径被挪到画布外，量到的只是裁剪时间）。

用法：.venv\\Scripts\\python.exe tools\\perf_backend.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("QT_QPA_PLATFORM", None)      # 真平台才有 GL 上下文

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from native import mapview
from native.app import MainWindow

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
FILES = ["gis_osm_landuse_a_free_1.shp", "gis_osm_buildings_a_free_1.shp",
         "gis_osm_water_a_free_1.shp", "gis_osm_roads_free_1.shp"]

try:
    from PySide6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
    from PySide6.QtOpenGL import (QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
                                  QOpenGLPaintDevice)
    HAS_GL = True
except Exception as e:                        # noqa: BLE001
    print("没有 QtOpenGL 模块：", e)
    HAS_GL = False


class GLTarget:
    """渲染线程之外的一层 GL 壳子：一个上下文 + 一个 FBO，够画一张图。"""

    def __init__(self, w, h, dpr):
        fmt = QSurfaceFormat()
        fmt.setSamples(0)
        self.ctx = QOpenGLContext()
        self.ctx.setFormat(fmt)
        self.ok = self.ctx.create()
        if not self.ok:
            return
        self.surf = QOffscreenSurface()
        self.surf.setFormat(self.ctx.format())
        self.surf.create()
        self.ok = self.surf.isValid() and self.ctx.makeCurrent(self.surf)
        if not self.ok:
            return
        f = QOpenGLFramebufferObjectFormat()
        f.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
        self.fbo = QOpenGLFramebufferObject(int(w * dpr), int(h * dpr), f)
        self.ok = self.fbo.isValid()
        self.w, self.h, self.dpr = w, h, dpr

    def paint(self, fn) -> QImage:
        self.ctx.makeCurrent(self.surf)
        self.fbo.bind()
        dev = QOpenGLPaintDevice(QSize(int(self.w * self.dpr), int(self.h * self.dpr)))
        dev.setDevicePixelRatio(self.dpr)
        p = QPainter(dev)
        p.setRenderHint(QPainter.Antialiasing, True)
        try:
            fn(p)
        finally:
            p.end()
            self.fbo.release()
        return self.fbo.toImage()


def ink(img: QImage) -> int:
    """落笔像素（抽 1/4 网格），用来确认这一遍真的画了东西。"""
    n = 0
    for y in range(0, img.height(), 4):
        for x in range(0, img.width(), 4):
            if (img.pixel(x, y) >> 24) & 0xFF:      # 非透明
                n += 1
    return n * 16


def main() -> None:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.resize(1440, 900)
    win.show()
    paths = [os.path.join(BASE, f) for f in FILES if os.path.exists(os.path.join(BASE, f))]
    win.load_paths(paths)
    while len(win.map.layers) < len(paths) or win.map._frame is None or win.map._has_pending():
        app.processEvents()
        time.sleep(0.01)

    mv = win.map
    snap = mv._make_snapshot()
    w, h, dpr = snap["w"], snap["h"], snap["dpr"]
    print(f"视口 {w}x{h} @ {mv.mpp():.0f} m/px，dpr={dpr}")

    gl = GLTarget(w, h, dpr) if HAS_GL else None
    if gl is not None:
        print("GL:", "可用" if gl.ok else "不可用（只测 CPU）",
              f"{gl.ctx.format().majorVersion()}.{gl.ctx.format().minorVersion()}" if gl.ok else "")
        if not gl.ok:
            gl = None

    def draw(p, lay):
        p.translate(snap["tx"], snap["ty"])
        p.scale(snap["scale"], snap["scale"])
        mapview._paint_layer(p, lay, snap["view"], snap["scale"], snap["mpp"])

    print(f"\n{'图层':34s}{'路径/线批':>12s}{'CPU':>10s}{'GL':>10s}{'GL/CPU':>9s}")
    for lay in snap["layers"]:
        name = next((l.name for l in mv.layers if l.id == lay["lid"]), "?")

        def cpu():
            img = QImage(int(w * dpr), int(h * dpr), QImage.Format_ARGB32_Premultiplied)
            img.setDevicePixelRatio(dpr)
            img.fill(Qt.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing, True)
            draw(p, lay)
            p.end()
            return img

        cpu()                                     # 预热
        t = time.perf_counter()
        img_cpu = cpu()
        tc = 1000 * (time.perf_counter() - t)
        ratio = gl_txt = ""
        gl_dt = None
        if gl is not None:
            gl.paint(lambda p: draw(p, lay))
            t = time.perf_counter()
            img_gl = gl.paint(lambda p: draw(p, lay))
            gl_dt = 1000 * (time.perf_counter() - t)
            ratio = f"{gl_dt / tc:.2f}×"
            gl_txt = f"{gl_dt:.0f} ms"
            if ink(img_gl) < ink(img_cpu) * 0.5:
                gl_txt += "(?)"
        print(f"{name[:34]:34s}{lay['npoly']:>5d}/{lay['nline']:<6d}"
              f"{tc:>8.0f} ms{gl_txt:>12s}{ratio:>9s}")

    print("\n结论（这台机器，2026-10）：线要素 GL 快约 2 倍；面要素 GL 慢 7~9 倍；"
          "\n24 万个点 GL 慢 100 倍以上（QPainter 在 GL 上走 stencil 三角化，"
          "\n大量小面/点等于把描边开销换成了三角化开销）。"
          "\n所以后端保持 CPU 光栅，性能花在「1px 笔宽 + 包围盒向量化」上更划算。")


if __name__ == "__main__":
    main()
