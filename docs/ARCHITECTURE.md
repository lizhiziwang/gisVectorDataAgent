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
├── 启动.bat / 调试启动.bat    Windows 双击启动（后者带控制台，便于看报错）
├── requirements.txt         依赖清单（核心 / 桌面界面 / 栅格 / 可选补充）
├── preview.png              README 首图
│
├── app/                     与界面无关的「引擎层」：数据、算子、规划、IO
│   ├── engine.py            Store（图层仓库）+ Layer + 26 个矢量算子 + Result
│   ├── raster.py            RasterLayer（鸭子类型图层）+ rasterio 读窗口 / 拉伸
│   ├── raster_ops.py        8 个通用栅格算子（体检/裁剪/重投影/重采样/计算/分区统计/镶嵌/转矢量）
│   ├── raster_clip_worker.py 栅格裁剪子进程（隔离 GDAL/GIL，主界面不冻结）
│   ├── skills.py            Agent 技能卡解析 / 启停 / 规划提示词上下文
│   ├── agent.py             两级规划器（规则 / 大模型）+ Agent 闭环 + 计划预检 + 算子签名表
│   ├── tool_registry.py     算子契约单一事实源：参数 schema / 模板 / 分组 / 校验
│   ├── tool_catalog.py      工具库分类清单（矢量/栅格 → 功能分组），左栏与设置页共用
│   ├── tool_prefs.py        工具启用/停用运行时状态（左栏、模型候选、执行前检查共用）
│   ├── online_map.py        在线地图：校验 + Token/参数/请求头 + XYZ/WMTS/WMS URL（不依赖 Qt/GDAL）
│   ├── custom_ops.py        自定义算子：AST 白名单校验、受限执行、落盘与装载
│   ├── io_utils.py          读写矢量数据：编码择优、zip 解压、导出 4 种格式
│   ├── vector_provider.py   矢量 provider：文件头元信息 + 按 bbox 从源文件取要素（属性按 Arrow 批）
│   ├── layer_style.py       图层样式的默认值 / 规范化 / 签名（地图与样式弹窗共用）
│   ├── gdb.py               Esri FileGDB（.gdb）读取：pyopenfilegdb → GeoDataFrame
│   ├── samples.py           生成 4 个示例图层（成都某片区）
│   ├── config.py            模型设置（Base URL / Key / Model）的规范化与解释
│   └── util.py              JSON 安全化（NaN/Inf/时间/numpy 标量 → JSON 友好）
│
├── native/                  纯 Qt 桌面界面（QWidget + QPainter，无 WebView）
│   ├── app.py               主窗口：三栏布局、图层面板、算子面板、状态栏、撤销栈、载入队列
│   ├── mapview.py           自绘地图 + 矢量渲染管线（渲染集、渲染线程、缓存、拾取、选区）
│   ├── vectorstream.py      大图层屏幕范围流式渲染（分块建路径，不缓存整层路径）
│   ├── compare.py           地图对比容器（左右/上下分屏、卷帘、透明度、闪烁、视图同步）
│   ├── agent_settings.py    Agent 设置窗口（技能管理 / 工具库管理 / 每步确认开关 / 模型设置入口）
│   ├── rasterview.py        独立栅格渲染管线（RasterViewPort / 线程 / 缓存 / 拉伸）
│   ├── proj.py              两条管线共用的 Web Mercator 投影函数
│   ├── chat.py              对话面板：对话/轨迹、任务胶囊、工具状态、模型选择器、统计、计划卡、步骤卡片
│   ├── workspace.py         左栏工作区：gis-agent → 未分组 → 会话 / 会话图层引用
│   ├── pyramid.py           栅格 overview 检测 + GDAL C API 生成 .ovr（QThread 进度）
│   ├── taskcenter.py        地图顶部任务通知（TaskNotice）+ 旧面板测试用 TaskCenter
│   ├── dialogs.py           属性表（分页/排序/筛选）、模型设置、多图层导入、算子确认、在线地图配置
│   ├── style_dialog.py      图层样式弹窗：矢量符号 + 要素标注 + 栅格显示参数，带实时预览
│   ├── labels.py            要素标注取数：视口锚点 + 文本；无索引源文件走整层锚点表
│   ├── theme.py             全局 QSS（暖白底 + 陶土橙，参照 Claude Code 桌面端）
│   ├── format.py            数值/单位/字段中文名格式化
│   └── assets/              应用图标（png / ico，由 tools/make_icon_assets.py 生成）
│
├── tools/                   验证与性能工具（不属于程序运行时）
│   ├── verify_render.py     渲染完备性 + 同视口重建像素比对
│   ├── perf_profile.py      一帧耗时分解（构建/简化/绘制/合成）
│   ├── perf_roads.py        道路绘制微基准（笔宽、端帽、抗锯齿对比）
│   ├── perf_backend.py      CPU 光栅 vs OpenGL 逐图层对比
│   ├── smoke_app.py         整机冒烟：真启动、真载入、截图、关闭
│   ├── test_raster.py       栅格载入/混合渲染/显隐/缩放端到端
│   ├── test_raster_ops.py   8 个通用栅格算子与规则规划/$prev 回归
│   ├── test_gdb.py          FileGDB 多要素类导入 / ZIP / 桌面端接线
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
   native/  ──────►  app/  ──►  geo 生态
   (Qt 界面)         (引擎)     (geopandas / shapely / pyproj / rasterio / pyopenfilegdb)
                       │
                       └──►  data/  （运行时产生：上传数据、设置、自定义算子、栅格派生结果）
```

`app/` 里**不出现任何 Qt 的 import**——这是刻意的：引擎层保持无界面依赖，可以独立测试，
界面只负责展示与交互。反过来 `native/` 也不直接调 geopandas 做运算，只调 `app.engine`。

---

## 2. 数据模型

```python
Layer(id, name, gdf, color, op, source, parent)   # 一个矢量图层 = GeoDataFrame + 元信息
                                                   # 源文件图层可带 source_path，gdf 保持懒加载
