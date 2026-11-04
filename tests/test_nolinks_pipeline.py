"""占位地址交付守卫、CLI 与首期回滚；只用手写返回和假 transport。"""
from dataclasses import replace
from datetime import date, datetime, time
import json
from pathlib import Path

import pytest

from test_pipe_edges import production_run, production_transport
from test_pipe_pipeline import arguments, offline, rows, write_fixture
from test_er_render import card
from vcradar import cli, config, db, outbox, pipeline, render, scan
from vcradar.config import BJ, Config
from vcradar.guard import check_no_placeholder
from vcradar.models import DigestData
from vcradar.replay import run_replay


DAY = date(2026, 10, 30)
BASE = "https://review.example/radar"
PLACEHOLDER = "https://vc.example.invalid/REPLACE_RANDOM_PATH"
REPLAY = Path(__file__).parent / "fixtures" / "pipe" / "replay.json"


def clock(day=DAY):
    return pipeline.Clock(day, time(8, 40))


def config_file(tmp_path, base_url, *, test_mode=False):
    path = tmp_path / "config.toml"
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"review_site_passed": bool(base_url)}))
    path.write_text(f"state_dir = {json.dumps(str(tmp_path / 'state'))}\n"
                    f"base_url = {json.dumps(base_url)}\n"
                    f"runtime_file = {json.dumps(str(runtime))}\n"
                    f"test_mode = {str(test_mode).lower()}\n")
    return path


@pytest.mark.parametrize("text,expected", [
    ("没有回看链接", []),
    ("https://review.example/radar", []),
    ("https://VC.EXAMPLE.INVALID/radar", ["vc.example.invalid"]),
    ("https://review.example/replace_random_path", ["REPLACE_RANDOM_PATH"]),
    (PLACEHOLDER + " VC.Example.Invalid REPLACE_random_PATH", ["vc.example.invalid", "REPLACE_RANDOM_PATH"]),
])
def test_placeholder_markers_ignore_case_and_only_report_each_once(text, expected):
    assert check_no_placeholder(text) == expected


def test_config_defaults_to_no_links_and_parser_accepts_switch(tmp_path):
    assert Config().base_url == ""
    assert config.load_config(tmp_path / "missing.toml").base_url == ""
    assert config.load_config(Path("config/vc-radar.toml.example")).base_url == ""
    assert cli.parse_args(["--no-links"]).no_links is True
    assert cli.parse_args([]).no_links is False


@pytest.mark.parametrize("base_url", [
    "https://VC.EXAMPLE.INVALID/radar", "https://review.example/replace_random_path", PLACEHOLDER,
])
def test_production_first_placeholder_fails_without_handoff_and_rolls_back(tmp_path, monkeypatch, capsys, base_url):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, base_url=base_url)
    fixture = write_fixture(tmp_path / "fixture", str(DAY))
    production_transport(monkeypatch, fixture)
    args = arguments(first_issue=True)
    assert not cfg.test_mode and not (args.fixture or args.dry_run or args.replay)
    assert pipeline.run(cfg, args, clock()) == 1
    assert not cfg.db_path.exists() and not list(cfg.outbox_dir.glob("*"))
    assert not list(cfg.state_dir.rglob("*.first-tmp.db*"))
    assert not list(cfg.state_dir.glob(".first-stage-*"))
    assert not (cfg.state_dir / "backup").exists()
    assert not (cfg.state_dir / "status.json").exists()
    assert "整体回滚" in capsys.readouterr().err


def test_production_incremental_placeholder_fails_before_outbox_and_rolls_back_new_items(tmp_path, monkeypatch, capsys):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, base_url=BASE)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    production_transport(monkeypatch, fixture)
    production_run(cfg, "2026-10-26", first_issue=True)
    existing = {path: path.read_bytes() for path in cfg.outbox_dir.iterdir()}
    write_fixture(fixture, str(DAY), new=["Birch"])
    cfg.base_url = PLACEHOLDER
    assert pipeline.run(cfg, arguments(), clock()) == 1
    assert {path: path.read_bytes() for path in cfg.outbox_dir.iterdir()} == existing
    assert not any(row["name"] == "Birch" for row in rows(cfg, "company"))
    assert not any(row["date"] == str(DAY) for row in rows(cfg, "digest"))
    assert "占位地址" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ["fixture", "dry_run", "test_db", "to_test"])
def test_test_modes_only_warn_for_placeholder_and_write_isolated_handoff(tmp_path, monkeypatch, capsys, mode):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, base_url=PLACEHOLDER, test_mode=mode == "test_db")
    fixture = write_fixture(tmp_path / "fixture", str(DAY))
    production_transport(monkeypatch, fixture)
    args = arguments(fixture if mode == "fixture" else None, first_issue=True,
                     dry_run=mode == "dry_run", to="test" if mode == "to_test" else None)
    assert pipeline.run(cfg, args, clock()) == 0
    test_cfg = replace(cfg, test_mode=True)
    path, = test_cfg.outbox_dir.glob("*-first.json")
    assert PLACEHOLDER in json.loads(path.read_text())["text"]
    assert test_cfg.db_path.exists()
    assert not (cfg.state_dir / "outbox").exists() and not (cfg.state_dir / "vc-radar.db").exists()
    stderr = capsys.readouterr().err
    assert "警告" in stderr and "占位地址" in stderr and "vc.example.invalid" in stderr


