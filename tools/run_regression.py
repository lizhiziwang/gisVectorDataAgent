"""回归测试总入口：一次跑完所有 tools/test_*.py。

用法：
    .venv\\Scripts\\python.exe tools\\run_regression.py            # 跑全部
    .venv\\Scripts\\python.exe tools\\run_regression.py storage memory   # 只跑名字含关键字的

退出码 0 = 全过，1 = 有失败。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
PY = sys.executable

#: 需要图形界面的测试（会起 QApplication）
GUI_TESTS = {
    "test_multitask.py", "test_session_persist.py", "test_runtime_phase2.py",
    "test_memory_ui.py", "test_acceptance.py", "test_chat.py", "test_chat_flow.py",
    "test_custom_op.py", "test_noop_layer.py", "test_no_block.py",
    "test_scroll.py", "test_step_buttons.py", "test_ux_batch.py",
    "test_ux_more.py", "test_visible.py", "test_agent_v2.py",
    "test_task_center.py", "test_clarify_flow.py", "test_settings_dialog.py",
}

#: 需要外部服务（真实 LLM 接口）的测试，跳过。
#: 注意 test_agent_llm.py 用的是假 LLM（离线），**不在**这里 ——
#: 它测的是「模型返回什么 → Agent 怎么反应」，不联网也能测。
SKIP: set[str] = set()

#: 顺序：先跑纯逻辑的，再跑要起窗口的
ORDER = [
    "test_storage.py",
    "test_task_scheduler.py",
    "test_memory.py",
    "test_memory_phase3.py",
    "test_api_formats.py",
    "test_conn_test.py",
    "test_mentions.py",
    "test_mentions_desktop.py",
    "test_multitask.py",
    "test_runtime_phase2.py",
    "test_memory_ui.py",
    "test_session_persist.py",
    "test_acceptance.py",
    "test_agent_llm.py",
    "test_clarify_flow.py",
    "test_settings_dialog.py",
]


def all_tests() -> list[str]:
    files = sorted(p.name for p in TOOLS.glob("test_*.py"))
    known = [f for f in ORDER if f in files]
    rest = [f for f in files if f not in known]
    return known + rest


def main() -> int:
    filters = [a.lower() for a in sys.argv[1:]]
    tests = all_tests()
    if filters:
        tests = [t for t in tests if any(f in t.lower() for f in filters)]

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    passed, failed, skipped = [], [], []
    t_all = time.time()
    for name in tests:
        if name in SKIP:
            skipped.append((name, "需要外部 LLM 接口"))
            print(f"\n=== SKIP {name} ===")
            continue
        print(f"\n=== {name} ===", flush=True)
        t0 = time.time()
        try:
            p = subprocess.run([PY, "-u", str(TOOLS / name)],
                               cwd=str(ROOT), env=env, timeout=600,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
        except subprocess.TimeoutExpired:
            failed.append((name, "超时"))
            print(f"  !! 超时")
            continue
        dt = time.time() - t0
        out = (p.stdout or "") + (p.stderr or "")
        # 只打印关键行，避免刷屏
        for line in out.splitlines():
            s = line.strip()
            if (s.startswith("OK ") or s.startswith("!! ") or s.startswith("结论")
                    or "Traceback" in s or s.startswith("Error")):
                print("  " + s)
        if p.returncode == 0:
            passed.append(name)
            print(f"  PASS ({dt:.1f}s)")
        else:
            failed.append((name, f"exit={p.returncode}"))
            print(f"  FAIL ({dt:.1f}s) exit={p.returncode}")
            tail = out.splitlines()[-15:]
            for line in tail:
                print("    | " + line)

    print("\n" + "=" * 60)
    print(f"通过 {len(passed)} / {len(tests)}，耗时 {time.time() - t_all:.1f}s")
    if passed:
        print("  ✅ " + ", ".join(passed))
    if skipped:
        print("  ⏭️  跳过：" + ", ".join(f"{n}（{r}）" for n, r in skipped))
    if failed:
        print("  ❌ 失败：" + ", ".join(f"{n}（{r}）" for n, r in failed))
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
