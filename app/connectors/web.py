"""제공자 자체 사이트용 일반 웹페이지 수집기. 항목 추출은 LLM 보강 단계에서 한다."""
from __future__ import annotations

from ..models import CardField, DataCard, DatasetRef, FieldStatus
from .base import CollectContext, fetch_text, html_meta, html_to_text


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
    return card
