"""기타 리스크: 유해 콘텐츠·규제 준수·중국 측 규제·지속성·품질·편향 (기획안 7-4장).

데이터 카드와 수집한 문서 본문에서 신호를 찾는 규칙 기반 점검이다. 문서에 근거가 없으면 '낮음'이 아니라 '확인 필요'로 두고,
찾은 신호는 문서 원문 문구를 그대로 인용해 보여 준다. 법역별 문구는 초안으로 법무 검토가 필요하다.
"""
from __future__ import annotations

import re

from .models import DataCard, Platform
from .reputation import concern_countries
from .risk import AreaOpinion, Finding, JurisdictionOpinion, Level, worst

_CSAM = ("csam", "child sexual", "child abuse material", "아동 성착취", "아동성착취", "아동 음란", "아동·청소년 성")
_HARM = ("nsfw", "not safe for work", "pornograph", "sexually explicit", "explicit content", "hate speech", "toxic", "violent content",
         "offensive", "obscene", "혐오", "폭력적", "음란", "선정적", "유해")
# 일반적인 'filtered'·'removed'는 쓰지 않는다. 유해성 대응으로 읽히는 표현만 안전 조치로 본다.
_SAFETY = ("bad words", "badwords", "dirty, naughty", "profanity filter", "safety filter", "content moderation", "nsfw filter",
           "toxicity filter", "유해 콘텐츠 제거", "유해 콘텐츠 필터", "욕설 필터", "비속어")
_QUALITY = ("label error", "mislabel", "noisy label", "annotation error", "label noise", "bias", "biased", "stereotype", "imbalanc",
            "limitation", "known issue", "lower quality", "라벨 오류", "편향", "한계", "불균형", "노이즈", "품질 문제")
_WITHDRAWN = ("retract", "withdrawn", "taken down", "takedown", "disabled", "철회", "삭제됨", "비공개 전환", "비활성")
_CRAWL = ("crawl", "크롤", "scrap", "스크래", "common crawl", "웹에서")


def _texts(card: DataCard) -> list[tuple[str, str]]:
    """(근거 URL, 본문) — 수집한 문서와 카드의 글 항목."""
    out = [(s.url, s.content) for s in card.sources if s.content]
    for key in ("known_issues", "description", "creation", "overview", "extra_terms"):
        f = card.fields[key]
        if not f.is_missing:
            out.append((f.evidence_url or card.dataset.url or "", _flat(f.value)))
    return out


