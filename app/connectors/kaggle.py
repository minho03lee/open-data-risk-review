"""Kaggle 커넥터. API 키가 있으면 Kaggle API, 없으면 공개 페이지 메타데이터만 쓴다."""
from __future__ import annotations

import httpx

from ..licenses import to_spdx
from ..models import CardField, DataCard, DatasetRef, FieldStatus
from .base import CollectContext, fetch_text, html_meta

API = "https://www.kaggle.com/api/v1/datasets/view"


async def collect(ctx: CollectContext, ref: DatasetRef) -> DataCard:
    page = f"https://www.kaggle.com/datasets/{ref.repo}"
    ref = ref.model_copy(update={"url": page})
    card = DataCard(dataset=ref)

    info: dict = {}
    if ctx.kaggle_auth:
        try:
            src, r = await fetch_text(ctx, f"{API}/{ref.repo}", auth=ctx.kaggle_auth)
            card.sources.append(src)
            info = r.json()
        except httpx.HTTPError:
            info = {}

    if not info:
        src, r = await fetch_text(ctx, page)
        card.sources.append(src)
        meta = html_meta(r.text)
        title = meta.get("og:title") or meta.get("title")
        card.set("name", CardField.confirmed({"official": title, "aliases": [ref.repo]}, page))
        desc = meta.get("og:description") or meta.get("description")
        if desc:
            card.set("overview", CardField(value=desc, evidence_url=page, status=FieldStatus.inferred))
        card.set("source_url", CardField.confirmed(page, page))
        card.needs_documents.append("Kaggle API 키(KAGGLE_USERNAME/KAGGLE_KEY)가 없어 라이선스·설명을 페이지 메타데이터로만 확인했습니다.")
        return card

    version = ref.version or (str(info["currentVersionNumber"]) if info.get("currentVersionNumber") else None)
    card.dataset = ref.model_copy(update={"version": version})
    card.set("name", CardField.confirmed({"official": info.get("title"), "aliases": [ref.repo]}, page))
    card.set("version", CardField.confirmed({"version": version, "last_updated": info.get("lastUpdated")}, page))
    if info.get("subtitle"):
        card.set("overview", CardField.confirmed(info["subtitle"], page))
    if info.get("description"):
        card.set("description", CardField.confirmed({"original": info["description"][:4000]}, page))
    card.set("size_language", CardField.confirmed({"bytes": info.get("totalBytes")} if info.get("totalBytes") else None, page))

    owner = info.get("ownerName") or info.get("creatorName") or ref.repo.split("/")[0]
    # Kaggle은 재업로드가 많아 업로더를 원저작자로 단정하지 않는다
    card.set("provider", CardField(value={"uploader": owner, "profile": f"https://www.kaggle.com/{ref.repo.split('/')[0]}"}, evidence_url=page, status=FieldStatus.inferred))
    card.set("source_url", CardField.confirmed(page, page))
    card.set("access", CardField.confirmed({"type": "로그인·동의 후 공개(gated)", "note": "Kaggle 계정 로그인 후 다운로드"}, page))

    lic = info.get("licenseName")
    if lic:
        spdx = to_spdx(lic)
        card.set("license", CardField.confirmed({"raw": [lic], "spdx": [spdx] if spdx else [], "custom": [] if spdx else [lic]}, page))
        card.set("license_evidence", CardField.confirmed([page], page))
    return card