RasterLayer(id, name, info, color, op, source, online=None)  # 本地栅格只存文件头；在线地图存 URL/方式/Token/参数
Store.layers: dict[str, Layer | RasterLayer]      # 有序字典，顺序即渲染顺序
Result(ok, message, layer=None, warnings=[], data={})   # 每个算子的返回
```

`RasterLayer` 是**鸭子类型**：字段形状对齐 `Layer`，但访问 `.gdf` 会抛中文
`RasterError`——这样图层面板/缩放/显隐/导出不用分叉，而误用矢量算子时
在校验期就得到「这是栅格，不能用矢量算子」的明确提示。栅格算子的结果会写回
`data/cache/R*.tif`，登记成新的 `RasterLayer`，`Store.remove/clear` 负责清理。

`Layer.gdf` 是懒加载属性：大图层可被 `Store` 按内存预算换出到 `data/cache/`，
内存里只保留 `meta`（count/bounds/fields）；下次访问时从 GeoParquet/GPKG 读回。

源文件图层再多一层 `provider`（`app/vector_provider.py`）：`Store.add_source()` 只读
`pyogrio.read_info()` 的文件头，`count/bounds/crs/fields` 直接来自文件头，`gdf` 仍是
None；渲染时 `VectorProvider.iter_bbox()` 用 `pyogrio.open_arrow()` 让 OGR 只返回屏幕
bbox 内的要素，并且**按 RecordBatch 惰性产出**，由 `pyarrow` 消费；一批解成 shapely、
画完即丢，不把整个 bbox 物化成 GeoDataFrame。只有算子、导出、拾取、选区等确实需要
全量数据的路径才会触发 `read_all()`。对应 QGIS 的
`QgsVectorDataProvider::getFeatures(QgsFeatureRequest::setFilterRect(...))` +
`QgsFeatureIterator`。

**磁盘缓存也走 provider**：会话恢复时 `Layer(cache_path=...)` 不再为了图层面板显示
count 去 `gpd.read_parquet()`；`Layer.provider` 对 `.gpkg` 用 pyogrio、对 `.parquet`
用 `pyarrow.ParquetFile.iter_batches()` 逐批解 WKB。打开 GeoParquet 只读 `geo` 元数据
（几何列 / CRS / 字段），bbox 在真正需要时才按 batch 扫描。实测一个 21k 块、
4480 万顶点的缓存图层，旧 `gpd.read_parquet()` 峰值 3 GB+；隐藏恢复从 3.7 GB
降到约 220 MB（WorkingSet）。渲染时按 512 个要素一批，避免单个 batch 的 shapely
对象再次把内存推高。

GeoParquet 的 `geo` 元数据不带包围盒，`VectorProvider` 打开时只读文件头，不会为了
判断“图层在不在当前视口”去扫 614 MB 几何。缓存写盘时在旁边写一个
`<cache>.bounds.json`（WGS84 范围，`Store._write_bounds_sidecar`），`VectorProvider`
构造时直接读取（`wgs_bounds_hint`）。`MapView._use_provider` 拿它填
`_LayerCache.extent`，于是 `_view_need()` 在图层完全位于视口外时直接返回 `None`，
这一层连 `iter_bbox()` 都不会调用。实测一个 21k 块、4480 万顶点、完全在当前视口
外的行政区划缓存：没有旁车时首帧 13.1s（反复扫全层判定“无要素”），有旁车后
首帧 0.006s。

旧缓存没有旁车时走 `_BoundsWorker`：先给该层打 `bounds_pending`，**当前帧跳过**
它（不扫全层），后台算完写旁车后只补一次矢量重绘。`_layer_bounds()`、`fit_all()`、
`fit_layer()` 也不再在 UI 线程调 `wgs_bounds`；`fit_all` 等所有范围到齐后再 fit，
`fit_layer` 等范围回来再缩放。这样即使打开旧缓存，也不会在启动或缩放时卡 13 秒。

**流式渲染为什么还会看到 0.9 GB**：batch 级流式只保证 Python 侧不同时持有整层
geom，但 PyArrow 的 `ParquetFile` 在销毁前会保留读过的 column chunk。现在
`_parquet_row_group_batches()` 按 row group 重开/销毁 reader，bounds 扫描和
`iter_bbox()` 结束都会 `del pf` 后调用
`pyarrow.default_memory_pool().release_unused()`，把 mimalloc 已释放的页还给 OS。
21k 块 / 4480 万顶点图层稳定后 WorkingSet 约 180–190 MB（其中 Qt + 依赖基线
约 146 MB），不是 0.9 GB。旧式单 row-group 缓存第一次全文件扫描的瞬时峰值
会到约 0.9 GB，扫完即降；新建缓存用 `row_group_size=512`，同样扫描的峰值
约 280 MB。当前这个 586 MB 的村行政区划缓存已重建为 42 个 512 行 row group，
实测会话启动峰值从约 1.0 GB 降到约 460 MB，空闲稳定在约 253 MB。

**栅格图层本身的内存**：栅格不建矢量渲染集，也不把整幅影像读进内存。一个
48128×30342、带内部 overview 的影像，在 1816×986 窗口下渲染后：源 block cache
只有几 MB、视口 QImage 约 1.8 MB；整份 8 图层会话里只显示这一张栅格时
WorkingSet 约 230 MB。从全隐藏状态再勾选这张栅格，实测只增加约 8 MB。
`TILE_CACHE` / `SOURCE_CACHE` 各封顶 64 MB，GDAL 块缓存 `GDAL_CACHEMAX=128`，
不会因为“加入栅格”长期多出 1 GB。

如果截图看到 1 GB 而图层列表里矢量都是隐藏的，通常是启动时“村行政区划”那次
流式渲染还没结束（旧缓存只有一个 586 MB 的 row group，PyArrow 仍持有约
645 MB column chunk；CPU 也还在 10% 以上）。重建为 42 个 512 行 row group
后，同会话启动峰值实测约 460 MB，5 秒内降到约 253 MB；栅格加入本身不是这条
峰值。

**投影大图层的渲染 LOD**：`VectorProvider.iter_bbox(..., simplify_tol=...)` 在源 CRS
是投影坐标时，先按等效 WGS84 容差在源 CRS 里 `shapely.simplify()`，再投影；否则
按原来的「投影后简化」保持画面一致。GeoParquet 路径把整层简化结果按 2 的幂次
容差档位缓存（键为档位，值为 WGS84 几何数组 + 包围盒），平移只做 bbox 过滤，
缩放可复用 2.5 倍容差内的相邻档位；缓存上限 96 MB，只存简化后的几何。
`read_bbox()` 不传 `simplify_tol`，始终返回完整几何供算子/导出使用。21k 块 /
4480 万顶点图层实测：首帧约 4s，同档位平移 0.2s，一级缩放 0.4s，跨 4 倍档位
重建约 4~5s。缓存里存的是简化后的包围盒，过滤时按容差外扩，避免视口边缘
要素被误裁。

**属性表只读属性列**：`TableDialog` 不再调用 `layer.gdf`。provider 图层走
`Layer.attrs_frame()` → `VectorProvider.attrs_frame()`：GeoParquet 用
`pq.read_table(columns=非几何列)`，Shapefile/GPKG/GeoJSON 用
`pyogrio.read_dataframe(read_geometry=False)`；内存里已有 gdf 的图层才从 gdf 切
属性列。`Layer.gdf` 保持 None，不会因为打开属性表把 4480 万顶点的 WKB 解码进
shapely。21k 块 / 4480 万顶点图层实测：打开属性表 0.45s、WorkingSet 从 146 MB
到 179 MB（属性表本身约 4.65 MB），而不是数 GB；表格只驻留 200 行/页。
点表格一行时 `MapView.zoom_to_feature()`/`_draw_highlight()` 用
`VectorProvider.feature_geom(row)` 只解码这一行的 row group/一个要素，不建整层
索引。

**悬停已移除，点击拾取也不能全量加载**：鼠标移动不做命中测试。provider 图层的
`_LayerCache.geoms` 始终为 None；当 `_gdf` 尚未进内存时，`_ensure_index()` 直接
返回 False。点击时 `_hit_test()`
走 `VectorProvider.hit_test()`：GeoParquet 在渲染 LOD 缓存里找最近简化几何，
再用缓存里的源文件行号调用 `attrs_at()`，只 `pq.read_table()` 属性列、不读几何；
非 GeoParquet 只读点击点附近的小 bbox。这样 21k 块 / 4480 万顶点图层点击是
0.02s、约 6MB 属性表，而不是 15.5s / 4.3GB。`zoom_to_feature`、`_select_rect`
在 `_gdf` 未加载时同样不会触发 provider 的全量索引；按行定位改成单要素读取。

Shapefile 没有内建空间索引：`app/spatial_index.py` 在后台用 GDAL `ogrinfo` 补一次
`.qix` 旁车文件（`GIS_AGENT_AUTO_INDEX=0` 可关）。470k 道路的城市窗口查询从
2.8s 降到 0.27s；500 万 Shapefile 首次打开先顺序流式显示，索引建好后才能跟手平移。
ZIP 内解压出的临时文件、CSV、FileGDB 不走 provider（临时目录导入后会被清理），
仍回退整份读取。

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

#### 大图层：屏幕范围流式渲染

内存图层超过 `STREAM_MIN_FEATURES`（25 万）时不建 `_RenderSet`，走
`native/vectorstream.py`（源文件图层则不看阈值，见下）：

```text
源文件：当前视口（经纬度）∩ 图层范围 → pyogrio.open_arrow(bbox=...)
  → pyarrow.RecordBatchReader 一批 4096（点图 65536）
       → shapely.from_wkb（可选 CRS 批量变换到 4326）
       → 亚像素面降点 / shapely.simplify（像素级容差）
       → 批量建 QPainterPath，立即画进该图层的整视口 QImage
       → 按 0.06~0.12s 节流把当前 QImage 复制一份 → layerProgress
  → 本批几何/路径丢弃，不保留
