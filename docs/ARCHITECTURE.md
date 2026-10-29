# 架构与原理

这份文档讲三件事：**代码放在哪**、**一帧地图是怎么画出来的**、**一句话需求是怎么变成图层的**。
细节都在代码注释里，这里只写「为什么这么设计」和「改动时必须守住什么」。

- 主程序入口：`native_app.py` → `native/app.py::MainWindow`
- 渲染核心：`native/mapview.py`（2000+ 行，全项目最重的文件）
- 规划与执行：`app/agent.py` → `app/engine.py`

---

## 1. 目录结构

```
gis-agent/
├── native_app.py            桌面版入口（创建 QApplication + MainWindow）
├── run.py                   网页版入口（uvicorn 起 FastAPI）
├── desktop.py               网页版套原生窗口（pywebview，可选）
├── 启动.bat / 调试启动.bat    Windows 双击启动（后者带控制台，便于看报错）
├── requirements.txt         依赖清单（分「核心 / 桌面 / 可选网页版」三段）
├── preview.png              README 首图
│
├── app/                     与界面无关的「引擎层」：数据、算子、规划、IO
│   ├── engine.py            Store（图层仓库）+ Layer + 25 个算子的真实实现 + Result
│   ├── agent.py             两级规划器（规则 / 大模型）+ 计划执行器 run_plan + 算子签名表
│   ├── custom_ops.py        自定义算子：AST 白名单校验、受限沙箱、落盘与装载
│   ├── io_utils.py          读写矢量数据：编码择优、zip 解压、导出 4 种格式
│   ├── samples.py           生成 4 个示例图层（成都某片区）
│   ├── config.py            模型设置（Base URL / Key / Model）的规范化与解释
│   ├── util.py              JSON 安全化（NaN/Inf/时间/numpy 标量 → JSON 友好）
│   └── main.py              网页版：FastAPI 路由 + 会话态（桌面版不加载它）
│
├── native/                  纯 Qt 桌面界面（QWidget + QPainter，无 WebView）
│   ├── app.py               主窗口：三栏布局、图层面板、算子面板、状态栏、撤销栈、载入队列
│   ├── mapview.py           自绘地图 + 渲染管线（渲染集、渲染线程、缓存、拾取、选区）
│   ├── chat.py              对话面板：气泡、步骤卡片、输入容器、指令历史、贴底滚动
│   ├── dialogs.py           属性表（分页/排序/筛选）、模型设置、多图层导入、算子确认
│   ├── theme.py             全局 QSS（暖白底 + 陶土橙，参照 Claude Code 桌面端）
│   ├── format.py            数值/单位/字段中文名格式化
│   └── assets/              应用图标（png / ico，由 tools/make_icon_assets.py 生成）
│
├── web/                     网页版前端（原生 JS + Canvas，与桌面版共用 app/ 引擎）
│   ├── index.html           单页骨架
│   ├── map.js               Canvas 渲染 + 交互
│   ├── app.js               对话、图层面板、接口调用
│   └── style.css
│
├── tools/                   验证与性能工具（不属于程序运行时）
│   ├── verify_render.py     渲染完备性 + 同视口重建像素比对
│   ├── perf_profile.py      一帧耗时分解（构建/简化/绘制/合成）
│   ├── perf_roads.py        道路绘制微基准（笔宽、端帽、抗锯齿对比）
│   ├── perf_backend.py      CPU 光栅 vs OpenGL 逐图层对比
│   ├── smoke_app.py         整机冒烟：真启动、真载入、截图、关闭
│   ├── shot_window.py       整窗截图（真字体路径，用于 README 配图）
│   ├── shot_tools.py        左栏截图 + 分组几何打印
│   ├── make_icon_assets.py  由绘制代码生成 icon.png / icon.ico
│   └── test_*.py            回归：渲染/显隐/对话/步骤卡/自定义算子/交互/滚动/不阻塞
│
├── samples/                 内置示例（GeoJSON，首次打开由「示例数据」按钮载入）
└── docs/
    ├── perf/                验证截图与性能对照图（README 里引用）
    └── qgis-ref/            对照用的 QGIS 头文件（QgsSimplifyMethod 等）
```

