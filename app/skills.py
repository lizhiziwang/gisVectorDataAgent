"""Agent Skills：给规划器补充领域知识 / 工作流。

技能就是根目录 `skills/` 下的一张 Markdown 卡片，前面用简单的 YAML
frontmatter 描述名称和用途：

    ---
    name: 遥感影像变化分析
    description: 两期影像先对齐再裁剪，统计变化面积
    ---
    正文就是给规划器的领域说明……

不需要额外依赖，文件本身就是唯一数据源；`settings.json` 只记录哪些技能被禁用。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import load_cfg, save_cfg

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"

_FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    enabled: bool = True

    @property
    def filename(self) -> str:
        return self.path.name


def ensure_dir() -> Path:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return SKILLS_DIR


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        try:
            return str(json.loads(v)) if v[0] == '"' else v[1:-1]
        except Exception:
            return v[1:-1]
    return v


def _parse(path: Path, disabled: set[str], globally_enabled: bool) -> Skill | None:
    if not path.is_file() or path.suffix.lower() not in (".md", ".markdown"):
        return None
    if path.name.lower() in ("readme.md", "index.md"):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    meta: dict[str, str] = {}
    body = text
    m = _FRONT.match(text)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = _unquote(v)
    name = (meta.get("name") or path.stem).strip()
    desc = (meta.get("description") or meta.get("desc") or "").strip()
    enabled = globally_enabled and name not in disabled and path.name not in disabled
    return Skill(name=name, description=desc, body=body.strip(), path=path,
                 enabled=enabled)


def list_skills() -> list[Skill]:
    ensure_dir()
    cfg = load_cfg()
    disabled = set(str(x) for x in (cfg.get("skills_disabled") or []))
    globally_enabled = bool(cfg.get("skills_enabled", True))
    out: list[Skill] = []
    for p in sorted(SKILLS_DIR.iterdir(), key=lambda x: x.name.lower()):
        target = p
        if p.is_dir():
            for cand in ("SKILL.md", "skill.md", "index.md"):
                q = p / cand
                if q.is_file():
                    target = q
                    break
            else:
                continue
        s = _parse(target, disabled, globally_enabled)
        if s is not None:
            out.append(s)
    out.sort(key=lambda s: s.name.lower())
    return out


def set_skill_enabled(name: str, enabled: bool) -> None:
    cfg = load_cfg()
    names = [str(x) for x in (cfg.get("skills_disabled") or [])]
    names = [x for x in names if x != name]
    if not enabled:
        names.append(name)
    cfg["skills_disabled"] = names
    save_cfg(cfg)


def set_skills_globally_enabled(enabled: bool) -> None:
    cfg = load_cfg()
    cfg["skills_enabled"] = bool(enabled)
    save_cfg(cfg)


def enabled_skills_context(max_chars: int = 10000) -> str:
    """给规划器用的技能说明块；没有启用技能时返回空串。"""
    skills = [s for s in list_skills() if s.enabled]
    if not skills:
        return ""
    chunks = [
        "下面是用户启用的技能（领域知识 / 操作偏好），只作为规划参考；"
        "与用户当前明确要求冲突时，以当前要求为准。"
    ]
    used = 0
    for s in skills:
        piece = f"### 技能：{s.name}\n"
        if s.description:
            piece += f"{s.description}\n"
        piece += s.body.strip() + "\n"
        if used + len(piece) > max_chars:
            piece = piece[: max(200, max_chars - used)] + "\n…"
        chunks.append(piece)
        used += len(piece)
        if used >= max_chars:
            break
    return "\n".join(chunks).strip()


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-_").lower()
    if not s:
        s = "skill-" + str(int(time.time()))
    return s[:48]


def save_skill(name: str, description: str, body: str,
               path: str | Path | None = None) -> Path:
    """新增或覆盖一张技能卡，返回文件路径。"""
    name = (name or "").strip()
    body = (body or "").strip()
    if not name:
        raise ValueError("技能名称不能为空")
    if not body:
        raise ValueError("技能内容不能为空")
    ensure_dir()
    if path:
        target = Path(path)
        try:
            target.resolve().relative_to(SKILLS_DIR.resolve())
        except Exception:
            raise ValueError("技能文件必须位于 skills 目录") from None
    else:
        base = _slug(name)
        target = SKILLS_DIR / f"{base}.md"
        n = 2
        while target.exists():
            target = SKILLS_DIR / f"{base}-{n}.md"
            n += 1
    front = (
        "---\n"
        f"name: {json.dumps(name, ensure_ascii=False)}\n"
        f"description: {json.dumps((description or '').strip(), ensure_ascii=False)}\n"
        f"updated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        "---\n\n"
    )
    target.write_text(front + body.rstrip() + "\n", encoding="utf-8")
    return target


def delete_skill(path: str | Path) -> None:
    target = Path(path)
    try:
        target.resolve().relative_to(SKILLS_DIR.resolve())
    except Exception:
        raise ValueError("只能删除 skills 目录中的技能") from None
    target.unlink(missing_ok=True)