内存图层：_select_indices()（包围盒相交，不抽样）→ 同样的 4096 分块
```

`layerProgress` 到主线程后合成 **provisional 帧**（小窗口更密，避免中等图层
只闪 1~2 次）：它只负责显示，不写图层缓存、不登记 `_frame_sig`，所以拖拽/缩放
不会把它当成完整帧复用；整帧完成后的 `frameReady` 才发 final 帧。这样的效果是
“要素一块块出现”，而不是整层画完才换图。

粒度是**块**不是单要素：Qt 的 `drawPath` 固定开销 >> 顶点开销，逐要素画
47 万条道路会慢一个数量级（README 有实测），块级渐进才是可用的流式粒度。

高密度点图层（当前视口估算 >8 万点）走另一条更激进的路径：把世界坐标量化到
1 个设备像素，用 numpy 布尔占用表去重，再按批 `drawPoints`。这不是抽样——某个
像素只要有点就一定会画；5M 点 / 1000×700 实测从 96s 降到 ~6s，首帧 ~0.2s。
放大到城市窗口后估算点数低于阈值，自动恢复 3.4px 圆点。

这对应 `QgsVectorLayerRenderer::render()`：按 `setFilterRect(extent)` 取要素、
逐要素渲染。源文件图层**不看要素数阈值，全部走 provider**；阈值只用于内存图层。
这里分成两种数据来源：

- **源文件图层（provider）**：快照里只放 `provider + bbox`，渲染线程调用
  `VectorProvider.iter_bbox()`，内存里没有整层 `geoms` / `bounds`，也没有整个 bbox
  窗口的 GeoDataFrame；
- **内存大图层**：快照里放 `geoms + indices`，只取屏幕范围内已加载的几何；
  `_LayerCache` 仍保留全量 `geoms` + `bounds` 供拾取/选区用，但**不再保留整层路径**。

小图层仍走渲染集缓存：预建路径换来平移/缩放时的复用，整体更快。
`GIS_AGENT_STREAM_VECTOR=1/0` 可强制开/关，便于对比测试
（`tools/test_stream_render.py`、`tools/test_provider_render.py`）。

> 性能注记：Shapefile 没有空间索引时，GDAL 的 bbox 过滤仍要顺序扫描整个 `.shp`，
> 只是**不会把范围外要素读进内存**。`app/spatial_index.py` 会在后台补 `.qix`；
> 索引建好前先顺序流式显示，建好后城市窗口查询走索引（470k 道路 2.8s → 0.27s）。
> GPKG 内建 R-tree，窗口读取通常更快。

#### 宽视野：整层粗路径缓存（源文件图层）

`.qix` 只解决「小窗口查得快」，解决不了「视口盖住整个图层」：省域视图下所有要素都命中，
`iter_bbox()` 还是得把文件顺序读一遍（47 万条道路实测 3.2~3.6s，占整帧 6s 的六成）。
拖动/缩放时每帧重来一次，就是用户感知的「卡住 5~8 秒」——这也是本文件里
「按 bbox 流式读」这条设计的边界：它省内存、省首次加载，但不省重复整层读。

`MapView` 因此给**要素数 ≥ `LOD_MIN_FEATURES`(4 万)** 的源文件图层排一个后台任务
（`_LodWorker`；只在图层可见、范围与视口相交且 `mpp ≥ LOD_MIN_MPP` 时排，城市视图、
隐藏图层和视口外图层不预建）：用固定粗容差 `LOD_TOL = LOD_MIN_MPP(45) × 0.8px` 把整层读**一次**
（`native/vectorstream.build_lod_paths()`，和流式路径同一套 shapely 向量化接口），
建成一整套世界坐标 `QPainterPath` 存进该图层的 `_LayerCache.sets`，矩形覆盖整个
图层范围（外扩 2×容差）。47 万条道路约 1.4M 顶点 / 30 MB。之后：

- `mpp ≥ 45`（约 1:17 万及更远）：`_lod_set()` 命中缓存，`_make_snapshot()` 直接把
  路径放进快照，渲染线程只按每批包围盒裁掉视野外部分，**不再碰磁盘** —— 整省帧
  从 ~6s 降到 ~0.3s；
- `mpp < 45`（城市级）：缓存容差比当前需要的粗，`_lod_set()` 返回 `None`，继续走
  按 bbox 读窗口的流式路径，保证放大后细节正确（城市窗口本来就只有几万要素）；
- 顶点总数超 `LOD_MAX_VERTS`(4M) 就放弃缓存，保持原流式行为；
- 点图层不建（高密度点走 1px 像素桶，本来就不建路径）。

缓存构建在后台线程；它建路径时持 GIL，和首帧渲染并行会让首帧慢 1~2s（首帧仍是
流式渐进显示，之后所有宽视野交互用缓存）。标注在渲染进行中（`_vec_busy`）不再
重新取数：整层读和渲染读的是同一个文件，同时读只会更慢，等这一帧画完再防抖重建。

对应 QGIS 的「多比例尺预生成 + 渲染缓存」思路，这里只做最粗的一级；
`GIS_AGENT_STREAM_VECTOR=0`（强制走整层渲染集）时不建这套缓存，便于对照。


### 3.3 简化与「点降级」

- **容差按像素**：`SIMPLIFY_PX(0.8)` 换算成图面单位（`mpp × 0.8`），跟着缩放自动变细；
- **亚像素面要素降为点**：屏幕尺寸 < `DOT_PX(1.5)` 的面不再画轮廓，改用 `drawPoints` 画 1 个设备像素，
  既不丢要素，也省掉海量顶点（省域视图下 24 万栋建筑只剩 24 万个点，而不是 24 万条路径）；
- 简化后若某要素「消失」或面积缩水 > 60%，回退用原几何。

### 3.4 线程模型

```
GUI 线程                    Worker(QThread)              渲染线程 / 合成线程
─────────                   ─────────────────            ────────────────────
事件循环 / 绘制             解析数据、跑算子             画各图层到 QImage
  │                            │                            │
  ├─ 10ms 一片地构建路径        ├─ open_arrow / 算子        ├─ 渲染线程：队列只留最新一帧
  ├─ paintEvent 画整帧          ├─ run_agent                 ├─ 合成线程：QImage 叠层
  └─ 只接合成好的 QImage        └─ 结果回主线程              └─ epoch 不符直接丢弃
