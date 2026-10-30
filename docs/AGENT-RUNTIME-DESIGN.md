# 桌面端 Agent 运行时设计：多任务、会话与记忆

> 目标：把现在「一个 Worker + 一个 `_busy` + 内存 history」的桌面 Agent，升级成
> **可排队、可取消、可恢复、会话可持久化、记忆分层**的运行时；继续保持纯 Qt 桌面端，不引入 Web 服务。
>
> 参考对象：opencode 的 server/session/event 架构（见文末参考）。它和本项目的共同点是
> 「Agent 循环 + 工具调用 + 会话历史 + 事件流」，不同点是 opencode 面向代码/终端，
> 本项目面向 GIS 图层/空间算子，所以只借架构，不照搬交互。

---

## 1. 现状分析

### 1.1 多任务：实际上是单任务

[ native/app.py ](../native/app.py) 目前只有：

- `self._worker: Worker | None`（第 208 行）——同一时刻只保存一个后台线程引用；
- `self._busy`（第 209 行）——聊天、载入、导出、生成算子共用同一个忙碌标志；
- `on_send()`（第 1249 行）直接 `_set_busy(True)`，聊天输入被禁用；
- `export_layer()`（第 1384 行）也会 `_set_busy(True)` 并覆盖 `self._worker`；
- `_load_paths()` 发现 `_busy` 就弹「当前还有任务在跑」。

结果：

| 场景 | 现在 | 问题 |
|---|---|---|
| 连续发两条分析指令 | 第二条要等第一条结束，输入框被禁用 | 没有任务队列，无法排队/调整优先级 |
| 分析中想导出当前图层 | 导出入口仍可能被触发，`_worker` 被覆盖 | 旧 QThread 引用丢失，线程销毁有崩溃风险 |
| 分析中导入数据 | 直接拒绝 | 无法把导入排到分析之后 |
| 长任务取消 | 没有取消入口 | 只能等 GEOS/GeoPandas 跑完 |
| 任务失败 | 只在聊天里显示错误 | 没有任务列表、重试、恢复 |
| 多个图层并行体检 | 无 | 明明只读，也不能并行 |

### 1.2 记忆：只有「工作记忆」

[app/agent.py](../app/agent.py)：

- `Planner.memory` 只记最近一次成功算子的 `last_tool / last_args / last_layer`（第 273 行），用于「再大一点」；
- `run_agent()` 通过 `history` 传最近 6 条消息，超过就丢；
- `MainWindow.history` 是普通 list，`_remember()` 最多保留 20 条（第 1440 行附近），关窗即丢；
- 没有项目级事实、用户偏好、成功工作流、用户纠正等长期记忆；
- `Store` 的磁盘缓存只是**图层数据换出**，不是 Agent 记忆。

所以现在的「记忆」只能支撑一轮对话内的指代，无法回答：

- 「我上次让你按什么字段统计来着？」
- 「这个项目的数据一般用 CGCS2000 3 度带，以后默认用它。」
- 「上次那个『每个地块内道路总长』的自定义算子，再跑一遍。」
- 「这块数据的行政区代码字段总是文本，筛选时记得加引号。」

### 1.3 已有基础（可以复用）

- `run_agent` / `run_plan` 已经是「计划 → 步骤 → 结果」的结构，适合落库；
- `Layer` 有不可变约定、`meta()` 缓存、`cache_path` 磁盘换出；
- `tool_registry` 有统一工具 schema；
- `Store` 有磁盘缓存和 LRU，适合做图层 artifact 的底层；
- 桌面端已有 `Worker`、`QThread`、步骤卡片，可接事件流。

---

## 2. opencode 的哪些设计值得借

根据 opencode 官方 Server 文档和社区架构分析，它的核心是：

1. **客户端/服务端分离**：`opencode serve` 是 headless HTTP 服务，TUI/IDE/Web 都是客户端；
2. **Session 是持久化单位**：session 下有 messages，message 由 typed **Parts** 组成（text/tool-call/reasoning 等）；
3. **SQLite + WAL**：session/message 状态落库，支持长历史、分页、恢复；
4. **事件总线 + SSE**：`/event` 推送状态变化，客户端不轮询；
5. **Session 生命周期能力**：`abort` 取消、`fork` 分叉、`summarize` 压缩、`revert` 回退、`status` 查询；
6. **父子 session**：subagent / fork 用 `parentID` 表达；
7. **权限模型**：`allow / ask / deny`，按工具/命令 pattern 匹配；
8. **Agent 定义**：primary agent + subagent，markdown frontmatter 配 model/tools/steps。

