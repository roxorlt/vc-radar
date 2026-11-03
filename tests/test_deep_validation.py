from copy import deepcopy
from datetime import date, datetime

import pytest

from vcradar import deep
from vcradar.deep_fetch import Page
from test_deep_support import FUNDING, HQ, PRODUCT, RISK, TEAM, USE, pages, payload, point


@pytest.mark.parametrize(("quote", "body", "expected"), [
    ("Alpha … Gamma", "Alpha Beta Gamma", True),
    ("Gamma … Alpha", "Alpha Beta Gamma", False),
    ("Alpha ... Gamma", "Alpha Beta Gamma", True),
    ("alpha … Gamma", "Alpha Beta Gamma", False),
    ('Alice’s  “tool” , works.', 'Alice\'s "tool",\nworks.', True),
    ("A &amp; B", "A & B", True),
    ("A\u200dB\u00a0C", "AB C", True),
    ("...", "anything", False),
    ("Alpha … Alpha", "Alpha", False),
    ("Beta", "Other page only", False),
])
def test_quote_matching_uses_reference_script_rules(quote, body, expected):
    assert deep.quote_matches(quote, body) is expected


def test_eight_items_and_real_sources_come_from_program():
    raw = payload()
    raw["全部来源"] = [{"page": 500, "url": "https://invented.example/"}]
    raw["产品是什么"][0]["url"] = "https://invented.example/"
    checked = deep.validate_result(raw, pages())
    assert checked.ok and not checked.quality_pending and checked.removed_points == 0
    assert list(checked.result)[:8] == list(deep.SECTIONS)
    assert checked.result["竞品"] == deep.MISSING
    assert checked.result["全部来源"][0]["url"] == pages()[0].url
    assert checked.result["产品是什么"][0]["url"] == pages()[0].url
    assert len(checked.result["融资"]) == 5


@pytest.mark.parametrize("bad", [
    point("公司有 13 家试用客户。", RISK),
    point("公司有 3% 客户。", RISK),
    point("公司成立于 2026 年。", TEAM),
    point("融资 $30M。", FUNDING),
    point("Invented Person 是创始人。", TEAM),
    point("名字来自原句。", TEAM, entities=[{"name": "虚构张三", "type": "人"}]),
    point("新机构领投。", FUNDING, entities=[{"name": "Invented Ventures", "type": "投资方"}]),
    point(quote=PRODUCT.lower()),
    point(quote="Made up quotation without any source."),
    point(page=99), point(page=True), point(quote="tiny"), point(quote="x" * 301),
    point(entities="invalid"), {"page": 1, "quote": PRODUCT},
])
def test_invalid_quotes_numbers_names_and_sources_are_removed(bad):
    raw = payload()
    raw["解决谁的什么问题"] = [bad]
    checked = deep.validate_result(raw, pages())
    assert checked.ok
    assert checked.removed_points == 1
    assert checked.result["解决谁的什么问题"] == deep.MISSING


def test_matching_quote_on_another_page_does_not_pass():
    raw = payload()
    raw["解决谁的什么问题"] = [point("不跨页引用。", "Only the second page contains this sentence.")]
    checked = deep.validate_result(raw, [*pages(), Page(2, "https://two.example/", "2026-10-31", "Only the second page contains this sentence.")])
    assert checked.removed_points == 1


@pytest.mark.parametrize(("hit", "verdict", "quote", "expected"), [
    (False, "是", PRODUCT, "是"), (False, "是", "", "待核"),
    (False, "否", "", "否"), (True, "否", "", "待核"),
    (True, "否", PRODUCT, "否"), (True, "是", "bad quotation here", "待核"),
    (True, "拿不准", "", "待核"),
])
def test_labels_follow_72(hit, verdict, quote, expected):
    raw = payload()
    raw["标签依据"]["social"] = {"verdict": verdict, "reason": "这是开发工具。", "quote": quote, "page": 1}
    checked = deep.validate_result(raw, pages(pages()[0].body + (" Their founders are close friends." if hit else "")))
    assert checked.result["标签依据"]["social"]["verdict"] == expected


def test_missing_reason_for_negative_label_is_pending():
    raw = payload()
    raw["标签依据"]["social"] = {"verdict": "否"}
    assert deep.validate_result(raw, pages()).result["标签依据"]["social"]["verdict"] == "待核"


