"""图层样式 / 要素标注的回归测试（不联网，全部离屏）。

覆盖：
· 默认样式和改造前的硬编码一致（软算子 4%/26% 等），规范化会夹范围、丢坏键
· `_paint_layer` 真的按样式画：填充色/不透明度、线色/线宽、点色/点大小
· 样式弹窗：按几何类型给控件、取值回写、预览能画出标注样例、栅格页
· 主窗口右键菜单有「图层样式…」，应用后样式写回图层并进会话描述
· 标注取数：内存图层按视口筛、provider 图层按 bbox 流式取（GPKG）
· 地图上真的画出标注；清空字段后标注缓存作废

用法：python tools/test_layer_style.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geopandas as gpd
from shapely.geometry import Point, box
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication, QDialog

fails = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else want(got)
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


app = QApplication.instance() or QApplication(sys.argv)

from app.engine import Layer
from app.layer_style import (default_vector_style, geom_kind, normalize_raster_style,
                             normalize_vector_style, resolve_vector_style,
                             style_signature)
from app.raster import RasterInfo, RasterLayer

print("[1] 默认样式与规范化")
st = default_vector_style("buffer", "#123456", "Polygon")
check("软算子填充 4%", st["fill_opacity"], 4)
check("软算子描边 26%", st["stroke_opacity"], 26)
check("默认颜色跟图层主色", st["fill_color"], "#123456")
check("普通面填充 14%", default_vector_style("", "#fff", "Polygon")["fill_opacity"], 14)
check("融合面填充 8%", default_vector_style("dissolve", "#fff", "Polygon")["fill_opacity"], 8)
check("线图层描边默认不透明", default_vector_style("", "#fff", "LineString")["stroke_opacity"], 100)
check("几何类型归类·线", geom_kind("MultiLineString"), "line")
check("几何类型归类·面", geom_kind("MultiPolygon"), "polygon")
check("几何类型归类·点", geom_kind("Point"), "point")

bad = normalize_vector_style({"fill_color": "red", "fill_opacity": 250,
                              "stroke_width": 0.1, "label_size": 99, "bogus": 1},
                             "", "#2f6fed", "Polygon")
check("坏颜色回退", bad["fill_color"], "#2f6fed")
check("不透明度夹到 100", bad["fill_opacity"], 100)
check("线宽夹到 0.5", bad["stroke_width"], 0.5)
check("字号夹到 24", bad["label_size"], 24)
check("不认识的键被丢掉", "bogus" in bad, False)
check("签名可哈希且稳定", style_signature({"a": 1, "b": 2}) == style_signature({"b": 2, "a": 1}), True)
check("覆盖值生效", resolve_vector_style("", "#111111", {"fill_color": "#abcdef"})["fill_color"],
      "#abcdef")

rs = normalize_raster_style({"opacity": 3, "stretch": "weird", "gamma": 9}, {"bands": [1, 2, 3]})
check("栅格透明度夹到 1", rs["opacity"], 1.0)
check("栅格拉伸回退", rs["stretch"], "percentile")
check("栅格 gamma 夹到 3", rs["gamma"], 3.0)
check("栅格其它键保留", rs["bands"], [1, 2, 3])

print("[2] 渲染函数按样式画")
from native.mapview import _paint_layer


def _img(w=60, h=60):
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    return img


def _poly_path():
    path = QPainterPath()
    path.addRect(0.2, 0.2, 0.6, 0.6)
    return path


img = _img()
p = QPainter(img)
p.scale(60, 60)
_paint_layer(p, {
    "op": "", "color": "#2f6fed", "geom": "Polygon",
    "style": {"fill_color": "#ff0000", "fill_opacity": 100,
              "stroke_color": "#00ff00", "stroke_opacity": 100, "stroke_width": 1},
    "poly_paths": [_poly_path()], "npoly": 1,
    "poly_bounds": [(0.2, 0.2, 0.8, 0.8)], "npoly_b": 1,
    "line_paths": [], "nline": 0, "line_bounds": [], "nline_b": 0,
    "points": [], "npt": 0, "point_bounds": [], "npt_b": 0, "dots": None,
}, (0, 0, 1, 1), 60.0, 0.0)
p.end()
c = img.pixelColor(30, 30)
check("面填充用设定颜色", (c.red(), c.green(), c.blue()), (255, 0, 0))

img = _img()
p = QPainter(img)
p.scale(60, 60)
line = QPainterPath()
line.moveTo(0.1, 0.5)
line.lineTo(0.9, 0.5)
_paint_layer(p, {
    "op": "", "color": "#2f6fed", "geom": "LineString",
    "style": {"stroke_color": "#0000ff", "stroke_opacity": 100, "stroke_width": 5},
    "poly_paths": [], "npoly": 0, "poly_bounds": [], "npoly_b": 0,
    "line_paths": [line], "nline": 1,
    "line_bounds": [(0.1, 0.5, 0.9, 0.5)], "nline_b": 1,
    "points": [], "npt": 0, "point_bounds": [], "npt_b": 0, "dots": None,
}, (0, 0, 1, 1), 60.0, 0.0)
p.end()
blue = sum(1 for x in range(60) for y in range(60)
           if img.pixelColor(x, y).blue() > 200 and img.pixelColor(x, y).red() < 80)
check("线宽 5px 画出更粗的线", blue > 150, True)

img = _img()
p = QPainter(img)
p.scale(60, 60)
_paint_layer(p, {
    "op": "", "color": "#2f6fed", "geom": "Point",
    "style": {"point_color": "#00ff00", "point_size": 14},
    "poly_paths": [], "npoly": 0, "poly_bounds": [], "npoly_b": 0,
    "line_paths": [], "nline": 0, "line_bounds": [], "nline_b": 0,
    "points": [(0.5, 0.5)], "npt": 1,
    "point_bounds": [(0.48, 0.48, 0.52, 0.52)], "npt_b": 1, "dots": None,
}, (0, 0, 1, 1), 60.0, 0.0)
p.end()
c = img.pixelColor(30, 30)
check("点符号用设定颜色", (c.red(), c.green(), c.blue()), (0, 255, 0))

print("[3] 样式弹窗")
from native.style_dialog import LayerStyleDialog

gdf = gpd.GeoDataFrame(
    {"name": ["滨江地块", "老城地块"], "pop": [1200, 800],
     "geometry": [box(0, 0, 1, 1), box(1, 1, 2, 2)]},
    crs="EPSG:4326")
lay = Layer("L1", "测试面", gdf=gdf)
lay.style = {"fill_color": "#d97757", "label_field": "name"}
dlg = LayerStyleDialog(lay)
check("识别为面图层", dlg._kind, "polygon")
check("字段下拉带出属性列", [dlg._w["label_field"].itemData(i)
                        for i in range(dlg._w["label_field"].count())], ["", "name", "pop"])
check("初值来自图层样式", dlg._colors["fill_color"], "#d97757")
dlg._w["fill_opacity"].setValue(60)
dlg._w["stroke_width"].setValue(3.0)
dlg._w["label_field"].setCurrentIndex(dlg._w["label_field"].findData("name"))
dlg._w["label_size"].setValue(18)
dlg._w["label_halo"].setChecked(False)
out = dlg.style()
check("弹窗取值·填充不透明度", out["fill_opacity"], 60)
check("弹窗取值·线宽", out["stroke_width"], 3.0)
check("弹窗取值·标注字段", out["label_field"], "name")
check("弹窗取值·字号", out["label_size"], 18)
check("弹窗取值·描边开关", out["label_halo"], False)
check("预览取样用字段值", dlg.sample_text(), "滨江地块")
prev = dlg.preview.grab().toImage()
dark = sum(1 for y in range(0, prev.height(), 2) for x in range(0, prev.width(), 2)
           if prev.pixelColor(x, y).red() < 90 and prev.pixelColor(x, y).green() < 90)
check("预览画出了文字", dark > 5, True)
dlg.close()

rinfo = RasterInfo(path=str(Path(tempfile.gettempdir()) / "nope.tif"), width=64, height=64,
                   bands=3, dtypes=["uint8"], crs="EPSG:4326", crs_name="WGS 84",
                   nodata=None, bounds=(120.0, 30.0, 120.1, 30.1), res=(0.001, 0.001),
                   band_desc=["", "", ""], driver="GTiff", overviews=())
rlay = RasterLayer("R1", "测试栅格", rinfo)
rdlg = LayerStyleDialog(rlay)
check("栅格弹窗不显示标注字段", "label_field" in rdlg._w, False)
rdlg._w["opacity"].setValue(45)
rdlg._w["stretch"].setCurrentIndex(rdlg._w["stretch"].findData("minmax"))
rst = rdlg.style()
check("栅格弹窗·不透明度", rst["opacity"], 0.45)
check("栅格弹窗·拉伸方式", rst["stretch"], "minmax")
rdlg.close()

print("[4] 主窗口右键菜单与应用")
from native.app import MainWindow

win = MainWindow()
win.resize(1200, 800)
test_layer = win.store.add(gdf, "样式回归层")
win.refresh_layers()

# 右键菜单：把 QMenu 换成假对象，拿到菜单项文本
import native.app as A


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


captured = {}
_orig_menu = A.QMenu
A.QMenu = _FakeMenu
try:
    item = win.layer_list.item(0)
    win._layer_menu(win.layer_list.visualItemRect(item).center())
finally:
    A.QMenu = _orig_menu
check("右键菜单含「图层样式…」", "图层样式…" in captured.get("items", []), True)


class _FakeStyleDlg:
    def __init__(self, layer, parent=None):
        self.layer = layer

    def exec(self):
        return QDialog.Accepted

    def style(self):
        return {"fill_color": "#00aa88", "fill_opacity": 33,
                "label_field": "name", "label_size": 13}


_orig_dlg = A.LayerStyleDialog
A.LayerStyleDialog = _FakeStyleDlg
try:
    win.open_layer_style(test_layer.id)
finally:
    A.LayerStyleDialog = _orig_dlg
check("样式写回图层", test_layer.style.get("fill_color"), "#00aa88")
check("填充不透明度写回", test_layer.style.get("fill_opacity"), 33)
check("标注字段写回", test_layer.style.get("label_field"), "name")
check("图例主色跟着走", test_layer.color, "#00aa88")
desc = [d for d in win._layer_descriptors() if d["id"] == test_layer.id][0]
check("样式进会话描述", (desc.get("style") or {}).get("label_field"), "name")

r2 = win.store.add_raster(rinfo, "栅格样式层")
win.apply_layer_style(r2.id, {"opacity": 0.5, "stretch": "minmax", "gamma": 1.4})
check("栅格透明度写回", r2.style.get("opacity"), 0.5)
check("栅格里伸写回", r2.style.get("stretch"), "minmax")
check("栅格 rev 递增", r2.rev >= 1, True)
check("栅格波段配置没被冲掉", r2.style.get("bands"), [1, 2, 3])

print("[5] 标注取数")
from native.labels import LABEL_CAP, build_label_data

xy, texts, angles = build_label_data(test_layer, "name", (-1.0, -1.0, 3.0, 3.0))
check("内存图层标注取到全部要素", sorted(texts), sorted(["滨江地块", "老城地块"]))
check("标注锚点是世界坐标", bool(xy is not None and 0.0 <= float(xy[0][0]) <= 1.0), True)
check("面要素标注不旋转", [float(a) for a in angles] if angles is not None else [], [0.0, 0.0])
xy2, texts2, _a2 = build_label_data(test_layer, "name", (10.0, 10.0, 11.0, 11.0))
check("视野外取不到标注", texts2, [])
check("空字段不取数", build_label_data(test_layer, "", (-1.0, -1.0, 3.0, 3.0))[1], [])
check("默认候选上限足够铺满屏幕", LABEL_CAP >= 800, True)

# 空间分层：5000 个点分 5 簇横跨视野，cap=50 时每簇都该有代表，不能只取到文件顺序靠前的簇
import numpy as np
from shapely.geometry import LineString as _Line
from shapely.geometry import Point as _Point

pts = []
for k in range(5):
    pts.extend([_Point(0.5 + k * 1.0 + (i % 10) * 0.01, 0.5 + (i // 10) * 0.01)
                for i in range(1000)])
spread_gdf = gpd.GeoDataFrame({"name": [f"点{i}" for i in range(len(pts))],
                               "geometry": pts}, crs="EPSG:4326")
spread_layer = Layer("L5", "五簇点", gdf=spread_gdf)
sxy, stexts, _sa = build_label_data(spread_layer, "name", (0.0, 0.0, 5.0, 1.0), cap=50)
check("分层取样不超过上限", len(stexts) <= 50, True)
check("分层取样覆盖全视野", bool(sxy is not None and float(np.ptp(sxy[:, 0])) > 0.008), True)

# 线要素：锚点取线中点、角度沿切线；角度归一化到 (-90, 90] 保证不倒着写
lines = gpd.GeoDataFrame(
    {"name": ["横路", "竖路", "斜路", "拐角路"],
     "geometry": [_Line([(120.00, 30.00), (120.10, 30.00)]),
                  _Line([(120.20, 30.00), (120.20, 30.10)]),
                  _Line([(120.30, 30.00), (120.40, 30.10)]),
                  # L 形：包围盒中心 (120.45,30.05) 不在线上，线中点是拐角 (120.50,30.00)
                  _Line([(120.40, 30.00), (120.50, 30.00), (120.50, 30.10)])]},
    crs="EPSG:4326")
line_layer = Layer("L6", "线标注", gdf=lines)
lxy, ltexts, lang = build_label_data(line_layer, "name", (119.9, 29.9, 120.7, 30.2))
by_text = {t: (float(xy[0]), float(xy[1]), float(a))
           for xy, t, a in zip(lxy, ltexts, lang)}
check("横线角度水平", round(by_text["横路"][2], 3), 0.0)
check("竖线角度归一到 90", round(by_text["竖路"][2], 3), 90.0)
check("斜线角度沿道路", round(abs(by_text["斜路"][2]), 1), 45.0)
check("角度不会倒着写", bool(-90.0 < by_text["拐角路"][2] <= 90.0), True)

# 横路：锚点是线中点 (120.05, 30.00)；L 形路：锚点是拐角，而不是包围盒中心
from native.labels import project_xy as _proj


def _world(lon, lat):
    return float(_proj(lon, lat)[0]), float(_proj(lon, lat)[1])


_flat = _world(120.05, 30.00)
_corner = _world(120.50, 30.00)
_box_center = _world(120.45, 30.05)
check("横线锚点取线中点",
      bool(abs(by_text["横路"][0] - _flat[0]) < 1e-9
           and abs(by_text["横路"][1] - _flat[1]) < 1e-9), True)
check("L 形线锚点落在线上（拐角）",
      bool(abs(by_text["拐角路"][0] - _corner[0]) < 1e-9
           and abs(by_text["拐角路"][1] - _corner[1]) < 1e-9), True)
check("L 形线锚点不是包围盒中心",
      bool(abs(by_text["拐角路"][1] - _box_center[1]) > 1e-6), True)

# provider 图层（GPKG 源文件，走 iter_bbox_attrs 的 bbox 流式读）
td = Path(tempfile.mkdtemp(prefix="gisstyle_"))
gpkg = td / "poi.gpkg"
poi = gpd.GeoDataFrame(
    {"name": [f"点{i}" for i in range(400)],
     "geometry": [Point(120.0 + i * 0.001, 30.0) for i in range(400)]},
    crs="EPSG:4326")
poi.to_file(gpkg, driver="GPKG", layer="poi")
from app.engine import Store

store = Store(cache_dir=td)
prov_layer = store.add_source(gpkg, "poi")
check("provider 图层判定", bool(prov_layer._gdf is None and prov_layer.provider is not None), True)
xy3, texts3, _a3 = build_label_data(prov_layer, "name", (119.0, 29.0, 121.0, 31.0))
check("provider 标注取到文本", bool(texts3 and texts3[0].startswith("点")), True)
check("provider 标注不超过默认上限", len(texts3) <= LABEL_CAP, True)
xy4, texts4, _a4 = build_label_data(prov_layer, "name", (119.0, 29.0, 121.0, 31.0), cap=25)
check("cap 参数生效", len(texts4) <= 25, True)
check("provider 视野外为空", build_label_data(prov_layer, "name", (0.0, 0.0, 0.5, 0.5))[1], [])

# provider 线图层：几何要跟着文本一起留下来，角度才不丢
road_gpkg = td / "road.gpkg"
road_gdf = gpd.GeoDataFrame(
    {"name": [f"斜路{i}" for i in range(20)],
     "geometry": [_Line([(120.0 + i * 0.01, 30.0), (120.05 + i * 0.01, 30.05)]) for i in range(20)]},
    crs="EPSG:4326")
road_gdf.to_file(road_gpkg, driver="GPKG", layer="road")
road_layer = store.add_source(road_gpkg, "road")
rxy, rtexts, rang = build_label_data(road_layer, "name", (119.0, 29.0, 122.0, 31.0))
check("provider 线标注取到文本", bool(rtexts), True)
check("provider 线标注带角度",
      bool(rang is not None and len(rang) == len(rtexts)
           and abs(float(rang[0]) - 45.0) < 0.5), True)

print("[6] 地图上画标注")
from native.mapview import MapView

mv = MapView()
mv.resize(640, 420)
mv.show()
map_layer = Layer("L9", "标注层", gdf=gdf)
map_layer.style = {"label_field": "name", "fill_color": "#d97757", "fill_opacity": 40}
rot_layer = Layer("L10", "斜线标注", gdf=lines)
rot_layer.style = {"label_field": "name", "stroke_color": "#2f6fed"}
mv.set_layers([map_layer, rot_layer])
mv.fit_all()
t0 = time.time()
while time.time() - t0 < 6:
    app.processEvents()
    if mv._frame is not None and mv._labels.get("L9") and mv._labels.get("L10"):
        break
    mv._rebuild_labels()
    time.sleep(0.02)
check("地图标注缓存有文本", sorted(mv._labels.get("L9", {}).get("texts") or []),
      sorted(["滨江地块", "老城地块"]))
_ang9 = mv._labels.get("L9", {}).get("angles")
check("面标注缓存角度为 0",
      bool(_ang9 is not None and all(abs(float(a)) < 1e-9 for a in _ang9)), True)
_ang10 = mv._labels.get("L10", {}).get("angles")
check("线标注缓存带旋转角",
      bool(_ang10 is not None and len(_ang10) == len(mv._labels["L10"]["texts"])
           and any(abs(float(a)) > 10.0 for a in _ang10)), True)
mv.grab()
img = mv.grab().toImage()
dark = sum(1 for y in range(img.height()) for x in range(img.width())
           if img.pixelColor(x, y).red() < 90 and img.pixelColor(x, y).green() < 90
           and img.pixelColor(x, y).blue() < 90)
check("标注真的画到了地图上", dark > 8, True)

# 缓存失效规则：同视野复用；放大到缓存范围的 1/1.6 以下必须重取
# （否则按大范围抽的锚点在新视野里寥寥无几，看起来就是「标注丢了」）
view = mv._view_lonlat_rect()
cache_rect = mv._pad_region(view, 1.4)
mv._labels["L9"] = {"field": "name", "rect": cache_rect,
                    "sig": mv._label_sig(map_layer), "xy": None, "texts": []}
check("同视野标注复用缓存", mv._label_cache_stale(map_layer, "name", view), False)
cx, cy = (view[0] + view[2]) / 2, (view[1] + view[3]) / 2
zw, zh = (view[2] - view[0]) / 6, (view[3] - view[1]) / 6
check("放大后标注缓存失效",
      mv._label_cache_stale(map_layer, "name", (cx - zw, cy - zh, cx + zw, cy + zh)), True)
mv._labels.pop("L9", None)

map_layer.style = {"label_field": ""}
mv.refresh_layer_style("L9")
check("清空字段后标注缓存作废", "L9" in mv._labels, False)
mv.shutdown()

print("[7] 源文件图层的宽视野粗路径缓存")
import native.mapview as _mv_mod
from native.mapview import LOD_MAX_VERTS, LOD_MIN_MPP, LOD_MIN_FEATURES, LOD_TOL
from native.vectorstream import build_lod_paths

# 600 条对角线（GPKG 源文件，无空间索引 → 走 provider 流式读）
lod_lines = gpd.GeoDataFrame(
    {"name": [f"斜路{i}" for i in range(600)],
     "geometry": [_Line([(120.0 + i * 0.002, 30.0), (120.0 + i * 0.002, 30.002)])
                  for i in range(600)]},
    crs="EPSG:4326")
lod_gpkg = td / "lod.gpkg"
lod_lines.to_file(lod_gpkg, driver="GPKG", layer="lod")
lod_store = Store(cache_dir=td)
lod_layer = lod_store.add_source(lod_gpkg, "lod")

lod_mv = MapView()
lod_mv.resize(420, 300)
lod_mv.show()
submitted = []
lod_mv._lod_worker.submit = lambda job: submitted.append(job)   # 测试里不真跑后台线程
_mv_old_min = _mv_mod.LOD_MIN_FEATURES
_mv_mod.LOD_MIN_FEATURES = 100          # 小样本也能触发
lod_mv.set_layers([lod_layer])
_mv_mod.LOD_MIN_FEATURES = _mv_old_min
check("大线图层排队建缓存", len(submitted), 1)
c7 = lod_mv._cache[lod_layer.id]
check("缓存状态标为 building", c7.lod_state, "building")


pt_gpkg = td / "pt.gpkg"
gpd.GeoDataFrame({"name": ["a"], "geometry": [Point(120.0, 30.0)]},
                 crs="EPSG:4326").to_file(pt_gpkg, driver="GPKG", layer="pt")
pt_layer = lod_store.add_source(pt_gpkg, "pt")
c_pt = _mv_mod._LayerCache()
c_pt.stream = True
c_pt.provider = pt_layer.provider
c_pt.total = 999999
c_pt.extent = (120.0, 30.0, 121.0, 31.0)
_before = len(submitted)
lod_mv._maybe_start_lod(pt_layer, c_pt)
check("点图层不建路径缓存", (c_pt.lod_state, len(submitted)), ("", _before))

# 直接建一次整层粗路径，装进图层缓存（等价于后台线程交回结果）
lod_prov = lod_layer.provider
ext7 = tuple(lod_prov.wgs_bounds)
pad7 = LOD_TOL * 2.0
rect7 = (ext7[0] - pad7, ext7[1] - pad7, ext7[2] + pad7, ext7[3] + pad7)
payload7 = build_lod_paths(lod_prov.iter_bbox(rect7, batch_size=256, columns=[]),
                           LOD_TOL, max_verts=LOD_MAX_VERTS)
check("粗路径有线和面", bool(payload7 and payload7[2] and not payload7[0]), True)
lod_mv._on_lod_ready(lod_layer.id, payload7, rect7, LOD_TOL, lod_prov)
check("缓存就绪", lod_mv._cache[lod_layer.id].lod_state, "ready")
check("缓存是一整套渲染集", len(lod_mv._cache[lod_layer.id].sets), 1)

lod_mv.fit_all()
for _ in range(50):
    app.processEvents()
    time.sleep(0.002)
wide_mpp = lod_mv.mpp()
check("fit_all 是宽视野", wide_mpp >= LOD_MIN_MPP, True)
s7 = lod_mv._lod_set(lod_mv._cache[lod_layer.id])
check("宽视野命中粗路径缓存", bool(s7 is not None and s7.line_paths), True)
snap7 = lod_mv._make_snapshot()
entry7 = [x for x in (snap7 or {}).get("layers", []) if x["lid"] == lod_layer.id]
check("宽视野快照直接用渲染集", bool(entry7 and not entry7[0].get("stream")
                                     and entry7[0]["nline"] > 0), True)
img7 = _img(lod_mv.width(), lod_mv.height())
p7 = QPainter(img7)
p7.setRenderHint(QPainter.Antialiasing, True)
p7.translate(lod_mv.tx, lod_mv.ty)
p7.scale(lod_mv.scale, lod_mv.scale)
from native.mapview import _paint_layer as _pl7
_lod_entry = dict(entry7[0])
_lod_entry["points"] = []
_lod_entry["point_bounds"] = []
_lod_entry["dots"] = None
_pl7(p7, _lod_entry, lod_mv._view_world_rect(), lod_mv.scale, wide_mpp)
p7.end()
_lod_px = sum(1 for y in range(img7.height()) for x in range(img7.width())
              if img7.pixelColor(x, y).alpha() > 20)
check("粗路径缓存真的画出了线", _lod_px > 30, True)

# 放大到城市级：缓存太粗，必须退回流式读窗口
lod_mv.zoom_at(30.0, 210, 150)
for _ in range(50):
    app.processEvents()
    time.sleep(0.002)
check("城市级不再用粗缓存", lod_mv._lod_set(lod_mv._cache[lod_layer.id]), None)
snap8 = lod_mv._make_snapshot()
entry8 = [x for x in (snap8 or {}).get("layers", []) if x["lid"] == lod_layer.id]
check("城市级快照回到流式", bool(entry8 and entry8[0].get("stream")), True)

# 过期/错配的结果要丢掉：provider 身份不一致就不收
lod_mv._on_lod_ready(lod_layer.id, payload7, rect7, LOD_TOL, object())
check("换源后旧结果被丢弃", len(lod_mv._cache[lod_layer.id].sets), 1)

# 渲染中不抢同一个源文件：vec_busy 时标注不重取
lod_mv._vec_busy = True
lod_mv._labels.pop(lod_layer.id, None)
lod_mv._labels_pending.discard(lod_layer.id)
lod_mv._ensure_labels(lod_layer, {"label_field": "name"}, lod_mv._view_lonlat_rect())
check("渲染中不排队标注取数", lod_layer.id in lod_mv._labels_pending, False)
lod_mv._vec_busy = False
lod_mv._ensure_labels(lod_layer, {"label_field": "name"}, lod_mv._view_lonlat_rect())
check("渲染结束后恢复标注取数", lod_layer.id in lod_mv._labels_pending, True)

lod_mv.shutdown()

print("[8] 整层锚点表：源文件图层的标注不再每次换视野重读")
from native.labels import LABEL_TABLE_MAX as _LTM8
from native.labels import build_label_data as _bld8
from native.labels import build_label_table as _blt8

_anchor_gj = td / "anchor.geojson"
gpd.GeoDataFrame(
    {"name": [f"村{i}" for i in range(300)],
     "geometry": [Point(120.0 + (i % 20) * 0.01, 30.0 + (i // 20) * 0.01)
                  for i in range(300)]},
    crs="EPSG:4326").to_file(_anchor_gj, driver="GeoJSON")
anchor_layer = lod_store.add_source(_anchor_gj, "anchor")
anchor_layer.style = {"label_field": "name"}

amv = MapView()
amv.resize(420, 300)
amv.show()
submitted8 = []
amv._label_worker.submit = lambda job: submitted8.append(job)
amv.set_layers([anchor_layer])
check("配了标注的源文件图层预建锚点表",
      [j.kind for j in submitted8], ["table"])
check("GeoJSON 可建表", amv._label_table_eligible(anchor_layer), True)
check("GPKG 有空间索引，不建表", amv._label_table_eligible(lod_layer), False)

axy, atexts, aang, arect = _blt8(anchor_layer, "name")
check("整层锚点表取出全部要素", len(atexts), 300)
check("表格上限大于样本", bool(_LTM8 > 300), True)
amv._on_labels_ready(anchor_layer.id, amv._label_tables[anchor_layer.id],
                     "table", ("name", arect, axy, atexts, aang))
check("锚点表已缓存", bool(amv._labels_anchors.get(anchor_layer.id)), True)
check("表就绪后清掉在建标记", anchor_layer.id in amv._label_tables, False)

amv.fit_all()
for _ in range(30):
    app.processEvents()
    time.sleep(0.002)
c8 = amv._ensure_labels(anchor_layer, {"label_field": "name"},
                        amv._view_lonlat_rect())
check("整层表生成视口标注", bool(c8 and len(c8["texts"]) > 50), True)
n_sub8 = len(submitted8)
amv.tx -= 40
amv._invalidate_frame()
amv.update()
for _ in range(30):
    app.processEvents()
    time.sleep(0.002)
c8b = amv._ensure_labels(anchor_layer, {"label_field": "name"},
                         amv._view_lonlat_rect())
check("平移后标注直接现算，不再提交取数",
      (bool(c8b and c8b["texts"]), len(submitted8)), (True, n_sub8))
check("平移后换成新视野的标注", c8b["rect"] != c8["rect"], True)

_empty8 = _bld8(anchor_layer, "name", amv._view_lonlat_rect(),
                cancelled=lambda: True)
check("取数可被新视野取消", _empty8[1], [])

amv.refresh_layer_style(anchor_layer.id)
check("改样式后锚点表作废", anchor_layer.id in amv._labels_anchors, False)
amv.shutdown()

win.close()


print()
if fails:
    print(f"失败 {len(fails)} 项：" + ", ".join(fails))
    raise SystemExit(1)
print("全部通过")
