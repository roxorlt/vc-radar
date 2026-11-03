"""抽取（任务 C）。PRD 7.1：规则先跑，再走文本抽取步骤（模型）；字段附原句；金额逐字在原文；
简介 80～140 字（不含空格）、3～4 句；数字、金额、日期照抄原文写法不换算；带数字的要点原句必须含该数字。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import html
import json
import logging
from pathlib import Path
import re
import time

from .llm import LLMClient
from .models import Extracted, LabelDim, RawItem
from . import cal, fx, scan, placenames
from .config import BJ


_LOG = logging.getLogger(__name__)
_TEXT_FIELDS = ("company_name", "website", "round_text", "amount_text", "hq_text", "one_liner")
_LIST_FIELDS = ("leads", "follows")
_STRUCTURED_FIELDS = (*_TEXT_FIELDS, *_LIST_FIELDS, "location", "yc_batch", "tags")
_AMOUNT = fx.AMOUNT_PATTERN
_ROUND = re.compile(
    r"\b(?:pre[-\s‐‑–]?seed|seed|angel|Series\s+[A-Z](?:\s*\+)?)(?![A-Za-z])",
    re.IGNORECASE,
)
_BASED = re.compile(r"\b([A-Z][\w.'’]*(?:\s+[A-Z][\w.'’]*){0,3})[-‐‑–—]based\b")
_NUMBER = re.compile(
    r"(?:(?:US\$|USD|EUR|GBP|[$€£¥])\s*)?\d+(?:,\d{3})*(?:\.\d+)?"
    r"(?:\s?(?:million|billion|thousand|[MBK])(?![A-Za-z])|%|x\b)?",
    re.IGNORECASE,
)
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "\u200b": ""})


def usage_cost(usage: dict, started_at: datetime) -> float:
    """按调用开始的北京时间及缓存命中量估算文本抽取费用（元）。"""
    stamp = started_at.astimezone(BJ)
    peak = (stamp.weekday() < 5 and cal.is_cn_workday(stamp.date())
            and (9 <= stamp.hour < 12 or 14 <= stamp.hour < 18))
    prompt = max(0, int(usage.get("prompt_tokens", 0)))
    hit = min(prompt, max(0, int(usage.get("prompt_cache_hit_tokens", 0))))
    output = max(0, int(usage.get("completion_tokens", 0)))
    price = (Decimal(hit) * Decimal("0.02") + Decimal(prompt - hit) + Decimal(output) * 4) / 1_000_000
    return float(price * (2 if peak else 1))


def rule_extract(item: RawItem) -> Extracted:
    """标题/摘要里的金额（$X M/B）、轮次词、led by、-based；YC 用结构化字段；
    Term Sheet / StrictlyVC 的 structured 字段直接用。设置 complete_by_rules。"""
    ext = Extracted()
    structured = item.structured
    for field in _TEXT_FIELDS:
        value = structured.get(field)
        if isinstance(value, str) and value.strip():
            setattr(ext, field, value.strip())
            ext.quotes[field] = value.strip()
    for field in _LIST_FIELDS:
        value = structured.get(field)
        if isinstance(value, list):
            names = [name.strip() for name in value if isinstance(name, str) and name.strip()]
            setattr(ext, field, names)
            if names:
                ext.quotes[field] = "、".join(names)
    ext.is_fund_raise = structured.get("is_fund_raise") is True
    ext.is_debt_only = structured.get("is_debt_only") is True
    if structured.get("yc_batch"):
        ext.round_text = "YC 标准投资"
        ext.leads = ["Y Combinator"]
        ext.quotes["round_text"] = str(structured["yc_batch"])
        ext.quotes["leads"] = str(structured["yc_batch"])
        ext.hq_text = ext.hq_text or _first_location(structured.get("location"))
        if ext.hq_text:
            ext.quotes["hq_text"] = ext.hq_text

    text = "\n".join(part for part in (item.title, item.excerpt) if part)
    for field, pattern in (("amount_text", _AMOUNT), ("round_text", _ROUND)):
        match = pattern.search(text)
        if not getattr(ext, field) and match:
            setattr(ext, field, match.group())
            ext.quotes[field] = _sentence(text, match.start())
    if not ext.amount_text:
        bare = re.search(r"\braise[sd]?\s+(\d+(?:,\d{3})*(?:\.\d+)?\s*(?:million|billion|thousand|[MBK])(?![A-Za-z]))", text, re.I)
        if bare:
            ext.amount_text = bare[1]
            ext.quotes["amount_text"] = _sentence(text, bare.start())
    based = _BASED.search(text)
    if not ext.hq_text and based:
        ext.hq_text = re.sub(r"^The\s+", "", based.group(1))
        ext.quotes["hq_text"] = _sentence(text, based.start())
    if not ext.company_name:
        candidate = re.sub(r"^\s*[A-Z][\w.'’]*(?:\s+[A-Z][\w.'’]*){0,3}[-‐‑–—]based\s+", "", text)
        name = r"([A-Z0-9][\w&.'’+\-]*(?:\s+[A-Z0-9][\w&.'’+\-]*){0,6})"
        match = re.search(
            r"^\s*" + name + r"(?:,\s+(?:a|an|the)\b|\s+(?:raises?|raised|lands?|secures?|"
            r"closes?|bags?|nets?|snags?|announces?|launches?|nabs?|gets?|is|has)\b)", candidate,
        )
        if not match:
            match = re.search(r"\blaunch(?:es|ed)?\s+" + name + r"\s+with\b", candidate)
        if match:
            ext.company_name = match.group(1)
            ext.quotes["company_name"] = _sentence(text, text.find(ext.company_name))
    if not ext.leads:
        led = re.search(r"\b(?:co[- ]?)?led by\s+([^.;\n]+)", text, re.IGNORECASE)
        if led:
            investors = re.split(r",?\s+(?:with|alongside|joined|in the round)\b", led.group(1), maxsplit=1,
                                 flags=re.IGNORECASE)[0]
            ext.leads = [name.strip(" ,") for name in re.split(r",\s*|\s+(?:and|&)\s+", investors)
                         if name.strip(" ,")]
            ext.quotes["leads"] = _sentence(text, led.start())
    ext.hq_quote = ext.quotes.get("hq_text", "")
    ext.complete_by_rules = all((ext.company_name, ext.hq_text, ext.round_text, ext.amount_text))
    return ext


def build_prompt(item: RawItem, ext: Extracted, mode: str) -> tuple[str, str]:
    """mode = "full"（补字段 + 简介 + 标签）| "summary"（抽全条目：只写简介、判标签）。返回 (system, user)。
    正文 ≤ 8000 字符；页面里的指令都当数据。"""
    if mode not in {"full", "summary"}:
        raise ValueError("mode must be full or summary")
    system = (Path(__file__).parent / "prompts" / f"extract_{mode}.txt").read_text(encoding="utf-8")
    system += ('\nJSON 另加 product_terms 数组：1～3 个说明产品做什么的名词短语，'
               '从来源原文逐字复制；不含公司名、轮次、投资方名，找不到写 []。')
    known = {field: getattr(ext, field) for field in (*_TEXT_FIELDS, *_LIST_FIELDS,
                                                   "is_fund_raise", "is_debt_only")}
    user = json.dumps({
        "source_data": {
            "title": item.title, "excerpt": item.excerpt, "body": item.body[:8000],
            "structured": {key: item.structured[key] for key in _STRUCTURED_FIELDS if key in item.structured},
        },
        "rule_extracted": known,
    }, ensure_ascii=False)
    return system, user


def validate_model_output(item: RawItem, data: dict, *, check_summary: bool = True) -> tuple[dict, list[str]]:
    """程序校验（不靠模型自查）：每个字段的原句必须逐字出现在来源文本里（统一空白/弯引号）；
    金额必须逐字在原文；带数字的字段值，其数字必须出现在原句里；找不到原句的字段改为未披露。
    返回 (清洗后的 data, 警告列表)；已过滤条目可跳过简介校验。"""
    cleaned = deepcopy(data)
    warnings: list[str] = []
    source = _normalise(_source_text(item))
    input_quotes = data.get("quotes")
    input_quotes = input_quotes if isinstance(input_quotes, dict) else {}
    quotes: dict[str, str] = {}
    for field in (*_TEXT_FIELDS, *_LIST_FIELDS, "is_fund_raise", "is_debt_only"):
        value = data.get(field)
        if field in _TEXT_FIELDS:
            value = value.strip() if isinstance(value, str) and value.strip() else None
        elif field in _LIST_FIELDS:
            value = [name.strip() for name in value if isinstance(name, str) and name.strip()] if isinstance(value, list) else []
        else:
            value = value is True
        if not value:
            cleaned[field] = value
            continue
        quote = input_quotes.get(field, "")
        normal_quote = _normalise(quote) if isinstance(quote, str) else ""
        reason = ""
        if not normal_quote or normal_quote not in source:
            reason = "quote_not_in_source"
        elif field == "amount_text" and (not _has_amount(source, value) or not _has_amount(normal_quote, value)):
            reason = "amount_not_verbatim"
        elif any(not _has_number(normal_quote, number) for number in _numbers(value)):
            reason = "number_not_in_quote"
        if reason:
            cleaned[field] = [] if field in _LIST_FIELDS else False if isinstance(value, bool) else None
            warnings.append(f"{field}:{reason}")
        else:
            cleaned[field] = value
            quotes[field] = quote
    cleaned["quotes"] = quotes
    cleaned["product_terms"] = validate_product_terms(item, data.get("product_terms"),
        company_name=data.get("company_name"), investors=[*cleaned["leads"], *cleaned["follows"]])
    for field in ("ai", "social"):
        dim = data.get(field)
        dim = dim if isinstance(dim, dict) else {}
        verdict = dim.get("verdict")
        if not isinstance(verdict, str) or verdict not in {"是", "否", "拿不准"}:
            verdict = None
        quote = dim.get("quote", "")
        quote = quote if isinstance(quote, str) else ""
        if quote and _normalise(quote) not in source:
            quote = ""
            warnings.append(f"{field}:quote_not_in_source")
        if not _normalise(quote):
            quote = ""
        if verdict == "是" and not quote:
            warnings.append(f"{field}:yes_without_quote")
        cleaned[field] = {"verdict": verdict, "quote": quote}

    summary = data.get("summary")
    summary = summary if isinstance(summary, str) else ""
    cleaned["summary"] = summary
    if not check_summary:
        return cleaned, warnings
    summary_quote = input_quotes.get("summary")
    if isinstance(summary_quote, str) and _normalise(summary_quote):
        if _normalise(summary_quote) in source:
            quotes["summary"] = summary_quote
        else:
            warnings.append("summary:quote_not_in_source")
    minimum = 40 if _fact_sentences(item) < 3 else 80
    length = summary_len(summary)
    if not minimum <= length <= 140:
        warnings.append(f"summary_length:{length} expected={minimum}..140")
    sentences = summary.count("。")
    if not 3 <= sentences <= 4:
        warnings.append(f"summary_sentences:{sentences} expected=3..4")
    for number in dict.fromkeys(_numbers(summary)):
        if not _has_number(source, number):
            warnings.append(f"summary_number_not_in_source:{number}")
    return cleaned, warnings


def _summary_excluded(item, ext, data=None, *, require_company=True):
    if require_company and not ext.company_name:
        return True
    data = data or {}
    if data.get("is_financing") is False or data.get("is_funding") is False:
        return True
    if isinstance(data.get("skip_reason"), str) and data["skip_reason"] in {"非融资", "不是融资报道"}:
        return True
    country, _ = scan.judge_country(item, ext)
    normalized = scan.normalize_round(ext.round_text, is_fund=ext.is_fund_raise, is_debt_only=ext.is_debt_only)
    return bool(scan.filter_reasons(item, ext, country, normalized, []))


def _apply_fields(ext, cleaned, mode):
    ext.product_terms = cleaned["product_terms"]
    if mode == "full":
        for field in (*_TEXT_FIELDS, *_LIST_FIELDS):
            if not getattr(ext, field) and cleaned.get(field):
                setattr(ext, field, cleaned[field])
                ext.quotes[field] = cleaned["quotes"][field]
        for field in ("is_fund_raise", "is_debt_only"):
            if cleaned[field]:
                setattr(ext, field, True)
                ext.quotes[field] = cleaned["quotes"][field]
        ext.hq_quote = ext.quotes.get("hq_text", ext.hq_quote)
    elif not ext.one_liner and cleaned.get("one_liner"):
        ext.one_liner = cleaned["one_liner"]
        ext.quotes["one_liner"] = cleaned["quotes"]["one_liner"]
    ext.ai = LabelDim(**cleaned["ai"])
    ext.social = LabelDim(**cleaned["social"])


def validate_product_terms(item: RawItem, terms, *, company_name=None, investors=()) -> list[str]:
    """只保留原文逐字出现、且不是公司名、轮次或投资方的最多三个产品词。"""
    if not isinstance(terms, list):
        return []
    source = _source_text(item).casefold()
    names = {str(name).strip().casefold() for name in
             (company_name, item.structured.get("company_name"), *investors,
              *item.structured.get("leads", []), *item.structured.get("follows", [])) if name}
    result = []
    for term in terms:
        if (isinstance(term, str) and term.strip() and term.strip().casefold() in source
                and term.strip().casefold() not in names
                and scan.normalize_round(term.strip()) == "待核"
                and term.strip().casefold() not in {t.casefold() for t in result}):
            result.append(term.strip())
    return result[:3]


def _summary_problems(cleaned, warnings):
    summary = cleaned["summary"]
    problems = []
    if summary_len(summary) > 140:
        problems.append(f"上次简介 {summary_len(summary)} 字，超过 140")
    if summary.count("。") > 4:
        problems.append(f"上次简介 {summary.count('。')} 句，超过 4")
    for warning in warnings:
        if warning.startswith("summary_number_not_in_source:"):
            problems.append("数字 " + warning.split(":", 1)[1] + " 来源里没有")
        elif warning == "summary:quote_not_in_source":
            problems.append("quotes.summary 不是原文逐字")
    return problems


def sanitize_summary(item: RawItem, summary: str) -> str:
    """删除无依据数字所在句，再按句保留最多四句、至多 140 字的最长前缀。"""
    source = _normalise(_source_text(item))
    sentences = [sentence.strip() for sentence in re.findall(r"[^。]+(?:。|$)", summary)]
    sentences = [sentence for sentence in sentences if not any(
        not _has_number(source, number) for number in _numbers(sentence))]
    if not sentences:
        return ""
    prefix = ""
    for sentence in sentences[:4]:
        if summary_len(prefix + sentence) > 140:
            break
        prefix += sentence
    if prefix:
        return prefix
    first = sentences[0]
    count = 0
    stop = 0
    while stop < len(first) and count < 139:
        count += int(not first[stop].isspace())
        stop += 1
    # 不把原文数字截成另一个数字。
    for match in _NUMBER.finditer(first):
        if match.start() < stop < match.end():
            stop = match.start()
            break
    return first[:stop].rstrip("。 \n\t") + "。"


def model_extract(item: RawItem, ext: Extracted, client: LLMClient, mode: str, *,
                  max_calls: int = 2, budget_s: float = 180.0) -> Extracted:
    """调模型并合并进 ext（规则已抽到的不被模型覆盖为空）。每条最多 2 次（含失败重试和简介修正），
    每次超时 = min(120, budget_s − 已用)，剩余 < 30 秒不发第 2 次。失败时标签两维 verdict=None（→ 标签待核）。"""
    if _summary_excluded(item, ext, require_company=False):
        ext.summary = ""
        ext.quotes.pop("summary", None)
        return ext
    system, user = build_prompt(item, ext, mode)
    started = time.monotonic()
    reported_elapsed = 0.0
    ext.model_used = client.model
    ext.model_error = None
    limit = max(0, min(2, max_calls))
    best = None
    best_summary, place_issues = "", []
    place_retry = False
    while ext.model_calls < limit:
        remaining = budget_s - max(time.monotonic() - started, reported_elapsed)
        if remaining <= 0 or (ext.model_calls > 0 and remaining < 30):
            ext.model_error = ext.model_error or "budget_exhausted"
            break
        ext.model_calls += 1
        if place_retry:
            placenames.record_retry(ext.company_name or "")
            place_retry = False
        result = client.complete_json(system, user, min(120.0, remaining))
        reported_elapsed += max(0.0, result.elapsed_s)
        for key, count in (result.usage or {}).items():
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                ext.usage[key] = ext.usage.get(key, 0) + count
        if not result.ok:
            ext.model_error = result.error or "model_error"
            continue
        try:
            data = json.loads(result.content)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (ValueError, TypeError):
            ext.model_error = "bad_json"
            continue
        fields, field_warnings = validate_model_output(item, data, check_summary=False)
        _apply_fields(ext, fields, mode)
        if _summary_excluded(item, ext, fields):
            for warning in field_warnings:
                _LOG.warning("extraction_validation source_id=%s external_id=%s %s", item.source_id, item.external_id, warning)
            ext.summary = ""
            ext.quotes.pop("summary", None)
            ext.model_error = None
            return ext
        cleaned, warnings = validate_model_output(item, data)
        best = cleaned
        for warning in warnings:
            _LOG.warning("extraction_validation source_id=%s external_id=%s %s",
                         item.source_id, item.external_id, warning)
        problems = _summary_problems(cleaned, warnings)
        # 字数和数字先按原始输出校验，再处理显示写法；两类修正共用第 2 次。
        names = placenames.protected_names({"name": ext.company_name, "product_terms": ext.product_terms,
                                            "leads": ext.leads, "follows": ext.follows})
        best_summary, _, place_issues = placenames.check_text(sanitize_summary(item, cleaned["summary"]),
            company=ext.company_name or "", protected_names=names)
        problems.extend("地名写法仍有问题：" + issue for issue in place_issues)
        if problems and ext.model_calls == 1 and limit > 1:
            prompt = json.loads(user)
            prompt["summary_correction"] = {"previous_summary": cleaned["summary"], "problems": problems,
                                              "instruction": "修正以上问题，只使用来源事实；简介最多 140 字、3～4 句，不凑字。"}
            user = json.dumps(prompt, ensure_ascii=False)
            place_retry = bool(place_issues)
            continue
        break
    if best is not None:
        ext.summary = best_summary
        placenames.record_remaining(ext.company_name or "", place_issues)
        ext.quotes.pop("summary", None)
        if best["quotes"].get("summary"):
            ext.quotes["summary"] = best["quotes"]["summary"]
        ext.model_error = None
        return ext
    ext.model_error = ext.model_error or "call_limit"
    ext.ai = LabelDim()
    ext.social = LabelDim()
    ext.summary = ""
    return ext


def summary_len(text: str) -> int:
    """不含空格的字符数。"""
    return len(re.sub(r"\s", "", text))


def _source_text(item: RawItem) -> str:
    pieces = [item.title, item.excerpt, item.body[:8000]]
    for key in _STRUCTURED_FIELDS:
        value = item.structured.get(key)
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, list):
            pieces.extend(part for part in value if isinstance(part, str))
    return "\n".join(dict.fromkeys(part for part in pieces if part))


def _normalise(text: str) -> str:
    text = html.unescape(text).translate(_QUOTES)
    text = re.sub(r"</?(?:p|div|br|li|h[1-6])\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", text)


def _numbers(value) -> list[str]:
    if isinstance(value, list):
        return [number for part in value for number in _numbers(part)]
    return _NUMBER.findall(_normalise(value)) if isinstance(value, str) else []


def _has_number(text: str, number: str) -> bool:
    if _AMOUNT.fullmatch(number):
        return _has_amount(text, number)
    return re.search(r"(?<!\d)" + re.escape(number) + r"(?!\d)", text) is not None


def _has_amount(text: str, amount: str) -> bool:
    amount = _normalise(amount)
    if _AMOUNT.fullmatch(amount):
        # 防止把 $34M 的前缀 $34 当成已核实的完整金额。
        return any(_normalise(match.group()) == amount for match in _AMOUNT.finditer(text))
    return amount in text


def _fact_sentences(item: RawItem) -> int:
    text = html.unescape(item.body[:8000] or item.excerpt or item.title)
    text = re.sub(r"<[^>]+>", " ", text)
    return len([part for part in re.split(r"[。！？!?]+|\.(?:\s+|$)|\n+", text) if part.strip()])


def _first_location(value) -> str | None:
    if isinstance(value, list):
        value = next((location for location in value if isinstance(location, str) and location.strip()), "")
    if not isinstance(value, str) or not value.strip():
        return None
    return re.split(r"[;；\n]", value, maxsplit=1)[0].strip() or None


def _sentence(text: str, position: int) -> str:
    boundaries = list(re.finditer(r"\n|[。!?]|\.(?=\s|$)", text))
    start = max((match.end() for match in boundaries if match.end() <= position), default=0)
    end = next((match.end() for match in boundaries if match.start() >= position), len(text))
    return text[start:end].strip()
