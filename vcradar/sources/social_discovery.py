"""四层社交/社群产品发现漏斗（多国 App Store 双榜新品 + 早期产品社区 + 早期融资公司池）。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import re
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET_XML

from .. import fx, scan
from ..config import BJ, UA

CHART_REGIONS: tuple[str, ...] = ("us", "jp", "kr", "tw", "hk", "gb")
CHART_GENRES: tuple[tuple[str, str], ...] = (
    ("6005", "社交"),
    ("6008", "摄影与录像"),
    ("6012", "生活"),
)
REGION_LABELS: dict[str, str] = {
    "us": "北美",
    "jp": "日本",
    "kr": "韩国",
    "tw": "台湾",
    "hk": "香港",
    "gb": "英国",
}

BIGTECH_SELLERS = re.compile(
    r"\b(?:bytedance|tiktok|poligon|spring\s+\(by|meta\s+platforms|instagram|snap\s+inc|"
    r"google\s+llc|tencent|kakao|naver|line\s+corp|yahoo\s+japan|alibaba|baidu|kuaishou|"
    r"netease|xiaohongshu|pinterest|reddit\s+inc|discord\s+inc|microsoft|apple\s+inc)\b",
    re.I,
)

UTILITY_NOISE_RE = re.compile(
    r"\b(?:vpn|proxy|adblock|qr\s*code|barcode|scanner|pdf|calculator|flashlight|cleaner|"
    r"battery|antivirus|authenticator|password|weather|alarm\s+clock|compass|translator|"
    r"remote\s+control|printer|dashcam|cctv|ip\s+camera|baby\s+monitor|parking|banking|"
    r"tax\s+|invoice|spreadsheet|watermark\s+remover|background\s+eraser|photo\s+enhancer|"
    r"upscaler|teleprompter|ringtone|wallpaper|font\s+keyboard|parcel|courier|"
    r"short\s+drama|reelshort|dramabox|shortmax|kalos\s+tv|netshort|goodshort|moboreels)\b|"
    r"扫描全能|清理大师|手电筒|万年历|天气预报|计算器|投屏|遥控器|路由器|行车记录仪|监控摄像|"
    r"短剧|看剧|免费小说|壁纸大全|铃声|输入法|网盘|压缩|解压|证件照|去水印|老照片修复|"
    r"マイナンバー|確定申告|乗換案内|ポイントカード|チラシ|クーポン|家計簿|歩数計|防犯|タクシー配車|"
    r"배달|택시|은행|보험|계산기|날씨|백신|스캐너|단편\s*드라마",
    re.I,
)

B2B_ENTERPRISE_RE = re.compile(
    r"\b(?:b2b|enterprise|cybersecurity|soc\s*2|compliance|payroll|erp|crm|devops|kubernetes|"
    r"cloud\s+infrastructure|semiconductor|defense|autonomous\s+vehicle|surgical|clinical\s+trial|"
    r"ehr|medical\s+billing|supply\s+chain|procurement|warehouse|freight|tax\s+automation|"
    r"legal\s+tech|law\s+firm|wealth\s+management|hedge\s+fund|actuarial|asic|fpga)\b",
    re.I,
)

SOCIAL_SIGNAL_RE = re.compile(
    r"\b(?:social|community|communities|friends?|besties|hangout|club|clubs|dating|matchmaking|"
    r"meet\s+people|group\s+chat|voice\s+chat|walkie|photo\s+sharing|disposable\s+camera|"
    r"widget|campus|college|co-create|multiplayer|creator|fandom|book\s+club|film\s+club|"
    r"taste|recommendations?|recs|scrapbook|journal\s+with|shared\s+album|party|events?|"
    r"invite|messaging|chat\s+with|connect\s+with)\b|"
    r"社交|社区|社群|好友|密友|朋友|交友|脱单|破冰|搭子|群聊|语音房|同好|共创|小组件|合拍|打卡|"
    r"友だち|友達|親友|コミュニティ|つながる|共有|トーク|マッチング|交換日記|"
    r"친구|커뮤니티|소셜|공유|모임|채팅|다이어리",
    re.I,
)


@dataclass
class ProductCandidate:
    product_id: str
    name: str
    channel: str  # "vc" | "chart"
    live_status: str = "ios"  # "ios" | "android" | "web" | "waitlist"
    origin_type: str = "indie"  # "indie" | "bigtech"
    company_id: str | None = None
    company_name: str = ""
    app_store_id: str | None = None
    app_store_url: str = ""
    play_store_url: str = ""
    web_url: str = ""
    icon_url: str = ""
    raw_screenshots: list[str] = field(default_factory=list)
    description: str = ""
    release_notes: str = ""
    funding_text: str = ""
    investors_text: str = ""
    release_status_text: str = ""
    source_note: str = ""
    chart_best_rank: int | None = None
    chart_regions: list[dict] = field(default_factory=list)
    rating_avg: float | None = None
    rating_count: int | None = None
    first_release_date: str | None = None
    primary_country: str = "us"
    seed_sources: list[dict] = field(default_factory=list)
    priority_score: float = 0.0


def slugify_product(name: str, fallback: str = "app") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    if slug:
        return slug[:48]
    digest = re.sub(r"[^a-z0-9]", "", fallback.lower())[:16]
    return f"p-{digest or 'item'}"


def clean_app_title(raw_title: str) -> str:
    """去掉 App Store 标题后缀卖点副标题（如 'Wabi — Create & Share Apps' -> 'Wabi'）。"""
    title = str(raw_title or "").strip()
    for sep in (" — ", " – ", " - ", " : ", "：", "｜", " | "):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if 2 <= len(head) <= 32:
                return head
    # 处理如 'Setlog: 2-Second Moments'
    if ":" in title:
        head = title.split(":", 1)[0].strip()
        if 2 <= len(head) <= 28 and not head.isdigit():
            return head
    return title[:40]


def is_obvious_utility_noise(name: str, seller: str, description: str, genres: list[str]) -> bool:
    """剔除摄影/生活榜中明显与真人社交无关的工具、短剧、硬件配套及银行政务应用。"""
    blob = f"{name}\n{seller}\n{description[:800]}"
    if UTILITY_NOISE_RE.search(blob):
        return True
    if B2B_ENTERPRISE_RE.search(blob) and not SOCIAL_SIGNAL_RE.search(name):
        return True
    genre_set = {str(g).lower() for g in genres}
    is_social_genre = any("social" in g or "ソーシャル" in g or "소셜" in g or "社交" in g for g in genre_set)
    if not is_social_genre and not SOCIAL_SIGNAL_RE.search(blob):
        return True
    return False


def _http_get_json(url: str, timeout_s: float = 15.0) -> dict | list | None:
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"}, method="GET")
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None


def discover_appstore_charts(
    *,
    regions: tuple[str, ...] = CHART_REGIONS,
    genres: tuple[tuple[str, str], ...] = CHART_GENRES,
    limit_per_chart: int = 80,
    cutoff_date: date | None = None,
    http_json=_http_get_json,
) -> list[ProductCandidate]:
    """抓取多国多分类 App Store 免费榜并批量回查 Lookup，筛选近 18 个月新上架的社交候选产品。"""
    cutoff = cutoff_date or (datetime.now(BJ).date() - timedelta(days=540))
    tasks = [(region, genre_id, genre_label) for region in regions for genre_id, genre_label in genres]
    appearances: dict[str, list[dict]] = {}
    first_region: dict[str, str] = {}

    def fetch_one(region: str, genre_id: str, genre_label: str):
        url = f"https://itunes.apple.com/{region}/rss/topfreeapplications/limit={limit_per_chart}/genre={genre_id}/json"
        payload = http_json(url)
        entries = ((payload or {}).get("feed") or {}).get("entry") or []
        if isinstance(entries, dict):
            entries = [entries]
        rows = []
        for rank, entry in enumerate(entries, 1):
            app_id = (((entry.get("id") or {}).get("attributes") or {}).get("im:id") or "").strip()
            if app_id.isdigit():
                rows.append((app_id, region, genre_id, genre_label, rank))
        return rows

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_one, r, gid, glabel) for r, gid, glabel in tasks]
        for fut in as_completed(futures):
            for app_id, region, genre_id, genre_label, rank in fut.result():
                appearances.setdefault(app_id, []).append(
                    {"region": region.upper(), "genre_id": genre_id, "genre": genre_label, "rank": rank}
                )
                if app_id not in first_region:
                    first_region[app_id] = region

    by_region_ids: dict[str, list[str]] = {}
    for app_id, ranks in appearances.items():
        best_entry = min(ranks, key=lambda x: (0 if x["genre_id"] == "6005" else 1, x["rank"]))
        lookup_country = best_entry["region"].lower()
        by_region_ids.setdefault(lookup_country, []).append(app_id)

    lookup_meta: dict[str, tuple[str, dict]] = {}

    def lookup_batch(country: str, batch_ids: list[str]):
        joined = ",".join(batch_ids)
        url = f"https://itunes.apple.com/lookup?id={quote(joined, safe=',')}&country={country}"
        payload = http_json(url)
        results = (payload or {}).get("results") if isinstance(payload, dict) else []
        out = []
        for item in results or []:
            tid = str(item.get("trackId") or "").strip()
            if tid:
                out.append((tid, country, item))
        return out

    batch_jobs = []
    for country, id_list in by_region_ids.items():
        for idx in range(0, len(id_list), 100):
            batch_jobs.append((country, id_list[idx : idx + 100]))

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(lookup_batch, c, b) for c, b in batch_jobs]
        for fut in as_completed(futures):
            for tid, country, item in fut.result():
                lookup_meta[tid] = (country, item)

    candidates: list[ProductCandidate] = []
    for app_id, (country, meta) in lookup_meta.items():
        rel_raw = str(meta.get("releaseDate") or "")[:10]
        try:
            rel_date = date.fromisoformat(rel_raw)
        except ValueError:
            continue
        if rel_date < cutoff:
            continue

        raw_name = str(meta.get("trackName") or "").strip()
        seller = str(meta.get("sellerName") or meta.get("artistName") or "").strip()
        desc = str(meta.get("description") or "").strip()
        genres_list = list(meta.get("genres") or [])
        if not raw_name or is_obvious_utility_noise(raw_name, seller, desc, genres_list):
            continue

        ranks = sorted(appearances.get(app_id, []), key=lambda r: (r["rank"], 0 if r["genre_id"] == "6005" else 1))
        best_rank = ranks[0]["rank"] if ranks else 99
        seen_reg = set()
        dedup_regions = []
        for r in ranks:
            if r["region"] not in seen_reg:
                seen_reg.add(r["region"])
                dedup_regions.append(r)

        region_summary = " · ".join(
            f"{REGION_LABELS.get(r['region'].lower(), r['region'])}{r['genre']}榜 #{r['rank']}"
            for r in dedup_regions[:3]
        )
        short_name = clean_app_title(raw_name)
        slug = slugify_product(short_name, fallback=f"ios-{app_id}")
        if slug == f"p-ios{app_id}" or len(slug) < 2:
            slug = f"app-{app_id}"

        is_bigtech = bool(BIGTECH_SELLERS.search(seller))
        rating_avg = meta.get("averageUserRating")
        rating_cnt = meta.get("userRatingCount")
        shots = [str(u) for u in (meta.get("screenshotUrls") or []) if isinstance(u, str) and u.startswith("http")][:5]
        icon = str(meta.get("artworkUrl512") or meta.get("artworkUrl100") or "").strip()
        store_url = str(meta.get("trackViewUrl") or f"https://apps.apple.com/{country}/app/id{app_id}").split("?")[0]
        seller_url = str(meta.get("sellerUrl") or "").strip()

        # 计算排序优先级：多国上榜 + 排名靠前 + 社交主榜加权 + 真实评分数加权
        genre_bonus = 18.0 if any(r["genre_id"] == "6005" for r in ranks) else 6.0
        multi_region_bonus = min(len(dedup_regions) * 8.0, 28.0)
        rank_score = max(0.0, 100.0 - best_rank)
        rating_bonus = min(15.0, (int(rating_cnt or 0) ** 0.35))
        score = rank_score + genre_bonus + multi_region_bonus + rating_bonus

        candidates.append(
            ProductCandidate(
                product_id=slug,
                name=short_name,
                channel="chart",
                live_status="ios",
                origin_type="bigtech" if is_bigtech else "indie",
                company_name=seller,
                app_store_id=app_id,
                app_store_url=store_url,
                web_url=seller_url if seller_url.startswith("http") else "",
                icon_url=icon,
                raw_screenshots=shots,
                description=desc[:2400],
                release_notes=str(meta.get("releaseNotes") or "")[:800],
                funding_text="未公开披露融资" if not is_bigtech else f"{seller} 旗下新包",
                investors_text="自然冲榜新品" if not is_bigtech else seller,
                release_status_text=f"iOS 已上架（{rel_raw[:7]} 首发）",
                source_note=region_summary or "App Store 免费榜新品",
                chart_best_rank=best_rank,
                chart_regions=dedup_regions[:4],
                rating_avg=round(float(rating_avg), 2) if isinstance(rating_avg, (int, float)) else None,
                rating_count=int(rating_cnt) if isinstance(rating_cnt, (int, float)) else None,
                first_release_date=rel_raw,
                primary_country=country,
                seed_sources=[
                    {
                        "source_kind": "appstore_meta",
                        "url": store_url,
                        "source_name": f"App Store ({country.upper()})",
                        "title": raw_name,
                        "excerpt": f"{region_summary}\n开发者：{seller}\n首发日期：{rel_raw}\n简介：{desc[:1200]}",
                    }
                ],
                priority_score=score,
            )
        )
    candidates.sort(key=lambda c: c.priority_score, reverse=True)
    return candidates


def discover_hn_and_ph_communities(*, http_json=_http_get_json) -> list[ProductCandidate]:
    """从 Hacker News Show HN 与 Product Hunt 公开 Feed 发现最新上线的社交/社区产品。"""
    found: list[ProductCandidate] = []
    hn_url = (
        "https://hn.algolia.com/api/v1/search_by_date?"
        "tags=show_hn&query=social%20OR%20friends%20OR%20community%20OR%20club&hitsPerPage=20"
    )
    payload = http_json(hn_url)
    for hit in (payload or {}).get("hits") or [] if isinstance(payload, dict) else []:
        title = str(hit.get("title") or "").strip()
        ext_url = str(hit.get("url") or "").strip()
        if not ext_url.startswith("http") or B2B_ENTERPRISE_RE.search(title):
            continue
        if not SOCIAL_SIGNAL_RE.search(title + " " + str(hit.get("story_text") or "")):
            continue
        cleaned = re.sub(r"^Show\s+HN\s*:\s*", "", title, flags=re.I)
        prod_name = clean_app_title(cleaned)
        if not prod_name or len(prod_name) > 36:
            continue
        hn_item_url = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        points = int(hit.get("points") or 0)
        found.append(
            ProductCandidate(
                product_id=slugify_product(prod_name, fallback=str(hit.get("objectID") or "hn")),
                name=prod_name,
                channel="chart",
                live_status="web",
                origin_type="indie",
                company_name=prod_name,
                web_url=ext_url,
                description=f"{cleaned}\n{str(hit.get('story_text') or '')[:800]}".strip(),
                funding_text="早期独立产品",
                investors_text="Show HN 社区首发",
                release_status_text="Web 已上线",
                source_note=f"Hacker News Show HN（{points} 赞）",
                seed_sources=[
                    {
                        "source_kind": "community_post",
                        "url": hn_item_url,
                        "source_name": "Hacker News",
                        "title": title,
                        "excerpt": f"{cleaned}\n{str(hit.get('story_text') or '')[:1000]}",
                    }
                ],
                priority_score=35.0 + min(points, 50),
            )
        )

    # Product Hunt 公开 Atom Feed 轻量补充
    req = Request("https://www.producthunt.com/feed", headers={"User-Agent": UA}, method="GET")
    try:
        with urlopen(req, timeout=12) as resp:
            if resp.status == 200:
                root = ET_XML.fromstring(resp.read().decode("utf-8", errors="replace"))
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                for entry in root.findall("atom:entry", ns)[:25]:
                    title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
                    summary = re.sub(r"<[^>]+>", " ", entry.findtext("atom:content", default="", namespaces=ns) or "")
                    summary = " ".join(summary.split())
                    link_el = entry.find("atom:link", ns)
                    href = (link_el.attrib.get("href") if link_el is not None else "") or ""
                    blob = f"{title} {summary}"
                    if not href.startswith("http") or B2B_ENTERPRISE_RE.search(blob) or UTILITY_NOISE_RE.search(blob):
                        continue
                    if not SOCIAL_SIGNAL_RE.search(blob):
                        continue
                    prod_name = clean_app_title(title)
                    found.append(
                        ProductCandidate(
                            product_id=slugify_product(prod_name, fallback="ph"),
                            name=prod_name,
                            channel="chart",
                            live_status="web",
                            origin_type="indie",
                            company_name=prod_name,
                            web_url=href,
                            description=summary[:1000],
                            funding_text="早期独立产品",
                            investors_text="Product Hunt 首发",
                            release_status_text="产品已上线",
                            source_note="Product Hunt 每日新品",
                            seed_sources=[
                                {
                                    "source_kind": "community_post",
                                    "url": href,
                                    "source_name": "Product Hunt",
                                    "title": title,
                                    "excerpt": summary[:800],
                                }
                            ],
                            priority_score=42.0,
                        )
                    )
    except Exception:
        pass
    return found


def discover_vc_social_candidates(conn: sqlite3.Connection) -> list[ProductCandidate]:
    """从 SQLite 早期融资库提取所有社交/社区向候选公司，保留完整轮次、金额与投资方信息。"""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"company", "funding_event"} <= tables:
        return []

    rows = conn.execute(
        """
        SELECT c.id AS company_id, c.name, c.website, c.summary, c.one_liner, c.yc_batch,
               c.ai_label, c.social_label, c.manual_label, c.country,
               e.id AS event_id, e.round_norm, e.round_text, e.amount_text, e.amount_usd,
               e.amount_status, e.leads_json, e.follows_json, e.event_date, e.filter_reasons_json
        FROM company c
        JOIN funding_event e ON e.company_id = c.id
        ORDER BY e.event_date DESC, e.id DESC
        """
    ).fetchall()

    seen_companies: set[str] = set()
    candidates: list[ProductCandidate] = []
    for row in rows:
        cid = row["company_id"]
        if cid in seen_companies:
            continue
        reasons = json.loads(row["filter_reasons_json"] or "[]")
        if reasons or row["round_norm"] == "过滤":
            continue

        manual = str(row["manual_label"] or "")
        is_labeled_social = row["social_label"] == "是" or "社交/社群相关" in manual
        blob = f"{row['name']} {row['one_liner']} {row['summary']}"
        if not is_labeled_social:
            if B2B_ENTERPRISE_RE.search(blob) or not SOCIAL_SIGNAL_RE.search(blob):
                continue
        elif B2B_ENTERPRISE_RE.search(blob) and not SOCIAL_SIGNAL_RE.search(blob):
            continue

        seen_companies.add(cid)
        leads = [x for x in json.loads(row["leads_json"] or "[]") if isinstance(x, str) and x.strip()]
        follows = [x for x in json.loads(row["follows_json"] or "[]") if isinstance(x, str) and x.strip()]
        investors = "、".join((leads + follows)[:3]) or ("Y Combinator" if row["yc_batch"] else "投资方未披露")
        if leads:
            investors = f"{'、'.join(leads[:2])} 领投" + (f" · {'、'.join(follows[:2])} 参投" if follows else "")

        amt_str = fx.format_amount(dict(row), row["amount_text"]) if row["amount_status"] == "披露" and row["amount_text"] else ""
        round_str = row["round_norm"] if row["round_norm"] not in {"待核", "过滤", ""} else (f"YC {row['yc_batch']}" if row["yc_batch"] else "早期轮次")
        funding_display = f"{round_str} · {amt_str}" if amt_str and "未披露" not in amt_str else round_str

        src_rows = conn.execute(
            """
            SELECT s.url, s.source_name, r.title, r.excerpt, r.structured_json
            FROM event_source s
            JOIN raw_item r ON r.id = s.raw_item_id
            WHERE s.event_id = ?
            ORDER BY r.id ASC
            """,
            (row["event_id"],),
        ).fetchall()

        website = str(row["website"] or "").strip()
        seed_sources = []
        for s in src_rows:
            if not website:
                st = json.loads(s["structured_json"] or "{}")
                if isinstance(st.get("website"), str) and st["website"].startswith("http"):
                    website = st["website"]
            seed_sources.append(
                {
                    "source_kind": "vc_article",
                    "url": s["url"],
                    "source_name": s["source_name"],
                    "title": s["title"],
                    "excerpt": s["excerpt"][:1200],
                }
            )

        usd_bonus = min(40.0, float(row["amount_usd"] or 0) / 500_000.0)
        label_bonus = 35.0 if is_labeled_social else 10.0
        candidates.append(
            ProductCandidate(
                product_id=slugify_product(row["name"], fallback=cid),
                name=row["name"],
                channel="vc",
                live_status="web" if website else "waitlist",
                origin_type="indie",
                company_id=cid,
                company_name=row["name"],
                web_url=website,
                description=(row["summary"] or row["one_liner"] or "").strip(),
                funding_text=funding_display,
                investors_text=investors,
                release_status_text="待探测上线状态",
                source_note=f"{seed_sources[0]['source_name']} 报道" if seed_sources else "早期融资收录",
                seed_sources=seed_sources,
                priority_score=60.0 + label_bonus + usd_bonus,
            )
        )

    candidates.sort(key=lambda c: c.priority_score, reverse=True)
    return candidates


def merge_candidates(
    vc_list: list[ProductCandidate],
    chart_list: list[ProductCandidate],
    community_list: list[ProductCandidate],
) -> list[ProductCandidate]:
    """合并去重三路候选：同域名或同名产品把榜单表现与融资记录自动合流。"""
    merged: dict[str, ProductCandidate] = {}
    by_domain: dict[str, str] = {}
    by_app_id: dict[str, str] = {}

    def _dom(url: str) -> str:
        d = scan.registrable_domain(url) if url else None
        if d in {None, "apple.com", "google.com", "producthunt.com", "ycombinator.com"}:
            return ""
        return d or ""

    for item in vc_list + chart_list + community_list:
        dom = _dom(item.web_url)
        existing_key = None
        if item.app_store_id and item.app_store_id in by_app_id:
            existing_key = by_app_id[item.app_store_id]
        elif dom and dom in by_domain:
            existing_key = by_domain[dom]
        elif item.product_id in merged:
            existing_key = item.product_id

        if existing_key:
            target = merged[existing_key]
            if not target.app_store_id and item.app_store_id:
                target.app_store_id = item.app_store_id
                target.app_store_url = item.app_store_url
                target.live_status = "ios"
            if not target.icon_url and item.icon_url:
                target.icon_url = item.icon_url
            if not target.raw_screenshots and item.raw_screenshots:
                target.raw_screenshots = list(item.raw_screenshots)
            if not target.web_url and item.web_url:
                target.web_url = item.web_url
            if item.chart_best_rank is not None and (
                target.chart_best_rank is None or item.chart_best_rank < target.chart_best_rank
            ):
                target.chart_best_rank = item.chart_best_rank
                target.chart_regions = item.chart_regions
            if item.rating_avg and not target.rating_avg:
                target.rating_avg = item.rating_avg
                target.rating_count = item.rating_count
            target.seed_sources.extend(item.seed_sources)
            target.priority_score = max(target.priority_score, item.priority_score) + 12.0
            continue

        pid = item.product_id
        if pid in merged:
            pid = f"{pid}-{len(merged) + 1}"
            item.product_id = pid
        merged[pid] = item
        if item.app_store_id:
            by_app_id[item.app_store_id] = pid
        if dom:
            by_domain[dom] = pid

    return sorted(merged.values(), key=lambda c: c.priority_score, reverse=True)
