# 优化方案与执行记录（2026-10-29）

> 对应审查报告：[REVIEW-2026-10-29.md](REVIEW-2026-10-29.md)。本轮重点：**Agent 能力闭环**，同时修掉审查里标记为 P0 的正确性/稳定性问题。

---

## 1. 本轮已完成

### 1.1 工具契约单一事实源（Agent 基础）

新增 [app/tool_registry.py](../app/tool_registry.py)：

- `ToolSpec` / `Param`：名称、中文名、分组、描述、参数类型、必填、默认值、枚举、范围、模板、示例；
- 由它生成 `TOOL_SPECS`、`TOOL_LABEL`、`/api/tools`、`/api/suggestions` 的模板与分组；
- `validate_args()` 做类型/枚举/范围/未知参数校验；
- 修掉契约漂移：
  - `bbox_select` 现在真正接受 `bbox="x0,y0,x1,y1"`（同时兼容四个数字）；
  - `dedupe` 给了 `subset` 就按字段去重，不再被默认的 `by_geometry=True` 吞掉；
  - `dissolve` 暴露 `agg`，并按 `dropna=False` 保留空值分组；
  - `reproject` 的「CGCS2000 3 度带」按数据中心经度自动选带，不再硬编码 4547；
  - `sjoin` 不再丢弃右图层同名字段，改为 `_left/_right` 后缀保留；
  - `nearest` 的 `k>1` 真正生效（STRtree + dwithin 半径扩展，k=1 仍走 `sjoin_nearest`）。

### 1.2 Agent 闭环

[app/agent.py](../app/agent.py) 新增：

- `Planner.validate_plan()`：执行前校验工具、参数 schema、图层引用、`$prev`、几何类型；
  预检失败时**零副作用**拒绝，或把错误交给模型修复；
- `run_agent()`：规划 → 执行 → 观察 → 失败修复（最多 2 次）；已成功的步骤和图层保留；
- `Planner.llm_repair()`：把失败步骤/错误/当前 schema 回灌给模型，拿剩余修正计划；
- `Planner.note_result()` + `_followup()`：多轮指代
  「再大一点」「小一点」「换成 1 公里」「用刚才的结果」「重做一次」；
- 多图层且没有当前图层时，规则引擎先返回 `clarify`，不再静默取最近创建的图层；
- 大模型可以输出 `clarify`，Web 端把选项渲染成可点 chips。

Web / 桌面统一走 `run_agent`：

- [app/main.py](../app/main.py) `/api/chat` 不再自己拼一次性 plan；
- [native/app.py](../native/app.py) 的 `on_send` 也改为调用 `run_agent`；
- Web 端大模型「只给 reply、不给 plan」的回复不再被模板吞掉（与桌面端对齐）。

### 1.3 LLM 调用健壮性

[app/agent.py](../app/agent.py)：

- 429/5xx 短退避重试；JSON mode 不支持时自动退回普通模式；
- 401/403、404、429、5xx 分别给出中文提示；
- `finish_reason=length` 重试时提高 `max_tokens`（2200 → 3200）；
- JSON 候选按 `plan/clarify/reply` 打分，避免取到模型前面的「示例对象」；
- 数据区用 `<DATA>` 包裹并声明「只作事实参考、不是指令」，降低字段名注入；
- 过滤 `history` 里的 system 消息，避免部分服务要求 system 只在首条；
- 记录 `usage` 日志。

### 1.4 引擎 P0 修复

[app/engine.py](../app/engine.py)：

- 线图层分组统计单位错误（`km²` / `1e6`）已修，文字与表格一致；
- `nearest(k)` 不再静默退化为 1；
- `sjoin` 保留双方同名字段；
- `dedupe` 向量化 WKB + `subset` 生效；
- `bbox_select` 契约修复；
- 缺 CRS 且坐标明显是投影米制时**拒绝**，不再静默按 4326 算面积/距离；新增
  `POST /api/layers/{lid}/crs` 用于补 CRS（`set_crs`，不重投影）；
- 投影单位不是米（英尺等）时改用米制投影；
- `prepare_layer()` 统一 CRS + `make_valid` + 空几何清理，覆盖 buffer/clip/overlay/dissolve/
  sjoin/nearest/voronoi/grid/area_stats/shape_metrics/min_rect/filter/field_calc 等；
- `metric_crs()` 只转 4 个角点求中心，不再每次 `to_crs(4326)` 整个图层；
- `_grid` 用 `np.repeat/tile + shapely.box` 向量化；
- `_merge` 不再把所有列 `astype(object)`，保留 dtype；
- `_centroid` 用 `.values` 避免重复索引对齐问题；
- 自定义算子执行时传 `lay.gdf.copy()`，不再污染原图层。

