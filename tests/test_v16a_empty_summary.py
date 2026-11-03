"""总负责验收补：没有简介的推送行不出现空的「｜｜」。"""
from vcradar import render
from vcradar.models import Card


def _card(summary):
    card = Card.__new__(Card)
    for field in Card.__dataclass_fields__.values():
        setattr(card, field.name, None)
    card.name, card.summary, card.marks, card.sources = "Machenta", summary, [], []
    card.round_label, card.amount_label = "种子轮", "$40 million"
    return card


def test_empty_summary_drops_segment():
    line = render._highlight(_card(""), {})
    assert "｜｜" not in line
    assert line.startswith("Machenta｜种子轮 · ")


def test_summary_keeps_first_sentence():
    line = render._highlight(_card("Machenta 做机器人。第二句。"), {})
    assert line.startswith("Machenta｜Machenta 做机器人。｜种子轮 · ")
