"""矢量数据读写（与界面无关），Web 版与桌面版共用。

要点：
· Shapefile 读入时在 UTF-8 / GBK 间择优，避免中文属性乱码；
· 导出 Shapefile 时字段名按 10 字节安全截断（超限改名 F1/F2…）并附字段对照表，
  否则中文会被切成半个字，DBF 直接读不回来；
· 导出 GeoJSON 前把 NaN 清洗成 null，保证是合法 JSON。
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .util import json_safe

VECTOR_EXTS = (".shp", ".gpkg", ".geojson", ".json", ".kml", ".gml")


# --------------------------------------------------------------------------- 读
def read_vector(path: Path, encoding: str | None = None) -> gpd.GeoDataFrame:
    # 注意：ESRI Shapefile 驱动不支持 ENCODING_ERRORS，传了会告警，这里只传 encoding
    opts: dict = {}
    if encoding:
        opts["encoding"] = encoding
    if not Path(path).exists():
        # 不给这条，下面 pyogrio 抛 DataSourceError 后会去试没装的 fiona，
        # 用户看到的是一句「requires the fiona package」——离真正原因差得远
        raise ValueError(f"文件不存在：{Path(path).name}")
    try:
        gdf = gpd.read_file(path, **opts)
    except Exception as first:
        try:
            gdf = gpd.read_file(path, engine="fiona", **opts)
        except Exception:
            raise ValueError(f"读取失败：{first}") from first
    if gdf.empty:
        raise ValueError("读取成功但没有任何要素。")
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    return gdf


def _mojibake_score(gdf: gpd.GeoDataFrame) -> int:
    cols = "".join(str(c) for c in gdf.columns)
    return cols.count("\ufffd") + cols.count("?") + sum(1 for ch in cols if ord(ch) < 32)


def _cpg_encoding(path: Path) -> str | None:
    """从 .cpg 文件里读出编码（Shapefile 自带的代码页声明）。

    OSM / QGIS / ArcGIS 导出的 Shapefile 基本都带 .cpg，读了就能一次读对；
    没有 .cpg 时才需要「读三遍择优」那种笨办法 —— 一个 100MB 的
    Shapefile 读三遍要 20 秒，读一遍只要 7 秒。
    """
    cpg = path.with_suffix(".cpg")
    try:
        if not cpg.exists():
            return None
        txt = cpg.read_text(encoding="ascii", errors="ignore").strip()
    except Exception:
        return None
    if not txt:
        return None
    key = txt.lower().replace("_", "-")
    if key in ("utf-8", "utf8", "65001", "cp65001"):
        return "utf-8"
    if key in ("gbk", "936", "cp936", "gb2312", "gb18030", "euc-cn"):
        return "GBK"
    if key.isdigit():
        try:
            return f"cp{int(key)}"
        except Exception:
            return None
    if key.startswith("cp") or key.startswith("iso-8859") or key.startswith("windows-"):
        return txt
    return None


def load_vector(path: Path) -> gpd.GeoDataFrame:
    """读取任意矢量文件；Shapefile 自动在 UTF-8 / GBK 之间择优。"""
    path = Path(path)
    if path.suffix.lower() not in (".shp", ".dbf"):
        return read_vector(path)
    import warnings
    # 有 .cpg 就按它声明的编码读一次，不再「三遍择优」
    enc = _cpg_encoding(path)
    if enc:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                gdf = read_vector(path, encoding=enc)
            if _mojibake_score(gdf) == 0:
                return gdf
        except Exception:
            pass
    best = read_vector(path)
    best_score = _mojibake_score(best)
    for enc in ("GBK", "utf-8"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                cand = read_vector(path, encoding=enc)
            sc = _mojibake_score(cand)
            if sc < best_score:
                best, best_score = cand, sc
        except Exception:
            pass
    return best


def load_csv(path: Path) -> gpd.GeoDataFrame:
    """CSV 需带经纬度列（lon/lat 或 经度/纬度）或 WKT 列。"""
    df = None
    for enc in ("utf-8-sig", "GBK", "utf-8"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except Exception:
            df = None
    if df is None:
        raise ValueError("CSV 编码无法识别。")
    lon_cols = [c for c in df.columns if str(c).lower() in ("lon", "lng", "x", "经度", "longitude")]
    lat_cols = [c for c in df.columns if str(c).lower() in ("lat", "y", "纬度", "latitude")]
    wkt_cols = [c for c in df.columns if str(c).lower() in ("wkt", "geometry", "geom")]
    if lon_cols and lat_cols:
        return gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[lon_cols[0]], df[lat_cols[0]]), crs=4326)
    if wkt_cols:
        from shapely import wkt
        return gpd.GeoDataFrame(df, geometry=df[wkt_cols[0]].apply(wkt.loads), crs=4326)
    raise ValueError("CSV 需要包含经纬度列（lon/lat 或 经度/纬度）或 WKT 列。")


def load_any(path: Path) -> gpd.GeoDataFrame:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return load_csv(path)
    return load_vector(path)


def extract_zip(zip_path: Path) -> tuple[Path, list[Path]]:
    """解压并返回（解压根目录, 矢量文件列表，按体积降序）。"""
    zip_path = Path(zip_path)
    out = zip_path.parent / ("x_" + zip_path.stem[:12])
    n = 1
    while out.exists():
        n += 1
        out = zip_path.parent / f"x_{zip_path.stem[:12]}_{n}"
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    files = sorted({p for ext in VECTOR_EXTS for p in out.rglob("*" + ext)},
                   key=lambda p: -p.stat().st_size)
    if not files:
        raise ValueError("压缩包中没有找到可识别的矢量文件（shp/gpkg/geojson/kml）。")
    return out, files


def file_info(path: Path) -> dict:
    """只读头信息，快速拿要素数与几何类型，不加载整份数据。"""
    info = {"features": 0, "geom": "", "crs": ""}
    try:
        import pyogrio
        meta = pyogrio.read_info(path)
        info["features"] = int(meta.get("features") or 0)
        info["geom"] = str(meta.get("geometry_type") or "")
        crs = meta.get("crs")
        info["crs"] = str(crs) if crs else ""
    except Exception:
        pass
    return info


# --------------------------------------------------------------------------- 写
def shp_field_names(cols: list[str]) -> list[str]:
    used: set[str] = set()
    out: list[str] = []
    for i, c in enumerate(cols, 1):
        c = str(c)
        if c.isascii() and len(c) <= 10:
            cand = c
        elif c.isascii():
            cand = c[:10]
        else:
            cand = f"F{i}"
        base, k = cand, 1
        while cand.lower() in used:
            k += 1
            cand = (base[:8] + str(k))[:10]
        used.add(cand.lower())
        out.append(cand)
    return out


def export_layer(gdf: gpd.GeoDataFrame, name: str, fmt: str, dest: Path) -> Path:
    """把图层写成文件，返回实际文件路径（fmt: geojson/gpkg/shp/csv）。"""
    fmt = fmt.lower()
    dest = Path(dest)
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "._-（）()、") or "layer"

    if fmt in ("geojson", "json"):
        g = gdf.to_crs(4326) if gdf.crs is not None else gdf
        payload = json_safe(json.loads(g.to_json(drop_id=True)))
        dest.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), "utf-8")
        return dest

    if fmt == "gpkg":
        gdf.to_file(dest, driver="GPKG", layer=safe[:60])
        return dest

    if fmt in ("shp", "zip"):
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "shp"
        d.mkdir()
        src = gdf.copy()
        mapping = {}
        if len(src):
            cols = [str(c) for c in src.columns if c != src.geometry.name]
            renamed = shp_field_names(cols)
            rename = {c: renamed[i] for i, c in enumerate(cols)}
            for orig, new in rename.items():
                if orig != new:
                    mapping[new] = orig
            src = src.rename(columns=rename)
        src.to_file(d / f"{safe[:40]}.shp", driver="ESRI Shapefile", encoding="UTF-8")
        if mapping:
            (d / "字段对照.txt").write_text(
                "Shapefile 字段名限制 10 字节，以下字段已改名，对照如下：\n"
                + "\n".join(f"{k}\t←\t{v}" for k, v in mapping.items()) + "\n",
                "utf-8")
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for f in d.iterdir():
                z.write(f, f.name)
        return dest

    if fmt == "csv":
        df = gdf.drop(columns=[gdf.geometry.name]).copy()
        df["wkt"] = (gdf.to_crs(4326).geometry.to_wkt()
                     if gdf.crs is not None else gdf.geometry.to_wkt())
        df.to_csv(dest, index=False, encoding="utf-8-sig")
        return dest

    raise ValueError(f"不支持的导出格式：{fmt}")
