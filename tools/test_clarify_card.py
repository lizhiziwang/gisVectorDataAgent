"""反问卡的状态机：点一次只能答一次，用输入框回答 / 新问题到来都要把旧卡收掉。

    python tools/test_clarify_card.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from native.chat import ChatPanel
from native.theme import QSS

Q = "要处理哪个图层？"
OPTS = ["地块", "道路", "学校"]

app = QApplication(sys.argv)
app.setStyleSheet(QSS)
panel = ChatPanel(None)
panel.resize(420, 760)
panel.show()
app.processEvents()

picked: list[str] = []
card = panel.add_clarify(Q, OPTS, picked.append)
app.processEvents()

assert panel._clarify_card is card, "新卡应该处于等待状态"
assert card.awaiting and card.chosen is None
assert panel.input.placeholderText().startswith("直接回答上面的问题"), \
    panel.input.placeholderText()
assert len(card._rows) == 3
assert card.width() <= panel._avail(), (card.width(), panel._avail())

# 点一次 → 只发一次，立刻锁住
card._rows[1]._fire()
assert picked == ["道路"], picked
assert not card.awaiting and card.chosen == "道路"
assert panel._clarify_card is None, "答完要从等待状态里摘掉"
assert panel.input.placeholderText().startswith("描述"), \
    panel.input.placeholderText()
assert card._rows[1].num_lb.text() == "✓"
assert card._rows[0]._done and card._rows[2]._done, "其余选项应失效"

# 再点别的（手滑 / 连点）不能再发
card._rows[2]._fire()
assert picked == ["道路"], picked

# 用输入框回答 → 旧卡收掉
card2 = panel.add_clarify(Q, OPTS, picked.append)
app.processEvents()
assert panel.input.placeholderText().startswith("直接回答上面的问题")
panel.add_user("都不对，用县界")
assert not card2.awaiting and card2.chosen is None
assert card2.chosen is None
assert "输入框" in card2.hint.text(), card2.hint.text()
assert card2._box is not None and card2._box.isHidden(), "用输入框答完，灰选项该收起来"
assert panel._clarify_card is None
assert panel.input.placeholderText().startswith("描述")

# 新问题到来 → 上一张卡标记为已取代
card3 = panel.add_clarify(Q, OPTS, picked.append)
app.processEvents()
card4 = panel.add_clarify("换一个问题：用哪个字段？", ["NAME", "code"], picked.append)
app.processEvents()
assert not card3.awaiting and card3._kicker.text() == "已回答"
assert "取代" in card3.hint.text(), card3.hint.text()
assert panel._clarify_card is card4

# 面板变窄：卡要跟着缩，不能顶出可视区
panel.resize(340, 760)
app.processEvents()
app.processEvents()
assert card4.width() <= panel._avail(), (card4.width(), panel._avail())
assert card4.question_lb.width() == card4.width() - 26

# 清空对话 → 等待状态一起清掉
panel.clear()
app.processEvents()
assert panel._clarify_card is None
assert panel.input.placeholderText().startswith("描述")

print("ok: clarify card state machine")
