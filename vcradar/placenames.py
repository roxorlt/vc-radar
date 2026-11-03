"""用户文字的地名写法、检查日志和离线旧文字修复。"""
from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import sqlite3
from threading import Lock


_CONFIG = Path(__file__).resolve().parent.parent / "config"
_NAMES = json.loads((_CONFIG / "city-names-zh.json").read_text(encoding="utf-8"))
_COUNTRIES = json.loads((_CONFIG / "country-names.json").read_text(encoding="utf-8"))
_CITY_COUNTRIES = json.loads((_CONFIG / "city-country.json").read_text(encoding="utf-8"))
_CITIES = _NAMES["cities"]
_STATES = {alias: _NAMES["us_states"][code] for code, aliases in _COUNTRIES["us_states"].items()
           for alias in aliases}
_STATES.update({aliases[0] + " State": _NAMES["us_states"][code]
                for code, aliases in _COUNTRIES["us_states"].items()})
_STATE_CODES = {alias: code for code, aliases in _COUNTRIES["us_states"].items()
                for alias in [code, *aliases, aliases[0] + " State", _NAMES["us_states"][code]]}
_COUNTRY_ALIASES = {alias: country["name"] for country in _COUNTRIES["countries"].values()
                    for alias in country["aliases"]}
_COUNTRY_QUALIFIERS = {**_COUNTRY_ALIASES, **{country["name"]: country["name"]
                       for country in _COUNTRIES["countries"].values()}}
_WORDS = {**_COUNTRY_ALIASES, **_STATES, **_CITIES}
_LETTERS = "A-Za-zÀ-ÖØ-öø-ÿ0-9_"
_LOG = logging.getLogger(__name__)
_AUDIT = ContextVar("place_audit", default=None)
_TEXT_KEYS = frozenset(("text", "zh", "point", "要点", "reason"))


def _pattern(words):
    return re.compile(r"(?<![" + _LETTERS + r"])(?:" + "|".join(
        re.escape(word) for word in sorted(words, key=len, reverse=True)) + r")(?![" + _LETTERS + "])")


_PLACES = _pattern(_WORDS)
_QUALIFIERS = _pattern({**_STATE_CODES, **_COUNTRY_QUALIFIERS})


def _protected(text, names):
    spans = [m.span() for m in re.finditer(r"https?://[^\s<>\"'）。，]+", text)]
    for name in names:
        if isinstance(name, str) and name:
            spans.extend(m.span() for m in _pattern([name]).finditer(text))
    # 简介没有单独的人名字段；Austin Example、New York University 这类完整专名也要保留。
    for match in re.finditer(r"(?<![A-Za-z])[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]*(?:[ \t]+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]*)+", text):
        rest = _PLACES.sub("", match[0])
        if rest.strip() and any(_PLACES.finditer(match[0])):
            spans.append(match.span())
    # 合并交叠区间，后续始终对原文位置判断。
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    return merged


def _overlaps(start, end, spans):
    return any(start < right and end > left for left, right in spans)


def _has_term(text, term):
    variants = [term]
    if term in _STATE_CODES:
        variants.append(_NAMES["us_states"][_STATE_CODES[term]])
    if term in _COUNTRY_ALIASES:
        variants.append(_COUNTRY_ALIASES[term])
    if isinstance(_CITIES.get(term), str):
        variants.append(_CITIES[term])
    return bool(_pattern(variants).search(text))


def _sentence(text, start, end):
    left = max((m.end() for m in re.finditer(r"[。；;!?！？\n]", text[:start])), default=0)
    right = re.search(r"[。；;!?！？\n]", text[end:])
    return text[left:end + right.start() if right else len(text)]


def _city_country(name, context):
    rule = _CITY_COUNTRIES.get(name, _CITY_COUNTRIES.get(_NAMES.get("aliases", {}).get(name, "")))
    if isinstance(rule, str):
        return rule
    if not isinstance(rule, dict):
        return None
    for condition in rule.get("when", []):
        if (any(_has_term(context, term) for term in condition.get("terms", []))
                or (condition.get("pattern") and re.search(condition["pattern"], context))
                or (condition.get("us_city") and any(_has_term(context, city)
                    for city, country in _CITY_COUNTRIES.items() if country == "美国"))):
            return condition["country"]
    return rule.get("default")


