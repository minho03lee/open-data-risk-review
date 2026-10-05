"""언론 보도 점검 (기획안 7-3장 '언론 보도').

데이터셋 이름과 제작·제공 기관 이름으로 GDELT DOC 2.0 API(무료, 키 없음)에서 최근 뉴스를 찾고,
기사 제목에 이름이 나오면서 부정 표현이 함께 있는 기사를 모아 근거(기사 URL·제목 원문)와 함께 보여 준다.

GDELT는 최근 약 3개월 보도만 검색되고 제목·메타데이터 중심이라, 기사가 없어도 '낮음'이 아니라 '확인 필요'로 둔다.
부정 기사가 있으면 중간(기획안 기준). 이름이 짧거나 흔해 동명 기사가 섞일 수 있으면 사람이 확인하게 '확인 필요'로 둔다.
부정 표현 목록은 초안이다.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

import httpx

from .models import DataCard
from .risk import Finding, Level
from .watchlists import norm

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_QUERIES = 3
MAX_RECORDS = 25
TTL_SECONDS = 6 * 3600
MIN_INTERVAL = 5.0  # GDELT 권장: 5초에 한 번 이하
COVERAGE = "GDELT는 최근 약 3개월 보도만 검색하고 제목 중심이라 이전 보도나 본문에만 나오는 보도는 찾지 못한다."

# 영어는 단어 단위로 맞춘다('issue'의 'sue', 'urban'의 'ban' 같은 오탐을 막는다). 끝의 *는 어간(접미 변화 허용).
_NEG_EN = ("lawsuit*", "sue", "sues", "sued", "suing", "copyright*", "infring*", "ban", "bans", "banned", "banning", "controvers*", "scandal*",
           "illegal*", "unlawful*", "privacy", "breach*", "leak*", "pirat*", "stolen", "scrap*", "child abuse", "csam", "bias*", "racis*",
           "toxic*", "sanction*", "blacklist*", "entity list", "withdraw*", "pulled", "taken down", "removed", "fined", "fines", "investigat*",
           "probe", "probes", "violat*", "backlash", "criticis*", "criticiz*")
_NEG_KO = ("소송", "고소", "고발", "저작권", "침해", "논란", "불법", "유출", "개인정보", "제재", "금지", "삭제", "철회", "조사", "위반", "비판", "편향", "혐오")
_NEG_RE = re.compile(r"\b(?:" + "|".join(re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w) for w in _NEG_EN) + r")\b", re.I)


@dataclass
class Article:
    title: str
    url: str
    domain: str = ""
    seen: str = ""


class SearchError(Exception):
    pass


def queries_for(card: DataCard, provider_names: list[str]) -> list[tuple[str, str]]:
    """(검색에 쓴 이름, 검색식). 데이터셋 이름 + 제작·제공 기관 이름(최대 MAX_QUERIES개)."""
    names: list[str] = []
    f = card.fields["name"]
    if not f.is_missing and isinstance(f.value, str):
        names.append(f.value.strip())
    names.append(card.dataset.repo.rsplit("/", 1)[-1].replace("_", " ").replace("-", " "))
    names += provider_names
    out, seen = [], set()
    for n in names:
        k = norm(n)
        if len(k) >= 2 and k not in seen and '"' not in n:
            seen.add(k)
            out.append((n, f'"{n}" (dataset OR "training data" OR "AI model")'))
    return out[:MAX_QUERIES]


class GdeltClient:
    """GDELT 호출. 5초 간격을 지키고 성공한 결과는 6시간 캐시한다."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, list[Article]]] = {}
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _get(self, query: str) -> dict:
        params = {"query": query, "mode": "artlist", "format": "json", "maxrecords": MAX_RECORDS, "sort": "datedesc", "timespan": "3months"}
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "open-data-risk-review/1.0"}) as client:
                    r = await client.get(GDELT_URL, params=params)
            except httpx.HTTPError as e:
                raise SearchError(f"{type(e).__name__}: {e}"[:120]) from e
            if r.status_code in (429, 502, 503, 504) and attempt == 1:  # 요청 한도·일시 장애는 한 번 더 시도한다
                await asyncio.sleep(MIN_INTERVAL + 1)
                continue
            if r.status_code >= 400:
                raise SearchError(f"HTTP {r.status_code} {r.text[:80].strip()}".strip())
            try:
                return r.json()
            except ValueError as e:  # 검색식 오류 등은 JSON이 아닌 문장으로 온다
                raise SearchError(r.text[:120].strip() or "응답 형식 오류") from e
        raise SearchError("재시도 실패")

    async def search(self, query: str) -> list[Article]:
        async with self._lock:
            hit = self._cache.get(query)
            if hit and time.time() - hit[0] < TTL_SECONDS:
                return hit[1]
            wait = MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                data = await self._get(query)
            finally:
                self._last = time.time()
            arts = [Article(title=(a.get("title") or "").strip(), url=a.get("url") or "", domain=a.get("domain") or "", seen=(a.get("seendate") or "")[:8])
                    for a in data.get("articles", []) if a.get("url") and a.get("title")]
            self._cache[query] = (time.time(), arts)
            return arts