def test_label_reason_without_quote_cannot_add_names_or_numbers():
    raw = payload()
    raw["标签依据"]["social"] = {"verdict": "否", "reason": "Invented Person 说有 900 个用户。"}
    label = deep.validate_result(raw, pages()).result["标签依据"]["social"]
    assert label["verdict"] == "待核" and label["text"] == deep.MISSING


@pytest.mark.parametrize("required", ["产品是什么", "融资"])
def test_required_section_must_have_valid_content(required):
    raw = payload()
    raw[required] = []
    assert deep.validate_result(raw, pages()).error == "required_empty"
    assert not deep.validate_result(raw, pages()).ok


def test_product_requires_two_points_and_sources_cannot_be_empty():
    raw = payload()
    raw["产品是什么"] = raw["产品是什么"][:1]
    assert not deep.validate_result(raw, pages()).ok
    assert not deep.validate_result(payload(), []).ok


def test_quality_pending_requires_strictly_more_than_half():
    raw = {"产品是什么": payload()["产品是什么"], "融资": payload()["融资"], "风险": [point(quote="Not a real quote for this source.")] * 3}
    checked = deep.validate_result(raw, pages())
    assert checked.ok and checked.removed_points == 3 and checked.total_points == 6 and not checked.quality_pending
    raw["风险"].append(point(quote="Not a real quote for this source."))
    assert deep.validate_result(raw, pages()).quality_pending


def test_risk_evaluations_are_removed_even_when_the_quote_matches():
    raw = payload()
    raw["风险"] = [point("这家公司潜力巨大，值得投资。", RISK)]
    assert deep.validate_result(raw, pages()).result["风险"] == deep.MISSING


def test_unsupported_fact_value_cannot_create_new_evidence():
    raw = payload()
    raw["团队"][0]["value"] = "Paris"
    assert "hq" not in deep.validate_result(raw, pages()).facts


def test_object_links_are_resolved_from_evidence_only():
    page = pages()[0]
    page.links = [
        {"url": "https://cedar.example/docs?utm_source=x&gclid=x&lang=en#fragment", "text": "Cedar Actors"},
        {"url": "https://www.linkedin.com/in/mira-example/?utm_campaign=x", "text": "Mira Example"},
        {"url": "https://crunchbase.com/organization/maple", "text": "Maple Ventures"},
    ]
    company = {"id": "cedar", "name": "Cedar Labs", "website": "https://unproven.example/", "sources": []}
    result = {"entities": [
        {"name": "Cedar Actors", "type": "产品", "url": "https://invented.example/"},
        {"name": "Mira Example", "type": "人", "url": ""},
        {"name": "Maple Ventures", "type": "投资方", "url": "https://crunchbase.com/organization/maple"},
        {"name": "Mystery", "type": "竞品", "url": "https://cedar.example/guessed-path"},
    ]}
    resolved = deep.resolve_objects(result, company, [page], [], date(2026, 10, 31))
    assert resolved["entities"][0]["url"] == "https://cedar.example/docs?lang=en"
    assert resolved["entities"][0]["url_source"] == "来源页"
    assert resolved["entities"][0]["target"] == "_blank"
    assert resolved["entities"][0]["rel"] == "noopener nofollow"
    assert resolved["entities"][1]["url"] == "https://www.linkedin.com/in/mira-example/"
    assert not resolved["entities"][2]["url"] and not resolved["entities"][3]["url"]
    assert not resolved["objects"][0]["url"]


@pytest.mark.parametrize(("verified", "verified_at", "url", "allowed"), [
    (True, "2026-10-30 20:22", "https://maple.example/team/mira?utm_medium=x#top", True),
    (False, "2026-10-30 20:22", "https://maple.example/team/mira", False),
    (True, "2026-11-01 00:00", "https://maple.example/team/mira", False),
    (True, "invalid", "https://maple.example/team/mira", False),
    (True, "2026-10-30 20:22", "javascript:alert(1)", False),
    (True, "2026-10-30 20:22", "https://crunchbase.com/person/mira", False),
    (True, "2026-10-30 20:22", "https://maple.example/blog/unrelated", False),
])
def test_verified_records_and_person_pages(verified, verified_at, url, allowed):
    record = {"name": "Mira Example", "company": "Cedar Labs", "verified": verified, "verified_at": verified_at, "url": url}
    result = {"entities": [{"name": "Mira Example", "type": "人"}]}
    deep.resolve_objects(result, {"id": "cedar", "name": "Cedar Labs"}, pages(), [record], date(2026, 10, 31))
    entity = result["entities"][0]
    assert bool(entity["url"]) is allowed
    if allowed:
        assert entity["url_source"] == "核对记录" and entity["verified_at"] == verified_at


