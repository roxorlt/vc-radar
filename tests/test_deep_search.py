import json

import pytest

from vcradar.config import Config
from vcradar.http import FixtureTransport
from vcradar.search import ENDPOINT, SearchClient, funding_query
from test_deep_support import FIXTURE


def test_search_official_request_shape_redaction_and_one_charge(tmp_path, monkeypatch):
    monkeypatch.setattr("vcradar.config.load_api_key", lambda *_: pytest.fail("测试入口不能读密钥"))
    transport = FixtureTransport(FIXTURE)
    client = SearchClient(transport, Config(state_dir=tmp_path, test_mode=True))
    result = client.search_company("Cedar Labs")
    assert result.ok and result.cost == 0.005 and result.attempted
    assert len(transport.request_log) == 1
    request = transport.request_log[0]
    assert request["url"] == ENDPOINT and request["method"] == "POST"
    assert request["headers"]["Authorization"] == "<key>"
    assert request["headers"]["Content-Type"] == "application/json"
    assert json.loads(request["data"]) == {"query": '"Cedar Labs" funding OR seed OR "Series A"', "count": 5, "need_content": False}


@pytest.mark.parametrize("spec", [
    {"status": 429}, {"status": 500}, {"status": 201}, {"status": None, "error": "timeout"},
    {"status": 200, "body": "not json"}, {"status": 200, "body": "[]"},
    {"status": 200, "body": '{"results":null}'}, {"status": 200, "body": '{"results":["bad"]}'},
    {"status": 200, "body": '{"results":[{}]}', "error": "timeout"},
])
def test_search_failures_do_not_retry_or_charge(spec):
    transport = FixtureTransport(FIXTURE)
    transport.manifest[ENDPOINT] = spec
    result = SearchClient(transport, api_key="offline-placeholder").search_company("Cedar Labs")
    assert not result.ok and result.cost == 0
    assert len(transport.request_log) == 1


def test_empty_results_success_is_free():
    transport = FixtureTransport(FIXTURE)
    transport.manifest[ENDPOINT] = {"body": '{"results":[]}'}
    result = SearchClient(transport, api_key="offline-placeholder").search_company("Cedar Labs")
    assert result.ok and not result.results and result.cost == 0


def test_search_timeout_is_capped_at_15_seconds():
    class RecordTimeout(FixtureTransport):
        def get(self, *args, **kwargs):
            self.timeout = kwargs["timeout"]
            return super().get(*args, **kwargs)

    transport = RecordTimeout(FIXTURE)
    assert SearchClient(transport, api_key="offline-placeholder").search_company("Cedar Labs", timeout_s=180).ok
    assert transport.timeout == 15
    SearchClient(transport, api_key="offline-placeholder").search_company("Cedar Labs", timeout_s=0.02)
    assert transport.timeout == 0.02


def test_client_timeout_returns_failure_without_retry():
    transport = FixtureTransport(FIXTURE)
    transport.manifest[ENDPOINT] = {"delay_s": 0.02, "body": '{"results":[{}]}'}
    result = SearchClient(transport, api_key="offline-placeholder").search_company("Cedar", timeout_s=0.001)
    assert not result.ok and result.cost == 0 and len(transport.request_log) == 1


@pytest.mark.parametrize("query", ["", " ", "x" * 129, None])
def test_invalid_query_never_sends(query):
    transport = FixtureTransport(FIXTURE)
    assert not SearchClient(transport, api_key="offline-placeholder").search(query).ok
    assert not transport.request_log


def test_funding_query_preserves_fixed_suffix_and_length():
    assert len(funding_query("长" * 200)) == 128
    assert funding_query("长" * 200).endswith('" funding OR seed OR "Series A"')
    with pytest.raises(ValueError):
        funding_query(" ")


def test_missing_key_never_sends_or_logs():
    transport = FixtureTransport(FIXTURE)
    result = SearchClient(transport).search_company("Cedar Labs")
    assert not result.attempted and result.error == "missing_key" and not transport.request_log