对桌面 GIS Agent 的映射：

| opencode | 本项目的对应物 | 是否照搬 |
|---|---|---|
| HTTP server + TUI client | 进程内 `AgentRuntime` + Qt UI（信号/槽） | 不照搬 HTTP；保留「运行时与 UI 分离」 |
| Session / Message / Parts | `Session / Message / Part`，Part 增加 `layer_ref / table / warning / clarify` | 借结构，扩展 GIS 类型 |
| SQLite WAL | 本地 `data/agent.db`，WAL + 外键 + 迁移 | 直接借 |
| SSE `/event` | Qt `Signal` + SQLite `events` 表（可回放） | 借事件模型，不搬 SSE |
| parent/child session | 主会话 + 分析/校验子会话（可选） | 借，但 v1 先做确定性校验 |
| abort | `CancelToken` + 步骤边界检查 | 借 |
| fork / revert | 会话分叉、消息级回退到某步之前 | 借，桌面 GIS 先做「重跑某步」 |
| summarize | 会话压缩成 summary，保留最近 N 轮原文 | 借 |
| permissions | GIS 操作的 `allow/ask/deny`（导出路径、覆盖、超大网格、自定义代码） | 借，规则换成空间操作 |
| OpenAPI SDK | 不需要；桌面端不对外暴露服务 | 不借 |

**不照搬的部分**：HTTP 服务、公开分享链接、LSP、git worktree/commit undo、多用户鉴权。
桌面端是单用户本地应用，套 HTTP 只会增加生命周期和端口问题。

---

## 3. 目标架构

```
┌────────────────────────────────────────────────────────────┐
│ Qt UI                                                      │
│  ChatPanel / TaskCenter / LayerPanel / TableDialog          │
│      ▲ Qt signals              │ commands                   │
│      │                         ▼                            │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ AgentRuntime                                        │  │
│  │  SessionManager  TaskScheduler  EventBus  MemoryMgr  │  │
│  └──────────────────────────────────────────────────────┘  │
│      │                                                      │
│      ▼                                                      │
│  Planner / run_agent / ToolRegistry / Store / LayerCache   │
└────────────────────────────────────────────────────────────┘
             │
             ▼
   data/agent.db (SQLite WAL)
   data/projects/<id>/layers/*.gpkg|parquet
```

### 3.1 分层职责

| 层 | 职责 | 不做什么 |
|---|---|---|
| UI | 渲染消息/任务/图层；发指令；显示进度；取消/重试按钮 | 不直接跑 geopandas，不拼 SQL |
| `AgentRuntime` | 会话生命周期、任务调度、事件分发、记忆读写编排 | 不画图、不碰 Qt widget |
| `SessionManager` | Session/Message/Part/Run 的 CRUD、恢复、摘要、分叉 | 不执行算子 |
| `TaskScheduler` | 队列、优先级、并发槽位、取消、重试、状态机 | 不做业务规则 |
| `EventBus` | `run.* / step.* / memory.* / session.*` 事件；Qt 信号桥接 | 不持久化（持久化由 Store 做） |
| `MemoryManager` | 工作记忆/会话摘要/项目事实/偏好/工作流；检索与写入策略 | 不存原始 GeoDataFrame |
| `Planner/run_agent` | 规划、校验、执行、修复 | 不知道 SQLite/UI |
| `Store` | 图层对象、缓存、磁盘换出 | 不管会话/记忆 |

关键原则：**运行时与 UI 解耦**。`run_agent` 只产出 `RunEvent`，UI 订阅；未来如果要加 CLI/HTTP，只需换订阅方。

---

## 4. 数据模型（SQLite）

数据库：`data/agent.db`，WAL、`foreign_keys=ON`、`busy_timeout=5000`、`synchronous=NORMAL`。
大对象（GeoDataFrame、导出文件、图片）放磁盘，DB 只存元信息与指针。

