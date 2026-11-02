"""对话框：属性表、模型设置、多图层选择、关于。"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from app import api_formats
from app import conn_test
from app.gdb import gdb_layer_infos
from app.io_utils import VECTOR_EXTS, extract_zip, file_info
from app.raster import RASTER_EXTS, read_info
from app.tool_registry import CUSTOM_GROUP

from .format import fmt_num
from .theme import QSS


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
    """大模型设置：接口地址 / 密钥 / 模型 / **API 格式**。

    API 格式决定请求怎么拼、响应怎么取（见 `app/api_formats.py`）：
    OpenAI Chat Completions、OpenAI Responses、Anthropic Messages、
    Gemini generateContent、Ollama 原生各有各的 URL、请求头和响应结构。
    选好格式后还能手动覆盖端点路径和请求头，用来对接自建网关。

    「测试连接」在后台线程里发一个最小请求，验证地址 / Key / 模型 / 格式对不对；
    失败时按状态码给出**具体是什么问题、怎么改**，而不是甩一段原始 JSON。
    """

    #: 后台线程的结果回到主线程
    _test_done = Signal(object)

    PRESETS = {
        "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat", "openai_chat"),
        "通义千问": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
                     "openai_chat"),
        "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini", "openai_chat"),
        "本地 Ollama": ("http://localhost:11434/v1", "qwen2.5:7b", "ollama"),
        "Claude": ("https://api.anthropic.com/v1", "claude-sonnet-4-20250514", "anthropic"),
        "Gemini": ("https://generativelanguage.googleapis.com/v1beta", "gemini-2.0-flash",
                   "gemini"),
    }

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("模型设置")
        self.setMinimumWidth(520)
        #: 正在跑测试的线程 / 取消标志
        self._test_thread: threading.Thread | None = None
        self._test_cancel: threading.Event | None = None
        # 后台线程只往 queue 放结果，绝不从非 Qt 线程 emit 信号。
        # 由主线程 QTimer 轮询，避免 Qt 在退出阶段清理 adopted thread 时崩溃。
        self._test_queue: queue.Queue = queue.Queue()
        self._test_timer = QTimer(self)
        self._test_timer.setInterval(15)
        self._test_timer.timeout.connect(self._poll_test)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(9)

        tip = QLabel("规则引擎已能理解常见中文指令，离线即可用。接入任意大模型接口后，"
                     "可用更自由的口语表达；接口异常会自动回退规则引擎。")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        root.addWidget(tip)

        # ---------------------------------------------------------- 接口格式
        root.addWidget(self._lab("接口格式（决定请求怎么发、响应怎么读）"))
        self.fmt = QComboBox()
        for label in api_formats.FORMATS:
            self.fmt.addItem(label, api_formats.FORMATS[label])
        cur = (cfg.get("api_format") or api_formats.DEFAULT_FORMAT).strip()
        idx = self.fmt.findData(cur)
        self.fmt.setCurrentIndex(idx if idx >= 0 else 0)
        root.addWidget(self.fmt)

        self.fmt_desc = QLabel("")
        self.fmt_desc.setWordWrap(True)
        self.fmt_desc.setObjectName("tiny")
        root.addWidget(self.fmt_desc)

        # ---------------------------------------------------------- Base URL
        root.addWidget(self._lab("接口地址 Base URL"))
        self.base = QLineEdit(cfg.get("base_url", ""))
        self.base.setPlaceholderText("https://api.deepseek.com/v1")
        root.addWidget(self.base)

        row = QHBoxLayout()
        row.setSpacing(6)
        for name, (url, model, fmt) in self.PRESETS.items():
            b = QPushButton(name)
            b.setObjectName("chip")
            b.clicked.connect(lambda _=False, u=url, m=model, f=fmt: self._preset(u, m, f))
            row.addWidget(b)
        row.addStretch(1)
        root.addLayout(row)

        # ---------------------------------------------------------- 模型 / Key
        root.addWidget(self._lab("模型名称"))
        self.model = QLineEdit(cfg.get("model", ""))
        self.model.setPlaceholderText("deepseek-chat")
        root.addWidget(self.model)

        root.addWidget(self._lab("API Key"))
        self.key = QLineEdit(cfg.get("api_key", ""))
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("sk-...")
        root.addWidget(self.key)

        # ---------------------------------------------------------- 自定义覆盖
        self.adv = QCheckBox("高级：自定义端点路径与请求头（对接自建网关）")
        ov = cfg.get("api_overrides") or {}
        self.adv.setChecked(bool(ov))
        root.addWidget(self.adv)

        self.adv_box = QWidget()
        av = QVBoxLayout(self.adv_box)
        av.setContentsMargins(10, 0, 0, 0)
        av.setSpacing(6)

        av.addWidget(self._lab("端点路径（留空用默认；可用 {model} 占位）"))
        self.ov_path = QLineEdit(str(ov.get("path") or ""))
        self.ov_path.setPlaceholderText("/chat/completions")
        av.addWidget(self.ov_path)

        av.addWidget(self._lab("附加请求头（每行一条：名字: 值）"))
        self.ov_headers = QPlainTextEdit()
        self.ov_headers.setPlaceholderText("X-Api-Key: xxx")
        self.ov_headers.setMaximumHeight(64)
        for k, v in (ov.get("headers") or {}).items():
            self.ov_headers.appendPlainText(f"{k}: {v}")
        av.addWidget(self.ov_headers)
        self.adv_box.setVisible(bool(ov))
        root.addWidget(self.adv_box)
        #: 高级区是否展开（对话框没 show 时 isVisible 恒为 False，
        #: 测试和外部都要读这个状态，所以单独留一个标记）
        self._adv_open = bool(ov)

        # ---------------------------------------------------------- 预览 / 测试
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setObjectName("tiny")
        root.addWidget(self.preview)

        brow = QHBoxLayout()
        brow.setSpacing(6)
        self.btn_test = QPushButton("测试连接")
        self.btn_test.setObjectName("chip")
        self.btn_test.setToolTip("发一个最小请求，验证地址 / Key / 模型 / 格式对不对")
        self.btn_test.clicked.connect(self._test)
        brow.addWidget(self.btn_test)
        self.btn_test_cancel = QPushButton("取消")
        self.btn_test_cancel.setObjectName("chip")
        self.btn_test_cancel.setToolTip("停止正在进行的测试")
        self.btn_test_cancel.clicked.connect(self._cancel_test)
        self.btn_test_cancel.hide()
        brow.addWidget(self.btn_test_cancel)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setObjectName("tiny")
        brow.addWidget(self.test_result, 1)
        root.addLayout(brow)

        # 测试结果的详细说明（建议怎么改 / 模型回了什么）
        self.test_detail_label = QLabel("")
        self.test_detail_label.setWordWrap(True)
        self.test_detail_label.setObjectName("tiny")
        self.test_detail_label.hide()
        root.addWidget(self.test_detail_label)
        #: 上次测试的详细说明（外部可读）
        self.test_detail = ""

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

        self.fmt.currentIndexChanged.connect(self._refresh_preview)
        self.adv.toggled.connect(self._on_adv_toggled)
        self.base.textChanged.connect(self._refresh_preview)
        self.model.textChanged.connect(self._refresh_preview)
        self._refresh_preview()

    @property
    def adv_open(self) -> bool:
        """高级区（自定义端点/请求头）是否展开。"""
        return self._adv_open

    def _on_adv_toggled(self, on: bool) -> None:
        self._adv_open = bool(on)
        self.adv_box.setVisible(bool(on))

    @staticmethod
    def _lab(text: str) -> QLabel:
        lb = QLabel(text)
        lb.setStyleSheet("font-size:11px;color:#43413c;font-weight:600;")
        return lb

    def _preset(self, url: str, model: str, fmt: str) -> None:
        self.base.setText(url)
        self.model.setText(model)
        idx = self.fmt.findData(fmt)
        if idx >= 0:
            self.fmt.setCurrentIndex(idx)
        self.enabled.setChecked(True)
        if not self.key.text() and "11434" in url:
            self.key.setText("ollama")

    def _refresh_preview(self) -> None:
        """显示「当前设置会发成什么样」，省得存完才知道填错了。"""
        fmt = self.fmt.currentData() or api_formats.DEFAULT_FORMAT
        self.fmt_desc.setText(api_formats.describe(fmt))
        try:
            cfg = self.value()
            url, headers, payload = api_formats.build_request(
                cfg, [{"role": "user", "content": "hi"}], False, 100)
            keys = ", ".join(sorted(k for k in headers if k != "Content-Type"))
            self.preview.setText(f"将请求：POST {url}\n请求头：{keys or '（无）'}")
        except Exception as e:  # noqa: BLE001 - 预览失败不该挡住设置
            self.preview.setText(f"（预览失败：{e}）")

    def _test(self) -> None:
        """测试连接。

        请求放到后台线程跑：主线程被 HTTP 阻塞住的话，界面会整块冻住
        （超时 12 秒起，网关不通时更久），窗口拖不动、关不掉。
        """
        if self._test_thread is not None and self._test_thread.is_alive():
            return
        cfg = self.value()
        self._test_cancel = threading.Event()
        while True:
            try:
                self._test_queue.get_nowait()
            except queue.Empty:
                break
        self.btn_test.setEnabled(False)
        self.btn_test_cancel.show()
        self.test_result.setText("正在测试…")

        def work():
            try:
                res = conn_test.test_connection(cfg)
            except Exception as e:  # noqa: BLE001
                res = conn_test.TestResult(False, f"测试出错：{e}")
            # 只写线程安全队列；界面更新由主线程 QTimer 完成。
            self._test_queue.put(res)

        self._test_thread = threading.Thread(target=work, daemon=True,
                                             name="llm-conn-test")
        self._test_thread.start()
        self._test_timer.start()

    def _poll_test(self) -> None:
        """主线程取后台测试结果（不跨线程 emit Qt 信号）。"""
        try:
            res = self._test_queue.get_nowait()
        except queue.Empty:
            if self._test_thread is not None and not self._test_thread.is_alive():
                self._test_timer.stop()
            return
        self._test_timer.stop()
        self._on_test_done(res)

    def _cancel_test(self) -> None:
        """请求取消正在进行的测试。

        urllib 的 urlopen 不能中途打断，但我们可以不让结果更新界面 ——
        用户至少能关掉窗口，不用干等。
        """
        if self._test_cancel is not None:
            self._test_cancel.set()
        self.test_result.setText("已取消测试")
        self._test_finish_ui()

    def _test_finish_ui(self) -> None:
        self.btn_test.setEnabled(True)
        self.btn_test_cancel.hide()

    def _on_test_done(self, res) -> None:
        """后台线程的结果回到主线程。"""
        if self._test_cancel is not None and self._test_cancel.is_set():
            return                      # 用户已取消，别再覆盖提示
        self._test_finish_ui()
        if res.ok:
            self.test_result.setText(f"✓ {res.message}"
                                     + (f"（{res.elapsed:.1f} 秒）" if res.elapsed else ""))
        else:
            self.test_result.setText(f"✗ {res.message}")
        self.test_detail = res.detail

    def closeEvent(self, e):  # noqa: N802
        """关窗口时把后台测试收尾，避免线程在解释器退出阶段改界面。"""
        self._test_timer.stop()
        if self._test_cancel is not None:
            self._test_cancel.set()
        t = self._test_thread
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
        super().closeEvent(e)

    def value(self) -> dict:
        overrides: dict = {}
        path = self.ov_path.text().strip()
        if path:
            overrides["path"] = path
        headers = {}
        for line in self.ov_headers.toPlainText().splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip()
            if k:
                headers[k] = v.strip()
        if headers:
            overrides["headers"] = headers
        return {
            "base_url": self.base.text().strip(),
            "model": self.model.text().strip(),
            "api_key": self.key.text().strip(),
            "enabled": self.enabled.isChecked(),
            "api_format": self.fmt.currentData() or api_formats.DEFAULT_FORMAT,
            "api_overrides": overrides,
        }


def _cand_detail(c: dict) -> str:
    """导入候选的副标题：矢量报要素数，栅格报尺寸和波段。"""
    crs = c.get("crs") or "无坐标系"
    if c.get("kind") == "raster":
        w, h, b = c.get("width") or 0, c.get("height") or 0, c.get("bands") or 0
        if not w or not h:
            return f"栅格 · {crs}"
        return f"{w}×{h} · {b} 波段 · {crs}"
    return f"{fmt_num(c.get('features'))} 要素 · {c.get('geom') or '未知类型'} · {crs}"


class ImportDialog(QDialog):
    """压缩包里有多个文件时，让用户挑要载入哪些（矢量、栅格都可以）。"""

    def __init__(self, candidates: list[dict], source: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择要载入的图层")
        self.resize(520, 520)
        self._boxes: list[tuple[QCheckBox, int]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(9)

        n_vec = sum(1 for c in candidates if c.get("kind") != "raster")
        n_ras = len(candidates) - n_vec
        parts = []
        if n_vec:
            parts.append(f"{n_vec} 个矢量")
        if n_ras:
            parts.append(f"{n_ras} 个栅格")
        tip = QLabel(f"压缩包里有 {len(candidates)} 个图层"
                     f"（{' · '.join(parts)}），勾选需要的（可多选）。")
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
            detail = QLabel(_cand_detail(c))
            detail.setObjectName("muted")
            info.addWidget(name)
            info.addWidget(detail)
            hl.addLayout(info, 1)

            mb = float(c.get("size_mb") or 0)
            # 小文件按 MB 保留 1 位会被舍成 "0.0 MB"，看着像空文件，改用 KB
            if mb < 0.1:
                size = QLabel(f"{max(int(mb * 1024), 1)} KB")
            elif mb < 1:
                size = QLabel(f"{mb:.2f} MB")
            else:
                size = QLabel(f"{mb:.1f} MB")
            size.setObjectName("muted")
            hl.addWidget(size)
            vl.addWidget(frame)

        vl.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        note = QLabel("体积大的图层载入、绘制会慢一些，建议先载入主要图层。")
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


class ImportDataDialog(QDialog):
    """自定义导入矢量数据：先选数据源，再勾选文件内部的图层。

    `.gdb` / `.gpkg` 不是「一个文件 = 一个图层」：一个库里有多个要素类。
    这里把内部图层列成可勾选卡片，导入时只读选中的图层，避免把整个 GDB
    的几十个图层一次性塞进图层面板。
    """

    def __init__(self, parent=None, start_paths: list[str] | None = None):
        super().__init__(parent)
        self.setWindowTitle("导入数据")
        self.resize(640, 560)
        # QDialog 是顶层窗口，不能总指望继承主窗口的 QSS；显式套一遍主题。
        self.setStyleSheet(QSS)
        self._entries: list[dict] = []
        self._boxes: list[tuple[QCheckBox, dict]] = []
        self._seen: set[tuple[str, str]] = set()
        self._tmp_roots: list[Path] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(8)

        pick = QHBoxLayout()
        pick.setSpacing(7)
        btn_file = QPushButton("添加文件…")
        btn_file.setObjectName("ghost")
        btn_file.clicked.connect(self.choose_files)
        pick.addWidget(btn_file)
        btn_gdb = QPushButton("添加 FileGDB 文件夹…")
        btn_gdb.setObjectName("ghost")
        btn_gdb.clicked.connect(self.choose_gdb)
        pick.addWidget(btn_gdb)
        pick.addStretch(1)
        root.addLayout(pick)

        tip = QLabel("支持 shp / GeoJSON / GPKG / KML / GML / CSV / TIF / ZIP；"
                     "GPKG、FileGDB 会自动列出内部图层，可多选。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        host = QWidget()
        self.list_layout = QVBoxLayout(host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(5)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(host)
        root.addWidget(self.scroll, 1)

        self.empty = QLabel("还没有选择数据。点「添加文件…」或「添加 FileGDB 文件夹…」。")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignCenter)
        root.addWidget(self.empty)

        tools = QHBoxLayout()
        btn_all = QPushButton("全选")
        btn_all.setObjectName("tiny")
        btn_all.clicked.connect(lambda: self._set_all(True))
        btn_none = QPushButton("全不选")
        btn_none.setObjectName("tiny")
        btn_none.clicked.connect(lambda: self._set_all(False))
        tools.addWidget(btn_all)
        tools.addWidget(btn_none)
        tools.addStretch(1)
        root.addLayout(tools)

        self.btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok = self.btns.button(QDialogButtonBox.Ok)
        self._ok.setText("导入所选")
        self.btns.button(QDialogButtonBox.Cancel).setText("取消")
        self.btns.accepted.connect(self.accept)
        self.btns.rejected.connect(self.reject)
        root.addWidget(self.btns)

        if start_paths:
            self.add_paths(start_paths)
        self._sync_empty()
        self._update_ok()

    # ---------------------------------------------------------------- 数据源
    @staticmethod
    def _file_filter() -> str:
        vec = " ".join("*" + e for e in VECTOR_EXTS if e != ".gdb")
        ras = " ".join("*" + e for e in RASTER_EXTS)
        return (f"矢量与栅格 ({vec} {ras} *.zip);;"
                f"矢量数据 ({vec} *.zip);;栅格数据 ({ras});;所有文件 (*.*)")

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择矢量/栅格文件", "", self._file_filter())
        if paths:
            self.add_paths(paths)

    def choose_gdb(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择 Esri FileGDB 文件夹（.gdb）", "", QFileDialog.ShowDirsOnly)
        if not d:
            return
        if not str(d).lower().endswith(".gdb"):
            QMessageBox.information(self, "不是 FileGDB",
                                    "请选择以 .gdb 结尾的文件夹（例如 2024年国土.gdb）。")
            return
        self.add_paths([d])

    def add_paths(self, paths: list[str]) -> None:
        errors = []
        for raw in paths:
            p = Path(raw)
            try:
                suffix = p.suffix.lower()
                if p.is_dir():
                    if suffix == ".gdb":
                        self._add_gdb(p)
                    else:
                        errors.append(f"{p.name}：不是 .gdb 文件夹")
                elif suffix == ".gpkg":
                    self._add_gpkg(p)
                elif suffix == ".zip":
                    root, files = extract_zip(p)
                    self._tmp_roots.append(root)
                    for f in files:
                        if f.suffix.lower() != ".zip":
                            self.add_paths([str(f)])
                elif suffix in RASTER_EXTS:
                    self._add_raster(p)
                elif suffix in VECTOR_EXTS:
                    self._add_single(p)
                else:
                    errors.append(f"{p.name}：不支持的格式")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p.name}：{e}")
        if errors:
            QMessageBox.warning(self, "部分数据未加入", "\n".join(errors[:8]))
        self._sync_empty()

    def _add_entry(self, entry: dict) -> None:
        key = (entry["path"], str(entry.get("layer") or ""))
        if key in self._seen:
            return
        self._seen.add(key)
        self._entries.append(entry)

        frame = QFrame()
        frame.setObjectName("candCard")
        frame.setStyleSheet("QFrame#candCard{background:#faf9f5;border:1px solid #e7e3d8;"
                            "border-radius:9px;}")
        hl = QHBoxLayout(frame)
        hl.setContentsMargins(9, 7, 9, 7)
        hl.setSpacing(8)
        cb = QCheckBox()
        cb.setChecked(True)
        cb.toggled.connect(lambda _checked: self._update_ok())
        hl.addWidget(cb)
        self._boxes.append((cb, entry))

        info = QVBoxLayout()
        info.setSpacing(1)
        name = QLabel(entry["name"])
        name.setStyleSheet("font-size:12px;font-weight:600;")
        detail = QLabel(
            f"{fmt_num(entry.get('features') or 0)} 个要素 · "
            f"{entry.get('geom') or '未知类型'} · {entry.get('crs') or '未定义坐标系'}")
        detail.setObjectName("muted")
        info.addWidget(name)
        info.addWidget(detail)
        hl.addLayout(info, 1)
        self.list_layout.insertWidget(self.list_layout.count() - 1, frame)
        self._update_ok()

    def _add_gdb(self, path: Path) -> None:
        infos = gdb_layer_infos(path)
        if not infos:
            raise ValueError("GDB 里没有可导入的矢量图层")
        for info in infos:
            self._add_entry({
                "path": str(path), "layer": info["name"], "kind": "vector",
                "name": f"{path.stem}/{info['name']}",
                "features": info.get("features"), "geom": info.get("geom"),
                "crs": info.get("crs"),
            })

    def _add_gpkg(self, path: Path) -> None:
        infos = []
        try:
            import pyogrio
            for name, geom in pyogrio.list_layers(path):
                name = str(name)
                try:
                    meta = pyogrio.read_info(path, layer=name)
                    infos.append({
                        "name": name,
                        "features": int(meta.get("features") or 0),
                        "geom": str(meta.get("geometry_type") or geom or ""),
                        "crs": str(meta.get("crs") or ""),
                    })
                except Exception:
                    infos.append({"name": name, "features": 0,
                                  "geom": str(geom or ""), "crs": ""})
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"读取 GeoPackage 图层失败：{e}") from e
        if not infos:
            raise ValueError("GeoPackage 里没有可导入的图层")
        for info in infos:
            self._add_entry({
                "path": str(path), "layer": info["name"], "kind": "vector",
                "name": f"{path.stem}/{info['name']}",
                "features": info.get("features"), "geom": info.get("geom"),
                "crs": info.get("crs"),
            })

    def _add_single(self, path: Path) -> None:
        info = file_info(path)
        self._add_entry({
            "path": str(path), "layer": None, "kind": "vector",
            "name": path.stem, "features": info.get("features"),
            "geom": info.get("geom"), "crs": info.get("crs"),
        })

    def _add_raster(self, path: Path) -> None:
        ri = read_info(path)
        self._add_entry({
            "path": str(path), "layer": None, "kind": "raster",
            "name": path.stem, "features": 0,
            "geom": f"{ri.width}×{ri.height} · {ri.bands} 波段",
            "crs": ri.crs_name or "",
        })

    # ---------------------------------------------------------------- 选择
    def _set_all(self, checked: bool) -> None:
        for cb, _entry in self._boxes:
            cb.setChecked(checked)

    def _update_ok(self) -> None:
        self._ok.setEnabled(any(cb.isChecked() for cb, _ in self._boxes))

    def _sync_empty(self) -> None:
        self.empty.setVisible(not self._boxes)

    def selections(self) -> list[dict]:
        return [entry for cb, entry in self._boxes if cb.isChecked()]

    def _cleanup_tmp(self) -> None:
        import shutil
        roots, self._tmp_roots = self._tmp_roots, []
        for root in roots:
            shutil.rmtree(root, ignore_errors=True)

    def take_tmp_roots(self) -> list[Path]:
        roots, self._tmp_roots = self._tmp_roots, []
        return roots

    def done(self, result: int) -> None:  # noqa: N802 (Qt 命名)
        # 取消/关窗时立即清理；接受时把解压目录交给主窗口，等导入完成再删。
        if result != QDialog.Accepted:
            self._cleanup_tmp()
        super().done(result)


class NewToolDialog(QDialog):
    """「＋ 新建工具」的第一步：描述算法 + 选一个分组。

    分组下拉放在这里，是因为这一步是用户唯一能主动表达「这个工具该放哪」的时机；
    之后的代码预览弹窗只是把这里的默认值带过去（用户仍可改）。
    不选就落「我的工具」。
    """

    def __init__(self, parent=None, group_choices: list[str] | None = None,
                 initial_group: str = ""):
        super().__init__(parent)
        self.setWindowTitle("用文字描述一个工具")
        self.resize(560, 360)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(8)

        lab = QLabel("说明要算什么、参数是什么。例如：\n"
                     "统计每个面内道路的总长度和条数，一个新图层输出这些字段。")
        lab.setWordWrap(True)
        lab.setObjectName("muted")
        root.addWidget(lab)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("描述这个工具要做的事…")
        self.editor.setMinimumHeight(150)
        root.addWidget(self.editor, 1)

        # ---- 分组：放进工具库的哪一组
        self.group_combo = QComboBox()
        self.group_combo.setObjectName("toolGroupCombo")
        choices = [str(g) for g in (group_choices or []) if str(g).strip()]
        if CUSTOM_GROUP not in choices:
            choices.append(CUSTOM_GROUP)
        for g in choices:
            self.group_combo.addItem(g)
        cur = str(initial_group or "").strip()
        if cur and self.group_combo.findText(cur) >= 0:
            self.group_combo.setCurrentText(cur)
        else:
            self.group_combo.setCurrentText(CUSTOM_GROUP)

        grow = QHBoxLayout()
        grow.setContentsMargins(0, 0, 0, 0)
        glab = QLabel("分组")
        glab.setStyleSheet("font-size:11px;color:#43413c;font-weight:600;")
        grow.addWidget(glab)
        grow.addWidget(self.group_combo, 1)
        root.addLayout(grow)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("下一步")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def value(self) -> tuple[str, str]:
        """返回 (算法描述, 选中的分组)。"""
        return (self.editor.toPlainText().strip(),
                str(self.group_combo.currentText() or "").strip() or CUSTOM_GROUP)


class OperatorDialog(QDialog):
    """预览「按文字描述生成的工具」：看得到代码、试跑结果，再决定要不要入库。

    还能挑它进工具库的哪个分组；不挑就放「我的工具」。
    """

    def __init__(self, spec: dict, parent=None, group_choices: list[str] | None = None):
        super().__init__(parent)
        self.spec = dict(spec or {})
        title = self.spec.get("title") or self.spec.get("name") or "新工具"
        self.setWindowTitle(f"新工具预览 · {title}")
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

        # ---- 分组：放进工具库的哪一组
        self.group_combo = QComboBox()
        self.group_combo.setObjectName("toolGroupCombo")
        # 候选 = 传入的分组 + 恒定的「我的工具」（没选时的默认落点）
        choices = [str(g) for g in (group_choices or []) if str(g).strip()]
        if CUSTOM_GROUP not in choices:
            choices.append(CUSTOM_GROUP)
        for g in choices:
            self.group_combo.addItem(g)
        cur_group = str(self.spec.get("group") or "").strip()
        if cur_group and self.group_combo.findText(cur_group) >= 0:
            self.group_combo.setCurrentText(cur_group)
        else:
            self.group_combo.setCurrentText(CUSTOM_GROUP)
        grow = QHBoxLayout()
        grow.setContentsMargins(0, 0, 0, 0)
        glab = QLabel("分组")
        glab.setStyleSheet("font-size:11px;color:#43413c;font-weight:600;")
        grow.addWidget(glab)
        grow.addWidget(self.group_combo, 1)
        root.addLayout(grow)

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

        lab = QLabel("工具代码（可编辑）")
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

        note = QLabel("代码会经过 AST 白名单检查（禁止 import / while / 文件读写 / "
                      "read_pickle 等危险方法），并在受限命名空间里运行；"
                      "但这不是操作系统级沙箱，请只保存你信任的代码。")
        note.setWordWrap(True)
        note.setObjectName("tiny")
        root.addWidget(note)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("保存并加入工具库")
        btns.button(QDialogButtonBox.Cancel).setText("取消")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        root.addWidget(btns)

    def value(self) -> dict:
        spec = dict(self.spec)
        spec["code"] = self.editor.toPlainText()
        # 分组：用户选了就用选的；下拉框为空（理论上不会）则回落到默认组
        picked = str(self.group_combo.currentText() or "").strip()
        spec["group"] = picked or CUSTOM_GROUP
        return spec