```

为什么这么分：

- 解析 shapefile 与空间运算都**长时间持有 GIL**，必须离开 GUI 线程，否则界面直接冻住；
- 渲染单独一条**常驻**线程（不是每次新建），队列只保留最新一帧，连续缩放时旧帧自动作废；
  多图层时该线程内部再用最多 4 个线程并行画层（`_render_job_parallel`），
  但按原始图层顺序回传 `layerReady`；
- 图层合成也放到**独立合成线程**：多张大 QImage 的 `drawImage` 不再占用 UI 事件循环，
  主线程只做一次整帧贴图；
- 小图层的渲染集路径留在 GUI 线程、按 10ms 分片——因为 QPainterPath 的创建很轻、但要访问共享数据，
  分片后界面在「准备渲染数据」阶段依然能响应；
- 大图层的流式路径在**渲染线程**里按块构建并立刻画掉，不占 GUI 线程，也不留路径缓存；
- 要素标注的取数在**独立常驻线程**（`_LabelWorker`）：源文件图层在大范围下 `open_arrow` 要一秒多，
  放 GUI 线程就是「缩放到全省卡住」；结果按世代号回主线程，和渲染帧互不阻塞。
  旧任务可以按世代号在批次之间提前退出，拖动时新视野不用排队等整层扫描；
  配了标注字段的源文件图层还会在后台预建**整层锚点表**（只读一次，之后视口切换纯内存）。
- 源文件图层的**宽视野整层粗路径缓存**在另一条常驻线程（`_LodWorker`）：整层读一次
  3~5s，建成路径后宽视野渲染只在内存里筛批；结果按 provider 身份校验，图层删了/换源就丢。

### 3.5 缓存层级

| 层 | 键 | 命中效果 |
|---|---|---|
| 帧缓存 `_frame` | 场景签名 + 视图 | 平移/缩放时直接贴旧帧（缩放的位图变换在 `paintEvent` 里做） |
| 图层图像 `_img_cache` | `(scale, tx, ty, w, h, dpr, 图层几何版本, 颜色, op, 线宽档位)` | 切显隐、调顺序几乎零成本 |
| 渲染集 `_RenderSet` | 矩形 + 容差 | 来回缩放不重建几何 |
| 宽视野粗路径 `_LayerCache.sets`（源文件层） | 固定 `LOD_TOL` + 整个图层矩形 | `mpp ≥ 45` 时宽视野平移/缩放不再读盘（整省帧 ~6s → ~0.3s），内存约 30 MB/47 万条 |
| 流式渲染 `vectorstream` | 无（按当前视口每次现算） | 大图层不保留整层路径，省内存 |
| provider bbox 读取 | 无（按当前屏幕 bbox 每次现读） | 源文件图层不保留整层要素，窗口内要素画完即释放 |
| 矢量渲染 LOD `_render_cache` | 简化容差档位（2 的幂次，GeoParquet） | 同一档位平移/一级缩放复用整层简化几何；跨 4 倍档位才重建；附带源文件行号 |
| 属性列 `_attrs_df` / `_attrs_table` | 图层 | 点击拾取和属性表只读属性列，不加载几何 |
| 标注 `_labels` | 图层 + 标注字段 + 要素版本 + 覆盖范围 | 平移/缩放不重新取数；改样式或删层立刻作废 |
| 整层锚点表 `_labels_anchors`（源文件层） | 图层 + 标注字段 | 无空间索引的源文件整层读一次（~2MB/2 万条），之后任何视野的标注都只做内存筛选，拖动即时跟随 |
| 磁盘图层缓存 | 图层 id（`data/cache/<id>.parquet` 或 `.gpkg`） | 大图层换出内存后仍可懒加载；`meta` 不读盘 |

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

但 `layer.style` 里**显式**配了 `stroke_width` 时用户说了算，不再套分档。

### 3.8 图层样式与要素标注

样式分两处存：

- `app/layer_style.py` 是唯一的**默认值 + 规范化**来源。空 `layer.style` 时
  `resolve_vector_style()` 算出的填充/描边/点大小与旧硬编码逐位一致
  （软算子 buffer/grid/merge/voronoi 4%/26%，dissolve 8%，普通面 14%/70%，
  线 1px/1.6px 分档，点 3.4px + 1.3px 白边）——**没配过样式的图层外观不会变**。
  `normalize_vector_style()` 夹范围（不透明度 0–100、线宽 0.5–10、点 2–40、字号 8–24）、
  颜色必须 `#rrggbb`，坏值回退图层主色。
