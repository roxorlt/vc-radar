"""社交/社群产品标准化多源富化、热帖检索、交叉比对与 R2 媒体入库流水线。

对每一个候选产品执行统一标准流程（零单品硬编码）：
1. 官网与多区 App Store 元数据 + 官方真实好差评拉取；
2. 搜索服务（DeepSeek Search need_content=True + 公开社区）检索热门评测帖与创始人专访，并提取文章实拍配图；
3. 将图标、文章实拍图与商店截图统一按 SHA-256 镜像至 Cloudflare R2（`vc-radar-media`）；
4. 基于多方证据交叉比对，预生成 22 岁互联网产品爱好者口吻的 6 维度大白话拆解并落盘 SQLite。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import html as html_lib
from html.parser import HTMLParser
import json
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen

from . import config, db, deep_fetch, http, llm, r2, scan, search
from .config import BJ, Config, UA
from .prompts.social_dossier import (
    DOSSIER_SYSTEM_PROMPT,
    normalize_dossier_payload,
    sanitize_vernacular,
)
from .robots import RobotsChecker
from .sources.social_discovery import (
    ProductCandidate,
    clean_app_title,
    discover_appstore_charts,
    discover_hn_and_ph_communities,
    discover_vc_social_candidates,
    discover_vc_social_via_search,
    merge_candidates,
)

SEO_SCRAPER_HOSTS = (
    "apkpure.com",
    "apkcombo.com",
    "apkcube.com",
    "uptodown.com",
    "softonic.com",
    "appbrain.com",
    "appadvice.com",
    "apkmonk.com",
    "malavida.com",
    "steprimo.com",
    "sensortower.com",
    "sensortower-china.com",
    "appstorestatistics.com",
    "openaso.ai",
    "apptopia.com",
    "gamemasha.com",
    "xianz.ai",
    "qimai.cn",
    "chandashi.com",
    "data.ai",
    "similarweb.com",
    "foxdata.com",
    "mwm.ai",
    "applion.jp",
    "appgoblin.info",
    "appmagic.rocks",
    "apprankspy.com",
    "app-ranking.net",
    "chrome-stats.com",
    "androidrank.org",
    "apprecs.com",
    "appstor.io",
    "storelift.net",
    "hn.algolia.com",
    "1ting.com",
    "unstar.app",
    "fluxbuilder.com",
    "bumetric.com",
    "clubhousedb.com",
    "biyografya.com",
)

BAD_IMG_URL_RE = re.compile(
    r"(?:avatar|logo|icon|favicon|emoji|badge|sprite|spinner|pixel|tracker|1x1|qrcode|banner-ad|\.svg(?:\?|$))",
    re.I,
)
APPSTORE_LINK_RE = re.compile(r"https?://apps\.apple\.com/(?:[a-z]{2}/)?app/(?:[^/\"'\s>]+/)?id(\d{8,12})", re.I)
PLAYSTORE_LINK_RE = re.compile(r"https?://play\.google\.com/store/apps/details\?id=[A-Za-z0-9._]+", re.I)
WAITLIST_TEXT_RE = re.compile(
    r"\b(?:join\s+(?:the\s+)?waitlist|request\s+early\s+access|coming\s+soon|invite[\s-]only\s+beta|get\s+early\s+access)\b|"
    r"预约内测|加入候补|即将上线|内测申请",
    re.I,
)


class _MediaAndMetaParser(HTMLParser):
    """从公开网页或评测热帖 HTML 中提取正文配图、OpenGraph 图及商店链接。"""

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.og_image = ""
        self.touch_icon = ""
        self.images: list[str] = []
        self.links: list[str] = []
        self._in_ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k.lower(): (v or "").strip() for k, v in attrs}
        t = tag.lower()
        if t in {"nav", "footer", "header", "aside"}:
            self._in_ignored += 1
        if t == "meta":
            prop = d.get("property", "").lower() or d.get("name", "").lower()
            content = d.get("content", "")
            if prop in {"og:image", "twitter:image"} and content and not self.og_image:
                self.og_image = urljoin(self.base_url, content)
        elif t == "link":
            rel = d.get("rel", "").lower()
            href = d.get("href", "")
            if "apple-touch-icon" in rel and href and not self.touch_icon:
                self.touch_icon = urljoin(self.base_url, href)
        elif t == "a":
            href = d.get("href", "")
            if href:
                self.links.append(urljoin(self.base_url, href))
        elif t == "img" and self._in_ignored == 0:
            raw_src = d.get("data-src") or d.get("data-original") or d.get("data-lazy-src") or d.get("src") or ""
            if not raw_src or raw_src.startswith("data:"):
                return
            for attr_dim in ("width", "height"):
                val = re.sub(r"\D", "", d.get(attr_dim, ""))
                if val and int(val) < 140:
                    return
            full = urljoin(self.base_url, html_lib.unescape(raw_src))
            if full.startswith(("http://", "https://")) and not BAD_IMG_URL_RE.search(full):
                if full not in self.images:
                    self.images.append(full)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"nav", "footer", "header", "aside"} and self._in_ignored > 0:
            self._in_ignored -= 1


def extract_page_assets(html_body: str, base_url: str) -> dict:
    parser = _MediaAndMetaParser(base_url)
    try:
        parser.feed(html_body[:350_000])
    except Exception:
        pass
    app_match = APPSTORE_LINK_RE.search(html_body)
    play_match = PLAYSTORE_LINK_RE.search(html_body)
    return {
        "og_image": parser.og_image,
        "touch_icon": parser.touch_icon,
        "images": parser.images[:8],
        "app_store_id": app_match.group(1) if app_match else None,
        "app_store_url": app_match.group(0) if app_match else "",
        "play_store_url": play_match.group(0) if play_match else "",
    }


def _http_get_text(url: str, timeout_s: float = 10.0) -> tuple[int | None, str, str]:
    req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}, method="GET")
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "image/" in ctype or "video/" in ctype or "application/pdf" in ctype:
                return resp.status, "", resp.geturl()
            raw = resp.read(450_000)
            return resp.status, raw.decode("utf-8", errors="replace"), resp.geturl()
    except HTTPError as exc:
        return exc.code, "", url
    except (URLError, TimeoutError, OSError, ValueError):
        return None, "", url


def fetch_appstore_reviews(app_id: str, countries: list[str], *, http_text=_http_get_text) -> list[dict]:
    """拉取 Apple 官方最具参考价值真实评论（兼顾 4～5 星爽点与 1～2 星真实槽点）。"""
    reviews: list[dict] = []
    seen_titles: set[str] = set()
    for country in dict.fromkeys([c.lower() for c in countries if c]):
        url = f"https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortBy=mostHelpful/json"
        status, body, _ = http_text(url, 10.0)
        if status != 200 or not body:
            continue
        try:
            entries = ((json.loads(body).get("feed") or {}).get("entry")) or []
        except ValueError:
            continue
        if isinstance(entries, dict):
            entries = [entries]
        positives, negatives = [], []
        for entry in entries:
            rating_str = ((entry.get("im:rating") or {}).get("label") or "").strip()
            title = ((entry.get("title") or {}).get("label") or "").strip()
            content = ((entry.get("content") or {}).get("label") or "").strip()
            if not rating_str.isdigit() or not content or title in seen_titles:
                continue
            seen_titles.add(title)
            stars = int(rating_str)
            item = {"country": country.upper(), "stars": stars, "title": title[:80], "content": content[:260]}
            if stars >= 4 and len(positives) < 4:
                positives.append(item)
            elif stars <= 2 and len(negatives) < 4:
                negatives.append(item)
        reviews.extend(positives + negatives)
        if len(reviews) >= 8:
            break
    return reviews[:10]


def _lookup_appstore_by_id(app_id: str, country: str = "us", *, http_text=_http_get_text) -> dict | None:
    for c in dict.fromkeys([country.lower(), "us", "jp", "kr"]):
        status, body, _ = http_text(f"https://itunes.apple.com/lookup?id={app_id}&country={c}", 10.0)
        if status == 200 and body:
            try:
                results = json.loads(body).get("results") or []
                if results:
                    return results[0]
            except ValueError:
                continue
    return None


def _search_appstore_for_vc_candidate(cand: ProductCandidate, *, http_text=_http_get_text) -> dict | None:
    """当融资公司官网是纯前端渲染未能直接扫出链接时，精确比对开发者域名或名称回查 iOS 包。"""
    term = quote(cand.name[:32])
    status, body, _ = http_text(f"https://itunes.apple.com/search?term={term}&entity=software&limit=5&country=us", 10.0)
    if status != 200 or not body:
        return None
    try:
        rows = json.loads(body).get("results") or []
    except ValueError:
        return None
    cand_dom = scan.registrable_domain(cand.web_url) if cand.web_url else None
    cand_tok = search.company_token(cand.name)
    for row in rows:
        seller_dom = scan.registrable_domain(str(row.get("sellerUrl") or ""))
        title_tok = search.company_token(clean_app_title(str(row.get("trackName") or "")))
        seller_tok = search.company_token(str(row.get("sellerName") or ""))
        if cand_dom and seller_dom and cand_dom == seller_dom:
            return row
        if cand_tok and len(cand_tok) >= 4 and title_tok == cand_tok and (
            cand_tok in seller_tok or SOCIAL_WORD_IN_DESC(str(row.get("description") or ""))
        ):
            return row
    return None


def SOCIAL_WORD_IN_DESC(desc: str) -> bool:
    return bool(re.search(r"\b(?:friends?|social|community|share|chat|club)\b|社交|好友|社区", desc, re.I))


def _product_name_matches(name: str, text: str) -> bool:
    """支持中日韩无空格助词粘连（如 'Setlogは' / 'ソラを' / 'New Chat株式会社'）的名称匹配。"""
    tok = search.company_token(name)
    if not tok:
        return False
    norm_text = __import__("unicodedata").normalize("NFKC", str(text or "")).casefold()
    sep = r"[^\w]*"
    escaped = sep.join(re.escape(ch) for ch in tok)
    pre = r"(?<![a-z0-9])" if re.match(r"^[a-z0-9]", tok) else ""
    post = r"(?![a-z0-9])" if re.search(r"[a-z0-9]$", tok) else ""
    return re.search(f"{pre}{escaped}{post}", norm_text, re.I) is not None


def _is_relevant_post(cand: ProductCandidate, row: dict) -> bool:
    url = str(row.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if any(host == d or host.endswith("." + d) for d in SEO_SCRAPER_HOSTS):
        return False
    if host in {"apps.apple.com", "play.google.com"}:
        return False
    blob = " ".join(str(row.get(k) or "") for k in ("title", "snippet", "content", "url"))
    if not (_product_name_matches(cand.name, blob) or (cand.company_name and _product_name_matches(cand.company_name, blob))):
        return False
    # 短常见英文单词产品名（如 Pops / Valid / Cove）要求同时命中社交/应用/公司上下文，杜绝同名噪音
    tok = search.company_token(cand.name)
    if len(tok) <= 5 and tok.isascii():
        ctx_ok = bool(
            re.search(
                r"\b(?:app|ios|iphone|android|social|friends|community|startup|raises?|seed|founder|launch|beta|club)\b|"
                r"应用|软件|小程序|社交|社区|好友|朋友|日活|下载|融资|领投|评测|测评|实测|玩法|上线|留存|冲榜|总榜|アプリ|앱",
                blob,
                re.I,
            )
        )
        if cand.company_name and cand.company_name.lower() != cand.name.lower():
            ctx_ok = ctx_ok or _product_name_matches(cand.company_name, blob)
        if not ctx_ok:
            return False
    return True


def _source_display_name(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    known = {
        "techcrunch.com": "海外创投报道",
        "mp.weixin.qq.com": "微信公众号评测",
        "36kr.com": "36氪",
        "sspai.com": "少数派",
        "zvc.vc": "ZVC 专访",
        "prtimes.jp": "日本企业新闻通稿",
        "jiji.com": "日本时事通信社",
        "watch.impress.co.jp": "日本科技媒体",
        "thebridge.jp": "日本创投媒体",
        "note.com": "日本创作者专栏",
        "speedrun.a16z.com": "a16z 加速器名录",
        "a16z.com": "a16z 创投动态",
        "ycombinator.com": "YC 孵化名录",
        "news.ycombinator.com": "极客社区讨论",
        "reddit.com": "海外社区讨论",
        "producthunt.com": "海外新品社区",
        "substack.com": "独立专栏评测",
        "medium.com": "专栏深度文章",
        "businessinsider.com": "海外商业报道",
        "forbes.com": "福布斯报道",
        "fortune.com": "财富商业报道",
        "bloomberg.com": "彭博商业报道",
        "alleywatch.com": "纽约创投报道",
        "siliconangle.com": "硅谷创投报道",
    }
    for domain, label in known.items():
        if host == domain or host.endswith("." + domain):
            return label
    return "外部报道"


def _has_untranslated_foreign_title(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if re.search(r"[\u3040-\u30ff\uac00-\ud7af\u0590-\u05ff\u0600-\u06ff\u0400-\u04ff]", s):
        return True
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", s))
    ascii_words = len(re.findall(r"[A-Za-z]{2,}", s))
    return (not has_cjk) and ascii_words >= 2


def _complete_with_retry(llm_client: llm.LLMClient, system_prompt: str, user_prompt: str, timeout_s: float = 45.0) -> llm.LLMResult:
    """对瞬时网络波动或 5xx/429 执行最多 3 次指数退避重试。"""
    delays = (0.0, 1.0, 2.5)
    last = llm.LLMResult(ok=False, error="not_called")
    for delay in delays:
        if delay > 0:
            time.sleep(delay)
        last = llm_client.complete_json(system_prompt, user_prompt, timeout_s)
        if last.ok:
            return last
        if last.error not in {"http_429", "http_500", "http_502", "http_503", "http_504", "http_522", "timeout", "connection_error"}:
            break
    return last


def enrich_single_candidate(
    cand: ProductCandidate,
    *,
    conn: sqlite3.Connection,
    cfg: Config,
    search_client: search.SearchClient,
    llm_client: llm.LLMClient,
    media_mirror: r2.MediaMirror,
    robots: RobotsChecker | None = None,
    http_text=_http_get_text,
    now: datetime | None = None,
) -> dict | None:
    """对单个候选产品跑完「探测 -> 搜索热帖 -> 提取配图与评论 -> R2 镜像 -> 交叉比对拆解」全标准流程。"""
    stamp = (now or datetime.now(BJ)).astimezone(BJ).isoformat()
    evidences: list[dict] = list(cand.seed_sources)
    article_images: list[tuple[str, str, str]] = []  # (img_url, label, referer)

    # 1. 探测官网，提取商店链接、内测状态与官网视觉图
    homepage_excerpt = ""
    if cand.web_url:
        allowed = True if robots is None else robots.allowed(cand.web_url)
        if allowed:
            st, html_body, final_url = http_text(cand.web_url, 10.0)
            if st and 200 <= st < 300 and html_body:
                cleaned_text, _, _, _ = deep_fetch.clean_page(html_body, final_url or cand.web_url)
                homepage_excerpt = cleaned_text[:1800]
                assets = extract_page_assets(html_body, final_url or cand.web_url)
                if not cand.app_store_id and assets["app_store_id"]:
                    cand.app_store_id = assets["app_store_id"]
                    cand.app_store_url = assets["app_store_url"]
                if not cand.play_store_url and assets["play_store_url"]:
                    cand.play_store_url = assets["play_store_url"]
                if not cand.icon_url and assets["touch_icon"]:
                    cand.icon_url = assets["touch_icon"]
                if assets["og_image"]:
                    article_images.append((assets["og_image"], "官网画面", final_url or cand.web_url))
                for img_u in assets["images"][:2]:
                    article_images.append((img_u, "官网画面", final_url or cand.web_url))
                if not cand.app_store_id and not cand.play_store_url and WAITLIST_TEXT_RE.search(html_body[:25_000]):
                    cand.live_status = "waitlist"
                evidences.append(
                    {
                        "source_kind": "homepage",
                        "url": final_url or cand.web_url,
                        "source_name": "产品官网",
                        "title": f"{cand.name} 官网",
                        "excerpt": homepage_excerpt,
                    }
                )

    # 2. 若融资产品尚未拿到 App Store ID，做一次精确匹配的 iTunes Search 回查
    store_meta = None
    if not cand.app_store_id and cand.channel == "vc":
        matched = _search_appstore_for_vc_candidate(cand, http_text=http_text)
        if matched and matched.get("trackId"):
            cand.app_store_id = str(matched["trackId"])
            store_meta = matched

    if cand.app_store_id and store_meta is None:
        store_meta = _lookup_appstore_by_id(cand.app_store_id, cand.primary_country, http_text=http_text)

    if store_meta:
        cand.live_status = "ios"
        cand.app_store_url = str(store_meta.get("trackViewUrl") or cand.app_store_url).split("?")[0]
        if not cand.icon_url:
            cand.icon_url = str(store_meta.get("artworkUrl512") or store_meta.get("artworkUrl100") or "")
        if not cand.raw_screenshots:
            cand.raw_screenshots = [
                str(u) for u in (store_meta.get("screenshotUrls") or []) if isinstance(u, str) and u.startswith("http")
            ][:5]
        if not cand.description:
            cand.description = str(store_meta.get("description") or "")[:2400]
        if not cand.release_notes:
            cand.release_notes = str(store_meta.get("releaseNotes") or "")[:800]
        if not cand.first_release_date:
            cand.first_release_date = str(store_meta.get("releaseDate") or "")[:10] or None
        if cand.rating_avg is None and isinstance(store_meta.get("averageUserRating"), (int, float)):
            cand.rating_avg = round(float(store_meta["averageUserRating"]), 2)
            cand.rating_count = int(store_meta.get("userRatingCount") or 0)
        if not cand.web_url and str(store_meta.get("sellerUrl") or "").startswith("http"):
            cand.web_url = str(store_meta["sellerUrl"])
        rel_month = (cand.first_release_date or "")[:7]
        cand.release_status_text = f"iOS 已上架（{rel_month} 首发）" if rel_month else "iOS 已上架"
    elif cand.play_store_url:
        cand.live_status = "android"
        cand.release_status_text = "Android 已上架"
    elif cand.live_status == "waitlist":
        cand.release_status_text = "内测候补中（官网登记）"
    elif cand.web_url:
        cand.live_status = "web"
        cand.release_status_text = "Web 可直接访问"

    # 3. 拉取 App Store 官方真实用户好评与差评
    reviews: list[dict] = []
    if cand.app_store_id:
        reviews = fetch_appstore_reviews(
            cand.app_store_id,
            [cand.primary_country, "us", "jp"],
            http_text=http_text,
        )
        if reviews:
            review_lines = [f"[{r['country']} {r['stars']}★] {r['title']}：{r['content']}" for r in reviews]
            evidences.append(
                {
                    "source_kind": "appstore_reviews",
                    "url": cand.app_store_url or f"https://apps.apple.com/app/id{cand.app_store_id}",
                    "source_name": "App Store 真实评价",
                    "title": f"{cand.name} 用户好差评样本",
                    "excerpt": "\n".join(review_lines)[:2000],
                }
            )

    # 4. 标准流程调用搜索服务找该产品的热门帖子、评测拆解与专访数据
    anchor = ""
    if cand.company_name and cand.company_name.lower() != cand.name.lower():
        anchor = cand.company_name.split(",")[0].split("Inc")[0].strip()[:24]
    elif cand.web_url:
        anchor = (scan.registrable_domain(cand.web_url) or "")[:24]

    queries = [
        f'"{cand.name}" {f"{anchor} " if anchor else ""}app interview OR DAU OR downloads OR funding'[:120],
        *(
            [f'"{cand.name}" {f"{anchor} " if anchor else ""}funding OR "seed round" OR 融资 OR 出資 OR 資金調達 OR 투자'[:120]]
            if cand.origin_type != "bigtech"
            else []
        ),
        f'"{cand.name}" social app review OR features OR how it works'[:120],
        f'"{cand.name}" App 评测 OR 玩法 OR 社交 OR 日活'[:120],
    ]
    seen_post_urls: set[str] = {deep_fetch.url_key(e["url"]) for e in evidences if e.get("url")}
    hot_posts: list[dict] = []

    for q in queries:
        s_res = search_client.search(q, timeout_s=15.0, count=6, need_content=True)
        if not s_res.ok:
            continue
        for row in s_res.results:
            if not _is_relevant_post(cand, row):
                continue
            u = deep_fetch.normalize_url(str(row.get("url") or ""))
            ukey = deep_fetch.url_key(u)
            if not u or not ukey or ukey in seen_post_urls:
                continue
            seen_post_urls.add(ukey)
            body_text = str(row.get("content") or row.get("snippet") or row.get("summary") or "").strip()
            hot_posts.append(
                {
                    "url": u,
                    "title": str(row.get("title") or "").strip()[:120],
                    "source_name": _source_display_name(u),
                    "excerpt": body_text[:2400],
                }
            )
            if len(hot_posts) >= 6:
                break
        if len(hot_posts) >= 6:
            break

    # 补充抓取排名前 3 的热门帖子原网页，补齐正文并提取帖子里的真实配图/实拍图
    for post in hot_posts[:3]:
        f_url = deep_fetch.fetchable_url(post["url"])
        if not f_url:
            continue
        if robots is not None and not robots.allowed(f_url):
            continue
        st, html_body, final_u = http_text(f_url, 9.0)
        if st and 200 <= st < 300 and html_body:
            if len(post["excerpt"]) < 500:
                cleaned_t, _, _, _ = deep_fetch.clean_page(html_body, final_u or f_url, hn="hn.algolia.com" in f_url)
                if cleaned_t:
                    post["excerpt"] = cleaned_t[:2400]
            if "hn.algolia.com" not in f_url:
                assets = extract_page_assets(html_body, final_u or f_url)
                post_imgs = []
                for img_u in ([assets["og_image"]] if assets["og_image"] else []) + assets["images"]:
                    if img_u and img_u not in post_imgs:
                        post_imgs.append(img_u)
                        article_images.append((img_u, f"{post['source_name']}配图", final_u or f_url))
                post["media_urls"] = post_imgs[:3]

    for post in hot_posts:
        evidences.append(
            {
                "source_kind": "search_post",
                "url": post["url"],
                "source_name": post["source_name"],
                "title": post["title"],
                "excerpt": post["excerpt"][:2000],
                "media_urls": post.get("media_urls", []),
            }
        )

    # 5. 组装多方证据包调用 DeepSeek 完成准入判定、交叉比对与 6 维度大白话拆解
    evidence_blocks = []
    evidence_blocks.append(
        f"【产品基础档案】\n"
        f"- 名称：{cand.name}\n"
        f"- 出品公司/开发者：{cand.company_name}\n"
        f"- 发现来源：{cand.channel}（{cand.source_note}）\n"
        f"- 当前上架状态：{cand.release_status_text}（live_status={cand.live_status}）\n"
        f"- 融资与投资方：{cand.funding_text}｜{cand.investors_text}\n"
        f"- 多区榜单排名：{json.dumps(cand.chart_regions, ensure_ascii=False) if cand.chart_regions else '未上免费榜前百'}\n"
        f"- 商店评分：{f'{cand.rating_avg} 分（{cand.rating_count} 人评价）' if cand.rating_avg else '暂无公开评分'}\n"
        f"- 商店简介：{cand.description[:1400]}\n"
        f"- 最近更新日志：{cand.release_notes[:400]}"
    )
    if homepage_excerpt:
        evidence_blocks.append(f"【产品官网正文摘录】\n{homepage_excerpt[:1200]}")
    if reviews:
        rev_txt = "\n".join(f"- [{r['country']} {r['stars']}星] {r['title']}：{r['content']}" for r in reviews[:8])
        evidence_blocks.append(f"【App Store 真实用户好评与差评样本】\n{rev_txt}")
    if hot_posts:
        posts_txt = "\n\n".join(
            f"- 帖子 {idx}：《{p['title']}》（来源：{p['source_name']}｜URL：{p['url']}）\n  摘要：{p['excerpt'][:900]}"
            for idx, p in enumerate(hot_posts[:6], 1)
        )
        evidence_blocks.append(f"【搜索服务抓到的外部热门帖子、专访与评测】\n{posts_txt}")
    for seed in cand.seed_sources:
        if seed["source_kind"] in {"vc_article", "community_post"}:
            evidence_blocks.append(
                f"【早期报道/首发帖：《{seed.get('title', '')}》（{seed.get('source_name', '')}｜URL：{seed.get('url', '')}）】\n{seed.get('excerpt', '')[:900]}"
            )

    user_prompt = "\n\n".join(evidence_blocks)
    llm_res = _complete_with_retry(llm_client, DOSSIER_SYSTEM_PROMPT, user_prompt, timeout_s=50.0)
    if not llm_res.ok:
        return None
    try:
        parsed_llm = json.loads(llm_res.content)
    except ValueError:
        return None

    normalized = normalize_dossier_payload(parsed_llm)
    admitted = 1 if normalized["admitted"] else 0

    # 6. 对准入产品执行 Cloudflare R2 图片资源托管（图标 + 热帖实拍图 + App Store 截图）
    mirrored_icon = ""
    mirrored_shots: list[dict] = []
    if admitted:
        if cand.icon_url:
            mirrored_icon = media_mirror.mirror_url(cand.icon_url, min_bytes=1000, referer=cand.web_url, now=now) or ""

        seen_paths: set[str] = {mirrored_icon} if mirrored_icon else set()
        # 优先放至多 3 张外部评测热帖/官网实拍图（解决单看商店宣传图不够立体的问题）
        for img_u, label, ref_u in article_images:
            if len([s for s in mirrored_shots if s["label"] != "App Store 截图"]) >= 3:
                break
            path = media_mirror.mirror_url(img_u, min_bytes=16_000, referer=ref_u, now=now)
            if path and path not in seen_paths:
                seen_paths.add(path)
                mirrored_shots.append({"url": path, "label": sanitize_vernacular(label)})

        # 补充至多 4 张 App Store 官方界面截图
        for shot_u in cand.raw_screenshots[:5]:
            if len(mirrored_shots) >= 6:
                break
            path = media_mirror.mirror_url(shot_u, min_bytes=10_000, referer=cand.app_store_url, now=now)
            if path and path not in seen_paths:
                seen_paths.add(path)
                mirrored_shots.append({"url": path, "label": "App Store 截图"})

    is_bigtech = (normalized.get("origin_type") or cand.origin_type) == "bigtech"
    display_prod_name = normalized.get("localized_name") or sanitize_vernacular(cand.name) or "海外社交新品"
    display_company = normalized.get("localized_company") or sanitize_vernacular(cand.company_name or display_prod_name) or display_prod_name

    # 7. 融资回填与外部参考链接全中文整理
    unverified_funding_set = {"", "未公开披露融资", "早期独立产品", "未公开披露"}
    final_funding = sanitize_vernacular(cand.funding_text)
    final_investors = sanitize_vernacular(cand.investors_text)
    verified_funding = normalized.get("verified_funding") or ""
    verified_investors = normalized.get("verified_investors") or ""
    if not is_bigtech and verified_funding:
        if cand.channel != "vc" or final_funding in unverified_funding_set or final_funding == "早期轮次":
            final_funding = verified_funding
        if verified_investors and (
            cand.channel != "vc"
            or final_investors in unverified_funding_set
            or final_investors in {"自然冲榜新品", "极客社区首发", "新品社区首发", "早期机构投资"}
        ):
            final_investors = verified_investors
        elif not verified_investors and final_investors == "自然冲榜新品":
            final_investors = "早期创投支持"

    tr_by_url = {
        deep_fetch.url_key(tr["url"]): tr
        for tr in (normalized.get("translated_refs") or [])
        if tr.get("url")
    }

    def _build_zh_ref(u: str, raw_title: str, fallback_src: str, kind: str) -> dict:
        tr = tr_by_url.get(deep_fetch.url_key(u)) or {}
        safe_default_src = _source_display_name(u)
        cand_src = sanitize_vernacular(tr.get("source_zh") or fallback_src or safe_default_src)
        final_src = cand_src if (cand_src and not _has_untranslated_foreign_title(cand_src)) else safe_default_src
        cand_title = sanitize_vernacular(tr.get("title_zh") or raw_title or "")
        if cand_title and not _has_untranslated_foreign_title(cand_title):
            final_title = cand_title
        else:
            suffix = final_src if final_src.endswith(("报道", "评测", "专访", "讨论", "通稿")) else f"{final_src}报道"
            final_title = f"{display_prod_name} 相关{suffix}"
        return {"title": final_title, "source": final_src, "url": u, "kind": kind}

    ref_links: list[dict] = []
    seen_ref_urls: set[str] = set()
    for p in hot_posts:
        uk = deep_fetch.url_key(p["url"])
        if uk and uk not in seen_ref_urls:
            seen_ref_urls.add(uk)
            ref_links.append(_build_zh_ref(p["url"], p["title"], p["source_name"], "post"))
    for seed in cand.seed_sources:
        u = seed.get("url") or ""
        uk = deep_fetch.url_key(u)
        if u.startswith("http") and uk and uk not in seen_ref_urls and seed.get("source_kind") != "appstore_meta":
            seen_ref_urls.add(uk)
            ref_links.append(_build_zh_ref(u, seed.get("title") or "", seed.get("source_name") or "", "article"))

    badge_kind = "vc" if cand.channel == "vc" else ("warn" if is_bigtech else "hot")
    if cand.live_status == "waitlist":
        badge_kind = "warn"

    has_verified_vc = (not is_bigtech) and (
        cand.channel == "vc"
        or (bool(final_funding) and final_funding not in unverified_funding_set and not final_funding.endswith("旗下新包"))
    )

    record = {
        "product_id": cand.product_id,
        "name": display_prod_name,
        "channel": cand.channel,
        "live_status": cand.live_status,
        "origin_type": "bigtech" if is_bigtech else "indie",
        "company_id": cand.company_id,
        "company_name": display_company,
        "app_store_id": cand.app_store_id,
        "app_store_url": cand.app_store_url,
        "play_store_url": cand.play_store_url,
        "web_url": cand.web_url,
        "icon_url": mirrored_icon,
        "badge_text": normalized.get("badge_text") or sanitize_vernacular(cand.source_note)[:20],
        "badge_kind": badge_kind,
        "platform_label": normalized.get("platform_label") or sanitize_vernacular(cand.release_status_text),
        "hook": normalized.get("hook", ""),
        "mech_tags": normalized.get("mech_tags", []),
        "deep_sections": normalized.get("deep_sections", []),
        "cross_check": normalized.get("cross_check", {"verified_points": [], "divergences": []}),
        "screenshots": mirrored_shots,
        "ref_links": ref_links[:6],
        "funding_text": final_funding,
        "investors_text": final_investors,
        "release_status_text": sanitize_vernacular(cand.release_status_text),
        "source_note": sanitize_vernacular(cand.source_note),
        "chart_best_rank": cand.chart_best_rank,
        "chart_regions": cand.chart_regions,
        "rating_avg": cand.rating_avg,
        "rating_count": cand.rating_count,
        "first_release_date": cand.first_release_date,
        "priority_score": cand.priority_score
        + (15.0 if len(hot_posts) >= 2 else 0.0)
        + (8.0 if mirrored_shots else 0.0)
        + (10.0 if (cand.channel != "vc" and has_verified_vc) else 0.0),
        "admitted": admitted,
        "reject_reason": normalized.get("reject_reason", ""),
        "enriched_at": stamp,
        "updated_at": stamp,
    }

    with conn:
        conn.execute(
            """
            INSERT INTO social_product (
                product_id, name, channel, live_status, origin_type, company_id, company_name,
                app_store_id, app_store_url, play_store_url, web_url, icon_url,
                badge_text, badge_kind, platform_label, hook, mech_tags_json,
                deep_sections_json, cross_check_json, screenshots_json, ref_links_json,
                funding_text, investors_text, release_status_text, source_note,
                chart_best_rank, chart_regions_json, rating_avg, rating_count,
                first_release_date, priority_score, admitted, reject_reason, enriched_at, updated_at
            ) VALUES (
                :product_id, :name, :channel, :live_status, :origin_type, :company_id, :company_name,
                :app_store_id, :app_store_url, :play_store_url, :web_url, :icon_url,
                :badge_text, :badge_kind, :platform_label, :hook, :mech_tags_json,
                :deep_sections_json, :cross_check_json, :screenshots_json, :ref_links_json,
                :funding_text, :investors_text, :release_status_text, :source_note,
                :chart_best_rank, :chart_regions_json, :rating_avg, :rating_count,
                :first_release_date, :priority_score, :admitted, :reject_reason, :enriched_at, :updated_at
            )
            ON CONFLICT(product_id) DO UPDATE SET
                name=excluded.name, channel=excluded.channel, live_status=excluded.live_status,
                origin_type=excluded.origin_type, company_id=excluded.company_id, company_name=excluded.company_name,
                app_store_id=excluded.app_store_id, app_store_url=excluded.app_store_url,
                play_store_url=excluded.play_store_url, web_url=excluded.web_url, icon_url=excluded.icon_url,
                badge_text=excluded.badge_text, badge_kind=excluded.badge_kind, platform_label=excluded.platform_label,
                hook=excluded.hook, mech_tags_json=excluded.mech_tags_json,
                deep_sections_json=excluded.deep_sections_json, cross_check_json=excluded.cross_check_json,
                screenshots_json=excluded.screenshots_json, ref_links_json=excluded.ref_links_json,
                funding_text=excluded.funding_text, investors_text=excluded.investors_text,
                release_status_text=excluded.release_status_text, source_note=excluded.source_note,
                chart_best_rank=excluded.chart_best_rank, chart_regions_json=excluded.chart_regions_json,
                rating_avg=excluded.rating_avg, rating_count=excluded.rating_count,
                first_release_date=excluded.first_release_date, priority_score=excluded.priority_score,
                admitted=excluded.admitted, reject_reason=excluded.reject_reason,
                enriched_at=excluded.enriched_at, updated_at=excluded.updated_at
            """,
            {
                **record,
                "mech_tags_json": json.dumps(record["mech_tags"], ensure_ascii=False),
                "deep_sections_json": json.dumps(record["deep_sections"], ensure_ascii=False),
                "cross_check_json": json.dumps(record["cross_check"], ensure_ascii=False),
                "screenshots_json": json.dumps(record["screenshots"], ensure_ascii=False),
                "ref_links_json": json.dumps(record["ref_links"], ensure_ascii=False),
                "chart_regions_json": json.dumps(record["chart_regions"], ensure_ascii=False),
            },
        )
        for ev in evidences:
            if not ev.get("url"):
                continue
            conn.execute(
                """
                INSERT INTO product_evidence(product_id, source_kind, url, source_name, title, excerpt, media_urls_json, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(product_id, source_kind, url) DO UPDATE SET
                    source_name=excluded.source_name, title=excluded.title,
                    excerpt=excluded.excerpt, media_urls_json=excluded.media_urls_json, fetched_at=excluded.fetched_at
                """,
                (
                    cand.product_id,
                    ev["source_kind"],
                    ev["url"],
                    str(ev.get("source_name") or "")[:60],
                    str(ev.get("title") or "")[:160],
                    str(ev.get("excerpt") or "")[:2400],
                    json.dumps(ev.get("media_urls") or [], ensure_ascii=False),
                    stamp,
                ),
            )
    return record


def sync_social_products(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    max_candidates: int = 32,
    force_refresh: bool = False,
    candidates_override: list[ProductCandidate] | None = None,
    llm_client: llm.LLMClient | None = None,
    search_client: search.SearchClient | None = None,
    media_mirror: r2.MediaMirror | None = None,
    http_text=_http_get_text,
    now: datetime | None = None,
) -> dict:
    """运行四层社交产品发现 + 多源搜索富化 + R2 媒体镜像全流程，返回统计摘要。"""
    db.ensure_social_schema(conn)
    moment = (now or datetime.now(BJ)).astimezone(BJ)

    api_key = None if cfg.test_mode else config.load_api_key(cfg)
    if llm_client is None:
        if not api_key:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY，无法执行产品拆解生成")
        llm_client = llm.DeepSeekClient(api_key, model=cfg.model)

    transport = http.UrllibTransport()
    if search_client is None:
        search_client = search.SearchClient(transport, cfg, api_key=api_key)
    if media_mirror is None:
        media_mirror = r2.MediaMirror(conn, cfg)

    if candidates_override is not None:
        merged = list(candidates_override)
    else:
        vc_db_cands = discover_vc_social_candidates(conn)
        vc_search_cands = discover_vc_social_via_search(search_client, llm_client)
        cutoff = moment.date() - __import__("datetime").timedelta(days=cfg.social_chart_cutoff_days)
        chart_cands = discover_appstore_charts(cutoff_date=cutoff)
        comm_cands = discover_hn_and_ph_communities()
        # 均衡采样：保证早期融资通道（vc）与多国榜单/社区通道（chart）都有充足名额
        merged_all = merge_candidates(vc_search_cands + vc_db_cands, chart_cands, comm_cands)
        vc_top = [c for c in merged_all if c.channel == "vc"][: max(14, max_candidates // 2)]
        chart_top = [c for c in merged_all if c.channel != "vc"][: max(16, max_candidates - len(vc_top))]
        merged = vc_top + chart_top

    existing_enriched = set()
    if not force_refresh:
        existing_enriched = {
            row["product_id"]
            for row in conn.execute(
                "SELECT product_id FROM social_product WHERE enriched_at IS NOT NULL AND (admitted = 0 OR deep_sections_json <> '[]')"
            ).fetchall()
        }

    todo = [c for c in merged if c.product_id not in existing_enriched][:max_candidates]
    admitted_count = 0
    rejected_count = 0
    failed_count = 0

    # 为避免 SQLite 跨线程冲突，网络 IO 与搜索/模型之后在主线程顺序写库；单品异常隔离不中断全盘
    for cand in todo:
        try:
            res = enrich_single_candidate(
                cand,
                conn=conn,
                cfg=cfg,
                search_client=search_client,
                llm_client=llm_client,
                media_mirror=media_mirror,
                robots=None,
                http_text=http_text,
                now=moment,
            )
        except Exception:
            res = None
        if res is None:
            failed_count += 1
        elif res["admitted"]:
            admitted_count += 1
        else:
            rejected_count += 1

    total_admitted = conn.execute("SELECT COUNT(*) FROM social_product WHERE admitted=1").fetchone()[0]
    return {
        "discovered": len(merged),
        "processed": len(todo),
        "admitted_new": admitted_count,
        "rejected_new": rejected_count,
        "failed": failed_count,
        "total_admitted": total_admitted,
    }
