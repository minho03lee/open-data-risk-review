"""원본 콘텐츠 계보 추적 (기획안 6장).

두 단계로 나눈다. (1) 탐색: 최종 원본까지 계보를 따라가며 원본별 기본 정보(이름·URL·설명·제공자·라이선스 표기)만
모은다 — 가볍다. (2) 분석: 사용자가 고른 상위 데이터셋에만 라이선스·개인정보·평판 분석을 돌리고, 그 밖의 유형
(웹 크롤링·플랫폼·출판물·합성·직접 수집)은 유형별 규칙으로 등급을 낸다.
데이터셋의 등급은 계보에서 가장 높은 등급을 따르고, 출처가 끊기거나 접근할 수 없는 지점은 '확인 불가' 노드로
두어 그 자체를 중간 이상으로 본다. 유형별 규칙은 초안이며 법무 검토가 필요하다.
"""
from __future__ import annotations

import re
import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

import anthropic
from pydantic import BaseModel, Field

from .identify import parse_url
from .models import DataCard, DatasetRef, Platform
from .risk import AreaOpinion, Finding, JurisdictionOpinion, Level, LineageNode, worst
from .watchlists import norm

MODEL = "claude-opus-5-5"
MAX_DEPTH = 5  # 탐색은 최종 원본까지 가되 폭주를 막는 상한
MAX_NODES = 20

Kind = Literal["데이터셋", "웹 크롤링", "플랫폼 콘텐츠", "출판물·저작물", "합성 데이터(모델)", "직접 수집", "기타"]


class Upstream(BaseModel):
    name: str
    kind: Kind
    url: str | None = None
    evidence_url: str | None = None
    evidence_quote: str | None = None


class _Extracted(BaseModel):
    name: str
    kind: Kind
    url: str | None
    evidence_url: str
    evidence_quote: str


class _Upstreams(BaseModel):
    upstreams: list[_Extracted]


SYSTEM = """당신은 AI 학습용 데이터셋이 어떤 원본에서 만들어졌는지 문서에서 찾는 분석가입니다.
문서(<document> 태그)에 적힌 원본만, 적힌 표기 그대로 냅니다. 추측하지 않습니다.
유형: 데이터셋(상위·재사용 데이터셋), 웹 크롤링(Common Crawl 등 웹 수집), 플랫폼 콘텐츠(YouTube·Reddit·X·GitHub 등 특정 플랫폼),
출판물·저작물(도서·논문·음원·이미지), 합성 데이터(모델) — 이름에는 생성 모델을 쓰고, 직접 수집(녹음·촬영·설문), 기타.
검색엔진(Google·Bing 등)이나 사진·영상 공유 플랫폼(Flickr·YouTube 등)에서 검색하거나 내려받아 모았다고 적혀 있으면,
각 검색엔진과 플랫폼을 따로 '플랫폼 콘텐츠' 원본으로 냅니다(이름은 문서의 표기 그대로, 예: Google, Flickr).
url은 문서에 적힌 경우에만 쓰고 없으면 null, evidence_url은 이름이 실린 문서의 url 속성 그대로,
evidence_quote는 그 문서의 원문을 글자 그대로 짧게 인용합니다. 이 데이터셋 자신은 제외합니다."""

