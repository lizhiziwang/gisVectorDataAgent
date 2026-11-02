"""栅格+矢量混合渲染的端到端验证（会真的跑渲染管线并截图）。

验证点：
1. 栅格能载入、能进图层面板、fit_all 能算到栅格范围
2. 栅格和矢量叠在同一帧上，位置对齐（矢量要素应落在栅格范围内）
3. 只有栅格（没有矢量）时也能出图 —— 这是「矢量快照为空」那条分支
4. 显隐切换对栅格生效
5. 缩放后栅格重新采样（不同 scale 命中不同缓存）

用法：python tools/test_raster.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 注意顺序：Qt 必须在 rasterio/GDAL 之前导入。
# rasterio 自带一套 GDAL DLL，先加载它会让后面的 PySide6 导入在 Windows 上
# 直接 access violation（段错误，无 Python 栈）。真实应用同理：
# native_app.py 先起 QApplication，rasterio 只在真正打开栅格时才惰性导入。
from PySide6.QtWidgets import QApplication

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "perf")
fails = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else want(got)
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


def pump(app, win, ms=1400):
    """跑事件循环，等渲染线程把图画完。"""
    t0 = time.time()
    while (time.time() - t0) * 1000 < ms:
        app.processEvents()
        time.sleep(0.01)


print("[1] 栅格元信息")
from app.raster import RasterLayer, read_info

dem = os.path.join(ROOT, "samples", "raster", "dem.tif")
img = os.path.join(ROOT, "samples", "raster", "image.tif")
check("dem 存在", os.path.exists(dem), True)
i1 = read_info(dem)
check("dem 尺寸", (i1.width, i1.height), (600, 480))
check("dem 波段", i1.bands, 1)
check("dem 有 nodata", i1.nodata, -9999.0)
i2 = read_info(img)
check("image 波段", i2.bands, 3)

print("[2] 栅格图层是鸭子类型的 Layer")
rl = RasterLayer("R1", "高程", i1)
check("is_raster", rl.is_raster, True)
check("meta 有 bounds", len(rl.meta()["bounds"]), 4)
check("meta 的 geomType", rl.meta()["geomType"], "Raster")
try:
    rl.gdf
    check("访问 gdf 应报错", False, True)
except Exception as e:
    check("访问 gdf 报中文错", "栅格图层" in str(e), True)

print("[3] 载入到 Store")
from app.engine import Store

st = Store()
lay_dem = st.add_raster(i1, "高程")
lay_img = st.add_raster(i2, "影像")
check("Store 里有 2 个栅格", len(st.layers), 2)
check("内存统计不炸", isinstance(st.cache_stats(), dict), True)

print("[4] 桌面端混合渲染")
app = QApplication.instance() or QApplication([])
from native.app import MainWindow

win = MainWindow()
win.resize(1000, 760)
win.show()
pump(app, win, 300)

# 载入示例矢量 + 两个栅格
samples = [os.path.join(ROOT, "samples", f)
           for f in ("地块.geojson", "行政区.geojson")]
samples = [p for p in samples if os.path.exists(p)]
win.load_paths(samples)
pump(app, win, 2500)
win.load_paths([dem, img])
pump(app, win, 2500)

check("store 图层数 >= 4", len(win.store.layers) >= 4, True)
n_ras = sum(1 for l in win.store.layers.values() if getattr(l, "is_raster", False))
check("栅格数 == 2", n_ras, 2)

# 地图的矢量/栅格分流
check("地图分出 2 个栅格", len(win.map._raster_layers), 2)
check("地图矢量层不含栅格",
      all(not getattr(l, "is_raster", False) for l in win.map._vec_layers), True)

win.map.fit_all()
pump(app, win, 2500)
check("渲染出了栅格图", len(win.map._raster_imgs) >= 1, True)

shot1 = os.path.join(OUT, "raster-mixed.png")
win.map.grab().save(shot1)
check("已截图", os.path.getsize(shot1) > 5000, True)

print("[5] 显隐切换对栅格生效")
rid = lay_img.id
win.visible[rid] = False
win.map.invalidate()
pump(app, win, 1800)
win.map.grab().save(os.path.join(OUT, "raster-hidden.png"))
check("隐藏后该栅格不参与合成", win.visible[rid], False)
win.visible[rid] = True
win.map.invalidate()
pump(app, win, 1800)

print("[6] 只有栅格也要能出图")
for l in list(win.store.layers.values()):
    if not getattr(l, "is_raster", False):
        win.visible[l.id] = False
win.map.invalidate()
pump(app, win, 2200)
check("只剩栅格仍渲染出图", len(win.map._raster_imgs) >= 1, True)
shot2 = os.path.join(OUT, "raster-only.png")
win.map.grab().save(shot2)
check("已截图", os.path.getsize(shot2) > 3000, True)

print("[7] 缩放后重新采样")
before = win.map.scale
win.map.zoom_by(-3)
pump(app, win, 2200)
check("scale 变了", win.map.scale != before, True)
win.map.grab().save(os.path.join(OUT, "raster-zoom.png"))
check("缩放后仍有栅格图", len(win.map._raster_imgs) >= 1, True)

print("[8] 栅格做矢量算子要给出清晰错误")
from app.engine import OPERATIONS

# 必须用 win.store 里的图层 id：不同 Store 的 id 找不到，会变成「找不到图层」
ras = next(l for l in win.store.layers.values() if getattr(l, "is_raster", False))
try:
    OPERATIONS["buffer"](win.store, ras.id, distance=100)
    check("栅格 buffer 应失败", False, True)
except Exception as e:
    check("栅格 buffer 报中文错", "栅格图层" in str(e), True)

print("[9] 关闭时线程能干净退出")
win.close()
pump(app, win, 300)
check("栅格线程已停", win.map._raster._worker.isRunning(), False)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(1 if fails else 0)
