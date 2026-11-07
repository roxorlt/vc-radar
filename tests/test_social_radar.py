"""Social Radar 产品发现漏斗、多源热帖检索交叉比对、R2 媒体镜像与浮层防穿透全链路测试。"""
from __future__ import annotations

from datetime import date, datetime
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import zlib

import pytest

from vcradar import db, deep_fetch, http, llm, product_pipeline, r2, search, site
from vcradar.config import BJ, Config
from vcradar.prompts.social_dossier import (
    DOSSIER_SYSTEM_PROMPT,
    SECTION_TITLES,
    normalize_dossier_payload,
    sanitize_vernacular,
)
from vcradar.sources.social_discovery import (
    ProductCandidate,
    clean_app_title,
    discover_appstore_charts,
    discover_vc_social_candidates,
    is_obvious_utility_noise,
    merge_candidates,
)
from test_v16_wording import clean as assert_clean_wording, copy_strings

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=BJ)

def _make_decodable_png(pad_bytes: int = 2048, rgb: tuple[int, int, int] = (79, 70, 229)) -> bytes:
    """构造可被真实 Chromium Blink 解码器正常解码的合法 PNG 字节流。"""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    w, h = 16, 16
    raw_rows = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw_rows))
        + chunk(b"tEXt", b"Comment\x00" + (b"x" * pad_bytes))
        + chunk(b"IEND", b"")
    )


PNG_ICON_BYTES = _make_decodable_png(2048, (79, 70, 229))
PNG_SHOT_BYTES = _make_decodable_png(14_000, (16, 185, 129))
JPEG_ARTICLE_PHOTO_A = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"A" * 18_500 + b"\xff\xd9"
JPEG_ARTICLE_PHOTO_B = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"B" * 19_000 + b"\xff\xd9"
WEBP_STORE_SHOT = b"RIFF\x24\x40\x00\x00WEBPVP8 " + b"S" * 14_000


def test_r2_sniff_sigv4_and_content_addressed_deduplication(tmp_path):
    assert r2.sniff_image(PNG_ICON_BYTES) == ("png", "image/png")
    assert r2.sniff_image(JPEG_ARTICLE_PHOTO_A) == ("jpg", "image/jpeg")
    assert r2.sniff_image(WEBP_STORE_SHOT) == ("webp", "image/webp")
    assert r2.sniff_image(b"<html>not an image</html>" * 100) is None
    assert r2.sniff_image(b"\xff\xd8\xffshort") is None

    s3_url, headers = r2._s3_sigv4_headers(
        method="PUT",
        account_id="acc123",
        bucket="vc-radar-media",
        key="deadbeef.jpg",
        access_key="AKIA_TEST",
        secret_key="SECRET_TEST",
        content_type="image/jpeg",
        payload=b"hello",
        now_utc=datetime(2026, 11, 7, 8, 0, tzinfo=BJ),
    )
    assert s3_url == "https://acc123.r2.cloudflarestorage.com/vc-radar-media/deadbeef.jpg"
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIA_TEST/20261107/auto/s3/aws4_request")
    assert "SECRET_TEST" not in json.dumps(headers)

    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    conn = db.connect(cfg.db_path)
    db.ensure_social_schema(conn)
    uploads: list[tuple[str, str, int]] = []

    payload_by_url = {
        "https://cdn.test/tiny-pixel.jpg": b"\xff\xd8\xff\xe0" + b"x" * 200,
        "https://cdn.test/post-shot-1.jpg": JPEG_ARTICLE_PHOTO_A,
        "https://mirror.test/same-bytes.jpg": JPEG_ARTICLE_PHOTO_A,
        "https://cdn.test/not-img.html": b"<!doctype html>" + b"z" * 20_000,
    }

    mirror = r2.MediaMirror(
        conn,
        cfg,
        r2_cfg=r2.R2Config(bucket="vc-radar-media", public_base="/media", account_id="acc123", api_token="tok_test"),
        fetcher=lambda url, ref="": payload_by_url.get(url),
        uploader=lambda key, raw, ctype: uploads.append((key, ctype, len(raw))) or True,
    )

    # 低于最小字节阈值或非图片魔数一律拦截
    assert mirror.mirror_url("https://cdn.test/tiny-pixel.jpg", min_bytes=16_000, now=NOW) is None
    assert mirror.mirror_url("https://cdn.test/not-img.html", min_bytes=1000, now=NOW) is None

    # 不同 URL 指向同一图片内容时，按 SHA-256 去重只上传一次 R2
    p1 = mirror.mirror_url("https://cdn.test/post-shot-1.jpg", min_bytes=16_000, now=NOW)
    p2 = mirror.mirror_url("https://mirror.test/same-bytes.jpg", min_bytes=16_000, now=NOW)
    assert p1 is not None and p1 == p2
    assert p1.startswith("/media/") and p1.endswith(".jpg")
    assert len(uploads) == 1
    assert (mirror.local_dir / p1.removeprefix("/media/")).is_file()
    conn.close()


