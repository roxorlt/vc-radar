import gzip
import io
import json
import socket
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from email.message import Message

import pytest

from test_sources_support import NOW, SOURCES, transport
from vcradar.config import BJ, UA
from vcradar.http import FixtureTransport, UrllibTransport, redact_keys, save_raw_response
from vcradar.models import HttpResponse
from vcradar.robots import RobotsChecker


class Reply(io.BytesIO):
    def __init__(self, payload, code=200, content_type="application/rss+xml", encoding=""):
        super().__init__(payload)
        self.code = code
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def geturl(self):
        return "https://example.invalid/final"


def test_urllib_fixed_ua_post_gzip_and_redacted_log(monkeypatch):
    seen = []
    def open_mock(request, timeout):
        seen.append((request, timeout))
        return Reply(gzip.compress("合成响应".encode()), content_type="application/json; charset=utf-8", encoding="gzip")
    monkeypatch.setattr("urllib.request.urlopen", open_mock)
    client = UrllibTransport()
    result = client.get("https://example.invalid/query", {"uSeR-aGeNt": "ignored", "X-Algolia-API-Key": "synthetic-private-value"}, data=b'{"query":"test"}', timeout=7)
    request, timeout = seen[0]
    assert request.get_header("User-agent") == UA
    assert request.get_method() == "POST" and timeout == 7
    assert result.body == "合成响应" and result.status == 200
    assert result.final_url == "https://example.invalid/final"
    assert client.request_log[0]["headers"]["X-Algolia-API-Key"] == "<key>"
    assert "synthetic-private-value" not in json.dumps(client.request_log)
    forbidden = ["anthropic-ai", "Claude-Web", "ClaudeBot", "ChatGPT-User", "GPTBot", "CCBot", "Google-Extended", "Applebot-Extended", "Bytespider", "cohere-ai", "Diffbot", "FacebookBot", "omgili", "Amazonbot"]
    assert not any(name.lower() in UA.lower() for name in forbidden)


@pytest.mark.parametrize("code", [404, 429, 503])
def test_urllib_http_errors_keep_status_and_body(monkeypatch, code):
    headers = Message()
    headers["Content-Type"] = "text/plain; charset=utf-8"
    def open_mock(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.invalid", code, "synthetic error", headers, io.BytesIO(b"synthetic error body"))
    monkeypatch.setattr("urllib.request.urlopen", open_mock)
    result = UrllibTransport().get("https://example.invalid")
    assert result.status == code and result.body == "synthetic error body"
    assert result.error is None


@pytest.mark.parametrize("exception,error", [(TimeoutError(), "timeout"), (socket.timeout(), "timeout"), (urllib.error.URLError(socket.timeout()), "timeout"), (urllib.error.URLError("synthetic connection failure"), "conn:unreachable")])
def test_urllib_connection_errors(monkeypatch, exception, error):
    def open_mock(*args, **kwargs):
        raise exception
    monkeypatch.setattr("urllib.request.urlopen", open_mock)
    result = UrllibTransport().get("https://example.invalid")
    assert result.status is None and result.error == error


def test_urllib_raw_capture_redacts_directory_configuration(monkeypatch, tmp_path):
    payload = b'<script>window.AlgoliaOpts = {"app":"SYNTHETIC","key":"synthetic-secret"};</script>'
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Reply(payload, content_type="text/html"))
    client = UrllibTransport(tmp_path)
    assert "synthetic-secret" in client.get(SOURCES["yc_directory"].url).body
    files = list(tmp_path.glob("*/yc_directory.out"))
    assert len(files) == 1
    assert "synthetic-secret" not in files[0].read_text()
    assert '"key":"<key>"' in files[0].read_text()


def test_raw_beijing_date_and_numbered_pages(tmp_path):
    now = datetime.fromisoformat("2026-10-29T20:00:00+00:00")
    result = HttpResponse("x", 200, body='{"key":"fictional-secret"}')
    save_raw_response(result, tmp_path, "yc_directory", 1, now)
    save_raw_response(result, tmp_path, "yc_directory", 2, now)
    assert sorted(p.name for p in (tmp_path / "2026-10-30").iterdir()) == ["yc_directory-p2.out", "yc_directory.out"]
    assert "fictional-secret" not in (tmp_path / "2026-10-30" / "yc_directory.out").read_text()
    save_raw_response(result, None, "yc_directory", 1, now)


@pytest.mark.parametrize("body", ['{"key":"synthetic"}', "window.AlgoliaOpts={key:'synthetic'};", '{"api_key": "synthetic"}'])
def test_key_redaction(body):
    assert "synthetic" not in redact_keys(body)
    assert "<key>" in redact_keys(body)


