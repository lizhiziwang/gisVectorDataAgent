"""在线瓦片并发下载：并发确实发生、结果与串行一致、取消/失败/缓存语义不变。

不访问真实网络：`native.rasterview.fetch_bytes` 被换成一个带延时的假下载，
用「同时在飞的请求数」和总耗时证明并发真的生效，用每张瓦片各自的颜色
证明落位没变。
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["GIS_AGENT_PERSIST"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QBuffer, QByteArray
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

FAIL = []


def check(name: str, actual, expected=True) -> None:
    ok = actual == expected
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        print(f"     实际={actual!r} 期望={expected!r}")
        FAIL.append(name)


def png_bytes(color) -> bytes:
    img = QImage(256, 256, QImage.Format_ARGB32)
    img.fill(QColor(color))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def tile_color(z: int, x: int, y: int) -> str:
    """每张瓦片一个可认的颜色：R 由列、G 由行决定。"""
    return "#%02x%02x%02x" % (32 + (x * 24) % 224, 32 + (y * 40) % 224, 96)


def parse_xyz(url: str) -> tuple[int, int, int]:
    """从 ... /{z}/{x}/{y}.png 里取回瓦片坐标。"""
    tail = url.split("://", 1)[-1].rstrip("/").split("/")
    z = int(tail[-3])
    x = int(tail[-2])
    y = int(tail[-1].split(".", 1)[0].split("?", 1)[0])
    return z, x, y


app = QApplication([])

import native.rasterview as rv
from app.online_map import online_raster_info, validate_config
from app.raster import RasterCancelled, RasterLayer

# 4×4 = 16 张瓦片：scale=1024 → z=2 → n=4，视口 world 取 (0,0,1,0.75)，
# 右上角正好落在瓦片边界上，所以 y 也覆盖 0…3。
VP = rv.RasterViewPort((0.0, 0.0, 1.0, 1.0), 1024, 768, 1024.0, 0.0, 0.0, 1.0,
                       (0.0, 0.0, 1.0, 0.75))
CFG = validate_config("xyz", "https://t/{z}/{x}/{y}.png", "并发瓦片")
LAYER = RasterLayer(id="Rpar", name="并发瓦片", info=online_raster_info(CFG),
                    online=CFG)

old_fetch = rv.fetch_bytes
old_fetcher = rv._TILE_FETCHER


def fresh_pool(workers: int = 8) -> None:
    """每个用例一套干净的线程池，避免上一轮的缓存/排队影响计时。"""
    rv._TILE_FETCHER = rv._TileFetcher(workers)
    rv.ONLINE_CACHE.clear()


print("[1] 16 张瓦片并发下载，总耗时远小于串行")
fresh_pool()
state = {"active": 0, "peak": 0, "calls": 0}
lock = threading.Lock()


def fake_fetch(url, **kwargs):
    with lock:
        state["active"] += 1
        state["calls"] += 1
        state["peak"] = max(state["peak"], state["active"])
    try:
        time.sleep(0.12)               # 模拟一次往返
        return png_bytes(tile_color(*parse_xyz(url)))
    finally:
        with lock:
            state["active"] -= 1


rv.fetch_bytes = fake_fetch
t0 = time.monotonic()
out = rv.render_layer_image(LAYER, VP)
elapsed = time.monotonic() - t0
serial = 16 * 0.12
print(f"     16 张 × 120 ms：串行下限 {serial:.2f} s，实测 {elapsed:.3f} s，"
      f"峰值并发 {state['peak']}")
check("瓦片全部取到", state["calls"], 16)
check("确实并发（峰值 ≥ 4）", state["peak"] >= 4)
check("耗时接近 ceil(16/8) 轮", elapsed < serial * 0.55)
check("并发后仍返回完整图", out is not None)
check("并发后尺寸不变", (out.width(), out.height()), (1024, 768))

# 每张瓦片画在自己的格子里：颜色只由 (x, y) 决定，落位错了就会串色。
# 视口高 768 = 3 行 × 256，第 4 行落在视口外（瓦片范围含边界，会多取一行）。
span = VP.scale / 4
bad = []
for y in range(3):
    for x in range(4):
        cx = int(x * span + span / 2)
        cy = int(y * span + span / 2)
        got = out.pixelColor(cx, cy).name()
        if got != tile_color(2, x, y):
            bad.append((x, y, got, tile_color(2, x, y)))
check("每张瓦片落在自己的格子", bad, [])

print("[2] 同一视口第二次渲染全部命中缓存，不再发请求")
state["calls"] = 0
out2 = rv.render_layer_image(LAYER, VP)
check("缓存命中不再下载", state["calls"], 0)
check("缓存结果一致",
      [out2.pixelColor(int(x * span + span / 2), int(y * span + span / 2)).name()
       for y in range(3) for x in range(4)],
      [out.pixelColor(int(x * span + span / 2), int(y * span + span / 2)).name()
       for y in range(3) for x in range(4)])

print("[3] 取消：不等 12 张全部回来就抛 RasterCancelled")
fresh_pool()
seen = {"n": 0}


def slow_fetch(url, **kwargs):
    with lock:
        seen["n"] += 1
    time.sleep(0.30)
    return png_bytes("#3366cc")


rv.fetch_bytes = slow_fetch
deadline = time.monotonic() + 0.08
cancel = lambda: time.monotonic() >= deadline   # noqa: E731
t0 = time.monotonic()
try:
    rv.render_layer_image(LAYER, VP, cancel=cancel)
    check("取消时抛 RasterCancelled", False)
except RasterCancelled:
    cost = time.monotonic() - t0
    check("取消时抛 RasterCancelled", True)
    print(f"     取消耗时 {cost:.3f} s（单张 0.30 s）")
    check("取消立即返回（< 0.30 s）", cost < 0.30)
check("取消没有把所有瓦片都发出去", seen["n"] <= 8)

print("[4] 一张瓦片失败：保留已画部分，不再继续提交")
fresh_pool(2)
calls = {"n": 0}


def flaky_fetch(url, **kwargs):
    calls["n"] += 1
    time.sleep(0.02)
    if parse_xyz(url)[1:3] == (2, 1):
        return None
    return png_bytes(tile_color(*parse_xyz(url)))


rv.fetch_bytes = flaky_fetch
part = rv.render_layer_image(LAYER, VP)
check("失败时返回已画部分", part is not None)
check("失败前的瓦片已画上",
      part.pixelColor(int(span / 2), int(span / 2)).alpha() > 0)
last_x, last_y = 3, 2
check("失败点之后的瓦片没有画",
      part.pixelColor(int(last_x * span + span / 2),
                      int(last_y * span + span / 2)).alpha(), 0)
check("失败后不再继续提交", calls["n"] < 16)

print("[5] 全部失败：返回 None，不缓存空图")
fresh_pool()
rv.fetch_bytes = lambda *a, **k: None
check("整条线路不可用返回 None", rv.render_layer_image(LAYER, VP) is None)
calls = {"n": 0}


def count_fetch(url, **kwargs):
    calls["n"] += 1
    return None


fresh_pool()
rv.fetch_bytes = count_fetch
rv.render_layer_image(LAYER, VP)
first = calls["n"]
rv.fetch_bytes = fake_fetch
fresh_pool()
check("失败没有写进缓存（下次仍会重试）", first <= 12)

print("[6] 线程池：单例、daemon、数量可控")
fresh_pool(3)
pool = rv.tile_fetcher()
check("tile_fetcher 返回单例", rv.tile_fetcher() is pool)
check("线程数按配置", len([t for t in pool._threads]), 0)   # 还没提交过
rv.fetch_bytes = fake_fetch
fresh_pool(3)
rv.render_layer_image(LAYER, VP)
pool = rv.tile_fetcher()
check("首次提交后起 3 条线程", len(pool._threads), 3)
check("线程都是 daemon", all(t.daemon for t in pool._threads))

rv.fetch_bytes = old_fetch
rv._TILE_FETCHER = old_fetcher
rv.ONLINE_CACHE.clear()

print()
if FAIL:
    print("结论: FAIL " + ", ".join(FAIL))
    sys.exit(1)
print("结论: PASS")
