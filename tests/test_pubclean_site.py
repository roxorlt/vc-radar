"""发布前检查的合成库回归；不使用真实网页正文。"""
from dataclasses import replace
from datetime import datetime
import json
import re
import shutil

import pytest

from test_site_build import add_company, add_event
from vcradar import db, site
from vcradar.config import BJ, Config
from vcradar.sources.registry import P0_SOURCES


NOW = datetime(2026, 10, 31, 11, 0, tzinfo=BJ)
BLOCKED = [
    ("本机路径", "/srv/private/private"),
    ("本机路径", "/srv/private/private"),
    ("本机路径", "/cursor/private"),
    ("本机路径", "/tmp/private"),
    ("本机路径", "/root/private"),
    ("本机路径", "/Users/private"),
    ("本机路径", "vc-radar-data"),
    ("密钥/环境变量", "DEEPSEEK"),
    ("密钥/环境变量", "API_KEY"),
    ("密钥/环境变量", "sk-1234567890abcdef"),
    ("密钥/环境变量", ".env"),
    ("密钥/环境变量", "Authorization"),
    ("密钥/环境变量", "Bearer handwritten"),
    ("邮箱", "x@y.z"),
    ("邮箱", "mailto:reader@news.example"),
    ("IPv4 地址", "127.0.0.1"),
    ("IPv4 地址", "10.1.2.3"),
    ("IPv4 地址", "172.16.0.1"),
    ("IPv4 地址", "172.31.255.255"),
    ("IPv4 地址", "192.168.1.1"),
    ("IPv4 地址", "8.8.8.8"),
    ("IPv4 地址", "地址127.0.0.1用于测试"),
    ("IPv4 地址", "010.001.002.003"),
    ("localhost", "LOCALHOST"),
    ("内部人名/bot 名", "ownerx"),
    ("内部人名/bot 名", "维护者x"),
    ("内部人名/bot 名", "取件程序x"),
    ("内部人名/bot 名", "assistx"),
    ("内部人名/bot 名", "codegenx"),
    ("内部人名/bot 名", "QA-x"),
    ("内部人名/bot 名", "UI-x"),
    ("内部人名/bot 名", "PD-x"),
]


@pytest.fixture
def publication(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state")
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, P0_SOURCES)
    add_company(conn, "terse", "Terse")
    add_event(conn, 1, "terse")
    conn.commit()
    monkeypatch.setattr(site, "LINK_RECORDS", tmp_path / "missing-links.json")
    yield conn, cfg
    conn.close()


@pytest.mark.parametrize("test_mode", [False, True])
@pytest.mark.parametrize("category,text", BLOCKED)
def test_hard_errors_report_files_and_categories_before_any_write(publication, tmp_path, test_mode, category, text):
    conn, cfg = publication
    conn.execute("UPDATE company SET summary=?", (text,))
    dest = tmp_path / "uncreated" / "site"
    with pytest.raises(ValueError) as error:
        site.build_site(conn, replace(cfg, test_mode=test_mode), out_dir=dest, now=NOW)
    message = str(error.value)
    assert category in message and "data.json" in message and "c/terse.json" in message
    assert text not in message
    assert not dest.parent.exists()


@pytest.mark.parametrize("text", [
    "公司为工程师提供内部机器人软件，帮助团队决策。种子轮用于研发。",
    "https://news.example/home/story",
    "https://news.example/srv/private/story",
    "https://news.example/srv/private/story",
    "https://news.example/cursor/story",
    "https://news.example/tmp/story",
    "https://news.example/root/story",
    "https://news.example/Users/story",
    "https://news.example/vc-radar-data/story",
    "https://news.example/home/(ownerx)/srv/private",
    "https://[2001:db8::1]/root/ownerx",
    "https://news.example/ownerx/维护者x/取件程序x/assistx/codegenx/QA-x/UI-x/PD-x",
    "sk-1234567890abcde",
    "环境配置来自公开说明，不展示身份验证内容。",
    "邮箱功能暂未开放，联系方式不公开。",
    "版本 256.0.0.1 与 1.2.3.456 均为手写的无效地址。",
    "local host",
    "团队提供软件服务。",
])
def test_public_copy_and_http_url_exceptions_are_preserved(publication, text):
    conn, cfg = publication
    conn.execute("UPDATE company SET summary=?", (text,))
    path = site.build_site(conn, cfg, now=NOW)
    detail = json.loads((path / "c/terse.json").read_text())
    assert detail["companies"][0]["summary"] == text


