"""AI허브 커넥터. 로그인·이용 신청 뒤 제공되므로 공개 소개 페이지와 약관을 기준으로 한다."""
from __future__ import annotations

from ..models import CardField, DataCard, DatasetRef
from .base import CollectContext, fetch_text, html_meta, html_to_text

TERMS_URL = "https://www.aihub.or.kr/intrcn/guid/usagepolicy.do"


async def collect(ctx: CollectContext, ref: DatasetRef) -> DataCard:
    page = ref.url or f"https://www.aihub.or.kr/aihubdata/data/view.do?dataSetSn={ref.repo}"
    src, r = await fetch_text(ctx, page)
    text = html_to_text(r.text)
    src = src.model_copy(update={"content": text, "content_type": "text/plain"})
    card = DataCard(dataset=ref.model_copy(update={"url": page}), sources=[src])

    meta = html_meta(r.text)
    title = meta.get("og:title") or meta.get("title")
    if title:
        card.set("name", CardField.confirmed({"official": title.replace(" - AI-Hub", "").strip(), "aliases": []}, page))
    card.set("source_url", CardField.confirmed(page, page))
    card.set("access", CardField.confirmed({"type": "신청제", "note": "AI허브 로그인 후 이용 신청·승인 필요"}, page))
    card.set("provider", CardField.confirmed({"platform": "AI허브(한국지능정보사회진흥원 운영)", "note": "구축 기관은 소개 페이지에서 확인"}, page))
    card.set("extra_terms", CardField.confirmed({"terms_url": TERMS_URL, "note": "AI허브 이용약관·데이터별 이용 조건 적용"}, TERMS_URL))
    card.needs_documents.append("AI허브 데이터는 신청 뒤 받는 이용 조건 문서가 있으면 올려 주세요.")
    return card
