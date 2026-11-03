"""任务 K：手写材料的句子编号与引用校验。"""
from copy import deepcopy

import pytest

from vcradar import deep
from vcradar.deep_fetch import Page, clean_page, split_sentences
from test_deep_support import FUNDING, PRODUCT, USE, pages, payload


def numbered_payload():
    """将共享的合成事实转换为模型的新编号契约。"""
    by_text = {item["text"]: item["id"] for item in split_sentences(pages()[0].body, 1)}
    result = payload()

    def visit(value):
        if isinstance(value, dict):
            if value.get("quote"):
                value["quote_ids"] = [by_text[value.pop("quote")]]
                value.pop("page", None)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    return result


def test_english_boundaries_preserve_abbreviations_decimals_and_original_characters():
    original = [
        "Cedar Inc. serves the U.S. market at $1.25 per job.",
        "Developers deploy through an SDK!", "Does it accept scripts?",
        "It supports SDKs;", "Teams manage scheduled jobs", "Works across operating systems.",
    ]
    body = " ".join(original[:4]) + "\n" + "\n".join(original[4:])
    sentences = split_sentences(body, 3)
    assert [item["text"] for item in sentences] == original
    assert [item["id"] for item in sentences] == [f"p3.s{i}" for i in range(1, 7)]
    assert all(item["text"] in body for item in sentences)


def test_chinese_punctuation_newlines_and_short_fragments_are_preserved():
    parts = ["这是面向软件开发团队的工具。", "它支持多个项目同时运行！", "是否支持团队共同使用？", "团队可以查看运行记录；", "没有标点的原始说明段落"]
    assert [s["text"] for s in split_sentences("\n".join(parts), 1)] == parts
    body = "好。\n这是保留原始字符的一句说明。\n对。"
    assert split_sentences(body, 2) == [{"id": "p2.s1", "text": body}]
    assert split_sentences("", 1) == []
    assert split_sentences("短。", 1) == [{"id": "p1.s1", "text": "短。"}]


@pytest.mark.parametrize("size", [300, 301, 305, 309, 310, 600, 601, 8000, 8100])
def test_long_sentences_split_without_losing_text_or_leaving_short_tail(size):
    body = "原" * size
    sentences = split_sentences(body, 7)
    assert "".join(item["text"] for item in sentences) == body[:8000]
    assert all(10 <= len(item["text"]) <= 300 for item in sentences)
    assert [item["id"] for item in sentences] == [f"p7.s{i}" for i in range(1, len(sentences) + 1)]


def test_cleaning_keeps_html_paragraph_and_plain_text_newline_boundaries():
    first, second, third = "First original paragraph without punctuation", "Second original paragraph", "Third original paragraph"
    text, *_ = clean_page(f"<article><p>{first}</p><div>{second}<br>{third}</div></article>", "https://cedar.example/")
    assert [s["text"] for s in split_sentences(text, 1)] == [first, second, third]
    plain, *_ = clean_page(first + "\n" + second, "https://cedar.example/")
    assert [s["text"] for s in split_sentences(plain, 1)] == [first, second]


def test_english_period_without_following_space_still_splits_but_website_dots_do_not():
    first, second = "This is a complete sentence.", "The site is https://cedar.example/docs."
    assert [s["text"] for s in split_sentences(first + second, 1)] == [first, second]


@pytest.mark.parametrize("ids,expected", [(["p1.s1"], PRODUCT), (["p1.s1", "p1.s2"], PRODUCT + " " + USE)])
def test_ids_supply_the_quote_and_page_without_model_transcription(ids, expected):
    raw = numbered_payload()
    raw["产品是什么"][0].update(quote_ids=ids, quote="The model's invented transcription is ignored.")
    before = deepcopy(raw)
    checked = deep.validate_result(raw, pages())
    point = checked.result["产品是什么"][0]
    assert checked.ok and checked.removed_points == 0
    assert point["quote"] == expected and point["quote_ids"] == ids
    assert point["page"] == 1 and point["url"] == pages()[0].url
    assert deep.quote_matches(point["quote"], pages()[0].body)
    assert raw == before
    assert checked.result["标签依据"]["ai"]["quote"] == PRODUCT
    assert checked.facts["round"][0]["quote"] == FUNDING


