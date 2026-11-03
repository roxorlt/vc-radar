"""任务 K：文本抽取的缓存命中计费与月账累计。"""
import json
from datetime import datetime

import pytest

from vcradar import extract, llm, pipeline
from vcradar.config import Config
from test_deep_support import ManualClock


@pytest.mark.parametrize("stamp,factor", [
    ("2026-10-30T08:59:59+08:00", 1), ("2026-10-30T09:00:00+08:00", 2),
    ("2026-10-30T12:00:00+08:00", 1), ("2026-10-30T14:00:00+08:00", 2),
    ("2026-10-30T18:00:00+08:00", 1), ("2026-10-31T09:00:00+08:00", 1),
    ("2026-10-01T09:00:00+08:00", 1), ("2026-10-10T09:00:00+08:00", 1),
    ("2026-10-30T01:00:00+00:00", 2),
])
@pytest.mark.parametrize("usage,base", [
    ({"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 600_000, "completion_tokens": 200_000}, 1.212),
    ({"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000}, 0.02),
    ({"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 2_000_000}, 0.02),
    ({"prompt_tokens": 1_000_000}, 1),
    ({"completion_tokens": 1_000_000}, 4),
])
def test_extract_cost_includes_cache_discount_at_beijing_call_start(stamp, factor, usage, base):
    assert extract.usage_cost(usage, datetime.fromisoformat(stamp)) == pytest.approx(base * factor)


def session(cfg, clock, client):
    return pipeline.ModelSession(client, cfg, clock, pipeline._Budget(cfg, clock, scheduled=False),
                                 pipeline._RunLog(cfg.log_dir / "synthetic-usage.log"))


def test_month_ledger_adds_cache_hits_to_legacy_month_and_prices_retries_separately(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    cfg.usage_file.parent.mkdir(parents=True)
    cfg.usage_file.write_text(json.dumps({"model": "deepseek-flash", "months": {"2026-10": {
        "calls": 10, "failed_calls": 1, "prompt_tokens": 500, "completion_tokens": 200, "estimated_cost": 0.7,
    }}}))
    clock = ManualClock("2026-10-30T08:59:59+08:00")

    class AdvanceModel(llm.MockLLMClient):
        def complete_json(self, *args):
            result = super().complete_json(*args)
            clock.advance(2)
            return result

    usage = {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 1_000_000}
    client = AdvanceModel([{"error": "http_429", "usage": usage}, {"content": {}, "usage": usage}], model="deepseek-flash")
    model = session(cfg, clock, client)
    assert not model.complete_json("system", "user", 30).ok
    assert model.complete_json("system", "user", 30).ok
    model.close()
    month = json.loads(cfg.usage_file.read_text())["months"]["2026-10"]
    assert month["calls"] == 12 and month["failed_calls"] == 2
    assert month["prompt_tokens"] == 2_000_500 and month["completion_tokens"] == 200
    assert month["prompt_cache_hit_tokens"] == 2_000_000
    assert month["estimated_cost"] == pytest.approx(0.7 + 0.02 + 0.04)
    events = [json.loads(line) for line in (cfg.log_dir / "synthetic-usage.log").read_text().splitlines()]
    ends = [event for event in events if event["event"] == "model_end"]
    assert [event["usage"]["prompt_cache_hit_tokens"] for event in ends] == [1_000_000, 1_000_000]
    assert not (cfg.state_dir / "extract-usage.json").exists()


@pytest.mark.parametrize("hit,expected", [(None, 0), (-1, 0), (True, 0), ("500", 0), (500, 500), (2000, 1000)])
def test_ledger_sanitizes_and_clamps_cache_hit_tokens(tmp_path, hit, expected):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    clock = ManualClock()
    usage = {"prompt_tokens": 1000, "prompt_cache_hit_tokens": hit, "completion_tokens": 100}
    model = session(cfg, clock, llm.MockLLMClient([{"content": {}, "usage": usage}]))
    model.complete_json("system", "user", 30)
    model.close()
    month = json.loads(cfg.usage_file.read_text())["months"]["2026-10"]
    assert month["prompt_cache_hit_tokens"] == expected
    assert month["estimated_cost"] == pytest.approx((1000 - expected + expected * 0.02 + 400) / 1_000_000)


def test_missing_usage_is_zero_cost_and_still_records_cache_counter(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    model = session(cfg, ManualClock(), llm.MockLLMClient([{"error": "timeout"}]))
    model.complete_json("system", "user", 30)
    model.close()
    month = json.loads(cfg.usage_file.read_text())["months"]["2026-10"]
    assert month["calls"] == month["failed_calls"] == 1
    assert month["prompt_cache_hit_tokens"] == month["estimated_cost"] == 0
