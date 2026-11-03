"""欧洲央行参考汇率、配置驱动的币种识别及中文金额写法。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from xml.etree import ElementTree

from .config import BJ, UA, Config, is_test_run
from .http import UrllibTransport


DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
HISTORY_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
HISTORY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
SOURCE = "欧洲央行参考汇率 " + DAILY_URL


def _load_currencies(path=None):
    try:
        path = path or Path(__file__).resolve().parent.parent / "config/currencies.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data or any(not alias or (not re.fullmatch(r"[A-Z]{3}", row["code"] or "")
                           and row.get("context") != "yuan_or_yen")
                           or not row["name"] or not row["prefix"] for alias, row in data.items()):
            raise ValueError("币种表字段无效")
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("币种配置缺失或损坏，无法启动") from exc


_CURRENCIES = _load_currencies()
_ALIASES = {alias.casefold(): row["code"] for alias, row in _CURRENCIES.items()}
_PREFIXES = {row["code"]: row["prefix"] for row in _CURRENCIES.values()}
_CONTEXTS = {alias.casefold(): row.get("context") for alias, row in _CURRENCIES.items()}


def _token_pattern(token):
    escaped = re.escape(token)
    if token == "元":
        return r"(?<![美欧英港日澳加韩新])元"
    return escaped + (r"(?![A-Za-z])" if token[-1].isascii() and token[-1].isalpha() else "")


_TOKEN = "(?:" + "|".join(_token_pattern(token) for token in sorted(_ALIASES, key=len, reverse=True)) + ")"
_NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?"
_UNIT = r"(?:(?:billion|million|thousand|crore|lakh|bn|mn|mm|[bmk])(?![A-Za-z])|千万|百万|十万|亿|万|千)"
AMOUNT_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_$])(?:(?P<prefix>{_TOKEN})\s*(?P<pnum>{_NUMBER})(?:\s*(?P<punit>{_UNIT}))?"
    rf"(?:\s*(?P<suffix>{_TOKEN}))?|(?P<snum>{_NUMBER})\s*(?P<sunit>{_UNIT})?\s*(?P<currency>{_TOKEN}))",
    re.I,
)
_BARE_AMOUNT = re.compile(rf"(?<![\w.])({_NUMBER})\s*({_UNIT})?", re.I)
_MULTIPLIERS = {"billion": 10**9, "bn": 10**9, "b": 10**9, "million": 10**6, "m": 10**6,
                "mn": 10**6, "mm": 10**6, "thousand": 1000, "k": 1000, "crore": 10**7,
                "lakh": 10**5, "亿": 10**8, "千万": 10**7, "百万": 10**6, "十万": 10**5, "万": 10000, "千": 1000}
_CNY_CONTEXT = re.compile(r"(?<![A-Za-z])(?:yuan|rmb|cny|renminbi)(?![A-Za-z])|人民币|(?<![美欧英港日澳加韩新])元", re.I)
_JPY_CONTEXT = re.compile(r"(?<![A-Za-z])(?:yen|jpy)(?![A-Za-z])|円|日元", re.I)


def _parse_amount(text, *, context=None):
    if not text:
        return None, None
    match = AMOUNT_PATTERN.search(text)
    if match:
        token = match["suffix"] or match["prefix"] or match["currency"]
        currency = _ALIASES[token.casefold()]
        number, unit = match["pnum"] or match["snum"], match["punit"] or match["sunit"]
        if _CONTEXTS.get((match["prefix"] or "").casefold()) == "yuan_or_yen":
            # 只认这个金额紧邻的币种词，括号内另一个金额或远处的币种不能借用。
            source = context if context and match[0] in context else text
            start = source.find(match[0])
            number_end = start + (match.end("punit") if match["punit"] else match.end("pnum")) - match.start()
            before = re.search(rf"({_TOKEN})\s*$", source[:start], re.I)
            after = re.match(rf"\s*({_TOKEN})", source[number_end:], re.I)
            codes = {_ALIASES[m[1].casefold()] for m in (before, after) if m}
            currency = next(iter(codes)) if len(codes) == 1 and codes <= {"CNY", "JPY"} else None
    else:
        bare = _BARE_AMOUNT.search(text)
        if not bare:
            return None, None
        currency, number, unit = None, bare[1], bare[2]
    if (unit or "").casefold() in {"lakh", "crore"} and currency != "INR":
        currency = None
    with localcontext() as context:
        context.prec = 50
        value = Decimal(number.replace(",", "")) * _MULTIPLIERS.get((unit or "").casefold(), 1)
    return currency, value if value.is_finite() and math.isfinite(float(value)) else None


def parse_amount(text: str | None, *, context: str | None = None) -> tuple[str | None, float | None]:
    """按配置和原文上下文识别币种，返回原金额数字；不猜币种。"""
    currency, value = _parse_amount(text, context=context)
    return currency, float(value) if value is not None else None


def _quantity(value) -> str:
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("金额必须是非负有限数字")
    with localcontext() as context:
        context.prec = max(50, len(number.as_tuple().digits) + abs(number.adjusted()) + 2)
        integer = number.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if integer < 10000:
            return str(integer)
        ten_thousands = (number / 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if ten_thousands < 10000:
            return f"{ten_thousands} 万"
        hundred_millions = (number / 100000000).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        return f"{format(hundred_millions, 'f').removesuffix('.0')} 亿"


def format_usd(value: float) -> str:
    """按先取整再定档算法显示美元金额，四舍五入采用十进制。"""
    quantity = _quantity(value)
    return quantity + ("美元" if " " in quantity else " 美元")


def format_original(currency: str, value: float) -> str:
    """用配置中的符号或中文币种名前缀显示原币金额。"""
    return _PREFIXES.get(currency, currency + " ") + _quantity(value)


@dataclass(frozen=True)
class Amount:
    """一份冻结的原金额和换算结果；字段名与融资事件相同。"""
    amount_currency: str | None = None
    amount_original: float | None = None
    amount_usd: float | None = None
    fx_rate: float | None = None
    fx_date: str | None = None
    fx_source: str | None = None
    fx_stale: int = 0

    def to_dict(self) -> dict:
        """返回可写入库或来源 JSON 的字段。"""
        return asdict(self)


def _usd_rates(snapshot):
    # P 的事件快照是 EUR 基准，保留读取兼容；V16e 新快照明确带 base。
    rates = {key: Decimal(str(value)) for key, value in snapshot.get("rates", {}).items()}
    if snapshot.get("base") == "USD":
        return rates
    usd = rates.get("USD")
    return {key: value / usd for key, value in {"EUR": Decimal(1), **rates}.items()} if usd else {}


def convert_amount(text: str | None, snapshot: dict | None = None, *, context: str | None = None) -> Amount:
    """用美元基准汇率换算，保留原币数字；兼容已有 EUR 基准快照。"""
    currency, number = _parse_amount(text, context=context)
    original = float(number) if number is not None else None
    if number is None or currency is None:
        return Amount(currency, original)
    if currency == "USD":
        return Amount(currency, original, original, 1.0, fx_source="原文美元")
    snapshot = snapshot or {}
    with localcontext() as context:
        context.prec = 50
        rate = _usd_rates(snapshot).get(currency)
        if rate is None or not rate.is_finite() or rate <= 0:
            return Amount(currency, original, fx_stale=int(bool(snapshot.get("stale"))))
        return Amount(currency, original, float(number / rate), float(1 / rate), snapshot.get("date"),
                      snapshot.get("source"), int(bool(snapshot.get("stale"))))


def format_amount(amount: Amount | dict, text: str | None = None) -> str:
    """显示冻结的美元及原币金额；币种未知和暂未换算分别标注。"""
    values = amount.to_dict() if isinstance(amount, Amount) else dict(amount)
    if values.get("amount_status") == "金额待确认":
        return "金额待确认"
    currency, original, usd = (values.get(key) for key in ("amount_currency", "amount_original", "amount_usd"))
    parsed_currency, exact = _parse_amount(text)
    if original is None:
        currency, original = parsed_currency, exact
    elif exact is not None and parsed_currency == currency and float(exact) == float(original):
        original = exact
    if original is None:
        return text or "金额未披露"
    if currency is None:
        symbol = re.search(r"[A-Za-z]{0,3}\$|[^\w\s.,，。()（）-]", text or "")
        return (symbol[0] if symbol else "") + _quantity(original) + "（币种待确认）"
    if currency == "USD":
        return format_usd(original if usd is None or float(usd) == float(original) else usd)
    raw = format_original(currency, original)
    if usd is None:
        return raw + "（暂未换算成美元）"
    when = values.get("fx_date")
    suffix = f"，按 {when[5:]} 汇率" if values.get("fx_stale") and when else ""
    return f"约 {format_usd(usd)}（原 {raw}{suffix}）"


def format_amount_text(text: str | None, snapshot: dict | None = None) -> str:
    """改写原文中的金额，保留已经格式化的金额，重复渲染结果不变。"""
    if not text:
        return "金额未披露"
    if re.fullmatch(rf"\s*(?:raised\s+)?{_NUMBER}\s*(?:crore|lakh)\s*", text, re.I):
        return format_amount(convert_amount(text, snapshot), text)
    # 已有展示串包含原币括号，不能把原币部分再当作待换算金额。
    quantity = r"\d+(?:\.\d+)?(?: 万| 亿)?"
    protected = re.compile(r"约 " + quantity + r"美元（原 [^（）\n]+）|"
                           r"(?:(?:" + _TOKEN + r"|[^\w\s])\s*)?" + quantity
                           + r"（(?:币种待确认|暂未换算成美元)）", re.I)
    parts, start = [], 0
    def replace(match):
        if re.fullmatch(r"\d+(?:\.\d+)?(?: 万| 亿)?美元", match[0]) or match[0].endswith(" 美元"):
            return match[0]
        currency, value = _parse_amount(match[0], context=text)
        if value is None:
            return match[0]
        converted = convert_amount(match[0], snapshot, context=text)
        return format_amount(converted, match[0])
    if "（" not in text:
        return AMOUNT_PATTERN.sub(replace, text)
    for match in protected.finditer(text):
        parts.extend((AMOUNT_PATTERN.sub(replace, text[start:match.start()]), match[0]))
        start = match.end()
    parts.append(AMOUNT_PATTERN.sub(replace, text[start:]))
    return "".join(parts)


def parse_rates(xml: str) -> dict[str, dict[str, float]]:
    """解析 ECB 的 EUR 基准 XML，转换为 1 美元可兑换的币种数量。"""
    tables = {}
    for cube in ElementTree.fromstring(xml).iter():
        if "time" not in cube.attrib:
            continue
        day = date.fromisoformat(cube.attrib["time"]).isoformat()
        rates = {"EUR": Decimal(1)}
        for child in cube:
            currency = child.attrib.get("currency", "")
            try:
                rate = Decimal(child.attrib.get("rate", "NaN"))
            except InvalidOperation as exc:
                raise ValueError("汇率表含无效数字") from exc
            if not re.fullmatch(r"[A-Z]{3}", currency) or not rate.is_finite() or rate <= 0 or not math.isfinite(float(rate)):
                raise ValueError("汇率表含无效币种或数字")
            rates[currency] = rate
        if "USD" not in rates:
            raise ValueError("汇率表缺少美元")
        tables[day] = {key: float(value / rates["USD"]) for key, value in rates.items()}
    if not tables:
        raise ValueError("汇率表没有日期")
    return tables


def _normalise_cache(cache):
    if not isinstance(cache, dict):
        raise ValueError("汇率缓存不是对象")
    converted = "days" not in cache and "rates" in cache
    if converted:
        days = {}
        for day, rates in cache["rates"].items():
            snapshot = {"rates": rates}
            days[day] = {"fetched_at": cache.get("fetched_at", ""),
                         "rates": {key: float(value) for key, value in _usd_rates(snapshot).items()}}
        source = cache.get("source", DAILY_URL)
        cache = {"base": "USD", "source": source if source.startswith("欧洲央行参考汇率 ") else "欧洲央行参考汇率 " + source,
                 "days": days}
    if (cache.get("base") != "USD" or not isinstance(cache.get("days"), dict)
            or not isinstance(cache.get("source"), str) or not cache["source"]):
        raise ValueError("汇率缓存格式无效")
    for day, entry in cache["days"].items():
        date.fromisoformat(day)
        fetched = entry["fetched_at"]
        if fetched:
            stamp = datetime.fromisoformat(fetched)
            if stamp.tzinfo is not None:
                stamp = stamp.astimezone(BJ)
            entry["fetched_at"] = stamp.strftime("%Y-%m-%d %H:%M:%S")
        rates = entry["rates"]
        if not rates or not isinstance(rates, dict) or any(not re.fullmatch(r"[A-Z]{3}", code)
                    or not math.isfinite(float(value)) or float(value) <= 0 for code, value in rates.items()):
            raise ValueError("缓存汇率无效")
        if "USD" in rates and float(rates["USD"]) != 1:
            raise ValueError("美元基准无效")
        entry["rates"] = {code: float(value) for code, value in rates.items()}
    return cache, converted


class FxRates:
    """每次运行一份汇率会话；缓存按 V16e 保存，可注入 Transport 或 fetcher。"""

    def __init__(self, cfg: Config, fetcher=None, *, now: datetime | None = None, log=None,
                 persist: bool = True, fixed_file: Path | None = None):
        self.path = Path(fixed_file) if fixed_file is not None else cfg.rates_file
        self.persist = persist and fixed_file is None and not cfg.test_mode
        self.fixed = fixed_file is not None or (cfg.test_mode and fetcher is None)
        self.fetcher = fetcher if fetcher is not None else UrllibTransport()
        self.now = (now or datetime.now(BJ)).astimezone(BJ)
        self.log = log
        self.cache = self._load()
        self.coverage = {}
        self.requested: set[str] = set()
        self.failed = False
        self.missing: set[str] = set()
        days = self.cache["days"]
        self.status = {"date": max(days) if days else None, "source": self.cache["source"],
                       "ok": False, "message": "汇率尚未更新" if days else "汇率没有取到"}

    def _load(self):
        try:
            cache, converted = _normalise_cache(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, InvalidOperation, KeyError, TypeError, AttributeError):
            return {"base": "USD", "source": SOURCE, "days": {}}
        if converted:
            try:
                self._write(cache)
            except OSError:
                self._emit({"event": "fx_cache_error", "source": cache["source"], "message": "汇率缓存写入失败"})
        return cache

    def _write(self, cache=None):
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, encoding="utf-8", delete=False) as target:
                name = target.name
                json.dump(self.cache if cache is None else cache, target, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    def _emit(self, record):
        if self.log is not None:
            self.log(record)

    def _request(self, url):
        if url in self.requested or self.failed:
            return
        self.requested.add(url)
        if self.fixed:
            days = self.cache["days"]
            self.coverage[url] = [min(days), max(days)] if days else [str(self.now.date())] * 2
            self.status = {"date": max(days) if days else None, "source": self.cache["source"],
                           "ok": bool(days), "message": "固定汇率" if days else "汇率没有取到"}
            return
        started = time.monotonic()
        code, elapsed, message = None, 0.0, ""
        try:
            get = getattr(self.fetcher, "get", self.fetcher)
            response = get(url, headers={"User-Agent": UA}, timeout=30.0)
            code, elapsed = response.status, response.elapsed_s
            if response.error or code != 200:
                message = "超时" if response.error == "timeout" else f"HTTP {code}" if code is not None else "连接失败"
                raise ValueError("汇率请求失败")
            tables = parse_rates(response.body)
            for day, rates in tables.items():
                self.cache["days"].setdefault(day, {"fetched_at": self.now.strftime("%Y-%m-%d %H:%M:%S"), "rates": rates})
            self.coverage[url] = [min(tables), max(tables)]
            self.cache["source"] = "欧洲央行参考汇率 " + url
            try:
                self._write()
            except OSError:
                self._emit({"event": "fx_cache_error", "source": url, "message": "汇率缓存写入失败"})
            self.status = {"date": max(tables), "source": self.cache["source"], "ok": True,
                           "message": f"汇率已更新至 {max(tables)[5:]}"}
            return
        except TimeoutError:
            message = "超时"
        except OSError:
            message = "连接失败"
        except (ValueError, InvalidOperation, ElementTree.ParseError, TypeError, AttributeError):
            message = message or "解析失败"
        self.failed = True
        days = self.cache["days"]
        last = max(days) if days else None
        self.status = {"date": last, "source": "欧洲央行参考汇率 " + url, "ok": False,
                       "message": f"汇率没更新，用的是 {last[5:]} 的汇率" if last else "汇率没有取到"}
        self._emit({"event": "fx_fetch_failed", "source": url, "http_code": code,
                    "message": message, "elapsed_s": elapsed or time.monotonic() - started})

    def refresh(self) -> dict:
        """更新 daily，重复调用不增加请求。"""
        self._request(DAILY_URL)
        return dict(self.status)

    def snapshot(self, event_date: date, currencies=()) -> dict:
        """公告日、抓取日、最近缓存依次取值；失败时只用最近缓存。"""
        self.refresh()
        def available(day):
            entry = self.cache["days"].get(str(day))
            return bool(entry and all(currency in entry["rates"] for currency in currencies
                                      if currency not in {None, "USD"}))
        if not self.failed and not self.fixed and not available(event_date):
            self._request(HISTORY_90D_URL)
            if not self.failed and not available(event_date) and (self.now.date() - event_date).days > 90:
                self._request(HISTORY_URL)
        days = [day for day in self.cache["days"] if available(day) and date.fromisoformat(day) <= self.now.date()]
        for currency in currencies:
            if currency not in {None, "USD"}:
                if any(currency in entry["rates"] for d, entry in self.cache["days"].items() if d <= str(self.now.date())):
                    self.missing.discard(currency)
                else:
                    self.missing.add(currency)
        day = (str(event_date) if not self.failed and available(event_date) else
               str(self.now.date()) if not self.failed and available(self.now.date()) else max(days, default=None))
        if day is None:
            self._missing_status()
            return {"base": "USD", "rates": {}, "date": None, "source": self.cache["source"], "stale": int(self.failed)}
        stale = self.failed or date.fromisoformat(day) != event_date
        if self.failed:
            self.status = {"date": day, "source": self.cache["source"], "ok": False,
                           "message": f"汇率没更新，用的是 {day[5:]} 的汇率"}
        self._missing_status()
        return {"base": "USD", "rates": dict(self.cache["days"][day]["rates"]), "date": day,
                "source": self.cache["source"], "stale": int(stale)}

    def _missing_status(self):
        if self.missing or not self.cache["days"]:
            message = "汇率没有取到" + ("：" + "、".join(sorted(self.missing)) if self.cache["days"] and self.missing else "")
            previous = self.status["message"].split(" · ")[0]
            if previous.startswith("汇率没更新"):
                message = previous + " · " + message
            self.status.update(ok=False, message=message)


def status_from_files(cfg: Config, *, currencies=()) -> dict:
    """只读运行状态及新旧缓存，不请求网络、不改缓存文件。"""
    root = cfg.state_dir / "test" if cfg.test_mode else cfg.state_dir
    recorded = None
    try:
        status = json.loads((root / "status.json").read_text(encoding="utf-8"))
        if isinstance(status.get("fx"), dict):
            recorded = status["fx"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        cache, _ = _normalise_cache(json.loads(cfg.rates_file.read_text(encoding="utf-8")))
        day = max(cache["days"]) if cache["days"] else None
        fallback = {"date": day, "source": cache["source"], "ok": bool(day),
                    "message": f"汇率日期 {day[5:]}" if day else "汇率没有取到"}
    except (OSError, ValueError, InvalidOperation, KeyError, TypeError, AttributeError):
        cache = {"days": {}}
        fallback = {"date": None, "source": SOURCE, "ok": False, "message": "汇率没有取到"}
    result = dict(recorded or fallback)
    available = set(recorded["currencies"]) if recorded is not None and "currencies" in recorded else {
        code for day in cache["days"].values() for code in day["rates"]}
    missing = sorted(set(currencies) - available - {None, "USD"})
    if missing:
        message = "汇率没有取到" + ("：" + "、".join(missing) if available else "")
        previous = result.get("message", "").split(" · ")[0]
        if previous.startswith("汇率没更新"):
            message = previous + " · " + message
        result.update(ok=False, message=message)
    return result


def rates_for_run(cfg: Config, args, *, fetcher=None, now=None, log=None, persist=True) -> FxRates:
    """统一固定汇率、失败模拟和测试隔离；清单可禁止任何缓存写入。"""
    from dataclasses import replace
    test_mode = is_test_run(args, cfg)
    cfg = replace(cfg, test_mode=test_mode)
    session = FxRates(cfg, fetcher, now=now, log=log, persist=persist and not test_mode,
                      fixed_file=getattr(args, "rates_file", None))
    if test_mode:
        session.fixed = True
    if getattr(args, "rates_fail", False):
        session.failed = True
        days = session.cache["days"]
        last = max(days, default=None)
        session.status.update(ok=False, date=last, message=(
            f"汇率没更新，用的是 {last[5:]} 的汇率" if last else "汇率没有取到"))
    return session
