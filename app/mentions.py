"""@ 图层提及：解析、匹配、消歧。

用户可以在输入框里用 `@` 引用图层，像 opencode 里 `@` 引用文件那样：

    对 @小区 做 300 米缓冲区，然后和 @地铁站 求交集

两层含义，缺一不可：
1. **指代**：`@小区` 就是「小区图层」，让模型/规则引擎知道说的是哪个图层，
   不用靠猜（`小区` vs `小区_合并` 这种，光看名字分不清）。
2. **选择**：`@` 是**显式**指定 —— 优先于「当前选中图层」，
   哪怕当前选中的是别的图层，也按 @ 指的来。

和 opencode 的差别要说清楚：@ 引用的是**图层**，不是文件路径；
而且 @ 是「点名道姓」，不用像路径那样靠上下文消歧 —— 但名字写错仍然要能报错，
不能悄悄操作到别的图层上去。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: @后面允许的字符：中文、字母、数字、下划线、连字符、点
_NAME_CHARS = r"[\w一-鿿\-.]"

#: @后面能一直吃到哪 —— 先粗略吃到「标点、换行或下一个 @」为止，
#: 再由解析阶段结合**真实图层名**贪心截断。
#: 这样「@2024 年 GDP」这种带空格的图层名才能认出来，
#: 而「@小区 做缓冲区」不会把后面整句都吃进来。
#: 遇到下一个 @ 要停 —— 否则「@A 和 @B」会变成一个提及。
_NAME_RE = r"[^\r\n@，。；、！？,;:!?）)\]【】“”\"']+"

#: @ 前不能紧挨着 ASCII 字母/数字/下划线/点 —— 那是 email（a@b.com）而不是提及。
#: 注意：Python 的 \w 在 Unicode 模式下**也匹配中文**，所以这里要显式排除汉字，
#: 否则「用@地铁站」这种常见写法会被误判成 email。
_MENTION_RE = re.compile(rf"(?<![A-Za-z0-9_.])@({_NAME_RE})")

#: 名字两端的空白和标点要去掉
_TRIM = " \t\r\n·.,;:，。；：、!！?？"

#: 常见的句末标点噪音：名字不该把「。」「，」吃进去
_NOISE_SUFFIX = re.compile(r"[.．。,，;；:：、!！?？]+$")

#: email 形态：@ 前面是本地部分、后面是域名（含点）
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass
class Mention:
    """一个 @提及。

    ``raw``     原文里写的东西（用户看到的）
    ``name``    去掉首尾空白后的名字
    ``start``   在原文中的起始位置
    ``end``     在原文中的结束位置
    ``layer_id`` 解析到的图层 id（没解析到是 None）
    ``layer``   解析到的图层对象（没解析到是 None）
    ``exact``   是否精确匹配到图层名
    ``candidates`` 名字有歧义时的候选图层（列表）
    ``reason``  没解析到时的原因（给用户看的）
    """

    raw: str
    name: str
    start: int
    end: int
    layer_id: str | None = None
    layer: object | None = None
    exact: bool = False
    candidates: list = field(default_factory=list)
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.layer_id is not None


def parse_mentions(text: str) -> list[Mention]:
    """从文本里抽出所有 @提及。

    只做**语法**解析，不碰图层库 —— 解析和匹配分开，
    这样单元测试不用造 Store。

    这里抓到的是「@后面一整串」（可能含空格），
    真正该取多长由 `resolve_mentions` 结合真实图层名贪心截断 ——
    「@2024 年 GDP」要三个词，「@小区 做缓冲区」只能要一个词。
    """
    out: list[Mention] = []
    if not text:
        return out
    # 先找出所有 email 片段，落在 email 里的 @ 不算提及
    emails = [(m.start(), m.end()) for m in _EMAIL_RE.finditer(text)]
    for m in _MENTION_RE.finditer(text):
        at = m.start()
        if any(a <= at < b for a, b in emails):
            continue
        raw = m.group(0)
        # 去掉句末标点（「@带.句点。」→「带.句点」）
        name = _NOISE_SUFFIX.sub("", raw[1:]).strip(_TRIM)
        if not name:
            continue
        out.append(Mention(raw=raw, name=name, start=m.start(),
                           end=m.start() + 1 + len(name)))
    return out


def _trim_to_layer(name: str, layers) -> tuple[str, object | None]:
    """把「@后面一整串」截到最匹配的真实图层名。

    从长到短试：先试整个串（「2024 年 GDP」），不行再逐个词缩短
    （「小区 做缓冲区」→「小区」）。命中就返回 (名字, 图层)。

    一个都没精确命中时，返回**最短**的一截（第一个词）——
    「不存在的图层 做缓冲区」应该缩成「不存在的图层」再去报错，
    而不是留着整句去和图层名做模糊匹配（那会莫名其妙地匹配上）。
    """
    if not layers:
        return name, None
    words = name.split()
    if not words:
        return name, None
    cands: list[str] = []
    for i in range(len(words), 0, -1):
        cands.append(" ".join(words[:i]))
    if len(words) == 1 and name not in cands:
        cands.append(name)
    for cand in cands:
        for lay in layers:
            if lay.name.lower() == cand.lower():
                return cand, lay
    # 没有精确命中：退回最短的一截（第一个词），交给上层报「没找到」
    return cands[-1], None


def _score(name: str, query: str) -> int:
    """给图层名和查询词打个匹配分，0 表示不匹配。

    分数越高越优先，用来在多个候选里挑最像的那个。
    """
    n = name.lower()
    q = query.lower()
    if not n or not q:
        return 0
    if n == q:
        return 100
    if n.startswith(q):
        return 80
    if q in n:
        return 60
    # 去掉常见后缀再比（「小区_合并」vs「小区」）
    stripped = re.sub(r"[_（(].*$", "", n).strip()
    if stripped == q:
        return 70
    if q and stripped and (stripped.startswith(q) or q in stripped):
        return 50
    # 字符级重合度 —— 只算「弱相关」，分数压得很低。
    # 「小区」和「不存在的图层」共享「小」「区」两个字，
    # 重合度有 3 分，但那**不是**用户想指的图层，不能当成候选。
    common = len(set(n) & set(q))
    return int(40 * common / max(len(set(n)), len(set(q)))) if common else 0


#: 低于这个分就算「不是同一个东西」。
#: 20 分对应「前一个字对得上」的弱关联，再低基本是巧合。
_MIN_SCORE = 20


def resolve_mentions(mentions: list[Mention], store) -> list[Mention]:
    """把提及解析到具体图层。原地修改并返回同一批对象。

    解析规则：
    · 先把「@后面一整串」按真实图层名**贪心截断**（「2024 年 GDP」）；
    · **精确匹配**图层名（不区分大小写）→ 直接命中；
    · **前缀 / 子串**匹配 → 命中候选里分数最高的；
    · 多个候选分数相同 → 有歧义，记下候选，交给上层问用户；
    · 一个都没匹配上 → 记原因（名字写错了 / 图层不存在）。
    """
    if not mentions:
        return mentions
    layers = list(store.layers.values()) if store is not None else []
    for mn in mentions:
        # 先按真实图层名截断（处理带空格的图层名）
        trimmed, exact_layer = _trim_to_layer(mn.name, layers)
        if trimmed != mn.name:
            mn.name = trimmed
            mn.end = mn.start + 1 + len(trimmed)
        if exact_layer is not None:
            mn.layer_id, mn.layer, mn.exact = exact_layer.id, exact_layer, True
            continue
        q = mn.name.lower()
        scored = []
        for lay in layers:
            s = _score(lay.name, mn.name)
            if s >= _MIN_SCORE:
                scored.append((s, lay))
        if not scored:
            mn.reason = f"没有叫「{mn.name}」的图层"
            continue
        scored.sort(key=lambda t: (-t[0], len(t[1].name)))
        best_s, best = scored[0]
        # 精确命中就落定；有歧义就交给上层处理
        if best_s >= 100:
            mn.layer_id, mn.layer, mn.exact = best.id, best, True
            continue
        same = [lay for s, lay in scored if s == best_s]
        if len(same) == 1:
            mn.layer_id, mn.layer = same[0].id, same[0]
            continue
        mn.candidates = same
        mn.reason = f"「{mn.name}」可能指：{'、'.join(l.name for l in same)}"
    return mentions


def strip_mentions(text: str, mentions: list[Mention]) -> str:
    """把 @标记从正文里去掉，只留名字。

    送给模型/规则引擎的文本里不该出现 `@` 这种标记符号 ——
    模型看到 `@小区` 会当成某种特殊 token，反而可能不去查图层清单。
    名字本身要留着：「对小区做 300 米缓冲区」规则引擎照样认得。
    """
    if not mentions:
        return text
    out = text
    for mn in sorted(mentions, key=lambda m: m.start, reverse=True):
        out = out[:mn.start] + mn.name + out[mn.end:]
    return out


def layer_context(mentions: list[Mention]) -> str:
    """把提及整理成一段给模型看的说明。

    大模型模式下，光把 `@` 去掉还不够 —— 模型得知道用户**点名**了哪些图层，
    否则「小区」和「小区_合并」它还是会挑错。
    """
    resolved = [m for m in mentions if m.resolved]
    if not resolved:
        return ""
    names = []
    for m in resolved:
        nm = m.layer.name if m.layer is not None else m.name
        names.append(nm)
    uniq = []
    for n in names:
        if n not in uniq:
            uniq.append(n)
    return ("用户用 @ 明确指定了图层：" + "、".join(uniq)
            + "。请严格按这些图层名来取数据，不要换成名字相近的别的图层。")


def mentions_note(mentions: list[Mention]) -> str:
    """给模型看的提示前缀（说明用户点名了哪些图层）。

    和 `layer_context` 一样的作用，但返回的是可以直接拼在正文前面的短句。
    """
    return layer_context(mentions)


def ambiguity_question(mentions: list[Mention]) -> dict | None:
    """有歧义的提及 → 一个澄清问题（question + options）。

    和 Agent 现有的 clarify 机制一样：问用户选哪个，而不是自己猜。
    """
    amb = [m for m in mentions if m.candidates]
    if not amb:
        return None
    m = amb[0]
    options = [lay.name for lay in m.candidates]
    return {
        "question": f"「{m.name}」指的是哪个图层？",
        "options": options,
        "mention": m,
    }


def not_found_note(mentions: list[Mention]) -> str:
    """没解析到的提及 → 给用户的一句话说明。"""
    bad = [m for m in mentions if not m.resolved and not m.candidates]
    if not bad:
        return ""
    names = "、".join(f"「{m.name}」" for m in bad)
    return f"注意：{names} 没有对应的图层，请检查名字。"
