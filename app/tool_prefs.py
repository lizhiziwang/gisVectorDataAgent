"""工具库「启用 / 停用」的运行时状态，外加用户自建的分组表。

为什么单独放一个模块
--------------------
停用某工具要同时影响三处：左栏工具库不显示、规划提示词不出现、执行前拒绝。
这三处分别在 native/ 与 app/agent.py，不能互相 import；把「当前停用集合」
放在这里，谁用谁读，避免界面状态和规划器状态各存一份走偏。

用户新建的工具分组（「＋ 新建分组…」建的）也放这儿，原因一样：左栏工具库、
Agent 设置 →「工具」、新建工具弹窗都要看到同一份列表。

持久化在 data/settings.json：`tools_disabled` / `tool_groups`，由桌面端启动时
和保存设置后调用 `set_disabled()` / `set_groups()` 灌进来；本模块只保存内存副本。
"""
from __future__ import annotations

_DISABLED: set[str] = set()

#: 用户自建的分组（按创建顺序；不含内置分组和「我的工具」）
_GROUPS: list[str] = []

#: 分组下拉里「新建分组」那一项的文案；界面和校验共用，避免被当成真分组名
NEW_GROUP_LABEL = "＋ 新建分组…"

#: 分组名上限：太长的名字会把左栏分组标题挤成省略号
MAX_GROUP_NAME = 12
#: 分组数量上限：防止误点一下多一个分组，把工具库撑爆
MAX_GROUPS = 24


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


# ---------------------------------------------------------------- 自定义分组
def _builtin_groups() -> list[str]:
    from .tool_registry import GROUP_CHOICES
    return list(GROUP_CHOICES)


def add_group(name) -> str:
    """登记一个用户新建的分组，返回最终采用的名字。

    · 名字会去掉首尾空白、把连续空白压成一个（左栏标题按单行显示）；
    · 选的是内置分组 / 已建过的分组 → 原样返回，不重复登记；
    · 空名字、太长、数量超限 → 抛 `ValueError`，消息直接给界面弹提示用。
    """
    n = " ".join(str(name or "").split())
    if not n:
        raise ValueError("分组名不能为空。")
    if n == NEW_GROUP_LABEL or n.startswith(NEW_GROUP_LABEL[0]):
        raise ValueError("分组名不能以「＋」开头（那是「新建分组」按钮的样子）。")
    if len(n) > MAX_GROUP_NAME:
        raise ValueError(f"分组名最多 {MAX_GROUP_NAME} 个字，太长了左栏放不下。")
    if n in _builtin_groups():
        return n
    if n in _GROUPS:
        return n
    if len(_GROUPS) >= MAX_GROUPS:
        raise ValueError(f"最多只能建 {MAX_GROUPS} 个分组，先清理掉不用的吧。")
    _GROUPS.append(n)
    return n


def set_groups(names) -> None:
    """整体替换自定义分组（启动 / 保存设置后调用）。坏名字直接跳过。"""
    _GROUPS.clear()
    for raw in names or ():
        try:
            add_group(raw)
        except ValueError:
            continue


def custom_groups() -> list[str]:
    """用户新建的分组（副本）。"""
    return list(_GROUPS)


def group_choices() -> list[str]:
    """分组下拉的候选：内置分组 → 用户新建的 → 「我的工具」（默认落点）。"""
    from .tool_registry import CUSTOM_GROUP

    builtin = [g for g in _builtin_groups() if g != CUSTOM_GROUP]
    return builtin + list(_GROUPS) + [CUSTOM_GROUP]
