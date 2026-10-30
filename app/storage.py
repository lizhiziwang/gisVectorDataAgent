"""SQLite 持久化：会话、消息、运行、步骤、事件、图层元信息、记忆。

设计原则
--------
· 只存元信息/结构化内容；GeoDataFrame、导出文件、图片放磁盘，DB 只存指针；
· WAL + 外键 + busy_timeout，适合桌面端「UI 线程读、后台线程写」；
· 每个线程一个连接（sqlite3 连接不能跨线程），写操作用一把锁串行化；
· 迁移用 PRAGMA user_version，启动时顺序执行，失败整体回滚；
· 记忆检索优先 FTS5 trigram（中文），不可用时退回 LIKE。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

from .models import (Event, LayerRecord, Memory, Message, Part, Project, Run,
                     Session, Step, new_id, now)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "agent.db"

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  root TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  parent_id TEXT,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'idle',
  model TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  summary_tokens INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  message_id TEXT,
  kind TEXT NOT NULL DEFAULT 'chat',
  status TEXT NOT NULL DEFAULT 'queued',
  priority INTEGER NOT NULL DEFAULT 50,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  model TEXT NOT NULL DEFAULT '',
  usage_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT,
  role TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  created_at REAL NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ordinal);

CREATE TABLE IF NOT EXISTS parts (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  type TEXT NOT NULL,
  content_json TEXT NOT NULL DEFAULT '{}',
  artifact_id TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parts_message ON parts(message_id, ordinal);

CREATE TABLE IF NOT EXISTS steps (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  data_json TEXT NOT NULL DEFAULT '{}',
  layer_id TEXT,
  elapsed_ms INTEGER NOT NULL DEFAULT 0,
  started_at REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id, ordinal);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
  session_id TEXT,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);

CREATE TABLE IF NOT EXISTS layers (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  parent_id TEXT NOT NULL DEFAULT '',
  op TEXT NOT NULL DEFAULT '',
  crs TEXT NOT NULL DEFAULT '',
  geom_type TEXT NOT NULL DEFAULT '',
  feature_count INTEGER NOT NULL DEFAULT 0,
  bounds_json TEXT NOT NULL DEFAULT '[]',
  fields_json TEXT NOT NULL DEFAULT '[]',
  cache_path TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  created_by_run TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  deleted_at REAL
);
CREATE INDEX IF NOT EXISTS idx_layers_project ON layers(project_id, created_at);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  type TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL DEFAULT 'null',
  value_text TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.8,
  importance REAL NOT NULL DEFAULT 0.5,
  source_message_id TEXT,
  source_run_id TEXT,
  created_at REAL NOT NULL,
  last_used_at REAL,
  expires_at REAL,
  pinned INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id, type, key)
);
CREATE INDEX IF NOT EXISTS idx_memories_lookup ON memories(project_id, type, key);

CREATE TABLE IF NOT EXISTS session_state (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  state_json TEXT NOT NULL DEFAULT '{}',
  updated_at REAL NOT NULL
);
"""


