"""桌面端 Agent 运行时：任务调度 + 事件流 + 运行持久化。

分层（见 docs/AGENT-RUNTIME-DESIGN.md）：
    AgentRuntime   —— 编排层：把 Store（SQLite）和 TaskScheduler 接起来
    TaskScheduler  —— 队列/优先级/并发槽位/取消（不依赖 Qt、不依赖 DB）
    CancelToken    —— 协作式取消，只在 step 边界生效

设计原则
--------
· **运行时与 UI 解耦**：本模块不 import Qt，回调由 `native/app.py` 转成 Qt 信号。
· **运行可回放**：每个 run 的状态迁移都写 `runs` / `steps` / `events` 三张表，
  重启后能看出「上次跑到哪、失败在哪」。
· **单写连接**：`AgentStore` 内部一把写锁串行化；本模块只通过 store API 写库。
· **崩溃恢复**：启动时把残留的 `queued/running/waiting` 标记为 `interrupted`，
  不自动重跑（避免一开应用就偷偷跑 500k overlay）。
"""
from __future__ import annotations

import heapq
import itertools
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------- 常量

#: 任务优先级（数字越大越先跑）
PRIORITY_CHAT = 100       # 用户当前聊天
PRIORITY_OPGEN = 90      # 生成自定义算子
PRIORITY_EXPORT = 70     # 用户显式导出
PRIORITY_IMPORT = 60     # 导入 / 载入
PRIORITY_BACKGROUND = 20  # 后台体检 / 预取

#: 任务状态（与 DB 里 runs.status 对齐）
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_WAITING = "waiting"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

#: 事件类型（与设计文档 §5.4 对齐）
EV_RUN_QUEUED = "run.queued"
EV_RUN_STARTED = "run.started"
EV_PLAN_CREATED = "plan.created"
EV_STEP_STARTED = "step.started"
EV_STEP_FINISHED = "step.finished"
EV_RUN_WAITING = "run.waiting"
EV_RUN_REPAIRING = "run.repairing"
EV_RUN_SUCCEEDED = "run.succeeded"
EV_RUN_FAILED = "run.failed"
EV_RUN_CANCELLED = "run.cancelled"
EV_RUN_INTERRUPTED = "run.interrupted"
EV_MEMORY_WRITTEN = "memory.written"
EV_SESSION_SUMMARIZED = "session.summarized"


class TaskCancelled(Exception):
    """任务在可取消点收到取消请求。"""


