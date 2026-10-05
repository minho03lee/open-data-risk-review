"""평판 리스크: 제재 목록 대조 (기획안 7-3장).

데이터셋 제작자·제공자·모회사·자금 제공자·원본 제공자 이름을 공개 제재 목록과 대조한다.
등급: 직접 등재(제작자·제공자가 정확히 일치) 높음, 간접 연결·유사 이름 중간, 일치 없음 낮음.
목록을 받지 못했거나 대조 범위가 비어 있으면 '낮음'이 아니라 '확인 필요'로 둔다.
"""
from __future__ import annotations

import re
from typing import Literal

import anthropic
from pydantic import BaseModel

from .models import DataCard, Platform
from .risk import AreaOpinion, Finding, JurisdictionOpinion, Level, worst
from .watchlists import Index, Match, norm

MODEL = "claude-opus-5-5"

DIRECT_ROLES = {"제작자", "제공자"}
Role = Literal["제작자", "제공자", "모회사·자금 제공자", "공동 제작 기관", "원본 제공자", "생성 모델 개발사"]

# 초안: 대상국 목록은 구축 시점의 제재 현황으로 법무가 확정한다 (기획안 7-3장).
CONCERN_COUNTRIES = {
    "중국": ("중국", "china", "chinese", "prc", "hong kong", "홍콩"),
    "러시아": ("러시아", "russia"),
    "이란": ("이란", "iran"),
    "북한": ("북한", "north korea", "dprk"),
    "쿠바": ("쿠바", "cuba"),
    "시리아": ("시리아", "syria"),
    "벨라루스": ("벨라루스", "belarus"),
    "미얀마": ("미얀마", "myanmar"),
    "베네수엘라": ("베네수엘라", "venezuela"),
}


class Entity(BaseModel):
    name: str
    role: str
    evidence_url: str | None = None


class _Extracted(BaseModel):
    name: str
    role: Role
    evidence_url: str
    evidence_quote: str


class _Entities(BaseModel):
    entities: list[_Extracted]


SYSTEM = """당신은 데이터셋 문서에서 제재 대조 대상이 되는 기관·회사 이름을 뽑는 분석가입니다.
문서(<document> 태그)에 적힌 이름만, 적힌 표기 그대로 냅니다. 추측하거나 번역하지 않습니다.
역할: 제작자, 제공자, 모회사·자금 제공자, 공동 제작 기관, 원본 제공자(크롤링 대상 기관·상위 데이터셋 제작 기관), 생성 모델 개발사.
evidence_url은 이름이 실린 문서의 url 속성 그대로, evidence_quote는 그 문서의 원문을 글자 그대로 짧게 인용합니다.
'web'·'users' 같은 일반어와 개인 이름은 제외합니다."""


def _flat(v) -> list[str]:
    if v in (None, "", [], {}):
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [x for k, val in v.items() if k not in {"hub_profile", "profile"} for x in _flat(val)]
    if isinstance(v, (list, tuple)):
        return [x for i in v for x in _flat(i)]
    return [str(v)]


def baseline_entities(card: DataCard) -> list[Entity]:
    """LLM 없이도 얻을 수 있는 이름: 제공자 항목과 저장소 소유자."""
    out: list[Entity] = []
    prov = card.fields["provider"]
    if isinstance(prov.value, dict):
        for key in ("name", "uploader"):
            if prov.value.get(key):
                out.append(Entity(name=str(prov.value[key]), role="제공자", evidence_url=prov.evidence_url))
    owner = card.dataset.repo.split("/")[0] if "/" in card.dataset.repo and card.dataset.platform != Platform.web else None
    if owner:
        out.append(Entity(name=owner, role="제공자", evidence_url=card.dataset.url))
    return _dedupe(out)


def _dedupe(entities: list[Entity]) -> list[Entity]:
    seen, out = set(), []
    for e in entities:
        k = (norm(e.name), e.role in DIRECT_ROLES)
        if norm(e.name) and k not in seen:
            seen.add(k)
            out.append(e)
    return out


