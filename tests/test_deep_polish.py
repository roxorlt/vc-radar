"""任务 I 的合成取材、轮次和 URL 回归用例。"""
from dataclasses import replace
import json

import pytest

from vcradar import deep
from vcradar.config import Config
from vcradar.deep_fetch import Materials, fetch_materials, url_key
from vcradar.http import FixtureTransport
from vcradar.llm import MockLLMClient
from vcradar.search import SearchResult, relevant_results
from test_deep_support import FIXTURE, ManualClock, database, pages, payload, point, seed


class Search:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def search_company(self, name, timeout):
        return SearchResult(True, self.rows, attempted=True)


def company(**overrides):
    return {"id": "dulo", "name": "Dulo", "website": None, "leads": [], "sources": [
        {"url": "https://news.example/dulo", "body": "Dulo builds a synthetic product.", "structured": {}}
    ], **overrides}


def materials(tmp_path, context=None, *, rows=(), manifest=None):
    transport = FixtureTransport(FIXTURE)
    transport.manifest = {"https://": {"status": 404}, **(manifest or {})}
    result = fetch_materials(context or company(), Config(state_dir=tmp_path, test_mode=True),
                             ManualClock(), transport, Search(rows))
    return result, [record["url"] for record in transport.request_log]


def test_search_relevance_boundaries_normalization_and_other_company_subject():
    rows = [
        {"title": "DULO raises a seed round", "url": "https://news.example/report"},
        {"summary": "A note about Dulo.", "url": "https://news.example/summary"},
        {"snippet": "Dulo is mentioned here.", "title": "Fluxcap raises funding",
         "url": "https://datapile.co/funding-news/fluxcap-19995"},
        {"title": "Dulo appeared in a footnote", "url": "https://news.example/fluxcap-raises-seed"},
        {"title": "Undulon raises funding", "url": "https://news.example/unrelated"},
        {"url": "https://news.example/Dulo"},
    ]
    assert {row["url"] for row in relevant_results("Dulo", rows)} == {
        "https://news.example/report", "https://news.example/summary", "https://news.example/Dulo"}
    assert relevant_results("General Compute", [{"url": "https://news.example/general-compute"}])
    assert relevant_results("General Compute", [{"title": "GENERALCOMPUTE", "url": "https://news.example/report"}])
    assert not relevant_results("Dulo", [{"url": "https://news.example/undulo"}])


def test_search_sorts_official_then_title_then_score_before_fetching_two(tmp_path):
    rows = [
        {"snippet": "Dulo funding", "url": "https://news.example/snippet", "rerank_score": 999},
        {"title": "Dulo financing", "url": "https://news.example/second", "rerank_score": 2},
        {"title": "Dulo financing", "url": "https://news.example/first", "rerank_score": 3},
        {"url": "https://dulo.example/", "rerank_score": -1},
    ]
    result, requests = materials(tmp_path, rows=rows, manifest={
        row["url"]: {"body": "<main>Dulo synthetic search material.</main>"} for row in rows})
    assert "https://dulo.example/" in requests and "https://news.example/first" in requests
    assert "https://news.example/second" not in requests and "https://news.example/snippet" not in requests
    assert result.fetch_count <= 8


@pytest.mark.parametrize(("url", "expected"), [
    ("HTTPS://WWW.News.Example/narrative/?utm_source=x&b=2&a=1#top", "https://news.example/narrative?a=1&b=2"),
    ("https://www.news.example/", "https://news.example/"),
    ("https://news.example/narrative?edition=2", "https://news.example/narrative?edition=2"),
])
def test_url_comparison_key(url, expected):
    assert url_key(url) == expected


def test_search_url_variants_fetch_once_and_fill_two_distinct_slots(tmp_path):
    rows = [{"title": "Dulo funding", "url": url} for url in (
        "https://www.news.example/narrative/", "https://news.example/narrative?utm_medium=x",
        "https://news.example/another")]
    result, requests = materials(tmp_path, rows=rows, manifest={
        "https://www.news.example/narrative/": {"body": "<article>Dulo synthetic funding.</article>"},
        "https://news.example/another": {"body": "<article>Dulo another synthetic report.</article>"},
    })
    assert sum(url_key(url) == "https://news.example/narrative" for url in requests) == 1
    assert "https://news.example/another" in requests
    assert len({url_key(page.url) for page in result.pages}) == len(result.pages)


def test_reused_source_variants_are_not_fetched_again(tmp_path):
    context = company(sources=[{"url": "https://www.news.example/narrative/", "body": "Dulo source body."}])
    result, requests = materials(tmp_path, context, rows=[{"title": "Dulo funding", "url": "https://news.example/narrative?utm_source=x"}])
    assert not any(url_key(url) == "https://news.example/narrative" for url in requests)
    assert len(result.pages) == 1


