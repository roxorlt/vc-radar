"""交接后深度调度的假时钟回归，不读取密钥或访问网络。"""
from concurrent.futures import wait as real_wait
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from threading import Event

import pytest

from vcradar import cli, config, db, deep, http, llm, outbox, pipeline, site
from vcradar.config import Config
from test_deep_support import FIXTURE, ManualClock, database, seed
from test_pipe_pipeline import write_fixture


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("测试禁止联网、真实模型和密钥读取")
    monkeypatch.setattr(config, "load_api_key", forbidden)
    monkeypatch.setattr(llm, "urlopen", forbidden)
    monkeypatch.setattr(http.urllib.request, "urlopen", forbidden)


def ordinary(tmp_path, monkeypatch, *, day="2026-10-30", options=()):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", day)
    args = cli.parse_args(["--fixture", str(fixture), "--date", day, "--now", "08:40", *options])
    clock = ManualClock(f"{day}T08:40:00+08:00")
    return cfg, args, clock


def test_handoff_build_deep_each_company_build_and_both_forced_builds(tmp_path, monkeypatch):
    cfg, args, clock = ordinary(tmp_path, monkeypatch)
    events, handoffs, ended = [], [], {}
    original_writer, original_deep = outbox.write_main, deep.run_for_today

    def write_main(*args, **kwargs):
        path = original_writer(*args, **kwargs)
        events.append(("handoff", clock.now()))
        handoffs.append((path, path.read_bytes()))
        return path

    def build(conn, cfg, *, now, snapshot_slot=None):
        assert handoffs and handoffs[0][0].read_bytes() == handoffs[0][1]
        events.append(("build", now))
        if now.strftime("%H:%M:%S") in {"09:30:00", "10:30:00"}:
            assert snapshot_slot == now.strftime("%H%M")
        clock.advance(1)
        return cfg.state_dir / "test/site"

    def work(company, cfg, task_clock, budget, transport, robots, search, client, records, output):
        output.status = "完成"
        output.ended_at = task_clock.now().isoformat()
        ended[company["id"]] = output.ended_at
        return output

    def run_deep(conn, cfg, task_clock, **kwargs):
        events.append(("deep", task_clock.now()))
        with pipeline.run_lock(cfg):
            pass
        assert kwargs["deep_deadline"].strftime("%H:%M") == "09:30"
        assert kwargs["deep_final"].strftime("%H:%M") == "10:30"
        assert callable(kwargs["publish"]) and callable(kwargs["checkpoint"])
        return original_deep(conn, cfg, task_clock, client=object(), link_records=[], **kwargs)

    monkeypatch.setattr(outbox, "write_main", write_main)
    monkeypatch.setattr(site, "build_site", build)
    monkeypatch.setattr(deep, "_work", work)
    monkeypatch.setattr(deep, "run_for_today", run_deep)
    assert pipeline.run(cfg, args, clock) == 0
    assert [event[0] for event in events[:3]] == ["handoff", "build", "deep"]
    builds = [stamp for event, stamp in events if event == "build"]
    conn = db.connect(cfg.db_path)
    rows = [dict(row) for row in conn.execute("SELECT * FROM deepdive")]
    assert rows and len(builds) == len(rows) + 3
    assert [stamp.strftime("%H:%M:%S") for stamp in builds[-2:]] == ["09:30:00", "10:30:00"]
    for row in rows:
        online = datetime.fromisoformat(row["online_at"])
        assert row["status"] == "完成" and online >= datetime.fromisoformat(ended[row["company_id"]])
        assert online in builds  # 上线时间与实际导出的发布时刻一致；瞬时假任务可同刻完成。
        assert online < builds[-2]
    payload = json.loads(handoffs[0][1])
    assert payload["generated_at"].startswith("2026-10-30 08:40")
    assert handoffs[0][0].read_bytes() == handoffs[0][1]
    conn.close()


