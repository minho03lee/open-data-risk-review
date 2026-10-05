"""플랫폼 커넥터 공통부."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape

import httpx

from ..models import DataCard, DatasetRef, Platform, Source

USER_AGENT = "open-data-risk-review/0.1 (internal dataset risk review)"
MAX_SNAPSHOT_CHARS = 200_000


@dataclass
class CollectContext:
    client: httpx.AsyncClient
    kaggle_auth: tuple[str, str] | None = None
    extra: dict = field(default_factory=dict)


async def fetch_text(ctx: CollectContext, url: str, **kw) -> tuple[Source, httpx.Response]:
    r = await ctx.client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, **kw)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "text/plain").split(";")[0]
    return Source(url=str(r.url), content_type=ctype, content=r.text[:MAX_SNAPSHOT_CHARS]), r


_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)


def html_to_text(html: str) -> str:
    text = _DROP.sub(" ", html)
    text = re.sub(r"<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.I)
    text = unescape(_TAG.sub(" ", text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def html_meta(html: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        meta["title"] = unescape(m.group(1)).strip()
    for m in re.finditer(r"<meta\s+[^>]*>", html, re.I):
        tag = m.group(0)
        key = re.search(r'(?:name|property)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        val = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if key and val:
            meta[key.group(1).lower()] = unescape(val.group(1)).strip()
    return meta


async def collect(ctx: CollectContext, ref: DatasetRef) -> DataCard:
    """플랫폼 커넥터로 메타데이터를 모아 1차 카드를 만든다 (LLM 보강 전)."""
    from . import aihub, huggingface, kaggle, web

    connector = {
        Platform.huggingface: huggingface.collect,
        Platform.kaggle: kaggle.collect,
        Platform.aihub: aihub.collect,
        Platform.web: web.collect,
    }[ref.platform]
    return await connector(ctx, ref)