def test_reused_official_home_still_discovers_team_without_refetch(tmp_path):
    context = company(website="https://dulo.example/", sources=[{
        "url": "https://www.dulo.example/", "body": '<main>Dulo homepage.</main><a href="/team">团队</a>'}])
    result, requests = materials(tmp_path, context, manifest={
        "https://www.dulo.example/team": {"body": "<main>Dulo synthetic team.</main>"}})
    assert "https://www.dulo.example/" not in requests and "https://dulo.example/" not in requests
    assert any(page.kind == "about" for page in result.pages)


@pytest.mark.parametrize(("has_body", "length", "refetch"), [(False, 1499, True), (False, 1500, False), (True, 40, False), (False, 0, True)])
def test_short_excerpt_refetch_replaces_page_one_without_duplicates(tmp_path, has_body, length, refetch):
    source = {"url": "https://news.example/dulo", "body" if has_body else "excerpt": "x" * length}
    result, requests = materials(tmp_path, company(sources=[source]), manifest={
        source["url"]: {"body": "<article>Dulo synthetic complete source.</article>"}})
    assert (source["url"] in requests) is refetch
    assert len(result.pages) == 1 and result.pages[0].page_no == 1
    if refetch:
        assert not result.pages[0].reused and "complete source" in result.pages[0].body


def test_refetched_original_is_stored_once_and_keeps_model_page_reference(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn, body="Cedar short synthetic excerpt.")
    transport = FixtureTransport(FIXTURE)
    transport.manifest["https://news.example/cedar"] = {"body": "<article>" + pages()[0].body + "</article>"}
    output = deep.run_for_today(conn, cfg, ManualClock(), transport=transport,
                                client=MockLLMClient([payload()]), link_records=[])[0]
    assert output.status == "完成"
    stored = [dict(row) for row in conn.execute("SELECT * FROM deepdive_pages")]
    original = [page for page in stored if url_key(page["url"]) == "https://news.example/cedar"]
    assert len(original) == 1 and original[0]["page_no"] == 1
    assert output.result["产品是什么"][0]["page"] == 1
    conn.close()


def test_search_official_home_discovers_two_about_links_and_ignores_external(tmp_path):
    result, requests = materials(tmp_path, rows=[{"url": "https://dulo.example/"}], manifest={
        "https://dulo.example/": {"body": '<main>Dulo synthetic home.</main><a href="/people">People</a>'
                                  '<a href="https://elsewhere.example/team">Team</a>'
                                  '<a href="/leadership">Leadership</a><a href="/company">Company</a>'},
        "https://dulo.example/people": {"body": "<main>Dulo founders.</main>"},
        "https://dulo.example/leadership": {"body": "<main>Dulo leadership.</main>"},
    })
    assert any(page.kind == "home" and page.url == "https://dulo.example/" for page in result.pages)
    assert "https://dulo.example/people" in requests and "https://dulo.example/leadership" in requests
    assert "https://dulo.example/company" not in requests and "https://elsewhere.example/team" not in requests


def test_search_without_company_in_domain_is_not_a_home(tmp_path):
    result, requests = materials(tmp_path, rows=[{"title": "Dulo funding", "url": "https://news.example/report"}],
                                  manifest={"https://news.example/report": {"body": '<main>Dulo report.</main><a href="/team">Team</a>'}})
    assert all(page.kind != "home" for page in result.pages)
    assert "https://news.example/team" not in requests


def test_about_fallback_uses_first_robots_allowed_path_only(tmp_path):
    result, requests = materials(tmp_path, company(website="https://dulo.example/"), manifest={
        "https://dulo.example/": {"body": "<main>Dulo has no navigation.</main>"},
        "https://dulo.example/robots.txt": {"body": "User-agent: *\nDisallow: /about\nAllow: /team"},
        "https://dulo.example/team": {"body": "<main>Dulo founders.</main>"},
    })
    assert "https://dulo.example/about" not in requests and "https://dulo.example/team" in requests
    assert "https://dulo.example/company" not in requests
    assert any(page.kind == "about" for page in result.pages)


