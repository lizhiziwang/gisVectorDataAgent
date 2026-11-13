"""任务栏 / 窗口图标的回归测试（不联网，离屏可跑）。

覆盖：
· app_icon() 多尺寸齐全（16 到 256），32px 有内容、带透明通道
· app_icon() 和 make_icon() 是同一张画（别悄悄换成别的图）
· Windows 上 set_windows_app_id() 真调 shell32 成功（从 pythonw 起的脚本，
  不声明 AppUserModelID 任务栏就会画 Python 的图标）
· 非 Windows 直接返回 False；系统调用抛异常也不炸
· native_app.prepare_app() 会把身份 + 图标一起立起来，主窗口图标跟它一致

用法：python tools/test_app_icon.py
"""
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else want(got)
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


print("[1] 图标本身")
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from native.branding import APP_USER_MODEL_ID, app_icon, make_icon, set_windows_app_id

icon = app_icon()
check("不是空图标", icon.isNull(), False)
sizes = sorted((s.width(), s.height()) for s in icon.availableSizes())
check("尺寸覆盖 16/24/32/48/64/128/256",
      all((n, n) in sizes for n in (16, 24, 32, 48, 64, 128, 256)), True)
pm32 = icon.pixmap(32, 32)
check("32px 能取到", pm32.isNull(), False)
check("32px 尺寸正确", (pm32.width(), pm32.height()), (32, 32))
img = pm32.toImage()
check("带透明通道", img.hasAlphaChannel(), True)
# 中心像素不透明（说明真的画了底），四角透明（圆角）
mid = img.pixelColor(16, 16)
corner = img.pixelColor(0, 0)
check("中心已绘制", mid.alpha(), 255)
check("四角透明（圆角底）", corner.alpha(), 0)
check("和 make_icon() 同一张图",
      app_icon().pixmap(48, 48).toImage()
      == make_icon().pixmap(48, 48).toImage(), True)

print("[2] Windows 应用身份（AppUserModelID）")
check("ID 非空且稳定", isinstance(APP_USER_MODEL_ID, str) and len(APP_USER_MODEL_ID) > 4, True)

import native.branding as branding

if sys.platform == "win32":
    check("真调 shell32 成功", set_windows_app_id(), True)
else:
    check("非 Windows 直接返回 False", set_windows_app_id(), False)

seen = {}
_real_impl = branding._set_app_id_win32
branding._set_app_id_win32 = lambda aid: seen.update({"id": aid}) or True
_real_platform = branding.sys
try:
    branding.sys = types.SimpleNamespace(platform="win32")
    check("把常量 ID 传给系统调用", set_windows_app_id(), True)
    check("传的就是 APP_USER_MODEL_ID", seen.get("id"), APP_USER_MODEL_ID)
    # 系统调用返回失败（非 0）= 没声明成功，要如实返回 False
    branding._set_app_id_win32 = lambda aid: False
    check("系统调用失败时返回 False", set_windows_app_id(), False)

    def _boom(aid):
        raise OSError("shell 不可用")

    branding._set_app_id_win32 = _boom
    check("系统调用抛异常也不炸", set_windows_app_id(), False)
finally:
    branding._set_app_id_win32 = _real_impl
    branding.sys = _real_platform

print("[3] 入口顺序：先身份 + 图标，再建窗口")
import native_app

seen2 = {}


def _fake_set_id(aid=None):
    seen2["id_called"] = True
    return True


branding._set_app_id_win32 = lambda aid: seen2.update({"id": aid}) or True
probe = QApplication.instance()
_real_platform = branding.sys
branding.sys = types.SimpleNamespace(platform="win32")
try:
    ok = native_app.prepare_app(probe)
finally:
    branding._set_app_id_win32 = _real_impl
    branding.sys = _real_platform
check("prepare_app 声明了应用身份", ok, True)
check("prepare_app 设了 QApplication 图标", probe.windowIcon().isNull(), False)
check("QApplication 图标 = app_icon()",
      probe.windowIcon().pixmap(32, 32).toImage() == app_icon().pixmap(32, 32).toImage(),
      True)

print("[4] 主窗口图标跟应用图标一致（标题栏 + 任务栏都读它）")
from native.app import MainWindow

win = MainWindow()
got = win.windowIcon()
check("主窗口有图标", got.isNull(), False)
check("主窗口图标 = app_icon()",
      got.pixmap(32, 32).toImage() == app_icon().pixmap(32, 32).toImage(), True)
win.close()
win.deleteLater()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(1 if fails else 0)
