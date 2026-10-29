"""FastAPI 服务层：文件读写、图层 API、Agent 对话、导出。"""
from __future__ import annotations

import io
import json
import math
import os
import shutil
import tempfile
import traceback
import uuid
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile, Body
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import Store, OPERATIONS, Result, to_display_geojson
from .agent import Planner, run_plan, SUGGESTIONS, TOOL_LABEL
from .samples import ensure_samples, SAMPLE_FILES
from .util import json_safe
from .io_utils import (export_layer as write_layer_file, extract_zip, file_info,
                       load_any)

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
CONFIG = DATA / "settings.json"
UPLOADS.mkdir(parents=True, exist_ok=True)

class SafeJSONResponse(JSONResponse):
    """严格 JSON 响应：先把 NaN/Inf/numpy 类型清洗掉，再序列化。

    否则真实数据里的空值会让 json.dumps(allow_nan=False) 抛错，接口变成 500。
    """

    def render(self, content) -> bytes:  # type: ignore[override]
        return json.dumps(
            json_safe(content), ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")


app = FastAPI(title="GIS 矢量数据 Agent", default_response_class=SafeJSONResponse)
store = Store()
planner = Planner(store)
history: list[dict] = []
IMPORT_JOBS: dict[str, dict] = {}   # 多图层压缩包的待选清单：job -> {root, files, source}


# --------------------------------------------------------------------------- 配置
def load_cfg() -> dict:
    # 走 app/config.py 的统一实现：会把「只填了 key、没填 Base URL」的情况补全，
    # 否则 Web 版也会出现「明明配了 key 却一直走规则引擎」
    from app.config import load_cfg as _load
    return _load()


def save_cfg(cfg: dict) -> None:
    from app.config import save_cfg as _save
    _save(cfg)


# --------------------------------------------------------------------------- 模型
class ChatIn(BaseModel):
    message: str
    current: str | None = None
    use_llm: bool = True


class OpIn(BaseModel):
    tool: str
    args: dict = {}


class SettingsIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = False


# --------------------------------------------------------------------------- 路由
@app.get("/api/layers")
def list_layers():
    return {"layers": [l.meta() for l in store.layers.values()]}


@app.get("/api/layers/{lid}/geojson")
def layer_geojson(lid: str, limit: int = 30000):
    lay = store.get(lid)
    if not lay:
        raise HTTPException(404, "图层不存在")
    total = len(lay.gdf)
    gj = to_display_geojson(lay.gdf, limit=limit)
    gj["_meta"] = {"total": total, "shown": len(gj.get("features", [])), "truncated": total > limit}
    return gj


@app.get("/api/layers/{lid}/table")
def layer_table(lid: str, limit: int = 200):
    lay = store.get(lid)
    if not lay:
        raise HTTPException(404, "图层不存在")
    g = lay.gdf
    cols = [str(c) for c in g.columns if c != g.geometry.name]
    df = g.drop(columns=[g.geometry.name]).head(limit)
    rows = []
    for row in df.itertuples(index=False):
        rows.append([_clean_cell(v) for v in row])
    return {"columns": cols, "rows": rows, "total": len(g), "shown": len(rows),
            "crs": lay.meta()["crs"], "fields": len(cols)}


def _clean_cell(v):
    """单元格取值：空值统一成 null，整数值的浮点去掉小数点，避免 241586.0 这种显示。"""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        if v.is_integer() and abs(v) < 1e15:
            return int(v)
        return round(v, 6)
    if isinstance(v, (list, dict)):
        return json.dumps(json_safe(v), ensure_ascii=False)[:200]
    if isinstance(v, (int, str, bool)):
        return v
    cleaned = json_safe(v)
    if isinstance(cleaned, (int, str, bool)) or cleaned is None:
        return cleaned
    return str(cleaned)


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    name = Path(file.filename or "data").name
    suffix = Path(name).suffix.lower()
    if suffix not in (".shp", ".zip", ".geojson", ".json", ".gpkg", ".kml", ".gml", ".csv"):
        raise HTTPException(400, f"暂不支持的格式 {suffix}，请上传 shp(zip)/geojson/gpkg/kml/csv")
    work = UPLOADS / uuid.uuid4().hex[:8]
    work.mkdir(parents=True, exist_ok=True)
    dest = work / name
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    if suffix == ".shp":
        # 单独的 .shp 不完整，必须有同名 .dbf/.shx（否则读出来只有几何没有属性）
        stem = dest.with_suffix("")
        missing = [ext for ext in (".dbf", ".shx") if not Path(str(stem) + ext).exists()]
        if missing:
            raise HTTPException(
                400,
                f"Shapefile 需要 .shp/.dbf/.shx 一起上传，缺少 {'、'.join(missing)}。"
                f"请把整套文件打成 zip 再拖进来（右键 → 压缩为 ZIP）。")

    try:
        if suffix == ".zip":
            root, files = extract_zip(dest)
            if len(files) > 1:
                # 多个图层：不擅自决定，交给用户勾选
                job = uuid.uuid4().hex[:8]
                cands = []
                for i, f in enumerate(files):
                    info = file_info(f)
                    cands.append({
                        "id": i,
                        "name": f.stem,
                        "rel": str(f.relative_to(root)),
                        "size_mb": round(f.stat().st_size / 1e6, 1),
                        "ext": f.suffix.lower(),
                        **info,
                    })
                IMPORT_JOBS[job] = {"root": root, "files": files, "source": name}
                return {"need_choice": True, "job": job, "source": name,
                        "candidates": cands,
                        "message": f"压缩包内有 {len(files)} 个矢量图层，请选择要载入的图层。"}
            dest = files[0]
        gdf = load_any(dest)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"读取失败：{e}")
    lay = store.add(gdf, Path(name).stem, source=name, op="load")
    history.append({"role": "system", "content": f"用户上传图层 {lay.name}（{len(gdf)} 要素）"})
    return {"layer": lay.meta(), "message": f"已加载「{lay.name}」：{len(gdf)} 个要素，坐标系 {lay.meta()['crs']}。"}