class CancelToken:
    """协作式取消令牌。

    长 GEOS 调用无法从线程外强杀，语义是「当前 step 跑完后停止」，
    所以取消检查点都放在 step 边界。
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TaskCancelled("任务已取消")


@dataclass
class Task:
    """一个待执行 / 执行中的后台任务。"""

    id: str
    kind: str
    label: str
    priority: int
    fn: Callable[[CancelToken], Any]
    status: str = STATUS_QUEUED
    cancel_token: CancelToken = field(default_factory=CancelToken)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result: Any = None
    #: 对应的 DB run id（由 AgentRuntime 填）
    run_id: str | None = None
    #: 事件回调：(event_name, task, payload)
    on_event: Callable[[str, "Task", Any], None] | None = None

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)


class TaskScheduler:
    """优先级队列 + 固定并发槽位的执行器。

    · 单 writer lane（``max_workers=1``）时所有任务严格串行，符合设计文档 §5.2；
    · 高优先级任务插队：堆按 ``(-priority, seq)`` 排序；
    · 取消：排队中直接置 cancelled；运行中只置 token，等 fn 自己检查到。
    """

    def __init__(self, on_event: Callable[[str, Task, Any], None] | None = None,
                 max_workers: int = 1) -> None:
        self._on_event = on_event
        self.max_workers = max(1, int(max_workers))
        self._heap: list[tuple[int, int, Task]] = []
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._stop = False
        self._workers: list[threading.Thread] = []
        self._active: dict[str, Task] = {}
        self._all: dict[str, Task] = {}
        #: 内部钩子（如 DB 持久化）。和 on_event 分开，避免外部替换回调时
        #: 把持久化链路一起弄丢——运行状态必须始终落库。
        self._hooks: list[Callable[[str, Task, Any], None]] = []

    def add_hook(self, hook: Callable[[str, Task, Any], None]) -> None:
        """注册一个内部事件钩子（持久化用）。"""
        self._hooks.append(hook)

    # ------------------------------------------------------------------ API
    def submit(self, fn: Callable[[CancelToken], Any], kind: str = "task",
               label: str = "", priority: int = 50,
               run_id: str | None = None) -> Task:
        task = Task(id=uuid.uuid4().hex[:12], kind=kind, label=label or kind,
                    priority=int(priority), fn=fn, run_id=run_id)
        with self._cond:
            self._all[task.id] = task
            heapq.heappush(self._heap, (-task.priority, next(self._seq), task))
            self._ensure_workers()
            self._emit("queued", task, None)
            self._cond.notify_all()
        return task

    def cancel(self, task_id: str) -> bool:
        with self._cond:
            task = self._all.get(task_id)
            if task is None:
                return False
            if task.status == STATUS_QUEUED:
                task.status = STATUS_CANCELLED
                task.finished_at = time.time()
                self._emit("cancelled", task, None)
                self._cond.notify_all()
            elif task.status == STATUS_RUNNING:
                task.cancel_token.cancel()
                self._emit("cancel_requested", task, None)
            return True

    def get(self, task_id: str) -> Task | None:
        with self._cond:
            return self._all.get(task_id)

    @property
    def active_task(self) -> Task | None:
        with self._cond:
            return next(iter(self._active.values()), None)

    @property
    def active_tasks(self) -> list[Task]:
        with self._cond:
            return list(self._active.values())

    @property
    def running_count(self) -> int:
        with self._cond:
            return len(self._active)

    @property
    def pending_count(self) -> int:
        with self._cond:
            return sum(1 for _, _, t in self._heap if t.status == STATUS_QUEUED)

    def pending_tasks(self) -> list[Task]:
        with self._cond:
            return [t for _, _, t in self._heap if t.status == STATUS_QUEUED]

    def snapshot(self) -> list[dict]:
        """按创建时间倒序的任务快照，供任务中心渲染。"""
        with self._cond:
            tasks = sorted(self._all.values(), key=lambda t: t.created_at, reverse=True)
            return [{"id": t.id, "kind": t.kind, "label": t.label, "status": t.status,
                     "error": t.error, "created_at": t.created_at,
                     "started_at": t.started_at, "finished_at": t.finished_at,
                     "elapsed": t.elapsed, "run_id": t.run_id} for t in tasks]

    def shutdown(self, wait: bool = True) -> None:
        with self._cond:
            self._stop = True
            for t in self._active.values():
                t.cancel_token.cancel()
            self._cond.notify_all()
        if wait:
            for th in self._workers:
                th.join(timeout=5)

    # ------------------------------------------------------------------ 内部
    def _ensure_workers(self) -> None:
        alive = [w for w in self._workers if w.is_alive()]
        while len(alive) < self.max_workers:
            th = threading.Thread(target=self._loop, daemon=True,
                                  name=f"gis-task-{len(self._workers)}")
            self._workers.append(th)
            th.start()
            alive.append(th)

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._stop and not self._heap:
                    self._cond.wait(timeout=1.0)
                if self._stop:
                    return
                # 跳过已取消的队列项
                task = None
                while self._heap:
                    _, _, cand = heapq.heappop(self._heap)
                    if cand.status == STATUS_QUEUED:
                        task = cand
                        break
                if task is None:
                    continue
                task.status = STATUS_RUNNING
                task.started_at = time.time()
                self._active[task.id] = task
                self._emit("started", task, None)
            try:
                result = task.fn(task.cancel_token)
                if task.cancel_token.is_set():
                    task.status = STATUS_CANCELLED
                    task.finished_at = time.time()
                    self._emit("cancelled", task, result)
                else:
                    task.status = STATUS_SUCCEEDED
                    task.result = result
                    task.finished_at = time.time()
                    self._emit("done", task, result)
            except TaskCancelled:
                task.status = STATUS_CANCELLED
                task.finished_at = time.time()
                self._emit("cancelled", task, None)
            except Exception as e:  # noqa: BLE001
                task.status = STATUS_FAILED
                task.error = str(e)
                task.finished_at = time.time()
                self._emit("failed", task, str(e))
            finally:
                with self._cond:
                    self._active.pop(task.id, None)
                    self._cond.notify_all()

    def _emit(self, event: str, task: Task, payload: Any) -> None:
        """事件分发：内部钩子 → 全局回调 → 任务级回调。

        内部钩子（持久化）先跑且不受外部回调替换影响，保证运行状态一定落库。
        """
        for hook in list(self._hooks):
            try:
                hook(event, task, payload)
            except Exception:
                pass
        for cb in (self._on_event,
                   (task.on_event if task is not None else None)):
            if cb is None:
                continue
            try:
                cb(event, task, payload)
            except Exception:
                pass


class AgentRuntime:
    """运行时编排层：SQLite 持久化 + 任务调度 + 事件分发。

    不依赖 Qt。桌面端通过 ``on_event`` 回调订阅事件，再转成主线程 signal。
    """

    def __init__(self, store: Any, on_event: Callable[[str, Task, Any], None] | None = None,
                 max_workers: int = 1) -> None:
        self.store = store
        self.scheduler = TaskScheduler(on_event=None, max_workers=max_workers)
        # 持久化走内部钩子：即使用户替换了 on_event，运行状态也一定落库
        self.scheduler.add_hook(self._on_scheduler_event)
        self._on_event = on_event
        self._session_id = ""
        self._project_id = ""
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 会话
    def bind_session(self, project_id: str, session_id: str) -> None:
        with self._lock:
            self._project_id = project_id
            self._session_id = session_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def project_id(self) -> str:
        return self._project_id

    # ------------------------------------------------------------------ 提交
    def submit_run(self, fn: Callable[[CancelToken], Any], kind: str = "chat",
                   label: str = "", priority: int = PRIORITY_CHAT,
                   user_text: str = "") -> Task:
        """提交一个运行：先建 DB run 行，再进调度队列。

        ``fn`` 收到 ``CancelToken``；返回值会作为 ``done`` 事件的 payload。
        """
        run_id = ""
        if self.store is not None:
            try:
                run = self.store.create_run(self._session_id, kind=kind,
                                            priority=priority)
                run_id = run.id
            except Exception:
                run_id = ""
        task = self.scheduler.submit(fn, kind=kind, label=label,
                                     priority=priority, run_id=run_id)
        with self._lock:
            if run_id:
                self._persist_run(task, run_id, "queued", started_at=None,
                                  finished_at=None, error=None)
                self._append_event(run_id, EV_RUN_QUEUED,
                                   {"label": label, "kind": kind, "priority": priority})
        return task

    def cancel(self, task_id: str) -> bool:
        task = self.scheduler.get(task_id)
        ok = self.scheduler.cancel(task_id)
        if ok and task is not None and task.run_id:
            try:
                self.store.update_run(task.run_id, cancel_requested=True)
            except Exception:
                pass
        return ok

    # ------------------------------------------------------------------ 事件
    def _on_scheduler_event(self, event: str, task: Task, payload: Any) -> None:
        """把调度器事件翻译成 DB 事件 + 对外回调。"""
        run_id = task.run_id
        if self.store is not None and run_id:
            try:
                if event == "started":
                    self._persist_run(task, run_id, STATUS_RUNNING, started_at=time.time())
                    self._append_event(run_id, EV_RUN_STARTED, {"label": task.label})
                elif event == "done":
                    self._persist_run(task, run_id, STATUS_SUCCEEDED,
                                      started_at=task.started_at,
                                      finished_at=task.finished_at)
                    self._append_event(run_id, EV_RUN_SUCCEEDED, {"label": task.label})
                elif event == "failed":
                    self._persist_run(task, run_id, STATUS_FAILED,
                                      started_at=task.started_at,
                                      finished_at=task.finished_at, error=task.error)
                    self._append_event(run_id, EV_RUN_FAILED,
                                       {"label": task.label, "error": task.error})
                elif event == "cancelled":
                    self._persist_run(task, run_id, STATUS_CANCELLED,
                                      started_at=task.started_at,
                                      finished_at=task.finished_at)
                    self._append_event(run_id, EV_RUN_CANCELLED, {"label": task.label})
            except Exception:
                pass
        if self._on_event is not None:
            try:
                self._on_event(event, task, payload)
            except Exception:
                pass

    # ------------------------------------------------------------------ 步骤
    def record_plan(self, run_id: str, steps: Iterable[dict]) -> None:
        """把计划写进 events（步骤本身在执行时逐条落 steps 表）。"""
        if self.store is None or not run_id:
            return
        try:
            self._append_event(run_id, EV_PLAN_CREATED,
                               {"steps": len(list(steps))})
        except Exception:
            pass

    def record_step(self, run_id: str, step: dict, status: str = "ok",
                    started_at: float | None = None,
                    finished_at: float | None = None) -> None:
        """落一条 step，并写 step.started / step.finished 事件。"""
        if self.store is None or not run_id:
            return
        try:
            elapsed = 0
            if started_at and finished_at:
                elapsed = int(max(0.0, finished_at - started_at) * 1000)
            self.store.append_step(run_id, {
                "id": "", "run_id": run_id, "ordinal": None,
                "tool": str(step.get("tool") or ""),
                "args": step.get("args") or {},
                "status": status,
                "message": str(step.get("message") or ""),
                "warnings": list(step.get("warnings") or []),
                "data": step.get("data") or {},
                "layer_id": step.get("layer_id") or step.get("layer_id_ref") or None,
                "elapsed_ms": elapsed,
                "started_at": started_at, "finished_at": finished_at,
            })
            self._append_event(run_id, EV_STEP_FINISHED,
                               {"tool": step.get("tool"), "status": status,
                                "message": step.get("message") or ""})
        except Exception:
            pass

    def record_step_started(self, run_id: str, tool: str, args: dict | None = None) -> None:
        if self.store is None or not run_id:
            return
        try:
            self._append_event(run_id, EV_STEP_STARTED,
                               {"tool": tool, "args": args or {}})
        except Exception:
            pass

    # ------------------------------------------------------------------ 内部
    def _persist_run(self, task: Task, run_id: str, status: str,
                     started_at: float | None = None,
                     finished_at: float | None = None,
                     error: str | None = None) -> None:
        if self.store is None or not run_id:
            return
        fields: dict[str, Any] = {"status": status}
        if started_at is not None:
            fields["started_at"] = started_at
        if finished_at is not None:
            fields["finished_at"] = finished_at
        if error is not None:
            fields["error"] = error
        self.store.update_run(run_id, **fields)

    def _append_event(self, run_id: str, type_: str, payload: dict | None = None) -> None:
        if self.store is None:
            return
        try:
            self.store.append_event(run_id, type_, payload or {},
                                    session_id=self._session_id or None)
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass


def recover_interrupted_runs(store: Any, project_id: str = "") -> list[str]:
    """把上次退出残留的未完成 run 标记为 ``interrupted``。

    返回被标记的 run id 列表。**不自动重跑**——长任务在用户不知情时
    突然开跑（比如 50 万要素的 overlay）既慢又危险，只提示「可重试」。
    """
    if store is None:
        return []
    marked: list[str] = []
    try:
        rows = store.conn().execute(
            "SELECT id FROM runs WHERE status IN (?,?,?)",
            (STATUS_QUEUED, STATUS_RUNNING, STATUS_WAITING)).fetchall()
        for r in rows:
            rid = r["id"]
            store.update_run(rid, status=STATUS_INTERRUPTED,
                             finished_at=now_ts())
            store.append_event(rid, EV_RUN_INTERRUPTED,
                               {"reason": "应用退出时任务未完成"}, session_id=None)
            marked.append(rid)
    except Exception:
        return marked
    return marked


def now_ts() -> float:
    return time.time()


def format_traceback(exc: BaseException) -> str:
    """给「看日志」用的完整堆栈（截断，避免塞爆 DB）。"""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[:8000]
