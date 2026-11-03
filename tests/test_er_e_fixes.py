"""E3：简介修正、两次上限、确定性清洗和过滤路径。"""
from copy import deepcopy
import json

import pytest

from test_er_extract import item, output
from test_pipe_pipeline import offline
from vcradar import extract, render
from vcradar.llm import MockLLMClient
from vcradar.models import Extracted, RawItem


@pytest.mark.parametrize("problem", ["length", "sentences", "number", "quote"])
def test_first_summary_problem_gets_one_correction_with_specific_feedback(item, output, problem):
    bad = deepcopy(output)
    if problem == "length":
        bad["summary"] = "软件服务社区组织者，提供活动安排和成员协作功能。" * 7
    elif problem == "sentences":
        bad["summary"] = "产品服务社群。支持交流。安排活动。组织协作。用于团队。"
    elif problem == "number":
        bad["summary"] = "公司提供软件。服务 1700 家客户。用于活动安排。"
    else:
        bad["quotes"]["summary"] = "This quotation was invented."
    mock = MockLLMClient([bad, output])
    result = extract.model_extract(item, extract.rule_extract(item), mock, "summary")
    assert mock.calls == result.model_calls == 2 and result.summary == output["summary"]
    correction = json.loads(mock.requests[1]["user"])["summary_correction"]
    assert correction["previous_summary"] == bad["summary"]
    assert json.loads(mock.requests[1]["user"])["source_data"] == json.loads(mock.requests[0]["user"])["source_data"]
    expected = {"length": "超过 140", "sentences": "超过 4", "number": "数字 1700 来源里没有", "quote": "quotes.summary 不是原文逐字"}
    assert expected[problem] in "；".join(correction["problems"])


def test_failed_first_call_uses_up_retry_and_never_makes_third_correction(item, output):
    bad = deepcopy(output)
    bad["summary"] = "公司提供协作软件，帮助用户安排活动和沟通。" * 8
    mock = MockLLMClient([{"error": "http_429"}, bad, output])
    result = extract.model_extract(item, extract.rule_extract(item), mock, "summary")
    assert mock.calls == 2 and 0 < extract.summary_len(result.summary) <= 140
    assert result.summary.endswith("。") and result.summary.count("。") <= 4


def test_second_bad_summary_keeps_longest_complete_prefix(item, output):
    data = deepcopy(output)
    sentences = ["公司提供社群协作软件。", "帮助组织者安排活动和成员交流。", "产品面向团队使用并支持活动规划。", "软件提供任务协调、日常沟通以及活动信息整理功能。"]
    data["summary"] = "".join(sentences) + "软件为社区成员提供协作界面和日常活动工具。" * 4
    mock = MockLLMClient([data, data])
    result = extract.model_extract(item, extract.rule_extract(item), mock, "summary")
    assert result.summary == "".join(sentences)
    assert mock.calls == 2 and extract.summary_len(result.summary) <= 140


def test_first_sentence_longer_than_limit_gets_139_characters_and_period(item, output):
    data = deepcopy(output)
    data["summary"] = "软件为社区组织者提供日常活动安排与沟通协作功能" * 8 + "。其他内容。"
    result = extract.model_extract(item, extract.rule_extract(item), MockLLMClient([data, data]), "summary")
    assert result.summary == data["summary"][:139] + "。"
    assert extract.summary_len(result.summary) == 140


def test_unknown_numeric_sentence_is_removed_and_supported_amount_stays(item, output):
    data = deepcopy(output)
    data["summary"] = "公司为社群开发软件。拥有 1700 家客户。公司融资 $34M。团队于 2024 年成立。"
    result = extract.model_extract(item, extract.rule_extract(item), MockLLMClient([data, data]), "summary")
    assert result.summary == "公司为社群开发软件。公司融资 $34M。团队于 2024 年成立。"


def test_persistently_wrong_summary_quote_is_removed_and_warned(item, output, caplog):
    data = deepcopy(output)
    data["quotes"]["summary"] = "Not a sentence from this source."
    result = extract.model_extract(item, extract.rule_extract(item), MockLLMClient([data, data]), "summary")
    assert result.summary == output["summary"] and "summary" not in result.quotes
    assert "summary:quote_not_in_source" in caplog.text


