"""栅格金字塔生成回归。

覆盖：
· 无 overview 检测；
· GDAL 外部 .ovr 生成 + 进度回调；
· 生成后 has_overviews 为 True；
· 桌面端 QProgressDialog 路径能完成并返回。

用法：.venv/Scripts/python.exe tools/test_pyramid.py
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="gis_pyramid_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

import numpy as np
import rasterio
from rasterio.transform import from_origin

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def make_raster(path: Path) -> None:
    with rasterio.open(
            path, "w", driver="GTiff", width=3000, height=3000, count=1,
            dtype="uint8", crs="EPSG:4326",
            transform=from_origin(100, 30, 0.001, 0.001)) as ds:
        ds.write(np.random.randint(0, 255, (1, 3000, 3000), dtype=np.uint8))


def main() -> int:
    from native.pyramid import build_overviews, has_overviews, overview_factors

    print("[1] 检测与层级")
    src = TMP / "plain.tif"
    make_raster(src)
    check("未生成时没有 overview", has_overviews(src), False)
    check("层级", overview_factors(3000, 3000), [2, 4, 8])

    print("[2] 生成外部 .ovr + 进度")
    values: list[int] = []
    ok, msg = build_overviews(src, overview_factors(3000, 3000),
                              progress=values.append)
    check("生成成功", ok, True)
    check("没有错误", msg, "")
    check("外部 .ovr 存在", Path(str(src) + ".ovr").is_file(), True)
    check("生成后检测为有 overview", has_overviews(src), True)
    check("进度收到 100", bool(values) and values[-1] == 100, True)
    check("进度单调到 100", max(values) == 100, True)

    print("[3] 桌面端 QProgressDialog 路径")
    from PySide6.QtWidgets import QApplication

    from native.app import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.resize(1000, 760)
    win.show()
    src2 = TMP / "ui.tif"
    make_raster(src2)
    t0 = time.time()
    win._generate_pyramids(src2, overview_factors(3000, 3000))
    check("界面路径完成", Path(str(src2) + ".ovr").is_file(), True)
    check("耗时可接受", time.time() - t0 < 30, True)

    print("[4] 打开大栅格时弹窗并生成")
    import native.app as app_mod

    class FakeBox:
        AcceptRole = 1
        RejectRole = 2

        def __init__(self, *a, **k):
            self._gen = object()

        def setWindowTitle(self, *a):
            pass

        def setText(self, *a):
            pass

        def setInformativeText(self, *a):
            pass

        def addButton(self, text, role):
            if text == "生成金字塔":
                return self._gen
            return object()

        def exec(self):
            return 1

        def clickedButton(self):
            return self._gen

        @staticmethod
        def warning(*a, **k):
            raise AssertionError("不应该失败")

    src3 = TMP / "prompt.tif"
    make_raster(src3)
    orig = app_mod.QMessageBox
    app_mod.QMessageBox = FakeBox
    try:
        win._maybe_offer_pyramids([src3])
    finally:
        app_mod.QMessageBox = orig
    check("弹窗确认后生成 .ovr", Path(str(src3) + ".ovr").is_file(), True)
    check("生成后可检测", has_overviews(src3), True)

    win.close()
    win.deleteLater()
    app.processEvents()
    app.quit()

    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