def test_discovery_funnel_charts_vc_and_dedup_merge(tmp_path):
    from vcradar.sources.social_discovery import discover_vc_social_via_search

    # 回归验证真实网页出现无值布尔属性 <div class><a href> 时 clean_page 不抛 AttributeError
    cleaned_html, _, _, _ = deep_fetch.clean_page("<html><body><div class><a href>Social Club</a></div></body></html>", "https://club.test")
    assert "Social Club" in cleaned_html

    assert clean_app_title("Setlog: 2-Second Moments") == "Setlog"
    assert clean_app_title("Wabi — Create & Share Mini Apps") == "Wabi"
    assert is_obvious_utility_noise("Super VPN Proxy", "Tools Ltd", "Fast proxy", ["Lifestyle"])
    assert is_obvious_utility_noise("ReelShort - Short Drama", "CrazyMaple", "Watch short drama", ["Entertainment"])
    assert is_obvious_utility_noise("探探-全球交友高速約會App", "Tantan", "Swipe dating", ["Social Networking"])
    assert not is_obvious_utility_noise("Setlog", "beni inc.", "Share 2-second moments with close friends", ["Social Networking"])

    class _DealTransport(http.Transport):
        def get(self, url, *, headers=None, timeout=20.0, data=None):
            return http.HttpResponse(url, 200, "application/json", json.dumps({"results": [
                {"title": "SuperMe raises $6M seed for taste-based social profiles", "url": "https://techcrunch.com/2026/05/superme-seed", "content": "SuperMe social app raises $6M seed led by First Round."},
            ]}))

    class _DealLLM(llm.LLMClient):
        def complete_json(self, sys_p, usr_p, timeout_s=30.0):
            return llm.LLMResult(ok=True, content=json.dumps({"deals": [{
                "product_name": "SuperMe",
                "company_name": "SuperMe Inc.",
                "web_url": "https://superme.com",
                "funding_text": "种子轮 · $6M",
                "investors_text": "First Round 领投",
                "summary": "Taste-based social profile network",
                "source_url": "https://techcrunch.com/2026/05/superme-seed",
                "source_title": "SuperMe raises $6M seed",
            }]}))

    vc_from_search = discover_vc_social_via_search(
        search.SearchClient(_DealTransport(), api_key="sk-test"),
        _DealLLM(),
    )
    assert len(vc_from_search) == 1 and vc_from_search[0].name == "SuperMe"
    assert "600 万美元" in vc_from_search[0].funding_text

    def fake_chart_http(url: str):
        if "/jp/rss/topfreeapplications/" in url and "genre=6005" in url:
            return {"feed": {"entry": [
                {"id": {"attributes": {"im:id": "6500000001"}}},
                {"id": {"attributes": {"im:id": "6500000002"}}},
                {"id": {"attributes": {"im:id": "389801252"}}},  # 老巨头应按首发日期剔除
            ]}}
        if "/kr/rss/topfreeapplications/" in url and "genre=6005" in url:
            return {"feed": {"entry": [
                {"id": {"attributes": {"im:id": "6500000001"}}},
                {"id": {"attributes": {"im:id": "6500000003"}}},
            ]}}
        if "itunes.apple.com/lookup" in url:
            return {"results": [
                {
                    "trackId": 6500000001,
                    "trackName": "Setlog - 2秒日常朋友社区",
                    "sellerName": "beni inc.",
                    "sellerUrl": "https://setlog.app",
                    "releaseDate": "2025-07-12T00:00:00Z",
                    "genres": ["Social Networking"],
                    "description": "Share 2-second video diaries with close friends.",
                    "averageUserRating": 4.7,
                    "userRatingCount": 8200,
                    "artworkUrl512": "https://img.test/setlog-icon.png",
                    "screenshotUrls": ["https://img.test/setlog-s1.webp"],
                },
                {
                    "trackId": 6500000002,
                    "trackName": "Super PDF Scanner",
                    "sellerName": "Scan Corp",
                    "releaseDate": "2026-02-01T00:00:00Z",
                    "genres": ["Social Networking"],
                    "description": "Scan PDF and QR code documents quickly.",
                },
                {
                    "trackId": 6500000003,
                    "trackName": "Whee — Close Friends Camera",
                    "sellerName": "ByteDance Pte. Ltd.",
                    "releaseDate": "2025-09-01T00:00:00Z",
                    "genres": ["Social Networking"],
                    "description": "Share candid photos only with close friends.",
                    "averageUserRating": 4.3,
                    "userRatingCount": 900,
                },
                {
                    "trackId": 389801252,
                    "trackName": "Instagram",
                    "sellerName": "Instagram, Inc.",
                    "releaseDate": "2010-10-06T00:00:00Z",
                    "genres": ["Photo & Video", "Social Networking"],
                    "description": "Connect with friends.",
                },
            ]}
        return {"feed": {"entry": []}}

    chart_cands = discover_appstore_charts(
        regions=("jp", "kr"),
        genres=(("6005", "社交"),),
        cutoff_date=date(2025, 5, 1),
        http_json=fake_chart_http,
    )
    by_name = {c.name: c for c in chart_cands}
    assert set(by_name) == {"Setlog", "Whee"}
    assert by_name["Setlog"].origin_type == "indie"
    assert len(by_name["Setlog"].chart_regions) == 2  # 日本 + 韩国双榜聚合
    assert by_name["Whee"].origin_type == "bigtech"

    # 测试早期融资池提取与三路合流（同域名产品同时保留 VC 轮次与榜单表现）
    conn = db.connect(tmp_path / "vc.db")
    conn.execute(
        """INSERT INTO company(id, name, name_key, website, summary, one_liner, social_label, ai_label, status, first_seen)
           VALUES ('wabi-inc', 'Wabi', 'wabi', 'https://wabi.ai', 'Friends create and share social mini-apps together.',
                   'Social mini-app community', '是', '是', '已纳入', '2026-10-28')"""
    )
    conn.execute(
        """INSERT INTO company(id, name, name_key, website, summary, one_liner, social_label, ai_label, status, first_seen)
           VALUES ('sec-saas', 'VaultSOC', 'vaultsoc', 'https://vaultsoc.io', 'Enterprise SOC 2 compliance automation platform.',
                   'B2B cybersecurity', '否', '是', '已纳入', '2026-10-28')"""
    )
    conn.execute(
        """INSERT INTO funding_event(id, company_id, round_norm, round_text, amount_text, amount_usd, amount_status,
           leads_json, follows_json, event_date, first_seen, first_ingested_at, filter_reasons_json)
           VALUES (1, 'wabi-inc', 'Pre-Seed', 'Pre-Seed', '$20M', 20000000, '披露',
                   '["a16z"]', '["Conviction"]', '2026-10-28', '2026-10-28', '2026-10-28T09:00:00+08:00', '[]')"""
    )
    conn.execute(
        """INSERT INTO funding_event(id, company_id, round_norm, round_text, amount_text, amount_usd, amount_status,
           leads_json, follows_json, event_date, first_seen, first_ingested_at, filter_reasons_json)
           VALUES (2, 'sec-saas', '种子轮', 'Seed', '$5M', 5000000, '披露',
                   '["Index"]', '[]', '2026-10-28', '2026-10-28', '2026-10-28T09:00:00+08:00', '[]')"""
    )
    conn.commit()
    vc_cands = discover_vc_social_candidates(conn)
    assert [c.name for c in vc_cands] == ["Wabi"]
    assert "2000 万美元" in vc_cands[0].funding_text and "$20M" not in vc_cands[0].funding_text
    assert "a16z 领投" in vc_cands[0].investors_text

    wabi_chart_dup = ProductCandidate(
        product_id="wabi-ios",
        name="Wabi",
        channel="chart",
        live_status="ios",
        app_store_id="6700000099",
        app_store_url="https://apps.apple.com/us/app/wabi/id6700000099",
        web_url="https://www.wabi.ai/download",
        chart_best_rank=18,
        chart_regions=[{"region": "US", "genre_id": "6005", "genre": "社交", "rank": 18}],
    )
    merged = merge_candidates(vc_cands, [wabi_chart_dup, by_name["Setlog"]], [])
    wabi_merged = next(c for c in merged if c.name == "Wabi")
    assert wabi_merged.channel == "vc"
    assert wabi_merged.app_store_id == "6700000099"
    assert wabi_merged.chart_best_rank == 18
    conn.close()


