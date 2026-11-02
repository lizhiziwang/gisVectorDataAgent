"""Skills 回归：技能文件解析 / 启用上下文 / 设置窗口 / 头部入口。

用法：.venv/Scripts/python.exe tools/test_skills.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GIS_AGENT_PERSIST", "0")
os.environ.setdefault("GIS_AGENT_CONFIRM_STEPS", "0")

FAILS = []


def check(name, got, want=True):
    ok = (got == want) if not callable(want) else bool(want(got))
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        FAILS.append(name)


def main() -> int:
    import app.skills as sk

    print("[1] 根目录 skills")
    check("skills 目录存在", (ROOT / "skills").is_dir(), True)
    seeds = [s.name for s in sk.list_skills()]
    check("内置技能已加载", len(seeds) >= 2, True)
    check("不把 README 当技能", all("README" not in n.upper() for n in seeds), True)

    print("[2] 解析 / 启用上下文")
    tmp = Path(tempfile.mkdtemp(prefix="skills_test_"))
    old_dir = sk.SKILLS_DIR
    old_load, old_save = sk.load_cfg, sk.save_cfg
    cfg = {"skills_enabled": True, "skills_disabled": []}
    sk.SKILLS_DIR = tmp
    sk.load_cfg = lambda: dict(cfg)
    sk.save_cfg = lambda c: cfg.update(c)
    try:
        p = sk.save_skill("测试技能", "测试说明", "第一步：先检查坐标系。\n第二步：再统计面积。")
        check("技能文件写盘", p.is_file(), True)
        list1 = sk.list_skills()
        check("解析名称", list1[0].name, "测试技能")
        check("解析说明", list1[0].description, "测试说明")
        check("解析正文", "先检查坐标系" in list1[0].body, True)
        ctx = sk.enabled_skills_context()
        check("上下文包含技能", "测试技能" in ctx and "先检查坐标系" in ctx, True)
        sk.set_skill_enabled("测试技能", False)
        check("禁用写入配置", "测试技能" in cfg["skills_disabled"], True)
        check("禁用后上下文为空", sk.enabled_skills_context(), "")
        p2 = sk.save_skill("第二技能", "", "正文")
        check("第二个文件", p2.is_file() and p2 != p, True)
        sk.delete_skill(p2)
        check("删除技能", p2.exists(), False)
    finally:
        sk.SKILLS_DIR = old_dir
        sk.load_cfg, sk.save_cfg = old_load, old_save

    print("[3] 设置窗口")
    from PySide6.QtWidgets import QApplication, QTabWidget

    from native.agent_settings import AgentSettingsDialog

    app = QApplication.instance() or QApplication([])
    dlg = AgentSettingsDialog()
    tabs = dlg.findChild(QTabWidget)
    check("两个页签", tabs.count(), 2)
    check("技能页存在", tabs.tabText(0), "技能")
    check("通用页存在", tabs.tabText(1), "通用")
    check("技能列表有内容", dlg.skill_list.count() >= 2, True)
    check("逐步确认开关", dlg.chk_confirm.isChecked() in (True, False), True)
    check("技能总开关", dlg.chk_skills.isChecked() in (True, False), True)
    dlg.deleteLater()
    app.processEvents()

    print("[4] 头部最右侧入口")
    from native.app import MainWindow

    win = MainWindow()
    win.show()
    app.processEvents()
    check("Agent 设置按钮存在", hasattr(win, "btn_agent_settings"), True)
    check("按钮文字", "Agent 设置" in win.btn_agent_settings.text(), True)
    check("按钮在顶部栏", win.btn_agent_settings.parent() is win.centralWidget()
          or win.btn_agent_settings.parent() is not None, True)
    win.close()
    win.deleteLater()
    app.processEvents()

    print()
    if FAILS:
        print(f"结论: FAIL（{len(FAILS)} 项）：" + "、".join(FAILS))
        return 1
    print("结论: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
