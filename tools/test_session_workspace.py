"""会话工作区回归：切会话同步恢复图层（共享缓存，不按会话复制）。

覆盖：
· 新建会话时旧会话图层能落盘、引用保存在 session_state；
· 切回旧会话时图层 id/名称/要素数恢复；
· 图层数据来自 data/cache 共享缓存，不生成会话专用副本；
· 会话列表里有图层数。

用法：.venv/Scripts/python.exe tools/test_session_workspace.py
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="gis_session_ws_"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["GIS_AGENT_PERSIST"] = "1"
os.environ["GIS_AGENT_DB"] = str(TMP / "agent.db")
os.environ["GIS_AGENT_CACHE_DIR"] = str(TMP / "cache")

import geopandas as gpd
from shapely.geometry import Point

from PySide6.QtWidgets import QApplication

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    from native.app import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.resize(1000, 760)
    win.show()
    app.processEvents()

    print("[1] 当前会话加一个图层并落盘")
    old_sid = win.session_id
    g1 = gpd.GeoDataFrame({"名称": ["A", "B"]},
                          geometry=[Point(104, 30), Point(104.1, 30.1)], crs=4326)
    lay1 = win.store.add(g1, "地块")
    win.visible[lay1.id] = False
    win._persist_state(force_cache=True)
    state = win.agent_db.session_state(old_sid)
    descs = state.get("layers") or []
    check("会话状态保存了图层", [d["name"] for d in descs], ["地块"])
    cache = Path(descs[0].get("cache_path") or "")
    check("图层写进共享缓存", cache.is_file(), True)
    check("缓存在 cache 目录", cache.parent.name, "cache")

    print("[2] 新建会话")
    win.new_session()
    app.processEvents()
    new_sid = win.session_id
    check("换成了新会话", new_sid != old_sid, True)
    check("新会话没有图层", len(win.store.layers), 0)
    listed = {s["id"]: s for s in win._workspace_sessions()}
    check("工作区有旧会话", old_sid in listed, True)
    check("旧会话图层数", listed[old_sid]["layer_count"], 1)
    check("新会话图层数", listed[new_sid]["layer_count"], 0)

    print("[3] 切回旧会话，同步恢复图层")
    win.open_session(old_sid)
    app.processEvents()
    check("会话 id 恢复", win.session_id, old_sid)
    check("图层数恢复", len(win.store.layers), 1)
    lay = next(iter(win.store.layers.values()))
    check("图层名恢复", lay.name, "地块")
    check("图层 id 恢复", lay.id, lay1.id)
    check("图层要素数恢复", len(lay.gdf), 2)
    check("可见状态恢复", win.visible.get(lay.id), False)
    check("地图读的是同一个显隐字典", win.map.visible is win.visible, True)
    check("地图显隐状态同步", win.map.visible.get(lay.id), False)
    check("地图同步图层", len(win.map._vec_layers), 1)
    check("当前选中", win.current_layer, lay.id)

    print("[4] 切回新会话")
    win.open_session(new_sid)
    app.processEvents()
    check("新会话仍为空", len(win.store.layers), 0)
    check("旧会话缓存没被删", cache.is_file(), True)

    win.tasks.shutdown(wait=False)
    win.close()
    win.deleteLater()
    app.processEvents()
    app.quit()

    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
