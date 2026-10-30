"""任务中心面板回归：状态展示、取消/重试/日志信号、清除已完成。

用法：.venv\\Scripts\\python.exe tools\\test_task_center.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from native.taskcenter import TaskCenter, TaskRow

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


app = QApplication.instance() or QApplication([])

TASKS = [
    {"id": "t1", "kind": "agent", "label": "对地块做 500 米缓冲区",
     "status": "running", "elapsed": 1.2, "error": None},
    {"id": "t2", "kind": "agent", "label": "统计道路长度",
     "status": "queued", "elapsed": 0.0, "error": None},
    {"id": "t3", "kind": "import", "label": "导入 道路.shp",
     "status": "succeeded", "elapsed": 12.0, "error": None},
    {"id": "t4", "kind": "opgen", "label": "生成算子",
     "status": "failed", "elapsed": 3.0, "error": "接口超时"},
]

print("[1] 面板渲染")
panel = TaskCenter(snapshot=lambda: list(TASKS))
panel.refresh()
check("显示了 4 行", len(panel._rows), 4)
check("面板应该显示", panel.should_show, True)
check("默认展开", panel._expanded, True)
row1 = panel._rows["t1"]
check("运行中显示取消按钮", row1.btn_cancel.isVisible(), True)
check("运行中不显示重试", row1.btn_retry.isVisible(), False)
row4 = panel._rows["t4"]
check("失败显示重试", row4.btn_retry.isVisible(), True)
check("失败显示日志", row4.btn_log.isVisible(), True)
check("失败显示错误", "接口超时" in row4.sub.text(), True)
row3 = panel._rows["t3"]
check("成功不显示取消", row3.btn_cancel.isVisible(), False)
check("成功显示日志", row3.btn_log.isVisible(), True)

print("[2] 信号")
got = []
panel.cancel_requested.connect(lambda tid: got.append(("cancel", tid)))
panel.retry_requested.connect(lambda tid: got.append(("retry", tid)))
panel.log_requested.connect(lambda tid: got.append(("log", tid)))
row1.btn_cancel.click()
row4.btn_retry.click()
row3.btn_log.click()
check("取消信号", ("cancel", "t1") in got, True)
check("重试信号", ("retry", "t4") in got, True)
check("日志信号", ("log", "t3") in got, True)

print("[3] 清除已完成")
panel._clear_finished()
check("已完成的被移除", "t3" not in panel._rows, True)
check("运行中的保留", "t1" in panel._rows, True)
check("排队中的保留", "t2" in panel._rows, True)
# 再刷新一次，被清除的不能回来
panel.refresh()
check("刷新后不回来", "t3" not in panel._rows, True)
check("刷新后运行中还在", "t1" in panel._rows, True)

print("[4] 无任务时收起")
panel._snapshot = lambda: []
panel.refresh()
check("面板收起", panel.should_show, False)
check("列表清空", len(panel._rows), 0)

print("[5] 折叠/展开")
panel._snapshot = lambda: list(TASKS)
panel.refresh()
panel.set_expanded(False)
check("已折叠", panel._expanded, False)
check("列表不可见", panel.scroll.isVisible(), False)
panel.set_expanded(True)
check("已展开", panel._expanded, True)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
