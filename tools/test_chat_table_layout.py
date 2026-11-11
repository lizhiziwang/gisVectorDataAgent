"""执行卡里的小表格不能被表头吃光高度。

之前 `_mini_table` 用 `26 + 22 * 行数` 拍脑袋定高，但 QSS 的 QHeaderView
padding + 字体让表头实际到 34px；1 行数据时数据区只剩 14px，行文字被裁得
几乎看不见（截图里就是只有表头、没有数值）。

这里直接量 QTableWidget 的 viewport / 行矩形，保证最后一行完整可见。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from native.chat import StepCard

FAIL = []


def check(name, ok, extra=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"   {extra}"))
    if not ok:
        FAIL.append(name)


app = QApplication.instance() or QApplication([])

for n in (1, 2, 6):
    print(f"\n[{n} 行]")
    rows = [["gis_osm_roads_free_1", 229885.368, 469714] for _ in range(n)]
    tw = StepCard._mini_table({"columns": ["范围", "长度(km)", "要素数"], "rows": rows})
    tw.resize(540, tw.height())
    tw.show()
    for _ in range(5):
        app.processEvents()

    header_h = tw.horizontalHeader().height()
    print(f"  table={tw.height()} header={header_h} viewport={tw.viewport().height()}")
    check("表格高度给数据区留了空间", tw.viewport().height() > 0,
          f"viewport={tw.viewport().height()}")
    check("表头没有吃掉所有高度", tw.viewport().height() >= tw.rowHeight(0) - 1,
          f"viewport={tw.viewport().height()} row={tw.rowHeight(0)}")

    last = tw.item(tw.rowCount() - 1, 0)
    rect = tw.visualItemRect(last)
    check("最后一行完整落在视口内", rect.bottom() <= tw.viewport().height(),
          f"rect={rect.bottom()} viewport={tw.viewport().height()}")
    check("单元格仍然有文字", bool(last and last.text()), repr(last.text() if last else None))
    tw.close()

print("\n" + ("FAIL: " + ", ".join(FAIL) if FAIL else "PASS"))
sys.stdout.flush()
os._exit(1 if FAIL else 0)
