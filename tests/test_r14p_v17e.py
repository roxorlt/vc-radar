"""R14p：用合成数据验证 V17e 组件、历史国家原因及详情显示。"""
from copy import deepcopy
from hashlib import md5
import json
from pathlib import Path
import re

import pytest

from vcradar import site, wording
from test_r14m_walkthrough_round3 import detail_data, detail, set_sources, source, ui  # noqa: F401
from test_r14n_v17d_components import parse
from test_r14o_round4 import facts_grid, funding_section
from test_site_build import add_company, add_event, render_page, seeded, snapshot  # noqa: F401


DESIGN = Path("design-pack/20261103-u-round4/v17e")
W = wording.SITE_TEXT


def round_cell(html):
    return next((cell for cell in facts_grid(html).find("div") if cell.find("dt")[0].text == W["current_round"]), None)


def test_0_legacy_country_reason_keeps_round_and_hides_push_date(seeded):
    conn, _ = seeded
    add_company(conn, "bleu-robotics", "Bleu Robotics", status="已过滤", country="法国")
    add_event(conn, 101, "bleu-robotics", filtered=["总部非美国"], round_norm="种子轮")
    conn.execute("UPDATE funding_event SET round_text='pre-seed' WHERE id=101")
    path, data = snapshot(seeded)
    payload = json.loads((path / "c/bleu-robotics.json").read_text())
    event = payload["events"][0]
    assert event["round"] == "种子轮" and not event["out_of_scope"]
    assert event["filter_reasons"] == [] and event["push_date"] is None
    grid = facts_grid(render_page(path, "detailHtml", "bleu-robotics"))
    assert grid.children[0].find("small")[0].text == "种子轮（原文 pre-seed）"
    assert "不在收录范围" not in grid.text and "推送日期" not in grid.text
    assert 101 not in data["listing"] and 101 not in data["week"]["event_ids"]
    row = conn.execute("SELECT round_norm,filter_reasons_json,push_date FROM funding_event WHERE id=101").fetchone()
    assert row["round_norm"] == "种子轮" and json.loads(row["filter_reasons_json"]) == ["总部非美国"]
    assert row["push_date"] == "2026-10-30"


@pytest.mark.parametrize("normalized,reasons,expected", [
    ("种子轮", ["总部非美国", "轮次不在范围"], "不在收录范围 · B 轮及以后"),
    ("种子轮", ["总部非美国", "批次超过 12 个月"], "不在收录范围 · YC 批次超过一年"),
    ("过滤", ["总部非美国"], "不在收录范围"),
    ("过滤", [], "不在收录范围"),
])
def test_0_visible_reasons_or_normalized_round_still_exclude(seeded, normalized, reasons, expected):
    conn, _ = seeded
    conn.execute("UPDATE funding_event SET round_norm=?,round_text='Series D',filter_reasons_json=? WHERE id=6",
                 (normalized, json.dumps(reasons)))
    path, _ = snapshot(seeded)
    payload = json.loads((path / "c/quippy.json").read_text())
    event = payload["events"][0]
    assert event["out_of_scope"] and event["round"] == expected and event["push_date"] is None
    html = render_page(path, "detailHtml", "quippy")
    assert round_cell(html).find("small")[0].text == expected
    assert "Series D" not in facts_grid(html).text and "推送日期" not in facts_grid(html).text


def test_0_empty_display_reason_does_not_mark_normal_round_out_of_scope(seeded, monkeypatch):
    conn, _ = seeded
    original = wording.filter_reason
    monkeypatch.setattr(wording, "filter_reason", lambda reason, **kwargs: "" if reason == "轮次不在范围" else original(reason, **kwargs))
    conn.execute("UPDATE funding_event SET round_norm='种子轮',round_text='pre-seed',filter_reasons_json=? WHERE id=6",
                 (json.dumps(["轮次不在范围"]),))
    path, _ = snapshot(seeded)
    payload = json.loads((path / "c/quippy.json").read_text())
    event = payload["events"][0]
    assert not event["out_of_scope"] and event["round"] == "种子轮" and event["filter_reasons"] == []
    assert event["push_date"] is None


def test_1_components_and_tokens_copied_cleanly_to_snapshot(seeded):
    actual = (site.ASSETS / "components.css").read_bytes()
    tokens = (site.ASSETS / "tokens.css").read_bytes()
    path, _ = snapshot(seeded)
    assert (path / "components.css").read_bytes() == actual
    assert (path / "tokens.css").read_bytes() == tokens
    style = (site.ASSETS / "style.css").read_text()
    assert not re.search(r"\.(?:evidence|ev-[\w-]+)\b[^{}]*\{", style)
    assert ".ev-vals { margin-top: var(--s-2); color: var(--c-ink); font: var(--t-body); }" in actual.decode()


