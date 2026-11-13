"""小控件：目前只有按宽度省略的单行标签。

本来 `ElideLabel` 写在 `native/app.py` 里，数据库页签也要用同一套省略规则
（`user@host:port/db` 一长就被硬裁，看不出是哪台库），而 `dbpanel` 会被
`app` 导入、不能反向依赖。所以把它挪到这个中立模块，两边都从这里取。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy


class ElideLabel(QLabel):
    """单行、按宽度自动省略的标签。

    图层名一长（`gis_osm_landuse_a_free_1_修复`）就会被行宽硬裁成
    `gis_osm_landuse_a_free_1`，和原图层**看起来一模一样**，用户根本分不清哪行是派生层。
    这里用省略号代替硬裁，并且从中间省（文件名/图层名的区分度常在尾部后缀）。
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(24)
        self.setToolTip(text)
        self._apply()

    def setText(self, text: str) -> None:      # noqa: N802 (Qt 命名)
        self._full = text
        self.setToolTip(text)
        self._apply()

    def fullText(self) -> str:
        return self._full

    def resizeEvent(self, e) -> None:          # noqa: N802
        super().resizeEvent(e)
        self._apply()

    def _apply(self) -> None:
        fm = self.fontMetrics()
        avail = max(24, self.width() - 2)
        super().setText(fm.elidedText(self._full, Qt.ElideMiddle, avail))
