"""R14o：第四轮详情显示回归，全部数据为手写合成样例。"""
from hashlib import sha256
import json
from pathlib import Path
import re

import pytest

from vcradar import site, wording
from test_r14m_walkthrough_round3 import detail_data, detail, ui  # noqa: F401
from test_r14n_v17d_components import parse
from test_site_build import add_event, dump, render_page, seeded, snapshot  # noqa: F401


W = wording.SITE_TEXT


def facts_grid(html):
    return parse(html).find("dl", "facts")[0]


def funding_section(html):
    return next(section for section in parse(html).find("section") if section.attrs.get("id") == "deep-5")


def current_event(conn):
    conn.execute("UPDATE funding_event SET first_ingested_at='2026-10-31 08:42:00' WHERE id=2")


def comparisons(seeded, judgement, entries):
    conn, _ = seeded
    current_event(conn)
    conn.execute("UPDATE deepdive SET push_judgement_json=?,new_evidence_json=? WHERE company_id='mach9'",
                 (dump(judgement), dump([{"page": 1, "quote": "Handwritten evidence for display.", **entry} for entry in entries])))
    path, _ = snapshot(seeded)
    payload = json.loads((path / "c/mach9.json").read_text())
    return path, payload["companies"][0]


def test_1_verification_note_once_and_sources_remain(detail_data):
    company = detail_data["companies"][0]
    company["verification"] = [{"field": "金额", "text": "已查过 3 次，仍无法确认", "reason": "搜索没找到", "urls": [], "quotes": []}]
    company["verification_sources"] = [{"url": "https://verify.example/evidence", "quote": "Handwritten verification."}]
    html = detail(detail_data)
    tree = parse(html)
    assert "入库查证" not in [node.text for node in tree.find("h2")]
    assert len(tree.find("p", "facts-note")) == 1
    assert html.count("已查过 3 次，仍无法确认") == 1
    assert "入库查证用到的来源" in [node.text for node in tree.find("h2")]
    assert 'href="https://verify.example/evidence"' in html


@pytest.mark.parametrize("case,point,amount", [
    ("Kanopy", "共完成 3 轮融资，累计融资 680K 美元", "金额未披露"),
    ("adsilico", "种子轮和 Series A 合计使总股权融资达 £13.5M", "1700 万美元"),
    ("Epoch", "金额：来源里没写", "425 万美元"),
    ("BioStack", "金额：来源里没写", "950 万美元"),
])
def test_2_funding_kv_uses_only_current_event(detail_data, case, point, amount):
    event = detail_data["events"][0]
    event.update(round="种子轮", round_text="YC 标准投资", leads=["Y Combinator"], amount=amount)
    item = detail_data["companies"][0]["deepdive"]["items"][4]
    item.update(points=[{"text": point, "evidence": [], "entities": []}], missing=["轮次", "金额", "领投"])
    section = funding_section(detail(detail_data))
    assert [node.text for node in section.find("dt")] == ["本轮", "领投", "跟投", "日期"]
    assert [node.text for node in section.find("dd")] == [f"种子轮 · {amount}", "Y Combinator", "Synthetic Ventures", "11-02"]
    assert len(section.find("dl", "kv")) == 1 and not section.find(cls="point-label")
    assert all(text not in section.text for text in ("累计", "共完成", "合计", "轮次：来源里没写", "金额：来源里没写", "原文", "YC 标准投资"))


def test_2_missing_rows_follow_event_and_keep_four_rows(detail_data):
    detail_data["events"][0].update(amount="", leads=[], follows=[], date=None)
    section = funding_section(detail(detail_data))
    assert [node.text for node in section.find("dt")] == ["本轮", "领投", "跟投", "日期"]
    assert [node.text for node in section.find("dd")] == ["种子轮 · 金额未披露"] + [W["missing"]] * 3


def test_2_backend_selects_company_event_and_keeps_stored_deep_result(seeded):
    conn, _ = seeded
    current_event(conn)
    raw = dump({"融资": [{"text": "共完成 3 轮融资，累计合计融资 680K 美元"}, {"field": "amount", "text": W["missing"]}]})
    conn.execute("UPDATE deepdive SET result_json=? WHERE company_id='mach9'", (raw,))
    path, _ = snapshot(seeded)
    payload = json.loads((path / "c/mach9.json").read_text())
    company = payload["companies"][0]
    event = next(event for event in payload["events"] if event["id"] == company["event_id"])
    assert company["event_id"] == 2
    section = funding_section(render_page(path, "detailHtml", "mach9"))
    assert section.find("dd")[0].text == f'{event["round"]} · {event["amount"]}'
    assert section.find("dd")[1].text == "、".join(event["leads"])
    assert "累计" not in section.text and "合计" not in section.text
    assert conn.execute("SELECT result_json FROM deepdive WHERE company_id='mach9'").fetchone()[0] == raw