**分层与依赖方向**（箭头 = 允许依赖）：

```
   native/  ──────┐
   (Qt 界面)      ├──►  app/  ──►  geo 生态
   web/     ──────┘     (引擎)     (geopandas / shapely / pyproj)
                          │
                          └──►  data/  （运行时产生：上传数据、设置、自定义算子）
```

`app/` 里**不出现任何 Qt 或 FastAPI 的 import**——这是刻意的：桌面版和网页版共用同一套算子与规划逻辑，
新增一个算子只写一遍。反过来 `native/` 也不直接调 geopandas 做运算，只调 `app.engine`。

---

## 2. 数据模型

```python
Layer(id, name, gdf, color, op, source, parent)   # 一个图层 = 一份 GeoDataFrame + 元信息
Store.layers: dict[str, Layer]                    # 有序字典，顺序即渲染顺序
Result(ok, message, layer=None, warnings=[], data={})   # 每个算子的返回
```

三条硬约束（改动时别破坏）：

1. **原始图层只读**。算子一律不改入参 `gdf`，结果要么是新图层，要么什么都不建。
2. **零变化不建层**。「几何修复 / 去重」发现没有改动时返回 `layer=None`，
   否则图层面板会凭空多出一个和原层几乎同名的副本。
3. **父子关系可追溯**。`Layer.source` 指向上游图层 id，`op` 记录是哪个算子产出的；
   撤销、导出命名、大模型引用都靠它。

---

## 3. 渲染管线（`native/mapview.py`）

### 3.1 一帧的生命周期

```
 视图变化（缩放/平移/显隐/图层增删）
        │
        ▼
 _invalidate_frame()  ──160ms 去抖──►  _start_render()
                                          │
                     ┌────────────────────┴────────────────────┐
                     │ 1. _kick_build()：按需重建「渲染集」      │
                     │ 2. _make_snapshot()：给每层拍一份数据快照 │
                     │ 3. 命中图层图像缓存 → 直接复用旧图        │
                     └────────────────────┬────────────────────┘
                                          ▼
                         _RenderJob(epoch, [LayerJob...])  ──►  常驻渲染线程
                                                                  │ 每层画到自己的 QImage
                                                                  │ layerReady(lid, img, epoch)
                                          ┌───────────────────────┘
                                          ▼
             主线程 _on_layer_ready → _compose() 在**干净画布**上从零叠加 → _promote_pending()
                                          │
                            整帧完成 ─────► _frame = 新帧；paintEvent 画它
```

### 3.2 渲染集（RenderSet）：性能与正确性的交汇点

渲染集 = **某个经纬度矩形 + 某个简化容差** 下构建好的一整套 QPainterPath。

- 构建范围 = 视口 × `LOD_VIEW_PAD(1.6)`，再裁到图层范围（省域视图下等于全图层 → 以后平移不用重建）；
- 容差变化超过 `LOD_REBUILD_RATIO(1.35)` 才重建，误差控制在 1 像素内；
- 每层最多留 `LOD_KEEP_SETS(3)` 套，来回缩放直接命中。

> **不变量：完备性。** 一套渲染集对它声明的矩形必须包含**全部**相交要素。
> 只有满足这条，「矩形」才能当缓存判据。历史上有过抽样 + 用并集 bbox 记账的写法，
> 屏幕上就出现一块块没渲染的空洞——这是本文件里最贵的一次教训。

### 3.3 简化与「点降级」

- **容差按像素**：`SIMPLIFY_PX(0.8)` 换算成图面单位（`mpp × 0.8`），跟着缩放自动变细；
- **亚像素面要素降为点**：屏幕尺寸 < `DOT_PX(1.5)` 的面不再画轮廓，改用 `drawPoints` 画 1 个设备像素，
  既不丢要素，也省掉海量顶点（省域视图下 24 万栋建筑只剩 24 万个点，而不是 24 万条路径）；
- 简化后若某要素「消失」或面积缩水 > 60%，回退用原几何。

### 3.4 线程模型