def test_person_page_itself_is_valid_only_when_person_is_present():
    page = Page(1, "https://cedar.example/about", "2026-10-31", TEAM)
    result = {"entities": [{"name": "Mira Example", "type": "人"}, {"name": "Not Present", "type": "人"}]}
    deep.resolve_objects(result, {"id": "cedar", "name": "Cedar Labs"}, [page], [], date(2026, 10, 31))
    assert result["entities"][0]["url"] == page.url
    assert not result["entities"][1]["url"]


def test_official_website_field_is_provenance_and_record_is_company_scoped():
    company = {"id": "cedar", "name": "Cedar Labs", "sources": [{"url": pages()[0].url, "structured": {"website": "https://cedar.example/"}}]}
    records = [{"company": "Other Company", "name": "Maple Ventures", "url": "https://maple.example/", "verified": True, "verified_at": "2026-10-30 20:22"}]
    result = {"entities": [{"name": "Maple Ventures", "type": "投资方"}]}
    deep.resolve_objects(result, company, pages(), records, date(2026, 10, 31))
    assert result["objects"][0]["url"] == "https://cedar.example/"
    assert result["objects"][0]["url_provenance"]["field"] == "website"
    assert not result["entities"][0]["url"]


def test_six_new_evidence_fields_and_equivalent_values():
    ended = "2026-10-31T12:00:00+08:00"
    values = {"hq": "Paris", "round": "Series A", "amount": "$7M", "website": "https://new.example/", "ai": "是", "social": "否"}
    facts = {key: {"value": value, "quote": PRODUCT, "page": 1, "url": pages()[0].url} for key, value in values.items()}
    for key in ("round", "amount"):
        facts[key]["quote"] = "Cedar Labs raised $7M in Series A."
    judgement = {"hq": "待核", "round": "待核", "amount": "$3M", "website": None, "ai": "待核", "social": "是"}
    before = deepcopy(judgement)
    found = deep.find_new_evidence(judgement, facts, ended)
    assert [entry["field"] for entry in found] == list(deep._FIELD_NAMES.values())
    assert all(entry["found_at"] == ended for entry in found)
    assert judgement == before
    assert not deep.find_new_evidence({"hq": "San Francisco", "round": "种子轮", "amount": "$3M", "website": "https://www.cedar.example/"}, {
        "hq": {**facts["hq"], "value": "旧金山"}, "round": {**facts["round"], "value": "Seed"},
        "amount": {**facts["amount"], "value": "$3 million", "quote": FUNDING},
        "website": {**facts["website"], "value": "http://cedar.example/"}}, ended)


@pytest.mark.parametrize(("stamp", "factor"), [
    ("2026-10-30T08:59:59+08:00", 1), ("2026-10-30T09:00:00+08:00", 2),
    ("2026-10-30T12:00:00+08:00", 1), ("2026-10-30T14:00:00+08:00", 2),
    ("2026-10-30T18:00:00+08:00", 1), ("2026-10-31T09:00:00+08:00", 1),
    ("2026-10-01T09:00:00+08:00", 1), ("2026-10-10T09:00:00+08:00", 1),
    ("2026-10-30T01:00:00+00:00", 2),
])
def test_usage_prices_cache_hits_and_peak_boundaries(stamp, factor):
    cost = deep.usage_cost({"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 600_000, "completion_tokens": 200_000}, datetime.fromisoformat(stamp))
    assert cost == pytest.approx(1.212 * factor)


def test_all_cache_hit_and_missing_cache_values():
    stamp = datetime.fromisoformat("2026-10-31T09:00:00+08:00")
    assert deep.usage_cost({"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000}, stamp) == 0.02
    assert deep.usage_cost({"prompt_tokens": 1_000_000}, stamp) == 1
