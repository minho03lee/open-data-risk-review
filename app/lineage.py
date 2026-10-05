"""원본 콘텐츠 계보 추적 (기획안 6장).

데이터셋이 기대는 원본(상위 데이터셋, 웹 크롤링, 플랫폼 콘텐츠, 출판물, 합성 데이터, 직접 수집)을 찾아
상위 데이터셋은 같은 분석을 재귀로 돌리고, 그 밖의 유형은 유형별 규칙으로 등급을 낸다.
데이터셋의 등급은 계보에서 가장 높은 등급을 따르고, 출처가 끊기거나 접근할 수 없는 지점은 '확인 불가' 노드로
두어 그 자체를 중간 이상으로 본다. 유형별 규칙은 초안이며 법무 검토가 필요하다.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable, Literal

import anthropic
from pydantic import BaseModel

from .identify import parse_url
from .models import DataCard, DatasetRef, Platform
from .risk import AreaOpinion, Finding, JurisdictionOpinion, Level, LineageNode, worst
from .watchlists import norm

MODEL = "claude-opus-5-5"
MAX_DEPTH = 3
MAX_NODES = 12

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


AreasFor = Callable[[DataCard], Awaitable[list[AreaOpinion]]]
BuildChild = Callable[[DatasetRef], Awaitable[DataCard]]
UpstreamsFor = Callable[[DataCard], Awaitable[list[Upstream]]]


async def trace(
    root: DataCard, *, depth: int, areas_for: AreasFor, build_child: BuildChild, upstreams_for: UpstreamsFor,
) -> tuple[AreaOpinion, list[LineageNode]]:
    """루트 데이터셋의 원본 계보를 depth 단계까지 추적해 '계보' 영역 의견과 노드 목록을 낸다."""
    depth = max(1, min(depth, MAX_DEPTH))
    nodes: list[LineageNode] = []
    visited = {root.dataset.uid}

    async def visit(card: DataCard, level_no: int, parent_id: str | None) -> tuple[list[Finding], list[Level]]:
        findings: list[Finding] = []
        levels: list[Level] = []
        ups = await upstreams_for(card)
        if not ups:
            lvl = Level.medium
            findings.append(Finding(
                check="원본 출처 공개 여부", level=lvl,
                note="원본 출처를 찾지 못했다. 계보가 끊기는 지점은 그 자체를 중간 이상 리스크로 본다.",
                recommendation="논문·제작 문서에서 원본 출처를 확인해 주세요."))
            return findings, [lvl]
        for u in ups:
            if len(nodes) >= MAX_NODES:
                findings.append(Finding(check="추적 생략", level=Level.unknown, note=f"노드가 {MAX_NODES}개를 넘어 '{u.name}'부터는 추적하지 않았다."))
                levels.append(Level.unknown)
                break
            node_id = f"n{len(nodes) + 1}"
            node = LineageNode(id=node_id, parent_id=parent_id, name=u.name, kind=u.kind, depth=level_no, level=Level.unknown, summary="", url=u.url)
            nodes.append(node)
            ref = resolve_ref(u)
            sub_levels: list[Level] = []
            if u.kind == "데이터셋":
                if ref is None:
                    node.level, node.summary = Level.medium, "상위 데이터셋을 하나로 특정하지 못해 분석하지 못했다(확인 불가 노드)."
                elif ref.uid in visited:
                    node.level, node.summary = Level.unknown, "이미 계보에 나온 데이터셋이다(순환 참조 방지로 다시 분석하지 않음)."
                else:
                    visited.add(ref.uid)
                    try:
                        child = await build_child(ref)
                    except Exception as e:  # 접근 불가 노드는 확인 불가로 둔다
                        node.level, node.summary = Level.medium, f"상위 데이터셋에 접근하지 못해 분석하지 못했다({type(e).__name__}). 확인 불가 노드."
                    else:
                        areas = await areas_for(child)
                        node.area_levels = {a.area: a.level for a in areas}
                        own = worst([a.level for a in areas])
                        if level_no < depth:
                            _, sub_levels = await visit(child, level_no + 1, node_id)
                        node.level = worst([own, *sub_levels])
                        node.summary = " · ".join(f"{a.area} {a.level.value}" for a in areas) + (
                            "" if level_no < depth else " (이보다 위 계보는 깊이 제한으로 추적하지 않음)")
                        node.url = child.dataset.url or node.url
            else:
                lvl, note, _ = RULES[u.kind]
                node.level, node.summary = lvl, note
            levels.append(node.level)
            rec = RULES[u.kind][2] if u.kind != "데이터셋" else "상위 데이터셋의 라이선스·개인정보 결과를 확인하세요."
            findings.append(Finding(
                check=f"원본: {u.name} ({u.kind})", level=node.level, note=node.summary,
                evidence_url=u.evidence_url or u.url, evidence_quote=u.evidence_quote,
                recommendation=rec if node.level != Level.low else None))
        return findings, levels

    findings, levels = await visit(root, 1, None)
    level = worst(levels)
    recs = list(dict.fromkeys(f.recommendation for f in findings if f.recommendation and f.level != Level.low))
    top = [f for f in findings if f.level == level]
    summary = ("계보에서 가장 높은 등급이 상위로 전파된다. " + "; ".join(f.check.removeprefix("원본: ") for f in top[:3]) + "이(가) 원인이다.") if top else "추적한 계보에서 쟁점이 없다."
    area = AreaOpinion(
        area="원본 계보", level=level, findings=findings, recommendations=recs, summary=summary,
        where=f"원본 {len(nodes)}개 노드, 최대 {depth}단계",
        jurisdictions=[JurisdictionOpinion(jurisdiction=j, level=level, note=n) for j, n in (
            ("한국", "원본 저작물의 권리는 원본 데이터셋 라이선스와 별개로 남을 수 있어 법무 확인이 필요하다."),
            ("미국", "상위 라이선스보다 넓게 재라이선스된 경우 계약·저작권 쟁점이 된다."),
            ("EU", "크롤링 원본은 DSM 지침의 TDM 예외와 권리자 옵트아웃 준수 여부를 확인해야 한다."))],
        not_checked=["대규모 크롤링의 상위 도메인별 약관·robots.txt 실측", "플랫폼별 이용약관 규칙 라이브러리", "원본 저작물의 불법 복제 사이트 대조 목록"],
    )
    return area, nodes
