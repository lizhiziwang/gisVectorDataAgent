"""启动画面回归：阶段推进 / 绘制 / 开关 / 最短时长 / 点按跳过 / 主窗口回调。

用法：.venv/Scripts/python.exe tools/test_splash.py

离屏平台不显示启动画面（`SplashScreen.maybe()` 返回 None），所以这里直接建
`SplashScreen()` 测动画，再用 `MainWindow(on_stage=…)` 验阶段回调的接线。
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")
os.environ.setdefault("GIS_AGENT_CONFIRM_STEPS", "0")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    from native.splash import STAGES, SplashScreen

    app = QApplication.instance() or QApplication([])

    print("[1] 开关")
    check("离屏平台不显示启动画面", SplashScreen.maybe(), None)
    os.environ["GIS_AGENT_NO_SPLASH"] = "1"
    check("GIS_AGENT_NO_SPLASH=1 关掉", SplashScreen.maybe(), None)
    os.environ.pop("GIS_AGENT_NO_SPLASH", None)

    print("[2] 卡片几何 / 窗口类型")
    s = SplashScreen()
    check("窗口比卡片大一圈（留给投影）",
          s.width() == SplashScreen.CARD_W + 2 * SplashScreen.SPREAD
          and s.height() == SplashScreen.CARD_H + 2 * SplashScreen.SPREAD, True)
    flags = s.windowFlags()
    check("无边框", bool(flags & Qt.FramelessWindowHint), True)
    check("置顶（盖住主窗口出现前的桌面）", bool(flags & Qt.WindowStaysOnTopHint), True)
    check("半透明底（圆角 + 投影要它）",
          s.testAttribute(Qt.WA_TranslucentBackground), True)

    print("[3] 阶段表")
    order = ["boot", "session", "ui", "map", "ready"]
    fracs = [STAGES[k][2] for k in order]
    check("进度只增不减", fracs == sorted(fracs), True)
    check("从 >0 起步、收在 100%", fracs[0] > 0 and fracs[-1] == 1.0, True)
    check("每档都有标题和副标题",
          all(t and d for t, d, _ in (STAGES[k] for k in order)), True)

    s.stage("session")
    check("切到 session 阶段",
          (s._stage_key, s._title, s._detail)
          == ("session", STAGES["session"][0], STAGES["session"][1]), True)
    check("进度立刻往前走", 0.0 < s._progress < 1.0, True)
    s.stage("不存在的阶段")
    check("未知阶段被忽略（不炸、不改文案）",
          s._stage_key == "session", True)

    print("[4] 绘制")
    sizes = set()
    for intro in (0.0, 0.1, 0.35, 0.7, 1.0):
        s._intro = intro
        for prog in (0.0, 0.5, 1.0):
            s._progress = prog
            pm = s.grab()          # 真跑一遍 paintEvent，有异常会抛出来
            sizes.add((pm.width(), pm.height()))
    check("各种动画状态下都能画出来",
          sizes == {(s.width(), s.height())}, True)
    small = SplashScreen()
    small._intro = 1.0
    small._progress = 1.0
    check("就绪态不是一张空图", not small.grab().toImage().isNull(), True)

    print("[5] 开场 / 收尾")
    s2 = SplashScreen()
    t0 = time.perf_counter()
    s2.start()
    intro_ms = (time.perf_counter() - t0) * 1000
    check("start() 播完开场", s2._intro == 1.0, True)
    check("开场确实用了几百毫秒（不是一闪）", intro_ms >= 300, True)
    check("开场后窗口可见", s2.isVisible(), True)
    t0 = time.perf_counter()
    s2.finish(min_ms=0)
    finish_ms = (time.perf_counter() - t0) * 1000
    check("finish() 后窗口关掉", s2.isVisible(), False)
    check("finish() 后进度 100%", abs(s2._progress - 1.0) < 1e-6, True)
    check("finish(min_ms=0) 很快收尾", finish_ms < 2500, True)

    print("[6] 最短展示时长 / 点按跳过")
    s3 = SplashScreen()
    s3.start()
    t0 = time.perf_counter()
    s3.finish(min_ms=600)
    wait_ms = (time.perf_counter() - t0) * 1000
    check("min_ms=600 时不会提前收", wait_ms >= 600, True)

    s4 = SplashScreen()
    s4.start()
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(4, 4), QPointF(4, 4),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    s4.mousePressEvent(ev)
    check("鼠标按下 = 跳过", s4._skip, True)
    t0 = time.perf_counter()
    s4.finish(min_ms=8000)
    skip_ms = (time.perf_counter() - t0) * 1000
    check("点过之后不再等满最短时长", skip_ms < 1200, True)

    print("[7] 主窗口阶段回调")
    from native.app import MainWindow

    seen: list[str] = []
    win = MainWindow(on_stage=seen.append)
    check("回报了 session / ui / map", {"session", "ui", "map"} <= set(seen), True)
    check("阶段名都在 STAGES 里", set(seen) <= set(STAGES), True)
    check("顺序是 session → ui → map",
          seen.index("session") < seen.index("ui") < seen.index("map"), True)
    win2 = MainWindow()          # 不给回调：和 tools/ 下的用法一致
    check("不传 on_stage 也能建窗口", win2.windowTitle(), "GIS 矢量数据 Agent")
    win.close()
    win2.close()
    app.processEvents()

    print("[8] 真实启动入口（native_app.main）")
    # 把 exec 换成「立刻返回」：不真的进事件循环，但整条启动路径（建 QApplication →
    # 启动画面 → 主窗口 → 淡出）都会真跑一遍。GIS_AGENT_SPLASH=1 让离屏也显示启动画面。
    from PySide6 import QtWidgets as QtW
    import native_app

    entered: list[int] = []
    orig_exec = QtW.QApplication.exec
    QtW.QApplication.exec = staticmethod(lambda *a, **k: (entered.append(1), 0)[1])
    os.environ["GIS_AGENT_SPLASH"] = "1"
    try:
        rc = native_app.main()
    finally:
        QtW.QApplication.exec = orig_exec
        os.environ.pop("GIS_AGENT_SPLASH", None)
    check("main() 走到事件循环并正常返回", (rc, bool(entered)), (0, True))
    for w in QApplication.topLevelWidgets():
        w.close()                # 收掉 main() 建的主窗口（停渲染线程）
    app.processEvents()

    print(f"\n结论: {'PASS' if not FAILS else 'FAIL ' + str(FAILS)}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
