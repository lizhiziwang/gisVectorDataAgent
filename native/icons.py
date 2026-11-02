"""工作区里的小图标：用 QPainter 画，不依赖系统主题图标。

系统 `SP_DirIcon` / `SP_FileDialogDetailedView` 在不同 Windows 主题下是
黄色文件夹、绿色小方块，和暖白 + 陶土橙的界面完全不在一个语言里。
这里统一画一套 16px 线性图标：
  · 工作区/分组：文件夹
  · 会话：对话气泡
  · 会话图层：叠片
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

_INK = "#6f6c64"
_MUTED = "#9a968c"
_ACCENT = "#b0522f"
_WARM = "#f1eadb"
_WARM_ACTIVE = "#f6e4d8"


def _pixmap(size: int, draw) -> QPixmap:
    dpr = 2.0
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 16.0, size / 16.0)
    draw(p)
    p.end()
    return pm


def _icon(size: int, draw) -> QIcon:
    return QIcon(_pixmap(size, draw))


def folder_icon(size: int = 16, active: bool = False) -> QIcon:
    color = _ACCENT if active else _INK

    def draw(p: QPainter) -> None:
        body = QPainterPath()
        body.moveTo(2.0, 5.0)
        body.lineTo(2.0, 12.0)
        body.quadTo(2.0, 13.3, 3.3, 13.3)
        body.lineTo(12.7, 13.3)
        body.quadTo(14.0, 13.3, 14.0, 12.0)
        body.lineTo(14.0, 6.4)
        body.quadTo(14.0, 5.2, 12.8, 5.2)
        body.lineTo(7.5, 5.2)
        body.lineTo(6.1, 3.4)
        body.quadTo(5.8, 3.0, 5.2, 3.0)
        body.lineTo(3.2, 3.0)
        body.quadTo(2.0, 3.0, 2.0, 4.2)
        body.closeSubpath()
        p.setPen(QPen(QColor(color), 1.25, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(_WARM_ACTIVE if active else _WARM))
        p.drawPath(body)

    return _icon(size, draw)


def session_icon(size: int = 16, active: bool = False) -> QIcon:
    color = _ACCENT if active else _INK

    def draw(p: QPainter) -> None:
        bubble = QPainterPath()
        bubble.addRoundedRect(QRectF(2.2, 2.8, 11.6, 8.8), 2.2, 2.2)
        # 小尾巴：从气泡左下角折出去
        tail = QPainterPath()
        tail.moveTo(5.0, 11.2)
        tail.lineTo(4.3, 14.0)
        tail.lineTo(7.2, 11.6)
        tail.closeSubpath()
        bubble = bubble.united(tail)
        p.setPen(QPen(QColor(color), 1.15, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor(_WARM_ACTIVE if active else "#f7f4ec"))
        p.drawPath(bubble)

        p.setPen(QPen(QColor(color), 1.0, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(5.0, 6.2), QPointF(11.0, 6.2))
        p.drawLine(QPointF(5.0, 8.7), QPointF(9.2, 8.7))

    return _icon(size, draw)


def layer_icon(size: int = 16) -> QIcon:
    def draw(p: QPainter) -> None:
        p.setPen(QPen(QColor("#8a6a4f"), 1.05, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor("#f1eadb"))
        # 三张叠片：最上面一张实一点，下面两张只露边
        for dy, fill in ((1.0, QColor("#f1eadb")), (3.4, Qt.NoBrush), (5.8, Qt.NoBrush)):
            poly = QPainterPath()
            poly.moveTo(8.0, 2.1 + dy)
            poly.lineTo(13.6, 5.0 + dy)
            poly.lineTo(8.0, 7.9 + dy)
            poly.lineTo(2.4, 5.0 + dy)
            poly.closeSubpath()
            p.setBrush(fill)
            p.drawPath(poly)

    return _icon(size, draw)


def search_icon(size: int = 14) -> QIcon:
    def draw(p: QPainter) -> None:
        p.setPen(QPen(QColor(_MUTED), 1.25, Qt.SolidLine, Qt.RoundCap))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(3.0, 3.0, 7.2, 7.2))
        p.drawLine(QPointF(9.6, 9.6), QPointF(13.0, 13.0))

    return _icon(size, draw)


def plus_icon(size: int = 14, color: str = _INK) -> QIcon:
    def draw(p: QPainter) -> None:
        p.setPen(QPen(QColor(color), 1.35, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(8.0, 3.5), QPointF(8.0, 12.5))
        p.drawLine(QPointF(3.5, 8.0), QPointF(12.5, 8.0))

    return _icon(size, draw)


def panel_icon(size: int = 15, color: str = _INK) -> QIcon:
    """右侧面板开关：外框 + 一列分隔线。"""
    def draw(p: QPainter) -> None:
        p.setPen(QPen(QColor(color), 1.15, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(2.2, 3.0, 11.6, 10.0), 1.6, 1.6)
        p.drawLine(QPointF(9.2, 3.4), QPointF(9.2, 12.6))

    return _icon(size, draw)