def llm_entities(card: DataCard, client: anthropic.Anthropic) -> list[Entity]:
    docs = "\n".join(f'<document url="{s.url}">\n{s.content[:20000]}\n</document>' for s in card.sources if s.content)
    fields = "\n".join(
        f"{k}: {' '.join(_flat(card.fields[k].value))}"
        for k in ("provider", "provider_origin", "creation", "upstream_sources") if not card.fields[k].is_missing
    )
    if not docs and not fields:
        return []
    response = client.beta.messages.parse(
        model=MODEL, max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": f"{docs}\n\n<card>\n{fields}\n</card>\n\n제재 대조 대상 이름을 모두 뽑아 주세요."}],
        output_format=_Entities,
    )
    if response.parsed_output is None:
        return []
    corpus = norm(docs + " " + fields)
    out = []
    for e in response.parsed_output.entities:
        # 문서에 실제로 없는 이름은 버린다 (환각 방지)
        if norm(e.name) and norm(e.name) in corpus:
            out.append(Entity(name=e.name, role=e.role, evidence_url=e.evidence_url))
    return out


def concern_countries(card: DataCard) -> list[tuple[str, str]]:
    """(국가, 근거 문구) — 소재국·소속·자금 출처 항목에서 우려국 언급을 찾는다."""
    texts = {k: " ".join(_flat(card.fields[k].value)) for k in ("provider_origin", "provider")}
    found = []
    for country, words in CONCERN_COUNTRIES.items():
        for key, text in texts.items():
            for w in words:
                m = re.search(re.escape(w), text, re.I)
                if m:
                    start = max(0, m.start() - 30)
                    found.append((country, text[start:m.end() + 30].strip()))
                    break
            else:
                continue
            break
    return found


def match_level(entity: Entity, m: Match) -> Level:
    """직접 등재(제작·제공 주체 정확 일치)는 높음, 간접 연결·유사 이름은 중간."""
    return Level.high if (m.kind == "exact" and entity.role in DIRECT_ROLES) else Level.medium


def _match_finding(entity: Entity, m: Match) -> Finding:
    e = m.entry
    direct = entity.role in DIRECT_ROLES
    level = match_level(entity, m)
    kind = "이름이 정확히 일치" if m.kind == "exact" else f"이름이 유사(유사도 {m.score})"
    where = "데이터셋 제작·제공 주체" if direct else f"{entity.role}"
    return Finding(
        check=f"제재 목록 대조: {e.list_name}",
        level=level,
        note=(f"'{entity.name}'({where})이(가) '{m.matched_name}'과(와) {kind}한다. 프로그램: {e.program or '표시 없음'}."
              + (f" 상세: {e.detail}" if e.detail else "")
              + " 동명이인·동명 기관일 수 있어 주소·별칭으로 같은 대상인지 사람이 확인해야 한다."),
        evidence_url=e.source_url or None,
        recommendation="같은 대상인지 확인하고, 맞다면 해당 데이터셋 사용을 중단하고 법무·컴플라이언스에 보고하세요.",
    )