# 유형별 초안 규칙 (등급, 판단, 권고). 법무 검토 전 초안이다.
RULES: dict[str, tuple[Level, str, str]] = {
    "웹 크롤링": (Level.medium, "공개 웹에서 수집된 콘텐츠라 개별 저작권자의 허락과 사이트 약관·robots.txt·옵트아웃 준수 여부를 확인하기 어렵다.",
                 "상위 도메인 목록과 크롤링 시점의 약관·옵트아웃 표시를 확인하세요."),
    "플랫폼 콘텐츠": (Level.medium, "특정 플랫폼 콘텐츠는 플랫폼 약관과 API 이용조건이 수집·AI 학습을 제한할 수 있다. 플랫폼별 규칙 라이브러리는 아직 없어 약관 원문 확인이 필요하다.",
                    "해당 플랫폼의 이용약관과 API 이용조건 원문을 확인하세요."),
    "출판물·저작물": (Level.medium, "도서·논문·음원·이미지는 저작권이 살아 있을 수 있고, 불법 복제 사이트 출처인지도 확인해야 한다.",
                    "원본 저작물의 저작권 상태와 입수 경로를 확인하세요."),
    "합성 데이터(모델)": (Level.medium, "모델이 생성한 데이터는 생성 모델의 이용약관(경쟁 모델 학습 금지 조항 등)과 모델 개발사 소재국이 쟁점이 된다.",
                      "생성 모델의 이용약관과 라이선스를 확인하세요."),
    "직접 수집": (Level.unknown, "녹음·촬영·설문 등 직접 수집이라 참여자 동의 범위와 수집 국가 법령을 확인해야 한다.",
                "논문·카드의 동의 절차 서술을 확인하세요."),
    "기타": (Level.unknown, "원본 유형을 판별하지 못했다.", "제작 문서에서 원본 출처를 확인하세요."),
}


# 이름에 이 낱말이 들어 있는 플랫폼·검색엔진 콘텐츠에 쓰는 초안 규칙. 법무 검토 전 초안이다.
PLATFORM_RULES: list[tuple[tuple[str, ...], tuple[Level, str, str]]] = [
    (("google", "bing", "baidu", "yahoo", "duckduckgo", "naver", "검색엔진", "image search", "이미지 검색"),
     (Level.high, "검색엔진 결과로 모은 이미지는 검색엔진이 이용 허락을 주는 것이 아니라 각 이미지의 저작권자에게 권리가 남아 있고, 개별 사이트의 약관도 적용된다. 상용 모델 학습에는 높은 리스크로 본다.",
      "수집 대상 사이트·이미지별 라이선스를 확인하고, 라이선스가 명시된 이미지만 쓰는 방안을 검토하세요.")),
    (("flickr",),
     (Level.medium, "Flickr 사진은 사진마다 라이선스가 다르다(CC 여러 종류 또는 모든 권리 보유). 상업적 이용·AI 학습 허용 여부와 Flickr 약관·API 조건을 개별로 확인해야 한다.",
      "사진별 라이선스 필터(상업적 이용 허용 CC 등) 적용 여부와 Flickr 약관을 확인하세요.")),
]


def rule_for(name: str, kind: str) -> tuple[Level, str, str]:
    low = name.lower()
    if kind in ("플랫폼 콘텐츠", "웹 크롤링", "출판물·저작물"):
        for words, rule in PLATFORM_RULES:
            if any(w in low for w in words):
                return rule
    return RULES.get(kind, RULES["기타"])


def _flat(v) -> list[str]:
    if v in (None, "", [], {}):
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [x for val in v.values() for x in _flat(val)]
    if isinstance(v, (list, tuple)):
        return [x for i in v for x in _flat(i)]
    return [str(v)]


def baseline_upstreams(card: DataCard) -> list[Upstream]:
    """LLM 없이 카드의 '원본 출처 목록'에서 얻을 수 있는 항목."""
    f = card.fields["upstream_sources"]
    out = []
    for text in _flat(f.value):
        text = text.replace("source_datasets:", "").strip()
        if not text or text.lower() == "original":
            continue
        url = re.search(r"https?://\S+", text)
        if url:
            out.append(Upstream(name=text, kind="데이터셋" if parse_url(url.group(0)) and parse_url(url.group(0)).platform != Platform.web else "기타",
                                url=url.group(0).rstrip(".,)"), evidence_url=f.evidence_url))
        elif re.fullmatch(r"[\w.-]+/[\w.-]+", text):
            out.append(Upstream(name=text, kind="데이터셋", url=f"https://huggingface.co/datasets/{text}", evidence_url=f.evidence_url))
        else:
            out.append(Upstream(name=text, kind="기타", evidence_url=f.evidence_url))
    return out


