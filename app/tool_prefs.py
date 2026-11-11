"""工具库「启用 / 停用」的运行时状态。

为什么单独放一个模块
--------------------
停用某工具要同时影响三处：左栏工具库不显示、规划提示词不出现、执行前拒绝。
这三处分别在 native/ 与 app/agent.py，不能互相 import；把「当前停用集合」
放在这里，谁用谁读，避免界面状态和规划器状态各存一份走偏。

持久化在 data/settings.json 的 `tools_disabled` 里，由桌面端启动时
和保存设置后调用 `set_disabled()` 灌进来；本模块只保存内存副本。
"""
from __future__ import annotations

_DISABLED: set[str] = set()


def set_disabled(names) -> None:
    """整体替换停用集合（启动时 / 保存设置后调用）。"""
    _DISABLED.clear()
    for n in names or ():
        n = str(n or "").strip()
        if n:
            _DISABLED.add(n)


def enable(name: str) -> None:
    """单独恢复一个工具（新建同名工具后避免它一出生就是隐藏的）。"""
    _DISABLED.discard(str(name or "").strip())


def is_enabled(name) -> bool:
    """这个工具当前是否启用；空名字一律当作启用，避免误伤导出等伪算子。"""
    return str(name or "").strip() not in _DISABLED


def disabled_names() -> set[str]:
    """返回停用集合的副本，调用方改它不会影响运行时状态。"""
    return set(_DISABLED)
