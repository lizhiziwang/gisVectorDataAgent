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

    print("[5] 边框拖拽缩放（无边框窗口要自己回答 WM_NCHITTEST）")
    from PySide6.QtCore import QPoint, QRect

    from native.app import (EDGE_BOTTOM, EDGE_LEFT, EDGE_RIGHT, EDGE_TOP, RESIZE_MARGIN,
                            edge_at, hit_test_code, msg_screen_point)

    rect = QRect(360, 120, 1200, 800)

    def at(x, y):
        return hit_test_code(edge_at(QPoint(x, y), rect))

    check("左边框", at(361, 500), 10)      # HTLEFT
    check("右边框", at(1558, 500), 11)     # HTRIGHT
    check("上边框", at(800, 121), 12)      # HTTOP
    check("下边框", at(800, 918), 15)      # HTBOTTOM
    check("左上角", at(361, 121), 13)      # HTTOPLEFT
    check("右上角", at(1558, 121), 14)     # HTTOPRIGHT
    check("左下角", at(361, 918), 16)      # HTBOTTOMLEFT
    check("右下角", at(1558, 918), 17)     # HTBOTTOMRIGHT
    check("窗口内部不是边框", at(800, 500), 0)
    check("边框内侧 %dpx 仍是内部" % (RESIZE_MARGIN + 4),
          at(360 + RESIZE_MARGIN + 4, 500), 0)
    check("窗口外不算边框", at(100, 100), 0)
    check("边掩码完整", (EDGE_LEFT, EDGE_RIGHT, EDGE_TOP, EDGE_BOTTOM), (1, 2, 4, 8))

    # MSG.lParam 是物理像素、按 16 位有符号拆，负坐标（副屏在左边）不能错
    class _Msg:
        pass

    m = _Msg()
    m.lParam = ((500 & 0xFFFF) << 16) | (361 & 0xFFFF)
    check("lParam 正坐标", (msg_screen_point(m).x(), msg_screen_point(m).y()), (361, 500))
    m.lParam = ((500 & 0xFFFF) << 16) | (-1920 & 0xFFFF)
    check("lParam 负坐标", msg_screen_point(m).x(), -1920)

    # 窗口自己的命中计算：offsscreen 下没有真实 HWND，只验证几何判断
    win.resize(1200, 800)
    app.processEvents()
    g = win.frameGeometry()
    check("窗口左边命中", win._resize_edge(QPoint(g.left() + 1, g.top() + 300)),
          EDGE_LEFT)
    check("窗口中间不命中", win._resize_edge(QPoint(g.left() + 300, g.top() + 300)), 0)

    if QApplication.platformName() == "windows":
        import ctypes

        user32 = ctypes.WinDLL("user32")
        hwnd = int(win.winId())
        lparam = lambda x, y: ((y & 0xFFFF) << 16) | (x & 0xFFFF)  # noqa: E731
        got = user32.SendMessageW(hwnd, 0x0084, 0, lparam(g.left() + 1, g.top() + 300))
        check("系统查询左边框", got, 10)
        got = user32.SendMessageW(hwnd, 0x0084, 0, lparam(g.left() + 300, g.top() + 300))
        check("系统查询窗口内部", got, 1)
        style = user32.GetWindowLongPtrW(hwnd, -16)
        check("补了 WS_THICKFRAME", bool(style & 0x00040000), True)
    else:
        print("  跳过  真实 HWND 命中（当前平台不是 windows，回归默认 offscreen）")

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