def _flat(v) -> str:
    if v in (None, "", [], {}):
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return " ".join(_flat(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return " ".join(_flat(x) for x in v)
    return str(v)


def find_snippet(card: DataCard, words: tuple[str, ...]) -> tuple[str, str, str] | None:
    """(근거 URL, 일치한 단어, 원문 인용). 문서 원문에서 그대로 잘라 낸 문구라 인용이 검증된다."""
    for url, text in _texts(card):
        low = text.lower()
        for w in words:
            i = low.find(w.lower())
            if i >= 0:
                a, b = max(0, i - 70), min(len(text), i + len(w) + 70)
                return url, w, re.sub(r"\s+", " ", text[a:b]).strip()
    return None


def _has_crawl(card: DataCard) -> bool:
    joined = " ".join(t for _, t in _texts(card)).lower()
    return any(w in joined for w in _CRAWL)


def other_opinion(card: DataCard) -> AreaOpinion:
    f = card.fields
    findings: list[Finding] = []
    not_checked = ["유해 콘텐츠·소송·언론 보도 검색 (뉴스 단계)", "데이터 샘플에 대한 유해 콘텐츠·품질 탐지", "라벨 오류·편향의 정량 평가"]

    # 1. 유해 콘텐츠
    csam = find_snippet(card, _CSAM)
    harm = find_snippet(card, _HARM)
    safety = find_snippet(card, _SAFETY)
    if csam:
        url, word, quote = csam
        findings.append(Finding(check="유해 콘텐츠", level=Level.high, evidence_url=url, evidence_quote=quote,
            note=f"문서에 아동 성착취 관련 언급('{word}')이 있다. 포함 보고 이력인지 문서 문맥을 확인해야 한다.",
            recommendation="해당 데이터가 포함·보고된 이력인지 확인하고, 확인되면 사용하지 말고 법무에 즉시 보고하세요."))
    elif harm:
        url, word, quote = harm
        mitigated = " 제작자가 필터링 조치('" + safety[1] + "')를 밝혔지만 효과는 검증되지 않아 잔존 가능성이 있다." if safety else ""
        findings.append(Finding(check="유해 콘텐츠", level=Level.medium, evidence_url=url, evidence_quote=quote,
            note=f"문서에 유해 콘텐츠 관련 언급('{word}')이 있다. 포함 가능성과 제거 조치 여부를 확인해야 한다." + mitigated,
            recommendation="유해 콘텐츠 필터링·제거 방식과 잔존 비율을 제작자 문서로 확인하세요."))
    elif safety:
        url, word, quote = safety
        findings.append(Finding(check="유해 콘텐츠", level=Level.low, evidence_url=url, evidence_quote=quote,
            note=f"제작자가 유해성 대응 조치('{word}')를 밝혔다. 효과는 검증되지 않았다."))
    else:
        findings.append(Finding(check="유해 콘텐츠", level=Level.unknown,
            note="유해 콘텐츠 포함 여부와 필터링 조치에 대한 기록을 문서에서 찾지 못했다." + (" 웹 수집 데이터는 유해 콘텐츠가 섞일 가능성이 있다." if _has_crawl(card) else ""),
            recommendation="제작자의 데이터시트·논문에서 안전성 검토 내용을 확인하세요."))

    # 2. 규제 준수: EU AI Act 학습 데이터 요약, TDM 예외
    provenance = not f["upstream_sources"].is_missing or not f["creation"].is_missing
    findings.append(Finding(
        check="EU AI Act 학습 데이터 요약", level=Level.low if provenance else Level.unknown,
        note="범용 AI 모델을 EU에 제공하면 학습에 쓴 데이터의 요약을 공개해야 한다." + (
            " 이 데이터셋은 제작·출처 정보가 있어 요약 작성에 쓸 수 있다." if provenance else " 이 데이터셋은 제작·출처 정보가 부족해 요약을 쓰기 어렵다."),
        recommendation=None if provenance else "제작 과정과 원본 출처를 확인해 요약에 쓸 수 있게 정리하세요."))
    if _has_crawl(card):
        findings.append(Finding(
            check="TDM 예외·옵트아웃", level=Level.unknown,
            note="웹 수집 데이터라 텍스트·데이터 마이닝 예외와 권리자의 옵트아웃(robots.txt 등) 준수 여부를 확인해야 한다. 국가별로 예외의 범위가 다르다.",
            recommendation="수집 시점의 옵트아웃 존중 여부를 제작자 문서와 법무 검토로 확인하세요."))

    # 3. 중국 측 규제
    cn = [q for c, q in concern_countries(card) if c == "중국"]
    if cn:
        prov = f["provider_origin"] if not f["provider_origin"].is_missing else f["provider"]
        findings.append(Finding(
            check="중국 측 규제", level=Level.unknown, evidence_url=prov.evidence_url, evidence_quote=cn[0],
            note=f"제공자 정보에 중국이 언급된다: \"{cn[0]}\". 중국 기관이 만든 데이터는 데이터의 국외 이전 제한과 수출통제 해당 여부를 확인해야 할 수 있다.",
            recommendation="데이터 반출·이용 경로와 해당 규제 적용 여부를 법무에서 확인하세요."))
    else:
        not_checked.append("중국 기관 데이터의 국외 이전 제한·수출통제 (제공자에 중국 언급이 없어 점검하지 않음)")

    # 4. 지속성: 철회·비공개 전환, 버전 고정
    issues = f["known_issues"]
    withdrawn = find_snippet(card, _WITHDRAWN) if not issues.is_missing else None
    if withdrawn:
        url, word, quote = withdrawn
        findings.append(Finding(check="지속성: 철회·비공개", level=Level.high, evidence_url=url, evidence_quote=quote,
            note=f"철회·비공개 전환 관련 기록('{word}')이 있다. 현재 받을 수 있는 데이터가 검토 대상과 같은지 확인해야 한다.",
            recommendation="철회된 데이터가 포함되지 않았는지, 대체 버전이 있는지 확인하세요."))
    rev = card.dataset.revision
    if rev:
        findings.append(Finding(check="지속성: 버전 고정", level=Level.low,
            note=f"이 검토는 버전 {rev[:10]} 기준이다. 실제로 받을 때 같은 버전을 고정해야 검토 결과가 유효하다.",
            evidence_url=card.dataset.url, recommendation="다운로드·학습 시 이 버전(커밋)을 고정해 사용하세요."))
    elif card.dataset.platform in (Platform.huggingface, Platform.kaggle):
        findings.append(Finding(check="지속성: 버전 고정", level=Level.medium,
            note="검토 기준 버전이 고정되지 않았다. 데이터가 바뀌면 검토 결과와 실제 사용분이 달라질 수 있다.",
            recommendation="특정 버전을 고정해 다시 검토하세요."))
    else:
        findings.append(Finding(check="지속성: 버전 고정", level=Level.unknown,
            note="이 출처는 버전을 고정할 수 없다. 수집 시점의 페이지 스냅샷을 근거로 보관한다.",
            recommendation="검토 후 사용 직전에 약관과 데이터가 바뀌지 않았는지 다시 확인하세요."))

    # 5. 품질·편향
    q = find_snippet(card, _QUALITY)
    if q:
        url, word, quote = q
        findings.append(Finding(check="품질·편향", level=Level.medium, evidence_url=url, evidence_quote=quote,
            note=f"문서에 품질·편향 관련 언급('{word}')이 있다. 알려진 한계가 사용 목적에 영향을 주는지 확인해야 한다.",
            recommendation="문서에 적힌 한계와 편향이 학습 목적에 미치는 영향을 검토하세요."))
    else:
        findings.append(Finding(check="품질·편향", level=Level.unknown,
            note="알려진 라벨 오류·편향에 대한 기록을 문서에서 찾지 못했다. 기록이 없다고 문제가 없는 것은 아니다.",
            recommendation="제작자의 데이터시트와 후속 논문의 한계 항목을 확인하세요."))

    level = worst([x.level for x in findings])
    recs = list(dict.fromkeys(x.recommendation for x in findings if x.recommendation and x.level != Level.low))
    top = [x for x in findings if x.level == level and level != Level.low]
    summary = (" ".join(dict.fromkeys(x.note.split(". ")[0].rstrip(".") + "." for x in top[:2]))) if top else "점검한 항목에서 쟁점이 보이지 않는다."
    juris = [
        JurisdictionOpinion(jurisdiction="한국", level=level, note="아동·청소년 성착취물과 유해 콘텐츠는 관련 법령상 형사·행정 책임이 문제될 수 있다. 법무 확인이 필요하다."),
        JurisdictionOpinion(jurisdiction="미국", level=level, note="아동 성착취물은 연방법상 소지 자체가 범죄다. 그 밖의 유해 콘텐츠는 플랫폼·주별 규제와 평판 영향을 함께 봐야 한다."),
        JurisdictionOpinion(jurisdiction="EU", level=level, note="AI Act의 범용 AI 모델 학습 데이터 요약 공개 의무와 DSM 지침의 TDM 옵트아웃 존중이 쟁점이다. 불법 콘텐츠는 EU 회원국 법도 적용된다."),
    ]
    return AreaOpinion(area="기타", level=level, findings=findings, recommendations=recs, summary=summary, jurisdictions=juris, not_checked=not_checked)
