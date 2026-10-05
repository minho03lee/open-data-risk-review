"""오픈데이터 리스크 검토 API (1단계: 식별 + 데이터 카드 생성)."""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import extract
from .config import settings
from .connectors import CollectContext, collect
from .identify import identify
from .models import CARD_FIELDS, CardField, DataCard, DatasetRef, FieldStatus, IdentifyResult
from .store import make_store

app = FastAPI(title="Open Data Risk Review", version="0.1.0")
store = make_store(settings.database_url)
STATIC = Path(__file__).parent / "static"


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
        card = await run_in_threadpool(extract.enrich, card)
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
