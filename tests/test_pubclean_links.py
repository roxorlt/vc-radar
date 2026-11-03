"""日期链接配置、推送拼接和启动拒绝的离线回归。"""
from datetime import date, datetime, time
import json
from pathlib import Path

import pytest

from test_er_render import card
from test_pipe_pipeline import arguments, offline, write_fixture
from vcradar import cli, config, db, pipeline, render
from vcradar.config import BJ, Config
from vcradar.models import DigestData


DAY = date(2026, 10, 30)
BASE = "https://review.example/radar"
INVALID_TEMPLATES = [
    "{week}", "{}", "{date!s}", "{date:%Y}", "{date.year}", "{date[0]}",
    "{date", "date}", "{{date}}", "../{date}", "archive/../{date}", "archive..old",
    "archive//{date}", " {date}", "{date}\t", "{date}\n", "{date}\u3000", "{date}?view=week", "{date}#week",
]


def write_config(tmp_path, template):
    path = tmp_path / "config.toml"
    path.write_text(f"state_dir = {json.dumps(str(tmp_path / 'uncreated'))}\n"
                    f"link_path_template = {json.dumps(template, ensure_ascii=False)}\n")
    return path


def test_link_template_defaults_remain_disabled(tmp_path):
    assert Config().link_path_template == ""
    assert config.load_config(tmp_path / "missing.toml").link_path_template == ""
    example = config.load_config(Path("config/vc-radar.toml.example"))
    assert example.link_path_template == "" and example.base_url == ""


@pytest.mark.parametrize("template", ["", "{date}", "archive/{date}", "/{date}/"])
def test_valid_template_loads_from_toml_and_overrides(tmp_path, template):
    assert Config(link_path_template=template).link_path_template == template
    assert config.load_config(write_config(tmp_path, template)).link_path_template == template
    assert config.load_config(tmp_path / "missing", link_path_template=template).link_path_template == template
    assert not (tmp_path / "uncreated").exists()


@pytest.mark.parametrize("template", INVALID_TEMPLATES)
def test_invalid_templates_are_rejected_during_construction_and_loading(tmp_path, template):
    with pytest.raises(ValueError, match="link_path_template"):
        Config(link_path_template=template)
    with pytest.raises(ValueError, match="link_path_template"):
        config.load_config(write_config(tmp_path, template))
    with pytest.raises(ValueError, match="link_path_template"):
        config.load_config(tmp_path / "missing", link_path_template=template)


@pytest.mark.parametrize("template", [None, 123, False, [], {}])
def test_non_string_templates_are_rejected(template):
    with pytest.raises(ValueError, match="link_path_template"):
        Config(link_path_template=template)


def test_invalid_template_fails_cli_startup_before_opening_state(tmp_path, capsys):
    path = write_config(tmp_path, "{week}")
    assert cli.main(["--config", str(path), "--build-site"]) == 1
    assert "ValueError" in capsys.readouterr().err
    assert not (tmp_path / "uncreated").exists()


@pytest.mark.parametrize("template,expected", [
    ("", BASE), ("{date}", BASE + "/2026-10-30"),
    ("/archive/{date}/", BASE + "/archive/2026-10-30"),
])
def test_template_applies_to_week_sources_evidence_and_details(template, expected):
    base = render.link_base_url(BASE + "/", template, DAY)
    digest = DigestData(DAY, None, datetime(2026, 11, 1, tzinfo=BJ), "有新增",
                        cards=[card()], new_evidence_count=2, base_url=base, review_site_passed=True)
    text = render.render_push(digest)
    assert "资料有出入" not in text
    assert f"[看本周全部]({expected}/#week)" in text
    assert "#sources" not in text
    assert f"[详情]({expected}/#c/aster)" in render.render_card(digest.cards[0], base)
    assert "2026-11-01" not in text


def test_empty_base_keeps_navigation_disabled_with_date_template():
    base = render.link_base_url("", "{date}", DAY)
    digest = DigestData(DAY, None, datetime(2026, 10, 30, tzinfo=BJ), "没有新增",
                        new_evidence_count=2, base_url=base, review_site_passed=True)
    text = render.render_push(digest)
    assert base == "" and "](" not in text
    assert "本周 2 家资料有出入" not in text
    assert "[详情]" not in render.render_card(card(), base)


@pytest.mark.parametrize("template", ["", "{date}"])
@pytest.mark.parametrize("base_url,no_links", [("", False), (BASE + "/", False), (BASE, True)])
def test_first_issue_and_resend_use_template_or_no_links(tmp_path, monkeypatch, template, base_url, no_links):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, base_url=base_url,
                 link_path_template=template, test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", str(DAY))
    clock = pipeline.Clock(DAY, time(8, 40))
    assert pipeline.run(cfg, arguments(fixture, first_issue=True, no_deep=True, no_links=no_links), clock) == 0
    handoff, = cfg.outbox_dir.glob("*-first.json")
    first_text = json.loads(handoff.read_text())["text"]
    assert pipeline.run(cfg, arguments(resend=True, no_links=no_links), clock) == 0
    resend, = cfg.outbox_dir.glob("*-first-resend-*.json")
    resend_text = json.loads(resend.read_text())["text"]
    for text in (first_text, resend_text):
        if not base_url or no_links:
            assert "/#" not in text and "看本周全部" not in text
        else:
            expected = BASE + ("/2026-10-30" if template else "")
            assert "#sources" not in text
            assert f"]({expected}/#week)" in text
    assert cfg.base_url == base_url and cfg.link_path_template == template


def test_weekly_increment_uses_push_date_instead_of_first_issue_date(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, test_mode=True, base_url=BASE, link_path_template="{date}")
    fixture = write_fixture(tmp_path / "fixture", "2026-10-26", old=True)
    assert pipeline.run(cfg, arguments(fixture, first_issue=True, no_deep=True),
                        pipeline.Clock(date(2026, 10, 26), time(8, 40))) == 0
    write_fixture(fixture, str(DAY), new=["Birch"])
    assert pipeline.run(cfg, arguments(fixture, no_deep=True),
                        pipeline.Clock(DAY, time(8, 40))) == 0
    handoff, = cfg.outbox_dir.glob(f"{DAY}-*.json")
    text = json.loads(handoff.read_text())["text"]
    assert f"]({BASE}/2026-10-30/#week)" in text
    assert "/2026-10-26/" not in text


def test_failure_handoff_uses_the_same_template(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, test_mode=True, base_url=BASE, link_path_template="{date}")
    conn = db.connect(cfg.db_path)
    try:
        pipeline._failure_file(conn, cfg, arguments(no_deep=True), pipeline.Clock(DAY, time(8, 40)), DAY)
    finally:
        conn.close()
    handoff, = cfg.outbox_dir.glob("*.json")
    text = json.loads(handoff.read_text())["text"]
    assert text.splitlines()[-1] == "[看本周全部](https://review.example/radar/2026-10-30/#week)"
    assert "#sources" not in text
    assert text.splitlines()[1] == "所有来源都没抓到：今天每个网站都出了问题。本周汇总可能不全，不代表没有新公司。"
