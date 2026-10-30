"""会话/运行/记忆的轻量数据模型。

这些 dataclass 只描述「要落库/要展示」的结构，不依赖 Qt、geopandas 或 LLM；
存储层（storage.py）负责 SQLite，运行时（runtime.py）负责调度。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


def new_id(prefix: str = "") -> str:
    return (prefix + uuid.uuid4().hex[:12]) if prefix else uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()


@dataclass
class Project:
    id: str
    name: str
    root: str
    created_at: float
    updated_at: float
    settings: dict = field(default_factory=dict)


@dataclass
class Session:
    id: str
    project_id: str
    title: str
    status: str = "idle"
    parent_id: str | None = None
    model: str = ""
    summary: str = ""
    summary_tokens: int = 0
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)


@dataclass
class Part:
    type: str                      # text/tool_call/tool_result/layer_ref/table/warning/clarify
    content: dict = field(default_factory=dict)
    ordinal: int = 0
    id: str = field(default_factory=lambda: new_id("part_"))
    artifact_id: str | None = None


@dataclass
class Message:
    id: str
    session_id: str
    role: str                      # user/assistant/system/tool
    ordinal: int
    created_at: float
    parts: list[Part] = field(default_factory=list)
    run_id: str | None = None
    metadata: dict = field(default_factory=dict)

    def text(self) -> str:
        return "\n".join(str(p.content.get("text", "")) for p in self.parts
                         if p.type == "text").strip()


@dataclass
class Run:
    id: str
    session_id: str
    kind: str = "chat"
    status: str = "queued"
    priority: int = 50
    message_id: str | None = None
    cancel_requested: bool = False
    error: str | None = None
    model: str = ""
    usage: dict = field(default_factory=dict)
    created_at: float = field(default_factory=now)
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class Step:
    id: str
    run_id: str
    ordinal: int | None      # None = 自动接在最后一条后面
    tool: str
    args: dict
    status: str
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    layer_id: str | None = None
    elapsed_ms: int = 0
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class Event:
    run_id: str
    seq: int
    type: str
    payload: dict = field(default_factory=dict)
    session_id: str | None = None
    created_at: float = field(default_factory=now)
    id: int | None = None


@dataclass
class LayerRecord:
    id: str
    project_id: str
    name: str
    parent_id: str = ""
    op: str = ""
    crs: str = ""
    geom_type: str = ""
    feature_count: int = 0
    bounds: list[float] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)
    cache_path: str = ""
    content_hash: str = ""
    created_by_run: str = ""
    created_at: float = field(default_factory=now)
    deleted_at: float | None = None


@dataclass
class Memory:
    id: str
    project_id: str
    type: str
    key: str
    value: Any
    confidence: float = 0.8
    importance: float = 0.5
    session_id: str | None = None
    source_message_id: str | None = None
    source_run_id: str | None = None
    created_at: float = field(default_factory=now)
    last_used_at: float | None = None
    expires_at: float | None = None
    pinned: bool = False