class ImportIn(BaseModel):
    job: str
    picks: list[int] = []


@app.post("/api/import")
def import_layers(body: ImportIn):
    """按用户勾选，从压缩包里载入指定图层。"""
    job = IMPORT_JOBS.pop(body.job, None)
    if job is None:
        raise HTTPException(404, "该导入任务已失效，请重新上传压缩包。")
    if not body.picks:
        shutil.rmtree(job["root"], ignore_errors=True)
        return {"layers": [], "message": "已取消导入。"}
    added, failed = [], []
    try:
        for idx in body.picks:
            if not (0 <= idx < len(job["files"])):
                continue
            f = job["files"][idx]
            try:
                gdf = load_any(f)
                lay = store.add(gdf, f.stem, source=job["source"], op="load")
                added.append(lay.meta())
            except Exception as e:
                failed.append(f"{f.stem}：{e}")
    finally:
        # 数据已进内存，解压出来的临时目录可以清掉，省几百 MB 磁盘
        shutil.rmtree(job["root"], ignore_errors=True)
    msg = f"已载入 {len(added)} 个图层：" + "、".join(a["name"] for a in added) if added else "没有图层被载入。"
    if failed:
        msg += f"（{len(failed)} 个失败：{'；'.join(failed[:3])}）"
    history.append({"role": "system", "content": msg})
    return {"layers": added, "message": msg, "failed": failed}


@app.delete("/api/layers/{lid}")
def delete_layer(lid: str):
    store.layers.pop(lid, None)
    return {"ok": True}


@app.post("/api/op")
def run_op(body: OpIn):
    if body.tool not in OPERATIONS:
        raise HTTPException(400, f"未知算子 {body.tool}")
    args = dict(body.args)
    for k in ("layer", "other", "mask"):
        if isinstance(args.get(k), str):
            lay = store.get(args[k])
            if lay:
                args[k] = lay.id
    try:
        res: Result = OPERATIONS[body.tool](store, **args)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": res.ok, "message": res.message, "warnings": res.warnings, "data": res.data,
            "layer": res.layer.meta() if res.layer else None}


