"""任务 L：公司名引用豁免和总部同义写法，全部使用手写合成材料。"""
from copy import deepcopy

import pytest

from vcradar import deep
from test_deep_support import pages, payload, point


SUBJECT_QUOTE = "The startup builds tools for developers to inspect application logs."
ENDED = "2026-10-31T11:00:00+08:00"


@pytest.mark.parametrize("company_name,alias", [
    ("Dulo", "Dulo"),
    ("Dulo", "DULO"),
    ("Dulo Inc.", "Dulo"),
    ("Dulo Labs", "Dulo AI"),
    ("Dulo AI", "Dulo"),
    ("Dulo", "Dulo Labs AI Inc."),
    ("Dulo", "D.u-l o"),
    ("D.u-l o Labs Inc.", "DULO"),
    ("Dulo.AI", "Dulo"),
    ("Dulo A.I.", "Dulo"),
    ("Ｄｕｌｏ．ＡＩ", "Dulo"),
])
@pytest.mark.parametrize("placement", ["text", "entities"])
def test_current_company_alias_needs_no_literal_name_in_quote(company_name, alias, placement):
    raw = payload()
    candidate = point(f"{alias} 提供日志检查工具。" if placement == "text" else "提供日志检查工具。",
                      SUBJECT_QUOTE,
                      entities=[{"name": alias, "type": "公司"}] if placement == "entities" else [])
    raw["解决谁的什么问题"] = [candidate]
    before = deepcopy(raw)
    materials = pages(pages()[0].body + " " + SUBJECT_QUOTE)
    checked = deep.validate_result(raw, materials, company_name=company_name)
    assert checked.ok and checked.removed_points == 0
    assert checked.result["解决谁的什么问题"][0]["quote"] == SUBJECT_QUOTE
    assert raw == before


@pytest.mark.parametrize("name,kind", [
    ("Other Company", "公司"), ("Dulo Ventures", "投资方"),
    ("DuloBot", "竞品"), ("Mira Example", "人"), ("Dulo Console", "产品"),
])
@pytest.mark.parametrize("placement", ["text", "entities"])
def test_company_exception_does_not_exempt_other_names(name, kind, placement):
    raw = payload()
    raw["解决谁的什么问题"] = [point(
        f"Dulo 与 {name} 提供工具。" if placement == "text" else "Dulo 提供工具。",
        SUBJECT_QUOTE, entities=[{"name": name, "type": kind}] if placement == "entities" else [])]
    checked = deep.validate_result(raw, pages(pages()[0].body + " " + SUBJECT_QUOTE), company_name="Dulo")
    assert checked.ok and checked.removed_points == 1
    assert checked.result["解决谁的什么问题"] == deep.MISSING
    assert checked.removed_reasons[0]["code"] == "name_not_in_quote"
    assert name in checked.removed_reasons[0]["reason"]


def test_model_cannot_supply_the_company_name_to_exempt_an_unrelated_entity():
    raw = payload()
    raw["company"] = "Dulo"
    raw["解决谁的什么问题"] = [point("Dulo 提供工具。", SUBJECT_QUOTE)]
    checked = deep.validate_result(raw, pages(pages()[0].body + " " + SUBJECT_QUOTE))
    assert checked.removed_points == 1
    assert checked.removed_reasons[0]["code"] == "name_not_in_quote"


@pytest.mark.parametrize("candidate,code", [
    (point("Dulo 有 999 个客户。", SUBJECT_QUOTE), "number_not_in_quote"),
    (point("Dulo 提供工具。", "The startup does something absent from these pages."), "quote_not_in_source"),
])
def test_company_exception_keeps_number_and_verbatim_quote_checks(candidate, code):
    raw = payload()
    raw["解决谁的什么问题"] = [candidate]
    checked = deep.validate_result(raw, pages(pages()[0].body + " " + SUBJECT_QUOTE), company_name="Dulo")
    assert checked.removed_points == 1 and checked.removed_reasons[0]["code"] == code


def headquarters_evidence(old, new):
    quote = f"The startup is headquartered in {new}."
    judgement = {"hq": old}
    facts = {"hq": {"value": new, "quote": quote, "page": 1, "url": "https://news.example/location"}}
    before = deepcopy((judgement, facts))
    found = deep.find_new_evidence(judgement, facts, ENDED)
    assert (judgement, facts) == before
    return found, quote


@pytest.mark.parametrize("old,new", [
    ("Emeryville, Calif.", "Emeryville, California"),
    ("Emeryville, California", "EMERYVILLE, ca, USA"),
    ("Boston, Mass.", "Boston, Massachusetts, US"),
    ("Albany, N.Y.", "Albany, New York"),
    ("Seattle, Wash.", "Seattle, Washington, U.S.A."),
    ("Boulder, Colo.", "Boulder, Colorado"),
    ("Austin, TX", "Austin, Texas"),
    ("Portland, Maine", "Portland, ME"),
    ("Washington, D.C.", "Washington, District of Columbia"),
    ("Newark, N.J.", "newark - new jersey - usa"),
    ("旧金山", "San Francisco, CA, USA"),
    ("Emeryville", "Emeryville, Calif."),
    ("CA", "California"),
    ("NYC", "New York, NY"),
])
def test_equivalent_headquarters_do_not_create_new_evidence(old, new):
    assert not headquarters_evidence(old, new)[0]
    assert not headquarters_evidence(new, old)[0]


@pytest.mark.parametrize("old,new", [
    ("Emeryville, Calif.", "Oakland, California, USA"),
    ("Boston, MA", "Cambridge, Massachusetts"),
    ("Portland, OR", "Portland, Maine"),
    ("Boston, Mass.", "Boston, CA"),
    ("San Francisco, CA", "South San Francisco, Calif."),
    ("总部待核", "Emeryville, California"),
])
def test_different_city_or_state_still_creates_headquarters_evidence(old, new):
    found, quote = headquarters_evidence(old, new)
    assert found == [{"field": "总部", "value": new, "old_value": old, "quote": quote,
                      "page": 1, "url": "https://news.example/location", "found_at": ENDED}]


def test_headquarters_equivalence_uses_validated_fact_values():
    raw = payload()
    quote = "The startup is headquartered in Emeryville, California."
    raw["团队"] = [point("总部在 Emeryville, California。", quote,
                          field="hq", value="Emeryville, California")]
    checked = deep.validate_result(raw, pages(pages()[0].body + " " + quote))
    assert checked.ok and checked.facts["hq"]["value"] == "Emeryville, California"
    assert not deep.find_new_evidence({"hq": "Emeryville, Calif."}, {"hq": checked.facts["hq"]}, ENDED)