class AgentStore:
    def __init__(self, path: str | Path | None = None,
                 project_root: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("GIS_AGENT_DB") or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.project_root = str(project_root or ROOT)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.fts_enabled = True
        self._init()

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
        return c

    @contextmanager
    def _write(self):
        with self._write_lock:
            c = self.conn()
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise

    def _init(self) -> None:
        c = self._connect()
        try:
            c.executescript(SCHEMA_V1)
            version = int(c.execute("PRAGMA user_version").fetchone()[0])
            if version < 1:
                c.execute("PRAGMA user_version=1")
                c.commit()
            self._init_fts(c)
        finally:
            c.close()

    def _init_fts(self, c: sqlite3.Connection) -> None:
        """FTS5 是可选能力：trigram / unicode61 都不支持时退回 LIKE。"""
        for tokenizer in ("trigram", "unicode61"):
            try:
                c.executescript(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                  key, value_text, content='memories', content_rowid='rowid',
                  tokenize='{tokenizer}');
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                  INSERT INTO memories_fts(rowid, key, value_text)
                  VALUES (new.rowid, new.key, new.value_text);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts, rowid, key, value_text)
                  VALUES ('delete', old.rowid, old.key, old.value_text);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts, rowid, key, value_text)
                  VALUES ('delete', old.rowid, old.key, old.value_text);
                  INSERT INTO memories_fts(rowid, key, value_text)
                  VALUES (new.rowid, new.key, new.value_text);
                END;
                """)
                c.commit()
                self.fts_enabled = True
                return
            except sqlite3.OperationalError:
                continue
        self.fts_enabled = False
        try:
            c.execute("DROP TABLE IF EXISTS memories_fts")
            c.commit()
        except Exception:
            pass

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self._local.conn = None

    # ------------------------------------------------------------------ 项目/会话
    def ensure_default_project(self, name: str = "默认项目") -> Project:
        c = self.conn()
        row = c.execute("SELECT * FROM projects ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            pid = new_id("proj_")
            t = now()
            with self._write() as cw:
                cw.execute("INSERT INTO projects(id,name,root,created_at,updated_at,settings_json) "
                           "VALUES(?,?,?,?,?,?)",
                           (pid, name, self.project_root, t, t, "{}"))
            row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return self._project(row)

    def create_session(self, project_id: str, title: str = "新会话",
                       parent_id: str | None = None) -> Session:
        sid = new_id("sess_")
        t = now()
        with self._write() as c:
            c.execute("INSERT INTO sessions(id,project_id,parent_id,title,status,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (sid, project_id, parent_id, title, "idle", t, t))
        return self.get_session(sid)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> Session | None:
        row = self.conn().execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return self._session(row) if row else None

    def latest_session(self, project_id: str) -> Session | None:
        row = self.conn().execute(
            "SELECT * FROM sessions WHERE project_id=? ORDER BY updated_at DESC LIMIT 1",
            (project_id,)).fetchone()
        return self._session(row) if row else None

    def list_sessions(self, project_id: str, limit: int = 50) -> list[Session]:
        rows = self.conn().execute(
            "SELECT * FROM sessions WHERE project_id=? ORDER BY updated_at DESC LIMIT ?",
            (project_id, limit)).fetchall()
        return [self._session(r) for r in rows]

    def touch_session(self, session_id: str, status: str | None = None,
                      title: str | None = None) -> None:
        sets, vals = ["updated_at=?"], [now()]
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        if title is not None:
            sets.append("title=?")
            vals.append(title)
        vals.append(session_id)
        with self._write() as c:
            c.execute(f"UPDATE sessions SET {','.join(sets)} WHERE id=?", vals)

    def delete_session(self, session_id: str) -> None:
        with self._write() as c:
            c.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    # ------------------------------------------------------------------ 消息/parts
    def append_message(self, session_id: str, role: str, parts: Iterable[Part | dict],
                       run_id: str | None = None, metadata: dict | None = None,
                       message_id: str | None = None) -> Message:
        plist = [p if isinstance(p, Part) else Part(**p) for p in (parts or [])]
        mid = message_id or new_id("msg_")
        t = now()
        with self._write() as c:
            row = c.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM messages WHERE session_id=?",
                            (session_id,)).fetchone()
            ordinal = int(row[0])
            c.execute("INSERT INTO messages(id,session_id,run_id,role,ordinal,created_at,metadata_json) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (mid, session_id, run_id, role, ordinal, t,
                       json.dumps(metadata or {}, ensure_ascii=False, default=str)))
            for i, p in enumerate(plist):
                ordv = p.ordinal if p.ordinal else i
                c.execute("INSERT INTO parts(id,message_id,ordinal,type,content_json,artifact_id,created_at) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (p.id, mid, ordv, p.type,
                           json.dumps(p.content, ensure_ascii=False, default=str),
                           p.artifact_id, t))
            c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (t, session_id))
        return Message(id=mid, session_id=session_id, role=role, ordinal=ordinal,
                       created_at=t, parts=plist, run_id=run_id, metadata=dict(metadata or {}))

    def append_text(self, session_id: str, role: str, text: str,
                    run_id: str | None = None, **kw) -> Message:
        return self.append_message(session_id, role,
                                   [Part(type="text", content={"text": text})], run_id=run_id, **kw)

    def list_messages(self, session_id: str, limit: int = 50) -> list[Message]:
        c = self.conn()
        rows = c.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE session_id=? "
            "ORDER BY ordinal DESC LIMIT ?) ORDER BY ordinal ASC",
            (session_id, limit)).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        qmarks = ",".join("?" * len(ids))
        parts = c.execute(
            f"SELECT * FROM parts WHERE message_id IN ({qmarks}) ORDER BY message_id, ordinal",
            ids).fetchall()
        by_msg: dict[str, list[Part]] = {r["id"]: [] for r in rows}
        for p in parts:
            by_msg.setdefault(p["message_id"], []).append(Part(
                id=p["id"], type=p["type"],
                content=self._loads(p["content_json"], {}),
                ordinal=p["ordinal"], artifact_id=p["artifact_id"]))
        return [Message(id=r["id"], session_id=r["session_id"], role=r["role"],
                        ordinal=r["ordinal"], created_at=r["created_at"],
                        parts=by_msg.get(r["id"], []), run_id=r["run_id"],
                        metadata=self._loads(r["metadata_json"], {})) for r in rows]

    def latest_messages_text(self, session_id: str, limit: int = 6) -> list[dict]:
        """给 Planner 的 history 格式：只取 user/assistant 文本。"""
        out = []
        for m in self.list_messages(session_id, limit=limit * 2):
            if m.role not in ("user", "assistant"):
                continue
            txt = m.text()
            if txt:
                out.append({"role": m.role, "content": txt})
        return out[-limit:]

    def session_state(self, session_id: str) -> dict:
        row = self.conn().execute("SELECT state_json FROM session_state WHERE session_id=?",
                                  (session_id,)).fetchone()
        return self._loads(row["state_json"], {}) if row else {}

    def save_session_state(self, session_id: str, state: dict) -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO session_state(session_id,state_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET state_json=excluded.state_json, "
                "updated_at=excluded.updated_at",
                (session_id, json.dumps(state, ensure_ascii=False, default=str), now()))

    # ------------------------------------------------------------------ 运行/步骤/事件
    def create_run(self, session_id: str, kind: str = "chat", priority: int = 50,
                   message_id: str | None = None, model: str = "") -> Run:
        rid = new_id("run_")
        with self._write() as c:
            c.execute("INSERT INTO runs(id,session_id,message_id,kind,status,priority,model,created_at) "
                      "VALUES(?,?,?,?,?,?,?,?)",
                      (rid, session_id, message_id, kind, "queued", priority, model, now()))
        return Run(id=rid, session_id=session_id, kind=kind, status="queued",
                   priority=priority, message_id=message_id, model=model)

    def update_run(self, run_id: str, **fields) -> None:
        allowed = {"status", "cancel_requested", "error", "model", "usage", "started_at", "finished_at"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "usage":
                sets.append("usage_json=?")
                vals.append(json.dumps(v or {}, ensure_ascii=False, default=str))
            elif k == "cancel_requested":
                sets.append("cancel_requested=?")
                vals.append(1 if v else 0)
            else:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(run_id)
        with self._write() as c:
            c.execute(f"UPDATE runs SET {','.join(sets)} WHERE id=?", vals)

    def get_run(self, run_id: str) -> Run | None:
        row = self.conn().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._run(row) if row else None

    def list_runs(self, session_id: str, limit: int = 50) -> list[Run]:
        rows = self.conn().execute(
            "SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit)).fetchall()
        return [self._run(r) for r in rows]

    def append_step(self, run_id: str, step: Step | dict) -> Step:
        s = step if isinstance(step, Step) else Step(**step)
        with self._write() as c:
            row = c.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM steps WHERE run_id=?",
                            (run_id,)).fetchone()
            # ordinal 为 None 表示「自动接在最后一条后面」；
            # 显式给 0 是合法的（第一条步骤），不能当成自动。
            ordinal = int(row[0]) if s.ordinal is None else int(s.ordinal)
            sid = s.id or new_id("step_")
            c.execute(
                "INSERT INTO steps(id,run_id,ordinal,tool,args_json,status,message,warnings_json,"
                "data_json,layer_id,elapsed_ms,started_at,finished_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, run_id, ordinal, s.tool,
                 json.dumps(s.args or {}, ensure_ascii=False, default=str),
                 s.status, s.message or "",
                 json.dumps(s.warnings or [], ensure_ascii=False, default=str),
                 json.dumps(s.data or {}, ensure_ascii=False, default=str),
                 s.layer_id, int(s.elapsed_ms or 0), s.started_at, s.finished_at))
        return Step(id=sid, run_id=run_id, ordinal=ordinal, tool=s.tool, args=s.args,
                    status=s.status, message=s.message, warnings=s.warnings, data=s.data,
                    layer_id=s.layer_id, elapsed_ms=s.elapsed_ms,
                    started_at=s.started_at, finished_at=s.finished_at)

    def list_steps(self, run_id: str) -> list[Step]:
        rows = self.conn().execute("SELECT * FROM steps WHERE run_id=? ORDER BY ordinal",
                                   (run_id,)).fetchall()
        return [Step(id=r["id"], run_id=r["run_id"], ordinal=r["ordinal"], tool=r["tool"],
                     args=self._loads(r["args_json"], {}), status=r["status"],
                     message=r["message"], warnings=self._loads(r["warnings_json"], []),
                     data=self._loads(r["data_json"], {}), layer_id=r["layer_id"],
                     elapsed_ms=r["elapsed_ms"], started_at=r["started_at"],
                     finished_at=r["finished_at"]) for r in rows]

    def append_event(self, run_id: str | None, type_: str, payload: dict | None = None,
                     session_id: str | None = None) -> Event:
        # seq 必须在写锁里算：并发 append 时否则会拿到同一个 seq，
        # 事件回放（按 seq 排序）就会乱序。
        t = now()
        with self._write() as cw:
            row = cw.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id IS ?",
                             (run_id,)).fetchone()
            seq = int(row[0])
            cur = cw.execute("INSERT INTO events(run_id,session_id,seq,type,payload_json,created_at) "
                             "VALUES(?,?,?,?,?,?)",
                             (run_id, session_id, seq, type_,
                              json.dumps(payload or {}, ensure_ascii=False, default=str), t))
            eid = int(cur.lastrowid)
        return Event(id=eid, run_id=run_id or "", seq=seq, type=type_,
                     payload=payload or {}, session_id=session_id, created_at=t)

    def list_events(self, run_id: str, after_seq: int = 0) -> list[Event]:
        rows = self.conn().execute(
            "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq",
            (run_id, after_seq)).fetchall()
        return [Event(id=r["id"], run_id=r["run_id"] or "", session_id=r["session_id"],
                      seq=r["seq"], type=r["type"],
                      payload=self._loads(r["payload_json"], {}),
                      created_at=r["created_at"]) for r in rows]

    def list_session_events(self, session_id: str, limit: int = 200) -> list[Event]:
        rows = self.conn().execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit)).fetchall()
        return [Event(id=r["id"], run_id=r["run_id"] or "", session_id=r["session_id"],
                      seq=r["seq"], type=r["type"],
                      payload=self._loads(r["payload_json"], {}),
                      created_at=r["created_at"]) for r in reversed(rows)]

    def list_unfinished_runs(self, limit: int = 100) -> list[Run]:
        """未完成的 run（queued/running/waiting）——重启后用于标记 interrupted。"""
        rows = self.conn().execute(
            "SELECT * FROM runs WHERE status IN ('queued','running','waiting') "
            "ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        return [self._run(r) for r in rows]

    def list_recent_runs(self, session_id: str = "", limit: int = 50) -> list[Run]:
        if session_id:
            rows = self.conn().execute(
                "SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit)).fetchall()
        else:
            rows = self.conn().execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._run(r) for r in rows]

    def set_session_summary(self, session_id: str, summary: str,
                            summary_tokens: int = 0) -> None:
        with self._write() as c:
            c.execute("UPDATE sessions SET summary=?, summary_tokens=?, updated_at=? "
                      "WHERE id=?", (summary, int(summary_tokens), now(), session_id))

    def count_messages(self, session_id: str) -> int:
        row = self.conn().execute("SELECT COUNT(*) FROM messages WHERE session_id=?",
                                  (session_id,)).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------ 图层元信息
    def upsert_layer(self, rec: LayerRecord) -> None:
        with self._write() as c:
            c.execute(
                "INSERT INTO layers(id,project_id,name,parent_id,op,crs,geom_type,feature_count,"
                "bounds_json,fields_json,cache_path,content_hash,created_by_run,created_at,deleted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, parent_id=excluded.parent_id, "
                "op=excluded.op, crs=excluded.crs, geom_type=excluded.geom_type, "
                "feature_count=excluded.feature_count, bounds_json=excluded.bounds_json, "
                "fields_json=excluded.fields_json, cache_path=excluded.cache_path, "
                "content_hash=excluded.content_hash, deleted_at=excluded.deleted_at",
                (rec.id, rec.project_id, rec.name, rec.parent_id, rec.op, rec.crs,
                 rec.geom_type, rec.feature_count,
                 json.dumps(rec.bounds or [], ensure_ascii=False),
                 json.dumps(rec.fields or [], ensure_ascii=False),
                 rec.cache_path, rec.content_hash, rec.created_by_run,
                 rec.created_at, rec.deleted_at))

    def list_layers(self, project_id: str, include_deleted: bool = False) -> list[LayerRecord]:
        sql = "SELECT * FROM layers WHERE project_id=?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY created_at"
        rows = self.conn().execute(sql, (project_id,)).fetchall()
        return [LayerRecord(id=r["id"], project_id=r["project_id"], name=r["name"],
                            parent_id=r["parent_id"], op=r["op"], crs=r["crs"],
                            geom_type=r["geom_type"], feature_count=r["feature_count"],
                            bounds=self._loads(r["bounds_json"], []),
                            fields=self._loads(r["fields_json"], []),
                            cache_path=r["cache_path"], content_hash=r["content_hash"],
                            created_by_run=r["created_by_run"], created_at=r["created_at"],
                            deleted_at=r["deleted_at"]) for r in rows]

    def mark_layer_deleted(self, layer_id: str) -> None:
        with self._write() as c:
            c.execute("UPDATE layers SET deleted_at=? WHERE id=?", (now(), layer_id))

    # ------------------------------------------------------------------ 记忆
    def upsert_memory(self, mem: Memory) -> Memory:
        value_json = json.dumps(mem.value, ensure_ascii=False, default=str)
        value_text = str(mem.value) if isinstance(mem.value, str) else value_json
        mid = mem.id or new_id("mem_")
        with self._write() as c:
            c.execute(
                "INSERT INTO memories(id,project_id,session_id,type,key,value_json,value_text,"
                "confidence,importance,source_message_id,source_run_id,created_at,last_used_at,"
                "expires_at,pinned) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id,type,key) DO UPDATE SET value_json=excluded.value_json, "
                "value_text=excluded.value_text, confidence=excluded.confidence, "
                "importance=excluded.importance, source_message_id=excluded.source_message_id, "
                "source_run_id=excluded.source_run_id, expires_at=excluded.expires_at, "
                "pinned=excluded.pinned",
                (mid, mem.project_id, mem.session_id, mem.type, mem.key, value_json, value_text,
                 mem.confidence, mem.importance, mem.source_message_id, mem.source_run_id,
                 mem.created_at, mem.last_used_at, mem.expires_at, 1 if mem.pinned else 0))
            row = c.execute("SELECT * FROM memories WHERE project_id=? AND type=? AND key=?",
                            (mem.project_id, mem.type, mem.key)).fetchone()
        return self._memory(row)

    def list_memories(self, project_id: str, type_: str | None = None,
                      limit: int = 200) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE project_id=?"
        vals: list[Any] = [project_id]
        if type_:
            sql += " AND type=?"
            vals.append(type_)
        sql += " ORDER BY pinned DESC, importance DESC, COALESCE(last_used_at,created_at) DESC LIMIT ?"
        vals.append(limit)
        rows = self.conn().execute(sql, vals).fetchall()
        return [self._memory(r) for r in rows]

    def search_memories(self, project_id: str, query: str, limit: int = 20) -> list[Memory]:
        q = (query or "").strip()
        if not q:
            return self.list_memories(project_id, limit=limit)
        import re as _re
        tokens = [t for t in _re.split(r"\s+", q) if t][:8]
        found: dict[str, Memory] = {}
        if self.fts_enabled:
            try:
                fts_q = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
                rows = self.conn().execute(
                    "SELECT m.* FROM memories_fts f JOIN memories m ON m.rowid=f.rowid "
                    "WHERE memories_fts MATCH ? AND m.project_id=? LIMIT ?",
                    (fts_q, project_id, limit)).fetchall()
                for r in rows:
                    m = self._memory(r)
                    found[m.id] = m
            except sqlite3.OperationalError:
                pass
        if len(found) < limit:
            where = " OR ".join("(key LIKE ? OR value_text LIKE ?)" for _ in tokens)
            params: list[Any] = [project_id]
            for t in tokens:
                params.extend([f"%{t}%", f"%{t}%"])
            params.append(limit)
            rows = self.conn().execute(
                f"SELECT * FROM memories WHERE project_id=? AND ({where}) "
                "ORDER BY pinned DESC, importance DESC LIMIT ?",
                params).fetchall()
            for r in rows:
                m = self._memory(r)
                found.setdefault(m.id, m)
        out = list(found.values())
        out.sort(key=lambda m: (m.pinned, m.importance,
                                m.last_used_at or m.created_at), reverse=True)
        return out[:limit]

    def touch_memories(self, ids: list[str]) -> None:
        if not ids:
            return
        qmarks = ",".join("?" * len(ids))
        with self._write() as c:
            c.execute(f"UPDATE memories SET last_used_at=? WHERE id IN ({qmarks})",
                      [now(), *ids])

    def delete_memory(self, memory_id: str) -> None:
        with self._write() as c:
            c.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def clear_memories(self, project_id: str) -> None:
        with self._write() as c:
            c.execute("DELETE FROM memories WHERE project_id=?", (project_id,))

    # ------------------------------------------------------------------ 辅助
    @staticmethod
    def _loads(s: Any, default: Any) -> Any:
        try:
            return json.loads(s) if s else default
        except Exception:
            return default

    @staticmethod
    def _project(r: sqlite3.Row) -> Project:
        return Project(id=r["id"], name=r["name"], root=r["root"],
                       created_at=r["created_at"], updated_at=r["updated_at"],
                       settings=AgentStore._loads(r["settings_json"], {}))

    @staticmethod
    def _session(r: sqlite3.Row) -> Session:
        return Session(id=r["id"], project_id=r["project_id"], parent_id=r["parent_id"],
                       title=r["title"], status=r["status"], model=r["model"],
                       summary=r["summary"], summary_tokens=r["summary_tokens"],
                       created_at=r["created_at"], updated_at=r["updated_at"])

    @staticmethod
    def _run(r: sqlite3.Row) -> Run:
        return Run(id=r["id"], session_id=r["session_id"], kind=r["kind"],
                   status=r["status"], priority=r["priority"],
                   message_id=r["message_id"], cancel_requested=bool(r["cancel_requested"]),
                   error=r["error"], model=r["model"],
                   usage=AgentStore._loads(r["usage_json"], {}),
                   created_at=r["created_at"], started_at=r["started_at"],
                   finished_at=r["finished_at"])

    @staticmethod
    def _memory(r: sqlite3.Row) -> Memory:
        return Memory(id=r["id"], project_id=r["project_id"], session_id=r["session_id"],
                      type=r["type"], key=r["key"],
                      value=AgentStore._loads(r["value_json"], None),
                      confidence=r["confidence"], importance=r["importance"],
                      source_message_id=r["source_message_id"],
                      source_run_id=r["source_run_id"], created_at=r["created_at"],
                      last_used_at=r["last_used_at"], expires_at=r["expires_at"],
                      pinned=bool(r["pinned"]))

    def stats(self) -> dict:
        c = self.conn()
        out = {}
        for table in ("projects", "sessions", "messages", "runs", "steps", "events",
                      "layers", "memories"):
            try:
                out[table] = int(c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except Exception:
                out[table] = -1
        out["fts"] = self.fts_enabled
        return out
