"""无边框窗口回归：标题栏 / 最小化最大化关闭 / 尺寸手柄。

用法：.venv/Scripts/python.exe tools/test_frameless.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")
os.environ.setdefault("GIS_AGENT_CONFIRM_STEPS", "0")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    from native.app import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.resize(1200, 800)
    win.show()
    app.processEvents()

    print("[1] 无边框")
    check("窗口无系统标题栏", bool(win.windowFlags() & Qt.FramelessWindowHint), True)
    check("顶部标题栏存在", win.btn_agent_settings.parentWidget() is not None, True)

    print("[2] 窗口按钮在 Agent 设置右侧")
    check("最小化按钮", win.btn_min.text(), "—")
    check("最大化按钮", win.btn_max.text(), "□")
    check("关闭按钮", win.btn_close.text(), "×")
    check("排列顺序",
          (win.btn_agent_settings.x() < win.btn_min.x() < win.btn_max.x()
           < win.btn_close.x()), True)
    check("关闭按钮在窗口内", win.btn_close.geometry().right() <= win.width(), True)

    print("[3] 最大化 / 还原")
    win.toggle_maximize()
    app.processEvents()
    check("最大化生效", win.isMaximized(), True)
    check("最大化图标", win.btn_max.text(), "❐")
    win.toggle_maximize()
    app.processEvents()
    check("还原生效", win.isMaximized(), False)
    check("还原图标", win.btn_max.text(), "□")

    print("[4] 尺寸手柄")
    check("右下角尺寸手柄", win.size_grip.isVisible(), True)

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