@pytest.mark.parametrize("amount,large", [("1700 万美元", True), ("金额未披露", False), ("", False)])
def test_3_amount_cell_uses_amt_and_small_lines(detail_data, amount, large):
    detail_data["events"][0].update(amount=amount, round_text="seed funding")
    grid = facts_grid(detail(detail_data))
    cell = grid.children[0]
    assert ("amt" in cell.attrs.get("class", "").split()) is large
    dd = cell.find("dd")[0]
    assert [node.text for node in dd.find("small")] == (["种子轮（原文 seed funding）"] if large else []) + [
        "领投：Synthetic Capital", "跟投：Synthetic Ventures"]
    assert dd.children[0] == (amount if large else "种子轮（原文 seed funding）")


def test_3_style_restores_component_defaults_without_changing_design_assets():
    style = (site.ASSETS / "style.css").read_text()
    assert ".fact-card .facts{margin-top:0;border-top:0}" in style
    assert not re.search(r"[^{}]*\.facts\s+(?:dt|dd)[^{}]*\{[^{}]*font\s*:", style)
    for name in ("components.css", "tokens.css"):
        assert (site.ASSETS / name).is_file() and (site.ASSETS / name).stat().st_size > 1000


@pytest.mark.parametrize("round_name,original,expected", [
    ("种子轮", " seed funding ", "种子轮（原文 seed funding）"),
    ("种子轮", "YC 标准投资", "种子轮"),
    ("A 轮", "Series A", "A 轮（原文 Series A）"),
    ("A 轮", " a 轮 ", "A 轮"),
    ("种子轮", "种子轮", "种子轮"),
    ("种子轮", "", "种子轮"),
])
def test_4_original_round_only_in_key_fields(detail_data, round_name, original, expected):
    detail_data["events"][0].update(round=round_name, round_text=original)
    html = detail(detail_data)
    grid = facts_grid(html)
    assert grid.children[0].find("small")[0].text == expected
    assert funding_section(html).find("dd")[0].text == f"{round_name} · 1000 万美元"
    assert parse(html).find("span", "ftl-round")[0].text == round_name
    assert "种子轮 · seed" not in html


@pytest.mark.parametrize("reasons,original,is_fund,expected", [
    (["轮次不在范围"], "Series D", False, "不在收录范围 · B 轮及以后"),
    (["轮次不在范围"], "Fund IV", True, "不在收录范围 · 基金募资"),
    (["轮次不在范围"], "debt", False, "不在收录范围 · 只有债务融资"),
    (["批次超过 12 个月"], "Seed", False, "不在收录范围 · YC 批次超过一年"),
    ([], "Series D", False, "不在收录范围"),
])
def test_5_scope_round_and_history_never_show_internal_state(seeded, reasons, original, is_fund, expected):
    conn, _ = seeded
    conn.execute("UPDATE company SET status='已过滤' WHERE id='quippy'")
    conn.execute("UPDATE funding_event SET round_norm='过滤',round_text=?,filter_reasons_json=? WHERE id=6", (original, dump(reasons)))
    conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=6", (dump({"is_fund_raise": is_fund}),))
    path, data = snapshot(seeded)
    payload = json.loads((path / "c/quippy.json").read_text())
    event = payload["events"][0]
    assert event["round"] == event["status"] + (f' · {event["filter_reasons"][0]}' if reasons else "") == expected
    assert event["status"] == event["company_status"] == W["out_of_scope"]
    assert event["out_of_scope"] is True
    html = render_page(path, "detailHtml", "quippy")
    assert facts_grid(html).children[0].find("small")[0].text == expected
    assert parse(html).find("span", "ftl-round")[0].text == expected
    assert original not in html and "推送日期" not in html
    assert 6 not in data["listing"] and 6 in data["week"]["filtered"]
    if reasons:
        assert event["filter_reasons"][0] in parse(render_page(path, "weekHtml")).text


def test_5_generated_site_contains_no_internal_scope_or_baseline_explanation(seeded):
    conn, _ = seeded
    conn.execute("UPDATE company SET status='已过滤' WHERE id='quippy'")
    conn.execute("UPDATE funding_event SET round_norm='过滤' WHERE id=6")
    path, _ = snapshot(seeded)
    outputs = [file for file in path.rglob("*") if file.is_file()]
    assert {"index.html", "app.js", "style.css", "data.json"} <= {file.name for file in outputs}
    assert any(file.parent.name == "c" for file in outputs)
    for file in outputs:
        content = file.read_bytes()
        assert "过滤".encode() not in content, file.name
        assert "首次运行时收录，未推送".encode() not in content, file.name
        assert "首次运行时已有，未推送".encode() not in content, file.name


