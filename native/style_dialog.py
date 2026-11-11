"""图层样式弹窗：改矢量符号（面/线/点）或栅格显示参数，并配要素标注字段。

对应 QGIS 的「图层属性 → 符号化 / 标注」两个页签的常用部分，压成一个窗口：
· 左侧是实时预览（复用 `mapview._paint_layer`，所见即所得）；
· 右边按图层几何类型只显示相关项：面看填充/边界，线看描边，点看点；
· 标注只暴露最常改的四项：字段、字号、颜色、白色描边。

预览为什么直接调渲染函数而不是自己画一遍：
两套画法迟早会走样（透明度/线宽/点大小各算一遍），用户看到的效果和地图上
不一致，比没有预览更糟。这里用 0..1 的世界坐标 + 等比缩放喂给同一个
`_paint_layer`，除了标注文字（那部分在 `MapView.paintEvent` 里画）之外
和地图完全同源。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QLinearGradient,
                           QPainter, QPainterPath, QPen)
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from app.layer_style import (RASTER_STRETCH, geom_kind, normalize_vector_style)

from .theme import QSS

_KIND_LABEL = {"polygon": "面图层", "line": "线图层", "point": "点图层", "": "图层"}
STRETCH_LABEL = {"percentile": "累计裁剪（推荐）", "minmax": "最小–最大", "none": "不拉伸"}


def _luminance(color: str) -> float:
    c = QColor(color)
    return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0


class _StylePreview(QWidget):
    """小块画布：始终用当前控件值重画一遍样例要素。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(136)
        self.setObjectName("stylePreview")
        self._style: dict = {}
        self._kind = ""
        self._text = ""
        self._raster = False

    def set_sample(self, style: dict, kind: str, text: str, raster: bool = False) -> None:
        self._style = dict(style or {})
        self._kind = kind
        self._text = text or ""
        self._raster = raster
        self.update()

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor("#e7e3d8")))
        p.setBrush(QColor("#f7f5ee"))
        p.drawRoundedRect(r, 8, 8)
        if self._raster:
            self._paint_raster(p, r)
            p.end()
            return

        pad = 12.0
        side = max(20.0, min(self.width() - 2 * pad, self.height() - 2 * pad))
        ox = (self.width() - side) / 2.0
        oy = (self.height() - side) / 2.0
        show_poly = self._kind in ("", "polygon")
        show_line = self._kind in ("", "line")
        show_point = self._kind in ("", "point")
        lay = {
            "op": "", "color": self._style.get("fill_color") or "#2f6fed",
            "style": self._style, "geom": self._kind,
            "poly_paths": [], "npoly": 0, "poly_bounds": [], "npoly_b": 0,
            "line_paths": [], "nline": 0, "line_bounds": [], "nline_b": 0,
            "points": [], "npt": 0, "point_bounds": [], "npt_b": 0,
            "dots": None,
        }
        if show_poly:
            lay.update({"poly_paths": [_checkbox_path()], "npoly": 1,
                        "poly_bounds": [(0.10, 0.18, 0.66, 0.84)], "npoly_b": 1})
        if show_line:
            lay.update({"line_paths": [_line_path()], "nline": 1,
                        "line_bounds": [(0.06, 0.86, 0.95, 0.97)], "nline_b": 1})
        if show_point:
            cx, cy = (0.82, 0.28) if show_poly else (0.5, 0.5)
            lay.update({"points": [(cx, cy)], "npt": 1,
                        "point_bounds": [(cx - 0.02, cy - 0.02, cx + 0.02, cy + 0.02)],
                        "npt_b": 1})
        p.save()
        try:
            p.translate(ox, oy)
            p.scale(side, side)
            from .mapview import _paint_layer

            _paint_layer(p, lay, (-0.05, -0.05, 1.05, 1.05), side, 0.0)
        finally:
            p.restore()
        # 标注样例画在面中心附近；点/线图层就画在中间偏上
        lx = ox + (0.38 if show_poly else 0.5) * side
        ly = oy + (0.55 if show_poly else 0.62) * side
        self._paint_sample_label(p, lx, ly)
        p.end()

    def _paint_sample_label(self, p: QPainter, cx: float, cy: float) -> None:
        if not self._text or not self._style.get("label_field"):
            return
        size = int(self._style.get("label_size") or 11)
        font = QFont("Microsoft YaHei")
        font.setPixelSize(max(8, size))
        fm = QFontMetricsF(font)
        w = fm.horizontalAdvance(self._text)
        bx = cx - w / 2
        by = cy + fm.ascent() / 2 - 1
        p.save()
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setFont(font)
        if self._style.get("label_halo", True):
            off = max(1.0, size / 12.0)
            p.setPen(QColor(255, 255, 255, 235))
            for dx, dy in ((-off, 0.0), (off, 0.0), (0.0, -off), (0.0, off)):
                p.drawText(QPointF(bx + dx, by + dy), self._text)
        p.setPen(QColor(self._style.get("label_color") or "#2b2a26"))
        p.drawText(QPointF(bx, by), self._text)
        p.restore()

    def _paint_raster(self, p: QPainter, r: QRectF) -> None:
        opacity = float(self._style.get("opacity", 1.0) or 1.0)
        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0.0, QColor("#6b8e6a"))
        grad.setColorAt(0.5, QColor("#d9c58a"))
        grad.setColorAt(1.0, QColor("#7f9fb8"))
        p.save()
        p.setOpacity(max(0.05, min(1.0, opacity)))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(r.adjusted(18, 18, -18, -18), 6, 6)
        p.restore()
        p.setPen(QColor("#8d897e"))
        p.setFont(QFont("Microsoft YaHei UI", 9))
        p.drawText(r.adjusted(10, 8, -10, -8), Qt.AlignLeft | Qt.AlignTop,
                   f"预览 · 不透明度 {int(round(opacity * 100))}%")


