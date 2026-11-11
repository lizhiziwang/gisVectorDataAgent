"""为 Shapefile 构建 .qix 空间索引。

用法：
    .venv\\Scripts\\python.exe tools/build_spatial_index.py path\\to\\layer.shp

默认同步等待，便于脚本/部署阶段一次性建好；应用内打开 Shapefile 时也会
在后台自动尝试一次（可用 GIS_AGENT_AUTO_INDEX=0 关闭）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.spatial_index import build_shapefile_index, index_status  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    p = Path(argv[1])
    before = index_status(p)
    ok = build_shapefile_index(p, wait=True)
    print(f"{p}: {before} -> {'fresh' if ok else index_status(p)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