- `native/style_dialog.py`（`LayerStyleDialog`）只负责收集交互值，预览直接调
  `mapview._paint_layer()` 画样例——和地图同一套渲染代码，不会出现「预览一个样、地图另一个样」。
  栅格页只暴露不透明度 / 拉伸方式 / gamma（其余波段配置原样保留）。

标注不进渲染线程：`native/labels.py::build_label_data()` 按**当前视野外扩 40%** 取要素中心，
内存图层走 `shapely.bounds` + 矩形掩码，provider 图层走 `VectorProvider.iter_label_anchors()`
（Arrow 批 + 源 CRS 里算锚点，只把「一条要素一个点」投影到 WGS84）。
它跑在常驻的 `_LabelWorker` 线程里（对应 QGIS 把标注放在 render job 里），
UI 线程只负责派发和上屏；每层只保留最新任务，世代号对不上就丢弃，避免旧视野的标注盖上来。
正在跑的旧任务也在**每批之间检查世代号提前退出**（`build_label_data(..., cancelled=...)`）——
不能中断的话，拖动时新视野只能排在旧视野的整层扫描后面，实测能积到 10s 以上。
取数结果是一组**世界坐标锚点 + 文本**，存进 `MapView._labels`；`paintEvent` 里
`_paint_labels()` 把锚点换算成屏幕坐标再画，所以平移/缩放时旧标注不会「飘」。

**整层锚点表**（`build_label_table` + `MapView._labels_anchors`）：按视口取数在
没有空间索引的源文件上（GeoParquet / 无 `.qix` 的 Shapefile）每次都要顺扫整个文件——
614MB 的村界面 parquet 一次 3.5~4.5s，拖动一次换一个视野就重来一次。要素数
≤ `LABEL_TABLE_MAX`(20 万) 的这类图层改成**整层读一次**，把 `(锚点, 文本, 角度)`
留在内存（2 万条约 2MB）；之后 `_derive_labels()` 每帧只做 numpy 掩码 + 网格取样
（2 万锚点约 0.1ms），拖动/缩放时标注跟地图同步，不再碰磁盘。表在图层加入时
（`_maybe_prefetch_labels`）就后台预建，用户看完初始视图前通常已经好了；
预建和按视口取数都会先看 `_view_need()`：视口外或范围还没回填的图层不排任务，
避免为了看不见的标注扫 614MB；建表期间不重复提交（`_label_tables` 记录在建世代），否则每次重绘都会把上一个表任务作废。
GPKG / FileGDB / FlatGeobuf / 有 `.qix` 的 Shapefile 按视口读本来就是毫秒级，不建表。
锚点表在改样式、删层、换数据源时作废；`c.rev`（渲染版本）变化不再影响它——LOD 缓存
就绪只加路径、不加数据。
过期重建用 140ms 防抖定时器（`_label_timer`）；**放大到缓存范围的 1/1.6 以下会失效重建**，
否则按大范围抽的点在新视野里寥寥无几，看起来像「标注丢了」。重建前继续画旧缓存。
渲染进行中（`_vec_busy`）**不登记新的取数**：源文件图层的整层读和渲染读的是同一个文件，
两边同时读只会互相拖慢（省域下各 3s 级别），等这一帧画完再防抖重建。
候选上限 `LABEL_CAP=1000`、文本截断 40 字；超过上限时按 **16×16 网格分层取样**（先保证每个格子都有
代表），不是「取前 N 个」——文件顺序取样只会标到地图一角。避让按 120×26 像素格子只比较相邻 9 格，
并对同名文本做近邻去重（180px 内不重复画），近似 QGIS 的 `mergeLines`。
线要素额外带一个**旋转角**：取数时用 `line.interpolate(0.5/0.45/0.55)` 求线中点和局部切线，
归一化到 (-90°, 90°] 避免倒字，绘制时 `p.rotate()` 让字与道路平行（对应 QGIS 线图层的
「沿线上放置」）；锚点是线中点而不是包围盒中心，弯路的字不会飘到路外。碰撞检测用旋转后
文字的外接矩形（轴对齐）近似 QGIS 的旋转几何求交。