@pytest.mark.parametrize("text,category", [
    ("https://news.example/?value=API_KEY", "密钥/环境变量"),
    ("https://news.example/?contact=x@y.z", "邮箱"),
    ("https://127.0.0.1/home/story", "IPv4 地址"),
    ("http://localhost/home/story", "localhost"),
    ("https:///srv/private/private", "本机路径"),
    ("https://news.example/home/story，维护者x", "内部人名/bot 名"),
    ("[来源](https://news.example/home/story)ownerx", "内部人名/bot 名"),
])
def test_url_exception_is_limited_to_paths_and_internal_names(publication, text, category):
    conn, cfg = publication
    conn.execute("UPDATE company SET summary=?", (text,))
    with pytest.raises(ValueError, match=category):
        site.build_site(conn, cfg, now=NOW)


def test_source_url_with_home_path_is_allowed(publication):
    conn, cfg = publication
    url = "https://news.example/home/ownerx/story"
    conn.execute("UPDATE event_source SET url=?", (url,))
    path = site.build_site(conn, cfg, now=NOW)
    detail = json.loads((path / "c/terse.json").read_text())
    assert detail["companies"][0]["sources"][0]["url"] == url


def test_json_fields_use_separate_lines_for_publication_scan(publication):
    conn, cfg = publication
    summary = "发布检查使用的合成简介。"
    url = "https://news.example/home/story"
    conn.execute("UPDATE company SET summary=?", (summary,))
    conn.execute("UPDATE event_source SET url=?", (url,))
    path = site.build_site(conn, cfg, now=NOW)
    for output in path.rglob("*.json"):
        text = output.read_text()
        assert text.startswith("{\n ") and text.endswith("\n}")
        lines = text.splitlines()
        summary_lines = {i for i, line in enumerate(lines) if summary in line}
        url_lines = {i for i, line in enumerate(lines) if url in line}
        assert summary_lines and url_lines and summary_lines.isdisjoint(url_lines)
    assert (path / "app.js").read_bytes() == (site.ASSETS / "app.js").read_bytes()


def test_email_in_multiline_json_field_fails_before_writing(publication, tmp_path, monkeypatch):
    conn, cfg = publication
    conn.execute("UPDATE company SET hq_basis='联系 reader@news.example'")
    validate = site._validate_outputs
    captured = {}

    def inspect_and_validate(outputs):
        captured.update(outputs)
        validate(outputs)

    monkeypatch.setattr(site, "_validate_outputs", inspect_and_validate)
    dest = tmp_path / "uncreated" / "site"
    with pytest.raises(ValueError) as error:
        site.build_site(conn, cfg, out_dir=dest, now=NOW)
    assert "c/terse.json：邮箱" in str(error.value)
    assert captured["data.json"].startswith("{\n ")
    detail_lines = captured["c/terse.json"].splitlines()
    assert len(detail_lines) > 1
    assert any(line.lstrip() == '"hq_basis": "联系 reader@news.example",' for line in detail_lines)
    assert "reader@news.example" not in str(error.value)
    assert not dest.parent.exists()