def llm_upstreams(card: DataCard, client: anthropic.Anthropic) -> list[Upstream]:
    docs = "\n".join(f'<document url="{s.url}">\n{s.content[:20000]}\n</document>' for s in card.sources if s.content)
    fields = "\n".join(
        f"{k}: {' '.join(_flat(card.fields[k].value))}" for k in ("creation", "upstream_sources", "description") if not card.fields[k].is_missing
    )
    if not docs and not fields:
        return []
    response = client.beta.messages.parse(
        model=MODEL, max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": f"{docs}\n\n<card>\n{fields}\n</card>\n\n원본을 모두 뽑아 주세요."}],
        output_format=_Upstreams,
    )
    if response.parsed_output is None:
        return []
    corpus = norm(docs + " " + fields)
    out = []
    for u in response.parsed_output.upstreams:
        # 문서에 없는 이름은 버린다 (환각 방지). URL은 문서에 있을 때만 쓴다.
        if norm(u.name) and norm(u.name) in corpus:
            url = u.url if u.url and u.url in (docs + fields) else None
            out.append(Upstream(name=u.name, kind=u.kind, url=url, evidence_url=u.evidence_url, evidence_quote=u.evidence_quote))
    return out


def merge(*lists: list[Upstream]) -> list[Upstream]:
    seen, out = set(), []
    for lst in lists:
        for u in lst:
            k = norm(u.name) or u.url
            if k and k not in seen:
                seen.add(k)
                out.append(u)
    return out


def resolve_ref(u: Upstream) -> DatasetRef | None:
    if u.kind != "데이터셋":
        return None
    if u.url:
        ref = parse_url(u.url)
        if ref and ref.platform != Platform.web:
            return ref
    if re.fullmatch(r"[\w.-]+/[\w.-]+", u.name):
        return DatasetRef(platform=Platform.huggingface, repo=u.name, url=f"https://huggingface.co/datasets/{u.name}")
    return None


BuildChild = Callable[[DatasetRef], Awaitable[DataCard]]
UpstreamsFor = Callable[[DataCard], Awaitable[list[Upstream]]]
AreasFor = Callable[[DataCard], Awaitable[list[AreaOpinion]]]

Status = Literal["확인됨", "확인 불가", "접근 실패", "중복", "추적 생략"]


class MapNode(BaseModel):
    """계보 지도의 한 원본. 분석 전에 보여 주는 기본 정보만 담는다."""

    id: str
    parent_id: str | None = None
    name: str
    kind: str
    depth: int
    url: str | None = None
    description: str = ""
    provider: str = ""
    license: str = ""
    status: Status = "확인됨"
    note: str = ""
    lineage_break: bool = False  # 이 데이터셋의 원본 출처를 찾지 못함
    ref: DatasetRef | None = None
    evidence_url: str | None = None
    evidence_quote: str | None = None