```sql
-- schema 版本
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  root TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  parent_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'idle',      -- idle/running/waiting/failed
  model TEXT,
  summary TEXT,
  summary_tokens INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX idx_sessions_project ON sessions(project_id, updated_at);

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
  role TEXT NOT NULL,                       -- user/assistant/system/tool
  ordinal INTEGER NOT NULL,
  created_at REAL NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_messages_session ON messages(session_id, ordinal);

CREATE TABLE parts (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  type TEXT NOT NULL,                       -- text/tool_call/tool_result/layer_ref/table/warning/clarify
  content_json TEXT NOT NULL DEFAULT '{}',
  artifact_id TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX idx_parts_message ON parts(message_id, ordinal);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
  kind TEXT NOT NULL DEFAULT 'chat',        -- chat/profile/import/export/custom_op
  status TEXT NOT NULL,                     -- queued/running/waiting/succeeded/failed/cancelled/interrupted
  priority INTEGER NOT NULL DEFAULT 50,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  model TEXT,
  usage_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);
CREATE INDEX idx_runs_session ON runs(session_id, created_at);
CREATE INDEX idx_runs_status ON runs(status);

CREATE TABLE steps (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,                     -- ok/warn/error/cancelled
  message TEXT,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  data_json TEXT NOT NULL DEFAULT '{}',
  layer_id TEXT,
  elapsed_ms INTEGER,
  started_at REAL,
  finished_at REAL
);
CREATE INDEX idx_steps_run ON steps(run_id, ordinal);

CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
  session_id TEXT,
  seq INTEGER NOT NULL,
  type TEXT NOT NULL,                       -- run.started/plan/step.started/step.finished/...
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX idx_events_run ON events(run_id, seq);

CREATE TABLE layers (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  parent_id TEXT,
  op TEXT,
  crs TEXT,
  geom_type TEXT,
  feature_count INTEGER,
  bounds_json TEXT,
  fields_json TEXT,
  cache_path TEXT,
  content_hash TEXT,
  created_by_run TEXT,
  created_at REAL NOT NULL,
  deleted_at REAL
);
CREATE INDEX idx_layers_project ON layers(project_id, created_at);

CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  type TEXT NOT NULL,                       -- preference/project_fact/workflow/correction/summary
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
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
CREATE INDEX idx_memories_lookup ON memories(project_id, type, key);
```

### 4.1 全文检索

SQLite FTS5：

```sql
CREATE VIRTUAL TABLE memories_fts USING fts5(
  key, value_text, content='memories', content_rowid='rowid',
  tokenize='trigram'          -- 中文没有空格，trigram 比 unicode61 更合适
);
CREATE VIRTUAL TABLE messages_fts USING fts5(
  content_text, content='', tokenize='trigram'
);
```

- 若当前 SQLite 不支持 trigram，退回 `unicode61` + 查询侧 bigram 展开；
- FTS 只做「候选召回」，最终排序由 `MemoryManager.score()` 控制；
- 不把原始 GeoDataFrame/大 JSON 放进 FTS。

### 4.2 存储约束

- 一个写连接串行化（`threading.Lock`），WAL 允许读并发；
- 每个 run 的 step/event 在一个事务里写，崩溃后最多丢最后一步；
- Layer 文件放 `data/projects/<project_id>/layers/<layer_id>.gpkg|parquet`；DB 存 `cache_path` 与 `content_hash`；
- 删除 session 级联删 messages/parts/runs/steps/events；layer 文件延迟清理（引用计数/垃圾回收）；
- 迁移：`schema_meta.version` + 顺序 migration 函数，启动时事务执行；
- 备份：每天首次启动 `VACUUM INTO data/backups/agent-YYYYMMDD.db`（可选）。

---

## 5. 多任务运行时

### 5.1 任务模型

```python
class RunKind(str, Enum):
    CHAT = "chat"
    PROFILE = "profile"       # 只读体检/字段统计
    IMPORT = "import"
    EXPORT = "export"
    CUSTOM_OP = "custom_op"   # 生成/试跑算子

class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"       # 等澄清/等权限
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"  # 应用退出/崩溃遗留
```

### 5.2 调度器

```python
class TaskScheduler:
    def submit(self, run: Run, fn: Callable[[CancelToken, Progress], dict]) -> Run: ...
    def cancel(self, run_id: str) -> None: ...
    def status(self, run_id: str) -> RunStatus: ...
```

并发槽位：

- **writer lane = 1**：所有会改 `Store` 的 Agent 运行（`run_agent`、导入、自定义算子安装）。  
  原因：`Store.layers` 是单字典，GIS 算子会新建图层；并行写需要 MVCC，第一版不做。
