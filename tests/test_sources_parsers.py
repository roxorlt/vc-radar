import json
from datetime import datetime

import pytest

from test_sources_support import FIXTURES, SOURCES, response
from vcradar.config import BJ, UTC
from vcradar.models import ErrorType, HttpResponse
from vcradar.sources import alleywatch, crunchbase, launch_hn, strictlyvc, techcrunch, termsheet, yc_directory, yc_launches
from vcradar.sources.common import html_lines, strip_html
from vcradar.sources.registry import P0_SOURCES, sources_by_name


def test_registry_contract():
    assert len(P0_SOURCES) == len({src.id for src in P0_SOURCES}) == 11
    grouped = sources_by_name()
    assert len(grouped) == 8
    assert list(grouped)[0] == "Term Sheet"
    assert len(grouped["TechCrunch"]) == 2
    assert len(grouped["Crunchbase News"]) == 3
    assert [src.id for src in P0_SOURCES if src.robots_exception] == ["ts_web"]
    assert SOURCES["yc_directory"].stale_days is None
    assert SOURCES["launch_hn"].stale_days == 30


@pytest.mark.parametrize("parser,source_id", [(techcrunch.parse, "tc_venture"), (crunchbase.parse, "cbn_main")])
def test_rss_fields_and_fallback(parser, source_id):
    parsed = parser(response("feed.xml", "application/rss+xml"), source_id)
    assert parsed.error_type is None
    first, second = parsed.items
    assert first.external_id == "synthetic-terse"
    assert first.excerpt == "Terse builds shared rooms for small teams."
    assert "seed funding" in first.body and "<p>" not in first.body
    assert second.external_id == second.url
    assert second.body == second.excerpt
    assert parsed.latest_at == datetime(2026, 10, 29, 21, 30, tzinfo=UTC)


def test_rss_limits_are_after_html_cleanup():
    body = "<rss xmlns:content='urn:content'><channel><item><link>https://example.invalid/one</link><description><![CDATA[<p>" + "长" * 1500 + "</p>]]></description><content:encoded><![CDATA[<p>" + "文" * 9000 + "</p>]]></content:encoded></item></channel></rss>"
    parsed = techcrunch.parse(HttpResponse("x", 200, "text/xml", body))
    assert len(parsed.items[0].excerpt) == 1000
    assert len(parsed.items[0].body) == 8000


@pytest.mark.parametrize("body,kind", [("<html></html>", "text/html"), ("broken", "text/xml"), ("<html/>", "text/xml")])
def test_rss_rejects_interceptions(body, kind):
    assert techcrunch.parse(HttpResponse("x", 200, kind, body)).error_type == ErrorType.INTERCEPTED


def test_alleywatch_aggregate_posts_are_counted_but_skipped():
    parsed = alleywatch.parse(response("alleywatch.xml", "application/rss+xml"))
    assert len(parsed.items) == 5
    assert [item.structured["kind"] for item in parsed.items] == ["single", "daily", "weekly", "monthly", "daily"]
    assert "skip_reason" not in parsed.items[0].structured
    assert all(item.structured["skip_reason"] for item in parsed.items[1:])


def test_yc_launches_normal():
    parsed = yc_launches.parse(response("launches.json", "application/json"))
    item, = parsed.items
    assert item.external_id == "501"
    assert item.url == "https://www.ycombinator.com/launches/synthetic-terse"
    assert item.structured == {"company_name": "Terse", "website": "https://example.invalid", "yc_batch": "Fall 2026", "tags": ["Collaboration", "AI"], "one_liner": "Shared rooms for fictional teams"}


def test_yc_directory_options_and_batches():
    options = yc_directory.options(response("directory.html"))
    assert options["app"] == "SYNTHETIC"
    assert yc_directory.options(HttpResponse("x", 200, "text/html", "<html/>")) is None
    assert yc_directory.latest_batches(response("facets.json", "application/json")) == ["Fall 2026", "Summer 2026", "Spring 2026", "Winter 2026"]
    assert sorted(["S24", "Winter 2026", "W24", "Fall 2025"], key=yc_directory.batch_order) == ["W24", "S24", "Fall 2025", "Winter 2026"]


def test_yc_directory_normal():
    parsed = yc_directory.parse(response("directory.json", "application/json"))
    item, = parsed.items
    assert item.published_at is None
    assert item.external_id == "terse-501"
    assert item.structured["location"] == "New York, NY, USA"
    assert item.structured["team_size"] == 4
    assert yc_directory.total(response("directory.json", "application/json")) == 5


