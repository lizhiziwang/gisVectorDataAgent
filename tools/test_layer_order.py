"""图层顺序（拖拽调整上下层级）的回归测试。

覆盖：
· `Store.reorder`：自下而上的顺序、未知 id 忽略、没提到的图层不丢
· 背景类算子（缓冲区/融合/网格/合并/凸包/泰森）新建时默认垫底
· 面板从上到下 = 从顶层到底层；面板顺序、Store 顺序、地图渲染顺序三者一致
· 拖拽交互：按下 → 拖动 → 松手真的换顺序；点选 / 小抖动不改顺序；
  拖动时出现提示线、松手后收起；当前图层跟着被拖的那层走
· 右键菜单「顺序」：置顶 / 上移一层 / 下移一层 / 置底
· 合成顺序：后画的压在上面（把栅格拖到矢量上面真的有效果）
· 旧会话迁移：背景算子挪到底部；`layer_order=2` 的会话原样恢复
· 会话持久化：调整过的图层顺序重启后还在

用法：.venv\\Scripts\\python.exe tools\\test_layer_order.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ["GIS_AGENT_PERSIST"] = "1"
_TMP = Path(tempfile.mkdtemp(prefix="gis_order_"))
os.environ["GIS_AGENT_DB"] = str(_TMP / "agent.db")
os.environ["GIS_AGENT_CACHE_DIR"] = str(_TMP / "cache")

import geopandas as gpd  # noqa: E402
from shapely.geometry import LineString, Point, box  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.engine import BACK_OPS, Store  # noqa: E402
from native.app import MainWindow, _migrate_layer_order  # noqa: E402
from native.mapview import _ComposeJob, _compose_job_image  # noqa: E402

fails: list[str] = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


def gdf_poly():
    return gpd.GeoDataFrame({"name": ["a", "b"]},
                            geometry=[box(0, 0, 1, 1), box(2, 2, 3, 3)],
                            crs="EPSG:4326")


def gdf_line():
    return gpd.GeoDataFrame({"name": ["r1", "r2"]},
                            geometry=[LineString([(0, 0), (1, 1)]),
                                      LineString([(2, 2), (3, 3)])],
                            crs="EPSG:4326")


def gdf_point():
    return gpd.GeoDataFrame({"name": ["p1", "p2", "p3"]},
                            geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
                            crs="EPSG:4326")


app = QApplication.instance() or QApplication([])

print("[1] Store.reorder：自下而上，未知 id 忽略")
st = Store(cache_dir=_TMP / "c1")
a = st.add(gdf_poly(), "A")
b = st.add(gdf_line(), "B")
c = st.add(gdf_point(), "C")
check("新图层默认追加在最上面", list(st.layers), [a.id, b.id, c.id])
st.reorder([c.id, a.id])
check("reorder 按给定顺序（第一个在最下面）", list(st.layers), [c.id, a.id, b.id])
check("没提到的图层按原顺序补在后面", set(st.layers), {a.id, b.id, c.id})
st.reorder(["nope", b.id])
check("未知 id 直接忽略，不丢图层", list(st.layers), [b.id, c.id, a.id])

print("[2] 背景类算子新建时默认垫底")
for op in sorted(BACK_OPS):
    s2 = Store(cache_dir=_TMP / f"c2_{op}")
    base = s2.add(gdf_poly(), "地块")
    out = s2.add(gdf_poly(), f"{op}结果", op=op)
    check(f"{op} 垫在原图层下面", list(s2.layers), [out.id, base.id])
s3 = Store(cache_dir=_TMP / "c3")
o1 = s3.add(gdf_poly(), "网格", op="grid")
n1 = s3.add(gdf_poly(), "普通图层")
check("普通图层仍然追加在最上面", list(s3.layers), [o1.id, n1.id])

print("[3] 面板顺序 = Store 顺序 = 地图渲染顺序")
win = MainWindow()
# 列表下面要留出空白，才测得动「拖到最后一行下面」；
# 图层多到超出一屏时靠自动滚动（[4] 末尾单独测）。
win.resize(1200, 1400)
win.show()
for _ in range(5):
    app.processEvents()
w = win.store
w.clear(preserve_cache=True)
la = w.add(gdf_poly(), "地块")
lb = w.add(gdf_line(), "道路")
lc = w.add(gdf_point(), "点位")
win.refresh_layers()
for _ in range(3):
    app.processEvents()

lst = win.layer_list


def panel_ids():
    return [lst.item(i).data(Qt.UserRole) for i in range(lst.count())]


def settle_frame(max_wait=30.0):
    """等地图把当前图层顺序完整画出来（帧签名 = 场景签名）。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < max_wait:
        app.processEvents()
        mv_ = win.map
        if (mv_._frame is not None and not mv_._has_pending()
                and mv_._frame_sig == mv_._scene_sig()):
            app.processEvents()
            return True
        time.sleep(0.01)
    return False


