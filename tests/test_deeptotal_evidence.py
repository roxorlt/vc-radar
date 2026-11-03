"""任务 M：只比较本轮融资，全部使用手写合成原句。"""
from copy import deepcopy

import pytest

from vcradar import deep
from test_deep_support import pages, payload, point


ENDED = "2026-10-31T09:00:00+08:00"
JUDGEMENT = {"round": "A 轮", "amount": "$45M", "event_date": "2026-10-30"}
ROUND_QUOTE = "Hyphenate raised $45 million in Series A."


def fact(value, quote):
    """构造同一来源页的合成融资事实。"""
    return {"value": value, "quote": quote, "page": 1, "url": "https://news.example/hyphenate"}


@pytest.mark.parametrize("quote,value", [
    ("The company has raised a total of $54 million.", "$54 million"),
    ("Hyphenate raised $60 million in total in Series A.", "$60 million"),
    ("This round brings funding to date to $60 million.", "$60 million"),
    ("The funding raised so far is $60 million.", "$60 million"),
    ("Overall, Series A funding reached $60 million.", "$60 million"),
    ("Cumulative Series A funding reached $60 million.", "$60 million"),
    ("Hyphenate closed Series A, bringing its total funding to $70 million.", "$70 million"),
    ("This round brings total funding to $70 million.", "$70 million"),
    ("All told, this round brings funding to $60 million.", "$60 million"),
    ("This funding brings the TOTAL to $60 million.", "$60 million"),
    ("本轮后公司累计融资 5 亿元。", "5 亿元"),
    ("本轮后公司总计融资 5 亿元。", "5 亿元"),
    ("本轮后公司共计融资 5 亿元。", "5 亿元"),
    ("迄今本轮融资 5 亿元。", "5 亿元"),
    ("至今本轮融资 5 亿元。", "5 亿元"),
    ("本轮后融资总额为 5 亿元。", "5 亿元"),
    ("本轮后公司总融资为 5 亿元。", "5 亿元"),
    ("bringing its total funding to $70 million", "$70 million"),
    ("累计融资 5 亿元", "5 亿元"),
])
def test_cumulative_amount_is_not_new_evidence_even_with_current_round_context(quote, value):
    facts = {"round": fact("Series A", ROUND_QUOTE), "amount": fact(value, quote)}
    assert deep.find_new_evidence(JUDGEMENT, facts, ENDED) == []


@pytest.mark.parametrize("round_label,quote,value", [
    ("A 轮", "Hyphenate raised $60 million in Series A.", "$60 million"),
    ("A 轮", "Hyphenate 完成 A 轮，融资 5 亿元。", "5 亿元"),
    ("A 轮", "公司完成A轮融资5亿元。", "5亿元"),
    ("种子轮", "Hyphenate raised $60 million in seed funding.", "$60 million"),
    ("种子轮", "Hyphenate raised $60 million in pre-seed funding.", "$60 million"),
    ("种子轮", "公司完成种子轮融资5亿元。", "5亿元"),
    ("天使轮", "Hyphenate raised $60 million in an angel round.", "$60 million"),
    ("天使轮", "公司完成天使轮融资5亿元。", "5亿元"),
    ("A 轮", "This round raised $60 million.", "$60 million"),
    ("A 轮", "The round was $50 million", "$50 million"),
    ("A 轮", "本轮融资 5 亿元。", "5 亿元"),
    ("A 轮", "This funding amounts to $60 million.", "$60 million"),
    ("A 轮", "The funding amounts to $60 million.", "$60 million"),
    ("A 轮", "The investment amounts to $60 million.", "$60 million"),
    ("A 轮", "THE\nROUND was $50 million.", "$50 million"),
])
def test_amount_with_its_own_current_round_evidence_is_reported(round_label, quote, value):
    judgement = {**JUDGEMENT, "round": round_label}
    facts = {"amount": fact(value, quote)}
    before = deepcopy((judgement, facts))
    assert deep.find_new_evidence(judgement, facts, ENDED) == [
        {"field": "金额", **facts["amount"], "old_value": "$45M", "found_at": ENDED}
    ]
    assert (judgement, facts) == before


@pytest.mark.parametrize("quote", [
    "Hyphenate raised $60 million.",
    "The company received an investment of $60 million.",
    "公司的融资金额为 5 亿元。",
])
@pytest.mark.parametrize("round_as_list", [False, True])
def test_other_sentences_cannot_supply_the_amounts_round_context(quote, round_as_list):
    round_fact = fact("Series A", ROUND_QUOTE)
    facts = {"round": [round_fact] if round_as_list else round_fact,
             "amount": fact("$60 million" if "$" in quote else "5 亿元", quote)}
    assert deep.find_new_evidence(JUDGEMENT, facts, ENDED) == []


@pytest.mark.parametrize("quote", [
    "This round raised $60 million in seed funding.",
    "The investment of $60 million was in an angel round.",
    "Hyphenate raised $60 million in Series B.",
    "本轮种子轮融资 5 亿元。",
])
def test_an_explicit_different_round_is_not_an_amount_correction(quote):
    facts = {"amount": fact("$60 million" if "$" in quote else "5 亿元", quote)}
    assert deep.find_new_evidence(JUDGEMENT, facts, ENDED) == []


@pytest.mark.parametrize("quote", [
    "Hyphenate raised $60 million in the previous Series A.",
    "Hyphenate raised $60 million earlier in Series A.",
    "Hyphenate raised $60 million prior to this Series A round.",
    "Hyphenate previously completed Series A funding of $60 million.",
    "Last year, Hyphenate raised $60 million in Series A.",
    "此前公司完成 A 轮融资 5 亿元。",
    "之前公司完成 A 轮融资 5 亿元。",
    "上一轮 A 轮融资 5 亿元。",
])
@pytest.mark.parametrize("field", ["round", "amount"])
def test_historical_rounds_cannot_create_round_or_amount_evidence(quote, field):
    value = "Series A" if field == "round" else ("$60 million" if "$" in quote else "5 亿元")
    judgement = {**JUDGEMENT, "round": "待核" if field == "round" else "A 轮"}
    assert deep.find_new_evidence(judgement, {field: fact(value, quote)}, ENDED) == []


def test_validated_cumulative_fact_does_not_hide_a_later_current_amount():
    cumulative = "The company has raised a total of $54 million."
    current = "Hyphenate raised $60 million in Series A."
    raw = payload()
    raw["融资"] = [
        point("本轮 Series A。", ROUND_QUOTE, field="round", value="Series A"),
        point("累计融资 $54 million。", cumulative, field="amount", value="$54 million"),
        point("本轮融资 $60 million。", current, field="amount", value="$60 million"),
    ]
    checked = deep.validate_result(raw, pages(" ".join((pages()[0].body, ROUND_QUOTE, cumulative, current))))
    assert checked.ok and checked.removed_points == 0
    found = deep.find_new_evidence(JUDGEMENT, checked.facts, ENDED)
    assert [item for item in found if item["field"] == "金额"] == [
        {"field": "金额", "value": "$60 million", "old_value": "$45M", "quote": current,
         "page": 1, "url": pages()[0].url, "found_at": ENDED}
    ]