def test_fixture_sequence_longest_prefix_missing_and_delay(tmp_path, monkeypatch):
    directory = tmp_path / "fixture"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({
        "https://example.invalid": {"status": 404},
        "https://example.invalid/exact": {"content_type": "text/plain", "sequence": [{"status":403}, {"status":200,"body":"synthetic","delay_s":0.2}]}
    }))
    sleeps = []
    monkeypatch.setattr("vcradar.http.time.sleep", sleeps.append)
    client = FixtureTransport(directory)
    assert client.get("https://example.invalid/exact").status == 403
    assert client.get("https://example.invalid/exact").body == "synthetic"
    assert client.get("https://example.invalid/exact").status == 200
    assert sleeps == [0.2, 0.2]
    assert client.get("https://example.invalid/other").status == 404
    assert client.get("https://unknown.invalid").error == "conn:no fixture"
    assert all(entry["headers"]["User-Agent"] == UA for entry in client.request_log)


def test_fixture_delay_obeys_request_timeout(tmp_path, monkeypatch):
    client = transport(tmp_path, {"https://example.invalid/slow": {"delay_s":31}})
    sleeps = []
    monkeypatch.setattr("vcradar.http.time.sleep", sleeps.append)
    response = client.get("https://example.invalid/slow", timeout=0.01)
    assert sleeps == [0.01]
    assert response.status is None and response.error == "timeout"


@pytest.mark.parametrize("code", [400, 401, 403, 404, 429, 451])
def test_robots_all_4xx_allow(tmp_path, code):
    client = transport(tmp_path, {"https://example.invalid/robots.txt": {"status":code, "body":"User-agent: *\nDisallow: /"}})
    checker = RobotsChecker(client, tmp_path / "cache", NOW.date())
    assert checker.allowed("https://example.invalid/any")


@pytest.mark.parametrize("spec", [{"status":500}, {"status":503}, {"status":None,"error":"timeout"}, {"status":None,"error":"conn:offline"}])
def test_robots_5xx_timeout_connection_forbid(tmp_path, spec):
    client = transport(tmp_path, {"https://example.invalid/robots.txt": spec})
    checker = RobotsChecker(client, tmp_path / "cache", NOW.date())
    assert not checker.allowed("https://example.invalid/any")
    assert not checker.allowed("https://example.invalid/another")
    assert len(client.request_log) == 1


def test_robots_concurrent_and_disk_cache_once_per_host_day(tmp_path):
    client = transport(tmp_path, {"https://example.invalid/robots.txt": {"body":"User-agent: *\nDisallow: /private\nCrawl-delay: 2"}})
    cache = tmp_path / "cache"
    checker = RobotsChecker(client, cache, date(2026, 10, 30))
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(checker.allowed, ["https://example.invalid/public"] * 12))
    assert not checker.allowed("https://example.invalid/private")
    assert checker.crawl_delay("https://example.invalid/public") == 2
    again = RobotsChecker(client, cache, date(2026, 10, 30))
    assert again.allowed("https://example.invalid/public")
    assert len(client.request_log) == 1
    assert (cache / "robots/2026-10-30/example.invalid.txt").exists()
    assert json.loads((cache / "robots/2026-10-30/example.invalid.json").read_text())["status"] == 200
    tomorrow = RobotsChecker(client, cache, date(2026, 10, 31))
    assert tomorrow.allowed("https://example.invalid/public")
    assert len(client.request_log) == 2


def test_robots_exception_does_not_request_robots(tmp_path):
    client = transport(tmp_path)
    checker = RobotsChecker(client, tmp_path / "cache", NOW.date())
    assert checker.allowed("https://content.fortune.com/newsletter/termsheet/?post_id=701", exception=True)
    assert client.request_log == []


def test_robots_crawl_delay_is_applied_between_requests(tmp_path):
    client = transport(tmp_path, {"https://example.invalid/robots.txt": {"body":"User-agent: *\nCrawl-delay: 2\n"}})
    checker = RobotsChecker(client, tmp_path / "cache", NOW.date())
    sleeps, sent = [], []
    for number in range(2):
        checker.request("https://example.invalid/page", lambda: sent.append(number), sleeps.append)
    assert sent == [0, 1]
    assert len(sleeps) == 1 and 1.9 < sleeps[0] <= 2


def test_robots_query_wildcard_allows_only_bare_directory(tmp_path):
    client = transport(tmp_path)
    checker = RobotsChecker(client, tmp_path / "cache", NOW.date())
    assert checker.allowed("https://www.ycombinator.com/companies")
    assert not checker.allowed("https://www.ycombinator.com/companies?batch=Fall+2026")


def test_same_host_requests_can_overlap_without_crawl_delay(tmp_path):
    from threading import Barrier
    client = transport(tmp_path, {"https://example.invalid/robots.txt":{"status":404}})
    checker = RobotsChecker(client, tmp_path / "cache", NOW.date())
    barrier = Barrier(2, timeout=1)
    def send():
        barrier.wait()
        return "ok"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:checker.request("https://example.invalid/page",send,lambda _:None), range(2)))
    assert results == ["ok", "ok"]
