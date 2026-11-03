import pytest

from vcradar.labels import decide_dim, keyword_hit, overall
from vcradar.models import LabelDim


@pytest.mark.parametrize("hit,verdict,quote,expected", [
    (True, "是", "依据", "是"), (True, "否", "依据", "否"),
    (False, "是", "依据", "是"), (False, "否", "", "否"),
    (True, "否", "", "待核"), (True, "是", "", "待核"), (False, "是", "", "待核"),
    (True, "拿不准", "", "待核"), (False, "拿不准", "", "待核"),
    (True, None, "", "待核"), (False, None, "", "待核"), (True, "是", "  ", "待核"),
])
def test_every_dimension_decision_row(hit, verdict, quote, expected):
    assert decide_dim(hit, LabelDim(verdict, quote)) == expected


@pytest.mark.parametrize("text,dim,expected", [
    ("It said this was paid", "ai", False), ("AI-driven software", "ai", True),
    ("models and agents", "ai", True), ("computer vision", "ai", True),
    ("MACHINE LEARNING", "ai", True), ("LLMs", "ai", True), ("agentic", "ai", True),
    ("generative world models", "ai", True), ("voice AI", "ai", True),
    ("modeling", "ai", False), ("socially", "social", False),
    ("AIS navigation", "ai", False), ("communities", "social", True),
    ("friends, dating, community, group, creator, forum, events", "social", True),
])
def test_keyword_boundaries(text, dim, expected):
    assert keyword_hit(text, dim) is expected


@pytest.mark.parametrize("ai,social,label,group", [
    ("是", "是", "AI相关 + 社交/社群相关", "AI+社交"),
    ("否", "是", "社交/社群相关", "社交"), ("是", "否", "AI相关", "AI"),
    ("否", "否", "都不是", "都不是"), ("是", "待核", "AI相关 + 标签待核", "标签待核"),
    ("待核", "是", "社交/社群相关 + 标签待核", "标签待核"),
    ("否", "待核", "标签待核", "标签待核"), ("待核", "否", "标签待核", "标签待核"),
    ("待核", "待核", "标签待核", "标签待核"),
])
def test_all_overall_combinations(ai, social, label, group):
    assert overall(ai, social) == (label, group)


# 只手写抽取回执和关键词命中情况，不把第三方正文复制成 fixture。
@pytest.mark.parametrize("name,hits,verdicts,expected", [
    ("Dream", (True, False), ("是", "否"), ("AI相关", "AI")),
    ("Mintive", (True, False), ("是", "否"), ("AI相关", "AI")),
    ("Mach9", (True, False), ("是", "否"), ("AI相关", "AI")),
    ("Terse", (True, False), ("是", "否"), ("AI相关", "AI")),
    ("Capveon", (True, False), ("是", "否"), ("AI相关", "AI")),
    ("Flapping Airplanes", (True, False), ("是", "否"), ("AI相关", "AI")),
    ("BeeSafe AI", (True, True), ("是", "否"), ("AI相关", "AI")),
    ("Helios Intelligence", (False, False), ("否", "否"), ("都不是", "都不是")),
    ("Keres", (False, False), ("否", "否"), ("都不是", "都不是")),
    ("Viraj Aero", (True, False), ("拿不准", "否"), ("标签待核", "标签待核")),
    ("Slingshot Insurance", (True, False), ("拿不准", "否"), ("标签待核", "标签待核")),
    ("Numeracle", (True, False), ("拿不准", "否"), ("标签待核", "标签待核")),
    ("Overwatch", (True, False), ("拿不准", "否"), ("标签待核", "标签待核")),
    ("Pops", (True, True), ("是", "是"), ("AI相关 + 社交/社群相关", "AI+社交")),
    ("Bubble Lab", (False, True), ("是", "是"), ("AI相关 + 社交/社群相关", "AI+社交")),
    ("SuperMe AI", (True, False), ("是", "是"), ("AI相关 + 社交/社群相关", "AI+社交")),
    ("Yam", (True, True), ("是", "否"), ("AI相关", "AI")),
    ("Grocalo", (True, True), ("是", "否"), ("AI相关", "AI")),
    ("Quippy", (False, True), ("否", "否"), ("都不是", "都不是")),
    ("TryNearby", (False, True), ("否", "拿不准"), ("标签待核", "标签待核")),
    ("Mochi", (False, False), ("否", "拿不准"), ("标签待核", "标签待核")),
])
def test_all_prd_examples(name, hits, verdicts, expected):
    dims = [decide_dim(hit, LabelDim(verdict, f"{name} 的合成依据" if verdict == "是" or hit and verdict == "否" else ""))
            for hit, verdict in zip(hits, verdicts)]
    assert overall(*dims) == expected