@pytest.mark.parametrize(("day", "options"), [("2026-10-31", ()), ("2026-10-30", ("--no-deep",)),
                                               ("2026-10-30", ("--first-issue",))])
def test_non_workday_no_deep_and_first_issue_do_not_start_automatic_deep(tmp_path, monkeypatch, day, options):
    cfg, args, clock = ordinary(tmp_path, monkeypatch, day=day, options=options)
    monkeypatch.setattr(site, "build_site", lambda *args, **kwargs: cfg.state_dir / "test/site")
    monkeypatch.setattr(deep, "run_for_today", lambda *args, **kwargs: pytest.fail("本次不能自动启动深度"))
    assert pipeline.run(cfg, args, clock) == 0


def test_deep_exception_preserves_main_exit_code_and_handoff(tmp_path, monkeypatch):
    cfg, args, clock = ordinary(tmp_path, monkeypatch)
    before = []
    monkeypatch.setattr(site, "build_site", lambda *args, **kwargs: cfg.state_dir / "test/site")

    def fail(conn, cfg, task_clock, **kwargs):
        paths = list(cfg.outbox_dir.glob("*.json"))
        assert len(paths) == 1
        before.append((paths[0], paths[0].read_bytes()))
        raise RuntimeError("synthetic deep failure")

    monkeypatch.setattr(deep, "run_for_today", fail)
    assert pipeline.run(cfg, args, clock) == 0
    assert before[0][0].read_bytes() == before[0][1]
    assert "deep_failed" in (cfg.log_dir / "2026-10-30.log").read_text()
    conn = db.connect(cfg.db_path)
    assert conn.execute("SELECT status FROM digest").fetchone()[0] != "全部失败"
    conn.close()


def test_custom_deadlines_are_passed_to_scheduled_deep(tmp_path, monkeypatch):
    cfg, args, clock = ordinary(tmp_path, monkeypatch, options=("--deep-deadline", "09:10", "--deep-final", "09:20"))
    captured = []
    monkeypatch.setattr(site, "build_site", lambda *args, **kwargs: cfg.state_dir / "test/site")
    monkeypatch.setattr(deep, "run_for_today", lambda *args, **kwargs: captured.append(kwargs))
    assert pipeline.run(cfg, args, clock) == 0
    assert captured[0]["deep_deadline"].strftime("%H:%M") == "09:10"
    assert captured[0]["deep_final"].strftime("%H:%M") == "09:20"


def test_final_deadline_stops_active_and_unstarted_companies_before_final_build(tmp_path, monkeypatch):
    conn, cfg = database(tmp_path)
    day = date(2026, 10, 30)
    for cid in ["a-fast", *[f"slow-{index:02d}" for index in range(15)]]:
        seed(conn, cid, first=str(day))
    clock = ManualClock("2026-10-30T09:29:59+08:00")
    deadline = datetime.fromisoformat("2026-10-30T09:30:00+08:00")
    final = datetime.fromisoformat("2026-10-30T10:30:00+08:00")
    release, fast_ready = Event(), Event()
    builds, waits = [], []

    def build(conn, cfg, clock, *args):
        builds.append((clock.now(), [dict(row) for row in conn.execute("SELECT * FROM deepdive")]))
        return clock.now()

    def work(company, cfg, task_clock, budget, transport, robots, search, client, records, output):
        if company["id"] == "a-fast":
            output.status, output.ended_at = "完成", task_clock.now().isoformat()
            fast_ready.set()
        else:
            release.wait(5)
            output.status, output.ended_at = "完成", task_clock.now().isoformat()
        return output

    def wait(futures, **kwargs):
        waits.append(1)
        if len(waits) == 1:
            assert fast_ready.wait(2)
            return real_wait(futures, timeout=0.05, return_when=deep.FIRST_COMPLETED)
        target = {2: deadline, 3: final - timedelta(seconds=1)}.get(len(waits), final)
        clock.advance((target - clock.now()).total_seconds())
        return set(), set(futures)

    monkeypatch.setattr(deep, "_work", work)
    monkeypatch.setattr(deep, "wait", wait)
    try:
        outputs = deep.run_for_today(conn, cfg, clock, transport=http.FixtureTransport(FIXTURE), client=object(),
                                     link_records=[], deep_deadline=deadline, deep_final=final,
                                     publish=build, checkpoint=build, sleep=clock.advance)
        assert len(outputs) == 16 and any(output.status == "暂时没有" for output in outputs)
        assert any(stamp == deadline and any(row["status"] == "生成中" for row in rows) for stamp, rows in builds)
        assert builds[-1][0] == final
        assert len(builds[-1][1]) == 16 and not any(row["running"] for row in builds[-1][1])
        assert not any(row["status"] == "生成中" for row in builds[-1][1])
        assert any(row["status"] == "暂时没有" and row["online_at"] is None for row in builds[-1][1])
        terminal = {row["company_id"] for row in builds[-1][1] if row["status"] == "暂时没有"}
        assert not terminal & {row["id"] for row in deep.select_companies(conn, day, now=clock.now())}
        late = next(item for item in outputs if item.company_id in terminal)
        assert not deep._save(conn, replace(late, status="完成", result={"late": True}), str(day))
        assert conn.execute("SELECT status FROM deepdive WHERE company_id=?", (late.company_id,)).fetchone()[0] == "暂时没有"
    finally:
        release.set()
        conn.close()