- **read lane = 1~2**：`describe`、导出、字段统计等只读任务；对 `layer.gdf` 做快照（`layer_id + content_hash`）。
- **render**：沿用桌面端已有常驻渲染线程，不归 TaskScheduler 管。
- **导入**：`IMPORT` 也走 writer lane，但可以排在当前聊天 run 之后。
- **自定义算子生成**：LLM 生成是 IO，可并行；安装是写，排 writer lane。

优先级：

```text
用户当前聊天 run      100
用户显式导出/保存      70
导入/载入             60
背景体检/预取          20
```

同一 session 只允许一个 `agent` run 处于 RUNNING；不同 session 可以排队，但 writer lane 仍串行。

### 5.3 取消与超时

- `CancelToken` 包 `threading.Event`；
- `run_plan` 在每个 step 开始前检查；`run_agent` 在 LLM 调用前后、修复前检查；
- 长 GEOS 调用不能从线程外杀死——语义是「当前 step 跑完后停止」，UI 显示「正在取消…」；
- `CUSTOM_OP` 用子进程 + timeout（后续），避免死循环拖住整个进程；
- 每个 run 有软超时（默认 10 分钟）和最大 step 数（默认 20），超时标记 `failed` 并保留已生成图层。

### 5.4 事件流

事件类型：

```text
run.queued
run.started
plan.created
step.started
step.finished
run.waiting           # clarify / permission
run.repairing
run.succeeded
run.failed
run.cancelled
run.interrupted
memory.written
session.summarized
```

- 事件写 `events` 表（append-only，`seq` 单调）；
- `EventBus` 同时 `emit` Qt signal，UI 订阅；
- 重启时把 `status IN (queued,running,waiting)` 改成 `interrupted`，UI 显示「上次未完成，可重试」；
- 不自动重跑长任务，避免启动时突然跑 500k overlay。

### 5.5 UI：任务中心

在右栏/状态栏增加一个可折叠的 **任务中心**：

```
● 正在执行  对地块做 500 米缓冲区        [取消]
  步骤 1/2  缓冲区  0.8s
◌ 排队中    统计道路长度                 [取消]
✓ 已完成    导入 道路.shp                12s  [查看图层]
! 失败      自定义算子生成                 [重试] [看日志]
```

- 每个任务有 `run_id`，聊天消息上的步骤卡片也显示同一 id；
- 任务结束生成 layer 时，任务中心可直接「定位图层」；
- 取消/重试不阻塞 UI；
- 关闭窗口时提示「还有 N 个任务在跑」，可选等待/取消。

### 5.6 并发安全

- `Store` 写操作在 writer lane 内串行；
- `Layer` 一旦创建不再原地修改（已有约定）；写操作用 `lay.gdf.copy()`；
- 只读任务拿 `LayerSnapshot(layer_id, content_hash)`；如果图层在任务期间被删除，任务失败并提示；
- `Layer.cache_path` 的懒加载要加 `threading.Lock`（当前 `Layer.gdf` 属性没有锁，两个线程同时读会重复加载）；
- `sqlite` 连接按线程创建，不跨线程共享 connection；
- Qt signal 必须从主线程发；worker 只发 `EventBus` 线程安全队列，UI 主线程消费。

---

## 6. 记忆系统

### 6.1 分层

| 层 | 内容 | 生命周期 | 存储 |
|---|---|---|---|
| 工作记忆 | 当前 run 的 plan、step 结果、当前图层、澄清状态 | 单次 run | 内存 `RunContext` |
| 会话记忆 | 最近 N 轮消息、tool calls、结果摘要 | 单 session | `messages/parts` + `summary` |
| 项目记忆 | 图层清单、字段语义、CRS、数据质量、命名习惯 | 单 project | `layers` + `memories(type=project_fact)` |
| 长期偏好 | 单位、默认 CRS、输出格式、交互偏好 | 跨 session | `memories(type=preference)` |
| 工作流记忆 | 成功的多步计划/自定义算子 | 跨 session | `memories(type=workflow)` |
| 纠正记忆 | 用户对规划/字段/参数的修正 | 跨 session | `memories(type=correction)` |

**不存**：原始 GeoDataFrame、完整大表、API Key、用户文件内容。  
**只存**：元信息、字段名/类型/取值样例（可选脱敏）、计划、简短事实、指针。

