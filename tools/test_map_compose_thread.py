"""地图合成走独立线程的回归。

· QImage 合成在 _ComposeWorker 里完成，UI 线程不直接调 _compose；
· 合成结果仍能变成 _frame 并上屏。

用法：.venv/Scripts/python.exe tools/test_map_compose_thread.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    from native.app import MainWindow
    from native.mapview import _ComposeJob

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.resize(1000, 760)
    win.show()
    app.processEvents()
    mv = win.map

    print("[1] 独立合成线程")
    check("合成线程在跑", mv._compose_worker.isRunning(), True)

    print("[2] 合成不再走 UI 线程的 _compose()")
    def boom(*a, **k):
        raise AssertionError("UI 线程不应该直接合成")

    orig_compose = mv._compose
    mv._compose = boom
    try:
        mv._frame = None
        mv._compose_async(
            mv._scene_sig(),
            (mv.scale, mv.tx, mv.ty, mv.width(), mv.height()),
            mv._epoch, "force")
        deadline = time.time() + 3
        while time.time() < deadline and mv._frame is None:
            app.processEvents()
            time.sleep(0.01)
    finally:
        mv._compose = orig_compose
    check("合成结果上屏", mv._frame is not None, True)

    print("[3] 大图合成在后台完成")
    big = QImage(1800, 1200, QImage.Format_ARGB32_Premultiplied)
    big.fill(Qt.transparent)
    from PySide6.QtGui import QPainter
    pp = QPainter(big)
    pp.fillRect(0, 0, 1800, 1200, QColor(30, 120, 200, 140))
    pp.end()
    job = _ComposeJob(mv._epoch, mv._scene_sig(),
                      (mv.scale, mv.tx, mv.ty, mv.width(), mv.height()),
                      mv._device_pixel_ratio(),
                      int(mv.width() * mv._device_pixel_ratio()),
                      int(mv.height() * mv._device_pixel_ratio()),
                      [(True, big, None)], "force")
    t0 = time.perf_counter()
    before = mv._frame
    mv._compose_worker.submit(job)
    dt = time.perf_counter() - t0
    check("提交合成 < 100ms", dt < 0.1, True)
    deadline = time.time() + 3
    while time.time() < deadline and mv._frame is before:
        app.processEvents()
        time.sleep(0.01)
    check("大图合成后新帧上屏", mv._frame is not before, True)

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
    sys.exit(main())
