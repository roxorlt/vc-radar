"""深度取材：复用首条原文、并行抓公开页面，受 robots 和时间预算约束。"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
import html
from html.parser import HTMLParser
import json
from queue import Queue, Empty
import re
from threading import Thread, local
import time
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .config import BJ, UA
from .fetcher import _intercepted_html
from .models import HttpResponse
from .robots import RobotsChecker
from .search import SearchResult, company_domain, relevant_results


class Budget:
    """同时看真实耗时和模拟钟；子预算不会超过父预算。"""

    def __init__(self, clock, seconds: float, parent=None):
        self.clock, self.seconds, self.parent = clock, seconds, parent
        self.started_at, self.started = clock.now(), time.monotonic()

    def elapsed(self) -> float:
        """返回已经消耗的秒数。"""
        return max(0.0, time.monotonic() - self.started,
                   (self.clock.now() - self.started_at).total_seconds())

    def remaining(self) -> float:
        """返回本层和父层共同允许的剩余秒数。"""
        return max(0.0, min(self.seconds - self.elapsed(),
                            self.parent.remaining() if self.parent else float("inf")))


def call_with_timeout(call, timeout_s: float):
    """隔离不守客户端超时的实现；超时后的线程不能写库或日志。"""
    if timeout_s <= 0:
        raise TimeoutError
    result = Queue(maxsize=1)

    def invoke():
        try:
            result.put((True, call()))
        except Exception as exc:
            result.put((False, exc))

    Thread(target=invoke, daemon=True).start()
    try:
        ok, value = result.get(timeout=timeout_s)
    except Empty:
        raise TimeoutError from None
    if not ok:
        raise value
    return value


def normalize_url(value: str, base: str = "") -> str:
    """只保留 http/https，保留路径，移除跟踪参数和片段。"""
    try:
        if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f]", value.strip()):
            return ""
        url = urlsplit(urljoin(base, html.unescape(value.strip())))
        if url.scheme.lower() not in {"http", "https"} or not url.hostname or url.username or url.password:
            return ""
        # 读取 port 也校验格式，拒绝畸形 URL。
        port = url.port
        host = url.hostname.lower()
        netloc = (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "")
        query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {
                     "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "ref_src", "ref_url"}]
        return urlunsplit((url.scheme.lower(), netloc, url.path or "/", urlencode(query), ""))
    except (ValueError, TypeError):
        return ""


def url_key(value: str) -> str:
    """生成比较键：统一主机、www、末尾斜杠、查询参数顺序及跟踪参数。"""
    normalized = normalize_url(value)
    if not normalized:
        return ""
    url = urlsplit(normalized)
    host = url.netloc.removeprefix("www.")
    return urlunsplit((url.scheme, host, url.path.rstrip("/") or "/",
                       urlencode(sorted(parse_qsl(url.query, keep_blank_values=True))), ""))


def fetchable_url(value: str) -> str:
    """排除登录、付费资料站和 LinkedIn；HN 网页改走公开 API。"""
    value = normalize_url(value)
    if not value:
        return ""
    url = urlsplit(value)
    host = url.hostname or ""
    if any(host == domain or host.endswith("." + domain) for domain in (
            "linkedin.com", "crunchbase.com", "pitchbook.com", "tracxn.com", "x.com", "twitter.com")):
        return ""
    if re.search(r"/(?:login|log-in|signin|sign-in|signup|sign-up|register|subscribe|paywall)(?:/|$)", url.path, re.I):
        return ""
    if host == "news.ycombinator.com":
        ident = parse_qs(url.query).get("id", [""])[0]
        return f"https://hn.algolia.com/api/v1/items/{ident}" if ident.isdigit() else ""
    return value


@dataclass
class Page:
    """存入 deepdive_pages 的清洗页面；页码也是模型引用编号。"""

    page_no: int
    url: str
    fetched_at: str
    body: str
    links: list[dict] = field(default_factory=list)
    http_code: int | None = 200
    kind: str = "source"
    footer: str = ""
    website_fields: list[dict] = field(default_factory=list)
    reused: bool = False


def split_sentences(body: str, page_no: int) -> list[dict]:
    """将每页前 8000 字按原文切句，合并短句并给至多 300 字的片段编号。"""
    text = body[:8000]
    protected = set()
    # 简单保留常见缩写和首字母缩写，不改写原文中的标点或大小写。
    for match in re.finditer(r"\b(?:Inc|Corp|Ltd|Co|Mr|Mrs|Ms|Dr|Prof|Jr|Sr)\.|\b(?:[A-Za-z]\.){2,}", text, re.I):
        protected.update(index for index in range(match.start(), match.end()) if text[index] == ".")
    for match in re.finditer(r"https?://[^\s<>\"']+", text):
        end = match.start() + len(match.group().rstrip(".,;:!?)]}。！？；"))
        protected.update(index for index in range(match.start(), end) if text[index] == ".")
    spans, start, index = [], 0, 0

    def append(end):
        nonlocal start
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            spans.append((left, right))
        start = end

    while index < len(text):
        char = text[index]
        if char == "." and (index in protected or (
                index > 0 and index + 1 < len(text)
                and text[index - 1].isdigit() and text[index + 1].isdigit())):
            index += 1
            continue
        if char in ".!?;。！？；\r\n":
            index += 1
            while index < len(text) and text[index] in ".!?;。！？；\"'”’）)]}":
                index += 1
            append(index)
        else:
            index += 1
    append(len(text))
    merged, pending = [], None
    for left, right in spans:
        if pending is not None:
            left, pending = pending, None
        if right - left < 10:
            if merged:
                merged[-1] = (merged[-1][0], right)
            else:
                pending = left
        else:
            merged.append((left, right))
    if pending is not None:
        merged.append((pending, len(text.rstrip())))
    parts = []
    for left, right in merged:
        while right - left > 300:
            # 末段不足十字时从前段让出几个字符，避免产生不能单独引用的碎片。
            size = min(300, right - left - 10)
            parts.append(text[left:left + size])
            left += size
        parts.append(text[left:right])
    return [{"id": f"p{page_no}.s{number}", "text": part}
            for number, part in enumerate(parts, 1)]


@dataclass
class Materials:
    """保留成功正文和所有失败记录；抓取数不含复用页。"""

    pages: list[Page] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    search: SearchResult = field(default_factory=lambda: SearchResult(False, error="not_run"))
    fetch_count: int = 0
    elapsed_s: float = 0.0


class PageTransport:
    """供共享 RobotsChecker 使用，把 robots 请求也限制在 15 秒内。"""

    def __init__(self, transport):
        self.transport, self._local = transport, local()

    def bind(self, budget):
        """为当前抓取线程绑定整家预算。"""
        self._local.budget = budget

    def get(self, url, headers=None, timeout=15.0, data=None):
        """所有取材请求都使用固定 UA，并检查剩余预算。"""
        budget = getattr(self._local, "budget", None)
        remaining = budget.remaining() if budget else 15.0
        if remaining <= 0:
            return HttpResponse(url, None, error="timeout")
        return self.transport.get(url, headers={**(headers or {}), "User-Agent": UA},
                                  timeout=min(15.0, timeout, remaining), data=data)


class _HTML(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.stack = url, []
        self.parts = {key: [] for key in ("body", "main", "article", "entry", "footer")}
        self.links, self.websites, self.scripts = [], [], []
        self.anchor, self.script = None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}:
            self.stack.append((tag, "entry-content" in (attrs.get("class") or "").split()))
        if tag == "a":
            self.anchor = {"url": normalize_url(attrs.get("href") or "", self.url), "text": ""}
        if tag == "script":
            self.script = []
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._break()

    def handle_endtag(self, tag):
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._break()
        if tag == "a" and self.anchor:
            if self.anchor["url"]:
                self.anchor["text"] = " ".join(self.anchor["text"].split())
                self.links.append(self.anchor)
            self.anchor = None
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def _break(self):
        tags = {tag for tag, _ in self.stack}
        if tags & {"script", "style", "noscript", "iframe", "form", "svg", "template", "nav", "header", "footer"}:
            return
        self.parts["body"].append("\n")
        for scope in ("main", "article"):
            if scope in tags:
                self.parts[scope].append("\n")
        if any(entry for _, entry in self.stack):
            self.parts["entry"].append("\n")

    def handle_data(self, data):
        tags = {tag for tag, _ in self.stack}
        if self.script is not None:
            self.script.append(data)
            return
        if tags & {"style", "noscript", "iframe", "form", "svg", "template"}:
            return
        if self.anchor is not None:
            self.anchor["text"] += " " + data
        if "footer" in tags:
            self.parts["footer"].append(data)
        if tags & {"nav", "header", "footer"}:
            return
        self.parts["body"].append(data)
        for scope in ("main", "article"):
            if scope in tags:
                self.parts[scope].append(data)
        if any(entry for _, entry in self.stack):
            self.parts["entry"].append(data)


def clean_page(body: str, url: str, *, hn: bool = False, github: bool = False) -> tuple[str, list[dict], str, list[dict]]:
    """提取正文、外链和页脚地址；不执行脚本，清洗后最多 8000 字。"""
    if hn:
        payload = json.loads(body)
        chunks = []

        def visit(node):
            if not isinstance(node, dict):
                return
            chunks.extend(str(node[k]) for k in ("title", "text") if node.get(k))
            if node.get("url"):
                chunks.append('<a href="' + html.escape(str(node["url"]), quote=True) + '">' +
                              html.escape(str(node.get("title", ""))) + "</a>")
            for child in node.get("children", []) or []:
                visit(child)

        visit(payload)
        body = "\n".join(chunks)
    parser = _HTML(url)
    parser.feed(body)
    chosen = next((parser.parts[key] for key in ("entry", "article", "main", "body") if parser.parts[key]), [])
    # 保留段落和原有换行，供句子编号使用；行内仍统一 HTML 留下的空白。
    text = "\n".join(" ".join(line.split()) for line in " ".join(chosen).splitlines() if line.strip())
    if github:
        # 仓库的目录、issue 和讨论列表不作为产品取材，只留 README 和平台计数。
        visible = " ".join(" ".join(parser.parts["body"]).split())
        if not parser.parts["article"] and not parser.parts["entry"]:
            text = text[text.find("README"):] if "README" in text else ""
        metadata = re.findall(
            r"\b[\d.,kKmM]+\s+stars?\b|\bstars?\s*[:：]?\s*[\d.,kKmM]+|"
            r"\b(?:MIT|Apache(?:[ -]2\.0)?|BSD(?:-[23]-Clause)?|GPL[\w.-]*)\s+licen[cs]e|"
            r"\blicen[cs]e\s*:\s*(?:MIT|Apache(?:[ -]2\.0)?|BSD(?:-[23]-Clause)?|GPL[\w.-]*)", visible, re.I)
        metadata.extend(re.findall(r'"stargazerCount"\s*:\s*\d+', body))
        text = text + "\n" + "\n".join(dict.fromkeys(metadata))
    footer = " ".join(" ".join(parser.parts["footer"]).split())[:1500]
    if footer:
        text = text[:max(0, 8000 - len(footer) - 1)] + "\n" + footer

    def websites(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"website", "website_url"} and isinstance(value, str):
                    found = normalize_url(value)
                    if found:
                        parser.websites.append({"url": found, "text": "website", "field": key})
                elif isinstance(value, (dict, list)):
                    websites(value)
        elif isinstance(node, list):
            for item in node:
                websites(item)

    # YC 的 website 是结构化字段，不能把脚本中的任意网址当作出处。
    if (urlsplit(url).hostname or "").endswith("ycombinator.com"):
        for script in parser.scripts:
            try:
                websites(json.loads(script))
            except (ValueError, TypeError):
                pass
    for found in re.findall(r"https?://[^\s<>\"']+", text):
        found = normalize_url(found.rstrip(".,;:!?)]}。"))
        if found:
            parser.links.append({"url": found, "text": found})
    links = list({(item["url"], item["text"]): item for item in parser.links}.values())
    return text.strip()[:8000], links, footer, parser.websites


def _source_page(source, now):
    url = normalize_url(source.get("url", ""))
    body = source.get("body") or source.get("excerpt") or ""
    text, links, footer, websites = clean_page(body, url)
    structured = source.get("structured", {})
    website = normalize_url(structured.get("website", ""))
    if website:
        websites.append({"url": website, "text": structured.get("company_name", ""), "field": "website"})
    return Page(1, url, source.get("fetched_at") or now.isoformat(), text, links,
                kind="source", footer=footer, website_fields=websites, reused=True)


def _candidates(company, reused):
    sources = company.get("sources", [])
    website = normalize_url(company.get("website") or "")
    home_host = urlsplit(website).hostname
    leads = company.get("leads", [])
    result = []

    def add(url, kind):
        url = fetchable_url(url)
        if url:
            result.append((url, kind))

    for source in sources:
        structured = source.get("structured", {})
        for key in ("press_release", "press_release_url", "news_url", "original_link"):
            if structured.get(key):
                add(structured[key], "press")
    links = reused.links if reused else []
    for link in links:
        url, text = link["url"], link["text"]
        if urlsplit(url).hostname == home_host and re.search(r"/news|/press|/newsroom", urlsplit(url).path, re.I):
            add(url, "press")
    yc_url = next((source["url"] for source in sources if re.search(r"ycombinator\.com/companies/[^/?#]+", source["url"])), "")
    if not yc_url and company.get("yc_batch"):
        slug = next((s.get("structured", {}).get("slug") for s in sources if s.get("structured", {}).get("slug")), None)
        slug = slug or re.sub(r"[^a-z0-9]+", "-", company["name"].lower()).strip("-")
        yc_url = "https://www.ycombinator.com/companies/" + slug
    if yc_url:
        add(yc_url, "yc")
    if website:
        add(website, "home")
    developer = bool(re.search(r"developer|\bsdk\b|\bapi\b|open.source|开发者|开源", company.get("one_liner", "") + " " + (reused.body if reused else ""), re.I))
    for source in sources:
        add(source["url"], "hn") if "news.ycombinator.com/item" in source["url"] else None
    for link in links:
        url, text = link["url"], link["text"]
        host, path = urlsplit(url).hostname or "", urlsplit(url).path
        if any(lead.casefold() in text.casefold() for lead in leads) and re.search(r"/news|/blog|/insights", path, re.I):
            add(url, "investor")
        if host in {"news.ycombinator.com", "hn.algolia.com"}:
            add(url, "hn")
        if developer and host == "github.com" and len(path.strip("/").split("/")) >= 2:
            add(url, "github")
    return result


def _homepage(company, pages, search_rows):
    candidates = []

    def add(url, rank):
        url = fetchable_url(url)
        if url:
            candidates.append((rank, url))

    for source in company.get("sources", []):
        yc = source.get("source_id", "").startswith("yc_") or "ycombinator.com/companies/" in source.get("url", "")
        add(source.get("structured", {}).get("website", ""), 0 if yc else 1)
    add(company.get("website") or "", 1)
    for page in pages:
        yc = page.kind == "yc" or "ycombinator.com/companies/" in page.url
        for website in page.website_fields:
            add(website.get("url", ""), 0 if yc else 1)
        if page.kind in {"source", "press", "yc", "investor"}:
            for link in page.links:
                if company_domain(company["name"], link.get("url", "")):
                    add(link["url"], 2)
    for row in search_rows:
        if company_domain(company["name"], row.get("url", "")):
            add(row["url"], 3)
    return min(candidates, key=lambda item: item[0])[1] if candidates else ""


def _about_candidates(page, transport, robots, budget):
    host = (urlsplit(page.url).hostname or "").removeprefix("www.")
    abouts = {}
    for link in page.links:
        url = fetchable_url(link.get("url", ""))
        if (url and (urlsplit(url).hostname or "").removeprefix("www.") == host
                and re.search(r"about|team|company|people|leadership|关于|团队",
                              unquote(urlsplit(url).path) + " " + link.get("text", ""), re.I)):
            abouts.setdefault(url_key(url), url)
    if abouts:
        return list(abouts.values())[:2]
    # 首页未提供团队入口时，只试第一个 robots 允许的固定路径。
    transport.bind(budget)
    for path in ("/about", "/team", "/company"):
        url = normalize_url(path, page.url)
        if budget.remaining() <= 0:
            break
        if robots.allowed(url):
            return [url]
    return []


def _fetch_page(url, kind, transport, robots, budget, sleep):
    transport.bind(budget)
    records = []

    def pause(seconds):
        if seconds >= budget.remaining():
            raise TimeoutError
        sleep(seconds)

    for attempt in range(2 if kind in {"home", "press"} else 1):
        if budget.remaining() <= 0:
            break
        started = budget.clock.now().astimezone(BJ).isoformat()
        try:
            response = robots.request(url, lambda: transport.get(url, timeout=15), pause)
            if response is None:
                records.append({"url": url, "kind": kind, "fetched_at": started, "http_code": None, "error": "robots"})
                break
            final = fetchable_url(response.final_url or url)
            blocked = (response.status in {401, 403, 429, 451} or _intercepted_html(response)
                       or not final or bool(re.search(r"subscribe to (?:read|continue)|sign in to (?:read|continue)|subscription required|验证您是人类", response.body, re.I)))
            ok = response.status is not None and 200 <= response.status < 300 and not response.error and not blocked
            text, links, footer, websites = clean_page(response.body, final or url, hn=kind == "hn", github=kind == "github") if ok else ("", [], "", [])
            records.append({"url": url, "kind": kind, "fetched_at": started, "http_code": response.status,
                            "chars": len(text), "attempt": attempt + 1, "error": "blocked" if blocked else (None if text else "unreadable")})
            if text and ok:
                return Page(0, final, started, text, links, response.status, kind, footer, websites), records
            if blocked:
                break
        except (OSError, ValueError, TypeError, RuntimeError):
            records.append({"url": url, "kind": kind, "fetched_at": started, "http_code": None, "error": "timeout_or_unreadable"})
    return None, records


def fetch_materials(company: dict, cfg, clock, transport, search_client, *, robots=None, budget=None, sleep=time.sleep) -> Materials:
    """一家至多抓 8 个新网址、搜索 1 次，60 秒后不再等后台抓取。"""
    budget = Budget(clock, 60.0, parent=budget)
    result = Materials()
    transport = transport if isinstance(transport, PageTransport) else PageTransport(transport)
    cache = cfg.state_dir / "test-cache" if cfg.test_mode else cfg.state_dir
    robots = robots or RobotsChecker(transport, cache, clock.now().astimezone(BJ).date())
    reused = _source_page(company["sources"][0], clock.now()) if company.get("sources") else None
    if reused and reused.url and reused.body:
        result.pages.append(reused)
        result.records.append({"url": reused.url, "fetched_at": reused.fetched_at, "http_code": 200,
                               "chars": len(reused.body), "reused": True})
    queue = _candidates(company, reused)
    executor = ThreadPoolExecutor(max_workers=5)
    searching = executor.submit(search_client.search_company, company["name"], min(15.0, budget.remaining()))
    pending, seen = {}, set()
    if reused and reused.body:
        seen.add(url_key(reused.url))
    # 主流程只有短摘录或空摘录时，把首条来源原文页再抓一次全文。
    if reused and not company["sources"][0].get("body") and len(reused.body) < 1500 and fetchable_url(reused.url):
        seen.discard(url_key(reused.url))
        queue.insert(0, (fetchable_url(reused.url), "press"))
    if reused:
        # YC 目录只有摘要；即使 URL 指向公司页，也要抓一次完整公司页。
        if any(kind == "yc" and url_key(url) == url_key(reused.url) for url, kind in queue):
            seen.discard(url_key(reused.url))
            queue = [(url, kind) for url, kind in queue if kind != "press" or url_key(url) != url_key(reused.url)]
    priority = {"press": 0, "yc": 1, "home": 2, "about": 3, "investor": 4, "hn": 5, "github": 6, "search": 7}
    about_discovered = set()

    def discover_about(page):
        key = url_key(page.url)
        if key not in about_discovered:
            about_discovered.add(key)
            queue.extend((url, "about") for url in _about_candidates(page, transport, robots, budget))

    def discover(page=None):
        official = _homepage(company, result.pages, result.search.results)
        queue[:] = [(url, kind) for url, kind in queue if kind != "home" or url_key(url) == url_key(official)]
        queue.extend(_candidates({**company, "website": official}, page or reused))
        return official

    discover()
    try:
        while (queue or pending or searching) and budget.remaining() > 0:
            queue.sort(key=lambda item: priority[item[1]])
            while queue and len(pending) < 4 and result.fetch_count < 8:
                url, kind = queue.pop(0)
                key = url_key(url)
                if key in seen:
                    if kind == "home" and not any(info[2] == key for info in pending.values()):
                        home = next((page for page in result.pages if url_key(page.url) == key), None)
                        if home:
                            discover_about(home)
                    continue
                if (kind == "home" and any(info[1] in {"press", "yc"} for info in pending.values())):
                    queue.insert(0, (url, kind))
                    break
                if (result.fetch_count >= 6 and priority[kind] > priority["about"]
                        and (searching or any(info[1] == "home" for info in pending.values()))):
                    # 首页尚未返回时给 about/team 留两个名额。
                    queue.insert(0, (url, kind))
                    break
                seen.add(key)
                result.fetch_count += 1
                future = executor.submit(_fetch_page, url, kind, transport, robots, budget, sleep)
                pending[future] = (result.fetch_count + (1 if reused and reused.body else 0), kind, key)
            waiting = set(pending) | ({searching} if searching else set())
            if not waiting:
                break
            done, _ = wait(waiting, timeout=budget.remaining(), return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                if future is searching:
                    try:
                        result.search = future.result()
                    except Exception:
                        result.search = SearchResult(False, error="request_failed", attempted=True)
                    searching = None
                    result.search.results = relevant_results(company["name"], result.search.results)
                    search_urls = {}
                    for row in result.search.results:
                        url = fetchable_url(row.get("url", ""))
                        if url:
                            search_urls.setdefault(url_key(url), url)
                    # 去重后再取前两条；官网搜索结果也占这两个名额。
                    picked = list(search_urls.values())[:2]
                    result.search.results = [row for row in result.search.results if fetchable_url(row.get("url", "")) in picked]
                    official = discover()
                    queue.extend((url, "home" if url_key(url) == url_key(official) else "search") for url in picked
                                 if not company_domain(company["name"], url) or url_key(url) == url_key(official))
                    continue
                page_no, _, requested_key = pending.pop(future)
                try:
                    page, records = future.result()
                except Exception:
                    page, records = None, []
                result.records.extend(records)
                if page:
                    key = url_key(page.url)
                    old = next((stored for stored in result.pages if url_key(stored.url) in {key, requested_key}), None)
                    if old and not old.reused:
                        seen.add(key)
                        continue
                    page.page_no = old.page_no if old else page_no
                    if old:
                        page.website_fields.extend(old.website_fields)
                        page.links.extend(link for link in old.links if link not in page.links)
                        result.pages[result.pages.index(old)] = page
                    else:
                        result.pages.append(page)
                    seen.add(key)
                    # 首页、新闻稿和 YC 页新给出的链接也属于来源；不凭名字猜投资方网址。
                    discover(page)
                    if page.kind == "home":
                        discover_about(page)
        if searching:
            if searching.done():
                try:
                    result.search = searching.result()
                except Exception:
                    result.search = SearchResult(False, error="request_failed", attempted=True)
            else:
                result.search = SearchResult(False, error="timeout", attempted=True)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    result.pages.sort(key=lambda page: page.page_no)
    result.elapsed_s = budget.elapsed()
    return result