def test_yc_website_beats_source_link_and_search_home(tmp_path):
    context = company(website="https://dulo-search.example/", yc_batch="Fall 2026", sources=[{
        "url": "https://www.ycombinator.com/companies/dulo", "source_id": "yc_directory",
        "body": '<main>Dulo synthetic YC excerpt.</main><a href="https://dulo-link.example/">Dulo</a>'}])
    result, requests = materials(tmp_path, context, rows=[{"url": "https://dulo-search.example/"}], manifest={
        "https://www.ycombinator.com/companies/dulo": {"body": '<main>Dulo synthetic YC profile.</main>'
            '<script type="application/json">{"company":{"website":"https://dulo-yc.example/"}}</script>'},
        "https://dulo-yc.example/": {"body": "<main>Dulo real synthetic homepage.</main>"},
    })
    assert "https://dulo-yc.example/" in requests
    assert "https://dulo-link.example/" not in requests and "https://dulo-search.example/" not in requests
    assert len({url_key(page.url) for page in result.pages}) == len(result.pages)


def test_source_external_home_beats_search_home(tmp_path):
    context = company(sources=[{"url": "https://news.example/dulo", "body":
                                '<main>Dulo report.</main><a href="https://dulo-source.example/">Website</a>'}])
    _, requests = materials(tmp_path, context, rows=[{"url": "https://dulo-search.example/"}], manifest={
        "https://dulo-source.example/": {"body": "<main>Dulo source-linked home.</main>"}})
    assert "https://dulo-source.example/" in requests
    assert "https://dulo-search.example/" not in requests


@pytest.mark.parametrize("quote", [
    "In September 2025, Foundation Capital led the $9 million seed round.",
    "Foundation Capital led the previous seed round of $9 million.",
    "Dulo raised $9 million in a Series A round on 2026-01-01.",
    "Dulo raised $9 million in a Series A round on 1 January 2026.",
    "Dulo raised $9 million in another Series A round.",
])
def test_historical_funding_is_not_new_evidence(quote):
    judgement = {"round": "A 轮", "amount": "$45M", "event_date": "2026-10-30"}
    facts = {"amount": {"value": "$9 million", "quote": quote, "url": "https://news.example/hyphenate", "page": 1}}
    assert deep.find_new_evidence(judgement, facts, "2026-10-31T09:00:00+08:00") == []


def test_current_round_mismatch_survives_later_historical_points():
    current = "Hyphenate raised $46 million in a Series A round."
    historical = "In September 2025, Foundation Capital led the $9 million seed round."
    raw = payload()
    raw["融资"] = [point("本轮融资 $46 million。", current, field="amount", value="$46 million"),
                   point("本轮 Series A。", current, field="round", value="Series A"),
                   point("此前融资 $9 million。", historical, field="amount", value="$9 million"),
                   point("此前 seed。", historical, field="round", value="seed")]
    checked = deep.validate_result(raw, pages(pages()[0].body + " " + current + " " + historical))
    found = deep.find_new_evidence({"round": "A 轮", "amount": "$45M", "event_date": "2026-10-30"}, checked.facts,
                                  "2026-10-31T09:00:00+08:00")
    assert [item["field"] for item in found if item["field"] in {"轮次", "金额"}] == ["金额"]
    assert next(item for item in found if item["field"] == "金额")["value"] == "$46 million"


def test_last_week_does_not_make_current_round_historical():
    quote = "Dulo raised $46 million in a Series A round last week."
    found = deep.find_new_evidence({"round": "A 轮", "amount": "$45M", "event_date": "2026-10-30"},
                                  {"amount": {"value": "$46 million", "quote": quote, "page": 1, "url": "https://news.example/dulo"}},
                                  "2026-10-31T09:00:00+08:00")
    assert [item["field"] for item in found] == ["金额"]


def test_storage_also_deduplicates_url_keys_and_remaps_references(tmp_path):
    conn, cfg = database(tmp_path)
    seed(conn)
    clock = ManualClock()
    context = deep.select_companies(conn, clock.now().date(), now=clock.now())[0]
    assert deep._claim(conn, context, "2026-10-31", clock)
    original = pages()[0]
    duplicate = replace(original, page_no=2, url="https://www.news.example/cedar/?utm_source=x")
    output = deep.DeepResult("cedar", "Cedar Labs", "完成", clock.now().isoformat(), clock.now().isoformat(),
                             materials=Materials(pages=[original, duplicate]),
                             result={"产品是什么": [{"page": 2, "url": duplicate.url}], "全部来源": []},
                             run_token=context["run_token"])
    assert deep._save(conn, output, "2026-10-31")
    assert conn.execute("SELECT COUNT(*) FROM deepdive_pages").fetchone()[0] == 1
    result = json.loads(conn.execute("SELECT result_json FROM deepdive").fetchone()[0])
    assert result["产品是什么"][0]["page"] == 1 and len(result["全部来源"]) == 1
    conn.close()
