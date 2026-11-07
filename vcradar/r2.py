"""Cloudflare R2 内容寻址图片镜像模块（纯 Python 标准库实现）。

下载产品图标、商店截图与评测热帖配图，校验真实图片魔数与最小字节数，
按 SHA-256 去重上传至 Cloudflare R2 存储桶（默认 vc-radar-media），
统一生成同源静态路径 `/media/<sha256>.<ext>`，绝不打印或记录任何密钥。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import sqlite3
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .config import BJ, Config, UA, load_env_var

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """根据文件头魔数识别安全的位图格式，返回 (ext, content_type)；非图片返回 None。"""
    if len(data) < 16:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif", "image/avif"
    return None


def _s3_sigv4_headers(
    *,
    method: str,
    account_id: str,
    bucket: str,
    key: str,
    access_key: str,
    secret_key: str,
    content_type: str,
    payload: bytes,
    now_utc: datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """构造 Cloudflare R2 S3 兼容接口的 AWS Signature V4 请求头。"""
    stamp = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = stamp.strftime("%Y%m%dT%H%M%SZ")
    datestamp = stamp.strftime("%Y%m%d")
    host = f"{account_id}.r2.cloudflarestorage.com"
    canonical_uri = f"/{bucket}/{key}"
    payload_hash = hashlib.sha256(payload).hexdigest()
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = f"{datestamp}/auto/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    def _sign(k: bytes, msg: str) -> bytes:
        return hmac.new(k, msg.encode("utf-8"), hashlib.sha256).digest()

    signing_key = _sign(
        _sign(_sign(_sign(("AWS4" + secret_key).encode("utf-8"), datestamp), "auto"), "s3"),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return f"https://{host}{canonical_uri}", {
        "Content-Type": content_type,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "Authorization": auth,
    }


@dataclass
class R2Config:
    bucket: str = "vc-radar-media"
    public_base: str = "/media"
    account_id: str | None = None
    api_token: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    @classmethod
    def from_config(cls, cfg: Config) -> R2Config:
        account_id = load_env_var(cfg, "CLOUDFLARE_ACCOUNT_ID") or load_env_var(cfg, "R2_ACCOUNT_ID")
        return cls(
            bucket=cfg.r2_bucket or "vc-radar-media",
            public_base=cfg.r2_public_base or "/media",
            account_id=account_id,
            api_token=load_env_var(cfg, "CLOUDFLARE_API_TOKEN"),
            s3_access_key=load_env_var(cfg, "R2_ACCESS_KEY_ID"),
            s3_secret_key=load_env_var(cfg, "R2_SECRET_ACCESS_KEY"),
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.bucket
            and self.account_id
            and (self.api_token or (self.s3_access_key and self.s3_secret_key))
        )


class MediaMirror:
    """带 SQLite 去重缓存与本机缓存目录的 R2 媒体镜像器。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        *,
        r2_cfg: R2Config | None = None,
        fetcher=None,
        uploader=None,
    ):
        self.conn = conn
        self.cfg = cfg
        self.r2 = r2_cfg or R2Config.from_config(cfg)
        self._fetcher = fetcher or self._default_fetch
        self._uploader = uploader or self._default_upload
        self.local_dir: Path = cfg.state_dir / ("test/media" if cfg.test_mode else "media")

    def _public_path(self, r2_key: str) -> str:
        base = (self.r2.public_base or "/media").rstrip("/")
        return f"{base}/{r2_key}"

    def _default_fetch(self, url: str, referer: str = "", timeout_s: float = 15.0) -> bytes | None:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        headers = {
            "User-Agent": UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        req = Request(url, headers=headers, method="GET")
        try:
            with urlopen(req, timeout=timeout_s) as resp:
                if resp.status != 200:
                    return None
                chunks = []
                total = 0
                while True:
                    block = resp.read(65536)
                    if not block:
                        break
                    total += len(block)
                    if total > MAX_IMAGE_BYTES:
                        return None
                    chunks.append(block)
                return b"".join(chunks)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            return None

    def _default_upload(self, r2_key: str, data: bytes, content_type: str, timeout_s: float = 20.0) -> bool:
        if not self.r2.enabled:
            return False
        if self.r2.api_token and self.r2.account_id:
            api_url = (
                f"https://api.cloudflare.com/client/v4/accounts/{self.r2.account_id}"
                f"/r2/buckets/{self.r2.bucket}/objects/{r2_key}"
            )
            req = Request(
                api_url,
                data=data,
                headers={
                    "Authorization": f"Bearer {self.r2.api_token}",
                    "Content-Type": content_type,
                },
                method="PUT",
            )
            try:
                with urlopen(req, timeout=timeout_s) as resp:
                    if 200 <= resp.status < 300:
                        return True
            except (HTTPError, URLError, TimeoutError, OSError):
                pass
        if self.r2.s3_access_key and self.r2.s3_secret_key and self.r2.account_id:
            s3_url, headers = _s3_sigv4_headers(
                method="PUT",
                account_id=self.r2.account_id,
                bucket=self.r2.bucket,
                key=r2_key,
                access_key=self.r2.s3_access_key,
                secret_key=self.r2.s3_secret_key,
                content_type=content_type,
                payload=data,
            )
            req = Request(s3_url, data=data, headers=headers, method="PUT")
            try:
                with urlopen(req, timeout=timeout_s) as resp:
                    return 200 <= resp.status < 300
            except (HTTPError, URLError, TimeoutError, OSError):
                return False
        return False

    def mirror_url(
        self,
        url: str,
        *,
        min_bytes: int = 1200,
        referer: str = "",
        now: datetime | None = None,
    ) -> str | None:
        """下载并镜像一张图片；成功返回 `/media/<sha256>.<ext>`，不合格或失败返回 None。"""
        clean_url = str(url or "").strip()
        if not clean_url:
            return None
        cached = self.conn.execute(
            "SELECT r2_key, public_path, byte_size, content_type, uploaded_r2 FROM media_asset WHERE src_url=?",
            (clean_url,),
        ).fetchone()
        if cached and cached["byte_size"] >= min_bytes:
            if cached["uploaded_r2"] or not self.r2.enabled:
                return cached["public_path"]
            local_file = self.local_dir / cached["r2_key"]
            if local_file.is_file():
                if self._uploader(cached["r2_key"], local_file.read_bytes(), cached["content_type"]):
                    with self.conn:
                        self.conn.execute(
                            "UPDATE media_asset SET uploaded_r2=1 WHERE r2_key=?",
                            (cached["r2_key"],),
                        )
                    return cached["public_path"]

        raw = self._fetcher(clean_url, referer)
        if not raw or len(raw) < min_bytes:
            return None
        sniffed = sniff_image(raw)
        if sniffed is None:
            return None
        ext, content_type = sniffed
        digest = hashlib.sha256(raw).hexdigest()
        r2_key = f"{digest}.{ext}"
        public_path = self._public_path(r2_key)

        self.local_dir.mkdir(parents=True, exist_ok=True)
        local_target = self.local_dir / r2_key
        if not local_target.exists():
            local_target.write_bytes(raw)

        same_hash = self.conn.execute(
            "SELECT uploaded_r2 FROM media_asset WHERE sha256=? AND uploaded_r2=1 LIMIT 1",
            (digest,),
        ).fetchone()
        uploaded = bool(same_hash) or bool(self._uploader(r2_key, raw, content_type))
        stamp = (now or datetime.now(BJ)).astimezone(BJ).isoformat()
        with self.conn:
            self.conn.execute(
                """INSERT INTO media_asset(src_url, sha256, r2_key, public_path, content_type, byte_size, uploaded_r2, uploaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(src_url) DO UPDATE SET
                     sha256=excluded.sha256, r2_key=excluded.r2_key, public_path=excluded.public_path,
                     content_type=excluded.content_type, byte_size=excluded.byte_size,
                     uploaded_r2=MAX(media_asset.uploaded_r2, excluded.uploaded_r2),
                     uploaded_at=excluded.uploaded_at""",
                (clean_url, digest, r2_key, public_path, content_type, len(raw), 1 if uploaded else 0, stamp),
            )
        if self.r2.enabled and not uploaded:
            return None
        return public_path