def test_standardized_multi_product_enrichment_crosscheck_and_site_export(tmp_path):
    """验证标准流水线对多个不同产品（非单品定制）统一执行搜索热帖、好差评抓取、实拍配图 R2 镜像与交叉比对。"""
    assert "22 岁" in DOSSIER_SYSTEM_PROMPT
    assert SECTION_TITLES[0] == "产品形态与切入点"
    cleaned = sanitize_vernacular("通过构建关系矩阵赋能用户沉淀闭环，融资 $7.5M，总部位于东京，信源待核")
    assert "赋能" not in cleaned and "矩阵" not in cleaned and "闭环" not in cleaned
    assert "750 万美元" in cleaned
    assert_clean_wording(cleaned)

    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    conn = db.connect(cfg.db_path)
    db.ensure_social_schema(conn)

    candidates = [
        ProductCandidate(
            product_id="wabi",
            name="Wabi",
            channel="vc",
            live_status="web",
            origin_type="indie",
            company_id="wabi-inc",
            company_name="Wabi Inc.",
            web_url="https://wabi.ai",
            funding_text="Pre-Seed · 2000 万美元",
            investors_text="a16z 领投",
            source_note="TechCrunch 报道",
            seed_sources=[{"source_kind": "vc_article", "url": "https://techcrunch.com/2026/10/wabi-seed", "source_name": "TechCrunch", "title": "Wabi raises Pre-Seed", "excerpt": "Eugenia Kuyda launches Wabi social mini apps."}],
            priority_score=110.0,
        ),
        ProductCandidate(
            product_id="setlog",
            name="Setlog",
            channel="chart",
            live_status="ios",
            origin_type="indie",
            company_name="beni inc.",
            app_store_id="6500000001",
            app_store_url="https://apps.apple.com/jp/app/id6500000001",
            icon_url="https://img.test/setlog-icon.png",
            raw_screenshots=["https://img.test/setlog-store-1.webp"],
            description="2-second video journal for close friends.",
            funding_text="未公开披露融资",
            investors_text="自然冲榜新品",
            release_status_text="iOS 已上架（2025-07 首发）",
            source_note="日本社交榜 #2 · 韩国社交榜 #4",
            chart_best_rank=2,
            chart_regions=[{"region": "JP", "genre_id": "6005", "genre": "社交", "rank": 2}],
            rating_avg=4.7,
            rating_count=8200,
            primary_country="jp",
            priority_score=125.0,
        ),
        ProductCandidate(
            product_id="clubroom",
            name="Clubroom",
            channel="vc",
            live_status="web",
            origin_type="indie",
            company_name="Clubroom Labs",
            web_url="https://clubroom.social",
            funding_text="种子轮 · 420 万美元",
            investors_text="Initialized 领投",
            source_note="YC 名单收录",
            priority_score=85.0,
        ),
        ProductCandidate(
            product_id="whee",
            name="Whee",
            channel="chart",
            live_status="ios",
            origin_type="bigtech",
            company_name="ByteDance Pte. Ltd.",
            app_store_id="6500000003",
            app_store_url="https://apps.apple.com/tw/app/id6500000003",
            icon_url="https://img.test/whee-icon.png",
            raw_screenshots=["https://img.test/whee-store-1.webp"],
            funding_text="ByteDance 旗下新包",
            investors_text="ByteDance",
            release_status_text="iOS 已上架",
            source_note="台湾社交榜 #6",
            priority_score=80.0,
        ),
        ProductCandidate(
            product_id="pure-vpn",
            name="PureVPN Tool",
            channel="chart",
            live_status="ios",
            company_name="NetUtil",
            app_store_id="6500000099",
            description="VPN utility app",
            priority_score=40.0,
        ),
    ]

    # Mock HTTP 网页、App Store Lookup 与官方好差评 RSS（每个产品都按统一规则命中最具帮助好差评与热帖）
    def fake_http_text(url: str, timeout_s: float = 10.0):
        if url == "https://wabi.ai":
            return 200, """<html><head><link rel="apple-touch-icon" href="https://img.test/wabi-icon.png">
                <meta property="og:image" content="https://img.test/wabi-og.jpg"></head>
                <body><a href="https://apps.apple.com/us/app/wabi-social/id6700000099">Download iOS</a>
                <p>Make tiny apps for your friend group in 30 seconds.</p></body></html>""", url
        if url == "https://clubroom.social":
            return 200, """<html><body><h1>Clubroom</h1><p>Join the waitlist for invite-only beta voice tables.</p></body></html>""", url
        if "itunes.apple.com/lookup?id=6700000099" in url:
            return 200, json.dumps({"results": [{
                "trackId": 6700000099,
                "trackName": "Wabi",
                "trackViewUrl": "https://apps.apple.com/us/app/wabi-social/id6700000099",
                "releaseDate": "2026-08-15T00:00:00Z",
                "averageUserRating": 4.8,
                "userRatingCount": 310,
                "screenshotUrls": ["https://img.test/wabi-store-1.webp"],
                "description": "Create and remix mini social apps with friends.",
            }]}), url
        if "itunes.apple.com/lookup?id=6500000001" in url:
            return 200, json.dumps({"results": [{
                "trackId": 6500000001,
                "trackName": "Setlog",
                "trackViewUrl": "https://apps.apple.com/jp/app/id6500000001",
                "releaseDate": "2025-07-12T00:00:00Z",
                "averageUserRating": 4.7,
                "userRatingCount": 8200,
                "artworkUrl512": "https://img.test/setlog-icon.png",
                "screenshotUrls": ["https://img.test/setlog-store-1.webp"],
                "description": "2-second daily moments.",
            }]}), url
        if "customerreviews/id=" in url:
            return 200, json.dumps({"feed": {"entry": [
                {"im:rating": {"label": "5"}, "title": {"label": "跟闺蜜每天必玩"}, "content": {"label": "不用修图没有点赞焦虑，每天随手拍两秒特别轻松。"}},
                {"im:rating": {"label": "1"}, "title": {"label": "新版有点卡"}, "content": {"label": "昨天更新之后安卓通知偶尔收不到，希望能尽快修好。"}},
            ]}}), url
        if "mp.weixin.qq.com/s/setlog-review" in url:
            return 200, """<html><head><meta property="og:image" content="https://img.test/wx-setlog-real.jpg"></head>
                <body><article><p>2个人搓出来的 Setlog 冲上日本总榜第一，日活突破 750 万。</p>
                <img data-src="https://img.test/wx-setlog-feed.jpg" width="600" height="800"></article></body></html>""", url
        if "sspai.com/post/wabi-review" in url:
            return 200, """<html><body><article><p>实测 Wabi：把小程序做成朋友之间的表情包，群聊里一句话就能改玩法。</p>
                <img src="https://img.test/sspai-wabi-real.jpg" width="720" height="900"></article></body></html>""", url
        return 404, "", url

    # Mock 搜索服务：对所有候选产品按查询词返回对应的外部评测热帖，同时混入 SEO 垃圾站验证过滤
    class FakeSearchTransport(http.Transport):
        def get(self, url, *, headers=None, timeout=20.0, data=None):
            payload = json.loads(data.decode("utf-8")) if data else {}
            q = payload.get("query", "")
            assert payload.get("need_content") is True
            if "Wabi" in q:
                return http.HttpResponse(url, 200, "application/json", json.dumps({"results": [
                    {"title": "实测 Wabi：a16z 领投的朋友圈互动小玩具工坊", "url": "https://sspai.com/post/wabi-review", "content": "Wabi 让普通人用一句话做出跟死党一起打卡投票的小程序，上线首月留存惊人。"},
                    {"title": "Wabi APK Download", "url": "https://apkpure.com/wabi", "content": "Wabi social app download"},
                ]}))
            if "Setlog" in q:
                return http.HttpResponse(url, 200, "application/json", json.dumps({"results": [
                    {"title": "2个人做出的 Setlog 登顶日韩榜单，靠 2 秒视频干翻精致朋友圈", "url": "https://mp.weixin.qq.com/s/setlog-review", "content": "创始人专访披露团队仅 2 人，靠高中生课间合拍裂变，日活达到 750 万。"},
                    {"title": "Setlog founder interview on ZVC", "url": "https://zvc.vc/posts/setlog-story", "content": "Setlog grew organically across high schools in Japan and Korea."},
                ]}))
            if "Clubroom" in q:
                return http.HttpResponse(url, 200, "application/json", json.dumps({"results": [
                    {"title": "Clubroom opens beta waitlist for dinner-table voice clubs", "url": "https://substack.com/home/post/clubroom-beta", "content": "Clubroom startup raises $4.2M seed to build cozy evening voice tables."},
                ]}))
            if "Whee" in q:
                return http.HttpResponse(url, 200, "application/json", json.dumps({"results": [
                    {"title": "TikTok 团队悄悄在海外测试密友相机 Whee", "url": "https://36kr.com/p/whee-test", "content": "字节跳动在十余个国家测试密友桌面相机 Whee，主打只给熟人看的生活照。"},
                ]}))
            return http.HttpResponse(url, 200, "application/json", '{"results":[]}')

    def build_dossier(name: str, origin: str, tags: list[str], hook: str):
        return json.dumps({
            "admitted": True,
            "origin_type": origin,
            "badge_text": f"{name} 亮点标签",
            "platform_label": "内测排队中" if name == "Clubroom" else "iOS 已上架",
            "mech_tags": tags,
            "hook": hook,
            "cross_check": {
                "verified_points": [f"{name} 商店排名与外部专访文章交叉印证一致"],
                "divergences": [f"{name} 好评夸上手快，但 1 星评价反馈新版偶发通知延迟"],
            },
            "deep_sections": [
                {"title": title, "body": f"{name} 在【{title}】维度的大白话拆解：点开直接跟朋友互动，没有任何黑话。"}
                for title in SECTION_TITLES
            ],
        }, ensure_ascii=False)

    class MultiProductLLM(llm.LLMClient):
        def __init__(self):
            self.prompts: dict[str, str] = {}

        def complete_json(self, system_prompt: str, user_prompt: str, timeout_s: float = 30.0) -> llm.LLMResult:
            if "PureVPN Tool" in user_prompt:
                return llm.LLMResult(ok=True, content='{"admitted": false, "reject_reason": "纯网络工具无社交关系链"}')
            for prod_name, origin, tags, hook in [
                ("Wabi", "indie", ["多人共创", "熟人/密友"], "不用写代码，群里一句话搓个跟死党互相打卡整活的专属小玩具"),
                ("Setlog", "indie", ["熟人/密友"], "砍掉修图和长视频，每天只拍 2 秒真实瞬间拼成死党合拍日历"),
                ("Clubroom", "indie", ["陌生人/破冰", "兴趣/品味社区"], "每晚八点按共同书单凑一桌 6 人语音小饭局，聊满 40 分钟散场"),
                ("Whee", "bigtech", ["熟人/密友"], "大厂下场做的桌面密友相机小组件，拍完直接霸占闺蜜手机锁屏"),
            ]:
                if f"- 名称：{prod_name}\n" in user_prompt:
                    self.prompts[prod_name] = user_prompt
                    return llm.LLMResult(ok=True, content=build_dossier(prod_name, origin, tags, hook))
            return llm.LLMResult(ok=False, error="unexpected_product")

    media_bytes = {
        "https://img.test/wabi-icon.png": PNG_ICON_BYTES + b"1",
        "https://img.test/setlog-icon.png": PNG_ICON_BYTES + b"2",
        "https://img.test/whee-icon.png": PNG_ICON_BYTES + b"3",
        "https://img.test/wabi-og.jpg": JPEG_ARTICLE_PHOTO_A,
        "https://img.test/sspai-wabi-real.jpg": JPEG_ARTICLE_PHOTO_B,
        "https://img.test/wx-setlog-real.jpg": JPEG_ARTICLE_PHOTO_A + b"wx1",
        "https://img.test/wx-setlog-feed.jpg": JPEG_ARTICLE_PHOTO_B + b"wx2",
        "https://img.test/wabi-store-1.webp": WEBP_STORE_SHOT + b"w1",
        "https://img.test/setlog-store-1.webp": WEBP_STORE_SHOT + b"s1",
        "https://img.test/whee-store-1.webp": WEBP_STORE_SHOT + b"wh1",
    }
    mirror = r2.MediaMirror(
        conn,
        cfg,
        r2_cfg=r2.R2Config(bucket="vc-radar-media", public_base="/media", account_id="acc", api_token="tok"),
        fetcher=lambda u, ref="": media_bytes.get(u),
        uploader=lambda k, raw, ct: True,
    )
    fake_llm = MultiProductLLM()
    search_client = search.SearchClient(FakeSearchTransport(), cfg, api_key="sk-test-search")

    summary = product_pipeline.sync_social_products(
        conn,
        cfg,
        candidates_override=candidates,
        llm_client=fake_llm,
        search_client=search_client,
        media_mirror=mirror,
        http_text=fake_http_text,
        now=NOW,
    )
    assert summary == {
        "discovered": 5,
        "processed": 5,
        "admitted_new": 4,
        "rejected_new": 1,
        "failed": 0,
        "total_admitted": 4,
    }

    # 验证每个准入产品的 Prompt 都注入了搜索抓到的外部热帖与 App Store 好差评（绝非只针对单一产品）
    assert "实测 Wabi" in fake_llm.prompts["Wabi"] and "App Store 真实用户好评与差评样本" in fake_llm.prompts["Wabi"]
    assert "2个人做出的 Setlog" in fake_llm.prompts["Setlog"] and "ZVC" in fake_llm.prompts["Setlog"]
    assert "apkpure.com" not in fake_llm.prompts["Wabi"]

    # 构建静态站点并校验产物与文案红线
    site_dir = site.build_site(conn, cfg, now=NOW, include_local_media=True)
    exported = json.loads((site_dir / "data.json").read_text(encoding="utf-8"))
    assert_clean_wording("\n".join(copy_strings(exported)))
    prods = {p["id"]: p for p in exported["products"]}
    assert set(prods) == {"wabi", "setlog", "clubroom", "whee"}

    # Wabi 与 Setlog 都拥有外部评测热帖配图 + App Store 截图 + 交叉比对 + 热门帖子外链
    for pid, expected_post_label in (("wabi", "少数派配图"), ("setlog", "微信公众号评测配图")):
        p = prods[pid]
        assert p["deep"][0]["title"] == "产品形态与切入点"
        assert len(p["deep"]) == 6
        assert p["cross_check"]["verified_points"] and p["cross_check"]["divergences"]
        assert any(s["label"] == expected_post_label and s["url"].startswith("./media/") for s in p["screenshots"])
        assert any(s["label"] == "App Store 截图" and s["url"].startswith("./media/") for s in p["screenshots"])
        assert len(p["ref_links"]) >= 1
        for shot in p["screenshots"]:
            assert (site_dir / shot["url"].removeprefix("./")).is_file()

    assert prods["clubroom"]["live_status"] == "waitlist"
    assert prods["whee"]["origin"] == "bigtech"
    conn.close()