def test_5_week_and_archive_sets_and_sorting_match_existing_contract(seeded):
    path, data = snapshot(seeded)
    assert data["week"]["cards"] == [3, 2, 4, 11, 8, 10, 15, 9, 12, 1]
    assert data["week"]["round_pending"] == [5] and data["week"]["filtered"] == [6]
    assert data["listing"] == [3, 2, 13, 4, 14, 11, 8, 10, 5, 7, 16, 15, 9, 12, 1]
    selected = ui(data, "['all','week','date:2026-10-23'].map(period=>ui.selectEvents(d,{...ui.defaultFilters(),period}).map(e=>e.id))")
    assert set(selected[0]) == set(range(1, 17)) - {6}
    assert set(selected[1]) == set(data["week"]["event_ids"]) - {6}
    assert set(selected[2]) == {1}
    pending = next(event for event in data["events"] if event["id"] == 5)
    assert pending["round"] == pending["status"] == W["round_pending"]
    assert 'data-value="过滤"' not in render_page(path, "weekHtml")


@pytest.mark.parametrize("kind,expected", [("scope", False), ("baseline", False), ("pushed", True)])
def test_6_push_date_only_for_pushed_in_scope_companies(detail_data, kind, expected):
    event = detail_data["events"][0]
    event["push_date"] = "2026-10-31"
    if kind == "scope":
        event.update(round="不在收录范围 · B 轮及以后", out_of_scope=True, filter_reasons=["B 轮及以后"])
    elif kind == "baseline":
        event["baseline_unpushed"] = True
        detail_data["companies"][0]["baseline_unpushed"] = True
    grid = facts_grid(detail(detail_data))
    assert ("推送日期" in [node.text for node in grid.find("dt")]) is expected


@pytest.mark.parametrize("field,old,new,found", [
    ("hq", {"country": "荷兰"}, "The Hague", None),
    ("国家", {"country": "英国"}, "London, England, United Kingdom", None),
    ("总部", {"hq": "Amsterdam"}, "The Hague", None),
    ("hq", {"country": "美国"}, "London", "英国"),
    ("hq", {"country": "美国"}, "Uncharted Place", "Uncharted Place"),
    ("round", {"round": "种子轮"}, "seed funding", None),
    ("round", {"round": "种子轮"}, "Series A", "A 轮"),
    ("amount", {"amount_display": "1000 万美元"}, "$10 million", None),
    ("amount", {"amount": "$10M"}, "$12 million", "1200 万美元"),
    ("amount", {"amount_display": "约 1333 万美元（原 £1000 万）"}, "£10 million ($13.4 million)", None),
    ("website", {"website": "https://www.frozen.example/pricing"}, "frozen.example/about", None),
    ("website", {"website": "https://www.frozen.example/"}, "https://fresh.example/", "fresh.example"),
    ("ai", {"ai": "是"}, True, None),
    ("ai", {"ai_label": "是"}, "否", "否"),
    ("ai", {"ai_label": "否"}, "否", None),
    ("ai", {"tags": ["都不是"]}, "否", None),
    ("social", {"tags": ["AI相关", "社交/社群相关"]}, "是", None),
    ("social", {"social": "否"}, False, None),
    ("social_label", {"social_label": "否"}, "是", "是"),
])
def test_7_normalizes_values_before_comparison(seeded, field, old, new, found):
    conn, _ = seeded
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('fx:event:2',?)", (dump({"base": "USD", "rates": {"GBP": 0.75}}),))
    path, company = comparisons(seeded, old, [{"field": field, "value": new}])
    evidence = company["deepdive"]["new_evidence"]
    if found is None:
        assert evidence == []
        assert not parse(render_page(path, "detailHtml", "mach9")).find("section", "evidence")
    else:
        assert len(evidence) == 1 and evidence[0]["found"] == found
        assert set(evidence[0]) == {"field", "pushed", "found", "evidence"}
        assert evidence[0]["evidence"][0]["quote"] == "Handwritten evidence for display."