改样式后的失效路径：`MapView.refresh_layer_style(lid)` 丢掉该层的
`_img_cache`/`_layer_imgs`/`_layer_views`/`_labels`、`c.rev += 1`，栅格再走
`RasterPipeline.bump_rev()`；`_layer_cache_key` 也把样式签名算进键里，双保险。

### 3.9 在线地图（复用栅格管线的“无文件”分支）

在线底图仍登记成 `RasterLayer`，但多一个 `online` 字典
（`method/url/name/min_zoom/max_zoom/token/token_key/params/headers`）。
`render_layer_image()` 在**任何 rasterio 调用之前**判断这个字段，先由
`prepare_config()` 做 Token 和自定义参数展开：

- **Token**：地址、参数、请求头中的 `{token}` 会替换；如果没有任何 `{token}`
  引用，才按 `token_key` 追加为查询参数。`headers` 里可写
  `Authorization: Bearer {token}`，此时 Token 只走请求头，不会重复出现在 URL 日志里。
- **自定义参数**：`params` 是 `key=value` 映射；瓦片请求合并进 URL 查询串，
  WMS 请求合并进 GetMap 参数。同名时以弹窗填写值为准，但 BBOX/WIDTH/HEIGHT/
  SRS(CRS) 等逐视图参数始终由渲染器覆盖。
- **XYZ / WMTS**：`app.online_map.build_tile_url()` 替换 `{z}/{x}/{y}`、`{-y}`、
  `{s}`、`{TileMatrix}/{TileCol}/{TileRow}`；按 `round(log2(scale / 256))` 选层级，
  取视口覆盖的瓦片，逐张缩放进整视口透明 `QImage`。一张失败即停，避免断网时几十个
  6 秒超时把一帧拖到几分钟。天地图是 KVP WMTS
  （`TILEMATRIX={z}&TILEROW={y}&TILECOL={x}`），识别到 `tianditu.gov.cn` 时
  把层级夹到 **1..18**（没有 0 级），并把 `{s}` 的子域从 `a/b/c` 换成 `t0`…`t7`。
  `validate_config()` 还会拒绝 `/wmts/{z}/{x}/{y}` 这种 REST 路径式地址和
  `_c`/`TILEMATRIXSET=c`（当前渲染是 Web Mercator，只支持 `_w`/`w`）。
  瓦片是**渐进式**的：`_render_online_tiles(..., progress=...)` 每贴上一张就回传
  一张 `out.copy()`，`_RasterWorker.layerProgress` → `RasterPipeline._on_partial`
  → `MapView._on_raster_progress` 立即合成 provisional 帧；`RasterPipeline`
  不把中间图写进完整帧缓存，最后一张到齐后才走 `layerReady`/`frameDone`。
  在线瓦片还走**独立线程** `_online_worker`，本地栅格不会被下载中的底图堵住；
  `_pending_jobs` 等本地/在线两边都结束才发 `on_frame`。单帧最多
  `MAX_ONLINE_TILES`(1024) 张，异常视口（例如整世界误算到 z15）直接放弃，不会
  进入几十分钟的下载循环。矢量构建中的 partial 帧用 `_start_render(request_raster=False)`，
  不重新请求栅格，避免把正在下载的在线瓦片取消后从头再下一遍。WMS 是单次 GetMap，
  仍等整图返回。
- **WMS**：`build_wms_url()` 保留用户地址里的 `LAYERS/STYLES/VERSION`，按当前视口
  补 `BBOX/WIDTH/HEIGHT/SRS`（1.3.0 用 `CRS`），请求一张图后铺满视口。
- **缓存**：`ONLINE_CACHE` 是解码后 `QImage` 的 LRU（默认 64 MB / 256 张），
  键是最终请求 URL + 自定义请求头；缩放/平移回来直接复用，不重复下载、不重复解码。
  网络请求在栅格渲染线程里同步做，主线程不阻塞；`fetch_bytes()` 会合并同名的
  UA/Referer 默认值。
- **持久化**：`_layer_descriptors()` 保存 `online` 配置（包括 Token/参数/请求头），
  恢复时用 `RasterInfo(**info)` 造元信息并跳过 `read_info()`（URL 不是本地文件）；
  图片本身不落盘，Token 不写入日志/聊天消息。
- **聊天卡片持久化**：`messages` 表的 `parts` 除 `text` 外还存
  `clarify`（问题/选项/是否已选）、`plan`（计划步骤及每步最终状态）、
  `steps`（结果步骤卡的 message/data/warnings）。`_remember()` 在写文本消息时
  一并写这些 part，`_restore_chat()` 按 part 顺序重建 `ClarifyCard` /
  `PlanCard` / `StepCard`；旧会话没有这些 part，仍按纯文本恢复。

---

## 4. 规划与执行（`app/agent.py`）

```
用户一句话
   │
   ├─► 闲聊判定 chit_chat()          —— 问候/道谢/问能力，直接回人话
   ├─► 生成算子 wants_new_operator()  —— 「写一个算子：…」走代码生成
   └─► 规划
        ├─ 有大模型：llm_plan()  — 图层清单+字段+算子 schema → JSON 计划
        │     · 提示词里写死 {tool, desc, args}，数据区声明为不可信输入
        │     · 返回后 normalize_plan() 归一（兼容 op/params、别名、参数摊平）
        │     · JSON 解析失败/被截断/429/5xx 会重试或提高 max_tokens
        │     · 仍失败 → 回退规则引擎，并静默 60 秒（避免每条都卡）
        └─ 无大模型：规则引擎 — 拆句（然后/再/；）→ 图层名模糊匹配 → 单位数字抽取
                                                    → 上一步输出自动串给下一步
   │
   ▼
validate_plan(): app/tool_registry.py 的 schema 校验（类型/枚举/必填/图层/几何类型）
   │  不通过 → 0 副作用拒绝，或让模型修复
   ▼
run_agent(): 逐步执行，产出步骤卡片数据
   · `$prev` 表示上一步产出；上一步没建层则沿用它的输入图层（链条不断）
   · 任一步失败 → 把失败步骤+错误+当前 schema 回灌给模型，最多修 2 次
   · 图层/字段不明确 → clarify，前端渲染可点选项
   · 每步记录 elapsed / message / warnings / data（小表）/ 新图层 meta
   · 全部结束后 note_result() 记住最近工具/参数/结果层，供下一轮指代
```