def test_radar_ui_filters_and_scroll_lock_in_node_vm(tmp_path):
    """无需外部浏览器的常驻单测：验证 Tab 文案、联动子筛选与浮层滚轮/触控防穿透逻辑。"""
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    conn = db.connect(cfg.db_path)
    db.ensure_social_schema(conn)
    for idx, (pid, name, ch, live, origin, tag) in enumerate([
        ("p-vc-live", "Wabi", "vc", "ios", "indie", "多人共创"),
        ("p-vc-wait", "Clubroom", "vc", "waitlist", "indie", "陌生人/破冰"),
        ("p-chart-indie", "Setlog", "chart", "ios", "indie", "熟人/密友"),
        ("p-chart-big", "Whee", "chart", "ios", "bigtech", "熟人/密友"),
    ]):
        conn.execute(
            """INSERT INTO social_product(product_id, name, channel, live_status, origin_type, company_name,
               hook, mech_tags_json, deep_sections_json, cross_check_json, priority_score, admitted, enriched_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '2026-11-07T12:00:00+08:00', '2026-11-07T12:00:00+08:00')""",
            (
                pid,
                name,
                ch,
                live,
                origin,
                f"{name} Team",
                f"{name} 一句话玩法钩子",
                json.dumps([tag], ensure_ascii=False),
                json.dumps([{"title": t, "body": f"{name} 拆解正文"} for t in SECTION_TITLES], ensure_ascii=False),
                json.dumps({"verified_points": ["交叉印证通过"], "divergences": ["真实差评槽点"]}, ensure_ascii=False),
                100 - idx,
            ),
        )
    conn.commit()
    site_dir = site.build_site(conn, cfg, now=NOW)
    conn.close()

    node_bin = shutil.which("node")
    assert node_bin is not None
    script = r"""
      const fs = require("fs"), path = require("path"), vm = require("vm");
      const root = process.argv[1];
      const ui = require(path.join(root, "app.js"));
      const d = JSON.parse(fs.readFileSync(path.join(root, "data.json"), "utf8"));

      // 1. 校验筛选组合：全部（默认不含内测）、早期融资（已上线 vs 含内测）、冲榜新品（独立新锐 vs 含大厂）
      const allIds = ui.selectProducts(d, ui.defaultRadarState()).map(p => p.id);
      const vcLiveIds = ui.selectProducts(d, {...ui.defaultRadarState(), view:"vc", vcLiveOnly:true}).map(p => p.id);
      const vcAllIds = ui.selectProducts(d, {...ui.defaultRadarState(), view:"vc", vcLiveOnly:false}).map(p => p.id);
      const chartIndieIds = ui.selectProducts(d, {...ui.defaultRadarState(), view:"chart", chartIndieOnly:true}).map(p => p.id);
      const chartAllIds = ui.selectProducts(d, {...ui.defaultRadarState(), view:"chart", chartIndieOnly:false}).map(p => p.id);
      const htmlAll = ui.radarHtml(d, ui.defaultRadarState());
      const modalHtml = ui.productModalHtml(d.products[0]);

      // 2. 在 VM 中挂载真实事件监听，验证打开详情锁死 body + 拦截蒙层滚轮穿透 + 放行弹窗内部滚动 + 关闭还原 scrollY
      const listeners = {};
      let scrollYVal = 240;
      const nodes = {
        main: {innerHTML: "", classList: {toggle(){}}},
        "sr-overlay": {innerHTML: ""}
      };
      const doc = {
        documentElement: {clientWidth: 1265, setAttribute(){}, removeAttribute(){}, getAttribute(){ return "radar"; }},
        body: {style: {}},
        activeElement: null,
        getElementById: id => nodes[id] || (id === "sr-detail-scroll" ? {scrollTop: 99} : null),
        querySelector: () => ({focus(){}}),
        querySelectorAll: () => [],
        addEventListener: (ev, fn) => { listeners[ev] = fn; }
      };
      const ctx = {
        URL,
        history: {replaceState(){}},
        location: {hash: ""},
        document: doc,
        window: {
          innerWidth: 1280,
          get scrollY() { return scrollYVal; },
          scrollTo: (_x, y) => { scrollYVal = y; },
          addEventListener: () => {}
        },
        localStorage: {getItem: () => null, setItem: () => {}},
        matchMedia: () => ({matches: false}),
        fetch: async () => ({ok: true, json: async () => d})
      };
      vm.runInNewContext(fs.readFileSync(path.join(root, "app.js"), "utf8"), ctx);
      (async () => {
        await new Promise(r => setImmediate(r));
        await new Promise(r => setImmediate(r));
        // 点击打开卡片
        listeners.click({
          target: {closest: sel => sel === "[data-open-product]" ? {dataset: {openProduct: "p-vc-live"}} : null},
          preventDefault(){}
        });
        const lockedPos = doc.body.style.position;
        const lockedTop = doc.body.style.top;
        const padRight = doc.body.style.paddingRight;

        let backdropPrevented = false, innerPrevented = false;
        listeners.wheel({
          target: {closest: () => null},
          preventDefault() { backdropPrevented = true; }
        });
        listeners.wheel({
          target: {closest: sel => sel === "#sr-detail-scroll" ? {} : null},
          preventDefault() { innerPrevented = true; }
        });

        scrollYVal = 0; // 模拟 fixed 期间 window.scrollY 归零
        listeners.keydown({key: "Escape", preventDefault(){}});
        const restoredY = scrollYVal;
        const unlockedPos = doc.body.style.position;

        console.log(JSON.stringify({
          allIds, vcLiveIds, vcAllIds, chartIndieIds, chartAllIds,
          hasExactTabs: htmlAll.includes(">全部<") && htmlAll.includes(">早期融资<") && htmlAll.includes(">冲榜新品<") && !htmlAll.includes("可玩"),
          hasFirstAngle: modalHtml.includes("产品形态与切入点") && modalHtml.includes("多方交叉比对"),
          lockedPos, lockedTop, padRight, backdropPrevented, innerPrevented, restoredY, unlockedPos
        }));
      })();
    """
    proc = subprocess.run([node_bin, "-e", script, str(site_dir)], text=True, capture_output=True, check=True)
    res = json.loads(proc.stdout)
    assert res["allIds"] == ["p-vc-live", "p-chart-indie", "p-chart-big"]
    assert res["vcLiveIds"] == ["p-vc-live"]
    assert res["vcAllIds"] == ["p-vc-live", "p-vc-wait"]
    assert res["chartIndieIds"] == ["p-chart-indie"]
    assert res["chartAllIds"] == ["p-chart-indie", "p-chart-big"]
    assert res["hasExactTabs"] is True
    assert res["hasFirstAngle"] is True
    assert res["lockedPos"] == "fixed" and res["lockedTop"] == "-240px" and res["padRight"] == "15px"
    assert res["backdropPrevented"] is True and res["innerPrevented"] is False
    assert res["restoredY"] == 240 and res["unlockedPos"] == ""