def test_oversize_json_reports_actual_utf8_bytes_without_changing_limit(publication):
    conn, cfg = publication
    path = site.build_site(conn, cfg, now=NOW)
    before = (path / "data.json").read_bytes()
    old_summary = conn.execute("SELECT summary FROM company").fetchone()[0]
    summary = "中" * 167_000
    expected_bytes = len(before) + len(summary.encode("utf-8")) - len(old_summary.encode("utf-8"))
    assert site.MAX_DATA_BYTES == 500_000 and expected_bytes >= 500_000
    conn.execute("UPDATE company SET summary=?", (summary,))
    with pytest.raises(ValueError) as error:
        site.build_site(conn, cfg, now=NOW)
    assert f"超过 500KB：data.json 实际 {expected_bytes} 字节" in str(error.value)
    assert (path / "data.json").read_bytes() == before


def test_hard_error_preserves_current_previous_and_parent_contents(publication, tmp_path):
    conn, cfg = publication
    dest = tmp_path / "exports" / "site"
    site.build_site(conn, cfg, out_dir=dest, now=NOW)
    conn.execute("UPDATE company SET summary='第二份合成简介。'")
    site.build_site(conn, cfg, out_dir=dest, now=NOW)
    before = {p.relative_to(dest.parent): p.read_bytes() for p in dest.parent.rglob("*") if p.is_file()}
    conn.execute("UPDATE company SET summary='Authorization: handwritten'")
    with pytest.raises(ValueError):
        site.build_site(conn, cfg, out_dir=dest, now=NOW)
    assert {p.relative_to(dest.parent): p.read_bytes() for p in dest.parent.rglob("*") if p.is_file()} == before
    assert {p.name for p in dest.parent.iterdir()} == {"site", "site.prev"}


@pytest.mark.parametrize("filename", ["index.html", "app.js", "style.css", "c/terse.json"])
def test_every_asset_and_detail_is_checked(publication, tmp_path, monkeypatch, filename):
    conn, cfg = publication
    if filename.startswith("c/"):
        conn.execute("UPDATE company SET hq_basis='/srv/private/private'")
    else:
        assets = tmp_path / "assets"
        shutil.copytree(site.ASSETS, assets)
        target = assets / filename
        target.write_text(target.read_text() + '\n"/srv/private/private"\n')
        monkeypatch.setattr(site, "ASSETS", assets)
    with pytest.raises(ValueError) as error:
        site.build_site(conn, cfg, now=NOW)
    assert filename in str(error.value) and "本机路径" in str(error.value)
    assert not (cfg.state_dir / "site").exists()


@pytest.mark.parametrize("marker,category", [
    ("Quorvia", "禁用名"), ("Tessaline", "禁用名"),
    ("https://vc.example.invalid/story", "占位地址"), ("REPLACE_RANDOM_PATH", "占位地址"),
])
def test_existing_delivery_guards_remain_strict_in_test_mode(publication, marker, category):
    conn, cfg = publication
    conn.execute("UPDATE company SET summary=?", (marker,))
    with pytest.raises(ValueError, match=category):
        site.build_site(conn, replace(cfg, test_mode=True), now=NOW)


def test_assets_have_no_comments_or_cursor_and_keep_privacy_meta(publication):
    conn, cfg = publication
    path = site.build_site(conn, cfg, now=NOW)
    for filename in ("index.html", "app.js", "style.css"):
        content = (path / filename).read_text()
        assert not re.search(r"//|/\*|\*/|<!--|-->", content)
        assert "cursor" not in content.casefold()
    html = (path / "index.html").read_text()
    assert '<meta name="robots" content="noindex, nofollow">' in html
    assert '<meta name="referrer" content="no-referrer">' in html


def test_url_literal_in_javascript_survives_build(publication, tmp_path, monkeypatch):
    conn, cfg = publication
    assets = tmp_path / "assets"
    shutil.copytree(site.ASSETS, assets)
    script = assets / "app.js"
    literal = 'const releaseURL = "https://news.example/home/story";'
    script.write_text(script.read_text() + "\n" + literal)
    monkeypatch.setattr(site, "ASSETS", assets)
    path = site.build_site(conn, cfg, now=NOW)
    assert literal in (path / "app.js").read_text()