规划器**永远不直接改数据**，只产出「计划」这一数据结构；执行、建层、撤销登记都在 `run_agent` 之后由主窗口完成。
这样规则引擎和大模型走的是同一条执行路径，行为可对比、可测试。

---

## 5. 算子体系（`app/engine.py` + `app/raster_ops.py`）

- 签名统一：`op(store, layer, **args) -> Result`，第一个参数是 `Store`（便于跨图层操作）；
- **矢量算子**在 `engine.py`，**栅格算子**在 `raster_ops.py`；两边共用 `Result` 和
  `Store`，但 `raster_ops` 顶层不 import rasterio（启动不加载 GDAL，执行时才惰性导入）；
- **CRS 策略**：距离/面积运算前把几何投到 **CGCS2000 3 度带**（按数据中心经度选带，EPSG:4534–4554），
  境外退回 UTM；结果字段统一带 `_m` / `_m2` 后缀；
- **几何有效性**：入口处 `make_valid` 并计数（>2 万要素的图层跳过逐个校验）；
- **文本型 ID**：Shapefile 的 `osm_id` 是字符串，表达式里写数字会自动补引号，
  否则 `osm_id == 4814005` 会静默筛出 0 条；
- **栅格结果落盘**：裁剪/重投影/重采样/计算/镶嵌的结果写到 `data/cache/R*.tif`，
  用 `read_info()` 只读头信息登记成 `RasterLayer`；`cache_path` 指向派生文件，
  `Store.remove/clear` 会清理，原始影像不动；
- **栅格算子按窗口/块处理**：裁剪读窗口、重投影按输出块、计算按 1024² 块、
  分区统计按要素 `geometry_window`，不会把大影像整幅读进内存；
