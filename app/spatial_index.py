"""Shapefile 空间索引（.qix）的辅助构建。

为什么需要
----------
Shapefile 没有内建空间索引。没有 `.qix` 时，GDAL 的 bbox 过滤仍要顺序扫描
整个 `.shp`：470k 条道路一次城市窗口查询约 2.8 s，500 万要素会到几十秒，
平移/缩放不可能跟手。QGIS 的做法是让用户显式「创建空间索引」，这里在
后台自动补一次 `.qix`，后续 bbox 查询走索引（同一查询约 0.27 s）。

约束
----
- 只处理 `.shp`，只写同目录的 `.qix` 旁车文件，不改 `.shp/.dbf`；
- `.qix` 比源文件新就直接复用；
- 需要外部 `ogrinfo`（GDAL）。`pyogrio` 自带 GDAL 运行时但没有 CLI，
  所以找不到 `ogrinfo` 时安静跳过，渲染退回顺序扫描；
- 默认后台线程构建，不阻塞首帧；可用 `GIS_AGENT_AUTO_INDEX=0` 关闭。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

_LOCK = threading.Lock()
_BUILDING: set[str] = set()


def _auto_enabled() -> bool:
    env = os.environ.get("GIS_AGENT_AUTO_INDEX", "").strip().lower()
    return env not in ("0", "false", "no", "off")


def qix_path(path: str | Path) -> Path:
    return Path(path).with_suffix(".qix")


def has_fresh_index(path: str | Path) -> bool:
    p = Path(path)
    q = qix_path(p)
    try:
        return q.is_file() and q.stat().st_size > 0 and q.stat().st_mtime >= p.stat().st_mtime
    except Exception:
        return False


def _can_write(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / f".idx_probe_{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except Exception:
        return False


def build_shapefile_index(path: str | Path, wait: bool = False) -> bool:
    """构建 `.qix`；`wait=True` 时同步等待，主要供测试/命令行使用。

    返回是否已有可用索引（异步构建还没完成时返回 False）。
    """
    p = Path(path)
    if p.suffix.lower() != ".shp" or not p.is_file():
        return False
    if has_fresh_index(p):
        return True
    if not _auto_enabled() and not wait:
        return False
    exe = shutil.which("ogrinfo")
    if exe is None or not _can_write(p):
        return False

    key = str(p.resolve()).lower()
    with _LOCK:
        if key in _BUILDING:
            return False
        _BUILDING.add(key)

    def _run() -> None:
        layer = p.stem.replace('"', '""')
        cmd = [exe, "-sql", f'CREATE SPATIAL INDEX ON "{layer}"', str(p)]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
                creationflags=creationflags,
                check=False,
            )
        except Exception:
            pass
        finally:
            with _LOCK:
                _BUILDING.discard(key)

    th = threading.Thread(target=_run, name="shp-index", daemon=True)
    th.start()
    if wait:
        th.join(timeout=300)
        return has_fresh_index(p)
    return False


def index_status(path: str | Path) -> str:
    """给元信息/调试用：fresh / stale / missing / unsupported。"""
    p = Path(path)
    if p.suffix.lower() != ".shp":
        return "unsupported"
    q = qix_path(p)
    if not q.is_file():
        return "missing"
    return "fresh" if has_fresh_index(p) else "stale"