### 6.2 写入策略

每次 run 成功后，按顺序做：

1. **规则提取（零成本）**
   - 新建图层：写 `layers`，并生成 `project_fact`「地块 有字段 用地类型/容积率，CRS EPSG:4326」；
   - 用户说了「以后都用米/都用 CGCS2000/导出 GeoPackage」→ `preference`；
   - 修复动作（字段名纠正、表达式改写）→ `correction`；
   - 成功且步骤数 ≥2 的计划 → `workflow`，key 用意图哈希，value 存 `{plan, layer_schema_fingerprint}`。
2. **LLM 提取（可选，默认关或按需）**
   - 只给最近 2 轮 + 结构化结果；
   - 要求输出 JSON 数组，最多 3 条：`{type,key,value,confidence,scope}`；
   - 过滤：含密钥/路径/大段数据的丢弃；confidence < 0.6 不写；
   - 同一 `(project,type,key)` UPSERT，保留 `source_run_id` 与时间。
3. **去重/合并**
   - `preference` 同 key 新值覆盖旧值，但保留历史版本（`memory_history` 可选）；
   - `project_fact` 以最新 schema 为准；
   - `correction` 累加计数，达到阈值提升 importance。
4. **摘要**
   - 每 20 条消息或 8k token，把旧消息压缩成 `sessions.summary`；
   - 原文保留，摘要只是检索入口；UI 可展开原文。

### 6.3 读取策略

用户发消息时：

1. 抽取查询词：原句 + 当前图层名 + 字段名；
2. 候选召回：
   - 结构化：当前 project 的 `preference/project_fact/correction`，按 key 精确匹配；
   - FTS：`memories_fts` / `messages_fts`，trigram；
   - 近期：最近 2 个 session 的 summary；
3. 打分：

```text
score = 0.50 * fts_score
      + 0.20 * recency
      + 0.15 * importance
      + 0.10 * type_prior(current_layer/project_fact)
      + 0.05 * pinned
```

4. 截断到 token budget（默认 1200 token），按类型分组注入 prompt：

```text
<MEMORY>
[偏好] 默认距离单位=米；默认导出=GPKG
[项目] 地块：665 要素，字段 用地类型/容积率/建筑密度，CRS EPSG:4326
[纠正] 用户说「按类型」时通常指「用地类型」
[工作流] 上次成功：buffer(500m) → area_stats(group_by=用地类型)
</MEMORY>
```

5. 冲突处理：显式用户偏好 > 最近 correction > project_fact > workflow；
6. 记录 `last_used_at`，用于衰减和 UI 排序。

### 6.4 记忆管理 UI

设置/侧栏增加「记忆」面板：

- 按类型分组；显示来源（第几条消息/哪个 run）、置信度、最后使用；
- 可编辑、置顶、删除、清空项目记忆；
- 「忘记这条」命令；
- 开关：是否允许自动提取、是否允许发送到 LLM 做摘要；
- 导出/导入记忆（JSON），便于迁移项目。

### 6.5 记忆与隐私

- 全部本地 SQLite；不上传；
- 默认只存结构化事实，不存原始属性表；
- 如果启用 LLM 提取，明确提示「会把最近对话摘要发给模型」；
- 项目删除时级联删除记忆/消息/事件；图层文件单独 GC。

---

## 7. 从当前代码迁移的步骤

### Phase 1：持久化底座（不改 UI 行为）—— ✅ 已完成

1. ✅ 新增 `app/storage.py`：连接、迁移、Repository；
2. ✅ 新增 `app/models.py`：Session/Message/Part/Run/Step/Memory dataclass；
3. ✅ 默认 project + 默认 session；启动时把 `MainWindow.history` 读/写 DB；
4. ✅ `Store.add` 通过回调写 `layers` 表（`MainWindow._sync_layer_registry` 统一登记）；
5. ✅ `Planner.memory` 落 `session_state` / `working_memory` JSON，启动恢复；
6. ✅ 测试：`tools/test_storage.py`（迁移幂等、消息 roundtrip、崩溃恢复、级联删除）。

### Phase 2：任务调度 + 事件 —— ✅ 已完成

