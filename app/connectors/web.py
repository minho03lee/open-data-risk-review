"""제공자 자체 사이트용 일반 웹페이지 수집기. 항목 추출은 LLM 보강 단계에서 한다."""
from __future__ import annotations

import re
from html import unescape
from urllib.parse import urldefrag, urljoin, urlparse

from ..models import CardField, DataCard, DatasetRef, FieldStatus
from .base import CollectContext, fetch_text, html_meta, html_to_text

MAX_EXTRA_PAGES = 5
# 소개·다운로드·약관·라이선스 같은 하위 페이지에 수집 방법과 이용 조건이 적혀 있는 경우가 많다.
_WANT = re.compile(r"about|download|terms|licen[cs]e|copyright|faq|overview|dataset|data|readme|paper|policy|privacy|"
                   r"소개|다운로드|약관|이용|라이선스|저작권|개인정보", re.I)
_SKIP_EXT = re.compile(r"\.(pdf|zip|gz|tar|tgz|png|jpe?g|gif|svg|ico|css|js|mp[34]|mov|csv|json|xml)(\?|$)", re.I)
_LINK = re.compile(r"<a\s[^>]*?href\s*=\s*[\"']([^\"'#][^\"']*)[\"'][^>]*>(.*?)</a>", re.I | re.S)


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def related_links(html: str, base_url: str) -> list[str]:
    """같은 사이트 안에서 소개·다운로드·약관 등으로 보이는 링크 (최대 MAX_EXTRA_PAGES개)."""
    out: list[str] = []
    for href, label in _LINK.findall(html):
        url = urldefrag(urljoin(base_url, unescape(href.strip())))[0]
        if not url.startswith(("http://", "https://")) or _host(url) != _host(base_url) or _SKIP_EXT.search(url):
            continue
        text = re.sub(r"<[^>]+>", " ", label)
        if url.rstrip("/") != base_url.rstrip("/") and url not in out and _WANT.search(urlparse(url).path + " " + text):
            out.append(url)
    return out[:MAX_EXTRA_PAGES]


async def collect(ctx: CollectContext, ref: DatasetRef) -> DataCard:
    src, r = await fetch_text(ctx, ref.url or f"https://{ref.repo}")
    is_html = "html" in src.content_type
    text = html_to_text(r.text) if is_html else r.text
    src = src.model_copy(update={"content": text[:200_000], "content_type": "text/plain"})
    card = DataCard(dataset=ref, sources=[src])

    meta = html_meta(r.text) if is_html else {}
    title = meta.get("og:title") or meta.get("title")
    if title:
        card.set("name", CardField(value={"official": title, "aliases": []}, evidence_url=src.url, status=FieldStatus.inferred))
    desc = meta.get("og:description") or meta.get("description")
    if desc:
        card.set("overview", CardField(value=desc, evidence_url=src.url, status=FieldStatus.inferred))
    card.set("source_url", CardField.confirmed(src.url, src.url))
    if is_html:
        for link in related_links(r.text, src.url):
            try:
                sub, sub_r = await fetch_text(ctx, link)
            except Exception:  # 하위 페이지를 못 읽어도 첫 페이지로 카드는 만든다
                continue
            body = html_to_text(sub_r.text) if "html" in sub.content_type else sub_r.text
            card.sources.append(sub.model_copy(update={"content": body[:100_000], "content_type": "text/plain"}))
    return card
