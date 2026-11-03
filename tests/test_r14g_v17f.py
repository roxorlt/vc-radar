"""R14g：V17f 最新普通主文件判据、任一回执停止新写和完整测试重置。"""
from dataclasses import replace
from datetime import date, datetime, time as day_time
import json
from pathlib import Path
import re

import pytest

from vcradar import cli, db, fetcher, outbox, pipeline
from vcradar.config import BJ, Config
from test_pipe_pipeline import arguments, offline, write_fixture  # noqa: F401
from test_r14e_v17c import extra_source
from test_r14f_v17d import NOTICE, manual_stop, receipt, row, stopped_date
from test_sources_support import SOURCES


DAY = date(2026, 11, 6)


@pytest.fixture
def env(tmp_path, monkeypatch):
    class FixedFileClock(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = datetime(2026, 11, 3, 12, tzinfo=BJ)
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)

    monkeypatch.setattr(pipeline, "datetime", FixedFileClock)
    source = tmp_path / "research-links.json"
    source.write_text(json.dumps([{"company": "Handwritten", "type": "公司", "url": "https://handwritten.example", "verified": True}]))
    monkeypatch.setattr(pipeline, "Config", lambda: replace(Config(), links_file=source))
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    # 每组先走实际重置入口，清除其他组的回执和缓存影响。
    assert pipeline.run(cfg, arguments(reset_test_db=True), pipeline.Clock(DAY, day_time(8, 40))) == 0
    with db.connect(cfg.db_path) as conn:
        db.sync_sources(conn, list(SOURCES.values()))
    fixture = write_fixture(tmp_path / "fixture", "2026-11-06", new=["Snapshot G"])
    cfg_file = tmp_path / "test.toml"
    cfg_file.write_text(f'state_dir = "{cfg.state_dir}"\nruntime_file = "{tmp_path / "runtime.json"}"\n')
    return cfg, fixture, cfg_file, source


def normal_files(cfg, day=DAY):
    pattern = re.compile(re.escape(str(day)) + r"-\d{6}\.json")
    return sorted((p for p in cfg.outbox_dir.glob(f"{day}-*.json") if pattern.fullmatch(p.name)), key=lambda p: p.name)


def run_day(env, *, expected=0, **overrides):
    cfg, fixture, _, _ = env
    args = arguments(fixture, date=DAY, no_deep=True, **overrides)
    assert pipeline.run(cfg, args, pipeline.Clock(DAY, day_time(8, 40))) == expected
    return pipeline._main_file(cfg, DAY)


def sync(env):
    _, _, cfg_file, _ = env
    assert cli.main(["--config", str(cfg_file), "--sync-sent", "--test", "--date", str(DAY)]) == 0


def handoff(cfg, name, *, text=NOTICE):
    path = cfg.outbox_dir / name
    outbox.write_atomic(path, {"date": name[:10], "push": True, "alarm": True,
                              "generated_at": name[:10] + " 08:40:00", "text": text})
    return path


@pytest.mark.parametrize("early_sent,late_sent,late_text,expected", [
    (False, True, NOTICE, "2026-11-06"),
    (True, False, NOTICE, None),
    (True, True, "普通公司内容，没有停抓句", None),
    (True, True, NOTICE, "2026-11-06"),
])
def test_latest_normal_json_and_its_own_receipt_control_notice_date(env, early_sent, late_sent, late_text, expected):
    cfg, _, _, _ = env
    manual_stop(cfg)
    early = handoff(cfg, "2026-11-06-084000.json")
    late = handoff(cfg, "2026-11-06-084100.json", text=late_text)
    if early_sent:
        receipt(early)
    if late_sent:
        receipt(late)
    sync(env)
    assert stopped_date(cfg) == expected
    with db.connect(cfg.db_path) as conn:
        selected = pipeline._latest_handoff(cfg, DAY)
        assert selected[1] == late and selected[2]["text"] == late_text
    assert pipeline._main_file(cfg, DAY) == late
    assert row(cfg)["sent_at"] == ("2026-11-06 08:59:30" if late_sent else None)


