"""robots.txt 检查（任务 A）。按（域名, 北京日期）缓存到 cache_dir，每域名每天读 1 次。
4xx（含 404）= 全部允许；5xx/超时 = 当天该域名全部禁止。Term Sheet 网页版地址是唯一例外（调用方传 exception=True）。"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from threading import Lock, RLock
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .config import UA
from .http import Transport


class _WildcardRule:
    def __init__(self, rule):
        self.path, self.allowance = rule.path, rule.allowance

    def applies_to(self, filename):
        # robotparser 保留通配符为 %2A，补足 /companies?* 这类路径的匹配。
        pattern = re.escape(self.path).replace("%2A", ".*")
        if pattern.endswith("%24"):
            pattern = pattern[:-3] + "$"
        return re.match(pattern, filename) is not None


class RobotsChecker:
    def __init__(self, transport: Transport, cache_dir: Path, today_bj: date):
        self.transport = transport
        self.directory = Path(cache_dir) / "robots" / today_bj.isoformat()
        self._policies: dict[str, RobotFileParser] = {}
        self._locks: dict[str, RLock] = {}
        self._last_request: dict[str, float] = {}
        self._guard = Lock()

    def allowed(self, url: str, exception: bool = False) -> bool:
        """检查当天规则；调用方只对 Term Sheet 的指定地址传例外。"""
        return True if exception else self._policy(url).can_fetch(UA, url)

    def crawl_delay(self, url: str) -> float | None:
        """返回当前 UA 对应的 Crawl-delay 秒数。"""
        return self._policy(url).crawl_delay(UA)

    def request(self, url, send, sleep, exception=False):
        """检查规则并串行执行有间隔要求的同域请求；禁止时返回 None。"""
        if exception:
            return send()
        host = urlsplit(url).netloc.lower()
        if not self.allowed(url):
            return None
        delay = self.crawl_delay(url)
        if delay is None:
            return send()
        with self._host_lock(host):
            if host in self._last_request:
                remaining = delay - (time.monotonic() - self._last_request[host])
                if remaining > 0:
                    sleep(remaining)
            self._last_request[host] = time.monotonic()
            return send()

    def _host_lock(self, host):
        with self._guard:
            return self._locks.setdefault(host, RLock())

    def _policy(self, url):
        parsed = urlsplit(url)
        host = parsed.netloc.lower()
        with self._host_lock(host):
            if host in self._policies:
                return self._policies[host]
            path = self.directory / (re.sub(r"[^\w.-]", "_", host) + ".txt")
            meta_path = path.with_suffix(".json")
            try:
                metadata = json.loads(meta_path.read_text())
                body = path.read_text()
                status = metadata["status"]
            except (OSError, ValueError, KeyError):
                response = self.transport.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt", timeout=30.0)
                status, body = response.status, response.body
                self.directory.mkdir(parents=True, exist_ok=True)
                path.write_text(body, encoding="utf-8")
                meta_path.write_text(json.dumps({"status": status, "error": response.error}), encoding="utf-8")
            policy = RobotFileParser()
            if status is not None and 400 <= status < 500:
                policy.allow_all = True
            elif status is None or not 200 <= status < 300:
                policy.disallow_all = True
            else:
                policy.parse(body.splitlines())
                for entry in [*policy.entries, policy.default_entry]:
                    if entry is not None:
                        entry.rulelines = [_WildcardRule(rule) for rule in entry.rulelines]
            self._policies[host] = policy
            return policy
