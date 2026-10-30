"""任务调度器回归：优先级、串行、排队取消、运行中取消。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.runtime import TaskScheduler

fails = []


def check(name, got, want=True):
    ok = got == want if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


events = []
s = TaskScheduler(on_event=lambda ev, task, payload: events.append((ev, task.label)))

print("[1] 优先级 + 串行")
release = threading.Event()
order = []


def first(token):
    order.append("first-start")
    release.wait(3)
    order.append("first-end")
    return "one"


def second(token):
    order.append("second")
    return "two"


t1 = s.submit(first, kind="agent", label="first", priority=100)
t2 = s.submit(second, kind="agent", label="second", priority=10)
time.sleep(0.2)
check("高优先级先跑", order, ["first-start"])
check("第二个在排队", s.pending_count, 1)
release.set()
for _ in range(40):
    if t1.status == "succeeded" and t2.status == "succeeded":
        break
    time.sleep(0.05)
check("串行顺序", order, ["first-start", "first-end", "second"])
check("都成功", (t1.status, t2.status), ("succeeded", "succeeded"))

print("[2] 排队任务取消")
block = threading.Event()


def slow(token):
    block.wait(3)
    return "ok"


a = s.submit(slow, label="a", priority=100)
time.sleep(0.1)
b = s.submit(slow, label="b", priority=50)
c = s.submit(slow, label="c", priority=10)
check("b 排队", b.status, "queued")
check("取消 b", s.cancel(b.id), True)
check("b 已取消", b.status, "cancelled")
block.set()
for _ in range(40):
    if a.status == "succeeded" and c.status == "succeeded":
        break
    time.sleep(0.05)
check("c 仍执行", c.status, "succeeded")

print("[3] 运行中取消")
started = threading.Event()


def loop(token):
    started.set()
    for _ in range(100):
        if token.is_set():
            from app.runtime import TaskCancelled
            raise TaskCancelled("stop")
        time.sleep(0.02)
    return "done"


d = s.submit(loop, label="d", priority=100)
started.wait(2)
check("取消运行中", s.cancel(d.id), True)
for _ in range(40):
    if d.status == "cancelled":
        break
    time.sleep(0.05)
check("状态为 cancelled", d.status, "cancelled")

s.shutdown()
print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
sys.exit(0 if not fails else 1)
