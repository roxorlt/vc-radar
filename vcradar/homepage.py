"""PRD 7.5 官网：只用来源事实核实候选，不调用模型。"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import fcntl
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from . import scan, verify
from .config import BJ, Config, UA, effective_links_file
from .deep_fetch import Budget, PageTransport, call_with_timeout
from .http import UrllibTransport
from .render import official_links, read_link_records
from .robots import RobotsChecker
from .search import name_matches


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _stamp(value):
    return value.astimezone(BJ).strftime("%Y-%m-%d %H:%M:%S")


def load_policy(cfg: Config) -> dict:
    """读取版本化配置；非法参数作为配置错误处理。"""
    policy = json.loads(Path(cfg.homepage_check_file).read_text(encoding="utf-8"))
    for key in ("daily_search_limit", "recheck_days", "step_timeout", "company_timeout", "page_timeout",
                "concurrency", "max_candidates", "max_result_pages", "company_name_body_chars", "keyword_min_letters", "keyword_limit"):
        if not isinstance(policy[key], (int, float)) or policy[key] < 0:
            raise ValueError("官网配置无效")
    for pattern in policy["parking_patterns"]:
        re.compile(pattern, re.I)
    return policy


def fixture_url(value: str) -> str:
    """P0-15 固定页键：只去两端空白、片段并小写 host，其余原样保留。"""
    try:
        value = value.strip().split("#", 1)[0]
        if re.search(r"[\x00-\x20\x7f]", value):
            return ""
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        parsed.port
        host = parsed.hostname
        # 替换 host 的原始字符，保留端口、路径及查询字符串的原始编码。
        netloc = parsed.netloc.lower()
        return value[:value.index(":") + 3] + netloc + value[value.index(":") + 3 + len(parsed.netloc):]
    except (ValueError, TypeError, AttributeError):
        return ""


def _home(value):
    value = fixture_url(value)
    if not value:
        return ""
    host = urlsplit(value).hostname.lower()
    return "https://" + ("[" + host + "]" if ":" in host else host) + "/"


def _excluded(url, policy):
    value = fixture_url(url)
    if not value:
        return True
    parsed = urlsplit(value)
    host = parsed.hostname.lower()
    if any(host == d or host.endswith("." + d) for d in policy["exclude_domains"] + policy["exclude_hosts"]):
        return True
    return any((host == d or host.endswith("." + d)) and any(parsed.path.startswith(p) for p in paths)
               for d, paths in policy["exclude_paths"].items())


def domain_matches(name: str, url: str, text: str = "") -> bool:
    """公司域名包含名称键，或满足短域名前缀规则，或锚文字精确等于公司名。"""
    domain = verify.registered_domain(url)
    suffix = max((s for s in verify.SUFFIXES if domain.endswith("." + s)), key=len, default="")
    label = domain[:-(len(suffix) + 1)] if suffix else domain.rsplit(".", 1)[0]
    label, key = label.replace("-", "").casefold(), scan.name_key(name).replace(" ", "")
    return bool(key and (key in label or (len(label) >= 4 and key.startswith(label) and len(label) >= len(key) * .6))
                or text.strip().casefold() == name.strip().casefold())


class _HTML(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url, self.stack, self.body, self.scopes = url, [], [], set()
        self.title, self.h1, self.meta, self.links = [], [], {}, []
        self.first_h1, self.anchor = False, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.scopes.add(tag)
        if tag == "meta":
            self.meta[(attrs.get("property") or attrs.get("name") or "").casefold()] = attrs.get("content", "")
        if tag not in {"meta", "link", "br", "img", "hr", "input", "source", "wbr", "area", "base", "embed", "param", "col"}:
            self.stack.append(tag)
        if tag == "h1" and not self.first_h1:
            self.first_h1 = True
            self.h1_active = True
        if tag == "a":
            self.anchor = {"url": fixture_url(urljoin(self.url, attrs.get("href", ""))), "parts": [],
                           "article": "article" in self.stack, "main": "main" in self.stack,
                           "visible": not set(self.stack) & {"header", "footer", "nav", "script", "style", "noscript"}}
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.body.append("\n")

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor:
            self.anchor["text"] = " ".join(" ".join(self.anchor.pop("parts")).split())
            if self.anchor["url"] and self.anchor["visible"]:
                self.links.append(self.anchor)
            self.anchor = None
        if tag == "h1":
            self.h1_active = False
        if tag in {"p", "div", "li", "h1", "h2", "h3"}:
            self.body.append("\n")
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        if "title" in self.stack:
            self.title.append(data)
        if set(self.stack) & {"script", "style", "noscript", "svg", "template", "iframe"}:
            return
        if getattr(self, "h1_active", False):
            self.h1.append(data)
        if self.anchor:
            self.anchor["parts"].append(data)
        if not set(self.stack) & {"head"}:
            self.body.append(data)


def page_parts(body: str, url: str) -> dict:
    """标准库提取标题、首个 h1、正文和正文范围内的外链。"""
    parser = _HTML(url)
    parser.feed(body)
    links = parser.links
    if "article" in parser.scopes:
        links = [link for link in links if link["article"]]
    elif "main" in parser.scopes:
        links = [link for link in links if link["main"]]
    text = "\n".join(" ".join(line.split()) for line in " ".join(parser.body).splitlines() if line.strip())[:8000]
    return {"title": " ".join(parser.title), "h1": " ".join(parser.h1), "meta": parser.meta,
            "body": text, "links": [{"url": link["url"], "text": link["text"]} for link in links]}


def _candidates(links, context, policy, origin, *, source_url=None):
    found, domains = [], set()
    source_domain = verify.registered_domain(source_url or context["source_url"])
    for link in links:
        url = link.get("url", "")
        domain = verify.registered_domain(url)
        if (not domain or domain == source_domain or domain in domains or _excluded(url, policy)
                or not domain_matches(context["company"]["name"], url, link.get("text", ""))):
            continue
        found.append({"url": url, "from": origin})
        domains.add(domain)
        if len(found) >= min(2, policy["max_candidates"]):
            break
    return found


def company_context(conn, cid, cfg, *, event_id=None, source_bodies=None) -> dict | None:
    """读取信源事实和首条来源，不使用上次搜索结果扩展已知事实。"""
    context = verify.company_context(conn, cid, cfg, event_id=event_id)
    if context is None:
        return None
    records = sorted(context["records"], key=scan._source_order)
    first = records[0] if records else None
    source_url = first["url"] if first else ""
    bodies = {fixture_url(k): v for k, v in (source_bodies or {}).items()}
    source_body = bodies.get(fixture_url(source_url), "")
    if not source_body and first:
        source_body = bodies.get(fixture_url(first["item"].structured.get("issue_url", "")), "")
    source_links = first["item"].structured.get("_homepage_links", []) if first else []
    if source_body:
        source_links = page_parts(source_body, source_url)["links"]
        if first:
            structured = {**first["item"].structured, "_homepage_links": source_links}
            conn.execute("UPDATE raw_item SET structured_json=? WHERE id=?", (_json(structured), first["raw_item_id"]))
    fields = []
    for record in records:
        if record["item"].source_id == "yc_directory":
            origin = "YC 目录"
        elif record["source_name"].casefold() == "crunchbase news":
            origin = "Crunchbase News"
        else:
            continue
        url = record["item"].structured.get("website") or record["ext"].get("website")
        if url:
            fields.append({"url": url, "from": origin})
    return {**context, "records": records, "source_url": source_url, "source_body": source_body,
            "source_links": source_links, "website_fields": fields}


def _facts(context, policy, source_body):
    company, event = context["company"], context["event"]
    investors = [i for i in context["investors"] if len(i.strip()) > 3]
    texts = [company.get("one_liner", "")]
    products = list(dict.fromkeys([*context.get("terms", []), *json.loads(company.get("product_terms_json", "[]"))]))
    texts.extend(products)
    for record in context["records"]:
        texts += [record["raw"]["excerpt"], record["ext"].get("one_liner", "")]
        texts += list(record["ext"].get("quotes", {}).values())
    source = page_parts(source_body, context["source_url"])["body"] if source_body else ""
    texts += [sentence for sentence in re.split(r"(?<=[.!?。])\s*|\n+", source) if name_matches(company["name"], sentence)]
    excluded = set(policy["stop_words"])
    for value in [company["name"], company.get("hq") or "", *json.loads(event["leads_json"]), *json.loads(event["follows_json"])]:
        excluded.update(re.findall(r"[a-z]+", str(value).casefold()))
    keywords = []
    for text in texts:
        for word in re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*", str(text)):
            word = word.casefold()
            if (len(word) >= policy["keyword_min_letters"] and word not in excluded
                    and not re.search(r"-(based|old|led|backed)$", word) and word not in keywords):
                keywords.append(word)
    # 金额直接匹配原币数值的常见写法，不换算、不借用其他币种的等值金额。
    amounts = []
    source_amount = event if context.get("frozen_amount") or not context["records"] else next(
        (r["ext"]["_amount_fx"] for r in context["records"] if r["ext"].get("_amount_fx", {}).get("amount_original") is not None), {})
    value = source_amount.get("amount_original")
    currency = source_amount.get("amount_currency")
    if value is not None and currency and event["amount_status"] != "金额待确认":
        number = Decimal(str(value))
        for unit, aliases in ((Decimal(1_000_000_000), ("billion", "b", "bn")),
                              (Decimal(1_000_000), ("million", "m", "mn")), (Decimal(1000), ("thousand", "k"))):
            if number >= unit:
                short = format(number / unit, "f").rstrip("0").rstrip(".") if number / unit % 1 else str(int(number / unit))
                amounts.extend(short + (" " if len(alias) > 2 else "") + alias for alias in aliases)
        amounts.append(str(int(number)) if number % 1 == 0 else str(number))
    return {"investors": investors, "amounts": amounts, "currency": currency,
            "keywords": keywords[:int(policy["keyword_limit"])], "products": products}


def _whole(word, text):
    return re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text, re.I) is not None


def _evaluate(context, candidate, response, facts, policy):
    final = response.get("final_url") or candidate["url"]
    result = {"candidate_from": candidate["from"], "candidate_url": candidate["url"], "final_url": final,
              "http": response.get("status"), "matched_facts": [], "weak": False}
    if response.get("error") or not isinstance(response.get("status"), int) or not 200 <= response["status"] < 300:
        error = response.get("error")
        reason = "测试跳过" if error == "测试跳过" else "robots 禁止" if error in {"robots", "robots 禁止"} else "超时" if error == "timeout" else f"HTTP {response.get('status')}"
        return {**result, "result": "未核实", "reason": reason}
    if _excluded(final, policy):
        return {**result, "result": "未核到", "reason": "排除名单域名"}
    parts = page_parts(response.get("body", ""), final)
    meta = parts["meta"]
    head = " ".join([parts["title"], meta.get("og:site_name", ""), meta.get("og:title", ""), parts["h1"], parts["body"][:int(policy["company_name_body_chars"])]])
    full = " ".join([parts["title"], meta.get("og:title", ""), meta.get("description", ""), meta.get("og:description", ""), parts["h1"], parts["body"]])
    if any(re.search(pattern, full + "\n" + response.get("body", ""), re.I) for pattern in policy["parking_patterns"]):
        return {**result, "result": "未核到", "reason": "停放页或出售页"}
    if not name_matches(context["company"]["name"], head):
        return {**result, "result": "未核到", "reason": "公司名没对上"}
    strong = ["投资方：" + investor for investor in facts["investors"] if _whole(investor, full)]
    for amount in facts["amounts"]:
        # 不把 100m 命中为 1100m，也不把 $12M 当 €12M 的强证据。
        pattern = r"(?<![\d.,])" + re.escape(amount).replace(r"\ ", r"\s+") + r"(?!\w|[.,]\d)"
        for match in re.finditer(pattern, full, re.I):
            prefix = full[max(0, match.start() - 4):match.start()].rstrip()
            symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(facts["currency"])
            if prefix.endswith(("$", "€", "£")) and symbol and not prefix.endswith(symbol):
                continue
            strong.append("金额：" + amount)
            break
        if any(hit.startswith("金额：") for hit in strong):
            break
    weak_words = set()
    for term in [*facts["products"], *facts["keywords"]]:
        # 同一个词在产品短语和关键词中出现两次，只算一个；不同短语共享的词也只算一次。
        for word in re.findall(r"[A-Za-z]+(?:-[A-Za-z]+)*|[\u4e00-\u9fff]+", term):
            if word.casefold() not in set(policy["stop_words"]) and _whole(word, full):
                weak_words.add(word.casefold())
    matched = strong + ["关键词：" + word for word in sorted(weak_words)]
    if not strong and len(weak_words) < 2:
        return {**result, "matched_facts": matched, "result": "未核到",
                "reason": "只命中 1 个关键词" if weak_words else "只对上公司名"}
    return {**result, "result": "核到", "reason": "", "matched_facts": matched,
            "weak": not bool(strong), "final_url": _home(final)}


class FixtureServices(verify.FixtureServices):
    """官网只用固定搜索及原始 HTML；状态、跳转、robots 和延迟均可离线注入。"""

    def __init__(self, path, clock):
        self.path, self.clock = Path(path) if path else None, clock
        self.search_specs, self.requests, self.pages = [], [], []
        if self.path and self.path.exists():
            super().__init__(path, clock)

    def fork(self, clock):
        """各并发槽使用自己的模拟钟，共享可检查的请求记录。"""
        child = FixtureServices(None, clock)
        child.path, child.search_specs, child.requests, child.pages = self.path, self.search_specs, self.requests, self.pages
        return child

    def page(self, url, timeout_s):
        """按规范化完整网址 sha1 读页；缺页绝不访问网络。"""
        self.pages.append(url)
        root = self.path if self.path and self.path.is_dir() else self.path.parent if self.path else None
        key = hashlib.sha1(fixture_url(url).encode()).hexdigest()
        path = root / "verify-pages" / (key + ".html") if root else None
        meta = path.with_suffix(".meta.json") if path else None
        spec = json.loads(meta.read_text()) if meta and meta.is_file() else {}
        if not (path and path.is_file()) and not spec:
            return {"status": None, "error": "测试跳过"}
        timed_out = self._delay(spec, timeout_s)
        error = "timeout" if timed_out or spec.get("status") == "timeout" else spec.get("error")
        if spec.get("robots") is False or spec.get("robots_allowed") is False:
            error = "robots 禁止"
        return {"status": None if timed_out else spec.get("status", 200), "error": error,
                "final_url": spec.get("final_url", url), "body": path.read_text() if path and path.is_file() else ""}


class LiveServices:
    """固定 UA、共享 robots 缓存，单次抓取不重试；搜索复用现有适配器。"""

    def __init__(self, cfg, clock, *, transport=None, search_service=None):
        self.clock, self.cfg = clock, cfg
        self.transport = PageTransport(transport or UrllibTransport())
        self.search_service = search_service or verify.LiveServices(cfg, clock, transport=transport)
        self.robots = RobotsChecker(self.transport, cfg.state_dir / "cache", clock.now().date())

    def search(self, query, timeout_s):
        """一次搜索，不调用模型。"""
        return self.search_service.search(query, timeout_s)

    def page(self, url, timeout_s):
        """守 robots、单页预算和抓取间隔，返回原始 HTML 供程序核实。"""
        budget = Budget(self.clock, min(15, timeout_s))
        self.transport.bind(budget)
        def pause(seconds):
            if seconds >= budget.remaining():
                raise TimeoutError
            time.sleep(seconds)
        response = self.robots.request(url, lambda: self.transport.get(url, timeout=budget.remaining()), pause)
        if response is None:
            return {"status": None, "error": "robots 禁止"}
        return {"status": response.status, "error": "timeout" if response.elapsed_s > timeout_s else response.error,
                "body": response.body, "final_url": response.final_url or url}


class _Quota:
    def __init__(self, count):
        self.remaining, self.lock = max(0, count), Lock()

    def take(self):
        with self.lock:
            if self.remaining <= 0:
                return False
            self.remaining -= 1
            return True


def _deadline(budget, step):
    return "整步到点" if step.remaining() <= .001 else "单家到点" if budget.remaining() <= .001 else ""


def _page(services, url, budget, policy):
    timeout = min(15, policy["page_timeout"], budget.remaining())
    try:
        return call_with_timeout(lambda: services.page(url, timeout), timeout)
    except TimeoutError:
        return {"status": None, "error": "timeout"}
    except Exception:
        return {"status": None, "error": "HTTP"}


def check_company(context, cfg, clock, services, policy, budget, step, quota, *, previous=None,
                  allow_source_fetch=False, journal=None):
    """依次尝试来源外链、官网字段、一次搜索及报道页，只返回内存结果。"""
    previous = previous or {}
    output = journal if journal is not None else {}
    retry = previous.get("result") == "未核实"
    previous_candidates = {c["url"]: c for c in previous.get("candidates", []) if c.get("url")} if retry else {}
    retry_candidates = [{"url": c["url"], "from": c["from"]} for c in sorted(previous_candidates.values(),
        key=lambda c: c.get("reason") not in {"单家到点", "整步到点"}) if c.get("result") == "未核实"]
    if retry and previous.get("candidate_url") and not any(c["url"] == previous["candidate_url"] for c in retry_candidates):
        retry_candidates.append({"url": previous["candidate_url"], "from": previous["candidate_from"]})
    completed = {url for url, c in previous_candidates.items() if c.get("result") == "未核到"}
    if retry:
        output.update({k: previous[k] for k in ("candidate_url", "candidate_from", "final_url", "http", "matched_facts", "weak") if k in previous})
    output.update(searches=[], candidates=list(deepcopy(previous_candidates).values()),
                  search_results=deepcopy(previous.get("search_results", [])), searched=bool(previous.get("searched")),
                  source_ids=context["source_ids"])

    def finish(result="未核到", reason="搜索没找到", **values):
        output.update(result=result, reason=reason, **values, checked_at=_stamp(clock.now()))
        output["first_unverified_date"] = (previous.get("first_unverified_date") or str(clock.now().date())) if result == "未核实" else None
        return output

    if reason := _deadline(budget, step):
        return finish("未核实", reason)

    source_body = context["source_body"]
    source_links = context["source_links"]
    if (not source_body and context["source_url"] and not retry_candidates
            and (allow_source_fetch or isinstance(services, FixtureServices))):
        response = _page(services, context["source_url"], budget, policy)
        if isinstance(response.get("status"), int) and 200 <= response["status"] < 300 and not response.get("error"):
            source_body = response.get("body", "")
            source_links = page_parts(source_body, response.get("final_url") or context["source_url"])["links"]
    if reason := _deadline(budget, step):
        return finish("未核实", reason)
    facts = _facts(context, policy, source_body)
    if retry and not source_body:
        cached = next((c["facts"] for c in previous_candidates.values() if c.get("facts")), {})
        facts["keywords"] = list(dict.fromkeys([*facts["keywords"], *cached.get("keywords", [])]))[:int(policy["keyword_limit"])]
    opened = {}

    def attempt(candidates):
        responses = []
        for candidate in candidates:
            if reason := _deadline(budget, step):
                result = {"result": "未核实", "reason": reason, "candidate_from": candidate["from"], "candidate_url": candidate["url"]}
            else:
                parsed = urlsplit(candidate["url"])
                home = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
                response = opened.get(home)
                if response is None:
                    response = _page(services, home, budget, policy)
                    opened[home] = response
                result = _evaluate(context, candidate, response, facts, policy)
                if reason := _deadline(budget, step):
                    result.update(result="未核实", reason=reason)
            output["candidates"].append({"url": candidate["url"], "from": candidate["from"], "facts": facts, **result})
            responses.append(result)
            if result["result"] == "核到":
                return finish(**result)
            if result["reason"] in {"整步到点", "单家到点"}:
                break
        chosen = next((r for r in responses if r["reason"] in {"单家到点", "整步到点"}), None)
        chosen = chosen or next((r for r in responses if r["result"] == "未核实"), responses[-1] if responses else {})
        return finish(**chosen) if chosen else finish()

    if retry_candidates:
        # 重核不再读取其他候选，也不重新搜索。
        return attempt(retry_candidates)
    candidates = _candidates(source_links, context, policy, "来源页")
    if candidates:
        return attempt(candidates)
    fields = [c for c in context["website_fields"] if fixture_url(c["url"]) and not _excluded(c["url"], policy)]
    if fields:
        return attempt(fields[:int(min(2, policy["max_candidates"]))])
    words = list(dict.fromkeys([*facts["products"][:3], *facts["keywords"][:3], *facts["investors"][:1]]))
    if not words:
        return finish(reason=verify.NO_IDENTITY)
    if reason := _deadline(budget, step):
        return finish("未核实", reason)
    results = previous.get("search_results", [])
    if not output["searched"]:
        if isinstance(services, FixtureServices) and services.path is None:
            return finish("未核实", "测试跳过")
        if not quota.take():
            return finish("未核实", "超当天上限")
        name = context["company"]["name"].replace('"', "").strip()[:50]
        words = " ".join(words).replace('"', "").strip()[:74]
        query = policy["query_template"].format(name=name, context=words)
        if not name_matches(name, query) or words not in query:
            query = f'"{name}" {words}'
        entry = {"requested_at": _stamp(clock.now()), "query": query[:128], "status": "timeout", "results": [],
                 "result_count": 0, "billed": False, "cost": 0, "fields": [], "conclusion": {}, "model_calls": []}
        output["searches"].append(entry)
        output["searched"] = True
        timeout = min(15, policy["page_timeout"], budget.remaining())
        try:
            response = call_with_timeout(lambda: services.search(entry["query"], timeout), timeout)
            status, results = response.get("status", 200), response.get("results", [])
            if not isinstance(results, list) or any(not isinstance(r, dict) for r in results):
                status, results = 500, []
        except TimeoutError:
            status, results = "timeout", []
        except Exception:
            status, results = 500, []
        ok = str(status) == "200"
        results = results if ok else []
        entry.update(status=status, results=results, result_count=len(results), billed=bool(results),
                     cost=cfg.verify_search_price if results else 0)
        output["search_results"] = results
        if reason := _deadline(budget, step):
            return finish("未核实", reason)
        if not ok:
            return finish("未核实", "超时" if str(status) in {"timeout", "超时"} else f"HTTP {status}")
    else:
        output["search_results"] = results
        if not results and previous.get("result") == "未核实":
            return finish("未核实", previous.get("reason", "超时"))
    candidates = _candidates([{"url": r.get("url", "")} for r in results], context, policy, "搜索结果")
    candidates = [candidate for candidate in candidates if candidate["url"] not in completed]
    pending = None
    if candidates:
        result = attempt(candidates)
        if result["result"] == "核到" or result["reason"] in {"整步到点", "单家到点"}:
            return result
        pending = deepcopy(result)
    seen, pages = set(), 0
    for row in results:
        url = fixture_url(row.get("url", ""))
        domain = verify.registered_domain(url)
        text = " ".join(str(row.get(k) or "") for k in ("title", "snippet", "summary", "url"))
        if (not url or url in seen or domain == "linkedin.com" or domain == verify.registered_domain(context["source_url"])
                or not name_matches(context["company"]["name"], text)):
            continue
        if pages >= min(3, policy["max_result_pages"]):
            break
        seen.add(url); pages += 1
        if reason := _deadline(budget, step):
            return finish("未核实", reason, candidate_from="", candidate_url="", final_url="", http=None)
        response = opened.get(url)
        if response is None:
            response = _page(services, url, budget, policy)
        if reason := _deadline(budget, step):
            return finish("未核实", reason, candidate_from="", candidate_url="", final_url="", http=None)
        if not isinstance(response.get("status"), int) or not 200 <= response["status"] < 300 or response.get("error"):
            continue
        links = page_parts(response.get("body", ""), response.get("final_url") or url)["links"]
        candidates = _candidates(links, context, policy, "搜索结果报道页", source_url=response.get("final_url") or url)
        candidates = [candidate for candidate in candidates if candidate["url"] not in completed]
        if candidates:
            result = attempt(candidates)
            if result["result"] == "核到" or result["reason"] in {"整步到点", "单家到点"}:
                return result
            if pending is None or result["result"] == "未核实":
                pending = deepcopy(result)
    if pending:
        # 保留整个尝试账本，最终结果优先选择仍需重核的网址。
        return finish(pending["result"], pending["reason"], **{k: pending[k] for k in (
            "candidate_from", "candidate_url", "final_url", "http", "matched_facts", "weak") if k in pending})
    return finish()


def has_record(name: str, path: Path) -> bool:
    """已有 true/false 公司记录均跳过，精确名称匹配和推送一致。"""
    return any(r.get("type") == "公司" and str(r.get("company", "")).strip().casefold() == name.strip().casefold()
               for r in read_link_records(path))


def append_verified(cfg, name: str, check: dict, *, before_replace=None) -> bool:
    """持文件锁复查后追加；保留原记录字节，原子替换，已有记录永不覆盖。"""
    if check.get("result") != "核到" or not _home(check.get("final_url", "")):
        return False
    path = effective_links_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raw = path.read_text(encoding="utf-8") if path.exists() else "[]"
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError("官网记录必须是数组，未写入")
        if has_record(name, path):
            return False
        by = "来源页" if check.get("candidate_from") == "来源页" else "官网核对"
        record = {"name": name, "type": "公司", "url": _home(check["final_url"]), "company": name,
                  "verified": True, "verified_by": by + ("（弱证据）" if check.get("weak") else ""),
                  "verified_at": check["checked_at"][:16], "checker": "脚本"}
        closing = raw.rfind("]")
        payload = raw[:closing] + ("," if value else "") + "\n  " + _json(record) + "\n" + raw[closing:]
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".links-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            if before_replace:
                before_replace(temporary)
            temporary.replace(path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return True


def save_check(conn, cid, output, *, now, source="日常", links_written=False):
    """每次核实保存一行；工作线程不接触数据库或 links 文件。"""
    columns = ("candidate_from", "candidate_url", "final_url", "http", "result", "reason")
    conn.execute("INSERT INTO homepage_check(company_id,source,run_date," + ",".join(columns) +
                 ",matched_facts_json,evidence_strength,first_unverified_date,searched,checked_at,links_written,"
                 "candidates_json,search_results_json,source_ids_json) VALUES (" + ",".join("?" for _ in range(18)) + ")",
                 (cid, source, str(now.date()), *(output.get(k, "") for k in columns),
                  _json(output.get("matched_facts", [])), "弱" if output.get("weak") else "强" if output["result"] == "核到" else "",
                  output.get("first_unverified_date"), bool(output.get("searched")), output["checked_at"], bool(links_written),
                  _json(output.get("candidates", [])), _json(output.get("search_results", [])), _json(output.get("source_ids", []))))
    for request in output.get("searches", []):
        conn.execute("INSERT OR IGNORE INTO verify_search(company_id,purpose,source,run_date,run_started_at,search_date,requested_at,query,status,result_count,billed,cost,results_json) "
                     "VALUES (?,'官网','日常',?,?,?,?,?,?,?,?,?,?)",
                     (cid, str(now.date()), _stamp(now), str(now.date()), request["requested_at"], request["query"], str(request["status"]),
                      request["result_count"], request["billed"], request["cost"], _json(request["results"])))


class _LocalClock:
    def __init__(self, now):
        self.base, self.started, self.offset = now, time.monotonic(), 0

    def now(self):
        return self.base + timedelta(seconds=self.offset + time.monotonic() - self.started)

    def advance(self, seconds):
        self.offset += max(0, seconds)


def _previous(conn):
    latest = {}
    cursor = conn.execute("SELECT * FROM homepage_check ORDER BY checked_at,id")
    columns = [column[0] for column in cursor.description]
    for row in cursor:
        record = dict(zip(columns, row))
        for key in ("source_ids", "search_results", "candidates", "matched_facts"):
            record[key] = json.loads(record[key + "_json"])
        record["weak"] = record["evidence_strength"] == "弱"
        latest[record["company_id"]] = record
    return latest


def run_homepage(conn, cfg, clock, *, company_ids=(), source_bodies=None, fixtures=None,
                 services=None, parent_budget=None, log=None):
    """入库查证之后单独运行；主线程保存结果，120 秒到点即返回。"""
    policy, now = load_policy(cfg), clock.now()
    seconds = min(120, policy["step_timeout"], parent_budget.remaining() if parent_budget else float("inf"))
    step = Budget(clock, seconds, parent=parent_budget)
    if log:
        log({"event": "homepage_start", "at": now.isoformat()})
    services = services if services is not None else FixtureServices(fixtures, clock) if cfg.test_mode else LiveServices(cfg, clock)
    latest = _previous(conn)
    ordered = list(dict.fromkeys(company_ids))
    ordered += [cid for cid, previous in latest.items() if cid not in ordered and
                (previous["result"] == "未核实" or (previous["result"] == "核到" and not previous["links_written"]))]
    candidates = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM funding_event WHERE baseline_unpushed=0 AND filter_reasons_json='[]'")}
    collapsed, _ = scan._collapsed_yc(conn, candidates)
    used = conn.execute("SELECT COUNT(*) FROM verify_search WHERE purpose='官网' AND source='日常' AND run_date=?", (str(now.date()),)).fetchone()[0]
    quota = _Quota(max(0, int(policy["daily_search_limit"]) - used))
    outputs, jobs = {}, []

    def store(context, output):
        cid = context["company"]["id"]
        written = False
        try:
            written = append_verified(cfg, context["company"]["name"], output)
        except (OSError, ValueError):
            if log:
                log({"event": "homepage_links_failed", "company_id": cid, "at": clock.now().isoformat()})
        save_check(conn, cid, output, now=now, links_written=written)
        outputs[cid] = output

    for cid in ordered:
        context = company_context(conn, cid, cfg, source_bodies=source_bodies)
        if (context is None or context["company"]["status"] != "已纳入" or context["event"]["id"] in collapsed
                or has_record(context["company"]["name"], effective_links_file(cfg))):
            continue
        event = context["event"]
        if event["original_first_ingested_at"] and not conn.execute("SELECT 1 FROM meta WHERE key=?", (f"backfill:normal:{event['id']}",)).fetchone():
            continue
        previous = latest.get(cid, {})
        if previous.get("result") == "停止重核":
            continue
        if previous.get("result") == "未核到" and not any(s not in previous["source_ids"] for s in context["source_ids"]):
            continue
        if previous.get("result") == "核到" and not previous.get("links_written"):
            store(context, {**previous, "searches": [], "checked_at": previous["checked_at"]})
            continue
        first = previous.get("first_unverified_date")
        if previous.get("result") == "未核实" and first and (now.date() - datetime.fromisoformat(first).date()).days >= policy["recheck_days"]:
            store(context, {**previous, "result": "停止重核", "searches": [], "checked_at": _stamp(now)})
            continue
        searched = conn.execute("SELECT 1 FROM verify_search WHERE company_id=? AND purpose='官网' LIMIT 1", (cid,)).fetchone()
        previous = {**previous, "searched": bool(searched or previous.get("searched"))}
        jobs.append((context, previous))
    if isinstance(services, FixtureServices):
        # 固定页延迟按四个槽的完成时刻推进；慢的一家不会让其他家的预算被累加。
        lanes = [0.0] * max(1, int(min(4, policy["concurrency"])))
        start = clock.now()
        for context, previous in jobs:
            lane = min(range(len(lanes)), key=lanes.__getitem__)
            offset = lanes[lane]
            local = _LocalClock(start + timedelta(seconds=offset))
            local_step = Budget(local, max(0, seconds - offset))
            budget = Budget(local, min(90, policy["company_timeout"]), parent=local_step)
            output = check_company(context, cfg, local, services.fork(local), policy, budget, local_step, quota, previous=previous)
            lanes[lane] += budget.elapsed()
            store(context, output)
        elapsed = max(lanes, default=0)
        clock.advance(max(0, min(seconds, elapsed) - (clock.now() - start).total_seconds()))
    else:
        executor = ThreadPoolExecutor(max_workers=max(1, int(min(4, policy["concurrency"]))))
        futures = {}
        def work(context, previous, journal):
            budget = Budget(clock, min(90, policy["company_timeout"]), parent=step)
            return check_company(context, cfg, clock, services, policy, budget, step, quota, previous=previous, journal=journal)
        for context, previous in jobs:
            journal = {}
            futures[executor.submit(work, context, previous, journal)] = (context, journal)
        try:
            while futures and step.remaining() > 0:
                done, _ = wait(futures, timeout=min(.05, step.remaining()), return_when=FIRST_COMPLETED)
                for future in done:
                    context, journal = futures.pop(future)
                    try:
                        output = future.result()
                    except Exception:
                        output = {**deepcopy(journal), "result": "未核实", "reason": "超时", "checked_at": _stamp(clock.now()),
                                  "first_unverified_date": str(now.date())}
                    store(context, output)
            for future, (context, journal) in futures.items():
                future.cancel()
                previous = latest.get(context["company"]["id"], {})
                store(context, {**previous, **deepcopy(journal), "result": "未核实", "reason": "整步到点", "checked_at": _stamp(clock.now()),
                                "first_unverified_date": latest.get(context["company"]["id"], {}).get("first_unverified_date") or str(now.date())})
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    if log:
        log({"event": "homepage_end", "at": clock.now().isoformat(), "companies": len(outputs)})
    return outputs


def backfill_checker(conn, cfg, clock, *, fixtures=None, services=None):
    """清单前十共用同一核实规则；只返回冻结结果，不写库和链接文件。"""
    policy = load_policy(cfg)
    services = services if services is not None else FixtureServices(fixtures, clock) if cfg.test_mode else LiveServices(cfg, clock)
    quota = _Quota(10)
    def check(candidate):
        if has_record(candidate["name"], effective_links_file(cfg)):
            return None
        context = company_context(conn, candidate["company_id"], cfg, event_id=candidate["event_id"])
        if context is None:
            return None
        if frozen := candidate.get("rejudge"):
            context["company"].update({k: v for k, v in frozen.items() if k in context["company"]})
            context["event"] = next(e for e in frozen["events"] if e["id"] == candidate["event_id"])
            context["frozen_amount"] = True
        # 清单模式没有整步120秒限制，仍遵守单家90秒。
        step = Budget(clock, float("inf"))
        budget = Budget(clock, min(90, policy["company_timeout"]))
        return check_company(context, cfg, clock, services, policy, budget, step, quota, allow_source_fetch=True)
    return check


def record_digest_snapshot(conn, cfg, day, cards, generated_at):
    """冻结该期生成时的官网覆盖率，后续补记录不回改历史 X/Y。"""
    links = official_links(effective_links_file(cfg))
    companies = {card.company_id: {"id": card.company_id, "name": card.name,
                 "linked": card.name.strip().casefold() in links} for card in cards}
    value = {"generated_at": str(generated_at), "companies": list(companies.values())}
    conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 ("homepage:digest:" + str(day), _json(value)))


def status(conn, cfg, now, digest, cards) -> dict:
    """生成用户状态：历史覆盖率、当前缺失名单和独立的官网搜索账目。"""
    links = official_links(effective_links_file(cfg))
    saved = conn.execute("SELECT value FROM meta WHERE key=?", ("homepage:digest:" + digest["date"],)).fetchone() if digest else None
    snapshot = json.loads(saved[0]) if saved else None
    if snapshot:
        snapshot = snapshot["companies"]
    else:
        # 升级前的期次没有生成时快照，只采用核实时间不晚于该期的记录。
        cutoff = verify._time(digest["generated_at"]) if digest and digest.get("generated_at") else now
        historical = set()
        for record in read_link_records(effective_links_file(cfg)):
            try:
                if record.get("verified") is True and record.get("type") == "公司" and record.get("url") and verify._time(record["verified_at"]) <= cutoff:
                    historical.add(record["company"].strip().casefold())
            except (KeyError, TypeError, ValueError):
                continue
        snapshot = [{"id": c["company_id"], "name": c["name"], "linked": c["name"].strip().casefold() in historical} for c in cards]
    snapshot = {c["id"]: c for c in snapshot}
    missing = {c["company_id"]: c["name"] for c in cards if c["name"].strip().casefold() not in links}
    companies = {r[0]: r[1] for r in conn.execute("SELECT id,name FROM company")}
    for cid, previous in _previous(conn).items():
        if previous["result"] == "停止重核" and cid in companies and not has_record(companies[cid], effective_links_file(cfg)):
            missing[cid] = companies[cid]
    day = conn.execute("SELECT COUNT(*) FROM verify_search WHERE purpose='官网' AND source='日常' AND run_date=?", (str(now.date()),)).fetchone()[0]
    month = conn.execute("SELECT COALESCE(SUM(cost),0) FROM verify_search WHERE purpose='官网' AND source='日常' AND substr(COALESCE(search_date,run_date),1,7)=?", (now.strftime("%Y-%m"),)).fetchone()[0]
    found, total = sum(c["linked"] for c in snapshot.values()), len(snapshot)
    return {"found": found, "total": total, "has_digest": digest is not None, "low": bool(total and found / total < .85),
            "missing": [{"company_id": cid, "name": name} for cid, name in sorted(missing.items(), key=lambda p: p[1].casefold())],
            "searches": day, "month_used": round(month, 6)}
