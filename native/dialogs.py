"""对话框：属性表、模型设置、多图层选择、关于。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
                               QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from .format import fmt_num


class TableDialog(QDialog):
    """属性表：分页 + 排序 + 筛选，点一行地图跟着高亮定位。

    和地图的联动靠 `on_pick(位置下标)` 回调 —— 传的是**行在 gdf 里的位置**，
    不是索引标签，因为框选/高亮用的都是位置。
    """

    PAGE = 200

    def __init__(self, layer, parent=None, on_pick=None):
        super().__init__(parent)
        self.layer = layer
        self.on_pick = on_pick
        self.setWindowTitle(f"属性表 · {layer.name}")
        self.resize(900, 580)

        g = layer.gdf
        self._cols = [str(c) for c in g.columns if c != g.geometry.name]
        # reset_index 让「标签」等于「位置」，后面 loc/iloc 和地图下标才能对得上
        self._data = g.drop(columns=[g.geometry.name]).reset_index(drop=True)
        self._order = list(range(len(self._data)))     # 当前显示顺序（位置下标）
        self._sort = (None, False)                     # (列名, 是否降序)
        self._page = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(layer.name)
        title.setStyleSheet("font-size:14px;font-weight:600;")
        head.addWidget(title)
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        head.addWidget(self.meta)
        head.addStretch(1)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("筛选：任意列包含…（例如 商业）")
        self.filter.setClearButtonEnabled(True)
        self.filter.setFixedWidth(220)
        self.filter.textChanged.connect(lambda _t: self._refilter())
        head.addWidget(self.filter)
        root.addLayout(head)

        self.tw = QTableWidget(0, len(self._cols))
        self.tw.setHorizontalHeaderLabels(self._cols)
        self.tw.verticalHeader().setVisible(True)
        self.tw.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tw.setSelectionBehavior(QTableWidget.SelectRows)
        self.tw.setSelectionMode(QTableWidget.SingleSelection)
        self.tw.setAlternatingRowColors(False)
        self.tw.setShowGrid(False)
        self.tw.horizontalHeader().sectionClicked.connect(self._on_header)
        self.tw.cellClicked.connect(self._on_cell)
        root.addWidget(self.tw, 1)

        foot = QHBoxLayout()
        self.prev = QPushButton("‹ 上一页")
        self.prev.setObjectName("chip")
        self.prev.clicked.connect(lambda _c=False: self._turn(-1))
        self.next = QPushButton("下一页 ›")
        self.next.setObjectName("chip")
        self.next.clicked.connect(lambda _c=False: self._turn(1))
        self.pageinfo = QLabel("")
        self.pageinfo.setObjectName("tiny")
        foot.addWidget(self.prev)
        foot.addWidget(self.pageinfo)
        foot.addWidget(self.next)
        foot.addStretch(1)
        self.hint = QLabel("点一行 → 地图高亮并定位")
        self.hint.setObjectName("tiny")
        foot.addWidget(self.hint)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        foot.addWidget(close)
        root.addLayout(foot)

        self._refilter()

    # ------------------------------------------------------------------ 交互
    def _refilter(self) -> None:
        q = self.filter.text().strip()
        if q:
            mask = None
            for col in self._cols:
                hit = self._data[col].astype(str).str.contains(q, case=False, na=False, regex=False)
                mask = hit if mask is None else (mask | hit)
            order = list(mask[mask].index)
        else:
            order = list(self._data.index)
        self._order = order
        self._page = 0
        self._render()

    def _on_header(self, col: int) -> None:
        name = self._cols[col]
        desc = self._sort[0] == name and not self._sort[1]
        self._sort = (name, desc)
        # 数值列按数值排，字符串列按字符串排（pandas 自己会判断类型）
        try:
            idx = self._data.loc[self._order].sort_values(
                name, ascending=not desc, kind="stable", na_position="last").index
        except Exception:
            idx = self._data.loc[self._order].sort_index()
        self._order = list(idx)
        self._page = 0
        self._render()

    def _turn(self, d: int) -> None:
        pages = max(1, (len(self._order) + self.PAGE - 1) // self.PAGE)
        self._page = max(0, min(pages - 1, self._page + d))
        self._render()

    def _on_cell(self, row: int, _col: int) -> None:
        if self.on_pick is None or row >= len(self.tw_pos):
            return
        self.on_pick(self.tw_pos[row])

    # ------------------------------------------------------------------ 渲染
    def _render(self) -> None:
        pages = max(1, (len(self._order) + self.PAGE - 1) // self.PAGE)
        self._page = min(self._page, pages - 1)
        chunk = self._order[self._page * self.PAGE:(self._page + 1) * self.PAGE]
        self.tw_pos = chunk                            # 表格行 → gdf 位置
        df = self._data.iloc[chunk] if chunk else self._data.iloc[[]]

        self.tw.setRowCount(len(chunk))
        for r, vals in enumerate(df.itertuples(index=False)):
            for c, val in enumerate(vals):
                if val is None or (isinstance(val, float) and val != val):
                    txt = ""
                elif isinstance(val, float):
                    txt = f"{val:g}" if abs(val) < 1e6 else f"{val:.6g}"
                else:
                    txt = str(val)
                item = QTableWidgetItem(txt)
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                self.tw.setItem(r, c, item)
        if self._page == 0:
            self.tw.resizeColumnsToContents()

        total = len(self._data)
        shown = len(self._order)
        sel = f"（筛选出 {fmt_num(shown)} 条）" if shown != total else ""
        self.meta.setText(f"{fmt_num(total)} 条{sel} · {self.layer.meta()['crs']} · "
                          f"{len(self._cols)} 个字段")
        self.pageinfo.setText(f"第 {self._page + 1}/{pages} 页"
                              + (f" · 本页 {len(chunk)} 行" if chunk else ""))
        self.prev.setEnabled(self._page > 0)
        self.next.setEnabled(self._page < pages - 1)


class SettingsDialog(QDialog):
    """大模型设置：任意 OpenAI 兼容接口。"""

    PRESETS = {
        "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
        "通义千问": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
        "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
        "本地 Ollama": ("http://localhost:11434/v1", "qwen2.5:7b"),
    }

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("模型设置")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        tip = QLabel("规则引擎已能理解常见中文指令，离线即可用。接入任意 <b>OpenAI 兼容</b>"
                     "接口后，可用更自由的口语表达；接口异常会自动回退规则引擎。")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        root.addWidget(tip)

        root.addWidget(self._lab("接口地址 Base URL"))
        self.base = QLineEdit(cfg.get("base_url", ""))
        self.base.setPlaceholderText("https://api.deepseek.com/v1")
        root.addWidget(self.base)

        row = QHBoxLayout()
        row.setSpacing(6)
        for name, (url, model) in self.PRESETS.items():
            b = QPushButton(name)
            b.setObjectName("chip")
            b.clicked.connect(lambda _=False, u=url, m=model: self._preset(u, m))
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)

        root.addWidget(self._lab("模型名称"))
        self.model = QLineEdit(cfg.get("model", ""))
        self.model.setPlaceholderText("deepseek-chat")
        root.addWidget(self.model)

        root.addWidget(self._lab("API Key"))
        self.key = QLineEdit(cfg.get("api_key", ""))
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("sk-...")
        root.addWidget(self.key)

        self.enabled = QCheckBox("启用大模型规划（失败自动回退规则引擎）")
        self.enabled.setChecked(bool(cfg.get("enabled")))
        root.addWidget(self.enabled)

        note = QLabel("Key 只保存在本机 data/settings.json，不会外传。")
        note.setObjectName("tiny")
        root.addWidget(note)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("保存")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    @staticmethod
    def _lab(text: str) -> QLabel:
        lb = QLabel(text)
        lb.setStyleSheet("font-size:11px;color:#43413c;font-weight:600;")
        return lb

    def _preset(self, url: str, model: str) -> None:
        self.base.setText(url)
        self.model.setText(model)
        self.enabled.setChecked(True)
        if not self.key.text() and "11434" in url:
            self.key.setText("ollama")

    def value(self) -> dict:
        return {
            "base_url": self.base.text().strip(),
            "model": self.model.text().strip(),
            "api_key": self.key.text().strip(),
            "enabled": self.enabled.isChecked(),
        }


class ImportDialog(QDialog):
    """压缩包里有多个矢量文件时，让用户挑要载入哪些。"""

    def __init__(self, candidates: list[dict], source: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择要载入的图层")
        self.resize(520, 520)
        self._boxes: list[tuple[QCheckBox, int]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(9)

        tip = QLabel(f"压缩包里有 {len(candidates)} 个矢量图层，勾选需要的（可多选）。")
        tip.setObjectName("muted")
        root.addWidget(tip)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        host = QWidget()
        vl = QVBoxLayout(host)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(5)

        for i, c in enumerate(candidates):
            frame = QFrame()
            # 用 id 选择器限定范围：写成 "QFrame{…}" 会连子 QFrame 一起套上
            frame.setObjectName("candCard")
            frame.setStyleSheet("QFrame#candCard{background:#faf9f5;border:1px solid #e7e3d8;"
                                "border-radius:9px;}")
            hl = QHBoxLayout(frame)
            hl.setContentsMargins(9, 7, 9, 7)
            hl.setSpacing(8)
            cb = QCheckBox()
            cb.setChecked(i == 0)          # 默认只勾最大那个，避免一下子几十万要素
            hl.addWidget(cb)
            self._boxes.append((cb, i))

            info = QVBoxLayout()
            info.setSpacing(1)
            name = QLabel(f"{c['name']}{c.get('ext', '')}")
            name.setStyleSheet("font-size:12px;font-weight:600;")
            detail = QLabel(f"{fmt_num(c.get('features'))} 要素 · {c.get('geom') or '未知类型'}"
                            f" · {c.get('crs') or '无坐标系'}")
            detail.setObjectName("muted")
            info.addWidget(name)
            info.addWidget(detail)
            hl.addLayout(info, 1)

            size = QLabel(f"{c.get('size_mb', 0)} MB")
            size.setObjectName("muted")
            hl.addWidget(size)
            vl.addWidget(frame)

        vl.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        note = QLabel("要素量大的图层载入、绘制会慢一些，建议先载入主要图层。")
        note.setObjectName("tiny")
        root.addWidget(note)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("载入所选")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def picks(self) -> list[int]:
        return [i for cb, i in self._boxes if cb.isChecked()]


class OperatorDialog(QDialog):
    """预览「按文字描述生成的算子」：看得到代码、试跑结果，再决定要不要入库。"""

    def __init__(self, spec: dict, parent=None):
        super().__init__(parent)
        self.spec = dict(spec or {})
        title = self.spec.get("title") or self.spec.get("name") or "新算子"
        self.setWindowTitle(f"新算子预览 · {title}")
        self.resize(720, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(8)

        head = QLabel(f"<b>{title}</b>　<span style='color:#6b7c93'>{self.spec.get('name', '')}</span>")
        head.setTextFormat(Qt.RichText)
        head.setStyleSheet("font-size:13px;")
        root.addWidget(head)

        desc = QLabel(self.spec.get("desc") or "（没有说明）")
        desc.setWordWrap(True)
        desc.setObjectName("muted")
        root.addWidget(desc)

        params = self.spec.get("params") or {}
        if params:
            pl = QLabel("参数：" + "，".join(f"{k}（{v}）" for k, v in params.items()))
            pl.setWordWrap(True)
            pl.setObjectName("tiny")
            root.addWidget(pl)

        trial = self.spec.get("_trial")
        if trial:
            ok = bool(trial.get("ok"))
            color = "#0ca678" if ok else "#d92d20"
            txt = (f"试跑通过（200 个要素 → {trial.get('rows', 0)}）"
                   if ok else f"试跑失败：{trial.get('msg', '')}")
            if ok and trial.get("msg"):
                txt += f"　{trial['msg'][:60]}"
            tl = QLabel(txt)
            tl.setWordWrap(True)
            tl.setStyleSheet(f"color:{color};font-size:11px;")
            root.addWidget(tl)

        lab = QLabel("算子代码（可编辑）")
        lab.setStyleSheet("font-size:11px;color:#43413c;font-weight:600;")
        root.addWidget(lab)

        self.editor = QPlainTextEdit(self.spec.get("code") or "")
        self.editor.setObjectName("codeEditor")
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        self.editor.setFont(font)
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setStyleSheet(
            "QPlainTextEdit{background:#0f1b2d;color:#d9e2f1;border-radius:8px;"
            "border:1px solid #24344d;padding:8px;}")
        root.addWidget(self.editor, 1)

        note = QLabel("代码会在沙箱里运行：禁止 import / while / 文件与网络访问。"
                      "保存后它就和内置算子一样，能在对话里被规划调用。")
        note.setWordWrap(True)
        note.setObjectName("tiny")
        root.addWidget(note)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("保存并加入算子库")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def value(self) -> dict:
        spec = dict(self.spec)
        spec["code"] = self.editor.toPlainText()
        return spec
