"""显隐按钮的回归测试：点复选框 → 地图必须真的变（隐藏全关时画面要清空）。

判定用「像素指纹」而不是猜颜色：
1. 隐藏某一层 → 画面必须变化；
2. 全部隐藏 → 画面必须只剩背景（着色像素 ≈ 0）；
3. 全部恢复 → 画面必须与最初**逐像素一致**（渲染是确定性的，缓存命中时应当完全一样）。

用法：python tools/test_visible.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from native.app import MainWindow

BASE = r"D:\gis-agent\data\uploads\2415b1ef\x_zhejiang-lat"
FILES = ["gis_osm_landuse_a_free_1.shp", "gis_osm_water_a_free_1.shp"]

app = QApplication(sys.argv)
win = MainWindow()
win.resize(1280, 800)
win.show()
win.load_paths([os.path.join(BASE, f) for f in FILES])


def settle(max_wait=90.0):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < max_wait:
        app.processEvents()
        if len(win.map.layers) >= len(FILES) and not win.map._has_pending():
            break
        time.sleep(0.01)
    t_end = time.perf_counter() + 30
    while time.perf_counter() < t_end:
        app.processEvents()
        if win.map._frame is not None and win.map._frame_sig == win.map._scene_sig():
            break
        time.sleep(0.01)


def grab():
    """整帧像素（合成到白底上，避免透明区域干扰比较）。"""
    src = win.map._frame.toImage()
    img = QImage(src.width(), src.height(), QImage.Format_RGB32)
    img.fill(0xFFFFFFFF)
    from PySide6.QtGui import QPainter
    p = QPainter(img)
    p.drawImage(0, 0, src)
    p.end()
    a = np.frombuffer(img.constBits(), count=img.sizeInBytes(), dtype=np.uint8)
    return a.reshape(img.height(), img.bytesPerLine() // 4, 4)[:, :img.width(), :].copy()


def ink(px):
    """着色像素数：偏离纯白超过阈值的像素。"""
    return int((np.abs(px[:, :, :3].astype(int) - 255).max(axis=2) > 12).sum())


settle()
ids = [l.id for l in win.map.layers]
print("图层", [l.name for l in win.map.layers])
base = grab()
print(f"初始着色像素 {ink(base)}")

win.toggle_visible(ids[1], False)
settle()
after1 = grab()
d1 = int((after1[:, :, :3] != base[:, :, :3]).any(axis=2).sum())
print(f"隐藏第 2 层：变化像素 {d1} → {'画面已变 OK' if d1 > 1000 else '没反应 !!'}")

win.toggle_visible(ids[0], False)
settle()
after2 = grab()
print(f"两层都隐藏：着色像素 {ink(after2)} → {'已清空 OK' if ink(after2) < 50 else '仍有残留 !!'}")

win.toggle_visible(ids[1], True)
settle()
after3 = grab()
print(f"只开第 2 层：着色像素 {ink(after3)} → {'回来了 OK' if ink(after3) > 100 else '没回来 !!'}")

win.toggle_visible(ids[0], True)
settle()
after4 = grab()
same = int((after4[:, :, :3] == base[:, :, :3]).all(axis=2).sum()) / (after4.shape[0] * after4.shape[1])
print(f"全部恢复：与初始一致 {same*100:.2f}% → {'OK' if same > 0.999 else '不一致 !!'}")

# 真正点一次复选框（走 LayerRow 里那条信号链，而不是直接调 toggle_visible）
item = win.layer_list.item(0)
row = win.layer_list.itemWidget(item)
lid = item.data(0x0100)          # Qt.UserRole
before = win.visible.get(lid, True)
row.eye.click()
settle()
flipped = win.visible.get(lid, True) != before
after5 = grab()
changed = int((after5[:, :, :3] != after4[:, :, :3]).any(axis=2).sum())
print(f"点击复选框：状态翻转={flipped} 画面变化 {changed} 像素 "
      f"→ {'OK' if flipped and changed > 1000 else '没生效 !!'}")
row2 = win.layer_list.itemWidget(win.layer_list.item(0))
row2.eye.click()                 # 恢复
settle()

ok = (d1 > 1000 and ink(after2) < 50 and ink(after3) > 100 and same > 0.999
      and flipped and changed > 1000)
print("\n结论:", "PASS" if ok else "FAIL")
win.close()
