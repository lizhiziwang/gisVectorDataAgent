"""看一眼「＋ 新建分组…」在真窗口里的样子（真字体，WA_DontShowOnScreen）。

弹出型 QComboBox 的候选列表是独立的顶层窗口，dlg.grab() 拍不到，
所以这里把下拉关掉、直接把「新建之后的样子」拍下来，并把候选列表打进 stdout。
用法：python tools/shot_tool_group_new.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app import tool_prefs
from native.app import MainWindow
from native.dialogs import NewToolDialog

OUT = r"D:\gis-agent\docs\perf\new-tool-dialog-newgroup.png"

app = QApplication(sys.argv)
win = MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.resize(1440, 900)
win.show()
app.processEvents()

tool_prefs.set_groups([])
dlg = NewToolDialog(win, group_choices=tool_prefs.group_choices())
dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
dlg.editor.setPlainText("统计每个面内道路的总长度和条数，一个新图层输出这些字段。")

# 模拟用户选「＋ 新建分组…」并输入名字（真弹窗会阻塞，这里只调登记 + 选中）
name = tool_prefs.add_group("城市规划")
dlg.group_combo.add_group_item(name)
dlg.group_combo.setCurrentText(name)
dlg.show()
app.processEvents()
dlg.grab().save(OUT)
print("已保存:", OUT, os.path.getsize(OUT), "字节")
print("候选:", [dlg.group_combo.itemText(i) for i in range(dlg.group_combo.count())])
print("value():", dlg.value())
tool_prefs.set_groups([])
