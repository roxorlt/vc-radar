"""无链接周报与原有带链接格式的离线回归。"""
from dataclasses import replace
from datetime import date
import json

import pytest

from test_er_render import BASE, card, digest
from vcradar import render
from vcradar.models import PendingLine


@pytest.mark.parametrize("status", ["有新增", "部分失败", "全部失败", "没跑完"])
@pytest.mark.parametrize("has_cards", [False, True])
@pytest.mark.parametrize("since_last_push", [False, True])
def test_no_links_only_removes_navigation_and_unlinks_evidence(digest, tmp_path, status, has_cards, since_last_push):
    digest.status = status
    digest.cards = [card()] if has_cards else []
    digest.new_evidence_count = 3
    digest.baseline_count = 2
    digest.round_pending = [PendingLine("dream", "Dream", "$17M", "来源没写轮次")]
    digest.filtered = {"轮次不在范围": 1}
    digest.yc_new_batch = ("Fall 2026", 36)
    digest.last_push_summary = (date(2026, 10, 23), 2)
    links = tmp_path / "links.json"
    links.write_text(json.dumps([
        {"type": "公司", "company": "Aster", "verified": True, "url": "https://aster.example/"},
    ]))
    options = dict(early_push=True, failed_days=1, since_last_push=since_last_push)
    linked = render.render_push(digest, links_file=links, **options)
    text = render.render_push(replace(digest, base_url=""), links_file=links, **options)
    footer = f"[看本周全部]({BASE}/#week)"
    evidence = f"{'上次推送后' if since_last_push else '本周'} 3 家资料有出入"
    expected = "\n".join(line for line in linked.splitlines() if line != footer)
    expected = expected.replace(f"[{evidence}]({BASE}/#sources)", evidence)
    expected = expected.replace(f"YC 新一批 36 家，[名单见网页]({BASE}/#yc)", "YC 新一批 36 家")
    assert text == expected
    assert BASE not in text and "详情" not in text and "看本周全部" not in text
    if has_cards:
        assert "[官网](https://aster.example/) [来源](https://news.example.invalid/aster)" in text
    else:
        assert "](" not in text
    assert "\n\n" not in text and not text.endswith("\n")
    assert evidence not in text and "YC 新一批 36 家" in text
    assert "名单见网页" not in text and "在往期页看" not in text
    assert render.remove_page_links(linked) == text
    assert digest.base_url == BASE


def test_no_new_without_hints_keeps_status_and_last_summary(digest):
    digest.last_push_summary = (date(2026, 10, 23), 2)
    text = render.render_push(replace(digest, base_url=""))
    lines = text.splitlines()
    assert len(lines) == 2 and lines[-1] == "本周没有发现新的天使轮、种子轮、A 轮融资。"
    assert "已接入" not in text and "上一次有新公司" not in text
    assert render.push_first_lines(text) == (lines[1], "")
    assert render.add_resend_notice(text) == text


def test_card_without_base_url_omits_details_and_preserves_source_links():
    company = card()
    linked = render.render_card(company, BASE)
    text = render.render_card(company, "")
    assert text == linked.removesuffix(f"　[详情]({BASE}/#c/aster)")
    assert "详情" not in text and len(text.splitlines()) == 3
    assert "[TechCrunch · 10-22](https://news.example.invalid/aster)" in text


def test_nonempty_base_url_keeps_navigation_evidence_and_details(digest):
    digest.base_url = "https://review.example/radar/"
    digest.cards = [card()]
    digest.new_evidence_count = 2
    text = render.render_push(digest)
    assert "资料有出入" not in text and "#sources" not in text
    assert text.splitlines()[-1] == "[看本周全部](https://review.example/radar/#week)"
    assert render.render_card(digest.cards[0], digest.base_url).endswith(
        "　[详情](https://review.example/radar/#c/aster)")
