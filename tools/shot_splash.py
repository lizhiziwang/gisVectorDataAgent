"""启动画面截图：把开场 / 各阶段 / 就绪 这几帧拼到灰底上，用来核对视觉。

离屏平台在这台机器上没有中文字体（全是豆腐块），所以和 shot_tools.py 一样
用真 windows 平台 + WA_DontShowOnScreen：能拿到系统字体，又不会真的弹窗。

用法：
    .venv\\Scripts\\python.exe tools\\shot_splash.py            # 存 docs/perf
    .venv\\Scripts\\python.exe tools\\shot_splash.py <目录>     # 指定输出目录
输出：splash-intro.png / splash-mid.png / splash-session.png / splash-ui.png
     / splash-map.png / splash-ready.png
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from native.splash import SplashScreen

OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "perf")


def compose(pm: QPixmap, name: str) -> str:
    """把透明底的卡片贴到一块桌面灰上（顺便看投影的观感）。"""
    pad = 34
    canvas = QPixmap(pm.width() + pad * 2, pm.height() + pad * 2)
    p = QPainter(canvas)
    grad = QLinearGradient(QPointF(0, 0), QPointF(0, canvas.height()))
    grad.setColorAt(0.0, QColor("#cfd2d4"))
    grad.setColorAt(1.0, QColor("#e4e3df"))
    p.fillRect(canvas.rect(), grad)
    p.drawPixmap(pad, pad, pm)
    p.end()
    path = os.path.join(OUT_DIR, name)
    canvas.save(path)
    print(f"已截图 {path}  {canvas.width()}x{canvas.height()}", flush=True)
    return path


app = QApplication(sys.argv)
splash = SplashScreen()
splash.setAttribute(Qt.WA_DontShowOnScreen, True)      # 能画、但不显示
splash.show()
app.processEvents()

# 开场：图标正在描边、文字正在浮上来
for tag, intro in (("splash-intro", 0.30), ("splash-mid", 0.62)):
    splash._intro = intro
    splash._phase = 1.05
    splash._progress = 0.06 * min(1.0, intro / 0.55)      # 和 _play_intro 里同一套推进
    splash.repaint()
    compose(splash.grab(), f"{tag}.png")

# 各真实阶段（进度按 STAGES 走）
for tag, stage, progress in (("splash-session", "session", 0.34),
                             ("splash-ui", "ui", 0.62),
                             ("splash-map", "map", 0.86),
                             ("splash-ready", "ready", 1.0)):
    splash._intro = 1.0
    splash._phase = 1.05
    splash.stage(stage)
    splash._progress = progress
    splash.repaint()
    compose(splash.grab(), f"{tag}.png")

splash.close()
app.processEvents()
print("关闭正常", flush=True)