1. ✅ 新增 `app/runtime.py`：`AgentRuntime` / `TaskScheduler` / `CancelToken`；
2. ✅ `MainWindow.on_send` 改为 `runtime.submit_run(...)`；
3. ✅ `self._busy` 改为由调度器状态统一维护（不再是单任务语义）；
4. ✅ `export/load/custom_op` 都走 scheduler；
5. ✅ 任务中心 UI（`native/taskcenter.py`）：排队列表、取消、重试、看日志；
6. ✅ 测试：`tools/test_task_scheduler.py`、`tools/test_multitask.py`、
   `tools/test_runtime_phase2.py`（排队、并发、取消、事件回放、关窗提示）。

### Phase 3：记忆 —— ✅ 已完成

1. ✅ 规则提取 + `project_fact/preference/correction/workflow`；
2. ✅ FTS5 trigram + `MemoryManager.context()` 打分注入 prompt；
3. ✅ 会话摘要（`sessions.summary`，原文保留）；
4. ✅ 记忆管理 UI（`native/memory_panel.py`）：分组展示、编辑、置顶、删除、清空、JSON 导入导出；
5. ✅ 测试：`tools/test_memory.py`、`tools/test_memory_phase3.py`、
   `tools/test_memory_ui.py`、`tools/test_acceptance.py`。

### Phase 4：进阶（可选）—— ⏳ 未开始

- 子 Agent：`ProfileAgent`（只读体检）、`VerifierAgent`（结果校验）；
- 任务 DAG：导入 → 多图层并行 profile → 规划；
- 会话 fork/revert；
- 操作权限（导出覆盖、超大网格、自定义代码执行）；
- 进程级沙箱/超时；
- 项目文件（`.gisproj`）打包 session/layer/memory 指针。

---

## 8. 验收标准

### 多任务

- 连续提交 3 个任务：第 2/3 个显示「排队中」，UI 不冻结；取消第 2 个不影响第 1/3；
- 分析运行中导出：导出排队或走只读 lane，不覆盖 `_worker`，不崩溃；
- 关闭窗口有运行中任务：提示并安全等待/取消，无 `QThread: Destroyed`；
- 重启后未完成任务标记为 `interrupted`，可重试，不自动重跑；
- 同一 run 的 `events` 可完整回放。

### 记忆

- 用户说「以后默认用米」→ 新 session 仍生效；
- 「按类型」自动纠正为「用地类型」并记住 correction；
- 关闭重开，最近会话、图层、偏好、工作流都在；
- 删除记忆后不再注入；清空项目记忆不影响图层；
- 记忆注入不超过预算，不包含原始大表/密钥；
- FTS 中文检索能命中（trigram）。

### 稳定性

- SQLite WAL 下并发读不阻塞写；
- 迁移失败回滚，旧库可打开；
- 长任务取消后线程在 5s 内退出；
- 任务异常写 `runs.error` + `events`，UI 有「看日志」。

---

## 9. 风险与取舍

| 风险 | 处理 |
|---|---|
| SQLite 写并发 | 单写连接 + WAL；Repository 加锁 |
| `Layer.gdf` 并发懒加载 | `Layer` 加 `threading.Lock`；或调度器保证单写、读快照 |
| 多任务内存翻倍 | writer lane=1；只读任务用快照/缓存；大图层磁盘换出 |
| GEOS 不能中断 | 取消只在 step 边界；长 op 前给成本预估/确认 |
| 记忆污染/幻觉 | 只写可验证事实；confidence/source；用户可删；冲突按显式偏好优先 |
| 中文 FTS | trigram；不支持时 bigram 扩展 |
| Token 成本 | 摘要 + 检索预算；LLM 提取默认关 |
| 迁移破坏旧数据 | 只新增表/列；启动前备份；迁移事务 |
| 过度设计 | Phase 1 只做 SQLite + 会话 + 事件，不做向量库/HTTP |

---

## 10. 建议的第一批代码接口

