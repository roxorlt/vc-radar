"""深度取材的一次搜索；失败不重试，不记录鉴权信息。"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
import time
import unicodedata
from urllib.parse import unquote, urlsplit

from . import config
from .http import Transport

ENDPOINT = "https://api.deepseek.com/v1/tools/search"
SEARCH_PRICE = 0.005


def company_token(name: str) -> str:
    """公司名转成连续字母数字，保留中文等非拉丁文字。"""
    return "".join(char for char in unicodedata.normalize("NFKC", str(name)).casefold() if char.isalnum())


def name_matches(name: str, text: str) -> bool:
    """按词边界匹配公司名，允许空白、标点和连字符写法不同。"""
    token = company_token(name)
    if not token:
        return False
    pattern = r"(?<![^\W_])" + r"[\W_]*".join(re.escape(char) for char in token) + r"(?![^\W_])"
    return re.search(pattern, unicodedata.normalize("NFKC", unquote(str(text))).casefold()) is not None


def company_domain(name: str, url: str) -> bool:
    """检查网址的主机名是否含规范化后的完整公司名。"""
    if not isinstance(url, str):
        return False
    try:
        token = company_token(name)
        return bool(token and token in company_token((urlsplit(url).hostname or "").removeprefix("www.")))
    except ValueError:
        return False


def relevant_results(name: str, results: list[dict]) -> list[dict]:
    """去掉无关或明确属于别家公司的结果，按官网、标题、分数排序。"""
    ranked = []
    for row in results:
        url, title = str(row.get("url") or ""), str(row.get("title") or "")
        text = " ".join(str(row.get(key) or "") for key in ("title", "snippet", "summary", "url"))
        if not (name_matches(name, text) or company_domain(name, url)):
            continue
        try:
            path = unquote(urlsplit(url).path)
        except ValueError:
            continue
        # 公司资料页和单家公司融资 slug 是明确的对象，不靠摘要里顺带提到的名字保留。
        profile = re.search(r"/(?:funding-news|companies|company|organization)/([^/]+)", path, re.I)
        subject = re.match(r"^(.+?)\s+(?:raises?|raised|lands?|secures?|closes?|bags?|snags?|nabs?)\b", title, re.I)
        slug = re.search(r"/([^/]+?)-(?:raises?|raised|lands?|secures?)-", path, re.I)
        if ((profile and not name_matches(name, profile[1]))
                or (subject and not name_matches(name, subject[1]))
                or (slug and not name_matches(name, slug[1]))):
            continue
        try:
            score = float(row.get("rerank_score", 0))
            score = score if math.isfinite(score) else 0.0
        except (ValueError, TypeError, OverflowError):
            score = 0.0
        ranked.append(((company_domain(name, url), name_matches(name, title), score), row))
    return [row for _, row in sorted(ranked, key=lambda item: item[0], reverse=True)]


@dataclass
class SearchResult:
    """空结果也是成功，但不产生搜索费用。"""

    ok: bool
    results: list[dict] = field(default_factory=list)
    error: str | None = None
    attempted: bool = False
    elapsed_s: float = 0.0
    cost: float = 0.0


def funding_query(name: str) -> str:
    """生成不超过 128 字符的固定融资检索式。"""
    name = " ".join(str(name).split()).replace('"', "").strip()
    if not name:
        raise ValueError("公司名不能为空")
    suffix = '" funding OR seed OR "Series A"'
    return '"' + name[:128 - 1 - len(suffix)] + suffix


class SearchClient:
    """通过既有 Transport 的 data 参数发 POST；每次只发一个请求。"""

    def __init__(self, transport: Transport, cfg=None, *, api_key: str | None = None):
        self.transport, self.cfg, self._api_key = transport, cfg, api_key

    def search_company(self, name: str, timeout_s: float = 15.0) -> SearchResult:
        """搜索一家公司的融资报道，非 200、超时或坏响应直接失败。"""
        result = self.search(funding_query(name), timeout_s)
        result.results = relevant_results(name, result.results)
        return result

    def search(self, query: str, timeout_s: float = 15.0) -> SearchResult:
        """发送合法的 1～128 字符查询，只有非空 results 才计费。"""
        if not isinstance(query, str) or not query.strip() or not 1 <= len(query) <= 128:
            return SearchResult(False, error="invalid_query")
        if timeout_s <= 0:
            return SearchResult(False, error="timeout")
        key = self._api_key
        if key is None and self.cfg is not None:
            # 测试入口不读环境或 .env；鉴权占位值只供 FixtureTransport 使用。
            key = "offline-fixture" if self.cfg.test_mode else config.load_api_key(self.cfg)
        if not key:
            return SearchResult(False, error="missing_key")
        started = time.monotonic()
        try:
            response = self.transport.get(
                ENDPOINT, headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                timeout=min(15.0, timeout_s),
                data=json.dumps({"query": query, "count": 5, "need_content": False}, ensure_ascii=False).encode("utf-8"),
            )
            elapsed = max(time.monotonic() - started, response.elapsed_s)
            if response.status != 200 or response.error or elapsed > min(15.0, timeout_s):
                return SearchResult(False, error="request_failed", attempted=True, elapsed_s=elapsed)
            payload = json.loads(response.body)
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
                raise ValueError("bad results")
            return SearchResult(True, results, attempted=True, elapsed_s=elapsed,
                                cost=SEARCH_PRICE if results else 0.0)
        except (OSError, ValueError, TypeError, RuntimeError):
            return SearchResult(False, error="request_failed", attempted=True,
                                elapsed_s=time.monotonic() - started)
