"""点选图层的回归测试：在左栏点一行**只**切换当前图层，不能动地图视野。

用户点名要求去掉「点一下就缩放到图层范围」：点选是为了看属性 / 接着操作，
视野被拽走会打断手头的活。缩放到图层保留在右键菜单和定位按钮上。

用法：python tools/test_layer_select.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import geopandas as gpd
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from shapely.geometry import Point

from native.app import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.resize(1100, 780)
win.show()
app.processEvents()

g1 = gpd.GeoDataFrame({"名称": ["A", "B"]},
                      geometry=[Point(104.0, 30.0), Point(104.1, 30.1)], crs=4326)
g2 = gpd.GeoDataFrame({"名称": ["C"]}, geometry=[Point(112.0, 34.0)], crs=4326)
near = win.store.add(g1, "近处")
far = win.store.add(g2, "远处")
win.refresh_layers()
app.processEvents()

mv = win.map
mv.center = (104.05, 30.05)
mv.scale = 1234.0
before = (mv.center, mv.scale)


def row_of(layer_id):
    for i in range(win.layer_list.count()):
        item = win.layer_list.item(i)
        if item.data(Qt.UserRole) == layer_id:
            return item
    return None


item = row_of(far.id)
win.layer_list.setCurrentItem(item)
app.processEvents()
after = (mv.center, mv.scale)
ok_select = before == after and win.current_layer == far.id
print(f"选中「远处」：视野 {before} → {after}，当前图层={win.current_layer} "
      f"→ {'未缩放 OK' if ok_select else '被缩放了 !!'}")

# 再点回第一层：同样不该缩放
win.layer_list.setCurrentItem(row_of(near.id))
app.processEvents()
after2 = (mv.center, mv.scale)
ok_second = after2 == before and win.current_layer == near.id
print(f"选中「近处」：视野 {after2} → {'未缩放 OK' if ok_second else '被缩放了 !!'}")

# 缩放到图层本身还在（右键菜单 / 定位按钮走的就是它）
mv.fit_layer(far.id)
app.processEvents()
moved = (mv.center, mv.scale) != before
print(f"fit_layer('远处') 仍可缩放：{moved} → {'OK' if moved else '功能丢了 !!'}")

ok = ok_select and ok_second and moved
print("\n结论:", "PASS" if ok else "FAIL")
win.close()
sys.exit(0 if ok else 1)