def test_launch_hn_normal():
    item, = launch_hn.parse(response("hn.json", "application/json")).items
    assert item.external_id == "901"
    assert item.url == "https://news.ycombinator.com/item?id=901"
    assert item.body == "Build shared rooms together."
    assert item.structured == {"company_name": "Terse", "yc_batch": "F26", "one_liner": "Shared rooms for teams"}


@pytest.mark.parametrize("parser", [launch_hn.parse, yc_launches.parse, yc_directory.parse])
def test_json_parsers_reject_invalid_shape(parser):
    assert parser(HttpResponse("x", 200, "application/json", "{}" )).error_type is not None


def test_strictlyvc_four_sections_ads_scripts_dates_and_links():
    url = "https://newsletter.strictlyvc.com/p/synthetic-issue"
    refs = strictlyvc.issues(response("strictlyvc-sitemap.xml", "application/xml", SOURCES["strictlyvc"].url))
    assert [ref.url for ref in refs] == [url]
    parsed = strictlyvc.parse(response("strictlyvc.html", url=url))
    assert parsed.error_type is None
    assert [item.structured["company_name"] for item in parsed.items] == ["Terse", "Dream", "Mintive", "Viraj Aero"]
    first = parsed.items[0]
    assert first.external_id == url + "#Terse"
    assert first.url == url
    assert first.original_date_et == "2026-10-29"
    assert first.published_at.astimezone(BJ).date().isoformat() == "2026-10-30"
    assert first.structured["hq_text"] == "San Francisco"
    assert first.structured["round_text"] == "Series A"
    assert first.structured["amount_text"] == "$80 million"
    assert first.structured["leads"] == ["Example Capital", "Room Ventures"]
    assert first.structured["original_link"] == "https://example.invalid/terse-report"
    assert parsed.items[1].structured["hq_text"] == "New York"
    assert parsed.items[3].structured["amount_text"] is None


def test_strictlyvc_unknown_fundings_heading_only_logs(caplog):
    content = (FIXTURES / "strictlyvc.html").read_text().replace("Smaller Fundings", "Tiny Fundings")
    with caplog.at_level("INFO"):
        parsed = strictlyvc.parse(HttpResponse("x", 200, "text/html", content))
    assert len(parsed.items) == 4
    assert "Tiny Fundings" in caplog.text


@pytest.mark.parametrize("parser", [strictlyvc.parse, termsheet.parse])
def test_newsletters_no_deals(parser):
    assert parser(response("empty-newsletter.html")).error_type == ErrorType.NEWSLETTER_NO_DEALS


@pytest.mark.parametrize("file", ["termsheet.html", "termsheet-fallback.html"])
def test_term_sheet_deals_and_fallback(file):
    published = datetime(2026, 10, 29, 21, 30, tzinfo=UTC)
    refs = termsheet.issues(response("termsheet-list.json", "application/json"))
    assert refs[0].url.endswith("?post_id=701")
    assert refs[0].at == published
    parsed = termsheet.parse(response(file, url=refs[0].url), published_at=published)
    assert parsed.error_type is None
    first = parsed.items[0]
    assert first.structured["company_name"] == "Terse"
    assert first.structured["hq_text"] == "New York"
    assert first.structured["leads"] == ["Example Ventures"]
    assert first.structured["amount_text"] == "$8 million"
    assert not any(item.structured["company_name"] == "Buyout Labs" for item in parsed.items)
    if file == "termsheet.html":
        assert first.structured["follows"] == ["Sample Capital"]


def test_html_cleanup_ignores_json_and_does_not_join_block_words():
    assert strip_html("<p>One</p><p>Two</p><script>{'text':'Third'}</script>") == "One Two"
    assert [line.text for line in html_lines("<p>A &amp; B<br>C</p>")] == ["A & B", "C"]


def test_newsletter_round_uses_financing_clause_instead_of_product_description():
    content = '<h2>VENTURE CAPITAL</h2><p>Terse, a New York-based seed planning company, raised $8 million in Series A funding.</p>'
    parsed = termsheet.parse(HttpResponse("x", 200, "text/html", content))
    assert parsed.items[0].structured["round_text"] == "Series A"


def test_json_mime_type_cannot_be_smuggled_in_html_parameter():
    value = response("launches.json", "text/html; json=true")
    assert yc_launches.parse(value).error_type == ErrorType.INTERCEPTED
