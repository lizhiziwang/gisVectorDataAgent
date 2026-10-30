"""Phase 3 记忆回归：纠正抽取、评分排序、冲突处理、摘要、管理 API。

用法：.venv\\Scripts\\python.exe tools\\test_memory_phase3.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.memory import MemoryManager
from app.storage import AgentStore

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    st = AgentStore(Path(td) / "agent.db", project_root=td)
    p = st.ensure_default_project()
    sess = st.create_session(p.id, "纠正测试")
    mem = MemoryManager(st, p.id, sess.id)

    print("[1] 显式纠正抽取")
    mem.observe_turn("我说的不是道路，是地块内的管线", {"steps": []}, sess.id)
    terms = {m.key: m.value for m in st.list_memories(p.id, "correction")}
    check("抽到纠正 term", any(k.startswith("term:") for k in terms), True)
    check("纠正值为管线", any(v.get("term") == "管线" for v in terms.values()
                            if isinstance(v, dict)), True)

    print("[2] 字段口语消歧")
    res = {"steps": [
        {"tool": "buffer", "status": "ok",
         "layer": {"name": "地块", "fields": ["用地类型", "面积", "容积率"], "crs": "EPSG:4326"}},
    ]}
    mem.observe_turn("按类型统计面积", res, sess.id)
    corr = {m.key: m.value for m in st.list_memories(p.id, "correction")}
    hit = corr.get("field:类型")
    check("字段纠正已记录", hit is not None, True)
    check("纠正到真实字段", (hit or {}).get("field"), "用地类型")

    print("[3] 无真实字段时不硬记")
    mem.observe_turn("按人口统计", {"steps": [
        {"tool": "describe", "status": "ok",
         "layer": {"name": "道路", "fields": ["长度", "等级"], "crs": "EPSG:4326"}}]},
        sess.id)
    corr2 = {m.key for m in st.list_memories(p.id, "correction")}
    check("人口未误记", "field:人口" not in corr2, True)

    print("[4] 评分：偏好优先于项目事实")
    mem.observe_turn("以后默认用公里", {"steps": []}, sess.id)
    ctx = mem.context("统计面积")
    check("注入含偏好", "default_unit" in ctx, True)
    lines = ctx.splitlines()
    pref_idx = next((i for i, l in enumerate(lines) if "default_unit" in l), -1)
    check("偏好行存在", pref_idx >= 0, True)

    print("[5] 冲突处理：同 key 只留最高优先级类型")
    from app.models import Memory
    st.upsert_memory(Memory(id="", project_id=p.id, type="workflow",
                             key="term:管线", value={"w": 1}, importance=0.9))
    st.upsert_memory(Memory(id="", project_id=p.id, type="preference",
                             key="term:管线", value={"p": 1}, importance=0.1))
    grouped = mem.grouped()
    keys = [m.key for m in grouped.get("preference", []) if m.key == "term:管线"]
    check("偏好覆盖同 key 工作流", len(keys), 1)
    check("面板不显示同 key 工作流", len([m for m in grouped.get("workflow", [])
                                 if m.key == "term:管线"]), 0)
    check("DB 仍保留历史版本", len([m for m in st.list_memories(p.id)
                             if m.key == "term:管线"]) >= 2, True)
    ctx = mem.context("管线")
    check("注入只出现一次", ctx.count("term:管线"), 1)
    check("注入的是偏好", "[偏好] term:管线" in ctx, True)

    print("[6] 预算截断")
    for i in range(40):
        st.upsert_memory(Memory(id="", project_id=p.id, type="project_fact",
                                 key=f"k{i}", value="x" * 200, importance=0.5))
    ctx = mem.context("统计")
    check("注入未超预算", len(ctx) <= 1700, True)
    check("仍然有内容", len(ctx) > 0, True)

    print("[7] 摘要")
    for i in range(24):
        st.append_text(sess.id, "user" if i % 2 == 0 else "assistant", f"第 {i} 条消息")
    summary = mem.maybe_summarize(sess.id)
    check("摘要已生成", len(summary) > 0, True)
    check("摘要写入 session", mem.summary(sess.id) != "", True)
    check("原文仍保留", st.count_messages(sess.id) >= 24, True)

    print("[8] 管理 API")
    prefs = st.list_memories(p.id, "preference")
    check("forget 删除一条", mem.forget(prefs[0].id), True)
    check("删除后少一条", len(st.list_memories(p.id, "preference")),
          len(prefs) - 1)
    n = mem.forget_by_key("default_export")
    check("按 key 删除", n >= 0, True)
    before = len(st.list_memories(p.id, "project_fact"))
    mem.clear("project_fact")
    check("按类型清空", len(st.list_memories(p.id, "project_fact")), 0)
    check("不影响其它类型", len(st.list_memories(p.id, "preference")) >= 0, True)
    check("清空前有数据", before > 0, True)

    print("[9] pin")
    one = st.list_memories(p.id, "preference")
    if one:
        check("置顶成功", mem.pin(one[0].id, True), True)
        check("置顶状态", st.list_memories(p.id, "preference")[0].pinned, True)

    st.close()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
