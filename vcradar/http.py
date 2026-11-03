"""HTTP 抓取（任务 A）。固定 UA（config.UA），单地址超时 30 秒，4xx 放行给调用方判断。

fixture 模式：FixtureTransport 按 URL 返回本地文件内容，可设状态码、内容类型、延迟（PRD P0-15 ①⑥）。
所有请求经 Transport 记一行到 request_log（防注入验收 13-#29：除了清单里的抓取没有别的请求）。
"""
from __future__ import annotations

import gzip
from hashlib import sha1
import json
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Optional, Protocol
from urllib.parse import urlsplit

from .config import BJ, UA
from .models import HttpResponse


class Transport(Protocol):
    def get(self, url: str, headers: Optional[dict[str, str]] = None, timeout: float = 30.0,
            data: Optional[bytes] = None) -> HttpResponse: ...


class UrllibTransport:
    """真实网络（urllib）。UA 固定 config.UA。"""
    def __init__(self, raw_dir: Optional[Path] = None):
        self.raw_dir = Path(raw_dir) if raw_dir is not None else None
        self.request_log: list[dict] = []
        self._lock = Lock()
        self._raw_counts: dict[tuple[str, str], int] = {}

    def get(self, url, headers=None, timeout=30.0, data=None) -> HttpResponse:
        """发送 GET 或带 data 的 POST，保留 HTTP 错误的状态和正文。"""
        request_headers = _headers(headers)
        with self._lock:
            self.request_log.append(_log_entry(url, request_headers, data))
        started = time.monotonic()
        response = HttpResponse(url, None)
        request = urllib.request.Request(url, headers=request_headers, data=data)
        try:
            try:
                handle = urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                handle = exc
            with handle:
                payload = handle.read()
                if "gzip" in handle.headers.get("Content-Encoding", "").lower():
                    payload = gzip.decompress(payload)
                charset = handle.headers.get_content_charset() or "utf-8"
                try:
                    body = payload.decode(charset, errors="replace")
                except LookupError:
                    body = payload.decode("utf-8", errors="replace")
                response = HttpResponse(url, handle.code, handle.headers.get("Content-Type", ""),
                                        body, final_url=handle.geturl())
        except (TimeoutError, socket.timeout):
            response.error = "timeout"
        except urllib.error.URLError as exc:
            response.error = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "conn:unreachable"
        except (OSError, EOFError, ValueError):
            response.error = "conn:read failed"
        response.elapsed_s = time.monotonic() - started
        if self.raw_dir is not None:
            source_id = _source_id(url)
            if source_id is not None:
                today = datetime.now(BJ).date()
                with self._lock:
                    key = (str(today), source_id)
                    number = self._raw_counts.get(key, 0) + 1
                    self._raw_counts[key] = number
                save_raw_response(response, self.raw_dir, source_id, number, datetime.now(BJ))
        return response


class FixtureTransport:
    """fixture 目录里 manifest.json：{"<url 或 url 前缀>": {"file": "x.xml", "status": 200,
    "content_type": "...", "delay_s": 0, "sequence": [ {...}, {...} ]}}。sequence 按调用次序依次返回
    （Term Sheet 403 → 重试 200 等）。没配的 URL 返回 status=None,error="conn:no fixture"。"""
    def __init__(self, fixture_dir: Path):
        self.fixture_dir = Path(fixture_dir)
        self.manifest = json.loads((self.fixture_dir / "manifest.json").read_text())
        self.request_log: list[dict] = []
        self._counts: dict[str, int] = {}
        self._lock = Lock()

    def get(self, url, headers=None, timeout=30.0, data=None) -> HttpResponse:
        """返回最长 URL 前缀对应的合成响应；序列用尽后保留末项。"""
        with self._lock:
            self.request_log.append(_log_entry(url, _headers(headers), data))
            matches = [key for key in self.manifest if url.startswith(key)]
            if not matches:
                return HttpResponse(url, None, error="conn:no fixture")
            key = max(matches, key=len)
            spec = dict(self.manifest[key])
            sequence = spec.pop("sequence", None)
            if sequence:
                count = self._counts.get(key, 0)
                self._counts[key] = count + 1
                current = sequence[min(count, len(sequence) - 1)]
                if "body" in current and "file" not in current:
                    spec.pop("file", None)
                spec.update(current)
        delay = max(0.0, float(spec.get("delay_s", 0)))
        if delay:
            time.sleep(min(delay, max(0.0, timeout)))
        if delay > timeout:
            return HttpResponse(url, None, elapsed_s=max(0.0, timeout), error="timeout")
        body = spec.get("body", "")
        if spec.get("file"):
            body = (self.fixture_dir / spec["file"]).read_text(encoding="utf-8")
        return HttpResponse(url, spec.get("status", 200), spec.get("content_type", ""), body,
                            delay, spec.get("error"), spec.get("final_url", url))


