"""回归：地图鼠标悬停已移除，移动鼠标不能触发拾取/命中测试。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from native.mapview import MapView

app = QApplication([])
mv = MapView()
mv.resize(480, 320)

calls = []
mv._hit_test = lambda *a, **k: calls.append(a) or None

ev = QMouseEvent(QEvent.MouseMove, QPointF(120, 80), QPointF(120, 80),
                 Qt.NoButton, Qt.NoButton, Qt.NoModifier)
app.sendEvent(mv, ev)
app.processEvents()

ok = True


def check(name: str, got: bool) -> None:
    global ok
    print(f"  {'ok  ' if got else 'FAIL'} {name}")
    ok = ok and got


check("鼠标移动不触发命中测试", calls == [])
check("不再提供 hoverChanged 信号", not hasattr(mv, "hoverChanged"))
check("不再保留 _hover 状态", not hasattr(mv, "_hover"))

mv.shutdown()
mv.close()
print("\nPASS" if ok else "\nFAIL")
raise SystemExit(0 if ok else 1)
