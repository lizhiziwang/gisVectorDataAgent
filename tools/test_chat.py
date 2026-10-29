"""聊天气泡外观的回归测试：用户消息必须是蓝底、右对齐、文字可见。

之前的事故：`self.msgs.setStyleSheet("background:#fdfefe;")` 这种
**不带选择器**的声明会级联到所有子控件，把 `QFrame#userBubble` 的蓝底盖成白色，
白底 + 白字 → 用户以为「自己发的消息没显示」。

判定：找出 userBubble，量它自己渲染出来的像素里蓝色占比（>50% 才算正常）。
用法：python tools/test_chat.py
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PySide6.QtWidgets import QApplication, QFrame

from native.app import MainWindow

app = QApplication(sys.argv)
win = MainWindow()
win.resize(1280, 800)
win.show()

t0 = time.perf_counter()
while time.perf_counter() - t0 < 30:
    app.processEvents()
    if win.chat.scroll.isVisible():
        break
    time.sleep(0.01)

win.chat.add_user("这是一条用户消息，应该显示成右侧蓝色气泡")
for _ in range(60):
    app.processEvents()
    time.sleep(0.01)

bubbles = [f for f in win.chat.findChildren(QFrame) if f.objectName() == "userBubble"]
if not bubbles:
    print("!! 没有找到 userBubble")
    print("结论: FAIL")
    sys.exit(1)

b = bubbles[-1]
img = b.grab().toImage()
a = np.frombuffer(img.constBits(), count=img.sizeInBytes(), dtype=np.uint8)
a = a.reshape(img.height(), img.bytesPerLine() // 4, 4)[:, :img.width(), :].astype(int)
# 新版视觉（参考 Claude Code）：用户消息是浅暖灰底块 + 深色字，
# 判定改成「和页面底色有明显差别」+「有深色文字」。
corner = a[2, 2, :3]
body = a[a.shape[0] // 3, a.shape[1] // 2, :3]
fill_delta = float(np.abs(corner - np.array([255, 255, 255])).max())
dark_ratio = float((a[:, :, :3].mean(axis=2) < 110).mean())

# 右对齐：气泡中心应当在消息区右半边
p = b.mapTo(win.chat, b.rect().topLeft())
right = p.x() + b.width() / 2 > win.chat.width() / 2

dark_px = int((a[:, :, :3].mean(axis=2) < 110).sum())
print(f"userBubble 尺寸 {b.width()}x{b.height()}, 底色偏离白 {fill_delta:.0f}, "
      f"深色字形像素 {dark_px}, 右对齐={right}")

# 短消息不能留一大片空白：气泡宽度应当≈文字宽度 + 内边距（26px），
# 老代码里 `_bubble_size` 写死了 80px 最小宽度，「你好」的气泡右边空掉一半。
win.chat.add_user("你好")
for _ in range(30):
    app.processEvents()
    time.sleep(0.01)
short = [f for f in win.chat.findChildren(QFrame) if f.objectName() == "userBubble"][-1]
slack = short.width() - short._label.width()
text_w = short._label.fontMetrics().horizontalAdvance("你好")
print(f"短消息气泡 {short.width()}x{short.height()}，文字宽 {text_w}，"
      f"两侧内边距合计 {slack}，右侧空白 {short._label.width() - text_w}")
fit_ok = short.width() <= text_w + 46 and slack <= 30
ok = fill_delta >= 6 and right and dark_px > 20 and fit_ok
print(f"短消息贴合文字: {fit_ok}")
print("\n结论:", "PASS" if ok else "FAIL")
win.close()
