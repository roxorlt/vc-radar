"""模型客户端（任务 C）。deepseek-flash：POST https://api.deepseek.com/chat/completions，
thinking={"type":"disabled"}，response_format json_object，temperature 0，max_tokens 5000。
key 只从 config.load_api_key 拿，绝不打印/写日志。"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import threading
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class LLMResult:
    ok: bool
    content: str = ""            # 模型返回的 JSON 文本
    usage: dict | None = None
    error: str | None = None     # "timeout" | "http_429" | "http_<code>" | "bad_json" | ...
    elapsed_s: float = 0.0
    started_at: str = ""
    ended_at: str = ""


class LLMClient(Protocol):
    model: str

    def complete_json(self, system: str, user: str, timeout_s: float) -> LLMResult:
        """在指定超时内完成一次 JSON 调用。"""
        ...


class DeepSeekClient:
    """不附带工具的 JSON 模型客户端；日志只接收调用元数据。"""

    def __init__(self, api_key: str, model: str = "deepseek-flash", log=None):
        self._api_key = api_key
        self.model = model
        self.log = log

    def complete_json(self, system, user, timeout_s) -> LLMResult:
        """调用一次模型，返回 JSON 文本、用量及可审计的起止时间。"""
        started = time.monotonic()
        result = LLMResult(ok=False, started_at=_now())
        self._log("start", result)
        try:
            if timeout_s <= 0:
                result.error = "timeout"
                return result
            body = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "temperature": 0,
                "max_tokens": 5000,
                "stream": False,
            }
            request = Request(
                "https://api.deepseek.com/chat/completions",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Authorization": "Bearer " + self._api_key,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(request, timeout=timeout_s) as response:
                envelope = json.loads(response.read().decode("utf-8"))
            if not isinstance(envelope, dict):
                raise ValueError("bad envelope")
            usage = envelope.get("usage")
            result.usage = dict(usage) if isinstance(usage, dict) else None
            result.content = envelope["choices"][0]["message"]["content"]
            if not isinstance(result.content, str) or not _json_object(result.content):
                result.content = ""
                result.error = "bad_json"
            else:
                result.ok = True
        except HTTPError as exc:
            result.error = f"http_{exc.code}"
        except TimeoutError:
            result.error = "timeout"
        except URLError as exc:
            result.error = "timeout" if isinstance(exc.reason, TimeoutError) else "connection_error"
        except OSError:
            result.error = "connection_error"
        except (ValueError, KeyError, IndexError, TypeError):
            result.error = "bad_json"
        finally:
            result.elapsed_s = time.monotonic() - started
            result.ended_at = _now()
            self._log("end", result)
        return result

    def _log(self, event: str, result: LLMResult) -> None:
        if self.log is not None:
            # 不传异常正文、请求内容或请求头，避免日志意外带出密钥。
            self.log({
                "event": event,
                "model": self.model,
                "started_at": result.started_at,
                "ended_at": result.ended_at,
                "elapsed_s": result.elapsed_s,
                "usage": dict(result.usage or {}),
                "error": result.error,
            })


class MockLLMClient:
    """测试/回放用：按调用次序返回预设结果（可设延迟、失败）。"""

    def __init__(self, responses: list, model: str = "mock"):
        self.responses = list(responses)
        self.model = model
        self.calls = 0
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def complete_json(self, system, user, timeout_s) -> LLMResult:
        """支持 JSON 字典、原始文本、LLMResult 或带 error/delay_s/usage 的回放项。"""
        started = time.monotonic()
        started_at = _now()
        with self._lock:
            index = self.calls
            self.calls += 1
            self.requests.append({"system": system, "user": user, "timeout_s": timeout_s})
            response = self.responses[index] if index < len(self.responses) else {"error": "mock_exhausted"}
        if isinstance(response, LLMResult):
            return replace(response, started_at=started_at, ended_at=_now(),
                           usage=dict(response.usage or {}))
        metadata = response if isinstance(response, dict) else {}
        delay = max(0.0, float(metadata.get("delay_s", 0)))
        timeout = max(0.0, timeout_s)
        if delay:
            time.sleep(min(delay, timeout))
        error = "timeout" if delay > timeout or timeout <= 0 else metadata.get("error")
        usage = metadata.get("usage")
        if isinstance(response, dict):
            content = response.get("content", {
                key: value for key, value in response.items()
                if key not in {"delay_s", "usage", "error"}
            })
        else:
            content = response
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        if not error and not _json_object(content):
            error = "bad_json"
        return LLMResult(
            ok=not bool(error), content=content if not error else "",
            usage=dict(usage) if isinstance(usage, dict) else None, error=error,
            elapsed_s=time.monotonic() - started, started_at=started_at, ended_at=_now(),
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(content: str) -> bool:
    try:
        return isinstance(json.loads(content), dict)
    except (ValueError, TypeError):
        return False
