from datetime import datetime
import json
import time

import pytest

from vcradar.config import Config, UA
from vcradar.deep_fetch import Budget, clean_page, fetch_materials, fetchable_url, normalize_url
from vcradar.http import FixtureTransport
from vcradar.search import ENDPOINT, SearchClient
from test_deep_support import FIXTURE, ManualClock


def context(**overrides):
    return {"id": "cedar", "name": "Cedar Labs", "website": "https://cedar.example/", "one_liner": "Developer runtime",
            "leads": ["Maple Ventures"], "sources": [{"url": "https://news.example/cedar", "source_id": "news",
             "body": (FIXTURE / "source.html").read_text(), "structured": {"website": "https://cedar.example/"}}], **overrides}


def fetch(tmp_path, transport=None, company=None, clock=None):
    transport = transport or FixtureTransport(FIXTURE)
    cfg = Config(state_dir=tmp_path, test_mode=True)
    return fetch_materials(company or context(), cfg, clock or ManualClock(), transport, SearchClient(transport, cfg)), transport


def test_materials_reuse_primary_discover_about_hn_api_and_no_live_fetch(tmp_path):
    result, transport = fetch(tmp_path)
    urls = [entry["url"] for entry in transport.request_log]
    assert result.pages[0].reused and result.pages[0].page_no == 1
    assert "https://news.example/cedar" not in urls
    assert "https://cedar.example/about" in urls
    assert "https://hn.algolia.com/api/v1/items/123456" in urls
    assert not any(url.startswith("https://news.ycombinator.com") for url in urls)
    assert not any("linkedin.com" in url for url in urls)
    assert result.search.ok and result.search.cost == 0.005
    assert result.fetch_count <= 8 and len(result.pages) <= 9
    assert all(len(page.body) <= 8000 for page in result.pages)
    assert all(entry["headers"]["User-Agent"] == UA for entry in transport.request_log)
    home = next(page for page in result.pages if page.kind == "home")
    assert "Registered office: Boston" in home.body and home.footer
    assert any(link["text"] == "Mira Example" for link in home.links)
    hn = next(page for page in result.pages if page.kind == "hn")
    assert "handwritten question" in hn.body and "<p>" not in hn.body
    assert (tmp_path / "test-cache" / "robots" / "2026-10-31").exists()
    assert not (tmp_path / "robots").exists()


@pytest.mark.parametrize(("status", "allowed"), [(400, True), (403, True), (404, True), (429, True), (500, False), (503, False), (None, False)])
def test_robots_4xx_allows_and_5xx_or_timeout_denies_for_the_day(tmp_path, status, allowed):
    transport = FixtureTransport(FIXTURE)
    transport.manifest["https://cedar.example/robots.txt"] = {"status": status, "error": "timeout" if status is None else None}
    result, _ = fetch(tmp_path, transport)
    requests = [item["url"] for item in transport.request_log]
    assert ("https://cedar.example/" in requests) is allowed
    assert ("https://cedar.example/news/seed" in requests) is allowed
    assert requests.count("https://cedar.example/robots.txt") == 1
    assert result.pages[0].reused
    second = FixtureTransport(FIXTURE)
    fetch(tmp_path, second)
    assert "https://cedar.example/robots.txt" not in [item["url"] for item in second.request_log]


def test_robots_disallow_is_obeyed_without_term_sheet_exception(tmp_path):
    transport = FixtureTransport(FIXTURE)
    transport.manifest["https://cedar.example/robots.txt"] = {"status": 200, "body": "User-agent: *\nDisallow: /"}
    result, _ = fetch(tmp_path, transport)
    assert not any(item["url"] == "https://cedar.example/" for item in transport.request_log)
    assert any(record.get("error") == "robots" for record in result.records)