class FixedPageTransport:
    """读取 SHA1 命名的固定页面及 meta；只用注入时钟模拟延迟，不联网或睡眠。"""

    def __init__(self, directory: Path, clock):
        self.directory, self.clock = Path(directory), clock
        self.request_log: list[dict] = []

    def get(self, url, headers=None, timeout=15.0, data=None) -> HttpResponse:
        """单页上限十五秒，出错或超时不返回正文。"""
        self.request_log.append(_log_entry(url, _headers(headers), data))
        stem = self.directory / sha1(url.encode("utf-8")).hexdigest()
        try:
            meta_path = stem.with_suffix(".meta.json")
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            delay = max(0.0, float(meta.get("delay_s", meta.get("delay", 0))))
            limit = max(0.0, min(15.0, timeout))
            status = meta.get("status", 200)
            self.clock.advance(limit if status == "timeout" else min(delay, limit))
            if status == "timeout" or delay > limit:
                return HttpResponse(url, None, elapsed_s=limit, error="timeout")
            body = stem.with_suffix(".html").read_text(encoding="utf-8") if 200 <= int(status) < 300 else ""
            return HttpResponse(url, int(status), "text/html", body, delay, final_url=url)
        except (OSError, ValueError, TypeError):
            return HttpResponse(url, None, error="conn:no fixture")


def redact_keys(body: str) -> str:
    """落盘前遮盖公开搜索 key；日志也使用相同规则。"""
    return re.sub(r'''(?i)(["']?(?:key|api[_-]?key|X-Algolia-API-Key)["']?\s*[:=]\s*)(["'])(.*?)(\2)''',
                  lambda m: m[1] + m[2] + "<key>" + m[2], body)


def save_raw_response(response: HttpResponse, raw_dir, source_id: str, page: int, now: datetime) -> None:
    """按北京日期保存脱敏响应；第一页不带页码。"""
    if raw_dir is None:
        return
    directory = Path(raw_dir) / now.astimezone(BJ).date().isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    suffix = f"-p{page}" if page > 1 else ""
    (directory / f"{source_id}{suffix}.out").write_text(redact_keys(response.body), encoding="utf-8")


def _headers(headers):
    result = {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}
    result["User-Agent"] = UA
    if not any(key.lower() == "accept" for key in result):
        result["Accept"] = "*/*"
    result.setdefault("Accept-Encoding", "gzip")
    return result


def _log_entry(url, headers, data):
    safe_headers = {k: "<key>" if k.lower() in {"x-algolia-api-key", "authorization", "cookie"} else v
                    for k, v in headers.items()}
    return {"url": url, "method": "POST" if data is not None else "GET", "headers": safe_headers,
            "data": redact_keys(data.decode("utf-8", errors="replace")) if data is not None else None}


def _source_id(url):
    from .sources.registry import P0_SOURCES
    parsed = urlsplit(url)
    if parsed.path == "/robots.txt":
        return None
    for src in sorted(P0_SOURCES, key=lambda s: len(s.url), reverse=True):
        if url.startswith(src.url):
            return src.id
    if parsed.hostname == "content.fortune.com" and parsed.path == "/newsletter/termsheet/":
        return "ts_web"
    if parsed.hostname == "newsletter.strictlyvc.com" and parsed.path.startswith("/p/"):
        return "strictlyvc"
    if parsed.hostname and parsed.hostname.endswith("-dsn.algolia.net"):
        return "yc_directory"
    return None