def test_first_and_resend_files_do_not_join_normal_hhmmss_maximum(env):
    cfg, _, _, _ = env
    manual_stop(cfg)
    normal = handoff(cfg, "2026-11-06-084100.json")
    receipt(normal)
    for name in ("2026-11-06-235959-first.json", "2026-11-06-resend-1-235959.json", "2026-11-06-first-resend-1-235959.json"):
        path = handoff(cfg, name, text="独立文件，不能替代普通主文件的 text")
        receipt(path, result="fail")
    sync(env)
    assert stopped_date(cfg) == "2026-11-06" and row(cfg)["sent_result"] == "ok"


@pytest.mark.parametrize("explicit_date,clock_day", [(True, date(2026, 11, 13)), (False, DAY)])
def test_future_test_handoffs_are_excluded_by_date_argument_or_run_day(env, explicit_date, clock_day):
    cfg, _, _, _ = env
    manual_stop(cfg)
    future = handoff(cfg, "2026-11-13-084000.json")
    receipt(future, day="2026-11-13")
    args = arguments(sync_sent=True, test=True, date=DAY if explicit_date else None)
    assert pipeline.run(cfg, args, pipeline.Clock(clock_day, day_time(9, 10))) == 0
    assert stopped_date(cfg) is None
    assert row(cfg)["sent_at"] is None
    with db.connect(cfg.db_path) as conn:
        pipeline._record_ts_notice(conn, cfg, DAY)
        assert conn.execute("SELECT stop_notice_date FROM source WHERE id='ts_web'").fetchone()[0] is None
    assert future.exists() and future.with_suffix(".sent").exists()


@pytest.mark.parametrize("failure", [False, True], ids=["normal", "failure-path"])
def test_52_4_two_normal_jsons_then_receipt_freezes_third_run_and_resend_snapshot(env, monkeypatch, failure):
    cfg, _, _, _ = env
    first = run_day(env)
    first_bytes = first.read_bytes()
    before = row(cfg)
    eid = json.loads(before["event_ids_json"])[0]
    with db.connect(cfg.db_path) as conn:
        added = extra_source(conn, eid)
    second = run_day(env)
    frozen = row(cfg)["card_sources_json"]
    assert first != second and first.name < second.name
    assert first.read_bytes() == first_bytes
    assert before["card_sources_json"] != frozen
    assert any(source["url"] == added for source in json.loads(frozen)[str(eid)])
    receipt(second)
    second_bytes = second.read_bytes()
    with db.connect(cfg.db_path) as conn:
        third_url = extra_source(conn, eid, source_id="cbn_main", name="Crunchbase News")
    if failure:
        def fail(*args, **kwargs):
            raise RuntimeError("合成抓取失败")
        monkeypatch.setattr(fetcher, "fetch_all", fail)
    third = run_day(env, expected=1 if failure else 0)
    assert third == second and second.read_bytes() == second_bytes
    assert row(cfg)["card_sources_json"] == frozen
    assert all(source["url"] != third_url for source in json.loads(frozen)[str(eid)])
    run_day(env, resend=True, dry_run=True)
    assert row(cfg)["card_sources_json"] == frozen
    assert normal_files(cfg) == [first, second]
    resends = list(cfg.outbox_dir.glob("2026-11-06-resend-*.json"))
    assert len(resends) == 1 and re.fullmatch(r"2026-11-06-resend-1-\d{6}\.json", resends[0].name)
    assert list(cfg.outbox_dir.glob("*.sent")) == [second.with_suffix(".sent")]
    assert len(list(cfg.outbox_dir.glob("*.json"))) == 3


@pytest.mark.parametrize("kind", ["ordinary", "first", "resend", "first-resend"])
@pytest.mark.parametrize("failure", [False, True], ids=["normal", "failure-path"])
def test_any_same_day_handoff_receipt_freezes_normal_rerun_snapshot(env, monkeypatch, kind, failure):
    cfg, _, _, _ = env
    original = run_day(env)
    frozen = row(cfg)["card_sources_json"]
    ack_path = original
    if kind != "ordinary":
        name = {"first": "2026-11-06-235959-first.json", "resend": "2026-11-06-resend-1-235959.json",
                "first-resend": "2026-11-06-first-resend-1-235959.json"}[kind]
        ack_path = cfg.outbox_dir / name
        outbox.write_atomic(ack_path, json.loads(original.read_text()))
    # 冻结只看存在，失败回执也不能让普通运行另写一个文件。
    receipt(ack_path, result="fail")
    with db.connect(cfg.db_path) as conn:
        extra_source(conn, json.loads(row(cfg)["event_ids_json"])[0])
    before = {p.name: p.read_bytes() for p in cfg.outbox_dir.iterdir() if p.is_file()}
    if failure:
        def fail(*args, **kwargs):
            raise RuntimeError("合成失败路径")
        monkeypatch.setattr(fetcher, "fetch_all", fail)
    run_day(env, expected=1 if failure else 0)
    after = {p.name: p.read_bytes() for p in cfg.outbox_dir.iterdir() if p.is_file()}
    assert after == before and row(cfg)["card_sources_json"] == frozen


