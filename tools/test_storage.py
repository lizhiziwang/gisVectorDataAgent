"""Phase 1 存储层回归：迁移、会话/消息、运行/事件、图层、记忆、级联删除。

用法：.venv\\Scripts\\python.exe tools\\test_storage.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.models import LayerRecord, Memory, Part, Step
from app.storage import AgentStore

fails: list[str] = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    db = Path(td) / "agent.db"
    s = AgentStore(db, project_root=td)

    print("[1] 迁移/默认项目/会话")
    check("初始为空", s.stats()["sessions"], 0)
    p = s.ensure_default_project()
    check("项目已建", bool(p.id), True)
    s2 = AgentStore(db, project_root=td)
    check("重复打开不重复建项目", len(s2.list_sessions(p.id)), 0)
    check("迁移幂等", s2.stats()["projects"], 1)
    se = s.create_session(p.id, "测试会话")
    check("会话已建", s.get_session(se.id).title, "测试会话")
    check("latest 会话", s.latest_session(p.id).id, se.id)
    s2.close()

    print("[2] 消息/parts")
    m1 = s.append_message(se.id, "user", [Part(type="text", content={"text": "对地块做缓冲"})])
    m2 = s.append_message(se.id, "assistant", [
        Part(type="text", content={"text": "执行了 1 步"}),
        Part(type="layer_ref", content={"layer_id": "L1", "name": "地块_缓冲"}),
    ])
    msgs = s.list_messages(se.id)
    check("消息顺序", [m.role for m in msgs], ["user", "assistant"])
    check("parts 数量", len(msgs[1].parts), 2)
    check("layer_ref 保留", msgs[1].parts[1].content["layer_id"], "L1")
    check("latest text", s.latest_messages_text(se.id, 2)[-1]["content"], "执行了 1 步")

    print("[3] 运行/步骤/事件/session state")
    r = s.create_run(se.id, kind="chat", message_id=m1.id)
    s.update_run(r.id, status="running", started_at=1.0)
    st = s.append_step(r.id, Step(id="", run_id=r.id, ordinal=None, tool="buffer",
                                  args={"distance": 500}, status="ok",
                                  message="ok", data={"features_out": 2}))
    s.append_event(r.id, "plan.created", {"steps": 1}, session_id=se.id)
    s.append_event(r.id, "step.finished", {"tool": "buffer"}, session_id=se.id)
    check("run 状态", s.get_run(r.id).status, "running")
    check("step 序号/工具", (st.ordinal, st.tool), (0, "buffer"))
    check("事件 seq", [e.seq for e in s.list_events(r.id)], [1, 2])
    s.save_session_state(se.id, {"planner_memory": {"last_tool": "buffer"}})
    check("state roundtrip", s.session_state(se.id)["planner_memory"]["last_tool"], "buffer")

    print("[4] 图层元信息")
    s.upsert_layer(LayerRecord(id="L1", project_id=p.id, name="地块_缓冲",
                               parent_id="L0", op="buffer", crs="EPSG:4544",
                               geom_type="Polygon", feature_count=2,
                               bounds=[0, 0, 1, 1], fields=["a"]))
    layers = s.list_layers(p.id)
    check("图层入库", (layers[0].id, layers[0].feature_count), ("L1", 2))
    s.mark_layer_deleted("L1")
    check("逻辑删除", s.list_layers(p.id), [])

    print("[5] 记忆 upsert/搜索/删除")
    s.upsert_memory(Memory(id="", project_id=p.id, type="preference",
                           key="default_unit", value="米", importance=0.9, pinned=True))
    s.upsert_memory(Memory(id="", project_id=p.id, type="preference",
                           key="default_unit", value="公里", importance=0.9))
    mems = s.list_memories(p.id)
    check("同 key 覆盖不重复", len(mems), 1)
    check("覆盖后值", mems[0].value, "公里")
    s.upsert_memory(Memory(id="", project_id=p.id, type="project_fact",
                           key="地块字段", value="用地类型/容积率"))
    hits = s.search_memories(p.id, "用地类型")
    check("中文检索命中", any(m.key == "地块字段" for m in hits), True)
    s.delete_memory(mems[0].id)
    check("删除记忆", len(s.list_memories(p.id)), 1)

    print("[6] 级联删除")
    s.delete_session(se.id)
    check("消息级联删除", s.list_messages(se.id), [])
    check("run 级联删除", s.get_run(r.id), None)

    s.close()

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
