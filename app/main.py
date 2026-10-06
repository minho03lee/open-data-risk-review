"""오픈데이터 리스크 검토 API (1단계: 식별 + 데이터 카드 생성)."""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import anthropic
import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import extract, lineage, news, others, reputation, risk, summary, watchlists
from .config import settings
from .connectors import CollectContext, collect
from .identify import identify
from .models import CARD_FIELDS, CardField, DataCard, DatasetRef, FieldStatus, IdentifyResult
from .store import make_store

app = FastAPI(title="Open Data Risk Review", version="0.1.0")
store = make_store(settings.database_url)
watch_cache = watchlists.IndexCache(watchlists.build_index)
gdelt = news.GdeltClient()
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def require_token(authorization: str | None = Header(default=None)) -> None:
    """사내 SSO 연동 전까지 쓰는 공유 토큰. APP_ACCESS_TOKEN이 없으면 검사하지 않는다."""
    if not settings.access_token:
        return
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, settings.access_token):
        raise HTTPException(status_code=401, detail="인증이 필요합니다.")


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30)


class IdentifyRequest(BaseModel):
    query: str


class CreateCardRequest(BaseModel):
    query: str | None = None
    ref: DatasetRef | None = None


class PatchCardRequest(BaseModel):
    fields: dict[str, Any]


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "store": type(store).__name__, "llm": bool(settings.anthropic_api_key)}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/card-fields", dependencies=[Depends(require_token)])
async def card_fields() -> dict:
    return {k: {"group": g, "label": label, "kind": kind} for k, (g, label, kind) in CARD_FIELDS.items()}


@app.post("/api/identify", dependencies=[Depends(require_token)])
async def api_identify(req: IdentifyRequest) -> IdentifyResult:
    async with http_client() as client:
        return await identify(client, req.query, settings.kaggle_auth)


def _api_error_text(e: anthropic.APIError) -> str:
    msg = getattr(e, "message", "") or str(e)
    if "credit balance" in msg:
        return "Anthropic API 크레딧이 부족합니다. Plans & Billing에서 충전해 주세요."
    return f"{type(e).__name__}: {msg[:150]}"


async def build_card(ref: DatasetRef, client: httpx.AsyncClient) -> DataCard:
    ctx = CollectContext(client=client, kaggle_auth=settings.kaggle_auth)
    try:
        card = await collect(ctx, ref)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403, 404):
            raise HTTPException(
                status_code=422,
                detail="데이터셋 페이지에 접근할 수 없습니다. 비공개라면 보유하신 README나 라이선스 문서를 올려 주세요.",
            ) from e
        raise HTTPException(status_code=502, detail=f"수집 실패: {e.request.url} ({code})") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"수집 실패: {e}") from e
    if settings.anthropic_api_key:
        try:
            card = await run_in_threadpool(extract.enrich, card)
        except anthropic.APIError as e:  # 크레딧 부족·한도·장애여도 수집한 기본 정보로 카드는 만든다
            card.needs_documents.append(f"Claude API 호출이 실패해 문서 기반 항목 추출을 건너뛰었습니다: {_api_error_text(e)}")
    else:
        card.needs_documents.append("ANTHROPIC_API_KEY가 없어 문서 기반 항목 추출을 건너뛰었습니다.")
    return card


@app.post("/api/datacards", dependencies=[Depends(require_token)])
async def create_card(req: CreateCardRequest):
    async with http_client() as client:
        ref = req.ref
        if ref is None:
            if not req.query:
                raise HTTPException(status_code=400, detail="query 또는 ref가 필요합니다.")
            result = await identify(client, req.query, settings.kaggle_auth)
            if result.status != "resolved":
                # 하나로 확정되지 않으면 분석하지 않고 선택·추가 정보를 요청한다
                raise HTTPException(status_code=409, detail=result.model_dump(mode="json"))
            ref = result.ref
        card = await build_card(ref, client)
    return await store.save(card)


@app.get("/api/datacards", dependencies=[Depends(require_token)])
async def list_cards(limit: int = 20) -> list[dict]:
    return await store.recent(min(limit, 100))


@app.get("/api/datacards/{card_id}", dependencies=[Depends(require_token)])
async def get_card(card_id: str) -> DataCard:
    card = await store.get(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="카드를 찾을 수 없습니다.")
    return card