@pytest.mark.parametrize("ids,code", [
    ([], "bad_quote_ids"), ("p1.s1", "bad_quote_ids"), ([False], "bad_quote_ids"),
    ([{}], "bad_quote_ids"), (["p1.s1"] * 3, "bad_quote_ids"),
    (["p1.s999"], "quote_id_not_found"), (["p99.s1"], "quote_id_not_found"),
    (["p1.s1", "p2.s1"], "quote_ids_cross_page"),
    (["p1.s1", "p1.s3"], "quote_ids_not_consecutive"),
    (["p1.s2", "p1.s1"], "quote_ids_not_consecutive"),
    (["p1.s1", "p1.s1"], "quote_ids_not_consecutive"),
])
def test_bad_ids_remove_the_point_even_with_a_valid_legacy_quote(ids, code):
    raw = numbered_payload()
    point = {"text": "开发工具。", "quote_ids": ids, "quote": PRODUCT, "page": 1}
    raw["解决谁的什么问题"] = [point]
    materials = [*pages(), Page(2, "https://second.example/", "2026-10-31", USE)]
    checked = deep.validate_result(raw, materials)
    assert checked.ok and checked.removed_points == 1
    assert checked.result["解决谁的什么问题"] == deep.MISSING
    assert checked.removed_reasons[0]["code"] == code
    assert checked.previous_errors[0]["point"] == point


@pytest.mark.parametrize("page", [2, True])
def test_explicit_page_cannot_override_sentence_page(page):
    raw = numbered_payload()
    raw["风险"] = [{"text": "提供工具。", "quote_ids": ["p1.s1"], "page": page}]
    checked = deep.validate_result(raw, pages())
    assert checked.removed_reasons[0]["code"] == "quote_page_mismatch"


@pytest.mark.parametrize("extra,code", [
    ({"text": "有 3 个试用客户。"}, "number_not_in_quote"),
    ({"text": "融资情况。", "value": "$100 million"}, "number_not_in_quote"),
    ({"text": "Mira Example 是创始人。"}, "name_not_in_quote"),
    ({"text": "创始人情况。", "value": "Mira Example"}, "name_not_in_quote"),
    ({"text": "创始人情况。", "entities": [{"name": "张三", "type": "人"}]}, "name_not_in_quote"),
])
def test_numbers_names_and_values_must_be_in_this_quote_even_if_elsewhere_on_page(extra, code):
    raw = numbered_payload()
    raw["解决谁的什么问题"] = [{"text": "事实说明。", "quote_ids": ["p1.s1"], **extra}]
    checked = deep.validate_result(raw, pages())
    assert checked.ok and checked.result["解决谁的什么问题"] == deep.MISSING
    assert checked.removed_reasons[0]["code"] == code
    assert "p1.s1" in checked.removed_reasons[0]["reason"]


def test_fact_value_does_not_get_combined_with_the_end_of_the_chinese_point():
    raw = numbered_payload()
    raw["团队"] = [{"text": "Boston", "field": "hq", "value": "Boston", "quote_ids": ["p1.s4"]}]
    checked = deep.validate_result(raw, pages())
    assert checked.removed_points == 0 and checked.facts["hq"]["value"] == "Boston"


def test_over_300_combined_characters_uses_first_sentence_and_rechecks_its_facts():
    first, second = "原" * 200 + "。", "文" * 110 + "88 个客户。"
    materials = [*pages(), Page(2, "https://second.example/", "2026-10-31", first + " " + second)]
    raw = numbered_payload()
    raw["风险"] = [{"text": "来源描述了一种工具。", "quote_ids": ["p2.s1", "p2.s2"]}]
    checked = deep.validate_result(raw, materials)
    assert checked.result["风险"][0]["quote"] == first
    assert checked.result["风险"][0]["quote_ids"] == ["p2.s1"]
    raw["风险"][0]["text"] = "有 88 个客户。"
    checked = deep.validate_result(raw, materials)
    assert checked.removed_reasons[0]["code"] == "number_not_in_quote"


def test_adjacent_chinese_sentences_without_whitespace_still_pass_verbatim_protection():
    first, second = "这是面向软件开发者的工具。", "它提供用于部署程序的界面。"
    materials = [*pages(), Page(2, "https://second.example/", "2026-10-31", first + second)]
    raw = numbered_payload()
    raw["风险"] = [{"text": "面向开发者提供工具。", "quote_ids": ["p2.s1", "p2.s2"]}]
    point = deep.validate_result(raw, materials).result["风险"][0]
    assert point["quote"] == first and point["quote_ids"] == ["p2.s1"]
    assert deep.quote_matches(point["quote"], materials[1].body)


def test_label_evidence_uses_same_ids_and_reports_invalid_references():
    raw = numbered_payload()
    raw["标签依据"]["ai"]["quote_ids"] = ["p1.s999"]
    checked = deep.validate_result(raw, pages())
    assert checked.result["标签依据"]["ai"]["verdict"] == "待核"
    assert "ai" not in checked.facts
    assert checked.removed_reasons[0]["section"] == "标签依据"
    assert checked.removed_reasons[0]["point_index"] == "ai"


def test_old_quotes_remain_compatible_with_case_sensitive_validation():
    raw = payload()
    assert deep.validate_result(raw, pages()).removed_points == 0
    raw["产品是什么"][0]["quote"] = PRODUCT.lower()
    checked = deep.validate_result(raw, pages())
    assert not checked.ok and checked.removed_reasons[0]["code"] == "quote_not_in_source"