@app.post("/api/chat")
def chat(body: ChatIn):
    msg = (body.message or "").strip()
    if not msg:
        raise HTTPException(400, "指令为空")
    if not store.layers:
        small = planner.chit_chat(msg, has_data=False)
        return {"reply": small or ("还没有数据。先把矢量文件拖进来（支持 shp 压缩包 / GeoJSON / GPKG / KML / CSV），"
                                   "或者点「载入示例数据」先试一遍。"), "steps": [], "layers": []}
    cfg = load_cfg()
    planner.store = store
    small = planner.chit_chat(msg, has_data=True)
    if small:
        reply = small
        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": reply})
        return {"reply": reply, "steps": [], "layers": [l.meta() for l in store.layers.values()],
                "planner": "chat", "note": ""}
    plan, source, note = None, "rule", ""
    if body.use_llm and cfg.get("enabled") and cfg.get("api_key") and cfg.get("base_url"):
        out = planner.llm_plan(msg, body.current, cfg, history)
        if out and out.get("plan"):
            plan, source = out["plan"], "llm"
            note = out.get("reply") or ""
        elif out and out.get("error"):
            note = out["error"]
    if not plan:
        plan = planner.rule_plan(msg, body.current)

    # 导出意图单独处理
    exports = [p for p in plan if p.get("tool") == "__export__"]
    plan = [p for p in plan if p.get("tool") != "__export__"]

    steps, lines, new_layers = [], [], []
    if plan:
        try:
            steps, lines, new_layers = run_plan(store, plan)
        except Exception as e:
            steps = [{"tool": "-", "label": "规划", "args": {}, "status": "error",
                      "message": str(e), "warnings": [], "data": {}, "elapsed": 0}]
            lines = [f"✗ {e}"]

    for ex in exports:
        lay = store.get(ex["args"].get("layer"))
        fmt = ex["args"].get("format", "geojson")
        if lay:
            steps.append({"tool": "export", "label": "导出", "args": {"layer": lay.name, "format": fmt},
                          "status": "ok", "message": f"生成 {fmt.upper()} 下载",
                          "warnings": [], "data": {"layer_id": lay.id, "format": fmt}, "elapsed": 0})
            lines.append(f"已准备「{lay.name}」的 {fmt.upper()} 导出，点击下方按钮下载。")

    if not steps:
        # 大模型给了自然语言回复（闲聊/问询）就用它，别把它丢掉换模板
        reply = (note or "").strip() or (
            "这句我还拆不出可执行的步骤。可以这样描述：\n"
            "· 对「图层名」做 500 米缓冲区\n"
            "· 用「边界」裁剪「地块」\n"
            "· 按「类型」融合后统计各类面积\n"
            "也可以把算子说明看一下，或先在设置里接入大模型（DeepSeek/OpenAI 兼容）来理解更自由的表达。")
        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": reply})
        return {"reply": reply, "steps": [], "layers": [l.meta() for l in store.layers.values()],
                "planner": source, "note": note}

    ok = sum(1 for s in steps if s["status"] == "ok")
    err = sum(1 for s in steps if s["status"] == "error")
    head = f"执行了 {len(steps)} 步" + (f"，其中 {err} 步失败" if err else "，全部成功") + "："
    reply = (note + "\n" if note else "") + head + "\n" + "\n".join(f"{i+1}. {l}" for i, l in enumerate(lines))
    history.append({"role": "user", "content": msg})
    history.append({"role": "assistant", "content": reply})
    return {"reply": reply, "steps": steps, "layers": [l.meta() for l in store.layers.values()],
            "planner": source}


@app.get("/api/layers/{lid}/export")
def export_layer(lid: str, format: str = "geojson"):
    lay = store.get(lid)
    if not lay:
        raise HTTPException(404, "图层不存在")
    fmt = format.lower()
    if fmt not in ("geojson", "json", "gpkg", "shp", "zip", "csv"):
        raise HTTPException(400, f"不支持的导出格式：{format}")
    fmt = {"json": "geojson", "zip": "shp"}.get(fmt, fmt)
    ext = {"geojson": ".geojson", "gpkg": ".gpkg", "shp": ".zip", "csv": ".csv"}[fmt]
    tmp = Path(tempfile.mkdtemp())
    safe = "".join(c for c in lay.name if c.isalnum() or c in "._-（）、()") or "layer"
    try:
        dest = write_layer_file(lay.gdf, lay.name, fmt, tmp / f"{safe}{ext}")
    except Exception as e:
        raise HTTPException(500, f"导出失败：{e}")
    media = {"geojson": "application/geo+json", "csv": "text/csv"}.get(fmt)
    return FileResponse(dest, filename=dest.name, media_type=media)


@app.get("/api/settings")
def get_settings():
    cfg = load_cfg()
    ready = bool(cfg.get("enabled") and cfg.get("api_key") and cfg.get("base_url"))
    return {"base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
            "enabled": bool(cfg.get("enabled")), "has_key": bool(cfg.get("api_key")),
            "ready": ready}


@app.post("/api/settings")
def set_settings(body: SettingsIn):
    cfg = load_cfg()
    cfg.update({"base_url": body.base_url.strip(), "model": body.model.strip(),
                "enabled": bool(body.enabled)})
    if body.api_key and body.api_key != "__KEEP__":
        cfg["api_key"] = body.api_key.strip()
    save_cfg(cfg)
    return {"ok": True}


@app.get("/api/suggestions")
def suggestions():
    return {"suggestions": SUGGESTIONS, "tools": [{"name": k, "label": TOOL_LABEL.get(k, k)}
                                                  for k in OPERATIONS if k != "describe"]}


@app.post("/api/samples")
def load_samples():
    files = ensure_samples()
    added = []
    for f in files:
        gdf = gpd.read_file(f)
        if gdf.crs is None:
            gdf = gdf.set_crs(4326)
        lay = store.add(gdf, f.stem, source=f.name, op="sample")
        added.append(lay.meta())
    return {"layers": added,
            "message": f"已载入 {len(added)} 个示例图层：" + "、".join(a["name"] for a in added)}


@app.post("/api/reset")
def reset():
    store.layers.clear()
    store._n = 0
    history.clear()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "layers": len(store.layers)}


app.mount("/", StaticFiles(directory=str(WEB), html=True), name="web")
