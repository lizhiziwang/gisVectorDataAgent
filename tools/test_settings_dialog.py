"""模型设置窗口的回归：格式下拉、预设联动、预览、自定义覆盖。

离线，不联网（测试连接按钮不打真接口）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app import api_formats
from native.dialogs import SettingsDialog

app = QApplication.instance() or QApplication([])
fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


print("[1] 格式下拉列出全部格式")
dlg = SettingsDialog({"api_key": "k", "base_url": "https://api.example.com/v1",
                       "model": "m1", "enabled": True})
labels = [dlg.fmt.itemText(i) for i in range(dlg.fmt.count())]
check("有 5 种格式", dlg.fmt.count(), 5)
check("默认选中 OpenAI", dlg.fmt.currentData(), "openai_chat")
check("回显当前格式", dlg.fmt_desc.text(), api_formats.describe("openai_chat"))

print("[2] 切格式 → 预览跟着变")
dlg.fmt.setCurrentIndex(dlg.fmt.findData("anthropic"))
check("切到 anthropic", dlg.fmt.currentData(), "anthropic")
check("预览更新", "x-api-key" in dlg.preview.text(), True)
check("预览 URL 正确", "/messages" in dlg.preview.text(), True)
dlg.fmt.setCurrentIndex(dlg.fmt.findData("gemini"))
check("Gemini 预览", ":generateContent" in dlg.preview.text(), True)
dlg.fmt.setCurrentIndex(dlg.fmt.findData("ollama"))
check("Ollama 预览", "/api/chat" in dlg.preview.text(), True)

print("[3] 预设按钮会同时改地址、模型和格式")
d = SettingsDialog({})
d._preset("https://api.anthropic.com/v1", "claude-sonnet-4-20250514", "anthropic")
check("地址", d.base.text(), "https://api.anthropic.com/v1")
check("模型", d.model.text(), "claude-sonnet-4-20250514")
check("格式", d.fmt.currentData(), "anthropic")
d._preset("http://localhost:11434/v1", "qwen2.5:7b", "ollama")
check("Ollama 预设带 key", d.key.text(), "ollama")

print("[4] 自定义覆盖：路径 + 请求头")
d = SettingsDialog({})
d.fmt.setCurrentIndex(d.fmt.findData("openai_chat"))
d.base.setText("https://gw.example.com")
d.adv.setChecked(True)
check("高级区展开", d.adv_open, True)
d.ov_path.setText("/v2/invoke")
d.ov_headers.setPlainText("X-Team: gis\n\n坏行没有冒号\nX-Other: 2")
v = d.value()
check("路径", v["api_overrides"]["path"], "/v2/invoke")
check("请求头解析", v["api_overrides"]["headers"], {"X-Team": "gis", "X-Other": "2"})
check("格式进配置", v["api_format"], "openai_chat")

print("[5] 旧配置没有 api_format 时也能打开")
d2 = SettingsDialog({"api_key": "k", "base_url": "https://x/v1", "model": "m"})
check("默认格式", d2.fmt.currentData(), "openai_chat")
check("高级区默认收起", d2.adv_open, False)

print("[6] 已有自定义配置的能回显")
d3 = SettingsDialog({"api_key": "k", "api_format": "anthropic",
                      "api_overrides": {"path": "/v9/x", "headers": {"X-A": "1"}}})
check("格式回显", d3.fmt.currentData(), "anthropic")
check("路径回显", d3.ov_path.text(), "/v9/x")
check("请求头回显", "X-A: 1" in d3.ov_headers.toPlainText(), True)
check("高级区展开", d3.adv_open, True)

print("[7] 测试连接按钮不能打真接口")
d4 = SettingsDialog({"api_key": "real-key-should-not-be-used", "base_url": "https://x/v1",
                      "model": "m", "enabled": True})


def boom(*a, **kw):
    raise AssertionError("设置窗口的测试按钮不允许发真实请求")


import urllib.request
urllib.request.urlopen = boom
d4._test()
check("测试失败有提示", "失败" in d4.test_result.text(), True)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