def test_empty_scheduled_day_forces_both_builds_without_constructing_clients(tmp_path, monkeypatch):
    conn, cfg = database(tmp_path)
    clock = ManualClock("2026-10-30T08:55:00+08:00")
    builds = []
    monkeypatch.setattr(deep, "_client", lambda *args: pytest.fail("没有公司不应创建模型客户端"))
    result = deep.run_for_today(conn, cfg, clock, deep_deadline=datetime.fromisoformat("2026-10-30T09:30:00+08:00"),
                                deep_final=datetime.fromisoformat("2026-10-30T10:30:00+08:00"),
                                checkpoint=lambda *args: builds.append(clock.now()), sleep=clock.advance)
    assert result == []
    assert [stamp.strftime("%H:%M") for stamp in builds] == ["09:30", "10:30"]
    conn.close()


def test_start_after_final_marks_unstarted_without_loading_clients(tmp_path, monkeypatch):
    conn, cfg = database(tmp_path)
    seed(conn, first="2026-10-30")
    clock = ManualClock("2026-10-30T10:31:00+08:00")
    builds = []
    monkeypatch.setattr(deep, "_client", lambda *args: pytest.fail("截止后不能创建模型客户端"))
    output = deep.run_for_today(conn, cfg, clock, deep_deadline=datetime.fromisoformat("2026-10-30T09:30:00+08:00"),
                                deep_final=datetime.fromisoformat("2026-10-30T10:30:00+08:00"),
                                checkpoint=lambda conn, *args: builds.append([row[0] for row in conn.execute("SELECT status FROM deepdive")]))
    assert [item.status for item in output] == ["暂时没有"]
    assert builds[-1] == ["暂时没有"]
    conn.close()


@pytest.mark.parametrize("test_mode", [False, True])
def test_initial_site_build_only_writes_the_mode_local_site(tmp_path, monkeypatch, test_mode):
    cfg = Config(state_dir=tmp_path / "state", test_mode=test_mode)
    conn = db.connect(cfg.db_path)
    args = cli.parse_args(["--no-deep"])
    clock = ManualClock("2026-10-30T08:55:00+08:00")
    pipeline._after_handoff(conn, cfg, args, clock, clock.now().date(), {})
    assert (cfg.state_dir / ("test/site/data.json" if test_mode else "site/data.json")).exists()
    assert (cfg.state_dir / "test-site").exists() is test_mode
    if test_mode:
        assert (cfg.state_dir / "test-site/2026-10-30/0855/status.json").exists()
    conn.close()
