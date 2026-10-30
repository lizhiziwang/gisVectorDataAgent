"""记忆管理回归：偏好/图层事实/工作流提取与检索注入。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.memory import MemoryManager
from app.storage import AgentStore

fails = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    st = AgentStore(Path(td) / "agent.db", project_root=td)
    p = st.ensure_default_project()
    sess = st.create_session(p.id, "记忆测试")
    mem = MemoryManager(st, p.id, sess.id)

    print("[1] 用户偏好")
    mem.observe_turn("以后默认用米，导出都用 gpkg", {"steps": []}, sess.id)
    prefs = {m.key: m.value for m in st.list_memories(p.id, "preference")}
    check("单位偏好", prefs.get("default_unit"), "m")
    check("导出偏好", prefs.get("default_export"), "gpkg")

    print("[2] 图层事实")
    result = {"steps": [
        {"tool": "buffer", "status": "ok",
         "layer": {"name": "地块_缓冲", "fields": ["用地类型", "面积"], "crs": "EPSG:4544",
                   "count": 665, "geomType": "Polygon"}},
    ]}
    mem.observe_turn("对地块做缓冲区", result, sess.id)
    facts = {m.key: m.value for m in st.list_memories(p.id, "project_fact")}
    check("图层事实 key", "layer:地块_缓冲" in facts, True)
    check("字段被记录", "用地类型" in facts["layer:地块_缓冲"]["fields"], True)

    print("[3] 工作流")
    plan = {"steps": [
        {"tool": "buffer", "status": "ok", "args": {"layer": "地块", "distance": 500}},
        {"tool": "area_stats", "status": "ok", "args": {"layer": "$prev"}},
    ]}
    mem.observe_turn("对地块缓冲后统计面积", plan, sess.id)
    workflows = st.list_memories(p.id, "workflow")
    check("工作流已记录", len(workflows), lambda n: n >= 1)

    print("[4] 检索/注入")
    ctx = mem.context("用地类型 默认单位")
    check("注入包含 MEMORY", ctx.startswith("<MEMORY>"), True)
    check("注入包含字段事实", "用地类型" in ctx, True)
    check("注入包含偏好", "default_unit" in ctx, True)
    check("预算截断", len(ctx) < 1700, True)
    mem.context("用地类型")
    touched = [m for m in st.list_memories(p.id) if m.last_used_at]
    check("last_used 更新", len(touched), lambda n: n >= 1)

    print("[5] 删除")
    for m in st.list_memories(p.id, "preference"):
        st.delete_memory(m.id)
    check("偏好已删除", st.list_memories(p.id, "preference"), [])

    st.close()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