def test_short_summary_warns_without_padding_or_second_call(item, output, caplog):
    data = deepcopy(output)
    data["summary"] = "公司开发协作软件。"
    mock = MockLLMClient([data, output])
    result = extract.model_extract(item, extract.rule_extract(item), mock, "summary")
    assert mock.calls == 1 and result.summary == data["summary"]
    assert "summary_length:" in caplog.text and "summary_sentences:" in caplog.text


def test_correction_timeout_retains_first_valid_fields_and_cleans_its_summary(item, output):
    bad = deepcopy(output)
    bad["summary"] = "产品提供社群软件。拥有 1700 个用户。"
    result = extract.model_extract(item, Extracted(), MockLLMClient([bad, {"error": "timeout"}]), "full")
    assert result.model_calls == 2 and result.company_name == "Aster"
    assert result.summary == "产品提供社群软件。" and result.ai.verdict == "是"


def test_short_remaining_budget_skips_correction_but_still_cleans(item, output):
    data = deepcopy(output)
    data["summary"] = "公司获得 1700 个客户。产品面向社区。"
    mock = MockLLMClient([data, output])
    result = extract.model_extract(item, extract.rule_extract(item), mock, "summary", budget_s=29)
    assert mock.calls == 1 and result.summary == "产品面向社区。"


@pytest.mark.parametrize("reason", ["missing_name", "not_financing", "fund", "debt"])
def test_model_filtered_items_do_not_validate_or_warn_about_summary(reason, caplog):
    body = "Aster raised a fund. Aster raised debt financing."
    item = RawItem("tc_venture", "filtered", "https://synthetic.example/filtered", "Financing update", None, body=body)
    data = {"summary": "公司拥有 1700 个客户。" * 30, "quotes": {"summary": "invented"}}
    if reason == "not_financing":
        data["is_financing"] = False
        data["company_name"] = "Aster"
        data["quotes"]["company_name"] = body
    elif reason in {"fund", "debt"}:
        key = "is_fund_raise" if reason == "fund" else "is_debt_only"
        data.update(company_name="Aster", **{key: True})
        data["quotes"].update(company_name=body, **{key: body})
    mock = MockLLMClient([data, data])
    result = extract.model_extract(item, Extracted(), mock, "full")
    assert mock.calls == 1 and result.summary == ""
    assert "summary" not in caplog.text


@pytest.mark.parametrize("field,value", [("is_fund_raise", True), ("is_debt_only", True), ("hq_text", "London"), ("round_text", "Series B")])
def test_rule_filtered_items_skip_model_except_foreign_country(item, field, value, caplog):
    ext = extract.rule_extract(item)
    setattr(ext, field, value)
    if field == "hq_text":
        ext.hq_quote = "The company is headquartered in London."
    mock = MockLLMClient([{"summary": "公司开发软件。服务团队。提供协作功能。"}])
    result = extract.model_extract(item, ext, mock, "full")
    if field == "hq_text":
        assert mock.calls == 1 and result.summary
    else:
        assert mock.calls == 0 and "summary" not in caplog.text


def test_clipping_does_not_turn_known_amount_into_an_unknown_number(item):
    summary = "字" * 137 + "$34M。"
    result = extract.sanitize_summary(item, summary)
    assert result == "字" * 137 + "。"


def test_push_first_sentence_over_sixty_characters_stays_intact():
    from test_er_render import card
    from vcradar.models import DigestData
    from test_er_render import DAY
    from datetime import datetime
    from vcradar.config import BJ
    first = "公司开发社区软件并帮助用户组织活动与沟通协作" * 4 + "。"
    data = DigestData(DAY, None, datetime(2026, 10, 30, 8, 40, tzinfo=BJ), "有新增", cards=[card(summary=first + "后续内容。")])
    text = render.render_push(data)
    assert f"｜{first}｜" in text and "后续内容" not in text