def _translation(name, text, start, end):
    context = _sentence(text, start, end)
    if name in _CITIES:
        value = _CITIES[name]
        country = _city_country(name, context)
        # 明写不同国家/州时不能套用默认城市（Paris, Texas 不写巴黎）。
        following = re.match(r"\s*[,，]\s*", text[end:])
        qualifier = _QUALIFIERS.match(text, end + following.end()) if following else None
        explicit, state = None, None
        if following:
            group = re.split(r"[。；;!?！？\n]", text[end + following.end():], maxsplit=1)[0][:100]
            for part in re.split(r"[,，]", group)[:3]:
                found = _QUALIFIERS.match(part.strip())
                if found is None:
                    continue
                if not re.match(r"\s*(?:$|[,.，。]|的|[（(])", part.strip()[found.end():]):
                    continue
                word = found[0]
                if isinstance(_CITY_COUNTRIES.get(word), dict):
                    qualifier_country = _city_country(word, context)
                    if qualifier_country not in {None, "有歧义", "美国"}:
                        explicit = qualifier_country
                    elif qualifier_country == "美国":
                        state = "美国"
                elif word in _STATE_CODES:
                    state = "美国"
                elif explicit is None:
                    explicit = _COUNTRY_QUALIFIERS[word]
        explicit = explicit or state
        if explicit:
            if country not in {explicit, "有歧义", None}:
                return None, end
            if isinstance(value, dict):
                value = value.get("by_country", {}).get(explicit)
            if value and qualifier and qualifier[0] in _COUNTRIES["us_states"]:
                return value, qualifier.end()
        elif isinstance(value, dict):
            value = value.get("by_country", {}).get(country)
        return value, end
    # Georgia 的国家/州歧义沿用城市国家条件表，不能一律当格鲁吉亚。
    if name in _CITY_COUNTRIES and isinstance(_CITY_COUNTRIES[name], dict):
        country = _city_country(name, context)
        if country == "有歧义":
            return None, end
        return (_STATES.get(name) if country == "美国" else country), end
    return _WORDS[name], end


def _after_unknown_city(text, start):
    """州名前紧跟「<英文专名>, 」且该专名不在译名表里时为真。"""
    before = re.search(r"([A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'’-]*(?:[ \t]+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ.'’-]*)*)[ \t]*[,，][ \t]*$", text[:start])
    return bool(before) and before[1] not in _WORDS


def normalize_place_text(text: str, *, protected_names) -> tuple[str, list[dict]]:
    """按原文区间保护名称和网址，返回处理文字及逐处的两类命中。"""
    spans = _protected(text, protected_names)
    changes = []
    for match in re.finditer(r"总部(?:位于|设在|在)\s*", text):
        if _overlaps(*match.span(), spans):
            continue
        # “是总部位于旧金山的公司”去掉定位短语后是“是旧金山的公司”。
        relative = re.match(r"[^。；;!?！？\n，,]{1,70}?的", text[match.end():])
        copula = bool(re.search(r"(?:是|是一家|是一个)\s*$", text[:match.start()]))
        replacement = "" if relative and copula else "在"
        if replacement and re.match(r"[A-Za-z]", text[match.end():]):
            place = _PLACES.match(text, match.end())
            translated = _translation(place[0], text, *place.span())[0] if place else None
            if not translated or _overlaps(match.end(), place.end() if place else match.end() + 1, spans):
                replacement += " "
        start = match.start()
        possessive = re.search(r"的[ \t]*$", text[:start])
        if possessive and not _overlaps(possessive.start(), start, spans):
            start = possessive.start()
        changes.append((start, match.end(), replacement, "总部"))
    # PM 10-31 17:15 R2：「总部地点」→「所在地」，和上面同一批检查、同样避开保护区间。
    for match in re.finditer(r"总部地点", text):
        if not _overlaps(*match.span(), spans):
            changes.append((*match.span(), "所在地", "总部"))
    occupied = [*spans, *((start, end) for start, end, _, _ in changes)]
    for match in _PLACES.finditer(text):
        if _overlaps(*match.span(), occupied):
            continue
        if match[0] in _STATES and _after_unknown_city(text, match.start()):
            # R2：城市不在译名表时「城市, 州」整段保留英文（Framingham, MA），不单独译州。
            continue
        replacement, end = _translation(match[0], text, *match.span())
        if not replacement:
            continue
        if _overlaps(match.start(), end, occupied):
            # 城市可译，限定词恰好是受保护名称时只译城市，不吞掉该名称。
            end = match.end()
        start = match.start()
        space = re.search(r"(?<=[\u3400-\u9fff])[ \t]+$", text[:start])
        if space and not _overlaps(space.start(), start, occupied):
            start = space.start()
        # 译成中文后，紧跟中文的空格也去掉：「旧金山 的」→「旧金山的」。
        trail = re.match(r"[ \t]+(?=[\u3400-\u9fff])", text[end:])
        if trail and not _overlaps(end, end + trail.end(), occupied):
            end += trail.end()
        changes.append((start, end, replacement, "英文地名"))
        occupied.append((start, end))
    hits, pieces, cursor = [], [], 0
    for start, end, replacement, kind in sorted(changes):
        pieces.extend((text[cursor:start], replacement))
        hits.append({"kind": kind, "before": text[start:end], "after": replacement})
        cursor = end
    pieces.append(text[cursor:])
    result = "".join(pieces)
    return result, hits


