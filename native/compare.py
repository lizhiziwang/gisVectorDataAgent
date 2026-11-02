"""地图对比：左右/上下分屏、卷帘、透明度、闪烁。

对比容器把主地图和一张副地图叠在同一个 QWidget 里：
· 分屏模式按比例摆两块 MapView，中间手柄可拖动；
· 卷帘模式两块地图都铺满，副图用 QRegion 裁出右侧/下侧；
· 透明度/闪烁模式两块地图叠在一起，由效果或定时器切换。

两张地图共用同一套视图参数（scale/tx/ty），拖拽任意一侧都会同步另一侧。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, Qt, QTimer
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QGraphicsOpacityEffect, QLabel, QMessageBox,
                               QVBoxLayout, QWidget)

from .mapview import MapView

MODES = [
    ("左右分屏", "horizontal"),
    ("上下分屏", "vertical"),
    ("卷帘拖动", "swipe"),
    ("透明度叠加", "fade"),
    ("闪烁对比", "flicker"),
]

MODE_LABEL = {key: label for label, key in MODES}


class MapCompareDialog(QDialog):
    """选择左/右底图和对比方式。"""

    def __init__(self, rasters: list, left_id: str | None = None,
                 right_id: str | None = None, mode: str = "horizontal", parent=None):
        super().__init__(parent)
        self.setWindowTitle("地图对比")
        self.setMinimumWidth(380)
        rasters = list(rasters)
        self._mode = mode
        vl = QVBoxLayout(self)
        vl.setContentsMargins(16, 14, 16, 12)
        vl.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)
        self.left = QComboBox()
        self.right = QComboBox()
        for r in rasters:
            self.left.addItem(r.name, r.id)
            self.right.addItem(r.name, r.id)
        ids = [r.id for r in rasters]
        if left_id in ids:
            self.left.setCurrentIndex(ids.index(left_id))
        if right_id in ids:
            self.right.setCurrentIndex(ids.index(right_id))
        elif len(ids) > 1:
            self.right.setCurrentIndex(1)
        self.mode = QComboBox()
        for label, key in MODES:
            self.mode.addItem(label, key)
        idx = self.mode.findData(mode)
        if idx >= 0:
            self.mode.setCurrentIndex(idx)
        form.addRow("左侧底图", self.left)
        form.addRow("右侧底图", self.right)
        form.addRow("对比方式", self.mode)
        vl.addLayout(form)

        tip = QLabel("左右/上下分屏默认各占 50%，拖动中间分隔条可调整比例。\n"
                     "卷帘：拖动分界线露出另一侧；透明度/闪烁：两图叠加观察差异。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        vl.addWidget(tip)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("开始对比")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        vl.addWidget(buttons)

    def _accept(self) -> None:
        if self.left.currentData() == self.right.currentData():
            QMessageBox.warning(self, "地图对比", "左侧和右侧底图不能是同一个图层。")
            self.right.setFocus()
            return
        self.accept()

    def selection(self) -> tuple[str, str, str]:
        return (str(self.left.currentData()), str(self.right.currentData()),
                str(self.mode.currentData() or "horizontal"))


class _CompareHandle(QWidget):
    """可拖动的分隔条。"""

    def __init__(self, owner: "MapCompareWidget"):
        super().__init__(owner)
        self._owner = owner
        self.setObjectName("compareHandle")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.SplitHCursor)

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._owner.begin_drag(e.position())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._owner.dragging:
            self._owner.drag_to(e.position())
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._owner.end_drag()
        super().mouseReleaseEvent(e)


class MapCompareWidget(QWidget):
    """两张 MapView 的对比容器；不激活时只显示主地图。"""

    def __init__(self, left_map: MapView, parent=None):
        super().__init__(parent)
        self.left = left_map
        self.left.setParent(self)
        self.right = MapView(self)
        self.right.hide()
        self.right.visible = {}
        self.active = False
        self.mode = "horizontal"
        self.split = 0.5
        self.dragging = False
        self._left_visible = None
        self._left_layers = None
        self._left_selected = None
        self._right_selected = None
        self._blocked = False
        self._flicker_on = True

        self.handle = _CompareHandle(self)
        self.handle.hide()
        self._guide = QWidget(self)
        self._guide.setObjectName("compareGuide")
        self._guide.setAttribute(Qt.WA_StyledBackground, True)
        self._guide.hide()

        self._opacity = QGraphicsOpacityEffect(self.right)
        self._opacity.setOpacity(0.5)

        self._flicker = QTimer(self)
        self._flicker.setInterval(700)
        self._flicker.timeout.connect(self._flicker_tick)

        self.left.viewChanged.connect(self._sync_from_left)
        self.right.viewChanged.connect(self._sync_from_right)

    # ------------------------------------------------------------- 对外接口
    def activate(self, left_id: str, right_id: str, mode: str,
                 layers: list, visible: dict) -> None:
        was_active = self.active
        if not was_active:
            self._left_visible = self.left.visible
            self._left_layers = list(self.left.layers)
            self._left_min = self.left.minimumSize()
            self._right_min = self.right.minimumSize()
            # 分屏后每侧可能 < 320px，主地图原本的最小尺寸会阻止收缩
            self.left.setMinimumSize(1, 1)
            self.right.setMinimumSize(1, 1)
        self._left_selected = left_id
        self._right_selected = right_id
        rasters = [l for l in layers if getattr(l, "is_raster", False)]
        vectors = [l for l in layers if not getattr(l, "is_raster", False)]
        left_ras = next((l for l in rasters if l.id == left_id), None)
        right_ras = next((l for l in rasters if l.id == right_id), None)
        if left_ras is None or right_ras is None:
            raise ValueError("对比底图已经不存在")
        left_layers = vectors + [left_ras]
        right_layers = vectors + [right_ras]
        base = dict(visible)
        lv = dict(base)
        rv = dict(base)
        for r in rasters:
            lv[r.id] = (r.id == left_id)
            rv[r.id] = (r.id == right_id)
        self.left.visible = lv
        self.left.set_layers(left_layers)
        self.right.visible = rv
        self.right.set_layers(right_layers)
        self.active = True
        if not was_active:
            self.split = 0.5
        self._set_mode(mode)
        self.right.set_view(self.left.scale, self.left.tx, self.left.ty)
        self.left.update()
        self.right.update()
        self._apply()

    def deactivate(self) -> None:
        self._stop_modes()
        self.active = False
        self.right.hide()
        self.handle.hide()
        self._guide.hide()
        self.left.setGraphicsEffect(None)
        try:
            self.left.visible = self._left_visible
            self.left.set_layers(self._left_layers)
        except Exception:
            pass
        try:
            self.left.setMinimumSize(self._left_min)
            self.right.setMinimumSize(self._right_min)
        except Exception:
            pass
        self.left.setGeometry(self.rect())
        self.left.update()

    def shutdown(self) -> None:
        self._stop_modes()
        try:
            self.right.shutdown()
        except Exception:
            pass

    def set_layer_visibility(self, layer_id: str, visible: bool) -> None:
        for m in (self.left, self.right):
            try:
                if layer_id in m.visible:
                    m.visible[layer_id] = bool(visible)
                    m.invalidate()
            except Exception:
                pass

    def refresh_layers(self, layers: list, visible: dict) -> None:
        """数据集变化时按当前左右底图重建，保持对比状态。"""
        if not self.active:
            return
        try:
            self.activate(self._left_selected, self._right_selected,
                          self.mode, layers, visible)
        except Exception:
            self.deactivate()

    # ------------------------------------------------------------- 模式/布局
    def _set_mode(self, mode: str) -> None:
        self._stop_modes()
        self._guide.hide()
        self.mode = mode if mode in MODE_LABEL else "horizontal"
        self.right.setGraphicsEffect(None)
        self.right.clearMask()
        self.right.show()
        if self.mode == "fade":
            self.right.setGraphicsEffect(self._opacity)
            self._opacity.setOpacity(0.5)
        elif self.mode == "flicker":
            self._flicker.start()
        self._apply()

    def _stop_modes(self) -> None:
        if self._flicker.isActive():
            self._flicker.stop()
        self._flicker_on = True
        try:
            self.left.show()
            self.right.show()
            self.right.setGraphicsEffect(None)
            self.right.clearMask()
        except Exception:
            pass

    def _apply(self) -> None:
        r = self.rect()
        if r.width() < 2 or r.height() < 2:
            return
        if not self.active:
            self.left.setGeometry(r)
            self.right.hide()
            self.handle.hide()
            self._guide.hide()
            return
        if self.dragging:
            # 拖动期间绝对不要连续 setGeometry：两个 MapView 的渲染/合成线程
            # 会在旧图和新尺寸之间打架，Windows 上会出现竖条纹。
            self._preview_handle()
            return
        self.left.show()
        self.right.show()
        gap = 10
        x = int(r.width() * self.split)
        y = int(r.height() * self.split)
        x = max(40, min(r.width() - 40, x))
        y = max(40, min(r.height() - 40, y))
        if self.mode == "horizontal":
            # 两块地图直接相接，手柄浮在接缝上；留 gap 会在拖动时留下
            # 一条旧的分隔空白，看起来像第二根分隔条。
            lw = max(1, x)
            self.left.setGeometry(0, 0, lw, r.height())
            self.right.setGeometry(lw, 0, max(1, r.width() - lw), r.height())
            self.handle.setCursor(Qt.SplitHCursor)
            self.handle.setGeometry(lw - gap // 2, 0, gap, r.height())
            self.handle.show()
            self.handle.raise_()
        elif self.mode == "vertical":
            lh = max(1, y)
            self.left.setGeometry(0, 0, r.width(), lh)
            self.right.setGeometry(0, lh, r.width(), max(1, r.height() - lh))
            self.handle.setCursor(Qt.SplitVCursor)
            self.handle.setGeometry(0, lh - gap // 2, r.width(), gap)
            self.handle.show()
            self.handle.raise_()
        elif self.mode == "swipe":
            self.left.setGeometry(r)
            self.right.setGeometry(r)
            mw = r.width() - x
            self.right.setMask(QRegion(x, 0, max(0, mw), r.height()) if mw > 0
                               else QRegion())
            self.handle.setCursor(Qt.SplitHCursor)
            self.handle.setGeometry(x - gap // 2, 0, gap, r.height())
            self.handle.show()
            self.handle.raise_()
        else:  # fade / flicker
            self.left.setGeometry(r)
            self.right.setGeometry(r)
            self.handle.hide()
            self.right.raise_()

    # ------------------------------------------------------------- 拖拽
    def _preview_handle(self) -> None:
        """拖动时只移动手柄和橙色引导线，不重排地图。"""
        r = self.rect()
        gap = 10
        if self.mode in ("horizontal", "swipe"):
            x = int(r.width() * self.split)
            x = max(40, min(r.width() - 40, x))
            self.handle.setGeometry(x - gap // 2, 0, gap, r.height())
            self._guide.setGeometry(x - 1, 0, 2, r.height())
        elif self.mode == "vertical":
            y = int(r.height() * self.split)
            y = max(40, min(r.height() - 40, y))
            self.handle.setGeometry(0, y - gap // 2, r.width(), gap)
            self._guide.setGeometry(0, y - 1, r.width(), 2)
        else:
            self.handle.hide()
            self._guide.hide()
            return
        self.handle.show()
        self.handle.raise_()
        self._guide.show()
        self._guide.raise_()

    def begin_drag(self, pos: QPointF) -> None:
        self.dragging = True
        self.split = self._ratio(pos)
        self._preview_handle()

    def drag_to(self, pos: QPointF) -> None:
        if not self.dragging:
            return
        self.split = self._ratio(pos)
        self._preview_handle()

    def end_drag(self) -> None:
        self.dragging = False
        self._guide.hide()
        self._apply()

    def _ratio(self, pos: QPointF) -> float:
        if self.mode == "vertical":
            total = max(1, self.height())
            return max(0.1, min(0.9, (pos.y() + self.handle.y()) / total))
        total = max(1, self.width())
        # pos 是手柄内部坐标；换算到容器坐标
        base = self.handle.x() if self.mode == "horizontal" or self.mode == "swipe" else 0
        return max(0.1, min(0.9, (pos.x() + base) / total))

    # ------------------------------------------------------------- 视图同步
    def _sync_from_left(self) -> None:
        if not self.active or self._blocked:
            return
        self._blocked = True
        try:
            self.right.set_view(self.left.scale, self.left.tx, self.left.ty)
        finally:
            self._blocked = False

    def _sync_from_right(self) -> None:
        if not self.active or self._blocked:
            return
        self._blocked = True
        try:
            self.left.set_view(self.right.scale, self.right.tx, self.right.ty)
        finally:
            self._blocked = False

    # ------------------------------------------------------------- 闪烁
    def _flicker_tick(self) -> None:
        if not self.active or self.mode != "flicker":
            return
        self._flicker_on = not self._flicker_on
        self.left.setVisible(self._flicker_on)
        self.right.setVisible(not self._flicker_on)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._apply()
