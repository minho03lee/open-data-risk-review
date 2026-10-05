"""입력(URL·이름)을 데이터셋 하나로 확정한다 (기획안 4장)."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urlparse

import httpx

from .models import Candidate, DatasetRef, IdentifyResult, Platform

_HF_HOSTS = {"huggingface.co", "www.huggingface.co", "hf.co"}
_KAGGLE_HOSTS = {"kaggle.com", "www.kaggle.com"}
_AIHUB_HOSTS = {"aihub.or.kr", "www.aihub.or.kr"}


def parse_url(text: str) -> DatasetRef | None:
    """플랫폼별 URL 파서. URL이 아니면 None."""
    text = text.strip()
    if not re.match(r"^https?://", text):
        return None
    u = urlparse(text)
    host = u.netloc.lower()
    parts = [p for p in u.path.split("/") if p]

    if host in _HF_HOSTS and len(parts) >= 3 and parts[0] == "datasets":
        repo = f"{parts[1]}/{parts[2]}"
        revision = None
        if len(parts) >= 5 and parts[3] in ("tree", "blob", "resolve"):
            revision = parts[4]
        subset = parse_qs(u.query).get("subset", [None])[0]
        return DatasetRef(
            platform=Platform.huggingface,
            repo=repo,
            version=subset,
            revision=revision,
            url=f"https://huggingface.co/datasets/{repo}",
        )

    if host in _KAGGLE_HOSTS and len(parts) >= 3 and parts[0] == "datasets":
        repo = f"{parts[1]}/{parts[2]}"
        version = parts[4] if len(parts) >= 5 and parts[3] == "versions" else None
        return DatasetRef(
            platform=Platform.kaggle,
            repo=repo,
            version=version,
            url=f"https://www.kaggle.com/datasets/{repo}",
        )

    if host in _AIHUB_HOSTS:
        sn = parse_qs(u.query).get("dataSetSn", [None])[0]
        if sn:
            return DatasetRef(
                platform=Platform.aihub,
                repo=sn,
                url=f"https://www.aihub.or.kr/aihubdata/data/view.do?dataSetSn={sn}",
            )

    return DatasetRef(platform=Platform.web, repo=f"{host}{u.path}".rstrip("/"), url=text)


def _score(query: str, title: str, repo: str) -> float:
    q = query.lower().strip()
    name = repo.split("/")[-1].lower()
    best = max(
        SequenceMatcher(None, q, title.lower()).ratio(),
        SequenceMatcher(None, q, name).ratio(),
        SequenceMatcher(None, q, repo.lower()).ratio(),
    )
    if q == name or q == repo.lower() or q == title.lower():
        best = 1.0
    return round(best, 3)


async def search_huggingface(client: httpx.AsyncClient, query: str, limit: int = 5) -> list[Candidate]:
    r = await client.get(
        "https://huggingface.co/api/datasets",
        params={"search": query, "limit": limit, "sort": "downloads", "direction": -1},
    )
    r.raise_for_status()
    out = []
    for d in r.json():
        repo = d.get("id", "")
        out.append(
            Candidate(
                ref=DatasetRef(platform=Platform.huggingface, repo=repo, url=f"https://huggingface.co/datasets/{repo}"),
                title=repo,
                provider=d.get("author"),
                score=_score(query, repo, repo),
                reason="Hugging Face 검색 결과",
            )
        )
    return out


async def search_kaggle(client: httpx.AsyncClient, query: str, auth: tuple[str, str] | None, limit: int = 5) -> list[Candidate]:
    if not auth:
        return []
    r = await client.get("https://www.kaggle.com/api/v1/datasets/list", params={"search": query}, auth=auth)
    r.raise_for_status()
    out = []
    for d in r.json()[:limit]:
        repo = d.get("ref", "")
        title = d.get("title") or repo
        out.append(
            Candidate(
                ref=DatasetRef(platform=Platform.kaggle, repo=repo, url=f"https://www.kaggle.com/datasets/{repo}"),
                title=title,
                provider=d.get("ownerName") or repo.split("/")[0],
                score=_score(query, title, repo),
                reason="Kaggle 검색 결과",
            )
        )
    return out


CONFIRM_THRESHOLD = 0.9
GAP_THRESHOLD = 0.15


def decide(query: str, candidates: list[Candidate]) -> IdentifyResult:
    """기획안 4장 표의 기준대로 확정하거나 추가 정보를 요청한다."""
    if not candidates:
        return IdentifyResult(
            status="need_info",
            question="다운로드 URL, 논문 제목, 제공 기관 중 아는 것을 알려 주세요.",
        )
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    top = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    if top.score >= CONFIRM_THRESHOLD and top.score - runner_up >= GAP_THRESHOLD:
        return IdentifyResult(
            status="confirm",
            ref=top.ref,
            candidates=[top],
            question=f"{_platform_label(top.ref.platform)}의 {top.ref.repo} 맞나요?",
        )
    return IdentifyResult(status="candidates", candidates=ranked[:5], question="아래 중 어떤 데이터셋인가요?")


def _platform_label(p: Platform) -> str:
    return {"huggingface": "Hugging Face", "kaggle": "Kaggle", "aihub": "AI허브", "web": "웹"}[p.value]


async def identify(client: httpx.AsyncClient, query: str, kaggle_auth: tuple[str, str] | None = None) -> IdentifyResult:
    ref = parse_url(query)
    if ref is not None:
        return IdentifyResult(status="resolved", ref=ref)
    candidates: list[Candidate] = []
    for search in (
        lambda: search_huggingface(client, query),
        lambda: search_kaggle(client, query, kaggle_auth),
    ):
        try:
            candidates += await search()
        except httpx.HTTPError:
            continue
    return decide(query, candidates)
