"""启动画面（splash）：主窗口出现之前先播一小段开场动画。

对标 QGIS 的启动 splash —— 一张品牌卡片 + 一条进度条 + 一行「正在做什么」，
区别只有两点，都是为了贴合本项目：

· 进度条跟着**真实启动阶段**走（恢复会话 → 构建工作台 → 准备渲染），不是纯计时器；
  阶段由 `MainWindow(on_stage=...)` 在关键节点回调，见 STAGES；
· 收尾是 220ms 淡出，主窗口在下面已经画好了，切过去不会闪一下白底。

为什么不用 QAbstractAnimation / QTimer：
这段代码跑在 `app.exec()` **之前**，既没有事件循环可依赖，又不能把主窗口的构造
塞进回调里（Qt 控件只能在主线程一次建完）。所以用「自旋帧 + processEvents」手动
推帧：`start()` 播开场，`finish()` 补足最短时长后淡出，全程界面都在动。

为什么不做进 `MainWindow.__init__`：
tools/ 下几十个测试都直接 `MainWindow()`，启动动画属于「进程入口」的事，
塞进 __init__ 会让每个测试都白等一秒。`native_app.py` 是唯一的接入点；
`GIS_AGENT_NO_SPLASH=1`（或命令行 `--no-splash`、offscreen 平台）可以关掉。
"""
from __future__ import annotations

import math
import os
import sys
import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QGuiApplication,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF,
                           QRadialGradient)
from PySide6.QtWidgets import QApplication, QWidget

from .branding import ICON_BG, icon_body_points
from .theme import COLORS

#: 启动阶段：key → (大标题, 副标题, 进度)。进度只增不减，最后停在 1.0。
#: key 由 MainWindow 在真实耗时处回调，顺序是 session → ui → map（`ready` 由 finish() 置位）。
STAGES: dict[str, tuple[str, str, float]] = {
    "boot": ("正在载入程序组件…", "地理运算库 · 界面主题 · 中文字体", 0.06),
    "session": ("正在恢复上次会话…", "上次的图层、对话记录和自定义工具", 0.34),
    "ui": ("正在构建工作台…", "三栏布局：图层 · 地图 · 对话", 0.62),
    "map": ("正在准备地图渲染…", "建立渲染线程与瓦片缓存", 0.86),
    "ready": ("准备就绪", "即将进入工作台", 1.0),
}

#: 界面语言里唯一的强调色（和 theme.COLORS 同值；这里单独取，免得为了一个色值多绕一层）
_ACCENT = COLORS["accent_fill"]
_ACCENT_DEEP = COLORS["accent"]


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _ease_out(x: float) -> float:
    """缓出（cubic）：开头快、收尾稳，适合「画出来 / 浮上来」这类动作。"""
    x = _clamp01(x)
    return 1.0 - (1.0 - x) ** 3