def test_crawl_delay_that_exceeds_budget_is_skipped_without_sleep(tmp_path):
    transport = FixtureTransport(FIXTURE)
    transport.manifest["https://cedar.example/robots.txt"] = {"status": 200, "body": "User-agent: *\nAllow: /\nCrawl-delay: 100"}
    started = time.monotonic()
    result, _ = fetch(tmp_path, transport)
    assert time.monotonic() - started < 2
    cedar_requests = [item for item in transport.request_log if item["url"].startswith("https://cedar.example/") and not item["url"].endswith("robots.txt")]
    assert len(cedar_requests) == 1
    assert any(record.get("error") == "timeout_or_unreadable" for record in result.records)


@pytest.mark.parametrize("blocked", [
    {"status": 403}, {"status": 429}, {"status": 200, "body": "<title>Verify you are human</title>"},
    {"status": 200, "body": "<p>Subscription required</p>"},
    {"status": 200, "body": "<p>Sign in to continue</p>"},
])
def test_home_blocked_pages_are_never_retried(tmp_path, blocked):
    transport = FixtureTransport(FIXTURE)
    transport.manifest["https://cedar.example/"] = blocked
    fetch(tmp_path, transport)
    assert sum(item["url"] == "https://cedar.example/" for item in transport.request_log) == 1


def test_home_and_press_transient_failure_retry_once_only(tmp_path):
    transport = FixtureTransport(FIXTURE)
    for url in ("https://cedar.example/", "https://cedar.example/news/seed"):
        transport.manifest[url] = {"sequence": [{"status": 500}, {"status": 200, "body": "<main>Handwritten successful retry.</main>"}]}
    result, _ = fetch(tmp_path, transport)
    for url in ("https://cedar.example/", "https://cedar.example/news/seed"):
        assert sum(item["url"] == url for item in transport.request_log) == 2
    assert any("successful retry" in page.body for page in result.pages)


def test_search_results_at_most_two_and_total_pages_at_most_eight(tmp_path):
    transport = FixtureTransport(FIXTURE)
    transport.manifest[ENDPOINT] = {"body": json.dumps({"results": [{"url": f"https://other.example/{i}"} for i in range(5)]})}
    transport.manifest["https://other.example/"] = {"body": "<main>A handwritten search page.</main>"}
    result, _ = fetch(tmp_path, transport)
    assert len([page for page in result.pages if page.kind == "search"]) <= 2
    assert result.fetch_count <= 8
    assert not any(item["url"] == "https://other.example/2" for item in transport.request_log)


def test_non_developer_company_does_not_fetch_github(tmp_path):
    company = context(one_liner="Gas turbines", sources=[{"url": "https://news.example/cedar", "body":
        '<p>Cedar Labs makes gas turbines.</p><a href="https://github.com/cedar-example/actors">Repository</a>'}])
    _, transport = fetch(tmp_path, company=company)
    assert not any("github.com" in item["url"] for item in transport.request_log)


def test_yc_company_page_is_mandatory(tmp_path):
    company = context(yc_batch="Fall 2026")
    company["sources"][0]["url"] = "https://www.ycombinator.com/companies/cedar-labs"
    company["sources"][0]["source_id"] = "yc_directory"
    transport = FixtureTransport(FIXTURE)
    transport.manifest["https://www.ycombinator.com/companies/cedar-labs"] = {"body": "<main>Full synthetic YC company page.</main>"}
    result, _ = fetch(tmp_path, transport, company)
    assert any(page.kind == "yc" for page in result.pages)
    assert sum(item["url"] == company["sources"][0]["url"] for item in transport.request_log) == 1


def test_cleaning_limits_body_preserves_footer_and_ignores_instructions_in_scripts():
    text, links, footer, websites = clean_page(
        '<nav>Navigation noise</nav><article>' + "A" * 10000 + '</article><footer>Boston address</footer>'
        '<script>{"website":"https://cedar.example/"}</script>', "https://www.ycombinator.com/companies/cedar")
    assert len(text) <= 8000 and "Boston address" in text and footer == "Boston address"
    assert "Navigation noise" not in text and "website" not in text
    assert websites[0]["url"] == "https://cedar.example/"


