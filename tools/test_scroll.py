"""对话区滚动：新内容要自动贴底，用户往上翻时不许被拽回去。

用法：.venv\\Scripts\\python.exe tools\\test_scroll.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from native.app import MainWindow

fails = []


def check(name, cond, extra=""):
    print(("  OK  " if cond else "  !!  ") + name + (f": {extra}" if extra else ""))
    if not cond:
        fails.append(name)


app = QApplication(sys.argv)
win = MainWindow()
win.resize(1440, 900)
win.show()
app.processEvents()
chat = win.chat
bar = chat.scroll.verticalScrollBar()


def settle(ms=120):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < ms / 1000:
        app.processEvents()
        time.sleep(0.005)


STEP = {
    "tool": "area_stats", "label": "面积统计", "status": "ok",
    "message": "按 fclass 类型分组统计了各类型面积：共 20 类，合计 75038.82 km²",
    "elapsed": 1.3, "warnings": [], "layer": None,
    "data": {"table": {"columns": ["fclass", "面积 km²", "要素数"],
                       "rows": [["forest", 67522.49, 27103], ["residential", 1726.10, 11906],
                                ["industrial", 176.75, 72586], ["farmland", 960.36, 9166],
                                ["park", 251.20, 3089]]}},
}

print("[1] 连续追加长内容（含步骤卡片 + 表格），每加一条都要贴底")
chat.add_agent("初始说明")
for i in range(8):
    chat.add_agent(f"第 {i + 1} 段：土地按 fclass 分组统计结果如下。" * 6,
                   [STEP, dict(STEP, label="缓冲区")])
    settle()
    at_bottom = bar.value() >= bar.maximum() - 1
    if not at_bottom:
        check(f"第 {i + 1} 条后应贴底", False, f"{bar.value()} / {bar.maximum()}")
        break
else:
    check("8 条长内容后仍然贴底", bar.value() >= bar.maximum() - 1, f"{bar.value()} / {bar.maximum()}")

print("[2] 内容继续长高（重排/表格）时也不能掉队")
bar.setValue(bar.maximum())
settle()
chat.add_agent("再来一条很长的消息：" + "这里是很多文字。" * 60)
settle(200)
check("追加后仍在底部", bar.value() >= bar.maximum() - 1, f"{bar.value()} / {bar.maximum()}")

print("[3] 用户往上翻 → 新消息不许把他拽回去")
bar.setValue(0)
settle()
check("已滚到顶部", bar.value() == 0)
chat.add_agent("这一条是在用户翻到顶部之后来的")
settle(200)
check("位置保持在顶部附近", bar.value() <= 40, f"value={bar.value()}")

print("[4] 回到底部后恢复自动跟随")
bar.setValue(bar.maximum())
settle()
chat.add_agent("回到底部之后的新消息")
settle(200)
check("又贴底了", bar.value() >= bar.maximum() - 1, f"{bar.value()} / {bar.maximum()}")

print("[5] 真实流程：发消息 → 出现「正在规划」→ 换成结果")
chat.add_user("按 fclass 类型分组统计面积")
chat.add_typing()
settle(80)
check("等待态贴底", bar.value() >= bar.maximum() - 1, f"{bar.value()} / {bar.maximum()}")
chat.remove_typing()
chat.add_agent("按 fclass 类型分组统计了各类面积：共 20 类，合计 75038.82 km²。\n"
               "前几位：forest 67522.485km²、nature_reserve 2741.91km²。\n"
               "面积单位已按 CGCS2000 / 浙江 CM 120°E (EPSG:4548) 换算。",
               [STEP, dict(STEP, label="分组统计")])
settle(250)
check("结果贴底（长回复 + 表格）", bar.value() >= bar.maximum() - 1, f"{bar.value()} / {bar.maximum()}")

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