check("面板从上到下 = Store 从顶到底", panel_ids(), [lc.id, lb.id, la.id])
check("地图图层顺序 = Store 顺序", [l.id for l in win.map.layers], [la.id, lb.id, lc.id])
check("矢量渲染顺序 = Store 顺序", [l.id for l in win.map._layers_render_order()],
      [la.id, lb.id, lc.id])

win.reorder_layer(lc.id, 2)          # 把最上面那层拖到最底下
check("置底后 Store 顺序", list(w.layers), [lc.id, la.id, lb.id])
check("置底后面板顺序", panel_ids(), [lb.id, la.id, lc.id])
check("地图跟着换顺序", [l.id for l in win.map.layers], [lc.id, la.id, lb.id])
check("当前图层跟着被拖的层走", win.current_layer, lc.id)

print("[4] 拖拽交互")
vp = lst.viewport()


def send(kind, pos, button=Qt.NoButton, buttons=Qt.NoButton):
    ev = QMouseEvent(kind, QPointF(pos), QPointF(vp.mapToGlobal(pos)),
                     button, buttons, Qt.NoModifier)
    QApplication.sendEvent(vp, ev)


# 走窗口系统的真实命中测试：用户按住的是行里的文字标签，事件必须能冒泡到
# 列表（QLabel 一旦把按下事件吃掉，选中 / 双击改名 / 拖拽就全都失效了）。
wh = win.windowHandle()
if wh is not None:
    lbl = lst.itemWidget(lst.item(2)).name
    hit = lbl.mapTo(win, QPoint(lbl.width() // 2, lbl.height() // 2))
    lst._reset_drag()
    order_before_hit = list(w.layers)
    QTest.mousePress(wh, Qt.LeftButton, Qt.NoModifier, hit)
    app.processEvents()
    check("点在行内文字上，列表也能收到按下", lst._press_row, 2)
    QTest.mouseRelease(wh, Qt.LeftButton, Qt.NoModifier, hit)
    app.processEvents()
    check("原地松手不改顺序", list(w.layers), order_before_hit)
    renamed = []
    orig_rename = win.rename_layer
    win.rename_layer = lambda lid: renamed.append(lid)
    try:
        QTest.mouseDClick(wh, Qt.LeftButton, Qt.NoModifier, hit)
        app.processEvents()
    finally:
        win.rename_layer = orig_rename
    check("双击图层行仍然能改名", renamed, [lst.item(2).data(Qt.UserRole)])
    lst._reset_drag()
else:
    print("  跳过：没有 windowHandle，测不了真实命中测试")


def row_center(i):
    return lst.visualItemRect(lst.item(i)).center()


def drop_to_bottom(src_row):
    """按住第 src_row 行拖到最后一行下面再松手。"""
    drag_id = lst.item(src_row).data(Qt.UserRole)
    start = row_center(src_row)
    send(QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton)
    check("按下时不显示提示线", lst._line.isVisible(), False)
    send(QEvent.MouseMove, start + QPoint(0, 30), Qt.NoButton, Qt.LeftButton)
    check("移动超过阈值进入拖拽态", lst._dragging, True)
    check("拖动时显示插入提示线", lst._line.isVisible(), True)
    end = row_center(lst.count() - 1) + QPoint(0, 24)
    send(QEvent.MouseMove, end, Qt.NoButton, Qt.LeftButton)
    send(QEvent.MouseButtonRelease, end, Qt.LeftButton, Qt.NoButton)
    for _ in range(3):
        app.processEvents()
    check("松手后提示线收起", lst._line.isVisible(), False)
    return drag_id


drag_id = drop_to_bottom(0)
rest = [x for x in panel_ids() if x != drag_id]
check("拖到最底下：面板顺序", panel_ids(), rest + [drag_id])
check("拖到最底下：Store 顺序", list(w.layers), list(reversed(rest + [drag_id])))
check("拖完选中的还是被拖的那层", win.current_layer, drag_id)

# 点选：按下→原地松手，只换选中，不改顺序
keep = list(w.layers)
p = row_center(1)
target_id = lst.item(1).data(Qt.UserRole)
send(QEvent.MouseButtonPress, p, Qt.LeftButton, Qt.LeftButton)
send(QEvent.MouseButtonRelease, p + QPoint(2, 0), Qt.LeftButton, Qt.NoButton)
for _ in range(2):
    app.processEvents()
check("点选不改图层顺序", list(w.layers), keep)
check("点选选中该行图层", win.current_layer, target_id)

# 小抖动（小于拖拽阈值）也当点选
p = row_center(0)
send(QEvent.MouseButtonPress, p, Qt.LeftButton, Qt.LeftButton)
send(QEvent.MouseMove, p + QPoint(0, 3), Qt.NoButton, Qt.LeftButton)
check("小抖动不进入拖拽态", lst._dragging, False)
send(QEvent.MouseButtonRelease, p + QPoint(0, 3), Qt.LeftButton, Qt.NoButton)
for _ in range(2):
    app.processEvents()
check("小抖动不改顺序", list(w.layers), keep)

# 拖到最上面：插到第一行之前
top_id = lst.item(lst.count() - 1).data(Qt.UserRole)
send(QEvent.MouseButtonPress, row_center(lst.count() - 1), Qt.LeftButton, Qt.LeftButton)
send(QEvent.MouseMove, row_center(1), Qt.NoButton, Qt.LeftButton)
send(QEvent.MouseMove, QPoint(row_center(0).x(), max(1, row_center(0).y() - 10)),
     Qt.NoButton, Qt.LeftButton)
send(QEvent.MouseButtonRelease,
     QPoint(row_center(0).x(), max(1, row_center(0).y() - 10)),
     Qt.LeftButton, Qt.NoButton)
for _ in range(3):
    app.processEvents()
rest = [x for x in panel_ids() if x != top_id]
check("拖到最上面：面板第一行", panel_ids(), [top_id] + rest)
check("拖到最上面：Store 最后一项", list(w.layers)[-1], top_id)

# 自动滚动：图层多到一屏放不下时，拖到列表下边缘要自己往下滚
lst.setFixedHeight(80)
for _ in range(3):
    app.processEvents()
check("列表变矮后出现滚动条", lst.verticalScrollBar().maximum() > 0, True)
lst._dragging = True
lst._last_pos = QPoint(10, lst.viewport().height() - 2)
v0 = lst.verticalScrollBar().value()
lst._auto_scroll()
check("拖到下边缘自动向下滚动", lst.verticalScrollBar().value() > v0, True)
lst._reset_drag()
lst.setMinimumHeight(0)
lst.setMaximumHeight(16777215)
for _ in range(3):
    app.processEvents()

print("[5] 右键菜单「顺序」")
import native.app as A  # noqa: E402


class _FakeMenu:
    def __init__(self, *a, **k):
        self.items = []

    def addAction(self, label, *_a, **_k):
        self.items.append(label)

    def addMenu(self, label):
        self.items.append(label)
        return _FakeMenu()

    def addSeparator(self):
        pass

    def exec(self, *_a, **_k):
        captured["items"] = list(self.items)
        return None


captured: dict = {}
_orig_menu = A.QMenu
A.QMenu = _FakeMenu
try:
    win._layer_menu(lst.visualItemRect(lst.item(0)).center())
finally:
    A.QMenu = _orig_menu
check("菜单里有「顺序」子菜单", "顺序" in captured.get("items", []), True)

ids = panel_ids()
win.move_layer(ids[0], "down")
check("下移一层：面板顺序", panel_ids(), [ids[1], ids[0]] + ids[2:])
win.move_layer(ids[0], "top")
check("置顶：面板顺序", panel_ids(), ids[:3])
win.move_layer(ids[0], "bottom")
check("置底：面板最后一行", panel_ids()[-1], ids[0])
check("置底：Store 第一项", list(w.layers)[0], ids[0])
check("移动后当前图层不变", win.current_layer, ids[0])

print("[6] 合成顺序：后画的压在上面")
def solid(color, w=40, h=30):
    img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    img.fill(QColor(color))
    return img


red, blue = solid("#ff0000"), solid("#0000ff")
view = (1.0, 0.0, 0.0, 40, 30)


def compose(items):
    return _compose_job_image(_ComposeJob(0, (), view, 1.0, 40, 30, items, "force"))


img = compose([(False, red, None), (True, blue, None)])
check("栅格在上：盖住下面的矢量", img.pixelColor(20, 15).name(), "#0000ff")
img = compose([(True, blue, None), (False, red, None)])
check("矢量在上：盖住下面的栅格", img.pixelColor(20, 15).name(), "#ff0000")
check("空合成帧保持透明", compose([]).pixelColor(20, 15).alpha(), 0)

# _compose_async 的合成项顺序 = 图层顺序（栅格和矢量混在同一个列表里）
mv = win.map


class _L:
    def __init__(self, lid, raster=False):
        self.id, self.is_raster, self.op = lid, raster, ""


ras, vec = _L("R1", True), _L("V1", False)
saved_layers = list(mv.layers)
saved_visible = dict(mv.visible)
saved_rimgs = dict(mv._raster_imgs)
saved_limgs = dict(mv._layer_imgs)
saved_lviews = dict(mv._layer_views)
saved_submit = mv._compose_worker.submit
got: dict = {}
mv._compose_worker.submit = lambda job: got.__setitem__("job", job)
mv._raster.view_of = lambda lid: None
try:
    mv.visible = {ras.id: True, vec.id: True}
    mv._raster_imgs = {ras.id: blue}
    mv._layer_imgs = {vec.id: red}
    mv._layer_views = {vec.id: None}
    sig = mv._scene_sig()
    v = (mv.scale, mv.tx, mv.ty, mv.width(), mv.height())
    mv.layers = [ras, vec]
    mv._compose_async(sig, v, mv._epoch, "force")
    check("栅格在下时合成项顺序", [x[0] for x in got["job"].items], [True, False])
    mv.layers = [vec, ras]
    mv._compose_async(sig, v, mv._epoch, "force")
    check("栅格拖到上面后合成项顺序翻转", [x[0] for x in got["job"].items], [False, True])
finally:
    mv.layers = saved_layers
    mv.visible.clear()
    mv.visible.update(saved_visible)
    mv._raster_imgs.clear()
    mv._raster_imgs.update(saved_rimgs)
    mv._layer_imgs.clear()
    mv._layer_imgs.update(saved_limgs)
    mv._layer_views.clear()
    mv._layer_views.update(saved_lviews)
    mv._compose_worker.submit = saved_submit
    mv._raster.__dict__.pop("view_of", None)

print("[6b] 端到端：改顺序后地图真的按新顺序重画")
from app.layer_style import default_vector_style  # noqa: E402

w.clear(preserve_cache=True)
big = w.add(gpd.GeoDataFrame({"n": [1]}, geometry=[box(0, 0, 10, 10)],
                             crs="EPSG:4326"), "底层红")
small = w.add(gpd.GeoDataFrame({"n": [1]}, geometry=[box(2, 2, 8, 8)],
                               crs="EPSG:4326"), "上层蓝")
for lay, color in ((big, "#ff0000"), (small, "#0000ff")):
    st = default_vector_style("", color, "Polygon")
    st.update({"fill_color": color, "fill_opacity": 100,
               "stroke_width": 0, "stroke_opacity": 0})
    lay.style = st
win.refresh_layers()
win.map.fit_all()
settle_frame()
cw, ch = win.map.width(), win.map.height()
check("默认顺序：上层蓝盖住红", win.map._frame.toImage().pixelColor(cw // 2, ch // 2).name(),
      "#0000ff")

win.reorder_layer(small.id, 1)          # 把蓝层拖到红层下面
keys_before = dict(win.map._img_keys)
settle_frame()
check("蓝层拖到底下后地图显示红", win.map._frame.toImage().pixelColor(cw // 2, ch // 2).name(),
      "#ff0000")
check("换序复用图层缓存，不重绘图层",
      dict(win.map._img_keys) == keys_before, True)

win.reorder_layer(small.id, 0)          # 再拖回上面
settle_frame()
check("再拖回最上面后地图又是蓝", win.map._frame.toImage().pixelColor(cw // 2, ch // 2).name(),
      "#0000ff")

print("[6c] 导入的栅格默认垫在矢量下面")
from app.raster import read_info  # noqa: E402
from native.app import _place_raster_bottom  # noqa: E402

tif = ROOT / "samples" / "raster" / "image.tif"
if tif.exists():
    w.clear(preserve_cache=True)
    v1 = w.add(gdf_poly(), "矢量1")
    ras = w.add_raster(read_info(str(tif)), "影像")
    _place_raster_bottom(w, ras)
    check("单张栅格：垫在矢量下面", list(w.layers), [ras.id, v1.id])
    v2 = w.add(gdf_poly(), "矢量2")
    check("再导入矢量：仍在栅格上面", list(w.layers), [ras.id, v1.id, v2.id])
    ras2 = w.add_raster(read_info(str(tif)), "影像2")
    _place_raster_bottom(w, ras2)
    check("第二张栅格：压在第一张上、仍在矢量下",
          list(w.layers), [ras.id, ras2.id, v1.id, v2.id])
else:
    print("  跳过：没有 samples/raster/image.tif")

print("[7] 旧会话迁移（背景算子挪到底部）")
old = [{"id": "L1", "name": "地块", "op": ""},
       {"id": "L2", "name": "道路", "op": ""},
       {"id": "L3", "name": "缓冲区", "op": "buffer"}]
check("旧会话：背景算子挪到最底部", [d["id"] for d in _migrate_layer_order(old, None)],
      ["L3", "L1", "L2"])
check("旧会话：多个背景算子保持原相对顺序",
      [d["id"] for d in _migrate_layer_order(
          [{"id": "A", "op": ""}, {"id": "B", "op": "grid"}, {"id": "C", "op": "buffer"}],
          "1")], ["B", "C", "A"])
check("新会话（layer_order=2）原样不动",
      [d["id"] for d in _migrate_layer_order(old, 2)], ["L1", "L2", "L3"])
check("没有需要迁移的图层时不动顺序",
      [d["id"] for d in _migrate_layer_order(old[:2], None)], ["L1", "L2"])
mixed = [{"id": "V1", "op": ""}, {"id": "R1", "kind": "raster"},
         {"id": "B1", "op": "buffer"}, {"id": "V2", "op": ""}]
check("旧会话：栅格垫底、背景算子其次、普通矢量在上",
      [d["id"] for d in _migrate_layer_order(mixed, None)], ["R1", "B1", "V1", "V2"])

print("[8] 会话持久化：顺序重启后还在")
win._persist_state(force_cache=True)
order_before = [l.id for l in win.store.layers.values()]
names_before = [l.name for l in win.store.layers.values()]
sid = win.session_id
win.close()
for _ in range(3):
    app.processEvents()

w2 = MainWindow()
for _ in range(5):
    app.processEvents()
check("重启后复用同一会话", w2.session_id, sid)
check("重启后图层数一致", len(w2.store.layers), len(order_before))
check("重启后图层顺序一致", [l.id for l in w2.store.layers.values()], order_before)
check("重启后图层名一致", [l.name for l in w2.store.layers.values()], names_before)
w2.close()
for _ in range(3):
    app.processEvents()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)



