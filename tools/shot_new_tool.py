"""截「用文字描述一个工具」弹窗，看分组下拉的实际效果。

离屏平台在这台机器上 QFontDatabase.families() 为 0，中文全是豆腐块，
所以用真 windows 平台 + WA_DontShowOnScreen：能拿到系统字体，又不会真的弹窗。
用法：python tools/shot_new_tool.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from native.app import MainWindow
from native.dialogs import GroupCombo, NewToolDialog
from app import tool_prefs
from app.tool_registry import GROUP_CHOICES

OUT = r"D:\gis-agent\docs\perf\new-tool-dialog.png"

app = QApplication(sys.argv)
win = MainWindow()
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.resize(1440, 900)
win.show()
app.processEvents()

# 演示「用户自建分组」的样子（截图专用，不动磁盘上的设置）
tool_prefs.set_groups(["城市规划", "管线巡检"])

dlg = NewToolDialog(win, group_choices=GROUP_CHOICES + tool_prefs.custom_groups())
dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
dlg.editor.setPlainText("统计每个面内道路的总长度和条数，一个新图层输出这些字段。")
dlg.group_combo.setCurrentText("属性与统计")
dlg.show()
app.processEvents()
dlg.grab().save(OUT)
print("已保存:", OUT, os.path.getsize(OUT), "字节")

# 也截一张默认选中「我的工具」的（下拉里能看到最下面的「＋ 新建分组…」）
dlg2 = NewToolDialog(win, group_choices=GROUP_CHOICES + tool_prefs.custom_groups())
dlg2.setAttribute(Qt.WA_DontShowOnScreen, True)
dlg2.editor.setPlainText("统计每个面内道路的总长度和条数。")
dlg2.show()
app.processEvents()
dlg2.group_combo.showPopup()
app.processEvents()
out2 = r"D:\gis-agent\docs\perf\new-tool-dialog-default.png"
dlg2.grab().save(out2)
print("已保存:", out2, os.path.getsize(out2), "字节",
      "（下拉项：", [dlg2.group_combo.itemText(i)
                     for i in range(dlg2.group_combo.count())], "）")
tool_prefs.set_groups([])