```
GUI 线程                    Worker(QThread)              常驻渲染线程
─────────                   ─────────────────            ──────────────
事件循环 / 绘制             解析数据、跑算子             画各图层到 QImage
  │                            │                            │
  ├─ 10ms 一片地构建路径        ├─ gpd.read_file            ├─ 队列只留最新一帧
  ├─ 合成图层图                ├─ run_plan                  ├─ epoch 不符直接丢弃
  └─ paintEvent 画帧           └─ 结果回主线程              └─ 每层画完先回传
```

为什么这么分：

- 解析 shapefile 与空间运算都**长时间持有 GIL**，必须离开 GUI 线程，否则界面直接冻住；
- 渲染单独一条**常驻**线程（不是每次新建），队列只保留最新一帧，连续缩放时旧帧自动作废；
- 构建路径却留在 GUI 线程、按 10ms 分片——因为 QPainterPath 的创建很轻、但要访问共享数据，
  分片后界面在「准备渲染数据」阶段依然能响应。

### 3.5 缓存层级

| 层 | 键 | 命中效果 |
|---|---|---|
| 帧缓存 `_frame` | 场景签名 + 视图 | 平移/缩放时直接贴旧帧（缩放的位图变换在 `paintEvent` 里做） |
| 图层图像 `_img_cache` | `(scale, tx, ty, w, h, dpr, 图层几何版本, 颜色, op, 线宽档位)` | 切显隐、调顺序几乎零成本 |
| 渲染集 `_RenderSet` | 矩形 + 容差 | 来回缩放不重建几何 |

注意图层缓存的键用**图层自己的** `rev`：别的图层重建不该把这张图废掉。

### 3.6 画笔与坐标

所有在**世界坐标变换**下使用的画笔必须 `setCosmetic(True)`，
否则 `QPen(width=2.4)` 会被解释成 2.4 个世界单位——在高缩放下等于上千万像素宽，
屏幕上就是「点一下出现一个大圆」那种事故。同理，`drawPoints` 只有 `width=0` 才是 1 像素
（笔宽 > 1 会按端帽把退化的点画成横条）。

### 3.7 线宽分档（性能关键）

Qt 光栅引擎对**整数宽度 1** 的画笔有一条快速路径；`> 1` 会走完整的抗锯齿描边器
（每个顶点生成四边形 + 端帽 + 拐角连接）。实测 47 万条道路 / 94 万顶点，省域视图：
`1.6px` 1914ms，`1.0px` 79ms，落笔像素几乎一样。所以「每像素地面米数 ≥ 45（约 1:17 万）」一律画 1px——
这个比例尺上 6 米宽的路本来就不足 0.01 像素，1px 才是正确画法。

---

## 4. 规划与执行（`app/agent.py`）

```
用户一句话
   │
   ├─► 闲聊判定 chit_chat()          —— 问候/道谢/问能力，直接回人话
   ├─► 生成算子 wants_new_operator()  —— 「写一个算子：…」走代码生成
   └─► 规划
        ├─ 有大模型：llm_plan()  — 图层清单+字段+算子签名 → JSON 计划
        │     · 提示词里写死 {tool, desc, args}
        │     · 返回后 normalize_plan() 归一（兼容 op/params、别名、参数摊平）
        │     · JSON 解析失败或 finish_reason=length 各重试一次
        │     · 仍失败 → 回退规则引擎，并静默 2 分钟（避免每条都卡）
        └─ 无大模型：规则引擎 — 拆句（然后/再/；）→ 图层名模糊匹配 → 单位数字抽取
                                                    → 上一步输出自动串给下一步
   │
   ▼
run_plan(): 逐步执行，产出步骤卡片数据
   · `$prev` 表示上一步产出；上一步没建层则沿用它的输入图层（链条不断）
   · 任一步抛异常 → 中止后续，前面已完成的保留
   · 每步记录 elapsed / message / warnings / data（小表）/ 新图层 meta
```

规划器**永远不直接改数据**，只产出「计划」这一数据结构；执行、建层、撤销登记都在 `run_plan` 之后由主窗口完成。
这样规则引擎和大模型走的是同一条执行路径，行为可对比、可测试。

