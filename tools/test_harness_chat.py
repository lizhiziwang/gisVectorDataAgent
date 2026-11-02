"""对话组件的 Harness 式按钮/状态回归。

覆盖：
· 对话 / 轨迹两个页签；
· 后台任务胶囊、工具调用状态条；
· 底栏模型选择器、轮数/步数/token 统计；
· 右侧面板开关能隐藏/恢复左侧工作区。

用法：.venv/Scripts/python.exe tools/test_harness_chat.py
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")

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
    win.resize(1100, 800)
    win.show()
    app.processEvents()
    chat = win.chat

    print("[1] 对话 / 轨迹")
    check("两个页签", chat.tabs.count(), 2)
    check("页签标题", [chat.tabs.tabText(i) for i in range(2)], ["对话", "轨迹"])
    chat.tabs.setCurrentIndex(1)
    app.processEvents()
    check("切到轨迹页", chat.chat_stack.currentIndex(), 1)
    chat.tabs.setCurrentIndex(0)
    app.processEvents()
    check("切回对话页", chat.chat_stack.currentIndex(), 0)

    print("[2] 后台任务胶囊")
    chat.set_background_tasks(2, 1)
    app.processEvents()
    check("胶囊显示", chat.task_pill.isVisible(), True)
    check("胶囊内容", "3 个后台任务运行中" in chat.task_pill.text(), True)
    chat.set_background_tasks(0, 0)
    check("无任务隐藏", chat.task_pill.isVisible(), False)

    print("[3] 工具调用状态")
    chat.set_tool_activity("job_output", True)
    app.processEvents()
    check("状态条显示", chat.tool_activity.isVisible(), True)
    check("工具名", "job_output" in chat.tool_label.text(), True)
    chat.set_tool_activity("", False)
    check("状态条隐藏", chat.tool_activity.isVisible(), False)

    print("[4] 轨迹")
    chat.add_trace("调用工具 · raster_info")
    check("轨迹有内容", "raster_info" in chat.trace_view.toPlainText(), True)

    print("[5] 模型与统计")
    chat._refresh_model_button()
    check("模型按钮有文字", bool(chat.model_btn.text()), True)
    chat.set_stats(18, 926, 197, 388_000_000, 99.6, 52)
    text = chat.stats.text()
    for token in ("18 轮", "926 步", "197 tok/s", "388.0M tok", "99.6%", "52%"):
        check(f"统计包含 {token}", token in text, True)

    print("[6] 右侧面板开关")
    before = win.left_panel.isVisible()
    chat.pane_btn.click()
    app.processEvents()
    check("点击后切换左栏", win.left_panel.isVisible(), not before)
    chat.pane_btn.click()
    app.processEvents()
    check("再点恢复", win.left_panel.isVisible(), before)

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
    sys.exit(main())