def test_dual_channel_has_vc_and_full_chinese_ui_rendering(tmp_path):
    """验证冲榜产品带融资实锤时同步出现在「早期融资」与「冲榜新品」双视图，且前端界面无残留外文标签。"""
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    conn = db.connect(cfg.db_path)
    db.ensure_social_schema(conn)
    conn.execute(
        """INSERT INTO social_product(
           product_id, name, channel, live_status, origin_type, company_name,
           app_store_url, platform_label, hook, mech_tags_json, deep_sections_json, cross_check_json,
           screenshots_json, ref_links_json, funding_text, investors_text, release_status_text, source_note,
           priority_score, admitted, enriched_at, updated_at
        ) VALUES (?, ?, 'chart', 'ios', 'indie', ?, 'https://apps.apple.com/jp/app/id6500000001',
                  'iOS 已上架', '两秒视频拼成密友合拍日历', '["熟人/密友"]', ?, ?, ?, ?,
                  '种子轮 · 金额未公开', 'ZVC、DG 大和创投参投', 'iOS 已上架（2025-07 首发）', '日本社交榜 #2',
                  130, 1, '2026-11-07T12:00:00+08:00', '2026-11-07T12:00:00+08:00')""",
        (
            "setlog",
            "Setlog",
            "New Chat 团队",
            json.dumps([{"title": t, "body": "全中文大白话拆解正文。"} for t in SECTION_TITLES], ensure_ascii=False),
            json.dumps({"verified_points": ["获 ZVC 与 DG 大和创投种子轮投资"], "divergences": ["金额未公开"]}, ensure_ascii=False),
            json.dumps([{"url": "https://img.example.com/s1.jpg", "label": "App Store 截图"}], ensure_ascii=False),
            json.dumps([{"title": "Setlog 创始人专访：两人团队登顶日韩总榜", "source": "ZVC 专访", "url": "https://zvc.vc/posts/setlog"}], ensure_ascii=False),
        ),
    )
    conn.commit()
    site_dir = site.build_site(conn, cfg, now=NOW)
    conn.close()

    node_bin = shutil.which("node")
    assert node_bin is not None
    script = r"""
      const fs = require("fs"), path = require("path");
      const root = process.argv[1];
      const ui = require(path.join(root, "app.js"));
      const d = JSON.parse(fs.readFileSync(path.join(root, "data.json"), "utf8"));
      const vcIds = ui.selectProducts(d, {...ui.defaultRadarState(), view:"vc"}).map(p => p.id);
      const chartIds = ui.selectProducts(d, {...ui.defaultRadarState(), view:"chart"}).map(p => p.id);
      const radar = ui.radarHtml(d, ui.defaultRadarState());
      const modal = ui.productModalHtml(d.products[0]);
      console.log(JSON.stringify({ p: d.products[0], vcIds, chartIds, radar, modal }));
    """
    proc = subprocess.run([node_bin, "-e", script, str(site_dir)], text=True, capture_output=True, check=True)
    res = json.loads(proc.stdout)
    assert res["p"]["has_vc"] is True
    assert res["vcIds"] == ["setlog"]
    assert res["chartIds"] == ["setlog"]
    assert "社交产品雷达" in res["radar"] and "Social Radar" not in res["radar"]
    assert "苹果端已上架" in res["radar"] and "iOS 已上架" not in res["radar"]
    assert "融资关联" in res["radar"] and "种子轮 · 金额未公开" in res["radar"]
    assert "苹果商店" in res["modal"] and "商店界面截图" in res["modal"]
    assert ">App Store<" not in res["modal"] and "App Store 截图" not in res["modal"]