@pytest.mark.parametrize("amount,round_name,leads,follows,first,small,large", [
    ("1700 万美元", "种子轮", ["Synthetic Capital"], ["Synthetic Ventures"], "1700 万美元",
     ["种子轮（原文 seed funding）", "领投：Synthetic Capital", "跟投：Synthetic Ventures"], True),
    ("金额未披露", "种子轮", ["Y Combinator"], [], "种子轮（原文 seed funding）", ["领投：Y Combinator"], False),
    ("", "种子轮", [], [], "种子轮（原文 seed funding）", [], False),
    ("1700 万美元", "", [], [], "1700 万美元", [], True),
    ("1700 万美元", W["round_pending"], ["Synthetic Capital"], [], "1700 万美元", ["领投：Synthetic Capital"], True),
    ("", "", ["Synthetic Capital"], ["Synthetic Ventures"], "领投：Synthetic Capital", ["跟投：Synthetic Ventures"], False),
    ("", "", [], ["Synthetic Ventures"], "跟投：Synthetic Ventures", [], False),
    ("", "", [], [], None, [], False),
    ("金额未披露", W["round_pending"], [], [], None, [], False),
])
def test_2_current_cell_only_renders_available_lines(detail_data, amount, round_name, leads, follows, first, small, large):
    detail_data["events"][0].update(amount=amount, round=round_name, round_text="seed funding", leads=leads, follows=follows)
    html = detail(detail_data)
    cell = round_cell(html)
    if first is None:
        assert cell is None and W["current_round"] not in [term.text for term in facts_grid(html).find("dt")]
    else:
        assert cell is not None and ("amt" in cell.attrs.get("class", "").split()) is large
        value = cell.find("dd")[0]
        assert value.children[0] == first
        assert [node.text for node in value.find("small")] == small
        assert not value.find("div")
    assert "金额未披露" not in facts_grid(html).text


def test_2_viraj_no_amount_structure_matches_design(detail_data):
    detail_data["events"][0].update(amount="金额未披露", round="种子轮", round_text="YC 标准投资", leads=["Y Combinator"], follows=[])
    actual = round_cell(detail(detail_data))
    design = parse((DESIGN / "system/parts/co-head.html").read_text()).find("dl", "facts")[0]
    reference = next(cell for cell in design.find("div") if cell.find("dt")[0].text == "本轮")
    assert actual.find("dd")[0].text == reference.find("dd")[0].text == "种子轮领投：Y Combinator"
    assert [node.tag for node in actual.find("dd")[0].children if not isinstance(node, str)] == ["small"]
    assert "amt" not in actual.attrs.get("class", "").split()


def test_3_hex_amount_cell_matches_v17e_example(detail_data):
    detail_data["events"][0].update(amount="1 亿美元", round="不在收录范围 · B 轮及以后", round_text="Series D", out_of_scope=True,
        leads=["Peak XV", "137 Ventures"], follows=["Amplify", "Avra", "Andreessen Horowitz"], push_date="2026-10-31")
    html = detail(detail_data)
    actual = round_cell(html)
    reference = parse((DESIGN / "system/parts/co-head.html").read_text()).find("dl", "facts")[-1].find("div", "amt")[0]
    assert actual.attrs["class"] == reference.attrs["class"] == "amt"
    assert actual.find("dd")[0].text == reference.find("dd")[0].text
    assert [node.text for node in actual.find("small")] == [node.text for node in reference.find("small")]
    assert "原文" not in actual.text and "Series D" not in actual.text and "推送日期" not in facts_grid(html).text


@pytest.mark.parametrize("kind,expected", [("scope", False), ("baseline", False), ("normal", True)])
def test_3_push_date_visibility_and_no_baseline_explanation(detail_data, kind, expected):
    detail_data["events"][0]["push_date"] = "2026-10-31"
    if kind == "scope":
        detail_data["events"][0].update(round="不在收录范围", out_of_scope=True)
    if kind == "baseline":
        detail_data["companies"][0]["baseline_unpushed"] = True
    html = detail(detail_data)
    assert ("推送日期" in [node.text for node in facts_grid(html).find("dt")]) is expected
    assert "首次运行时收录，未推送" not in html