def place_problems(text: str, *, protected_names) -> list[str]:
    """只检查未受保护的禁用写法和可翻译地名，小城和歧义名保留。"""
    spans = _protected(text, protected_names)
    problems = [m[0] for m in re.finditer("总部", text) if not _overlaps(*m.span(), spans)]
    for match in _PLACES.finditer(text):
        replacement, _ = _translation(match[0], text, *match.span())
        if replacement and not _overlaps(*match.span(), spans):
            problems.append(match[0])
    return list(dict.fromkeys(problems))


def protected_names(company: dict, events=(), document=None) -> list[str]:
    """汇集公司、产品词、投资方和文档中的实体名、原句。"""
    names = [company.get("name"), company.get("company_name")]
    names.extend(company.get("_place_names", []))
    for key in ("product_terms", "leads", "follows"):
        names.extend(company.get(key) or [])
    for key in ("product_terms_json", "leads_json", "follows_json"):
        names.extend(json.loads(company.get(key) or "[]"))
    for event in events:
        for key in ("leads", "follows"):
            names.extend(event.get(key) or [])
        for key in ("leads_json", "follows_json"):
            names.extend(json.loads(event.get(key) or "[]"))

    def visit(value):
        if isinstance(value, dict):
            names.extend(e.get("name") for e in value.get("entities", []) if isinstance(e, dict))
            names.extend(v for k, v in value.items() if k in {"quote", "原句"} and isinstance(v, str))
            for key, child in value.items():
                if key not in {"entities", "quotes", "quote", "原句"}:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(document)
    return list(dict.fromkeys(name for name in names if isinstance(name, str) and name))


def company_names(conn, company_id) -> list[str]:
    """同一公司的历史显示共用全部投资方和产品词，避免不同页面保护范围不同。"""
    cursor = conn.execute("SELECT * FROM company WHERE id=?", (company_id,))
    company = dict(zip((column[0] for column in cursor.description), cursor.fetchone()))
    events = [dict(zip(("leads_json", "follows_json"), row)) for row in
              conn.execute("SELECT leads_json,follows_json FROM funding_event WHERE company_id=?", (company_id,))]
    return protected_names(company, events)


def city_label(city, location) -> str:
    """城市字段按完整所在地判歧义，再只输出城市中文名。"""
    match = _PLACES.fullmatch(city)
    if not match:
        return city
    context = location if city in location else city
    start = context.index(city)
    translated, _ = _translation(city, context, start, start + len(city))
    return translated or city