def opinion(card: DataCard, index: Index, entities: list[Entity], news: list[Finding] | None = None) -> AreaOpinion:
    """news: 언론 보도 점검 결과(None이면 뉴스를 검색하지 않은 것)."""
    findings: list[Finding] = []
    loaded = index.loaded
    failed = [s for s in index.statuses if not s.ok]
    entities = _dedupe(entities)

    # 대조 범위
    if not loaded:
        findings.append(Finding(check="제재 목록 대조 범위", level=Level.unknown,
            note="제재 목록을 하나도 불러오지 못해 대조하지 못했다." + (" 실패: " + "; ".join(f"{s.list_name}({s.error})" for s in failed) if failed else ""),
            recommendation="잠시 뒤 다시 분석하거나 법무에서 직접 대조하세요."))
    else:
        covered = ", ".join(f"{s.list_name} {s.count:,}건" for s in loaded)
        findings.append(Finding(check="제재 목록 대조 범위", level=Level.low, note="대조한 목록: " + covered))
        for s in failed:
            findings.append(Finding(check="제재 목록 대조 범위", level=Level.unknown,
                note=f"{s.list_name}을(를) 불러오지 못해 대조하지 못했다 ({s.error}).", recommendation="다시 분석하거나 법무에서 직접 대조하세요."))
    if not any(s.list_id.startswith("manual:") for s in loaded):
        findings.append(Finding(check="수동 목록 대조", level=Level.unknown,
            note="미 국방부 1260H, UFLPA Entity List, FCC Covered List, 한국 정부 제재 명단은 내려받을 수 있는 공개 데이터가 없어 수동 목록(data/watchlists)이 비어 있는 동안 대조하지 못한다.",
            recommendation="법무에서 해당 목록을 data/watchlists에 정리해 넣어 주세요."))

    # 대상 이름
    if not entities:
        findings.append(Finding(check="대조 대상 기관", level=Level.unknown,
            note="제작자·제공자 등 대조할 기관 이름을 찾지 못했다.", recommendation="제공자 정보를 카드에서 직접 입력해 주세요."))
    hits: list[tuple[Entity, Match]] = []
    if loaded:
        for ent in entities:
            for m in index.match(ent.name):
                hits.append((ent, m))
                findings.append(_match_finding(ent, m))
        if entities and not hits:
            findings.append(Finding(check="제재 목록 일치", level=Level.low,
                note="대조한 이름 " + ", ".join(f"{e.name}({e.role})" for e in entities[:8]) + "이(가) 불러온 목록에서 일치하지 않았다. 이름 대조만으로는 모회사·자금 관계를 찾지 못한다."))

    # 우려국 연관
    for country, quote in concern_countries(card):
        prov = card.fields["provider_origin"] if not card.fields["provider_origin"].is_missing else card.fields["provider"]
        findings.append(Finding(check=f"{country} 연관", level=Level.unknown,
            note=f"제공자 정보에 {country}가 언급된다: \"{quote}\". 소재국만으로 제재 대상이 되는 것은 아니며 평판 관점의 참고 신호다.",
            evidence_url=prov.evidence_url, evidence_quote=quote,
            recommendation="모회사·자금 제공 관계와 해당 국가 규제(수출통제·데이터 국외 이전)를 확인하세요."))

    findings += news or []
    level = worst([f.level for f in findings])
    recs = list(dict.fromkeys(f.recommendation for f in findings if f.recommendation and f.level != Level.low))
    return AreaOpinion(
        area="평판(제재)", level=level, findings=findings, recommendations=recs,
        summary=_summary(level, hits, bool(loaded)),
        where="데이터셋 제작·제공 주체와 확인된 관련 기관",
        jurisdictions=_jurisdictions(index, hits, bool(entities)),
        not_checked=([] if news is not None else ["언론 보도·부정 기사 검색"]) + ["의회 법안·행정명령 등 정책 동향", "감시·군사 기관 연관, 인권 이슈 지역 수집 여부",
                     "미국 대량 민감 개인정보 이전 제한(DOJ 규칙)·AI 분야 대중 투자 규제 해당 여부", "모회사·자금 제공자 조사 (제작 문서에 적힌 경우만 대조)"]
        + (["최근 3개월을 넘는 과거 보도, 본문에만 나오는 보도"] if news is not None else []),
    )


def _summary(level: Level, hits: list[tuple[Entity, Match]], loaded: bool) -> str:
    if hits:
        e, m = hits[0]
        return f"'{e.name}'이(가) {m.entry.list_name}의 '{m.matched_name}'과(와) {'정확히 일치' if m.kind == 'exact' else '유사'}한다. 같은 대상인지 확인이 필요하다."
    if not loaded:
        return "제재 목록을 불러오지 못해 대조하지 못했다."
    if level == Level.low:
        return "대조한 목록에서 일치하는 기관이 없다."
    return "목록 일치는 없지만 대조하지 못한 범위가 남아 있다."


def _jurisdictions(index: Index, hits: list[tuple[Entity, Match]], has_entities: bool) -> list[JurisdictionOpinion]:
    notes = {
        "미국": "OFAC 제재와 BIS 수출통제 목록 등재 기관과의 거래·데이터 이용은 미국 규제 리스크로 이어질 수 있다.",
        "EU": "EU 제재 대상과의 거래·자금 제공은 EU 영역에서 금지·제한된다.",
        "한국": "한국 정부 제재 명단은 공개 데이터가 없어 수동 목록으로만 대조한다.",
    }
    out = []
    for j, base in notes.items():
        statuses = [s for s in index.statuses if s.jurisdiction == j and s.ok]
        jh = [match_level(e, m) for e, m in hits if m.entry.jurisdiction == j]
        if jh:
            level = worst(jh)
        elif statuses and has_entities:
            level = Level.low
        else:
            level = Level.unknown
        out.append(JurisdictionOpinion(jurisdiction=j, level=level, note=base + ("" if statuses else " 이 법역의 목록은 아직 대조하지 못했다.")))
    return out
