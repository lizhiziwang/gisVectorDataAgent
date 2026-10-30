"""记忆管理：规则提取 + 检索 + prompt 注入。

第一版刻意保持「可解释」：
· 用户偏好（默认单位/CRS/导出格式）用正则提取；
· 新图层的事实（名称/字段/CRS/要素数）从 run steps 的 layer meta 提取；
· 成功且多步的计划存成 workflow；
· 检索用 SQLite FTS + 结构化 key，截断到预算，带来源类型。

不存原始 GeoDataFrame/大表/密钥；LLM 提取以后再加，先把规则层做稳。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from .models import Memory
from .storage import AgentStore

TYPE_LABEL = {
    "preference": "偏好",
    "project_fact": "项目",
    "workflow": "工作流",
    "correction": "纠正",
}

#: 记忆类型之间的冲突优先级（数字大者胜）。
#: 设计文档 §6.3：显式用户偏好 > 最近 correction > project_fact > workflow
TYPE_PRIOR = {
    "preference": 4,
    "correction": 3,
    "project_fact": 2,
    "workflow": 1,
}

#: 常见口语纠正：「按类型」实际指「用地类型」这类字段名消歧。
#: key 是用户可能说的口语，value 是候选真实字段名（命中后仍需确认字段存在）。
FIELD_ALIASES = {
    "类型": ["用地类型", "土地类型", "地类", "分类", "类别"],
    "面积": ["面积", "用地面积", "建筑面积", "占地面积"],
    "长度": ["长度", "道路长度", "管线长度"],
    "数量": ["数量", "个数", "条数", "数目"],
    "人口": ["人口", "常住人口", "人口数"],
}


class MemoryManager:
    def __init__(self, store: AgentStore, project_id: str, session_id: str | None = None) -> None:
        self.store = store
        self.project_id = project_id
        self.session_id = session_id

    # ------------------------------------------------------------------ 写入
    def observe_turn(self, user_text: str, result: dict,
                     session_id: str | None = None) -> list[Memory]:
        """从一轮对话/执行结果里抽取可复用记忆。"""
        sid = session_id or self.session_id
        written: list[Memory] = []
        text = (user_text or "").strip()
        if text:
            written += self._preferences(text, sid, result)
            written += self._corrections(text, sid, result)
        steps = (result or {}).get("steps") or []
        if steps:
            written += self._layer_facts(steps, sid, result)
            written += self._workflow(text, steps, sid, result)
        return written

    def _preferences(self, text: str, sid: str | None, result: dict) -> list[Memory]:
        out: list[Memory] = []
        if not re.search(r"以后|默认|都用|统一|一律", text):
            return out
        # 距离单位
        m = re.search(r"(?:默认|以后|都用|统一|一律)[^。；\n]{0,12}?(米|公里|千米|km)", text, re.I)
        if m:
            unit = "km" if m.group(1).lower() in ("公里", "千米", "km") else "m"
            out.append(self._upsert("preference", "default_unit", unit,
                                    source_run=(result or {}).get("run_id"), sid=sid))
        # 导出格式
        m = re.search(r"(?:默认|以后|都用|统一|一律)[^。；\n]{0,12}?(geojson|gpkg|geopackage|shapefile|shp|csv)", text, re.I)
        if m:
            fmt = {"geopackage": "gpkg", "shapefile": "shp"}.get(m.group(1).lower(),
                                                                 m.group(1).lower())
            out.append(self._upsert("preference", "default_export", fmt,
                                    source_run=(result or {}).get("run_id"), sid=sid))
        # CRS
        m = re.search(r"(?:默认|以后|都用|统一|一律)[^。；\n]{0,20}?(CGCS\s*2000|WGS\s*84|EPSG\s*[:：]?\s*(\d{4,6}))", text, re.I)
        if m:
            crs = f"EPSG:{m.group(2)}" if m.group(2) else ("CGCS2000" if "2000" in m.group(1) else "WGS84")
            out.append(self._upsert("preference", "default_crs", crs,
                                    source_run=(result or {}).get("run_id"), sid=sid))
        return out

    def _layer_facts(self, steps: list[dict], sid: str | None, result: dict) -> list[Memory]:
        out: list[Memory] = []
        for step in steps:
            lay = (step or {}).get("layer")
            if not isinstance(lay, dict):
                continue
            name = str(lay.get("name") or "")
            if not name:
                continue
            value = {
                "name": name,
                "fields": list(lay.get("fields") or [])[:30],
                "crs": lay.get("crs") or "",
                "count": lay.get("count") or 0,
                "geomType": lay.get("geomType") or "",
            }
            out.append(self._upsert("project_fact", f"layer:{name}", value,
                                    source_run=(result or {}).get("run_id"), sid=sid,
                                    importance=0.6))
        return out

    def _workflow(self, text: str, steps: list[dict], sid: str | None, result: dict) -> list[Memory]:
        if len(steps) < 2 or any(s.get("status") == "error" for s in steps):
            return []
        plan = [{"tool": s.get("tool"), "args": s.get("args") or {}} for s in steps
                if s.get("tool") not in ("describe", "export")]
        if len(plan) < 2:
            return []
        key = "workflow:" + hashlib.sha1(
            (text or " ".join(str(p.get("tool")) for p in plan)).encode("utf-8")).hexdigest()[:12]
        return [self._upsert("workflow", key, {"intent": text, "plan": plan},
                             source_run=(result or {}).get("run_id"), sid=sid,
                             importance=0.5)]

    def _corrections(self, text: str, sid: str | None, result: dict) -> list[Memory]:
        """抽取用户对规划/字段/参数的纠正。

        两类来源：
        1. 显式纠正语（「不是 X，是 Y」「我说的是 Y」「应该按 Y 算」）；
        2. 字段口语消歧（「按类型」→ 真实字段「用地类型」），需要图层里真有该字段。
        """
        out: list[Memory] = []
        run_id = (result or {}).get("run_id")
        # 1) 显式纠正：「不是 X，是 Y」→ 记 Y（用户真正想要的那个）
        not_pat = re.compile(
            r"(?:不是|不是用|应该是|其实是|我要的是|我是说)\s*"
            r"([^，,。；;：:\s]{1,12})\s*"
            r"(?:而是|，是|,是|，应该|,应该|，其实|,其实|，改用|,改用|，换成|,换成)\s*"
            r"([^，,。；;：:\s]{1,12})")
        for m in not_pat.finditer(text):
            target = self._trim_term(m.group(2))
            if not target:
                continue
            out.append(self._upsert("correction", f"term:{target}", {"term": target},
                                     source_run=run_id, sid=sid, importance=0.6))
        # 1b) 「应该按 X 统计」这类：只记后面的目标
        should_pat = re.compile(
            r"(?:应该|要|想|需要)(?:按|用|以)\s*([^，,。；;：:\s]{1,12}?)\s*"
            r"(?:统计|算|计算|做|来|分)")
        for m in should_pat.finditer(text):
            target = self._trim_term(m.group(1))
            if not target:
                continue
            out.append(self._upsert("correction", f"term:{target}", {"term": target},
                                     source_run=run_id, sid=sid, importance=0.6))
        # 2) 字段口语消歧
        for alias, candidates in FIELD_ALIASES.items():
            if alias not in text:
                continue
            real = self._match_field(alias, candidates, result)
            if real:
                out.append(self._upsert(
                    "correction", f"field:{alias}", {"alias": alias, "field": real},
                    source_run=run_id, sid=sid, importance=0.65))
        return out

    @staticmethod
    def _trim_term(raw: str) -> str:
        """从「地块内的管线」这种长短语里取出真正想要的术语。

        规则：砍掉方位/数量修饰前缀（「地块内的」「每个」「所有」），
        再砍掉尾部动词性后缀（「图层」「字段」），留下核心名词。
        """
        s = (raw or "").strip()
        if not s:
            return ""
        # 砍掉结尾的「图层/字段/表/列」
        for suf in ("图层", "字段", "表", "列", "要素"):
            if s.endswith(suf) and len(s) > len(suf):
                s = s[:-len(suf)]
                break
        # 砍掉方位修饰：取最后一个「的」之后的片段
        if "的" in s:
            tail = s.rsplit("的", 1)[1].strip()
            if tail:
                s = tail
        # 砍掉数量/范围前缀
        for pre in ("每个", "所有", "全部", "整个", "这些", "那些"):
            if s.startswith(pre):
                s = s[len(pre):].strip()
        # 太长就砍掉（记忆里只留短术语）
        if len(s) > 8:
            s = s[:8]
        return s.strip()

    def _match_field(self, alias: str, candidates: list[str], result: dict) -> str:
        """在本次结果涉及的图层字段里找最贴近 alias 的真实字段名。"""
        fields: list[str] = []
        for step in (result or {}).get("steps") or []:
            lay = (step or {}).get("layer")
            if isinstance(lay, dict):
                fields.extend(str(f) for f in (lay.get("fields") or []))
        if not fields:
            return ""
        for cand in candidates:
            if cand in fields:
                return cand
        # 退一步：包含关系（"类型" 命中 "用地类型"）
        for f in fields:
            if alias in f:
                return f
        return ""

    def _upsert(self, type_: str, key: str, value: Any, source_run: str | None = None,
                sid: str | None = None, importance: float = 0.7) -> Memory:
        mem = Memory(id="", project_id=self.project_id, type=type_, key=key, value=value,
                     session_id=sid, source_run_id=source_run, importance=importance,
                     created_at=time.time())
        return self.store.upsert_memory(mem)

    # ------------------------------------------------------------------ 读取
    def context(self, query: str, budget_chars: int = 1600) -> str:
        """检索并拼成可注入 prompt 的 <MEMORY> 块；没有命中返回空串。"""
        try:
            mems = self._retrieve(query)
        except Exception:
            return ""
        if not mems:
            return ""
        lines: list[str] = []
        used = 0
        picked: list[Memory] = []
        for m in mems:
            try:
                val = m.value if isinstance(m.value, str) else json.dumps(
                    m.value, ensure_ascii=False, default=str)
            except Exception:
                val = str(m.value)
            val = " ".join(str(val).split())[:300]
            label = TYPE_LABEL.get(m.type, m.type)
            line = f"[{label}] {m.key}: {val}"
            if used + len(line) > budget_chars:
                continue          # 换下一条更短的，而不是直接丢弃全部
            lines.append(line)
            picked.append(m)
            used += len(line)
        if not lines:
            return ""
        try:
            self.store.touch_memories([m.id for m in picked])
        except Exception:
            pass
        return "<MEMORY>\n" + "\n".join(lines) + "\n</MEMORY>"

    def _retrieve(self, query: str) -> list[Memory]:
        """候选召回 + 打分 + 冲突处理。

        评分（设计文档 §6.3）：
            score = 0.50*fts + 0.20*recency + 0.15*importance
                  + 0.10*type_prior + 0.05*pinned
        冲突：同 key 保留最高优先级的类型（偏好 > 纠正 > 项目事实 > 工作流）。
        """
        try:
            mems = self.store.search_memories(self.project_id, query, limit=30)
        except Exception:
            mems = []
        # 偏好 / 纠正 一定要带上：它们通常很短，且与查询词不一定字面匹配
        for type_ in ("preference", "correction"):
            try:
                extra = self.store.list_memories(self.project_id, type_, limit=20)
            except Exception:
                extra = []
            known = {m.id for m in mems}
            mems.extend(m for m in extra if m.id not in known)
        now = time.time()
        live = [m for m in mems if not (m.expires_at and m.expires_at < now)]
        scored = sorted(live, key=lambda m: self._score(m, query, now), reverse=True)
        return self._resolve_conflicts(scored)

    @staticmethod
    def _score(m: Memory, query: str, now: float) -> float:
        """记忆打分。"""
        # 关键词命中：query 里出现了记忆的 key 或值文本
        text = (query or "")
        hit = 0.0
        if m.key and m.key in text:
            hit += 1.0
        try:
            val = m.value if isinstance(m.value, str) else json.dumps(
                m.value, ensure_ascii=False, default=str)
        except Exception:
            val = str(m.value)
        if val and any(tok and tok in val for tok in re.split(r"\s+", text) if len(tok) >= 2):
            hit += 0.5
        fts = min(1.0, hit)
        # 新近度：7 天内线性衰减
        age = max(0.0, now - (m.last_used_at or m.created_at))
        recency = max(0.0, 1.0 - age / (7 * 24 * 3600))
        importance = max(0.0, min(1.0, m.importance))
        type_prior = TYPE_PRIOR.get(m.type, 0) / 4.0
        pinned = 1.0 if m.pinned else 0.0
        return (0.50 * fts + 0.20 * recency + 0.15 * importance
                + 0.10 * type_prior + 0.05 * pinned)

    @staticmethod
    def _resolve_conflicts(scored: list[Memory]) -> list[Memory]:
        """同 key 只保留优先级最高的类型，避免注入互相矛盾的指令。"""
        best: dict[str, Memory] = {}
        out: list[Memory] = []
        for m in scored:
            prev = best.get(m.key)
            if prev is None:
                best[m.key] = m
                out.append(m)
                continue
            if TYPE_PRIOR.get(m.type, 0) > TYPE_PRIOR.get(prev.type, 0):
                out[out.index(prev)] = m
                best[m.key] = m
        return out

    # ------------------------------------------------------------------ 摘要
    SUMMARIZE_AFTER_MESSAGES = 20
    SUMMARY_TOKEN_BUDGET = 1200

    def maybe_summarize(self, session_id: str | None = None) -> str:
        """消息数超阈值时把旧消息压缩成一句摘要写进 ``sessions.summary``。

        原文仍在 ``messages`` 表里，摘要只是检索入口；不做删除。
        返回新摘要（没生成则返回空串）。
        """
        sid = session_id or self.session_id
        if not sid or self.store is None:
            return ""
        try:
            total = self.store.count_messages(sid)
            if total < self.SUMMARIZE_AFTER_MESSAGES:
                return ""
            msgs = self.store.list_messages(sid, limit=200)
        except Exception:
            return ""
        if len(msgs) < 4:
            return ""
        # 只摘要较老的部分，保留最近 8 条原文
        old = msgs[:-8]
        lines: list[str] = []
        for m in old:
            txt = m.text().strip().replace("\n", " ")
            if not txt:
                continue
            lines.append(f"{'用户' if m.role == 'user' else '助手'}：{txt[:120]}")
        if not lines:
            return ""
        summary = "；".join(lines[-12:])
        summary = f"历史要点：{summary}"[: self.SUMMARY_TOKEN_BUDGET * 2]
        try:
            self.store.set_session_summary(sid, summary, len(summary) // 2)
        except Exception:
            return ""
        return summary

    # ------------------------------------------------------------------ 管理
    def forget(self, memory_id: str) -> bool:
        """「忘记这条」：删掉一条记忆。"""
        if self.store is None or not memory_id:
            return False
        try:
            self.store.delete_memory(memory_id)
            return True
        except Exception:
            return False

    def forget_by_key(self, key: str, type_: str = "") -> int:
        """按 key 忘掉记忆（「以后别再用米」这类）。返回删除条数。"""
        if self.store is None or not key:
            return 0
        try:
            mems = self.store.list_memories(self.project_id, type_ or None, limit=500)
        except Exception:
            return 0
        n = 0
        for m in mems:
            if m.key == key or m.key.endswith(":" + key):
                if self.forget(m.id):
                    n += 1
        return n

    def clear(self, type_: str = "") -> int:
        """清空项目记忆（可按类型）。返回删除条数。"""
        if self.store is None:
            return 0
        try:
            mems = self.store.list_memories(self.project_id, type_ or None, limit=1000)
        except Exception:
            return 0
        n = 0
        for m in mems:
            if self.forget(m.id):
                n += 1
        return n

    def pin(self, memory_id: str, pinned: bool = True) -> bool:
        """置顶/取消置顶一条记忆。"""
        if self.store is None or not memory_id:
            return False
        try:
            mems = self.store.list_memories(self.project_id, None, limit=1000)
            for m in mems:
                if m.id == memory_id:
                    m.pinned = pinned
                    self.store.upsert_memory(m)
                    return True
        except Exception:
            return False
        return False

    def grouped(self, limit: int = 300) -> dict[str, list[Memory]]:
        """按类型分组，供记忆面板展示。

        同 key 的多条会先做冲突消解（只显示优先级最高的那条），
        避免面板和实际注入给模型的内容不一致。
        """
        out: dict[str, list[Memory]] = {}
        if self.store is None:
            return out
        try:
            mems = self.store.list_memories(self.project_id, None, limit=limit)
        except Exception:
            return out
        for m in self._resolve_conflicts(list(mems)):
            out.setdefault(m.type, []).append(m)
        return out

    def summary(self, session_id: str | None = None) -> str:
        """取会话摘要。"""
        sid = session_id or self.session_id
        if self.store is None or not sid:
            return ""
        try:
            sess = self.store.get_session(sid)
            return (sess.summary or "") if sess else ""
        except Exception:
            return ""