class PlaceAudit:
    """线程共享的运行计数；仅主线程在结束时写日志。"""

    def __init__(self):
        self.companies = {}
        self.lock = Lock()

    def record(self, company, *, hits=(), checked=0, retries=0, remaining=0):
        """累计一家公司的检查、替换、重试及最终残留。"""
        with self.lock:
            counts = self.companies.setdefault(company, Counter())
            counts.update({"checked": checked, "retries": retries, "remaining": remaining})
            counts.update(hit["kind"] for hit in hits)

    def merge(self, other):
        """主线程合并已完成深度任务的计数快照。"""
        with other.lock, self.lock:
            for company, counts in other.companies.items():
                self.companies.setdefault(company, Counter()).update(counts)

    def write(self, log):
        """逐家公司和整次运行各写一行可独立核对的计数。"""
        total = Counter()
        with self.lock:
            companies = [(company, counts.copy()) for company, counts in self.companies.items()]
        for company, counts in companies:
            total.update(counts)
            log({"event": "place_company", "company": company, **dict(counts),
                 "message": f"{company}：替换 {counts['总部'] + counts['英文地名']} 处，"
                            f"总部 {counts['总部']} · 英文地名 {counts['英文地名']}"
                            + (f"；地名未替换：{company}" if counts["remaining"] else "")})
        log({"event": "place_summary", **dict(total), "message":
             f"地名检查：检查条数 {total['checked']}，程序替换条数 "
             f"{total['总部'] + total['英文地名']}（总部 {total['总部']} / 英文地名 {total['英文地名']}），"
             f"重试 {total['retries']}，仍未替换 {total['remaining']}"})


@contextmanager
def place_audit(log):
    """为一次运行建立独立计数，异常退出也保留已完成的检查日志。"""
    audit = PlaceAudit()
    token = _AUDIT.set(audit)
    try:
        yield audit
    finally:
        _AUDIT.reset(token)
        audit.write(log)


def check_text(text, *, company, protected_names, audit=None):
    """规范化一段生成文字并计数，返回文字、命中和待重试问题。"""
    result, hits = normalize_place_text(text, protected_names=protected_names)
    audit = audit or _AUDIT.get()
    if audit is not None:
        audit.record(company, hits=hits, checked=1)
    return result, hits, place_problems(result, protected_names=protected_names)


def record_retry(company, *, audit=None):
    """记录确实发起的地名修正重试。"""
    audit = audit or _AUDIT.get()
    if audit is not None:
        audit.record(company, retries=1)


def record_remaining(company, problems, *, audit=None):
    """最终仍有问题时保留文字，记录公司名而不额外调用模型。"""
    if problems:
        _LOG.warning("地名未替换：%s", company)
        audit = audit or _AUDIT.get()
        if audit is not None:
            audit.record(company, remaining=len(problems))


def normalize_document(document, *, names, company="", audit=None):
    """只处理深度要点和标签理由；实体、事实值、字段、原句和网址原样保留。"""
    result = deepcopy(document)
    protected = [*names, *protected_names({}, document=document)]
    changes, problems = [], []

    def visit(value, path):
        if isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, [*path, str(index)])
        elif isinstance(value, dict):
            for key, child in value.items():
                if key in _TEXT_KEYS and isinstance(child, str):
                    new, hits, remaining = check_text(child, company=company, protected_names=protected, audit=audit)
                    if hits:
                        changes.append({"field": ".".join([*path, key]), "before": child, "after": new,
                                        "hits": hits, "hit_count": len(hits)})
                        value[key] = new
                    if remaining:
                        problems.append(new)
                elif key not in {"entities", "objects", "quotes", "quote", "原句", "url_provenance"}:
                    visit(child, [*path, key])
    visit(result, [])
    return result, changes, problems


def _plan(conn, audit):
    changes, unresolved = [], []
    events = {}
    for row in conn.execute("SELECT company_id,leads_json,follows_json FROM funding_event"):
        events.setdefault(row[0], []).append(dict(row))
    companies = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM company")}
    for cid, company in companies.items():
        names = protected_names(company, events.get(cid, []))
        old = company["summary"] or ""
        if old:
            new, hits, problems = check_text(old, company=company["name"], protected_names=names, audit=audit)
            if hits:
                changes.append({"table": "company", "company_id": cid, "company": company["name"],
                                "field": "summary", "before": old, "after": new, "hits": hits, "hit_count": len(hits)})
            if problems:
                unresolved.append({"company": company["name"], "field": "summary", "text": new, "problems": problems})
                record_remaining(company["name"], problems, audit=audit)
    for row in conn.execute("SELECT * FROM deepdive"):
        company = companies[row["company_id"]]
        names = protected_names(company, events.get(company["id"], []))
        for column in ("result_json", "new_evidence_json"):
            original = json.loads(row[column])
            normalized, details, problems = normalize_document(original, names=names, company=company["name"], audit=audit)
            if details:
                changes.append({"table": "deepdive", "company_id": company["id"], "company": company["name"],
                    "run_date": row["run_date"], "field": column, "before": row[column],
                    "after": json.dumps(normalized, ensure_ascii=False), "details": details,
                    "hits": [hit for detail in details for hit in detail["hits"]],
                    "hit_count": sum(detail["hit_count"] for detail in details)})
            if problems:
                unresolved.extend({"company": company["name"], "field": column, "text": text, "problems": [text]}
                                  for text in problems)
                record_remaining(company["name"], problems, audit=audit)
    return changes, unresolved