- **自定义算子**：`custom_ops.py` 先用 AST 白名单拦截 `import / while / exec / read_pickle / to_file` 等，
  再在受限命名空间里跑；先拿前 200 个要素试跑，确认后才落盘到 `data/operators/`，
  重启自动装回，与内置算子同权（工具表、提示词、工具库三处同步可见）。
  **这不是 OS 级沙箱**，只应运行可信代码；算子名不能覆盖内置算子，执行时传原图层副本以守住「只读」。

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
.venv\Scripts\python.exe tools\test_agent_v2.py     # 工具契约/引擎/多轮/自定义算子安全
.venv\Scripts\python.exe tools\test_raster_ops.py   # 8 个栅格算子 + 规则规划 + $prev
.venv\Scripts\python.exe tools\test_raster.py       # 栅格载入/混合渲染/显隐/缩放
.venv\Scripts\python.exe tools\test_raster_tiles.py # 栅格 tile cache 首帧/平移复用
.venv\Scripts\python.exe tools\test_raster_source_cache.py  # 源块/overview 选择/跨缩放复用
.venv\Scripts\python.exe tools\test_overview_auto.py        # 缺 overview 时后台补 .ovr
.venv\Scripts\python.exe tools\test_agent_llm.py    # 假 LLM：校验修复/执行修复/澄清/reply
.venv\Scripts\python.exe tools\verify_render.py     # 完备性 + 像素比对（最该看的）
.venv\Scripts\python.exe tools\test_provider_render.py  # provider 只读/bbox 渲染一致性
.venv\Scripts\python.exe tools\test_provider_stream.py  # Arrow 惰性流：首批早于全量
.venv\Scripts\python.exe tools\test_vector_lod_cache.py  # 投影图层简化 LOD 缓存 / 平移过滤
.venv\Scripts\python.exe tools\test_render_guards.py      # 旧 provider 范围后台化 / 在线瓦片上限
.venv\Scripts\python.exe tools\test_chat_persist_cards.py # 重启后恢复澄清卡/计划卡/步骤卡
.venv\Scripts\python.exe tools\test_no_hover.py          # 悬停已移除：移动不触发拾取
.venv\Scripts\python.exe tools\test_parallel_layers.py   # 并行层 job 与串行逐像素一致
.venv\Scripts\python.exe tools\bench_vector_5m.py --generate  # 5M 点：首帧/全图/城市窗口
.venv\Scripts\python.exe tools\build_spatial_index.py layer.shp  # 提前建 .qix
.venv\Scripts\python.exe tools\perf_profile.py      # 一帧耗时拆到 构建/简化/绘制/合成
.venv\Scripts\python.exe tools\perf_wide_view.py    # 宽视野：整层流式读 vs 粗路径缓存
.venv\Scripts\python.exe tools\perf_label_latency.py  # 标注：按视口重读 vs 整层锚点表 + 拖动跟随
.venv\Scripts\python.exe tools\test_ux_batch.py     # 选区/撤销/属性表联动
.venv\Scripts\python.exe tools\test_attr_table_provider.py  # 属性表不加载全量几何
.venv\Scripts\python.exe tools\test_chat_table_layout.py     # 执行卡小表格不裁行
.venv\Scripts\python.exe tools\test_layer_style.py           # 图层样式与要素标注
.venv\Scripts\python.exe tools\test_online_map.py            # 在线地图 URL/天地图/Token/参数/瓦片/WMS/图层登记（离线）
.venv\Scripts\python.exe tools\test_tool_manage.py           # 工具库管理（启用/停用/自定义工具编辑）
.venv\Scripts\python.exe tools\smoke_app.py         # 整机冒烟 + 截图
```

`perf_roads.py` / `perf_backend.py` 是「换实现之前先量一下」的工具：
前者比笔宽/端帽/抗锯齿，后者比 CPU 光栅与 OpenGL。

---

## 8. 扩展指引

**加一个算子**：矢量算子在 `engine.py` 写 `_my_op(store, layer, ...) -> Result`，栅格算子在
`raster_ops.py` 写同名签名并加入它的 `OPERATIONS`；然后在 `tool_registry.py` 加 `ToolSpec`
（参数 schema / 模板 / 分组 / 中文名），工具库按钮、大模型提示词、规则校验三处自动可见。
栅格算子记得只在函数体里 import rasterio，保持启动惰性。

**加一个渲染特性**：先想清楚它属于哪一层——几何构建（`_build_*`）、绘制（`_paint_layer`）、
还是交互叠加（`_draw_*`，画在屏幕坐标系）。要改画笔，记得世界坐标下必须 cosmetic。

**加一个交互**：状态尽量放在 `MapView`（视图相关）或 `MainWindow`（文档相关），
两者通过信号通信（`selectionChanged` / `picked` / `mapMenu`），别互相直接调方法。

---

## 9. 与 QGIS 渲染管线的对照

| QGIS | 本实现 | 状态 |
|---|---|---|
| `QgsVectorDataProvider::getFeatures(filterRect)` | `VectorProvider.iter_bbox()` + `pyogrio.open_arrow()` | 已对齐 |
| `QgsFeatureIterator` 惰性取要素 | `pyarrow.RecordBatchReader` 逐批产出 | 已对齐 |
| `QgsVectorLayerRenderer::render()` | `vectorstream.paint_stream_layer()` 按层画到独立 QImage | 已对齐（简化版符号） |
| `LayerRenderJob::image` | 每层一张整视口 `QImage` | 已对齐 |
| `QgsMapRendererParallelJob::composeImage()` | 常驻 `_ComposeWorker` 叠层 | 已对齐 |
| `QgsFeedback::cancel()` | `_RenderJob.cancelled` + batch 边界检查 | 已对齐 |
| `QgsMapRendererParallelJob` 并行层 job | `_render_job_parallel()`：每层一个任务、各画各的 QImage，按原顺序回传 | 已对齐（`GIS_AGENT_PARALLEL_LAYERS=0` 可退回串行对照） |
| `QgsFeatureRenderer` / symbol / `QgsPalLayerSettings` | 每层可配填充/描边/点符号 + 单字段标注（字号/颜色/白边） | 部分对齐（无分类/分级符号、无表达式标注、避让是简化版） |
| 矢量瓦片 / 预生成金字塔 | 无；靠 bbox + 简化 + 分块 | **未对齐** |
| Shapefile `.qix` | 后台自动补 sidecar | 已补齐 |
| 栅格 `QgsRasterIterator` 分块 | `rasterio.read(out_shape=...)` 按视口抽像元 | 行为等价 |
| 栅格 `QgsRasterBlockCache` | `RasterSourceCache`：overview 层固定 256px 源块 + LRU | 已对齐（源块级；另有屏幕 tile 层） |
| 栅格 overview 选择 | `select_overview_index()` 选不粗于目标分辨率的层 | 已对齐 |
| 栅格 overview 生成 | 载入询问 + 首次渲染缺层时后台 `.ovr` | 已补齐 |

所以“严格复刻”目前落在**数据取用、逐层 QImage、并行层 job、合成、取消**这条主链上；
符号系统、标注、矢量瓦片仍是 QGIS 的完整实现才有的部分。
500 万点的流畅度靠上面第 3 节的像素桶 + 惰性 Arrow 流，而不是靠抽样。

## 10. 已知边界

- 源文件 provider 打开只读文件头（毫秒级）；首屏的 bbox 读取在无空间索引的 Shapefile 上
  仍是顺序扫描，只是不把范围外要素读进内存。应用会在后台补 `.qix`，GPKG 有索引时更快；
- 5M 点全图实测（1000×700）：首帧 ~200ms、整帧 ~6s；城市窗口 ~0.65s。全图仍不是
  60fps，因为 5M 个 WKB 解码 + 5M 次坐标投影本身约 2.6s；这是“渐进可用”而不是魔法。
  线/面 5M 仍按块建路径，性能取决于顶点总数，当前没有矢量瓦片/预生成金字塔；
- 投影坐标的大多边形图层（21k 块 / 4480 万顶点）首帧仍需约 4s 解码 + 简化；
  之后同档位平移约 0.2s，跨 4 倍简化档位重建一次约 4~5s。渲染 LOD 缓存只存
  简化几何（上限 96MB），不是原始全量；
- provider 大图层（`_gdf is None`）的点击拾取只查属性，不建全量索引；框选 `_select_rect()`
  仍不支持整层框选，`zoom_to_feature()`/属性表点行改成 provider 单要素读取，不再加载全量几何；
- 栅格有两层缓存：**源块**（overview 层固定 256px，键不含视口，跨缩放复用）
  和**屏幕 tile**（同一 scale 平移复用）。源影像小于 200 万像元时自动关闭
  屏幕 tile（整窗更快，也避免小块边界误差）。仍有边界：跨 overview 层的
  新块要首次读取；没有 overview 时源块路径不启用，回退 `read_window()` 的
  整窗/目标块路径，同时由后台 overview 管理补 `.ovr`——所以大图最终仍应
  有金字塔；`overviewNeeded` 只对与当前视口相交的栅格发，视口外的图层不会
  触发 `.ovr` 生成；
- 大栅格读取有两条内存保护：`GDAL_CACHEMAX=128 MB`（否则 GDAL 按物理内存
  放大缓存）和超过 400 万源像元时的 256×256 目标块分块读取。无 overview 的
  448 MB / 20673×29673 TIFF 实测 WorkingSet 从 1.4~1.9 GB 降到约 85 MB，
  代价是首帧更慢；有 `.ovr` 时读取走缩减层，0.3 s 级。
- 点像素桶只对 `Point/MultiPoint` 生效；线/面靠简化 + 分块，不做像素级聚合；
- 放大级别的 1 秒左右主要在几何构建（每个顶点一次 `path.lineTo`）与简化，属于每级只付一次的成本；
- 单机单会话，图层存内存，关窗即清空（需要留存就导出 GeoJSON/GPKG）；栅格派生结果写
  `data/cache/R*.tif`，随图层删除/清空清理；
- 栅格工具覆盖体检/裁剪/重投影/重采样/计算/分区统计/镶嵌/转矢量，但没有栅格属性表、
  没有伪彩色/拉伸面板。