class SplashScreen(QWidget):
    """一张 520×242 的暖白卡片：logo + 名称 + 阶段文字 + 进度条 + 百分比。

    自绘（不走 QSS）：启动画面要在主窗口的样式表装上去之前就能画，
    而且这里全是动画（描边进度、呼吸点、流光、淡出），用 QPainter 直接画最省事。
    """

    #: 卡片尺寸（逻辑像素）
    CARD_W = 520
    CARD_H = 242
    #: 圆角
    RADIUS = 16
    #: 卡片内边距
    PAD = 28
    #: 卡片四周留给投影的空白（窗口比卡片大一圈）
    SPREAD = 18
    #: 最短展示时长（毫秒，从 start() 起算）。导入模块 + 建窗口通常已经超过它，
    #: 只有极快的机器才会在这里多停留一会儿，避免「一闪而过」。
    MIN_MS = 1150.0

    def __init__(self, parent=None):
        super().__init__(parent, Qt.SplashScreen | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedSize(self.CARD_W + 2 * self.SPREAD, self.CARD_H + 2 * self.SPREAD)

        self._f_title = self._font(17, QFont.DemiBold)
        self._f_sub = self._font(11)
        self._f_status = self._font(13)
        self._f_detail = self._font(11)
        self._f_foot = self._font(10)

        self._t0 = 0.0
        self._stage_key = "boot"
        self._title, self._detail, self._target = STAGES["boot"]
        self._progress = 0.0
        self._intro = 0.0      # 开场动画进度（淡入 / 上浮 / 图标描边）
        self._phase = 0.0      # 呼吸点 / 流光的相位
        self._skip = False     # 用户点了一下 → 别等满最短时长
        self._finished = False
        self._opacity_ok = self._supports_opacity()

    # ------------------------------------------------------------------ 生命周期
    @classmethod
    def maybe(cls) -> "SplashScreen | None":
        """要显示启动画面就返回一个，否则 None。

        默认在离屏/无头平台上关掉（回归测试、截图脚本走这里）。关掉的方式：
        `GIS_AGENT_NO_SPLASH=1`、命令行 `--no-splash`；反过来 `GIS_AGENT_SPLASH=1`
        可以强制打开 —— 测试用它把「启动入口」整条路径在离屏下跑一遍。
        """
        if os.environ.get("GIS_AGENT_NO_SPLASH") == "1":
            return None
        if "--no-splash" in sys.argv:
            return None
        if QApplication.instance() is None:
            return None
        if os.environ.get("GIS_AGENT_SPLASH") == "1":
            return cls()
        platform = os.environ.get("QT_QPA_PLATFORM") or ""
        if not platform:
            try:
                platform = QGuiApplication.platformName() or ""
            except Exception:      # noqa: BLE001
                platform = ""
        if platform.lower().startswith(("offscreen", "minimal", "vnc")):
            return None
        return cls()

    def start(self) -> None:
        """居中显示，先播开场动画（约 380ms），然后就可以去干重活了。

        这段自旋不是白等：它让卡片真的「浮」出来，而不是伴随导入模块一起卡在
        第一帧；播放完就停在一个完整、静止的界面上，正好遮住后面约 1 秒的模块导入。
        """
        self._t0 = time.perf_counter()
        self._center()
        self._set_opacity(0.0)
        self.show()
        self._flush()                     # 让窗口先映射出来，别让导入期间是一块空白
        self._play_intro(380.0)

    def stage(self, key: str) -> None:
        """切换到某个启动阶段（MainWindow 在关键节点回调）。

        这里只 `repaint()`，**不跑事件队列**：调用点在 MainWindow.__init__ 里，
        此刻处理事件会让后台线程的信号打到还没建好的控件上。
        """
        if key not in STAGES:
            return
        self._stage_key = key
        self._title, self._detail, self._target = STAGES[key]
        # 立刻吃掉大半个差值：界面下一刻可能就被构建工作卡住，等不到动画
        self._progress += (self._target - self._progress) * 0.75
        self.repaint()

    def finish(self, min_ms: float | None = None) -> None:
        """补足最短展示时长，把进度走到 100%，淡出并关掉自己。"""
        if self._finished:
            return
        self._finished = True
        if self._intro <= 0.0:            # 没走过 start()（测试 / 截图脚本）
            self._intro = 1.0
        self.stage("ready")
        deadline = self._t0 + (self.MIN_MS if min_ms is None
                               else max(0.0, float(min_ms))) / 1000.0
        while True:
            self._advance(1.0 / 60.0)
            self._flush()
            if self._skip:
                break
            if time.perf_counter() >= deadline and self._progress >= 0.999:
                break
            time.sleep(1.0 / 60.0)
        self._fade_out(220.0)
        self.close()

    def abort(self) -> None:
        """出错时立刻收掉（不播动画），让错误对话框能正常弹出来。"""
        self._finished = True
        try:
            self.close()
        except Exception:      # noqa: BLE001  收尾失败不该盖住真正的异常
            pass

    # ------------------------------------------------------------------ 动画
    def _play_intro(self, ms: float) -> None:
        """开头的一小段：卡片淡入 + 内容上浮 + 图标一笔画出来。"""
        t0 = time.perf_counter()
        while True:
            f = (time.perf_counter() - t0) * 1000.0 / max(1.0, ms)
            if f >= 1.0:
                break
            self._intro = f
            self._phase += 1.0 / 60.0
            self._set_opacity(_ease_out(_clamp01(f / 0.42)))       # 卡片淡入
            # 开场就把进度从 0 推到「载入组件」这一档，别让进度条一直停在 0%
            self._progress = max(self._progress,
                                 self._target * _ease_out(_clamp01(f / 0.55)))
            self._flush()
            time.sleep(1.0 / 60.0)
        self._intro = 1.0
        self._set_opacity(1.0)
        self._progress = max(self._progress, self._target)
        self._flush()

    def _advance(self, dt: float) -> None:
        """推一帧状态：进度逼近目标、相位前进（呼吸点 / 流光靠它动）。"""
        self._phase += dt
        gap = self._target - self._progress
        if abs(gap) < 0.005:
            self._progress = self._target
        else:
            self._progress += gap * min(1.0, dt * 8.0)

    def _fade_out(self, ms: float) -> None:
        start = self.windowOpacity() if self._opacity_ok else 1.0
        t0 = time.perf_counter()
        while True:
            f = _clamp01((time.perf_counter() - t0) * 1000.0 / max(1.0, ms))
            self._set_opacity(start * (1.0 - _ease_out(f)))
            self._flush()
            if f >= 1.0:
                break
            time.sleep(1.0 / 60.0)

    def _flush(self) -> None:
        """立刻画一帧并把事件队列推一下（窗口贴图 / 透明度要它才会真的变）。"""
        self.repaint()
        app = QApplication.instance()
        if app is not None:
            app.processEvents()

    def _set_opacity(self, value: float) -> None:
        if not self._opacity_ok:
            return
        try:
            self.setWindowOpacity(_clamp01(value))
        except Exception:      # noqa: BLE001  极少数平台不支持窗口透明度
            self._opacity_ok = False

    @staticmethod
    def _supports_opacity() -> bool:
        """离屏/最小平台没有真窗口，设透明度只会刷一排警告。"""
        try:
            platform = (QGuiApplication.platformName() or "").lower()
        except Exception:      # noqa: BLE001
            return True
        return not platform.startswith(("offscreen", "minimal", "vnc"))

    def _center(self) -> None:
        """落在鼠标所在的那块屏幕上（多屏时别总是弹到主屏）。"""
        scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if scr is None:
            return
        geo = scr.availableGeometry()
        self.move(geo.x() + (geo.width() - self.width()) // 2,
                  geo.y() + (geo.height() - self.height()) // 2)

    # ------------------------------------------------------------------ 事件
    def mousePressEvent(self, e):  # noqa: N802
        # 点一下就别等最短时长了（构建工作干完就走）
        self._skip = True
        e.accept()

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() in (Qt.Key_Escape, Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self._skip = True
            e.accept()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, e):  # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing
                             | QPainter.SmoothPixmapTransform)
            card = QRectF(self.SPREAD, self.SPREAD, self.CARD_W, self.CARD_H)
            self._paint_shadow(p, card)
            self._paint_card(p, card)
            clip = QPainterPath()
            clip.addRoundedRect(card, self.RADIUS, self.RADIUS)
            p.save()
            p.setClipPath(clip)
            self._paint_content(p, card)
            p.restore()
        finally:
            p.end()

    def _paint_shadow(self, p: QPainter, card: QRectF) -> None:
        """一圈极淡的暖色投影。不用 QGraphicsDropShadowEffect：半透明顶层窗口上
        那个效果又慢又容易画歪，纸片感也不符合本项目的「少阴影」语言。"""
        p.setPen(Qt.NoPen)
        for i in range(self.SPREAD, 0, -1):
            alpha = 1 + int(3.2 * (1.0 - i / float(self.SPREAD)) ** 2)
            p.setBrush(QColor(60, 48, 36, alpha))
            p.drawRoundedRect(card.adjusted(-i, -i + 1, i, i + 3),
                              self.RADIUS + i, self.RADIUS + i)

    def _paint_card(self, p: QPainter, card: QRectF) -> None:
        p.setPen(Qt.NoPen)
        grad = QLinearGradient(card.topLeft(), card.bottomLeft())
        grad.setColorAt(0.0, QColor("#fdfcf8"))
        grad.setColorAt(1.0, QColor("#f7f5ee"))
        p.setBrush(grad)
        p.drawRoundedRect(card, self.RADIUS, self.RADIUS)
        pen = QPen(QColor(226, 221, 208))
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5),
                          self.RADIUS, self.RADIUS)

    def _paint_content(self, p: QPainter, card: QRectF) -> None:
        f = self._intro
        title_a = _ease_out(_clamp01((f - 0.14) / 0.42))
        sub_a = _ease_out(_clamp01((f - 0.24) / 0.42))
        body_a = _ease_out(_clamp01((f - 0.32) / 0.45))
        rise = _ease_out(_clamp01(f / 0.55))
        logo_t = _ease_out(_clamp01((f - 0.04) / 0.72))

        # 版式（卡片固定 520×242，纵向节奏写死在这一段里）：
        #   28 起 logo(64) → 112 分隔线 → 138 状态 / 158 副标题 → 186 进度条 → 216 底行
        x0 = card.left()
        y0 = card.top() + (1.0 - rise) * 9.0      # 内容随开场轻轻上浮
        pad = self.PAD

        # logo 区背后一小片暖光（很淡，只是让左上角不空）
        glow = QRadialGradient(QPointF(x0 + pad + 34, y0 + pad + 34), 150.0)
        glow_end = QColor(_ACCENT)
        glow_end.setAlpha(0)
        glow.setColorAt(0.0, self._a(QColor(_ACCENT), 20.0 / 255.0 * body_a))
        glow.setColorAt(1.0, glow_end)
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawRect(card)

        logo_rect = QRectF(x0 + pad, y0 + pad, 64.0, 64.0)
        self._paint_logo(p, logo_rect, logo_t)

        fm_t = QFontMetrics(self._f_title)
        fm_s = QFontMetrics(self._f_sub)
        block_h = fm_t.height() + 5 + fm_s.height()
        block_top = logo_rect.top() + (64.0 - block_h) / 2.0
        tx = logo_rect.right() + 16.0

        p.setFont(self._f_title)
        p.setPen(self._a(COLORS["ink"], title_a))
        p.drawText(QPointF(tx, block_top + fm_t.ascent()), "GIS 矢量数据 Agent")

        p.setFont(self._f_sub)
        p.setPen(self._a(COLORS["muted"], sub_a))
        p.drawText(QPointF(tx, block_top + fm_t.height() + 5 + fm_s.ascent()),
                   "说一句话，完成空间分析")

        # 分隔线
        p.setPen(QPen(self._a(COLORS["line"], body_a), 1.0))
        p.drawLine(QPointF(x0 + pad, y0 + 112),
                   QPointF(x0 + self.CARD_W - pad, y0 + 112))

        # 状态行：呼吸的小圆点 + 「正在做什么」
        fm_st = QFontMetrics(self._f_status)
        status_base = y0 + 138
        pulse = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(self._phase * 3.2))
        p.setPen(Qt.NoPen)
        p.setBrush(self._a(COLORS["accent"], body_a * pulse))
        p.drawEllipse(QPointF(x0 + pad + 4.0, status_base - fm_st.ascent() * 0.42),
                      3.2, 3.2)
        p.setFont(self._f_status)
        p.setPen(self._a(COLORS["ink2"], body_a))
        p.drawText(QPointF(x0 + pad + 16.0, status_base), self._title)

        p.setFont(self._f_detail)
        p.setPen(self._a(COLORS["faint"], body_a))
        p.drawText(QPointF(x0 + pad + 16.0, y0 + 158), self._detail)

        # 进度条
        bar = QRectF(x0 + pad, y0 + 186, self.CARD_W - 2 * pad, 4.0)
        p.setPen(Qt.NoPen)
        p.setBrush(self._a(QColor("#ece8db"), body_a))
        p.drawRoundedRect(bar, 2.0, 2.0)
        filled = QRectF(bar.left(), bar.top(),
                        max(6.0, bar.width() * _clamp01(self._progress)), bar.height())
        fill_grad = QLinearGradient(filled.topLeft(), filled.topRight())
        fill_grad.setColorAt(0.0, self._a(QColor(_ACCENT), body_a))
        fill_grad.setColorAt(1.0, self._a(QColor(_ACCENT_DEEP), body_a))
        p.setBrush(fill_grad)
        p.drawRoundedRect(filled, 2.0, 2.0)

        # 流光：沿整条轨道扫，表示「还在动」（走完 100% 就不再扫）
        if self._progress < 0.998:
            p.save()
            p.setClipRect(bar)
            band = 96.0
            bx = bar.left() - band + (bar.width() + band) * ((self._phase * 0.55) % 1.0)
            sweep = QLinearGradient(bx, 0.0, bx + band, 0.0)
            sweep.setColorAt(0.0, QColor(255, 255, 255, 0))
            sweep.setColorAt(0.5, QColor(255, 255, 255, 96))
            sweep.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(sweep)
            p.drawRect(bar)
            p.restore()

        # 底行：技术签名 + 百分比
        foot_base = y0 + 216
        p.setFont(self._f_foot)
        p.setPen(self._a(COLORS["faint"], body_a))
        p.drawText(QPointF(x0 + pad, foot_base), "纯 Qt 原生渲染 · 无 Web 引擎")
        pct = f"{int(round(_clamp01(self._progress) * 100))}%"
        fm_f = QFontMetrics(self._f_foot)
        p.drawText(QPointF(x0 + self.CARD_W - pad - fm_f.horizontalAdvance(pct),
                           foot_base), pct)

    def _paint_logo(self, p: QPainter, rect: QRectF, t: float) -> None:
        """把应用图标按 64×64 设计稿画出来，并按 t 播「底 → 描边 → 填面 → 句柄」。

        几何取自 native/branding.py 的同一份 `ICON_SHAPE`，所以启动画面里这个 logo
        和窗口图标、任务栏图标是同一个图形，只是多了画出来的过程。
        """
        if t <= 0.001:
            return
        pts = icon_body_points()
        p.save()
        p.translate(rect.left(), rect.top())
        p.scale(rect.width() / 64.0, rect.width() / 64.0)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 1) 陶土橙圆角底
        p.setPen(Qt.NoPen)
        p.setBrush(self._a(QColor(ICON_BG), _ease_out(_clamp01(t / 0.34))))
        p.drawRoundedRect(QRectF(1.5, 1.5, 61.0, 61.0), 15.0, 15.0)

        # 2) 后层叠置（大图标才有的那层），最后淡入
        back = _ease_out(_clamp01((t - 0.55) / 0.30)) * (84.0 / 255.0)
        if back > 0.004:
            pen = QPen(self._a(QColor(255, 255, 255), back))
            pen.setWidthF(2.0)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(QPolygonF([QPointF(x + 3.4, y - 4.2) for x, y in pts]))

        # 3) 主体：白色轮廓一笔画出来
        path = self._partial_polygon(pts, _clamp01((t - 0.06) / 0.66))
        if path is not None:
            pen = QPen(QColor(255, 255, 255))
            pen.setWidthF(2.6)
            pen.setJoinStyle(Qt.RoundJoin)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

        # 4) 半透明填面
        fill = _ease_out(_clamp01((t - 0.62) / 0.26)) * (52.0 / 255.0)
        if fill > 0.004:
            p.setPen(Qt.NoPen)
            p.setBrush(self._a(QColor(255, 255, 255), fill))
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))

        # 5) 三个顶点句柄依次弹出（和图标一样落在同一批顶点上）
        p.setPen(Qt.NoPen)
        for i, idx in enumerate((1, 3, 5)):
            k = _clamp01((t - (0.70 + i * 0.08)) / 0.18)
            if k <= 0.0:
                continue
            side = 5.6 * (0.55 + 0.45 * _ease_out(k))
            radius = 1.5 * side / 5.6
            x, y = pts[idx]
            p.setBrush(self._a(QColor(255, 255, 255), k))
            p.drawRoundedRect(QRectF(x - side / 2, y - side / 2, side, side),
                              radius, radius)
        p.restore()

    @staticmethod
    def _partial_polygon(pts, frac: float) -> QPainterPath | None:
        """按周长比例截出闭合折线的前一段（图标描边动画用）。"""
        frac = _clamp01(frac)
        if frac <= 0.0:
            return None
        segs = [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
        remain = sum(math.dist(a, b) for a, b in segs) * frac
        path = QPainterPath(QPointF(*pts[0]))
        for a, b in segs:
            if remain <= 0.0:
                break
            seg = math.dist(a, b) or 1e-6
            k = min(1.0, remain / seg)
            path.lineTo(QPointF(a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k))
            remain -= seg
        return path

    # ------------------------------------------------------------------ 小工具
    @staticmethod
    def _font(px: int, weight=None) -> QFont:
        f = QFont()
        f.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"])
        f.setPixelSize(px)
        if weight is not None:
            f.setWeight(weight)
        return f

    @staticmethod
    def _a(color: QColor, k: float) -> QColor:
        """同一颜色乘一个 0..1 的透明度系数（文字/描边的渐显都走它）。"""
        c = QColor(color)
        c.setAlpha(int(round(c.alpha() * _clamp01(k))))
        return c