@pytest.mark.parametrize(("url", "expected"), [
    ("https://cedar.example/path?utm_source=x&a=1#s", "https://cedar.example/path?a=1"),
    ("https://cedar.example/path?fbclid=x&mc_eid=x&q=keep", "https://cedar.example/path?q=keep"),
    ("javascript:alert(1)", ""), ("file:///tmp/example", ""),
    ("https://user:password@cedar.example/path", ""), ("https://cedar.example:bad/", ""),
])
def test_url_normalization(url, expected):
    assert normalize_url(url) == expected


@pytest.mark.parametrize("url", ["https://linkedin.com/in/name", "https://www.crunchbase.com/organization/example", "https://pitchbook.com/company/x", "https://tracxn.com/x", "https://x.com/a", "https://cedar.example/sign-in"])
def test_fetch_disallows_private_or_paid_pages(url):
    assert not fetchable_url(url)


def test_fetch_budget_also_bounds_robots_request(tmp_path):
    class RecordingTransport(FixtureTransport):
        def get(self, url, **kwargs):
            self.timeouts.append((url, kwargs["timeout"]))
            return super().get(url, **kwargs)

    transport = RecordingTransport(FIXTURE)
    transport.timeouts = []
    fetch(tmp_path, transport)
    assert all(timeout <= 15 for _, timeout in transport.timeouts)
    assert any(url.endswith("robots.txt") for url, _ in transport.timeouts)


def test_slow_home_does_not_let_search_take_about_page_slot(tmp_path):
    transport = FixtureTransport(FIXTURE)
    transport.manifest["https://cedar.example/"]["delay_s"] = 0.03
    company = context(yc_batch="Fall 2026")
    company["sources"][0]["body"] += '<a href="https://cedar.example/news/extra">Second release</a>'
    transport.manifest[ENDPOINT] = {"body": json.dumps({"results": [{"url": f"https://other.example/{i}"} for i in range(5)]})}
    transport.manifest["https://other.example/"] = {"body": "<main>Synthetic search evidence.</main>"}
    result, _ = fetch(tmp_path, transport, company)
    assert result.fetch_count <= 8
    assert any(page.url == "https://cedar.example/about" for page in result.pages)


def test_yc_website_field_can_discover_the_homepage(tmp_path):
    transport = FixtureTransport(FIXTURE)
    company = context(website=None, yc_batch="Fall 2026")
    company["sources"][0]["url"] = "https://www.ycombinator.com/companies/cedar-labs"
    transport.manifest[company["sources"][0]["url"]] = {"body":
        '<main>Synthetic YC founders.</main><script type="application/json">{"company":{"website":"https://cedar.example/"}}</script>'}
    result, _ = fetch(tmp_path, transport, company)
    assert any(page.kind == "home" and page.url == "https://cedar.example/" for page in result.pages)


def test_github_uses_readme_stars_license_without_issue_list():
    body = '<main><div>Issue: Unrelated Person announces imaginary funding.</div><article>README: Handwritten runtime.</article><div>12 stars</div><div>MIT license</div></main>'
    text, _, _, _ = clean_page(body, "https://github.com/cedar-example/actors", github=True)
    assert "README: Handwritten runtime." in text and "12 stars" in text and "MIT license" in text
    assert "Unrelated Person" not in text


def test_fetch_total_budget_returns_without_waiting_for_slow_pages(tmp_path, monkeypatch):
    import vcradar.deep_fetch as module
    real_budget = module.Budget
    monkeypatch.setattr(module, "Budget", lambda clock, seconds, parent=None: real_budget(clock, min(seconds, 0.03), parent))
    transport = FixtureTransport(FIXTURE)
    for url, spec in transport.manifest.items():
        if url != ENDPOINT:
            spec["delay_s"] = 1
    started = time.monotonic()
    result, _ = fetch(tmp_path, transport)
    assert time.monotonic() - started < 0.3
    assert result.pages[0].reused and not any(page.kind == "home" for page in result.pages)