def _checkbox_path() -> QPainterPath:
    """预览里的面样例：一个圆角「田块」。"""
    path = QPainterPath()
    path.moveTo(0.14, 0.30)
    path.lineTo(0.40, 0.18)
    path.lineTo(0.64, 0.38)
    path.lineTo(0.58, 0.78)
    path.lineTo(0.24, 0.82)
    path.closeSubpath()
    return path


def _line_path() -> QPainterPath:
    path = QPainterPath()
    path.moveTo(0.06, 0.93)
    path.cubicTo(0.32, 0.84, 0.52, 0.97, 0.95, 0.89)
    return path


def _sub(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setObjectName("muted")
    lb.setWordWrap(True)
    return lb


class LayerStyleDialog(QDialog):
    """改一个图层的渲染样式。`style()` 返回可直接写回图层的字典。"""

    def __init__(self, layer, parent=None):
        super().__init__(parent)
        self.layer = layer
        self._raster = bool(getattr(layer, "is_raster", False))
        self._geom = ""
        try:
            self._geom = str((layer.meta() or {}).get("geomType") or "")
        except Exception:  # noqa: BLE001
            self._geom = ""
        self._kind = geom_kind(self._geom)
        self._op = str(getattr(layer, "op", "") or "")
        self._base_color = str(getattr(layer, "color", "#2f6fed") or "#2f6fed")
        self._colors: dict[str, str] = {}
        self._w: dict[str, QWidget] = {}
        self._sample_cache: dict[str, str] = {}
        self._fields = self._layer_fields()

        self.setWindowTitle(f"图层样式 · {getattr(layer, 'name', '图层')}")
        self.setStyleSheet(QSS)
        self.setMinimumSize(480, 560)
        self.resize(520, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QLabel(str(getattr(layer, "name", "图层")))
        head.setStyleSheet("font-size:14px;font-weight:600;")
        root.addWidget(head)
        sub = _KIND_LABEL.get(self._kind, "图层")
        if not self._raster and self._geom:
            sub += f" · {self._geom}"
        root.addWidget(_sub(sub))

        # 内容放进滚动区：字段多的时候窗口不至于被撑出屏幕
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("styleBody")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(10)
        self.preview = _StylePreview(body)
        bl.addWidget(self.preview)
        if self._raster:
            self._build_raster(bl)
        else:
            self._build_vector(bl)
        bl.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Save).setText("保存")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)
        self._refresh_preview()

    # ------------------------------------------------------------------ 构建
    def _layer_fields(self) -> list[str]:
        """属性字段名。优先用图层缓存的 meta，避免为开弹窗去全量读属性表。"""
        try:
            meta = self.layer.meta() or {}
            names = [str(f) for f in (meta.get("fields") or []) if str(f)]
            if names:
                return names
        except Exception:  # noqa: BLE001
            pass
        try:
            df = self.layer.attrs_frame()
            return [str(c) for c in df.columns]
        except Exception:  # noqa: BLE001
            return []

    def _build_vector(self, root: QVBoxLayout) -> None:
        st = normalize_vector_style(getattr(self.layer, "style", None), self._op,
                                    self._base_color, self._geom)
        show_fill = self._kind in ("", "polygon")
        show_stroke = self._kind in ("", "polygon", "line")
        show_point = self._kind in ("", "point")
        self._colors.update({
            "fill_color": st["fill_color"],
            "stroke_color": st["stroke_color"],
            "point_color": st["point_color"],
            "label_color": st["label_color"],
        })

        if show_fill:
            box = QGroupBox("面填充")
            form = QFormLayout(box)
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.addRow("填充颜色", self._color_button("fill_color"))
            form.addRow("填充不透明度", self._opacity_row("fill_opacity",
                                                       st["fill_opacity"]))
            root.addWidget(box)

        if show_stroke:
            box = QGroupBox("线 / 边界")
            form = QFormLayout(box)
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.addRow("描边颜色", self._color_button("stroke_color"))
            form.addRow("描边不透明度", self._opacity_row("stroke_opacity",
                                                      st["stroke_opacity"]))
            width = QDoubleSpinBox()
            width.setRange(0.5, 10.0)
            width.setSingleStep(0.5)
            width.setDecimals(1)
            width.setSuffix(" px")
            width.setValue(float(st["stroke_width"]))
            width.valueChanged.connect(self._refresh_preview)
            self._w["stroke_width"] = width
            form.addRow("线宽", width)
            root.addWidget(box)

        if show_point:
            box = QGroupBox("点符号")
            form = QFormLayout(box)
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.addRow("点颜色", self._color_button("point_color"))
            size = QDoubleSpinBox()
            size.setRange(2.0, 40.0)
            size.setSingleStep(0.5)
            size.setDecimals(1)
            size.setSuffix(" px")
            size.setValue(float(st["point_size"]))
            size.valueChanged.connect(self._refresh_preview)
            self._w["point_size"] = size
            form.addRow("点大小", size)
            root.addWidget(box)

        box = QGroupBox("要素标注")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        combo = QComboBox()
        combo.addItem("（不标注）", "")
        for name in self._fields:
            combo.addItem(name, name)
        idx = combo.findData(st["label_field"])
        combo.setCurrentIndex(max(0, idx))
        combo.currentIndexChanged.connect(self._refresh_preview)
        self._w["label_field"] = combo
        form.addRow("标注字段", combo)

        lsize = QSpinBox()
        lsize.setRange(8, 24)
        lsize.setSuffix(" px")
        lsize.setValue(int(st["label_size"]))
        lsize.valueChanged.connect(self._refresh_preview)
        self._w["label_size"] = lsize
        form.addRow("字号", lsize)
        form.addRow("文字颜色", self._color_button("label_color"))
        halo = QCheckBox("白色描边（在深色底图上更清楚）")
        halo.setChecked(bool(st["label_halo"]))
        halo.toggled.connect(self._refresh_preview)
        self._w["label_halo"] = halo
        form.addRow("", halo)
        root.addWidget(box)
        root.addWidget(_sub("标注按当前视野取点，屏幕上自动避让；线要素沿道路方向排字，"
                            "同名道路挨得太近不会重复堆叠。超过 40 字的文本会截断。"))

    def _build_raster(self, root: QVBoxLayout) -> None:
        st = dict(getattr(self.layer, "style", None) or {})
        box = QGroupBox("栅格显示")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        pct = int(round(float(st.get("opacity", 1.0) or 1.0) * 100))
        form.addRow("不透明度", self._opacity_row("opacity", pct))
        combo = QComboBox()
        for key in RASTER_STRETCH:
            combo.addItem(STRETCH_LABEL.get(key, key), key)
        idx = combo.findData(str(st.get("stretch") or "percentile"))
        combo.setCurrentIndex(max(0, idx))
        combo.currentIndexChanged.connect(self._refresh_preview)
        self._w["stretch"] = combo
        form.addRow("拉伸方式", combo)
        gamma = QDoubleSpinBox()
        gamma.setRange(0.2, 3.0)
        gamma.setSingleStep(0.1)
        gamma.setDecimals(2)
        gamma.setValue(float(st.get("gamma") or 1.0))
        gamma.valueChanged.connect(self._refresh_preview)
        self._w["gamma"] = gamma
        form.addRow("Gamma", gamma)
        root.addWidget(box)
        root.addWidget(_sub("改完立刻重绘。波段组合与重采样沿用该图层的当前配置。"))

    def _color_button(self, key: str) -> QWidget:
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        btn = QPushButton()
        btn.setObjectName("swatch")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(84, 26)
        btn.clicked.connect(lambda _=False, k=key: self._pick_color(k))
        hexlb = QLabel(self._colors.get(key, "#2f6fed").upper())
        hexlb.setObjectName("muted")
        hl.addWidget(btn)
        hl.addWidget(hexlb)
        hl.addStretch(1)
        self._w[f"btn_{key}"] = btn
        self._w[f"hex_{key}"] = hexlb
        self._paint_swatch(key)
        return row

    def _pick_color(self, key: str) -> None:
        cur = QColor(self._colors.get(key) or "#2f6fed")
        color = QColorDialog.getColor(cur, self, "选择颜色")
        if not color.isValid():
            return
        self._colors[key] = color.name().lower()
        self._paint_swatch(key)
        self._refresh_preview()

    def _paint_swatch(self, key: str) -> None:
        btn = self._w.get(f"btn_{key}")
        lb = self._w.get(f"hex_{key}")
        if not isinstance(btn, QPushButton):
            return
        color = self._colors.get(key) or "#2f6fed"
        fg = "#1f1e1d" if _luminance(color) > 0.6 else "#ffffff"
        btn.setText(color.upper())
        btn.setStyleSheet(
            f"QPushButton#swatch{{background:{color};color:{fg};"
            "border:1px solid #d8d3c4;border-radius:6px;font-size:10px;"
            "font-family:Consolas,monospace;}")
        if isinstance(lb, QLabel):
            lb.setText(color.upper())

    def _opacity_row(self, key: str, value: int) -> QWidget:
        row = QWidget()
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        sl = QSlider(Qt.Horizontal)
        sl.setObjectName("styleSlider")
        sl.setRange(0, 100)
        sl.setValue(int(value))
        lab = QLabel(f"{int(value)}%")
        lab.setObjectName("muted")
        lab.setFixedWidth(38)
        sl.valueChanged.connect(lambda v, l=lab: l.setText(f"{int(v)}%"))
        sl.valueChanged.connect(self._refresh_preview)
        hl.addWidget(sl, 1)
        hl.addWidget(lab)
        self._w[key] = sl
        return row

    # ------------------------------------------------------------------ 取值
    def style(self) -> dict:
        if self._raster:
            return {
                "opacity": round(self._w["opacity"].value() / 100.0, 3),
                "stretch": str(self._w["stretch"].currentData() or "percentile"),
                "gamma": round(float(self._w["gamma"].value()), 2),
            }
        raw = dict(self._colors)
        raw["fill_opacity"] = int(self._w["fill_opacity"].value()) \
            if "fill_opacity" in self._w else 14
        raw["stroke_opacity"] = int(self._w["stroke_opacity"].value()) \
            if "stroke_opacity" in self._w else 70
        if "stroke_width" in self._w:
            raw["stroke_width"] = float(self._w["stroke_width"].value())
        if "point_size" in self._w:
            raw["point_size"] = float(self._w["point_size"].value())
        if "label_field" in self._w:
            raw["label_field"] = str(self._w["label_field"].currentData() or "")
        if "label_size" in self._w:
            raw["label_size"] = int(self._w["label_size"].value())
        if "label_halo" in self._w:
            raw["label_halo"] = bool(self._w["label_halo"].isChecked())
        return normalize_vector_style(raw, self._op, self._base_color, self._geom)

    def sample_text(self) -> str:
        """预览用的第一个非空字段值（按字段缓存，拖动滑块时不重复扫属性表）。

        大图层（provider）不为了预览去全量读属性表：只有内存图层或已经载入过
        属性表时才取真实值，否则退回「示例标注」。
        """
        if "label_field" not in self._w:
            return ""
        field = str(self._w["label_field"].currentData() or "")
        if not field:
            return ""
        if field in self._sample_cache:
            return self._sample_cache[field]
        text = "示例标注"
        try:
            from .labels import clean_text

            gdf = getattr(self.layer, "_gdf", None)
            prov = getattr(self.layer, "provider", None)
            attrs = None
            if gdf is not None and field in gdf.columns:
                attrs = gdf[field]
            elif prov is not None and getattr(prov, "_attrs_df", None) is not None \
                    and field in prov._attrs_df.columns:
                attrs = prov._attrs_df[field]
            if attrs is not None:
                for value in attrs.head(200).tolist():
                    got = clean_text(value)
                    if got:
                        text = got
                        break
        except Exception:  # noqa: BLE001
            pass
        self._sample_cache[field] = text
        return text

    def _refresh_preview(self, *_args) -> None:
        if self._raster:
            self.preview.set_sample(self.style(), self._kind, "", raster=True)
        else:
            self.preview.set_sample(self.style(), self._kind, self.sample_text())