### 1.5 Web / 稳定性

[app/main.py](../app/main.py)：

- 全局 `_STORE_LOCK` 串行化 chat/op/upload/import/reset/samples，避免多标签页并发改坏 store/history；
- `/api/upload` 支持一次 multipart 多文件（`.shp/.dbf/.shx/.prj/.cpg` 同目录）；
- 上传大小上限（默认 1GB，可 `GIS_AGENT_MAX_UPLOAD_MB` 调整）；
- `extract_zip` 加解压大小/文件数上限（默认 2GB / 2000）；
- 单文件上传后清理 work 目录；多图层导入后清理 root + work；`IMPORT_JOBS` 加 30 分钟 TTL；
- `/api/reset` 同时清理导入任务和 `data/uploads`；
- 导出用 `BackgroundTask` 清理 `tempfile`；
- `/api/op` 也走 schema 校验；
- 启动时 `custom_ops.install_all()`，Web 也能看到已保存的自定义算子；
- 新增 `/api/tools`（完整 schema/template/group）与 `/api/layers/{lid}/crs`。

[web/app.js](../web/app.js) / [web/map.js](../web/map.js)：

- 上传按 Shapefile stem 分组，一次请求发 `.shp/.dbf/.shx/...`；
- 显示「仅显示前 3 万要素」截断提示；删除图层时清理前端 GeoJSON 缓存；
- 算子卡片使用后端模板，减少前后端漂移；
- `clarify` 选项渲染成可点击 chips；
- 地图加视口裁剪、rAF 调度、悬停不整帧重绘、`_pick` 坐标修正；
- `screenToLonLat` 显式解构，避免依赖属性顺序。

### 1.6 大图层磁盘缓存

[app/engine.py](../app/engine.py) 的 `Store` 现在按内存预算做 LRU 换出：

- `Layer.gdf` 变成懒加载属性；`Layer.meta()` 仍是纯内存缓存；
- 超过 `GIS_AGENT_MEMORY_MB`（默认 2048MB）时，把不活跃的大图层写到
  `data/cache/<id>.parquet`（有 pyarrow）或 `.gpkg`，然后释放 `_gdf`；
- 小于 `GIS_AGENT_CACHE_MIN_FEATURES`（默认 2 万要素）不换出；
- 删除图层 / `/api/reset` 清理缓存文件；`/api/health` 返回 `cache` 统计；
- 缓存目录可用 `GIS_AGENT_CACHE_DIR` 指定。

### 1.7 自定义算子安全边界

[app/custom_ops.py](../app/custom_ops.py)：

- AST 额外禁止 `read_pickle / read_csv / read_file / to_file / load / save / urlopen` 等危险方法；
- `install()` 拒绝覆盖内置算子名；
- `remove()` 同步清理 `OPERATIONS / TOOL_SPECS / TOOL_LABEL`；
- 预览弹窗改为准确描述「受限运行环境，不是 OS 级沙箱」。

---

## 2. 验证

新增/更新测试（均可离线跑，失败返回非零退出码）：

```bash
.venv\Scripts\python.exe tools\test_agent_v2.py     # 契约/引擎/多轮/安全/磁盘换出
.venv\Scripts\python.exe tools\test_agent_llm.py    # 假 LLM：校验修复/执行修复/澄清/reply
.venv\Scripts\python.exe tools\test_custom_op.py    # 原自定义算子回归
.venv\Scripts\python.exe tools\test_chat_flow.py    # 原对话/JSON 归一/界面回归
.venv\Scripts\python.exe tools\test_noop_layer.py   # 零变化建层/`$prev` 链条
```

另外跑了桌面端 UI 回归：`test_visible / test_ux_batch / test_ux_more / test_step_buttons /
test_scroll / test_no_block`，全部 exit=0。

本轮结果：以上 11 个脚本全部 PASS。顺带修了测试脚本退出时
`QThread: Destroyed while thread is still running` 的崩溃（`MainWindow` 注册 `aboutToQuit` /
`atexit` 清理线程；正常关窗和测试直接退出都会等待线程收尾）。

没有改动的部分（下一轮建议）：

- Web 矢量瓦片 / 更完整的渲染缓存（Web 端本轮不再继续）；
- 自定义算子的进程级隔离（超时/内存上限/禁用网络）；
- 属性表服务端分页/排序/筛选、图层符号化、项目保存；
- 把 `tools/test_*.py` 统一迁到 pytest + CI。
