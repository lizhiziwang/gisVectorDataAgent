"""纯原生桌面入口（PySide6 / Qt）：不开浏览器、不用 WebView。

    python native_app.py      # 带控制台，便于排查
    pythonw native_app.py     # 无控制台（启动.bat 用这个）

启动顺序（有启动画面时）：

    QApplication → 启动画面开场动画 → 导入 native.app（约 1 秒的地理库）
    → MainWindow(on_stage=…) 构建界面（进度条跟着真实阶段走）→ 窗口显示 → 淡出

启动画面关掉的三种方式：`GIS_AGENT_NO_SPLASH=1`、命令行 `--no-splash`、离屏/无头平台。
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LOG_PATH = ROOT / "data" / "app.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def prepare_app(app) -> bool:
    """建窗口之前立好「应用身份 + 图标」—— 任务栏按钮认的就是这两样。

    Windows 上从 `pythonw.exe`（`启动.bat` 就是这种起法）运行脚本时，进程默认
    没有自己的 AppUserModelID，Windows 会把窗口算成「Python 的一个窗口」，任务栏
    按钮画 Python 的图标；窗口自己设的图标只作用于标题栏，管不到任务栏。
    所以顺序很重要：先声明身份，再设图标，最后才建窗口。

    返回是否成功声明了 Windows 应用身份（其它平台 / 失败都是 False，不影响启动）。
    """
    from native.branding import app_icon, set_windows_app_id

    ok = set_windows_app_id()
    app.setWindowIcon(app_icon())
    return ok


def main() -> int:
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    QApplication.setApplicationName("GIS 矢量数据 Agent")
    QApplication.setOrganizationName("local")
    # 已经有一个 QApplication 就复用（测试会先建好再调 main()）
    app = QApplication.instance() or QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei UI", 9))
    prepare_app(app)

    # 启动画面要赶在导入 native.app 之前立起来：那一步要载入 geopandas / shapely
    # 等地理库（约 1 秒），没有它会是一段「双击了但什么都没发生」的空白。
    # 顺带把 QApplication 排到 rasterio/GDAL 前面 —— GDAL 必须在 Qt 之后导入
    # （见 tools/test_raster.py 顶注），否则 Windows 上会 access violation。
    from native.splash import SplashScreen

    splash = SplashScreen.maybe()
    if splash is not None:
        try:
            splash.start()
        except Exception:      # noqa: BLE001  启动画面不该拦住启动
            splash.abort()
            splash = None

    try:
        from native.app import MainWindow, install_qt_translations

        # 让输入框右键菜单 / 文件对话框走 Qt 自带中文翻译
        install_qt_translations(app)
        win = MainWindow(on_stage=splash.stage if splash is not None else None)
        win.show()
    except Exception:
        # 出错先收掉启动画面：它是置顶窗口，留着会把错误弹窗压在下面
        if splash is not None:
            splash.abort()
        raise
    if splash is not None:
        splash.finish()
    return app.exec()


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        detail = traceback.format_exc()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] fatal:\n{detail}\n")
        except Exception:
            pass
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "启动失败", detail[-1500:])
        except Exception:
            pass
        code = 1
    sys.exit(code)
