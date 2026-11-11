"""地图对比回归：分屏 / 卷帘 / 透明度 / 闪烁 / 视图同步。

用法：.venv/Scripts/python.exe tools/test_map_compare.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication, QDialog

from app.raster import read_info

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def pump(app, seconds=0.4):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    from native.app import MainWindow
    from native.compare import MapCompareDialog, MODES

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.resize(1200, 800)
    win.show()
    app.processEvents()

    r1 = win.store.add_raster(read_info(ROOT / "samples" / "raster" / "image.tif"), "底图 A")
    r2 = win.store.add_raster(read_info(ROOT / "samples" / "raster" / "dem.tif"), "底图 B")
    win.visible[r1.id] = True
    win.visible[r2.id] = True
    win.refresh_layers(fit=True)
    pump(app)

    print("[1] 工具栏按钮")
    buttons = [b for b in win.findChildren(type(win.compare_btn))
               if b.objectName() == "mapCtrl"]
    check("地图按钮数量", len(buttons), 4)
    check("对比按钮可勾选", win.compare_btn.isCheckable(), True)
    check("对比按钮文字", win.compare_btn.text(), "◫")

    print("[2] 左右分屏")
    m = win.map_compare
    m.activate(r1.id, r2.id, "horizontal", list(win.store.layers.values()), win.visible)
    pump(app, 0.6)
    check("对比已激活", m.active, True)
    check("左右各占一半", abs(m.left.width() - m.right.width()) <= 2, True)
    check("分隔条可见", m.handle.isVisible(), True)
    check("右侧图层", [l.name for l in m.right.layers], ["底图 B"])
    check("右侧只看 B", m.right.visible.get(r2.id), True)
    check("左侧只看 A", m.left.visible.get(r1.id), True)
    check("右侧排除 A", m.right.visible.get(r1.id), False)

    print("[3] 拖拽分隔条")
    old_split = m.split
    m.begin_drag(QPointF(0, 0))
    m.drag_to(QPointF(m.width() * 0.65, 0))
    m.end_drag()
    pump(app, 0.2)
    check("比例已变化", abs(m.split - old_split) > 0.05, True)
    check("左/右几何跟随", m.left.width() + m.right.width() <= m.width(), True)

    print("[3b] 卷帘：拖动时蒙版实时跟手、露出的主图要重绘")
    m._set_mode("swipe")
    pump(app, 0.2)

    def _want_x(ratio):
        return max(40, min(m.width() - 40, int(m.width() * ratio)))

    check("卷帘模式", m.mode, "swipe")
    check("初始蒙版跟着当前比例",
          abs(m.right.mask().boundingRect().left() - _want_x(m.split)) <= 2, True)
    m.split = 0.8
    m._preview_handle()
    check("拖动中蒙版跟手（不是只在松手时更新）",
          abs(m.right.mask().boundingRect().left() - _want_x(0.8)) <= 2, True)
    # 蒙版变小（分隔条右移）时露出来的是下面主地图的旧像素：Qt/Windows 不会
    # 自动重绘，肉眼就是「往右拖没反应」。必须显式 update 变化的那条竖带。
    repaints = []
    orig_update = m.left.update
    m.left.update = lambda *a: (repaints.append(a), orig_update(*a))
    try:
        m.split = 0.85
        m._preview_handle()
    finally:
        m.left.update = orig_update
    check("蒙版右移后主图被要求重绘", len(repaints), 1)
    check("重绘的是变化的竖条",
          bool(repaints and repaints[0] and repaints[0][0].width() < m.width()), True)
    m.split = 0.5
    m._apply()
    pump(app, 0.1)
    check("回到 50% 蒙版复位",
          abs(m.right.mask().boundingRect().left() - m.width() / 2) <= 2, True)
    m._set_mode("horizontal")
    pump(app, 0.15)
    check("切回分屏清掉蒙版", m.right.mask().isEmpty(), True)

    print("[4] 视图同步")
    old = m.right.scale
    m.left.zoom_at(1.5, m.left.width() / 2, m.left.height() / 2)
    pump(app, 0.2)
    check("右侧跟随缩放", abs(m.right.scale - m.left.scale) < 1e-6, True)
    check("缩放确实变化", abs(m.left.scale - old) > 1, True)

    print("[5] 其它模式")
    m._set_mode("vertical")
    pump(app, 0.2)
    check("上下分屏生效", m.left.height() < m.height(), True)
    m._set_mode("swipe")
    pump(app, 0.2)
    check("卷帘蒙版", m.right.mask().boundingRect().width() > 0, True)
    m._set_mode("fade")
    pump(app, 0.2)
    check("透明度效果", m.right.graphicsEffect() is not None, True)
    m._set_mode("flicker")
    pump(app, 1.5)
    check("闪烁在切换", (m.left.isVisible() != m.right.isVisible()), True)

    print("[6] 退出恢复")
    m.deactivate()
    pump(app, 0.3)
    check("已退出对比", m.active, False)
    check("右侧隐藏", m.right.isVisible(), False)
    check("左侧恢复全部图层", [l.name for l in m.left.layers], ["底图 A", "底图 B"])
    check("显隐引用恢复", m.left.visible is win.visible, True)
    check("最小尺寸恢复", m.left.minimumWidth(), 320)

    print("[7] 对比对话框")
    dlg = MapCompareDialog([r1, r2], r1.id, r2.id, "vertical", win)
    check("方式数量", dlg.mode.count(), len(MODES))
    dlg.right.setCurrentIndex(1)
    dlg.mode.setCurrentIndex(1)
    dlg._accept()
    check("对话框接受", dlg.result(), QDialog.Accepted)
    check("返回左右和方式", dlg.selection(), (r1.id, r2.id, "vertical"))
    dlg.deleteLater()

    print("[8] 按钮进入/退出")
    import native.app as app_mod

    class FakeDialog:
        def __init__(self, *a, **k):
            pass

        def exec(self):
            return QDialog.Accepted

        def selection(self):
            return (r1.id, r2.id, "horizontal")

    orig = app_mod.MapCompareDialog
    app_mod.MapCompareDialog = FakeDialog
    try:
        win.compare_btn.setChecked(False)
        win.compare_btn.click()
        pump(app, 0.3)
        check("按钮进入对比", m.active, True)
        check("按钮保持勾选", win.compare_btn.isChecked(), True)
        win.compare_btn.click()
        pump(app, 0.3)
        check("再次点击退出", m.active, False)
        check("退出后取消勾选", win.compare_btn.isChecked(), False)
    finally:
        app_mod.MapCompareDialog = orig

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
