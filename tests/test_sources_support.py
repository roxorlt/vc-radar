"""离线测试辅助：所有响应均来自手写合成数据。"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vcradar.config import BJ
from vcradar.http import FixtureTransport
from vcradar.models import HttpResponse, SourceState
from vcradar.robots import RobotsChecker
from vcradar.sources.registry import P0_SOURCES

FIXTURES = Path(__file__).parent / "fixtures" / "sources"
NOW = datetime(2026, 10, 30, 8, 40, tzinfo=BJ)
SOURCES = {src.id: src for src in P0_SOURCES}


def response(file, content_type="text/html", url="https://example.invalid/issue"):
    return HttpResponse(url, 200, content_type, (FIXTURES / file).read_text())


def transport(tmp_path, overrides=None):
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    def absolute(spec):
        if "file" in spec:
            spec["file"] = str(FIXTURES / spec["file"])
        for item in spec.get("sequence", []):
            absolute(item)
    manifest.update(overrides or {})
    for spec in manifest.values():
        absolute(spec)
    directory = tmp_path / "fixtures"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return FixtureTransport(directory)


def workdays(a, b):
    count = 0
    while a < b:
        a += timedelta(days=1)
        if a.weekday() < 5 and a.isoformat() != "2026-11-26":
            count += 1
    return count


def fetch(tmp_path, source_id, overrides=None, state=None, now=NOW, days=workdays, sleep=None, raw_dir=None):
    from vcradar.fetcher import fetch_source
    client = transport(tmp_path, overrides)
    robots = RobotsChecker(client, tmp_path / "cache", now.astimezone(BJ).date())
    outcome = fetch_source(SOURCES[source_id], state or SourceState(source_id), client, robots,
                           now, days, sleep or (lambda _: None), raw_dir)
    return outcome, client


def rss(*entries):
    values = []
    for ident, timestamp in entries:
        values.append(f"<item><guid>{ident}</guid><link>https://example.invalid/{ident}</link><title>{ident}</title><pubDate>{timestamp}</pubDate><description>Synthetic financing.</description></item>")
    return "<rss><channel>" + "".join(values) + "</channel></rss>"