def is_negative(title: str) -> str | None:
    m = _NEG_RE.search(title)
    if m:
        return m.group(0).lower()
    return next((w for w in _NEG_KO if w in title), None)


def _ambiguous(name: str) -> bool:
    return len(norm(name)) < 5


async def news_findings(card: DataCard, provider_names: list[str], search) -> list[Finding]:
    """search: async (query) -> list[Article]. 실패하면 '확인 필요' 항목을 낸다."""
    qs = queries_for(card, provider_names)
    if not qs:
        return [Finding(check="언론 보도", level=Level.unknown, note="검색할 이름(데이터셋·제작 기관)이 없어 뉴스를 검색하지 못했다.",
                        recommendation="카드에 데이터명·제공자를 입력한 뒤 다시 분석하세요.")]
    out: list[Finding] = []
    negative: list[tuple[str, Article, str]] = []
    ok_names, failed = [], []
    seen_urls: set[str] = set()
    for name, q in qs:
        try:
            arts = await search(q)
        except SearchError as e:
            failed.append(f"{name}({e})")
            continue
        ok_names.append(name)
        key = norm(name)
        for a in arts:
            if a.url in seen_urls or key not in norm(a.title):
                continue
            seen_urls.add(a.url)
            w = is_negative(a.title)
            if w:
                negative.append((name, a, w))
    if failed:
        out.append(Finding(check="언론 보도", level=Level.unknown, note="뉴스 검색에 실패해 확인하지 못했다: " + "; ".join(failed),
                           recommendation="잠시 뒤 다시 분석하거나 직접 뉴스를 검색하세요."))
    for name, a, w in negative[:5]:
        amb = _ambiguous(name)
        when = f"{a.seen[:4]}-{a.seen[4:6]}-{a.seen[6:8]}" if len(a.seen) >= 8 else "날짜 미상"
        out.append(Finding(
            check="언론 보도: 부정 기사", level=Level.unknown if amb else Level.medium,
            note=(f"'{name}'이(가) 제목에 나오고 부정 표현('{w}')이 있는 기사다({a.domain or '출처 미상'}, {when})."
                  + (" 이름이 짧거나 흔해 다른 대상의 기사일 수 있다." if amb else " 같은 대상의 기사인지 읽어 보고 확인해야 한다.")),
            evidence_url=a.url, evidence_quote=a.title,
            recommendation="기사를 읽고 해당 데이터셋·기관에 관한 것인지, 사용 시 평판에 미칠 영향이 있는지 검토하세요."))
    if len(negative) > 5:
        out.append(Finding(check="언론 보도: 부정 기사", level=Level.medium, note=f"위 5건 외에 부정 표현이 있는 기사가 {len(negative) - 5}건 더 있다."))
    if ok_names and not negative:
        out.append(Finding(check="언론 보도", level=Level.unknown,
            note=f"{', '.join(ok_names)}(으)로 검색했지만 제목에 이름이 나오면서 부정 표현이 있는 기사는 찾지 못했다. {COVERAGE}",
            recommendation="중요한 데이터셋이면 직접 뉴스 검색으로 한 번 더 확인하세요."))
    return out