def test_7_multiple_fields_render_separate_frozen_value_rows(seeded):
    conn, _ = seeded
    conn.execute("UPDATE company SET country='法国',ai_label='否',social_label='是' WHERE id='mach9'")
    conn.execute("UPDATE funding_event SET round_norm='A 轮',round_text='Series A',amount_text='$34M' WHERE id=2")
    old = {"name": "Mach9", "country": "荷兰", "round": "种子轮", "amount_display": "1000 万美元", "website": "https://frozen.example/", "ai": "是", "social": "否"}
    entries = [{"field": field, "value": value} for field, value in [
        ("hq", "London"), ("round", "Series A"), ("amount", "$12 million"),
        ("website", "https://fresh.example/"), ("ai", "否"), ("social", "是")]]
    path, company = comparisons(seeded, old, entries)
    assert company["header"]["country"] == "荷兰" and company["header"]["tags"] == [W["ai"]]
    assert [(row["pushed"], row["found"]) for row in company["deepdive"]["new_evidence"]] == [
        ("荷兰", "英国"), ("种子轮", "A 轮"), ("1000 万美元", "1200 万美元"), ("frozen.example", "fresh.example"), ("是", "否"), ("否", "是")]
    html = render_page(path, "detailHtml", "mach9")
    blocks = parse(html).find("section", "evidence")
    assert len(blocks) == 6
    assert [block.find("b")[0].text for block in blocks] == ["资料有出入：" + row["field"] for row in company["deepdive"]["new_evidence"]]
    for block, row in zip(blocks, company["deepdive"]["new_evidence"]):
        assert block.find("p")[0].text == f'推送时写的：{row["pushed"]}；新来源写的：{row["found"]}'
        assert block.find("q")[0].text == "Handwritten evidence for display."
        assert block.find("span", "ev-src")[0].find("a")
        assert block.find("span", "muted")[0].text == "页面没写日期 · 10-31 读取"
    assert html.index('class="evidence"') < html.index(">简介</h2>")
    assert "推送时的判定不变" not in html and "人工核实后" not in html


def test_7_one_comparison_per_field_after_ignoring_equal_values(seeded):
    path, company = comparisons(seeded, {"country": "荷兰"}, [
        {"field": "hq", "value": "The Hague"}, {"field": "总部", "value": "London"}, {"field": "country", "value": "Paris"}])
    evidence = company["deepdive"]["new_evidence"]
    assert [(row["field"], row["pushed"], row["found"]) for row in evidence] == [("国家", "荷兰", "英国")]
    assert len(parse(render_page(path, "detailHtml", "mach9")).find("section", "evidence")) == 1


@pytest.mark.parametrize("basis,expected", [
    ("San Francisco, CA, USA", "San Francisco, CA, USA"),
    ("London, England, United Kingdom", "London, England, United Kingdom"),
    ("Synthetic example company that verifies businesses and provides a service to teams worldwide.", "Crunchbase News 报道"),
    ("A startup has useful tools.", "Crunchbase News 报道"),
    ("Develops tools for teams.", "Crunchbase News 报道"),
    ("One two three four five six", "Crunchbase News 报道"),
])
def test_B_country_basis_keeps_short_places_and_uses_matching_source(seeded, basis, expected):
    conn, _ = seeded
    current_event(conn)
    conn.execute("UPDATE company SET hq='San Francisco',hq_basis=?,country='美国' WHERE id='mach9'", (basis,))
    add_event(conn, 20, "mach9", seen="2026-10-20 08:41:00", source="cbn_seed")
    conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=2", (dump({"hq_quote": basis}),))
    # 匹配来源排在本事件的第一来源后，避免总是取第一条也误过。
    conn.execute("INSERT INTO event_source SELECT 2,raw_item_id,url,source_name,?,credibility,model FROM event_source WHERE event_id=20", (dump({"_hq_basis": basis}),))
    conn.execute("UPDATE event_source SET extraction_json='{}' WHERE event_id=2 AND raw_item_id=2")
    path, _ = snapshot(seeded)
    payload = json.loads((path / "c/mach9.json").read_text())
    event = next(event for event in payload["events"] if event["id"] == 2)
    assert event["country_basis"] == expected
    html = render_page(path, "detailHtml", "mach9")
    grid = facts_grid(html)
    country = next(cell for cell in grid.children if cell.find("dt")[0].text == "国家和依据")
    assert country.find("dd")[0].text == f"美国 · 旧金山 · {expected}"
    assert "verifies businesses" not in grid.text
    assert conn.execute("SELECT hq_basis FROM company WHERE id='mach9'").fetchone()[0] == basis


def test_B_sentence_without_matching_source_uses_event_first_source(seeded):
    conn, _ = seeded
    current_event(conn)
    conn.execute("UPDATE company SET hq_basis='Synthetic company that verifies businesses for local teams.' WHERE id='mach9'")
    path, _ = snapshot(seeded)
    payload = json.loads((path / "c/mach9.json").read_text())
    event = next(event for event in payload["events"] if event["id"] == 2)
    assert event["country_basis"] == "TechCrunch 报道"
    assert "verifies businesses" not in facts_grid(render_page(path, "detailHtml", "mach9")).text


def test_B_conflict_basis_reduces_each_sentence_to_place_or_source():
    basis = "国家来源有冲突：The company is based in London, England, United Kingdom.；Synthetic firm has products for small teams."
    sources = [{"name": "TechCrunch", "source_id": "tc_venture", "extraction": {}},
               {"name": "Crunchbase News", "source_id": "cbn_seed", "extraction": {"hq_quote": "Synthetic firm has products for small teams."}}]
    assert site._country_basis({"hq_basis": basis}, sources) == "国家来源有冲突：伦敦, 英国, 英国；Crunchbase News 报道"