def _backup_table(conn, table, path):
    # 从同一个写事务快照导出整张表；保留全部列，备份可直接用 SQLite 读取。
    with closing(sqlite3.connect(path)) as backup, backup:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        backup.execute(schema)
        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        columns = len(conn.execute(f'PRAGMA table_info("{table}")').fetchall())
        backup.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in range(columns))})', rows)


def run_fix_place_names(cfg, args, clock) -> int:
    """默认只读预览；apply 先备份两张表，再在一个事务内改全部历史文字。"""
    from .pipeline import run_lock
    from .config import BJ, is_test_run

    cfg = replace(cfg, test_mode=is_test_run(args, cfg))
    apply = bool(args.apply)
    directory = cfg.db_path.parent / "data" if cfg.test_mode else cfg.state_dir / "data"
    mode = "rw" if apply else "ro"
    audit = PlaceAudit()
    conn = sqlite3.connect(cfg.db_path.resolve().as_uri() + f"?mode={mode}", uri=True)
    conn.row_factory = sqlite3.Row
    backups = []
    try:
        if apply:
            with run_lock(cfg), conn:
                conn.execute("BEGIN IMMEDIATE")
                changes, unresolved = _plan(conn, audit)
                if changes:
                    stamp = datetime.now(BJ).strftime("%Y%m%d-%H%M%S-%f")
                    backup_dir = directory / "backup"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    for table in ("company", "deepdive"):
                        path = backup_dir / f"{table}-{stamp}.db"
                        _backup_table(conn, table, path)
                        backups.append(str(path))
                for change in changes:
                    if change["table"] == "company":
                        conn.execute("UPDATE company SET summary=? WHERE id=?", (change["after"], change["company_id"]))
                    else:
                        conn.execute(f'UPDATE deepdive SET "{change["field"]}"=? WHERE company_id=? AND run_date=?',
                                     (change["after"], change["company_id"], change["run_date"]))
        else:
            conn.execute("BEGIN")
            changes, unresolved = _plan(conn, audit)
        table_counts = {}
        for table in ("company", "deepdive"):
            entries = [change for change in changes if change["table"] == table]
            kinds = Counter(hit["kind"] for change in entries for hit in change["hits"])
            table_counts[table] = {"fields": len(entries), "总部": kinds["总部"], "英文地名": kinds["英文地名"]}
        report = {"apply": apply, "changed": len(changes), "changes": changes, "counts": table_counts,
                  "unresolved": unresolved, "backups": backups, "model_calls": 0, "searches": 0}
        if args.report_dir is not None:
            args.report_dir.mkdir(parents=True, exist_ok=True)
            (args.report_dir / "fix-place-names.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        counts = Counter(change["table"] for change in changes)
        kinds = Counter(hit["kind"] for change in changes for hit in change["hits"])
        print(f"地名替换{'已执行' if apply else '预览'}：{len(changes)} 条（company {counts['company']} / "
              f"deepdive {counts['deepdive']}），总部 {kinds['总部']} / 英文地名 {kinds['英文地名']}，"
              f"仍未替换 {len(unresolved)}，模型调用 0，搜索 0")
        if apply:
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / "fix-place-names.log").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"at": clock.now().isoformat(), **report}, ensure_ascii=False) + "\n")
                audit.write(lambda event: log.write(json.dumps(event, ensure_ascii=False) + "\n"))
        return 0
    finally:
        conn.close()