class LineageMap(BaseModel):
    id: str | None = None
    datacard_id: str
    nodes: list[MapNode]
    max_depth: int = 0
    root_break: bool = False  # 루트 데이터셋의 원본 출처를 찾지 못함
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _short(text: str, n: int = 240) -> str:
    text = re.sub(r"^#+\s*", "", text.strip(), flags=re.M)
    text = re.sub(r"[`*_>\[\]]|\(https?://[^)]*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def basic_info(card: DataCard) -> dict[str, str]:
    """분석 없이 카드에서 바로 읽을 수 있는 기본 정보."""
    desc = card.fields["description"].value
    if isinstance(desc, dict):
        text = desc.get("summary_ko") or desc.get("original") or ""
    else:
        text = _flat(desc)[0] if _flat(desc) else ""
    if not text:
        text = " ".join(_flat(card.fields["overview"].value))
    lic = card.fields["license"].value
    raw = lic.get("raw") if isinstance(lic, dict) else _flat(lic)
    name = card.fields["name"].value
    return {
        "name": (name.get("official") if isinstance(name, dict) else name) or card.dataset.repo,
        "description": _short(str(text)),
        "provider": ", ".join(_flat(card.fields["provider"].value))[:120],
        "license": ", ".join(_flat(raw))[:120],
    }


async def explore(
    root: DataCard, *, max_depth: int = MAX_DEPTH, build_child: BuildChild, upstreams_for: UpstreamsFor,
) -> LineageMap:
    """최종 원본까지(max_depth, MAX_NODES 상한) 계보 지도를 만든다. 리스크 분석은 하지 않는다."""
    max_depth = max(1, min(max_depth, MAX_DEPTH))
    nodes: list[MapNode] = []
    visited = {root.dataset.uid}

    async def visit(card: DataCard, depth: int, parent_id: str | None) -> bool:
        """원본을 찾았으면 True, 못 찾았으면 False(계보 끊김)."""
        ups = await upstreams_for(card)
        for u in ups:
            if len(nodes) >= MAX_NODES:
                nodes.append(MapNode(id=f"n{len(nodes) + 1}", parent_id=parent_id, name=u.name, kind=u.kind, depth=depth,
                                     status="추적 생략", note=f"노드가 {MAX_NODES}개를 넘어 더 탐색하지 않았다."))
                break
            node = MapNode(id=f"n{len(nodes) + 1}", parent_id=parent_id, name=u.name, kind=u.kind, depth=depth, url=u.url,
                           evidence_url=u.evidence_url, evidence_quote=u.evidence_quote)
            nodes.append(node)
            if u.kind != "데이터셋":
                continue
            ref = resolve_ref(u)
            if ref is None:
                node.status, node.note = "확인 불가", "상위 데이터셋을 하나로 특정하지 못했다."
                continue
            node.ref = ref
            if ref.uid in visited:
                node.status, node.note = "중복", "이미 계보에 나온 데이터셋이다(순환 참조 방지)."
                continue
            visited.add(ref.uid)
            try:
                child = await build_child(ref)
            except Exception as e:  # 접근 불가 노드는 확인 불가로 둔다
                node.status, node.note = "접근 실패", f"상위 데이터셋에 접근하지 못했다({type(e).__name__})."
                continue
            info = basic_info(child)
            node.name, node.description, node.provider, node.license = info["name"], info["description"], info["provider"], info["license"]
            node.url = child.dataset.url or node.url
            if depth < max_depth:
                node.lineage_break = not await visit(child, depth + 1, node.id)
            else:
                node.note = "최대 탐색 단계에 도달해 이보다 위 원본은 찾지 않았다."
        return bool(ups)

    found = await visit(root, 1, None)
    return LineageMap(datacard_id=root.id or "", nodes=nodes, max_depth=max((n.depth for n in nodes), default=0), root_break=not found)


def default_selection(m: LineageMap, depth: int = 1) -> set[str]:
    """기본 선택: depth 단계 이내의 분석 가능한 상위 데이터셋."""
    return {n.id for n in m.nodes if n.kind == "데이터셋" and n.status == "확인됨" and n.depth <= depth}


async def analyze(
    m: LineageMap, selected: set[str], *, areas_for: AreasFor, build_child: BuildChild,
) -> tuple[AreaOpinion, list[LineageNode]]:
    """고른 상위 데이터셋만 분석하고, 계보 전체의 가장 높은 등급을 위로 전파한다."""
    by_id = {n.id: n for n in m.nodes}
    sem = asyncio.Semaphore(3)
    areas: dict[str, list[AreaOpinion] | Exception] = {}

    async def run(n: MapNode) -> None:
        async with sem:
            try:
                areas[n.id] = await areas_for(await build_child(n.ref))
            except Exception as e:  # 한 원본의 실패가 전체 분석을 막지 않는다
                areas[n.id] = e

    await asyncio.gather(*(run(by_id[i]) for i in selected if i in by_id and by_id[i].kind == "데이터셋" and by_id[i].ref and by_id[i].status == "확인됨"))

    out: dict[str, LineageNode] = {}
    children: dict[str | None, list[str]] = {}
    for n in m.nodes:
        children.setdefault(n.parent_id, []).append(n.id)

    for n in sorted(m.nodes, key=lambda x: -x.depth):  # 깊은 노드부터 계산해 위로 전파한다
        analyzed = False
        area_levels: dict[str, Level] = {}
        if n.kind != "데이터셋":
            own, summary = rule_for(n.name, n.kind)[:2]
        elif n.status == "확인됨" and n.id in selected:
            got = areas.get(n.id)
            if isinstance(got, Exception):
                own, summary = Level.medium, f"선택했지만 분석에 실패했다({type(got).__name__}). 확인 불가 노드."
            else:
                analyzed = True
                area_levels = {a.area: a.level for a in got}
                own = worst(list(area_levels.values()))
                summary = " · ".join(f"{a.area} {a.level.value}" for a in got)
        elif n.status == "확인됨":
            own, summary = Level.unknown, "분석하지 않았다(선택에서 제외). 낮음으로 단정하지 않는다."
        elif n.status == "중복":
            own, summary = Level.unknown, n.note
        else:  # 확인 불가·접근 실패·추적 생략
            own, summary = Level.medium, n.note + " 확인 불가 노드로 본다."
        if n.lineage_break and n.status == "확인됨":
            own = worst([own, Level.medium])
            summary += " (이 데이터셋의 원본 출처를 찾지 못했다)"
        level = worst([own, *(out[c].level for c in children.get(n.id, []))])
        out[n.id] = LineageNode(id=n.id, parent_id=n.parent_id, name=n.name, kind=n.kind, depth=n.depth, level=level,
                                summary=summary, url=n.url, area_levels=area_levels, analyzed=analyzed,
                                description=n.description, provider=n.provider, license=n.license)

    findings: list[Finding] = []
    if m.root_break:
        findings.append(Finding(
            check="원본 출처 공개 여부", level=Level.medium,
            note="원본 출처를 찾지 못했다. 계보가 끊기는 지점은 그 자체를 중간 이상 리스크로 본다.",
            recommendation="논문·제작 문서에서 원본 출처를 확인해 주세요."))
    for n in m.nodes:
        if n.parent_id is not None:
            continue
        node = out[n.id]
        rec = rule_for(n.name, n.kind)[2] if n.kind != "데이터셋" else "상위 데이터셋의 라이선스·개인정보 결과를 확인하세요."
        findings.append(Finding(check=f"원본: {n.name} ({n.kind})", level=node.level, note=node.summary,
                                evidence_url=n.evidence_url or n.url, evidence_quote=n.evidence_quote,
                                recommendation=rec if node.level != Level.low else None))
    skipped = [n for n in m.nodes if n.kind == "데이터셋" and n.status == "확인됨" and n.id not in selected]
    if skipped:
        findings.append(Finding(
            check="분석하지 않은 원본", level=Level.unknown,
            note=f"상위 데이터셋 {len(skipped)}개를 분석하지 않았다: " + ", ".join(n.name for n in skipped[:5]) + (" 외" if len(skipped) > 5 else "") + ". 낮음으로 단정하지 않는다.",
            recommendation="위험해 보이는 원본이 있으면 선택해서 다시 분석하세요."))

    levels = [f.level for f in findings]
    level = worst(levels)
    recs = list(dict.fromkeys(f.recommendation for f in findings if f.recommendation and f.level != Level.low))
    top = [f for f in findings if f.level == level and level != Level.low]
    summary = ("계보에서 가장 높은 등급이 상위로 전파된다. " + "; ".join(f.check.removeprefix("원본: ") for f in top[:3]) + "이(가) 원인이다.") if top else "분석한 계보에서 쟁점이 없다."
    n_an = sum(1 for x in out.values() if x.analyzed)
    area = AreaOpinion(
        area="원본 계보", level=level, findings=findings, recommendations=recs, summary=summary,
        where=f"원본 {len(m.nodes)}개 노드(최대 {m.max_depth}단계), 상위 데이터셋 {n_an}개 분석",
        jurisdictions=[JurisdictionOpinion(jurisdiction=j, level=level, note=t) for j, t in (
            ("한국", "원본 저작물의 권리는 원본 데이터셋 라이선스와 별개로 남을 수 있어 법무 확인이 필요하다."),
            ("미국", "상위 라이선스보다 넓게 재라이선스된 경우 계약·저작권 쟁점이 된다."),
            ("EU", "크롤링 원본은 DSM 지침의 TDM 예외와 권리자 옵트아웃 준수 여부를 확인해야 한다."))],
        not_checked=["대규모 크롤링의 상위 도메인별 약관·robots.txt 실측", "플랫폼별 이용약관 규칙 라이브러리", "원본 저작물의 불법 복제 사이트 대조 목록"],
    )
    return area, [out[n.id] for n in m.nodes]
