"""对话能力回归：闲聊识别、配置补全、大模型回复不被模板吞掉。

不联网也能跑（除最后一项可选）。
用法：python tools/test_chat_flow.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QLabel

from app.agent import Planner
from app.config import explain, is_ready, load_cfg, normalize
from app.engine import Store
from native.app import MainWindow

fails = []


def check(name, got, want):
    ok = got == want if not callable(want) else want(got)
    print(f"  {'OK ' if ok else '!! '} {name}: {got!r}")
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- 配置补全
print("[1] 配置补全（只贴 key、没填 Base URL）")
cfg = normalize({"api_key": "sk-test", "base_url": "", "model": "", "enabled": True})
check("base_url", cfg["base_url"], "https://api.deepseek.com/v1")
check("model", cfg["model"], "deepseek-chat")
check("is_ready", is_ready(cfg), True)
check("补 /v1", normalize({"api_key": "k", "base_url": "https://api.deepseek.com"})["base_url"],
      "https://api.deepseek.com/v1")
check("去掉 /chat/completions",
      normalize({"api_key": "k", "base_url": "https://api.deepseek.com/v1/chat/completions"})["base_url"],
      "https://api.deepseek.com/v1")
check("显式关闭时不被覆盖", normalize({"api_key": "k", "enabled": False})["enabled"], False)
print("  当前本机配置:", explain(load_cfg()))

# ---------------------------------------------------------------- 闲聊识别
print("[2] 闲聊识别（不联网）")
p = Planner(Store())
for text in ("你好", "您好！", "hi", "嗨", "谢谢", "你是谁", "你能做什么", "再见", "好的"):
    r = p.chit_chat(text, has_data=False) or p.chit_chat(text, has_data=True)
    check(f"「{text}」识别为闲聊", bool(r), True)

# 带需求的不能被拦
for text in ("你好，帮我做个缓冲区", "谢谢，把地块融合一下", "你能做什么分析，按类型统计面积"):
    check(f"「{text}」不能当闲聊", p.chit_chat(text, has_data=True) is None, True)

# 乱码/短数字：不认领，交给规划器给出可执行的描述建议
check("「123」不当闲聊", p.chit_chat("123", has_data=True) is None, True)

# ---------------------------------------------------------------- 界面接线
print("[3] 界面：大模型的自然语言回复不能被「拆不出步骤」模板吞掉")
app = QApplication.instance() or QApplication(sys.argv)
win = MainWindow()
win.chat.add_agent("dummy")           # 占位，模拟已有对话
win._on_plan_done({"steps": [], "note": "这是模型给的自然语言回答。", "user": "随便聊聊"})
texts = [w.text() for w in win.chat.msgs.findChildren(QLabel) if w.objectName() == "agentText"]
check("显示模型回复", texts[-1], "这是模型给的自然语言回答。")
check("记入历史", win.history[-2:], [{"role": "user", "content": "随便聊聊"},
                                    {"role": "assistant", "content": "这是模型给的自然语言回答。"}])

win._on_plan_done({"steps": [], "note": "", "user": "abc"})
texts = [w.text() for w in win.chat.msgs.findChildren(QLabel) if w.objectName() == "agentText"]
check("没回复时仍给提示", "拆不出可执行" in texts[-1], True)

win.close()

# ---------------------------------------------------------------- 大模型输出解析
print("[4] 大模型返回的 JSON 各种写法都要能认")
from app.agent import _parse_json_payload, normalize_plan

deepseek_actual = ('{"plan":[{"op":"filter","params":{"layer":"pois","expression":"osm_id == 4814005"}},'
                   '{"op":"buffer","params":{"layer":"pois","distance":500,"unit":"m"}}],"reply":"先筛选再缓冲"}')
plan, reply = normalize_plan(_parse_json_payload(deepseek_actual))
check("op/params → tool/args", plan[0]["tool"], "filter")
check("args 取到 expression", plan[0]["args"].get("expression"), "osm_id == 4814005")
check("第二步是 buffer", plan[1]["tool"], "buffer")

fenced = '结果：\n```json\n{"plan":[{"tool":"buffer","args":{"layer":"地块","distance":500,"unit":"m"}}]}\n```\n'
plan, _ = normalize_plan(_parse_json_payload(fenced))
check("带围栏+说明也能解析", plan[0]["tool"], "buffer")

flat = '{"plan":[{"tool":"clip","layer":"地块","other":"行政区"}],"reply":"裁剪"}'
plan, _ = normalize_plan(_parse_json_payload(flat))
check("参数摊平在顶层", plan[0]["args"], {"layer": "地块", "other": "行政区"})

alias = '{"steps":[{"name":"buffer_analysis","arguments":{"layer_name":"道路","radius":300}}]}'
plan, _ = normalize_plan(_parse_json_payload(alias))
check("name/arguments + radius 归一", plan[0], {"tool": "buffer", "args": {"layer": "道路", "distance": 300, "unit": "m"}})

check("纯文本不冒充 JSON", _parse_json_payload("抱歉，我无法处理。"), None)

# ---------------------------------------------------------------- 文本型 ID 比较
print("[5] 文本字段和数字比较要自动加引号")
import geopandas as gpd
from shapely.geometry import Point
from app.engine import _coerce_text_literals, _filter
from app.engine import Store

g = gpd.GeoDataFrame({"osm_id": ["4814005", "5031545"], "n": [1, 2]},
                     geometry=[Point(0, 0), Point(1, 1)], crs=4326)
check("文本列加引号", _coerce_text_literals(g, "osm_id == 4814005"), 'osm_id == "4814005"')
check("数值列不动", _coerce_text_literals(g, "n == 1"), "n == 1")
st = Store()
st.add(g, "pois")
res = _filter(st, "pois", "osm_id == 4814005")
check("筛选命中 1 个", res.data["features_out"], 1)
check("提示文案正常", "20094" not in res.message, True)

# ---------------------------------------------------------------- 新算子
print("[6] 新算子：最小外接矩形 / 形状指标")
from app.engine import OPERATIONS
from shapely.geometry import Polygon

box_gdf = gpd.GeoDataFrame(
    {"name": ["方", "长条"]},
    geometry=[Polygon([(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)]),
              Polygon([(0, 0), (0.05, 0), (0.05, 0.002), (0, 0.002)])],
    crs=4326)
st2 = Store()
st2.add(box_gdf, "boxes")

check("min_rect 已注册", "min_rect" in OPERATIONS, True)
check("shape_metrics 已注册", "shape_metrics" in OPERATIONS, True)

r = OPERATIONS["min_rect"](st2, "boxes", "rotated")
check("矩形数量与要素一致", len(r.layer.gdf), 2)
check("矩形带周长字段", "rect_perim_m" in r.layer.gdf.columns, True)

r = OPERATIONS["min_rect"](st2, "boxes", "envelope")
check("包围盒带长宽字段", "rect_w_m" in r.layer.gdf.columns, True)

r = OPERATIONS["shape_metrics"](st2, "boxes")
g2 = r.layer.gdf
check("周长比字段存在", "rect_ratio" in g2.columns, True)
# 正方形：周长 4a，外接矩形周长也是 4a → 比值≈1
check("正方形周长比≈1", float(g2.loc[0, "rect_ratio"]), lambda v: abs(v - 1) < 0.03)
# 长条 50:2：周长 2*(50+2)=104 单位，外接矩形周长≈104 → 比值≈1
check("细长条周长比也≈1", float(g2.loc[1, "rect_ratio"]), lambda v: abs(v - 1) < 0.05)

# 在「形状指标」的产物上再做字段计算（模型也是用 $prev 接到上一步）
mid = r.layer.id
r = OPERATIONS["field_calc"](st2, mid, "ratio2", "perim_m / rect_perim_m")
check("用指标字段做计算", "ratio2" in r.layer.gdf.columns, True)

print("\n结论:", "PASS" if not fails else f"FAIL {fails}")
