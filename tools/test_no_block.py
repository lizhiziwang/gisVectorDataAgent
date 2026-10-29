"""回归：Agent 执行任务期间，改视图必须能立刻出帧（渲染管线不能被掐住）。

指标：任务执行过程中发起缩放，从改视图到「新帧上屏」的延迟。
用法：python tools/test_no_block.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from native.app import MainWindow

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"

app = QApplication(sys.argv)
win = MainWindow()
win.resize(1280, 800)
win.show()
win.load_paths([os.path.join(BASE, "gis_osm_landuse_a_free_1.shp")])

t0 = time.perf_counter()
while time.perf_counter() - t0 < 90:
    app.processEvents()
    if win.map.layers and not win.map._has_pending() and win.map._frame is not None:
        break
    time.sleep(0.01)
print("图层就绪:", [l.name for l in win.map.layers])
mv = win.map


def pump(until, timeout):
    """跑事件循环直到条件满足或超时，返回耗时。"""
    t = time.perf_counter()
    while time.perf_counter() - t < timeout:
        app.processEvents()
        if until():
            return time.perf_counter() - t
        time.sleep(0.005)
    return None


win.on_send("对地块做 500 米缓冲区")
pump(lambda: win._busy, 10)
print("任务已启动，busy =", win._busy)

latencies = []
loading_seen = False
for i in range(4):
    time.sleep(0.6)
    if not win._busy:
        break
    loading_seen = loading_seen or bool(mv._loading)
    want = None

    def zoom():
        global want
        mv.zoom_at(1.35, mv.width() * 0.5, mv.height() * 0.5)
        want = (mv.scale, mv.tx, mv.ty, mv.width(), mv.height())

    zoom()
    dt = pump(lambda: mv._frame_view == want, 20)
    latencies.append(dt)
    print(f"  第 {i+1} 次缩放：出帧延迟 {('%.0f ms' % (dt*1000)) if dt else '超时 >20s'}  busy={win._busy}")

rest = pump(lambda: not win._busy, 300)
print(f"任务总耗时约 {time.perf_counter() - t0 - 0:.0f}s（含前面等待）")

ok_loading = not loading_seen
ok_lat = all(x is not None for x in latencies) and latencies and max(latencies) < 5.0
print(f"\n执行期间 _loading 曾为 True: {loading_seen}")
print(f"最长出帧延迟: {max([x for x in latencies if x], default=-1)*1000:.0f} ms")
print("\n结论:", "PASS" if (ok_loading and ok_lat) else "FAIL")
win.close()
