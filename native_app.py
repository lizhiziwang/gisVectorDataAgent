"""纯原生桌面入口（PySide6 / Qt）：不开浏览器、不用 WebView。

    python native_app.py      # 带控制台，便于排查
    pythonw native_app.py     # 无控制台（启动.bat 用这个）
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


def main() -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from native.app import MainWindow, make_icon

    QApplication.setApplicationName("GIS 矢量数据 Agent")
    QApplication.setOrganizationName("local")
    app = QApplication(sys.argv)
    app.setWindowIcon(make_icon())
    app.setFont(QFont("Microsoft YaHei UI", 9))
    win = MainWindow()
    win.show()
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