class _CheckAttrParser(HTMLParser):
    def __init__(self, html_str: str):
        super().__init__()
        self.payload = None
        self.feed(html_str)

    def handle_starttag(self, tag, attrs):
        if tag == "html":
            raw = dict(attrs).get("data-radar-check")
            if raw:
                self.payload = json.loads(raw)


def _find_chrome() -> Path | None:
    for cand in (
        os.environ.get("VCRADAR_CHROME_BIN"),
        "/opt/ungoogled-chromium/chrome",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ):
        if cand and Path(cand).exists():
            return Path(cand)
    return None


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 900)])
def test_headless_chrome_radar_layout_media_and_modal_scroll_non_penetration(tmp_path, width, height):
    if os.environ.get("VCRADAR_BROWSER_TESTS") != "1":
        pytest.skip("浏览器 E2E 默认关闭；设 VCRADAR_BROWSER_TESTS=1 开启")
    browser = _find_chrome()
    if browser is None:
        pytest.skip("未找到 Chrome/Chromium")

    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    conn = db.connect(cfg.db_path)
    db.ensure_social_schema(conn)
    mirror = r2.MediaMirror(
        conn,
        cfg,
        r2_cfg=r2.R2Config(bucket="vc-radar-media", public_base="/media"),
        fetcher=lambda u, ref="": PNG_ICON_BYTES if "icon" in u else PNG_SHOT_BYTES,
        uploader=lambda k, raw, ct: False,
    )
    icon_path = mirror.mirror_url("https://img.test/icon.png", min_bytes=500, now=NOW)
    shot_path = mirror.mirror_url("https://img.test/shot.png", min_bytes=10_000, now=NOW)

    # 写入足够多的卡片使页面产生真实纵向滚动条，以实测浮层打开前后底层滚动位置绝不穿透
    for i in range(14):
        conn.execute(
            """INSERT INTO social_product(product_id, name, channel, live_status, origin_type, company_name,
               icon_url, badge_text, badge_kind, platform_label, hook, mech_tags_json,
               deep_sections_json, cross_check_json, screenshots_json, ref_links_json,
               funding_text, investors_text, release_status_text, source_note, priority_score, admitted, enriched_at, updated_at)
               VALUES (?, ?, ?, 'ios', 'indie', ?, ?, '日本总榜 #1', 'hot', 'iOS 已上架', ?, '["熟人/密友"]',
                       ?, ?, ?, ?, '种子轮 · 500 万美元', 'a16z 领投', 'iOS 已上架', '日本社交榜 #1', ?, 1,
                       '2026-11-07T12:00:00+08:00', '2026-11-07T12:00:00+08:00')""",
            (
                f"prod-{i:02d}",
                f"SocialApp {i:02d}",
                "vc" if i % 2 == 0 else "chart",
                f"Studio {i:02d}",
                icon_path,
                "砍掉点赞和滤镜，每天跟死党随手拍两秒生活碎片拼成共同日历。",
                json.dumps([{"title": t, "body": ("真实母语大白话深度拆解段落内容，确保浮层内部也拥有充足的纵向滚动高度。" * 4)} for t in SECTION_TITLES], ensure_ascii=False),
                json.dumps({"verified_points": ["多国榜单排名与创始人专访日活数据一致"], "divergences": ["部分 1 星评论反映通知偶发延迟"]}, ensure_ascii=False),
                json.dumps([{"url": shot_path, "label": "微信公众号评测配图"}], ensure_ascii=False),
                json.dumps([{"title": "深度实测评测帖", "source": "少数派", "url": "https://sspai.com/post/100"}], ensure_ascii=False),
                200 - i,
            ),
        )
    conn.commit()
    site_dir = site.build_site(conn, cfg, now=NOW, include_local_media=True)
    conn.close()

    host = site_dir / f"_radar_vp_{width}.html"
    host.write_text(
        f"""<!doctype html><html><body style="margin:0;padding:0;overflow:hidden">
        <iframe id="vp" src="./index.html" style="width:{width}px;height:{height}px;border:0;display:block"></iframe>
        <script>
        (async () => {{
          const frame = document.getElementById("vp");
          for (let i = 0; i < 200 && (!frame.contentDocument || !frame.contentDocument.querySelector(".sr-card")); i++) {{
            await new Promise(r => setTimeout(r, 15));
          }}
          const win = frame.contentWindow, doc = frame.contentDocument;
          const errors = [];
          if (doc.documentElement.scrollWidth > win.innerWidth) {{
            errors.push("列表横向溢出:" + doc.documentElement.scrollWidth + ">" + win.innerWidth);
          }}
          doc.documentElement.style.scrollBehavior = "auto";
          win.scrollTo(0, 180);
          doc.documentElement.style.scrollBehavior = "";
          await new Promise(r => setTimeout(r, 30));
          const beforeY = Math.round(win.scrollY);
          if (beforeY < 80) errors.push("页面未能滚动到测试基线位置:" + beforeY);

          // 点击第一张卡片，0ms 同步弹出详情浮层
          doc.querySelector("[data-open-product]").click();
          await new Promise(r => setTimeout(r, 25));
          const modal = doc.getElementById("sr-modal");
          const detailScroll = doc.getElementById("sr-detail-scroll");
          const backdrop = doc.getElementById("sr-backdrop");
          if (!modal || !detailScroll) errors.push("详情浮层未打开");
          if (doc.body.style.position !== "fixed") errors.push("打开浮层后 body 未固定锁死:" + doc.body.style.position);
          if (doc.documentElement.scrollWidth > win.innerWidth) {{
            errors.push("浮层横向溢出:" + doc.documentElement.scrollWidth + ">" + win.innerWidth);
          }}

          // 在蒙层上派发可取消滚轮事件，必须被拦截；在内部滚动容器滚动，底层 fixed 偏移绝不变动
          const wheelEv = new win.WheelEvent("wheel", {{deltaY: 300, bubbles: true, cancelable: true}});
          backdrop.dispatchEvent(wheelEv);
          if (!wheelEv.defaultPrevented) errors.push("蒙层滚轮事件未被拦截，存在滚动穿透风险");

          const bodyTopBefore = doc.body.style.top;
          detailScroll.scrollTop = 220;
          await new Promise(r => setTimeout(r, 20));
          if (detailScroll.scrollTop <= 0) errors.push("浮层内部详情区无法正常纵向滚动");
          if (doc.body.style.top !== bodyTopBefore) errors.push("内部滚动导致底层锁定位置漂移");

          // 验证图片从本地镜像 ./media/ 真实解码成功
          const imgsOk = [...modal.querySelectorAll("img")].every(img => img.complete && img.naturalWidth > 0);
          if (!imgsOk) errors.push("详情图片未加载成功");

          // 按 Escape 关闭浮层，底层滚动条精确回到打开前位置
          doc.dispatchEvent(new win.KeyboardEvent("keydown", {{key: "Escape", bubbles: true}}));
          await new Promise(r => setTimeout(r, 25));
          const afterY = Math.round(win.scrollY);
          if (doc.getElementById("sr-modal")) errors.push("Escape 未能关闭浮层");
          if (Math.abs(afterY - beforeY) > 2) errors.push("关闭浮层后底层滚动位置未还原:" + beforeY + "->" + afterY);

          document.documentElement.setAttribute("data-radar-check", JSON.stringify({{
            width: win.innerWidth,
            beforeY,
            afterY,
            errors,
            tabsText: doc.querySelector(".sr-tabs")?.textContent || ""
          }}));
        }})().catch(e => document.documentElement.setAttribute("data-radar-check", JSON.stringify({{error: String(e)}})));
        </script></body></html>""",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [
                str(browser),
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--no-first-run",
                "--disable-background-networking",
                "--host-resolver-rules=MAP * ~NOTFOUND",
                "--allow-file-access-from-files",
                "--hide-scrollbars",
                f"--window-size={max(width, 520)},{max(height, 920)}",
                "--force-device-scale-factor=1",
                "--virtual-time-budget=3500",
                f"--user-data-dir={tmp_path / f'chrome-prof-{width}'}",
                "--dump-dom",
                host.as_uri(),
            ],
            text=True,
            capture_output=True,
            timeout=25,
        )
    finally:
        host.unlink(missing_ok=True)

    if proc.returncode and "Operation not permitted" in proc.stderr:
        pytest.skip("沙箱限制 Chromium 启动")
    assert proc.returncode == 0, proc.stderr[-1000:]
    parsed = _CheckAttrParser(proc.stdout).payload
    assert parsed is not None and "error" not in parsed, parsed
    assert parsed["errors"] == [], parsed
    assert "全部" in parsed["tabsText"] and "早期融资" in parsed["tabsText"] and "冲榜新品" in parsed["tabsText"]
    assert "可玩" not in parsed["tabsText"]
