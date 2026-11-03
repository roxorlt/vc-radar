"""模型客户端的离线协议、错误和回放测试。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError

import pytest

# 仓库骨架没有安装元数据，兼容任务指定的 pytest 可执行文件入口。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vcradar import llm
from vcradar.llm import DeepSeekClient, LLMResult, MockLLMClient


def test_deepseek_request_and_safe_call_log(monkeypatch):
    records = []
    requests = []

    def reply(request, *, timeout):
        requests.append((request, timeout))
        return io.BytesIO(json.dumps({
            "choices": [{"message": {"content": '{"company_name":"Aster"}'}}],
            "usage": {"prompt_tokens": 27, "completion_tokens": 8},
        }).encode())

    monkeypatch.setattr(llm, "urlopen", reply)
    client = DeepSeekClient("test-placeholder", log=records.append)
    result = client.complete_json("系统提示", "来源数据", 57.5)
    assert result.ok and json.loads(result.content) == {"company_name": "Aster"}
    assert result.usage == {"prompt_tokens": 27, "completion_tokens": 8}
    request, timeout = requests[0]
    assert request.full_url == "https://api.deepseek.com/chat/completions"
    assert request.get_method() == "POST" and timeout == 57.5
    assert request.get_header("Authorization") == "Bearer test-placeholder"
    assert json.loads(request.data) == {
        "model": "deepseek-flash",
        "messages": [{"role": "system", "content": "系统提示"}, {"role": "user", "content": "来源数据"}],
        "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
        "temperature": 0, "max_tokens": 5000, "stream": False,
    }
    assert [record["event"] for record in records] == ["start", "end"]
    assert records[0]["started_at"] == result.started_at
    assert records[1]["ended_at"] == result.ended_at
    assert records[1]["usage"] == result.usage
    assert records[1]["error"] is None and result.elapsed_s >= 0
    assert datetime.fromisoformat(result.started_at).tzinfo is not None
    assert datetime.fromisoformat(result.ended_at) >= datetime.fromisoformat(result.started_at)
    logged = json.dumps(records)
    assert "test-placeholder" not in logged and "Authorization" not in logged
    assert "系统提示" not in logged and "来源数据" not in logged


@pytest.mark.parametrize(("failure", "expected"), [
    (HTTPError("https://example.invalid", 429, "limited", {}, None), "http_429"),
    (HTTPError("https://example.invalid", 503, "failed", {}, None), "http_503"),
    (TimeoutError(), "timeout"),
    (URLError(TimeoutError()), "timeout"),
    (URLError("no connection"), "connection_error"),
    (OSError("disconnected"), "connection_error"),
])
def test_deepseek_errors_are_results_and_logged(monkeypatch, failure, expected):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(llm, "urlopen", fail)
    records = []
    result = DeepSeekClient("test-placeholder", log=records.append).complete_json("", "", 12)
    assert not result.ok and result.error == expected
    assert records[-1]["error"] == expected and records[-1]["ended_at"]


@pytest.mark.parametrize("response", [
    b"not JSON", b"[]", b"{}", b'{"choices":[]}',
    b'{"choices":[{"message":{"content":"[]"}}]}',
    b'{"choices":[{"message":{"content":"broken"}}]}',
    b'{"choices":[{"message":{"content":null}}]}',
])
def test_deepseek_rejects_invalid_envelope_or_json(monkeypatch, response):
    monkeypatch.setattr(llm, "urlopen", lambda *args, **kwargs: io.BytesIO(response))
    result = DeepSeekClient("test-placeholder").complete_json("", "", 12)
    assert not result.ok and result.error == "bad_json"


def test_deepseek_preserves_usage_on_bad_content(monkeypatch):
    envelope = {"choices": [{"message": {"content": "bad"}}], "usage": {"prompt_tokens": 42}}
    monkeypatch.setattr(llm, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(envelope).encode()))
    result = DeepSeekClient("test-placeholder").complete_json("", "", 12)
    assert result.error == "bad_json" and result.usage == {"prompt_tokens": 42}


def test_expired_client_timeout_does_not_open_url(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("过期预算不应发请求")

    monkeypatch.setattr(llm, "urlopen", forbidden)
    assert DeepSeekClient("test-placeholder").complete_json("", "", 0).error == "timeout"


def test_mock_responses_delays_usage_and_exhaustion(monkeypatch):
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", sleeps.append)
    responses = [
        {"company_name": "Aster"},
        {"error": "http_429", "delay_s": 0.5},
        {"content": {"summary": "合成简介"}, "usage": {"prompt_tokens": 9}},
        {"delay_s": 90, "content": {}},
        "bad JSON",
    ]
    client = MockLLMClient(responses)
    assert json.loads(client.complete_json("", "", 10).content) == {"company_name": "Aster"}
    assert client.complete_json("", "", 10).error == "http_429"
    assert client.complete_json("", "", 10).usage == {"prompt_tokens": 9}
    assert client.complete_json("", "", 30).error == "timeout"
    assert client.complete_json("", "", 10).error == "bad_json"
    assert client.complete_json("", "", 10).error == "mock_exhausted"
    assert sleeps == [0.5, 30] and client.calls == 6 and len(responses) == 5


def test_mock_supports_result_replay_and_concurrent_call_order():
    preset = LLMResult(True, '{"summary":"离线回放"}', {"prompt_tokens": 2}, elapsed_s=151)
    result = MockLLMClient([preset]).complete_json("", "", 120)
    assert result.elapsed_s == 151 and result.started_at and result.ended_at
    assert preset.started_at == ""
    client = MockLLMClient([{"index": index} for index in range(12)])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: client.complete_json("", "", 1), range(12)))
    assert sorted(json.loads(result.content)["index"] for result in results) == list(range(12))
    assert client.calls == 12
