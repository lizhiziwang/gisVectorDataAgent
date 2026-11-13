"""品牌标识：应用图标的几何与绘制 + Windows 应用身份。

主窗口图标、任务栏图标、启动画面里的 logo 都从这里取同一份几何
（`ICON_SHAPE`），改图标只改这一处，不会出现「窗口图标」和「启动画面 logo」
长得不一样的问题。

图标语言：陶土橙圆角底 + 一个「面要素」轮廓 + 三个顶点句柄。
一眼能读出「矢量面 + 可编辑节点」，也和小尺寸下最稳（没有细线、没有多层嵌套）。
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF

#: 陶土橙底（和 theme.COLORS["accent_fill"] 同色，这里独立一份避免循环 import）
ICON_BG = "#d97757"

#: 任务栏「应用身份」。Windows 按它给窗口分组、决定按钮上画什么图标。
#: 由 `启动.bat` 起的进程实际是 `pythonw.exe`，不声明这个 ID 就会被算成
#: 「Python 的一个窗口」，任务栏显示 Python 的图标而不是我们自己的。
APP_USER_MODEL_ID = "GisAgent.VectorDataAgent"

#: 64×64 设计稿上的「面要素」顶点
ICON_SHAPE = [(15, 24), (29, 16), (50, 21), (47, 42), (27, 50), (13, 41)]

#: 主体整体左移 1.5、下移 1.5，让右上角的「后层」有安全边距
ICON_SHIFT = (-1.5, 1.5)


def icon_body_points() -> list[tuple[float, float]]:
    """主体顶点（已做整体偏移，落在 64×64 的坐标系里）。"""
    dx, dy = ICON_SHIFT
    return [(x + dx, y + dy) for x, y in ICON_SHAPE]


def icon_pixmap(size: int) -> QPixmap:
    """按 64×64 的设计稿等比画到指定尺寸。

    小尺寸要「减笔画」：16/20 px 下双层轮廓 + 句柄会糊成一团，
    那里只留一个实心面；24~40 画轮廓 + 句柄；再大才加后层叠置。
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 64.0, size / 64.0)

    p.setPen(Qt.NoPen)
    p.setBrush(QColor(ICON_BG))
    p.drawRoundedRect(1.5, 1.5, 61.0, 61.0, 15.0, 15.0)

    pts = icon_body_points()
    shape = QPolygonF([QPointF(x, y) for x, y in pts])

    detail = 0 if size <= 24 else (1 if size <= 40 else 2)
    if detail == 0:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawPolygon(shape)
    else:
        if detail == 2:
            pen = QPen(QColor(255, 255, 255, 84))
            pen.setWidthF(2.0)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(QPolygonF([QPointF(x + 3.4, y - 4.2) for x, y in pts]))

        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(3.0 if detail == 1 else 2.6)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(QColor(255, 255, 255, 52) if detail == 2 else Qt.NoBrush)
        p.drawPolygon(shape)

        # 句柄必须落在顶点上（用同一份 pts，别再手算偏移）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        for x, y in (pts[1], pts[3], pts[5]):
            p.drawRoundedRect(x - 2.8, y - 2.8, 5.6, 5.6, 1.5, 1.5)
    p.end()
    return pm


def make_icon() -> QIcon:
    """多尺寸应用图标（任务栏 16/24、窗口 32/48、关于页 128 都清晰）。"""
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(icon_pixmap(s))
    return ic


def app_icon() -> QIcon:
    """应用 / 窗口 / 任务栏共用的图标。

    现在就是 `make_icon()`：一份几何画到底，比拿 `assets/icon.ico` 里的旧帧
    更一致（那份 .ico 是早先导出的简版，少了顶点句柄）。留一个名字是因为
    入口、主窗口、测试都该从「应用图标」这个语义取，而不是各记一遍尺寸表。
    """
    return make_icon()


def _set_app_id_win32(app_id: str) -> bool:
    """真正调 shell32 的那一步（单独拎出来，测试里可以替换掉）。"""
    import ctypes

    fn = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    fn.argtypes = [ctypes.c_wchar_p]
    fn.restype = ctypes.c_long
    return int(fn(app_id)) == 0


def set_windows_app_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Windows：把本进程声明成一个独立应用（任务栏图标/分组的前提）。

    必须在建窗口之前调用。从 `pythonw.exe` 起的脚本默认没有自己的
    AppUserModelID，Windows 会把窗口归到 Python 名下，任务栏按钮画的是
    Python 那张图标 —— 窗口自己设的图标只影响标题栏，管不到任务栏。

    非 Windows 平台、或系统调用失败时返回 False（只影响图标，不影响启动）；
    重复调用是幂等的。
    """
    if sys.platform != "win32":
        return False
    try:
        return _set_app_id_win32(app_id)
    except Exception:      # noqa: BLE001  图标问题不该拦住启动
        return False
