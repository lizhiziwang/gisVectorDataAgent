"""左栏「图层 / 工具库」分隔条的回归测试：能拖、有最小高度、记得住。

这两块原来是布局按 stretch 硬分配的（图层一多就只能在小窗里滚，工具库却
一直占着固定高度）。现在换成 QSplitter：拖动改高度、松手写进本地设置、
双击恢复默认比例。

用法：python tools/test_sidebar_split.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSplitter

import native.app as app_mod
from native.app import MainWindow, SideSplitter

app = QApplication(sys.argv)

# 启动阶段先屏蔽磁盘上存过的 sidebar_split：断言要的是「默认比例 + 拖拽行为」，
# 不能被上一次运行留下的高度影响（写盘用下面的内存假配置，不碰真实 settings.json）。
_real_load_cfg = app_mod.load_cfg


def _boot_load():
    cfg = dict(_real_load_cfg())
    cfg.pop("sidebar_split", None)
    return cfg


app_mod.load_cfg = _boot_load
try:
    win = MainWindow()
finally:
    app_mod.load_cfg = _real_load_cfg
win.resize(1400, 900)
win.show()
app.processEvents()

split = win.side_split
ok_struct = (isinstance(split, SideSplitter) and isinstance(split, QSplitter)
             and split.orientation() == Qt.Vertical and split.count() == 2
             and split.handleWidth() > 0)
print(f"分隔条：{type(split).__name__}，{split.count()} 块，handle={split.handleWidth()}px，"
      f"整个高 {split.height()}px")
print(f"分区结构 → {'OK' if ok_struct else '不对 !!'}")

sizes = split.sizes()
ok_init = len(sizes) == 2 and min(sizes) > 0 and sum(sizes) <= split.height()
print(f"初始高度 {sizes}（两块的 minHeight {split.widget(0).minimumHeight()} / "
      f"{split.widget(1).minimumHeight()}）→ {'OK' if ok_init else '没分好 !!'}")

# 拖：上面拉大，下面就得变小（QSplitter 会把给的数按可用高度换算）
split.setSizes([500, 260])
app.processEvents()
grown = split.sizes()
ok_drag = grown[0] > sizes[0] and grown[1] < sizes[1]
print(f"上面拉大：{sizes} → {grown} → {'OK' if ok_drag else '没生效 !!'}")

# 压到 1px：两块都得被最小高度挡住，不能塌没
split.setSizes([1, 1])
app.processEvents()
crushed = split.sizes()
ok_min = (crushed[0] >= SideSplitter.MIN_TOP
          and crushed[1] >= SideSplitter.MIN_BOTTOM)
print(f"压到最小：{crushed}（下限 {SideSplitter.MIN_TOP} / {SideSplitter.MIN_BOTTOM}）"
      f"→ {'OK' if ok_min else '被压塌了 !!'}")

# 双击分隔条 → 回到默认比例
split.setSizes([600, 180])
app.processEvents()
handle = split.handle(1)
QTest.mouseDClick(handle, Qt.LeftButton)
app.processEvents()
reset = split.sizes()
d0, d1 = split.default_sizes()
want = d0 / (d0 + d1)
got = reset[0] / max(1, sum(reset))
ok_reset = abs(got - want) < 0.05
print(f"双击恢复默认：{reset}（默认比例 {want:.2f}，实际 {got:.2f}）"
      f"→ {'OK' if ok_reset else '没回到默认 !!'}")

# 存盘 / 恢复：把 load_cfg / save_cfg 换成内存里的，别动真实的 data/settings.json
real_load, real_save = app_mod.load_cfg, app_mod.save_cfg
store = {}


def fake_load():
    cfg = dict(real_load())
    cfg.update(store)
    return cfg


def fake_save(cfg):
    store.update(cfg)


app_mod.load_cfg, app_mod.save_cfg = fake_load, fake_save
try:
    split.setSizes([520, 240])
    app.processEvents()
    want_sizes = list(split.sizes())
    win._save_sidebar_split()
    saved = store.get("sidebar_split") or []
    ok_save = (len(saved) == 2
               and all(abs(int(a) - int(b)) <= 2 for a, b in zip(saved, want_sizes)))
    print(f"保存：{want_sizes} → {saved} → {'OK' if ok_save else '没存对 !!'}")

    split.setSizes([240, 520])
    app.processEvents()
    win._restore_sidebar_split()
    app.processEvents()
    got = list(split.sizes())
    ok_restore = (len(got) == 2
                  and all(abs(a - b) <= 2 for a, b in zip(got, want_sizes)))
    print(f"恢复：{list(split.sizes())} → {'OK' if ok_restore else '没恢复 !!'}")

    # 拖动过程中的多次信号只攒一次写盘
    win._on_sidebar_split_moved(0, 1)
    ok_debounce = win._split_timer.isActive()
    print(f"拖动后延时写盘：{'OK' if ok_debounce else '没挂上 !!'}")
    win._split_timer.stop()
finally:
    app_mod.load_cfg, app_mod.save_cfg = real_load, real_save

# 坏值不能把界面搞坏：normalize 会丢掉不合理的高度
from app.config import normalize
bad = normalize({"sidebar_split": ["x", None]})
ok_bad = "sidebar_split" not in bad
good = normalize({"sidebar_split": [400, 300]})
ok_good = good.get("sidebar_split") == [400, 300]
print(f"配置校验：坏值丢弃={ok_bad}，好值保留={ok_good}")

ok = (ok_struct and ok_init and ok_drag and ok_min and ok_reset and ok_save
      and ok_restore and ok_debounce and ok_bad and ok_good)
print("\n结论:", "PASS" if ok else "FAIL")
win.close()
sys.exit(0 if ok else 1)
