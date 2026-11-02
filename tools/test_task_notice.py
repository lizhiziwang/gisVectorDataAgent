"""地图顶部任务通知回归。

覆盖：
· 活动任务出现通知、should_show=True；
· 完成后通知短暂保留、should_show=False，TTL 后自动隐藏；
· 取消/重试/日志信号；
· 多任务最多显示 3 条，宽度限制在地图内；
· 旧的 TaskCenter 面板仍保留给低层测试（不再放进右栏）。

用法：.venv/Scripts/python.exe tools/test_task_notice.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from native.taskcenter import TaskNotice

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    host = QWidget()
    host.resize(1000, 700)
    host.show()

    now = time.time()
    tasks = []

    def snap():
        return list(tasks)

    notice = TaskNotice(snapshot=snap, parent=host)
    notice.refresh()
    check("没有任务时隐藏", notice.isVisible(), False)
    check("没有任务时 should_show", notice.should_show, False)

    print("[1] 活动任务")
    tasks.append({"id": "t1", "label": "导入 2024年国土行政区划.gdb",
                  "status": "running", "elapsed": 7.4, "created_at": now})
    tasks.append({"id": "t2", "label": "检查影像与行政区划相交的所有要素",
                  "status": "queued", "elapsed": 0, "created_at": now + 1})
    notice.refresh()
    app.processEvents()
    check("显示通知", notice.isVisible(), True)
    check("should_show=True", notice.should_show, True)
    check("两条活动通知", len(notice._rows), 2)
    check("标题正确", notice._rows["t1"].title.text(),
          "导入 2024年国土行政区划.gdb")
    check("执行中行有取消", notice._rows["t1"].btn_cancel.isVisible(), True)

    print("[2] 信号")
    got = []
    notice.cancel_requested.connect(lambda tid: got.append(("cancel", tid)))
    notice.retry_requested.connect(lambda tid: got.append(("retry", tid)))
    notice.log_requested.connect(lambda tid: got.append(("log", tid)))
    notice._rows["t1"].btn_cancel.click()
    app.processEvents()
    check("取消信号", got, [("cancel", "t1")])

    print("[3] 完成/失败通知与 TTL")
    tasks[0] = {"id": "t1", "label": "导入 2024年国土行政区划.gdb",
                "status": "succeeded", "elapsed": 7.4,
                "created_at": now, "finished_at": time.time() - 1}
    tasks[1] = {"id": "t2", "label": "检查影像与行政区划相交的所有要素",
                "status": "failed", "elapsed": 0.7,
                "created_at": now + 1, "finished_at": time.time() - 1,
                "error": "字段不存在"}
    notice.refresh()
    check("完成后仍短暂保留", len(notice._rows), 2)
    check("完成后 should_show=False", notice.should_show, False)
    check("失败行有重试", notice._rows["t2"].btn_retry.isVisible(), True)
    check("完成行有日志", notice._rows["t1"].btn_log.isVisible(), True)
    # 把完成时间改到 TTL 之前，刷新后应只剩失败那条（失败 TTL 更长）
    tasks[0]["finished_at"] = time.time() - 20
    notice.refresh()
    check("成功通知过期", "t1" not in notice._rows, True)
    check("失败通知还在", "t2" in notice._rows, True)

    print("[4] 最多 3 条 / 宽度")
    tasks.clear()
    for i in range(5):
        tasks.append({"id": f"n{i}", "label": f"任务 {i}", "status": "succeeded",
                      "elapsed": 1, "created_at": now + i,
                      "finished_at": time.time() - 0.1})
    notice.refresh()
    check("最多显示 3 条", len(notice._rows), 3)
    notice._reposition()
    check("宽度不超过宿主", notice.width() <= host.width() - 72, True)
    check("水平居中", abs(notice.x() - (host.width() - notice.width()) // 2) <= 1, True)

    print("[5] 手动清除完成")
    notice._clear_finished()
    check("清除后隐藏", notice.isVisible(), False)
    check("清除后无行", len(notice._rows), 0)

    notice.close()
    host.close()
    app.processEvents()

    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