def test_4_each_evidence_field_has_heading_values_quote_and_source(detail_data):
    evidence = detail_data["companies"][0]["deepdive"]["new_evidence"]
    evidence[0].update(pushed="1000 万美元", found="1200 万美元")
    evidence.append({"field": "国家", "pushed": "荷兰", "found": "英国", "evidence": deepcopy(evidence[0]["evidence"])})
    tree = parse(detail(detail_data))
    blocks = tree.find("section", "evidence")
    assert len(blocks) == 2
    for block, entry in zip(blocks, evidence):
        children = [child for child in block.children if not isinstance(child, str)]
        assert [child.tag for child in children] == ["div", "p", "ul"]
        assert children[0].attrs["class"] == "ev-h" and children[0].find("b")[0].text == f'资料有出入：{entry["field"]}'
        assert children[1].attrs["class"] == "ev-vals"
        assert children[1].text == f'推送时写的：{entry["pushed"]}；新来源写的：{entry["found"]}'
        assert block.find("q")[0].text == entry["evidence"][0]["quote"]
        assert block.find("span", "ev-src")[0].find("a")
    assert not tree.find(cls="ev-lead") and not tree.find(cls="ev-foot")


@pytest.mark.parametrize("kind,expected", [
    ("both", "北京时间 10-30 09:48 · 原文日期（美国东部时间）10-29"),
    ("original", "原文日期（美国东部时间）10-29"),
    ("published", "北京时间 10-30 09:48"),
    ("neither", "页面没写日期 · 10-31 读取"),
])
def test_5_four_source_date_states_share_one_formatter(detail_data, kind, expected):
    src = source(published_at="2026-10-30 09:48:59" if kind in {"both", "published"} else None,
                 original_date_et="2026-10-29" if kind in {"both", "original"} else None, beijing_at="2026-10-31 08:49:55")
    set_sources(detail_data, [src])
    tree = parse(detail(detail_data))
    all_source_time = tree.find("div", "source-row")[0].find("div", "muted")[0].text
    deep_source_time = tree.find("div", "source")[0].find("div", "muted")[0].text
    comparison_source_time = tree.find("section", "evidence")[0].find("span", "muted")[0].text
    assert [all_source_time, deep_source_time, comparison_source_time] == [expected] * 3
    assert "09:48:59" not in tree.text


def test_5_backend_preserves_stored_et_and_derives_only_missing_et(seeded):
    conn, _ = seeded
    conn.execute("UPDATE raw_item SET published_at='2026-10-30T06:31:00Z',original_date_et='2026-10-29' WHERE id=2")
    conn.execute("UPDATE raw_item SET source_id='strictlyvc' WHERE id=2")
    conn.execute("UPDATE raw_item SET published_at='2026-10-30T06:31:00Z',original_date_et=NULL WHERE id=3")
    sources = site._sources(conn)
    assert sources[2][0]["original_date_et"] == "2026-10-29"
    assert sources[3][0]["original_date_et"] == "2026-10-30"
    assert sources[2][0]["published_at"] == sources[3][0]["published_at"] == "2026-10-30 14:31:00"


@pytest.mark.parametrize("year", [2025, 2026])
def test_6_detail_dates_have_no_weekday_but_list_group_titles_keep_it(detail_data, year):
    day = f"{year}-12-30"
    label = day if year == 2025 else "12-30"
    detail_data["events"][0].update(date=day, first_seen=day, push_date=day)
    set_sources(detail_data, [source(published_at=f"{day} 19:51:59", original_date_et=day)])
    html = detail(detail_data)
    tree = parse(html)
    assert not re.search(r"\d{2}-\d{2}\s*周[一二三四五六日]", html)
    assert not re.search(r"\d{2}-\d{2}\s*周[一二三四五六日]", tree.text)
    assert funding_section(html).find("dd")[-1].text == label
    assert tree.find("span", "ftl-when")[0].text == label
    fields = dict(zip([node.text for node in facts_grid(html).find("dt")], [node.text for node in facts_grid(html).find("dd")]))
    assert [fields[key] for key in ("日期", "首次发现", "推送日期")] == [label] * 3
    for expression in ("ui.weekHtml(d)", "ui.listHtml(d,ui.defaultFilters())"):
        groups = parse(ui(detail_data, expression)).find("h2", "dgroup-h")
        assert len(groups) == 1 and re.fullmatch(re.escape(label) + r" 周[一二三四五六日]", groups[0].text)


def test_7_missing_amount_stays_on_cards_and_funding_history(detail_data):
    detail_data["events"][0]["amount"] = W["amount_missing"]
    html = detail(detail_data)
    assert W["amount_missing"] not in facts_grid(html).text
    history = parse(html).find("span", "ftl-amt")[0]
    assert history.text == W["amount_missing"] and "is-none" in history.attrs["class"].split()
    for expression in ("ui.weekHtml(d)", "ui.listHtml(d,ui.defaultFilters())"):
        amount = parse(ui(detail_data, expression)).find("span", "co-amt-i")[0]
        assert amount.text == W["amount_missing"] and "is-none" in amount.attrs["class"].split()