```python
# app/models.py
@dataclass
class Part:
    id: str
    type: str                 # text/tool_call/tool_result/layer_ref/table/warning/clarify
    content: dict

@dataclass
class Run:
    id: str
    session_id: str
    kind: str
    status: str
    priority: int = 50

# app/storage.py
class AgentStore:
    def create_session(self, project_id: str, title: str) -> Session: ...
    def append_message(self, session_id: str, role: str,
                       parts: list[Part], run_id: str | None = None) -> Message: ...
    def create_run(self, session_id: str, kind: str, priority: int) -> Run: ...
    def append_event(self, run_id: str, type: str, payload: dict) -> None: ...
    def list_events(self, run_id: str, after_seq: int = 0) -> list[Event]: ...
    def upsert_memory(self, memory: Memory) -> None: ...
    def search_memories(self, project_id: str, query: str, limit: int = 20) -> list[Memory]: ...

# app/runtime.py
class CancelToken:
    def cancel(self) -> None: ...
    def is_set(self) -> bool: ...
    def raise_if_cancelled(self) -> None: ...      # 取消检查点

class TaskScheduler:
    def submit(self, fn: Callable[[CancelToken], Any], kind: str = "task",
               label: str = "", priority: int = 50) -> Task: ...
    def cancel(self, task_id: str) -> bool: ...
    def snapshot(self) -> list[dict]: ...          # 任务中心渲染用

class AgentRuntime:
    def bind_session(self, project_id: str, session_id: str) -> None: ...
    def submit_run(self, fn: Callable[[CancelToken], Any], kind: str = "chat",
                   label: str = "", priority: int = PRIORITY_CHAT) -> Task: ...
    def record_step(self, run_id: str, step: dict, status: str = "ok") -> None: ...
    def cancel(self, task_id: str) -> bool: ...

def recover_interrupted_runs(store: AgentStore) -> list[str]: ...   # 重启后标记中断

# app/memory.py
class MemoryManager:
    def context(self, query: str, budget_chars: int = 1600) -> str: ...
    def observe_turn(self, user_text: str, result: dict,
                     session_id: str | None = None) -> list[Memory]: ...
    def maybe_summarize(self, session_id: str | None = None) -> str: ...
    def forget(self, memory_id: str) -> bool: ...
    def clear(self, type_: str = "") -> int: ...
    def grouped(self) -> dict[str, list[Memory]]: ...
```

第一版不需要改 `run_agent` 的算法，只把它包进 `TaskScheduler` 的 writer lane，并把 `run_plan` 的 step 回调写成 `events`。

### 10.1 与原文的差异说明

实现时对第 10 节的接口做了几处调整，都是为了适配现有代码：

| 原文 | 实际实现 | 原因 |
|---|---|---|
| `TaskScheduler.submit(run, fn)` | `submit(fn, kind, label, priority)` → 返回 `Task` | 调度器不该知道 DB 的 `Run`；运行记录由 `AgentRuntime` 负责落库 |
| `MemoryManager.retrieve()` | `context()` | 直接返回可注入 prompt 的 `<MEMORY>` 块，检索和拼装合成一步 |
| `MemoryManager.observe_run()` | `observe_turn(user_text, result, ...)` | 现有的执行结果结构是 `{"steps": [...], "lines": [...]}`，按这个形状抽取更直接 |
| `AgentStore` 只列了核心方法 | 另加 `list_recent_runs` / `list_unfinished_runs` / `set_session_summary` / `count_messages` | 任务中心和摘要要用 |

另外实现时补了两个原文没提、但实际必须有的东西：

- **`TaskScheduler.add_hook()`**：运行状态持久化走独立钩子，不挂在对外的事件回调上。
  否则上层一旦替换回调（比如接 UI），运行状态就悄悄不落库了。
- **`AgentStore.append_event` 的 seq 在写锁内计算**：并发写事件时，
  先读 max(seq) 再插入会有竞态，回放顺序会乱。

---

## 参考

- opencode 官方 Server 文档（sessions/messages/parts、abort/fork/summarize、SSE `/event`、OpenAPI `/doc`）：  
  <https://opencode.ai/docs/server>
- 社区对 opencode 架构的对照分析（client/server、SQLite WAL、SSE、session/subagent、permissions）：  
  <https://github.com/shenxingy/Clade/blob/v0.2.0/docs/research/2026-06-18-sst-opencode.md>
- 本项目现有实现：  
  [native/app.py](../native/app.py)、[app/agent.py](../app/agent.py)、[app/engine.py](../app/engine.py)、[app/tool_registry.py](../app/tool_registry.py)、
  [app/runtime.py](../app/runtime.py)、[app/storage.py](../app/storage.py)、[app/memory.py](../app/memory.py)、
  [native/taskcenter.py](../native/taskcenter.py)、[native/memory_panel.py](../native/memory_panel.py)
