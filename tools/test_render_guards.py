"""渲染管线防卡死回归：范围未知的 provider、异常在线视口。

跑法：
    .venv\\Scripts\\python.exe tools\\test_render_guards.py

检查：
1. 旧 GeoParquet（没有 `.bounds.json`）不会被渲染线程扫全层；
   第一帧先跳过该层，后台算出范围后再参与裁剪；
2. 后台范围只算一次，失败也不会每帧重复排队；
3. 异常大的在线瓦片视口会直接放弃，不发几百上千个请求。
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from app.engine import Layer
from native.mapview import MapView, project
from native.rasterview import RasterViewPort, _render_online_tiles

FAIL = []


def check(name, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"   {extra}"))
    if not ok:
        FAIL.append(name)


class SlowProvider:
    """模拟没有 bbox 的大 GeoParquet：算范围要 0.25s，整层读会报错。"""

    path = "fake.parquet"
    count = 300_000
    bounds = ()
    wgs_bounds_hint = None

    def __init__(self):
        self.scans = 0

    def meta(self):
        return {"count": self.count, "geomType": "Polygon",
                "bounds": [], "fields": []}

    @property
    def wgs_bounds(self):
        self.scans += 1
        time.sleep(0.25)
        return (120.0, 30.0, 120.2, 30.2)

    def iter_bbox(self, *args, **kwargs):
        raise AssertionError("范围未知时不应调用 iter_bbox 扫全层")

    def read_all(self):
        raise AssertionError("范围未知时不应整层读取")


def settle(mv, want, seconds=3.0):
    app = QApplication.instance()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        app.processEvents()
        if mv._frame_view == want and mv._frame_sig == mv._scene_sig():
            return time.perf_counter() - t0
        time.sleep(0.005)
    return None


def test_async_bounds():
    print("[1] 旧 provider 没有范围旁车")
    app = QApplication.instance() or QApplication([])
    mv = MapView()
    mv.resize(640, 480)
    # 避免 set_layers 里的 fit_all 同步调用 provider.wgs_bounds
    mv.fit_all = lambda: None
    prov = SlowProvider()
    lay = Layer(id="Lguard", name="guard", gdf=None, _provider=prov)
    mv.set_layers([lay])
    c = mv._cache[lay.id]

    s = 8_000_000.0
    x, y = project(121.5, 31.5)
    mv.set_view(s, mv.width() / 2 - x * s, mv.height() / 2 - y * s)
    check("视口外时先标记 pending", c.bounds_pending, f"pending={c.bounds_pending}")
    check("此时还没有 extent", c.extent is None)
    t0 = time.perf_counter()
    lb = mv._layer_bounds(lay)
    check("fit 入口也不扫全层", lb is None and time.perf_counter() - t0 < 0.1,
          f"lb={lb} dt={time.perf_counter() - t0}")

    t0 = time.perf_counter()
    mv._start_render()
    want = (mv.scale, mv.tx, mv.ty, mv.width(), mv.height())
    dt = settle(mv, want)
    check("第一帧不等范围计算", dt is not None and dt < 1.0, f"dt={dt}")

    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 3.0 and c.bounds_pending:
        app.processEvents()
        time.sleep(0.02)
    check("后台只算一次范围", prov.scans == 1, f"scans={prov.scans}")
    check("范围回填", c.extent == (120.0, 30.0, 120.2, 30.2), f"extent={c.extent}")

    # 范围已知后，当前视口仍在范围外：再次渲染也不能调 iter_bbox
    mv._start_render()
    settle(mv, want)
    check("视口外不扫全层", prov.scans == 1, f"scans={prov.scans}")
    mv.shutdown()


def test_online_tile_guard():
    print("[2] 异常在线瓦片视口")
    vp = RasterViewPort(
        extent=(-180.0, -85.0, 180.0, 85.0),
        width=1100, height=940, scale=256.0 * (1 << 15),
        tx=0.0, ty=0.0, dpr=1.0, world=(0.0, 0.0, 1.0, 1.0))
    img = _render_online_tiles(
        None, {"url": "https://example.invalid/{z}/{x}/{y}.png",
               "subdomains": "a"}, vp)
    check("整世界误算到 z15 时不发请求", img is None, f"type={type(img)}")


def main() -> int:
    test_async_bounds()
    test_online_tile_guard()
    print("\nPASS" if not FAIL else f"\nFAIL: {FAIL}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