async def reputation_area(card: DataCard, with_news: bool = True) -> risk.AreaOpinion:
    index = await watch_cache.get()
    entities = reputation.baseline_entities(card)
    if settings.anthropic_api_key:
        try:
            entities += await run_in_threadpool(reputation.llm_entities, card, anthropic.Anthropic())
        except Exception:  # 이름 추출 실패는 기본 이름만으로 대조한다
            pass
    found = None
    if with_news:  # 뉴스는 검토 대상 데이터셋에만 쓴다(원본 노드마다 검색하면 호출이 너무 많아진다)
        names = [e.name for e in entities if e.role in reputation.DIRECT_ROLES]
        found = await news.news_findings(card, names, gdelt.search)
    return reputation.opinion(card, index, entities, found)


async def areas_for(card: DataCard) -> list[risk.AreaOpinion]:
    """한 데이터셋의 라이선스·개인정보·평판·기타 영역 (원본 데이터셋 분석에도 같은 것을 쓴다)."""
    return [risk.license_opinion(card), risk.privacy_opinion(card), await reputation_area(card, with_news=False), others.other_opinion(card)]


async def upstreams_for(card: DataCard) -> list[lineage.Upstream]:
    found = lineage.baseline_upstreams(card)
    if settings.anthropic_api_key:
        try:
            found = lineage.merge(await run_in_threadpool(lineage.llm_upstreams, card, anthropic.Anthropic()), found)
        except Exception:  # 추출 실패 시 카드에 적힌 원본만 쓴다
            pass
    return found


async def build_child(ref: DatasetRef) -> DataCard:
    async with http_client() as client:
        return await build_card(ref, client)


class RiskRequest(BaseModel):
    node_ids: list[str] | None = None  # 분석할 원본(계보 지도의 노드 id). 없으면 depth 단계 이내


@app.post("/api/datacards/{card_id}/lineage", dependencies=[Depends(require_token)])
async def explore_lineage(card_id: str, depth: int = lineage.MAX_DEPTH) -> lineage.LineageMap:
    """최종 원본까지 계보를 따라가며 원본별 기본 정보만 모은다(리스크 분석 없음)."""
    card = await store.get(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="카드를 찾을 수 없습니다.")
    m = await lineage.explore(card, max_depth=depth, build_child=build_child, upstreams_for=upstreams_for)
    return await store.save_lineage(m)


@app.get("/api/datacards/{card_id}/lineage", dependencies=[Depends(require_token)])
async def get_lineage(card_id: str) -> lineage.LineageMap:
    m = await store.latest_lineage(card_id)
    if m is None:
        raise HTTPException(status_code=404, detail="아직 원본 계보를 탐색하지 않았습니다.")
    return m


@app.post("/api/datacards/{card_id}/risk", dependencies=[Depends(require_token)])
async def run_risk(card_id: str, req: RiskRequest | None = None, depth: int = 1) -> risk.RiskReport:
    """저장된 데이터 카드로 리스크를 (재)분석한다. 카드를 고친 뒤 다시 돌리면 반영된다.

    원본 계보 지도가 없으면 먼저 탐색한다. 분석할 상위 데이터셋은 node_ids로 고르고,
    고르지 않으면 depth 단계 이내(기본 1)를 분석한다.
    """
    card = await store.get(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="카드를 찾을 수 없습니다.")
    m = await store.latest_lineage(card_id)
    if m is None:
        m = await explore_lineage(card_id)
    selected = set(req.node_ids) if req and req.node_ids is not None else lineage.default_selection(m, depth)
    rep = await reputation_area(card)
    area, nodes = await lineage.analyze(m, selected, areas_for=areas_for, build_child=build_child)
    return await store.save_risk(risk.analyze(card, extra=[rep, others.other_opinion(card), area], lineage=nodes))


@app.get("/api/datacards/{card_id}/risk", dependencies=[Depends(require_token)])
async def get_risk(card_id: str) -> risk.RiskReport:
    report = await store.latest_risk(card_id)
    if report is None:
        raise HTTPException(status_code=404, detail="아직 리스크 분석을 하지 않았습니다.")
    if report.opinion is None:  # 종합 의견이 생기기 전에 만든 리포트도 저장된 결과로 바로 정리해 보여 준다
        report.opinion = summary.summarize(report.areas, report.lineage).model_dump(mode="json")
    return report


@app.patch("/api/datacards/{card_id}", dependencies=[Depends(require_token)])
async def patch_card(card_id: str, req: PatchCardRequest) -> DataCard:
    card = await store.get(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="카드를 찾을 수 없습니다.")
    unknown = set(req.fields) - set(CARD_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"알 수 없는 항목: {sorted(unknown)}")
    for key, value in req.fields.items():
        card.fields[key] = CardField(value=value, status=FieldStatus.user)
    return await store.update(card)
