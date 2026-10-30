"""@ 图层提及的回归：解析、匹配、消歧、输入框补全弹窗。

离线，不联网。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.mentions import (ambiguity_question, mentions_note, not_found_note,
                          parse_mentions, resolve_mentions, strip_mentions)

app = QApplication.instance() or QApplication([])
fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


class FakeLayer:
    def __init__(self, i, n, geom="Polygon", count=100):
        self.id, self.name = i, n
        self._geom, self._count = geom, count

    def meta(self):
        return {"geomType": self._geom, "count": self._count,
                "crs": "EPSG:4326", "fields": []}


class FakeStore:
    def __init__(self, layers):
        self.layers = {l.id: l for l in layers}


LAYERS = [FakeLayer("l1", "小区"), FakeLayer("l2", "小区_合并"),
          FakeLayer("l3", "地铁站"), FakeLayer("l4", "2024 年 GDP"),
          FakeLayer("l5", "gis_osm_roads_free_1.shp")]
store = FakeStore(LAYERS)

print("[1] 语法解析（此时还不知道图层名，先粗抓）")
check("单个", [m.name for m in parse_mentions("对 @小区 做缓冲区")], ["小区 做缓冲区"])
check("多个", [m.name for m in parse_mentions("@A 和 @B 求交集")], ["A 和", "B 求交集"])
check("没有提及", parse_mentions("就做个缓冲区"), [])
check("中文紧挨着", [m.name for m in parse_mentions("用@地铁站 求交集")], ["地铁站 求交集"])
check("email 不算", parse_mentions("发邮件 a.b@x.co.cn 吧"), [])
check("数字坐标不误伤", parse_mentions("116.4, 39.9 那个点"), [])
check("带点的文件名", [m.name for m in parse_mentions("@gis_osm_roads.shp 抽一下")],
      ["gis_osm_roads.shp 抽一下"])

print("[2] 结合图层名截断（这才是真正取名字的一步）")
ms = resolve_mentions(parse_mentions("对 @小区 做缓冲区"), store)
check("截到 小区", ms[0].name, "小区")
check("命中 小区", ms[0].layer.id, "l1")
ms = resolve_mentions(parse_mentions("@2024 年 GDP 的分布"), store)
check("带空格的图层名", ms[0].name, "2024 年 GDP")
check("命中 GDP 图层", ms[0].layer.id, "l4")
ms = resolve_mentions(parse_mentions("@gis_osm_roads_free_1.shp 抽一下"), store)
check("带点文件名", ms[0].name, "gis_osm_roads_free_1.shp")
check("命中 roads", ms[0].layer.id, "l5")
ms = resolve_mentions(parse_mentions("@A 和 @B 求交集"), store)
check("多个各自截断", [m.name for m in ms], ["A", "B"])

print("[3] 位置信息正确")
ms = parse_mentions("对 @小区 做缓冲区")
m = ms[0]
check("start", m.start, 2)
check("raw", m.raw, "@小区 做缓冲区")
ms = resolve_mentions(parse_mentions("对 @小区 做缓冲区"), store)
check("截断后 end 也跟着缩", ms[0].end, 5)

print("[3] 精确匹配")
ms = resolve_mentions(parse_mentions("@小区"), store)
check("命中 小区", ms[0].layer.name, "小区")
check("不是 小区_合并", ms[0].layer.id, "l1")
check("exact", ms[0].exact, True)

print("[4] 完整名字也精确命中")
ms = resolve_mentions(parse_mentions("@小区_合并"), store)
check("命中 小区_合并", ms[0].layer.id, "l2")

print("[5] 歧义 → 候选 + 澄清问题")
ms = resolve_mentions(parse_mentions("@小"), store)
check("没有直接命中", ms[0].layer, None)
check("有两个候选", len(ms[0].candidates), 2)
q = ambiguity_question(ms)
check("有澄清问题", q is not None, True)
check("问题提到原名", "小" in q["question"], True)
check("选项是两个图层", sorted(q["options"]), ["小区", "小区_合并"])

print("[6] 找不到 → 说清楚")
ms = resolve_mentions(parse_mentions("@不存在"), store)
check("没命中", ms[0].layer, None)
check("有原因", "不存在" in ms[0].reason, True)
note = not_found_note(ms)
check("提示里带名字", "不存在" in note, True)
check("歧义的不算没找到", ambiguity_question(ms), None)

print("[7] 去掉 @ 标记但保留名字")
ms = parse_mentions("对 @小区 做 300 米缓冲区")
check("strip 后没有 @", "@" not in strip_mentions("对 @小区 做 300 米缓冲区", ms), True)
check("strip 后名字还在", "小区" in strip_mentions("对 @小区 做 300 米缓冲区", ms), True)

print("[8] 给模型看的提示")
ms = resolve_mentions(parse_mentions("@小区 和 @地铁站"), store)
note = mentions_note(ms)
check("提到第一个", "小区" in note, True)
check("提到第二个", "地铁站" in note, True)
check("没解析到就不给提示", mentions_note(resolve_mentions(parse_mentions("@无"), store)), "")

print("[9] 补全弹窗")
from native.mention_popup import MentionPopup
popup = MentionPopup()
popup.set_candidates(LAYERS, "")
check("全部列出", popup.count(), 5)
popup.set_candidates(LAYERS, "小区")
check("过滤出两个", popup.count(), 2)
popup.set_candidates(LAYERS, "小区_")
check("前缀优先", popup.current_name(), "小区_合并")
popup.set_candidates(LAYERS, "zzzz")
check("没有匹配就是 0", popup.count(), 0)

print("[9b] 弹窗样式：标题、计数、提示")
popup.set_candidates(LAYERS, "")
check("标题", popup.title.text(), "选择图层")
check("显示总数", "5" in popup.count_label.text(), True)
popup.set_candidates(LAYERS, "小区")
check("过滤时标题变了", popup.title.text(), "匹配图层")
check("过滤时显示查询词", "小区" in popup.count_label.text(), True)
check("有快捷键提示", "Enter" in popup.foot_hint.text(), True)
check("每项有固定行高", popup.list.item(0).sizeHint().height(), 34)

print("[9c] 自绘：几何色点 + 选中行高亮")
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget
popup.set_candidates(LAYERS, "")
popup.resize(292, 44 + 34 * 5 + 30)
popup.show()
app.processEvents()
img = QImage(popup.size(), QImage.Format_ARGB32)
img.fill(QColor("#ffffff"))
popup.render(img)
dots = set()
for y in range(20, img.height() - 30):
    for x in range(0, 60):
        c = QColor(img.pixel(x, y)).name()
        if c in ("#5b8def", "#6fa87a", "#d9a441"):
            dots.add(c)
check("画出了几何类型色点", len(dots) >= 1, True)
check("点用的是几何类型的颜色", "#6fa87a" in dots, True)
popup._current = 0
popup.list.setCurrentRow(0)
img2 = QImage(popup.size(), QImage.Format_ARGB32)
img2.fill(QColor("#ffffff"))
popup.render(img2)
found_sel = found_accent = False
for y in range(20, 60):
    for x in range(0, img2.width()):
        c = QColor(img2.pixel(x, y)).name()
        if c == "#f3f0e8":
            found_sel = True
        if c == "#d97757":
            found_accent = True
check("选中行有浅底", found_sel, True)
check("选中行有橙色强调条", found_accent, True)

print("[9d] 弹窗背景不能是透明的（独立顶层窗口拿不到主窗口的 QSS）")
from PySide6.QtCore import QPoint
check("弹窗自己带了样式表", popup.styleSheet() != "", True)
check("样式表里有 mentionBox 规则", "mentionBox" in popup.styleSheet(), True)
anchor = QWidget()
anchor.resize(400, 44)
popup2 = MentionPopup()
popup2.set_candidates(LAYERS, "")
popup2.show_at(anchor)
app.processEvents()
img3 = QImage(popup2.size(), QImage.Format_ARGB32)
img3.fill(QColor("#ffffff"))
popup2.render(img3)


def _at(x, y):
    return QColor(img3.pixel(x, y)).name()


check("内容区是白底（不是透明）", _at(150, 6), "#ffffff")
check("底栏是浅色带", _at(150, popup2.height() - 8), "#fbfaf6")
check("有细边框", _at(0, 40), "#e0dbcd")

print("[9e] 圆角：四个角被遮罩裁掉")
region = popup2.mask()
check("有遮罩", not region.isEmpty(), True)
check("左上角在框外", region.contains(QPoint(0, 0)), False)
check("左下角在框外", region.contains(QPoint(0, popup2.height() - 1)), False)
check("右上角在框外", region.contains(QPoint(popup2.width() - 1, 0)), False)
check("右下角在框外", region.contains(QPoint(popup2.width() - 1, popup2.height() - 1)), False)
check("中心在框内", region.contains(QPoint(popup2.width() // 2, popup2.height() // 2)), True)

print("[10] 弹窗键盘操作")
popup.set_candidates(LAYERS, "")
first = popup.current_name()
popup.move_selection(1)
check("下移换了一个", popup.current_name() != first, True)
popup.move_selection(-1)
check("上移回来了", popup.current_name(), first)
popup.move_selection(-1)
check("从头上移 → 绕到最后", popup.current_name() != first, True)

print("[11] 输入框里的补全联动")
from native.chat import ChatPanel


class FakeWin:
    def __init__(self, store):
        self.store = store


def type_text(panel, text):
    """模拟真实打字：insertPlainText 会把光标留在末尾（setPlainText 不会）。"""
    panel.input.setPlainText("")
    panel.input.insertPlainText(text)
    panel._on_input_changed()


panel = ChatPanel(FakeWin(store))
type_text(panel, "@")
check("输入 @ 弹出弹窗", panel._mention_popup is not None
      and panel._mention_popup.count() == 5, True)
type_text(panel, "@小区")
check("继续打字会过滤", panel._mention_popup.count(), 2)
panel._on_mention_picked("小区_合并")
check("选中后文本正确", panel.input.toPlainText(), "@小区_合并")
check("光标在名字后面", panel.input.textCursor().position(), len("@小区_合并"))

type_text(panel, "对 @小区 做缓冲区")
check("有空格就关闭弹窗", panel._mention_popup.isVisible(), False)

type_text(panel, "@")
check("又弹出来了", panel._mention_popup.isVisible(), True)

print("[12] 没有图层时不弹")
panel2 = ChatPanel(FakeWin(FakeStore([])))
type_text(panel2, "@")
check("不弹", panel2._mention_popup is None
      or not panel2._mention_popup.isVisible(), True)

print("[12b] 回车能选中图层（弹窗不能抢焦点）")
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent

popup_obj = panel._mention_popup
check("弹窗显示时不抢焦点",
      popup_obj.testAttribute(Qt.WA_ShowWithoutActivating), True)
check("弹窗不接收焦点",
      bool(popup_obj.windowFlags() & Qt.WindowDoesNotAcceptFocus), True)
check("弹窗焦点策略是 NoFocus", popup_obj.focusPolicy() == Qt.NoFocus, True)

type_text(panel, "@小区")
check("弹窗开着", panel._mention_popup.isVisible(), True)
ev = QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier)
app.sendEvent(panel.input, ev)
app.processEvents()
check("回车后填入了图层名", panel.input.toPlainText(), "@小区")
check("回车后弹窗关掉了", not panel._mention_popup.isVisible(), True)
check("光标停在名字后面", panel.input.textCursor().position(), len("@小区"))

print("[12c] 弹窗自己也能处理按键（焦点万一跑过去时的兜底）")
popup2 = MentionPopup()
popup2.set_candidates(LAYERS, "")
got = []
popup2.picked.connect(got.append)
popup2.dismissed.connect(lambda: got.append("__dismissed__"))
app.sendEvent(popup2, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
check("回车 → 选中第一项", got, ["小区"])
got.clear()
popup2._current = 1
app.sendEvent(popup2, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
# 无查询时按名字长度排序：小区 / 地铁站 / 小区_合并 / …
check("回车 → 选中当前项", got, ["地铁站"])
got.clear()
app.sendEvent(popup2, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
check("ESC → 关闭", got, ["__dismissed__"])
got.clear()
popup2._current = 0
app.sendEvent(popup2, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Down, Qt.NoModifier))
check("↓ → 移到第二项", popup2.current_name(), "地铁站")
app.sendEvent(popup2, QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Up, Qt.NoModifier))
check("↑ → 移回第一项", popup2.current_name(), "小区")

print("[13] 规划器：规则引擎认得被提及的图层")
from app.agent import Planner

p = Planner(store)
ms = resolve_mentions(parse_mentions("对 @小区 做 500 米缓冲区"), store)
stripped = strip_mentions("对 @小区 做 500 米缓冲区", ms)
plan = p.rule_plan(stripped, None)
check("生成了计划", len(plan) >= 1, True)
check("用的是 小区（不是 小区_合并）", plan[0]["args"].get("layer"), "l1")
check("距离对", plan[0]["args"].get("distance"), 500.0)

print("[14] 规划器：提示里告诉模型用户点名了哪个图层")
captured = {}


def fake_call(cfg, msgs, json_mode=True, max_tokens=1600):
    captured["sys"] = msgs[0]["content"]
    captured["user"] = msgs[-1]["content"]
    return ('{"plan":[],"reply":"ok"}', "stop")


p2 = Planner(store)
p2._call_llm = fake_call
ms = resolve_mentions(parse_mentions("对 @小区 做缓冲区"), store)
p2.llm_plan(strip_mentions("对 @小区 做缓冲区", ms), "l1",
            {"api_key": "k", "base_url": "http://x", "model": "m"}, [],
            mention_context=mentions_note(ms))
check("系统提示含点名说明", "明确指定" in captured["sys"], True)
check("点名说明里是 小区", "小区" in captured["sys"], True)
check("用户消息里没有 @ 了", "@" not in captured["user"], True)

print("[15] @ 提到的名字仍然留在正文里（规则引擎要靠它认图层）")
check("正文保留名字", "小区" in stripped, True)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