@pytest.mark.parametrize("base_url,no_links", [("", False), (BASE, False), (BASE, True), (PLACEHOLDER, True)])
def test_cli_first_issue_uses_default_or_explicit_link_mode(tmp_path, monkeypatch, capsys, base_url, no_links):
    path = config_file(tmp_path, base_url)
    fixture = write_fixture(tmp_path / "fixture", str(DAY))
    production_transport(monkeypatch, fixture)
    argv = ["--config", str(path), "--first-issue", "--date", str(DAY)]
    if no_links:
        argv.append("--no-links")
    assert cli.main(argv) == 0
    handoff = tmp_path / "state" / "outbox" / f"{DAY}-first.json"
    text = json.loads(handoff.read_text())["text"]
    if not base_url or no_links:
        assert "/#" not in text and "详情" not in text and "看本周全部" not in text
    else:
        assert f"[看本周全部]({BASE}/#week)" in text and "#sources" not in text
    assert "[来源](https://synthetic.example/anchor)" in text
    assert "本周共" not in text and "1. Anchor" in text
    assert not check_no_placeholder(text)
    assert "占位地址" not in capsys.readouterr().err
    assert config.load_config(path).base_url == base_url


def test_cli_preserves_configured_test_database_for_placeholder_warning(tmp_path, monkeypatch, capsys):
    path = config_file(tmp_path, PLACEHOLDER, test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", str(DAY))
    production_transport(monkeypatch, fixture)
    assert cli.main(["--config", str(path), "--first-issue", "--date", str(DAY)]) == 0
    assert list((tmp_path / "state" / "outbox-test").glob("*-first.json"))
    assert not (tmp_path / "state" / "outbox").exists()
    assert "警告：交付文本包含占位地址" in capsys.readouterr().err


@pytest.mark.parametrize("no_links", [False, True])
def test_replay_with_placeholder_warns_or_omits_links(tmp_path, capsys, no_links):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, base_url=PLACEHOLDER)
    assert run_replay(cfg, arguments(replay=REPLAY, no_links=no_links), clock()) == 0
    path, = (cfg.state_dir / "outbox-test").glob("*.json")
    text = json.loads(path.read_text())["text"]
    stderr = capsys.readouterr().err
    if no_links:
        assert "/#" not in text and "看本周全部" not in text and not check_no_placeholder(text)
        assert "占位地址" not in stderr
    else:
        assert PLACEHOLDER in text and "警告：交付文本包含占位地址" in stderr
    assert "[来源](https://synthetic.example/aster-tc)" in text
    assert not (cfg.state_dir / "outbox").exists() and not list(cfg.state_dir.rglob("*.db"))


def stored_issue(cfg, kind):
    generated = datetime(2026, 10, 30, 8, 40, tzinfo=BJ)
    text = render.render_push(DigestData(DAY, None, generated, "有新增", cards=[card()], base_url=PLACEHOLDER,
                                         new_evidence_count=2, review_site_passed=True))
    conn = db.connect(cfg.db_path)
    scan.upsert_digest(conn, {"date": DAY, "push": True, "status": "没有新增", "text": text,
                              "generated_at": generated})
    conn.commit()
    conn.close()
    if kind != "digest":
        writer = outbox.write_first if kind == "first" else outbox.write_main
        kwargs = {} if kind == "first" else {"push": True}
        writer(cfg.outbox_dir, DAY, alarm=False, generated_at=generated, text=text,
               test=cfg.test_mode, real_now=generated, **kwargs)
    return text


@pytest.mark.parametrize("kind", ["main", "first", "digest"])
@pytest.mark.parametrize("no_links", [False, True])
def test_resend_checks_saved_text_or_applies_no_links_without_changing_original(tmp_path, capsys, kind, no_links):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, base_url=BASE)
    original = stored_issue(cfg, kind)
    existing = {path: path.read_bytes() for path in cfg.outbox_dir.glob("*.json")}
    result = pipeline.run(cfg, arguments(resend=True, no_links=no_links), clock())
    paths = list(cfg.outbox_dir.glob("*-resend-*.json"))
    if no_links:
        assert result == 0 and len(paths) == 1
        text = json.loads(paths[0].read_text())["text"]
        assert "重发" not in text and "资料有出入" not in text
        assert "[来源]" in text and "看本周全部" not in text and not check_no_placeholder(text)
        assert rows(cfg, "digest")[0]["resend_count"] == 1
    else:
        assert result == 1 and not paths
        assert "重发失败" in capsys.readouterr().err
    assert all(path.read_bytes() == contents for path, contents in existing.items())
    assert rows(cfg, "digest")[0]["text"] == original


def test_test_resend_only_warns_for_saved_placeholder(tmp_path, capsys):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, base_url=BASE, test_mode=True)
    stored_issue(cfg, "main")
    assert pipeline.run(cfg, arguments(resend=True), clock()) == 0
    path, = cfg.outbox_dir.glob("*-resend-*.json")
    assert PLACEHOLDER in json.loads(path.read_text())["text"]
    assert "警告：交付文本包含占位地址" in capsys.readouterr().err


def test_production_failure_fallback_also_rejects_placeholder_before_outbox(tmp_path, monkeypatch, capsys):
    cfg = Config(state_dir=tmp_path / "state", review_site_passed=True, base_url=BASE)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    production_transport(monkeypatch, fixture)
    production_run(cfg, "2026-10-26", first_issue=True)
    cfg.base_url = PLACEHOLDER
    # 保留一家公司，才能覆盖带页脚链接时的占位地址守卫。
    original_listing = pipeline._listing_for_issue
    def listing(*args, **kwargs):
        result = original_listing(*args, **kwargs)
        result["cards"] = [card()]
        return result
    monkeypatch.setattr(pipeline, "_listing_for_issue", listing)

    def fail(*args, **kwargs):
        raise RuntimeError("手写抓取故障")

    monkeypatch.setattr(pipeline.fetcher, "fetch_all", fail)
    assert pipeline.run(cfg, arguments(), clock()) == 1
    assert not (cfg.outbox_dir / f"{DAY}.json").exists()
    assert "占位地址" in capsys.readouterr().err