---

## 5. 算子体系（`app/engine.py`）

- 签名统一：`op(store, layer, **args) -> Result`，第一个参数是 `Store`（便于跨图层操作）；
- **CRS 策略**：距离/面积运算前把几何投到 **CGCS2000 3 度带**（按数据中心经度选带，EPSG:4534–4554），
  境外退回 UTM；结果字段统一带 `_m` / `_m2` 后缀；
- **几何有效性**：入口处 `make_valid` 并计数（>5 万要素的图层跳过逐个校验）；
- **文本型 ID**：Shapefile 的 `osm_id` 是字符串，表达式里写数字会自动补引号，
  否则 `osm_id == 4814005` 会静默筛出 0 条；
- **自定义算子**：`custom_ops.py` 先用 AST 白名单拦截 `import / while / exec / __dunder__`，
  再在受限 `eval` 环境里跑；先拿前 200 个要素试跑，确认后才落盘到 `data/operators/`，
  重启自动装回，与内置算子同权（工具表、提示词、算子库三处同步可见）。

---

## 6. 交互状态

| 状态 | 存在哪 | 要点 |
|---|---|---|
| 选区 | `mapview._sel = {layer_id: np.ndarray}` | 只作用于当前图层；发送指令时复制成 `原名_选中N` 独立图层再运算 |
| 高亮 | `mapview._hl = (layer_id, pos)` | 属性表点行 → 高亮 + 缩放；和 hover 分开，互不覆盖 |
| 撤销栈 | `app._undo_stack`（20 步） | 只记「加图层 / 删图层 / 改名」；重做需要图层对象，所以存 `(lid, layer)` |
| 贴底 | `chat._stick` | 用户在底部才跟随；`rangeChanged` 兜住「内容后来才长高」的情况 |
| 载入进度 | `app._load_total/_done/_cancel` | 按文件推进；取消 = 当前文件读完就停，已载完的保留 |

---

## 7. 验证与性能工具

改渲染或算子前后，按这个顺序跑：

```bash
.venv\Scripts\python.exe tools\verify_render.py   # 完备性 + 像素比对（最该看的）
.venv\Scripts\python.exe tools\perf_profile.py    # 一帧耗时拆到 构建/简化/绘制/合成
.venv\Scripts\python.exe tools\test_ux_batch.py   # 选区/撤销/属性表联动
.venv\Scripts\python.exe tools\smoke_app.py       # 整机冒烟 + 截图
```

`perf_roads.py` / `perf_backend.py` 是「换实现之前先量一下」的工具：
前者比笔宽/端帽/抗锯齿，后者比 CPU 光栅与 OpenGL。

---

## 8. 扩展指引

**加一个算子**：在 `engine.py` 写 `_my_op(store, layer, ...) -> Result` 并注册进 `OPERATIONS`，
再到 `agent.py` 的 `TOOL_SPECS` / `TOOL_LABEL` 补签名与中文名，算子库按钮、大模型提示词、规则引擎三处自动可见。

**加一个渲染特性**：先想清楚它属于哪一层——几何构建（`_build_*`）、绘制（`_paint_layer`）、
还是交互叠加（`_draw_*`，画在屏幕坐标系）。要改画笔，记得世界坐标下必须 cosmetic。

**加一个交互**：状态尽量放在 `MapView`（视图相关）或 `MainWindow`（文档相关），
两者通过信号通信（`selectionChanged` / `picked` / `hoverChanged` / `mapMenu`），别互相直接调方法。

---

## 9. 已知边界

- 载入 77 万要素约 20 秒，其中 15 秒是 `pyogrio` 解析 shapefile，不是渲染；
- 放大级别的 1 秒左右主要在几何构建（每个顶点一次 `path.lineTo`）与简化，属于每级只付一次的成本；
- 单机单会话，图层存内存，关窗即清空（需要留存就导出 GeoJSON/GPKG）；
- 网页版功能是桌面版的子集，且没有渲染线程与缓存分层，大图层体验明显弱于桌面版。