def test_receipt_created_during_fetch_is_checked_before_writing_json(env, monkeypatch):
    cfg, _, _, _ = env
    first = run_day(env)
    frozen = row(cfg)["card_sources_json"]
    with db.connect(cfg.db_path) as conn:
        extra_source(conn, json.loads(row(cfg)["event_ids_json"])[0])
    original = fetcher.fetch_all

    def fetch_and_ack(*args, **kwargs):
        result = original(*args, **kwargs)
        receipt(first)
        return result

    monkeypatch.setattr(fetcher, "fetch_all", fetch_and_ack)
    run_day(env)
    assert normal_files(cfg) == [first] and row(cfg)["card_sources_json"] == frozen


@pytest.mark.parametrize("kind", ["ordinary", "first", "resend", "first-resend"])
def test_outbox_write_guard_handles_all_receipt_names_and_leaves_production_behavior(tmp_path, kind):
    names = {"ordinary": "2026-11-06-084000.json", "first": "2026-11-06-084000-first.json",
             "resend": "2026-11-06-resend-1-084000.json", "first-resend": "2026-11-06-first-resend-1.json"}
    path = tmp_path / names[kind]
    outbox.write_atomic(path, {"push": True, "text": "已经保存的合成推送"})
    receipt(path)
    kwargs = dict(push=True, alarm=False, generated_at=datetime(2026, 11, 6, 8, 40, tzinfo=BJ),
                  text="新的合成推送", real_now=datetime(2026, 11, 3, 12, tzinfo=BJ))
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert outbox.write_main(tmp_path, DAY, test=True, **kwargs) == path
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert outbox.write_main(tmp_path, DAY, test=False, **kwargs).name == "2026-11-06.json"


def test_unsent_same_second_runs_create_distinct_normal_jsons_without_overwrite(tmp_path):
    kwargs = dict(push=True, alarm=False, generated_at=datetime(2026, 11, 6, 8, 40, tzinfo=BJ),
                  test=True, real_now=datetime(2026, 11, 3, 12, tzinfo=BJ))
    first = outbox.write_main(tmp_path, DAY, text="第一次", **kwargs)
    original = first.read_bytes()
    second = outbox.write_main(tmp_path, DAY, text="第二次", **kwargs)
    third = outbox.write_main(tmp_path, DAY, text="第三次", **kwargs)
    assert [p.name for p in (first, second, third)] == [f"2026-11-06-12000{i}.json" for i in range(3)]
    assert first.read_bytes() == original
    assert json.loads(second.read_text())["text"] == "第二次"


def test_reset_removes_test_receipts_cache_rates_and_restores_research_links(env):
    cfg, _, _, research = env
    source_bytes = research.read_bytes()
    targets = [cfg.db_path, *(Path(str(cfg.db_path) + suffix) for suffix in ("-wal", "-shm")),
               cfg.outbox_dir / "2026-11-06-084000.json", cfg.outbox_dir / "2026-11-06-084000.sent",
               cfg.outbox_dir / "2026-11-06-resend-1-084100.json", cfg.outbox_dir / "2026-11-06-resend-1-084100.sent",
               cfg.state_dir / "test-cache/robots.txt", cfg.rates_file, cfg.raw_dir / "old.out"]
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("手写的待清理测试文件")
    links = cfg.db_path.parent / "links.json"
    links.write_text('[{"company":"旧测试记录"}]')
    assert pipeline.run(cfg, arguments(reset_test_db=True), pipeline.Clock(DAY, day_time(8, 40))) == 0
    assert all(not path.exists() for path in targets)
    assert all(not directory.exists() or not list(directory.iterdir()) for directory in (cfg.outbox_dir, cfg.state_dir / "test-cache"))
    assert links.read_bytes() == source_bytes and research.read_bytes() == source_bytes
