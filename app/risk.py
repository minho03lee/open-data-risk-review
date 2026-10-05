"""라이선스·개인정보 리스크 분석 (기획안 7-1, 7-2장).

규칙으로 판정할 수 있는 부분만 다룬다: 데이터 카드 항목 → 점검 결과 → 영역 등급.
근거를 찾지 못하면 '낮음'이 아니라 '확인 필요'로 두고(기획안 10장), 카드 항목이 '추정'이면
'낮음'을 주지 않는다. 법역별(한국·미국·EU) 문구는 초안이며 법무 검토가 필요하다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .licenses import flags, to_spdx
from .models import CardField, DataCard, FieldStatus, Platform


class Level(str, Enum):
    high = "높음"
    medium = "중간"
    low = "낮음"
    unknown = "확인 필요"


# 종합 등급은 가장 높은 영역을 따른다. 확인 필요는 낮음보다 위에 둔다(보수적 기본값).
_RANK = {Level.low: 0, Level.unknown: 1, Level.medium: 2, Level.high: 3}


def worst(levels: list[Level]) -> Level:
    return max(levels, key=_RANK.__getitem__) if levels else Level.unknown


class Finding(BaseModel):
    check: str
    level: Level
    note: str
    evidence_url: str | None = None
    evidence_quote: str | None = None
    recommendation: str | None = None


class JurisdictionOpinion(BaseModel):
    jurisdiction: str
    level: Level
    note: str


class AreaOpinion(BaseModel):
    area: str
    level: Level
    summary: str
    findings: list[Finding]
    where: str = "데이터셋 자체"
    recommendations: list[str] = Field(default_factory=list)
    jurisdictions: list[JurisdictionOpinion] = Field(default_factory=list)
    not_checked: list[str] = Field(default_factory=list)


class RiskReport(BaseModel):
    id: str | None = None
    datacard_id: str
    dataset_uid: str
    overall: Level
    areas: list[AreaOpinion]
    scope_note: str = (
        "이번 분석 범위: 데이터셋 자체의 라이선스·개인정보. 평판·기타 영역과 원본 출처 계보는 아직 분석하지 않았다. "
        "상용 모델 학습 기준의 참고용 분석이며 법률 자문이 아니다. 법역별 문구는 초안으로 법무 검토가 필요하다."
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- 카드 항목 읽기 -------------------------------------------------------------

def _text(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_text(v)}" for k, v in value.items() if _text(v))
    if isinstance(value, (list, tuple)):
        return ", ".join(t for t in map(_text, value) if t)
    return str(value)


def _has(text: str, *words: str) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


def _ev(field: CardField) -> dict:
    return {"evidence_url": field.evidence_url, "evidence_quote": field.evidence_quote}


def _soften(level: Level, field: CardField) -> tuple[Level, str]:
    """근거가 '추정'이면 낮음을 줄 수 없다."""
    if level == Level.low and field.status == FieldStatus.inferred:
        return Level.unknown, " (카드 항목이 '추정'이라 근거 문서에서 직접 확인이 필요하다)"
    return level, ""


# --- 라이선스 ------------------------------------------------------------------

def _license_ids(field: CardField) -> tuple[list[str], list[str]]:
    """(SPDX 식별자 목록, 커스텀·미인식 표기 목록)."""
    v = field.value
    if isinstance(v, dict):
        spdx = [s for s in v.get("spdx") or [] if s]
        custom = [c for c in v.get("custom") or [] if c]
        if not spdx and not custom:
            custom = [c for c in v.get("raw") or [] if c]
        return spdx, custom
    # LLM이 문자열로 채운 경우: 알려진 표기만 SPDX로, 나머지는 원문 그대로 커스텀으로 본다
    text = _text(v)
    found = to_spdx(text)
    return ([found], []) if found else ([], [text])


def license_opinion(card: DataCard) -> AreaOpinion:
    f = card.fields
    lic = f["license"]
    findings: list[Finding] = []
    not_checked = ["원본 저작물의 저작권 상태와 계보 내 라이선스 충돌 (계보 분석 단계)", "저작권 소송·철회 보도 검색 (평판·기타 단계)"]

    spdx, custom = ([], []) if lic.is_missing else _license_ids(lic)

    # 1. 명시 여부
    if lic.is_missing:
        structured = card.dataset.platform in (Platform.huggingface, Platform.kaggle)
        findings.append(Finding(
            check="라이선스 명시 여부",
            level=Level.high if structured else Level.unknown,
            note="라이선스 표기를 찾지 못했다. 표기가 없으면 권리 유보로 보는 것이 안전하다."
            if structured else "공개 페이지에서 라이선스를 찾지 못했다. 신청·로그인 뒤 약관에 있을 수 있다.",
            recommendation="제공자에게 라이선스를 문의하거나 보유한 라이선스·약관 문서를 업로드해 주세요.",
        ))
    else:
        ev = _ev(lic)
        shown = ", ".join(spdx + custom)
        findings.append(Finding(check="라이선스 명시 여부", level=Level.low, note=f"표기: {shown}", **ev))

        # 2~4. 상업적 이용·파생·동일조건변경허락 (SPDX를 아는 경우)
        for sid in spdx:
            fl = flags(sid)
            if fl["commercial"] is False:
                findings.append(Finding(check="상업적 이용 허용", level=Level.high, **ev,
                    note=f"{sid}는 비영리(NC) 조건이라 상용 모델 학습에 쓰기 어렵다.",
                    recommendation="상업적 이용이 허용되는 대체 데이터셋을 찾거나 권리자에게 별도 허락을 받으세요."))
            elif fl["commercial"] is True:
                findings.append(Finding(check="상업적 이용 허용", level=Level.low, note=f"{sid}는 상업적 이용을 허용한다.", **ev))
            else:
                custom.append(sid)
            if fl["derivatives"] is False:
                findings.append(Finding(check="변경·파생 허용", level=Level.high, **ev,
                    note=f"{sid}는 변경금지(ND) 조건이라 학습·가공이 파생물 작성에 해당하는지 다툼의 소지가 있다.",
                    recommendation="법무 검토 전에는 사용을 보류하세요."))
            if fl["share_alike"]:
                findings.append(Finding(check="의무 조건(SA)", level=Level.medium, **ev,
                    note=f"{sid}는 동일조건변경허락(SA) 조건이라 학습된 모델·출력물에 의무가 미치는지 불분명하다.",
                    recommendation="모델·출력물을 같은 조건으로 공개해야 하는지 법무 확인을 받으세요."))
            if sid.upper().startswith("CC-BY") or sid.upper() in {"ODC-BY-1.0", "APACHE-2.0", "MIT"}:
                findings.append(Finding(check="표시 의무", level=Level.low, **ev,
                    note=f"{sid}는 출처 표시 의무가 있다. 배포·공개 시 표시 방법을 정해 두어야 한다."))

        if custom:
            findings.append(Finding(
                check="커스텀·미분류 라이선스", level=Level.unknown, **ev,
                note="표준 식별자로 분류되지 않는 라이선스: " + ", ".join(dict.fromkeys(custom)) + ". 상업적 이용·파생 조건을 조항에서 직접 확인해야 한다.",
                recommendation="라이선스 원문을 법무에서 검토하세요. 원문이 카드에 없으면 업로드해 주세요.",
            ))
        if len(spdx) + len(custom) > 1:
            findings.append(Finding(check="복수 라이선스", level=Level.medium, **ev,
                note="라이선스가 여러 개 표기되어 있다. 선택 가능한 것인지(OR) 모두 적용되는지(AND) 문서로 확인해야 한다.",
                recommendation="어느 쪽으로 해석하든 가장 제한적인 조건을 기준으로 삼으세요."))

    # 5. 추가 이용조건·약관 (게이트 동의 문구 포함)
    terms, access = f["extra_terms"], f["access"]
    if not terms.is_missing:
        findings.append(Finding(check="약관·사용 제한", level=Level.medium, **_ev(terms),
            note="라이선스 외 추가 이용조건이 있다: " + _text(terms.value)[:200],
            recommendation="약관의 사용 제한(AUP)과 AI 학습 금지 조항 여부를 법무에서 확인하세요."))
    gate = access.value.get("gate_prompt") if isinstance(access.value, dict) else None
    if gate:
        findings.append(Finding(check="약관·사용 제한", level=Level.medium, **_ev(access),
            note="접근 시 동의해야 하는 조건이 있다: " + _text(gate)[:200],
            recommendation="동의 문구가 상업적 이용·재배포를 제한하는지 확인하세요."))
    elif terms.is_missing:
        not_checked.append("플랫폼·제공자 이용약관의 AI 학습 금지 조항")

    # 6. 원본 출처 (계보는 다음 단계, 여기서는 존재 여부만)
    up = f["upstream_sources"]
    if up.is_missing:
        findings.append(Finding(check="원본 출처 공개 여부", level=Level.unknown,
            note="원본 출처 정보를 찾지 못했다. 계보가 끊기면 데이터셋 라이선스만으로 안전하다고 볼 수 없다.",
            recommendation="논문·제작 문서에서 원본 출처를 확인해 주세요."))
    else:
        findings.append(Finding(check="원본 출처 계보", level=Level.unknown, **_ev(up),
            note="원본 출처가 있다(" + _text(up.value)[:160] + "). 데이터셋 라이선스가 원본보다 넓지 않은지는 계보 분석 전이라 확인하지 못했다.",
            recommendation="원본 출처별 라이선스와 약관을 확인하세요."))

    # 7. 분쟁 이력 (카드에 있을 때만)
    issues = f["known_issues"]
    if not issues.is_missing:
        text = _text(issues.value)
        severe = _has(text, "소송", "lawsuit", "철회", "retract", "삭제", "removed", "takedown", "disabled", "비활성")
        findings.append(Finding(check="분쟁 이력", level=Level.high if severe else Level.medium, **_ev(issues),
            note="알려진 이슈: " + text[:200],
            recommendation="철회·삭제된 데이터가 포함돼 있지 않은지, 대체 버전이 있는지 확인하세요."))
    else:
        not_checked.append("철회·소송 이력 (카드에 기록된 이슈 없음, 검색은 하지 않음)")

    # 근거가 추정이면 '낮음'을 올린다
    adjusted = []
    for fd in findings:
        if fd.level == Level.low and fd.check in {"라이선스 명시 여부", "상업적 이용 허용"}:
            lvl, extra = _soften(fd.level, lic)
            fd = fd.model_copy(update={"level": lvl, "note": fd.note + extra})
        adjusted.append(fd)
    findings = adjusted

    level = worst([x.level for x in findings])
    return AreaOpinion(
        area="라이선스", level=level, findings=findings, not_checked=not_checked,
        summary=_license_summary(level, findings),
        recommendations=list(dict.fromkeys(x.recommendation for x in findings if x.recommendation and x.level != Level.low)),
        jurisdictions=_license_jurisdictions(level, card),
    )


def _license_summary(level: Level, findings: list[Finding]) -> str:
    top = [x for x in findings if x.level == level and level != Level.low]
    if not top:
        return "표기된 라이선스에서 상업적 이용·파생을 막는 조건은 보이지 않는다."
    return " ".join(dict.fromkeys(x.note.split(".")[0] + "." for x in top[:2]))


def _license_jurisdictions(level: Level, card: DataCard) -> list[JurisdictionOpinion]:
    crawl = _has(_text(card.fields["creation"].value) + _text(card.fields["upstream_sources"].value), "crawl", "크롤", "scrap", "common crawl", "웹")
    return [
        JurisdictionOpinion(jurisdiction="한국", level=level,
            note="저작권법상 AI 학습을 위한 이용(TDM)이 허용되는 범위가 불명확하다. 라이선스 조건과 별개로 원본 저작물 권리자 문제가 남을 수 있어 법무 확인이 필요하다."),
        JurisdictionOpinion(jurisdiction="미국", level=level,
            note="학습의 공정 이용(fair use) 인정 여부는 사안별로 판단되고 판결이 계속 나오는 중이다. 라이선스 위반은 계약·저작권 침해 양쪽 쟁점이 될 수 있다."),
        JurisdictionOpinion(jurisdiction="EU", level=level,
            note="DSM 지침의 TDM 예외는 권리자의 옵트아웃(기계가 읽을 수 있는 방식)을 존중하는 것이 전제다."
            + (" 웹 크롤링 출처라 robots.txt·옵트아웃 표시를 확인해야 한다." if crawl else "")),
    ]


# --- 개인정보 ------------------------------------------------------------------

_SENSITIVE = ("건강", "의료", "생체", "지문", "홍채", "아동", "미성년", "종교", "정치", "성적", "범죄", "인종", "유전",
              "health", "medical", "biometric", "child", "minor", "religio", "political", "sexual", "ethnic", "genetic")
_BIOMETRIC = ("얼굴", "음성", "목소리", "생체", "지문", "홍채", "face", "voice", "biometric", "speaker")
_MEDIA_TYPES = ("이미지", "오디오", "비디오", "멀티모달")


def _pii_state(field: CardField) -> str:
    """'포함' | '가능성' | '미포함' | '불명'."""
    t = _text(field.value)
    if field.is_missing or not t:
        return "불명"
    if _has(t, "미포함", "포함하지 않", "없음", "no pii", "not contain"):
        return "미포함"
    if _has(t, "가능성", "possible", "may contain", "might"):
        return "가능성"
    if _has(t, "확인 불가", "불명", "unknown"):
        return "불명"
    return "포함"


def privacy_opinion(card: DataCard) -> AreaOpinion:
    f = card.fields
    pii, types, deid = f["pii_included"], f["pii_types"], f["deidentification_consent"]
    state = _pii_state(pii)
    type_text = _text(types.value)
    media = _has(_text(f["data_type"].value), *_MEDIA_TYPES)
    findings: list[Finding] = []
    not_checked = ["공개 샘플 대상 개인정보 탐지(PII 패턴·얼굴 검출) (운영 단계)", "원본 출처의 개인정보 (계보 분석 단계)"]

    # 1. 포함 여부
    base = {"포함": Level.high, "가능성": Level.medium, "미포함": Level.low, "불명": Level.unknown}[state]
    lvl, extra = _soften(base, pii)
    notes = {
        "포함": "개인정보가 포함된 것으로 확인되었다.",
        "가능성": "개인정보가 포함되었을 가능성이 있다.",
        "미포함": "개인정보가 없다고 밝혀져 있다.",
        "불명": "개인정보 포함 여부를 확인하지 못했다.",
    }
    note = notes[state] + extra
    rec = None
    if state == "불명" and media:
        note += " 이미지·음성 데이터는 얼굴·목소리가 들어 있을 수 있어 확인이 특히 필요하다."
    if lvl != Level.low:
        rec = "제작자의 데이터시트·논문에서 개인정보 처리 방식을 확인하고, 필요하면 문서를 업로드해 주세요."
    findings.append(Finding(check="개인정보 포함", level=lvl, note=note, recommendation=rec, **_ev(pii)))

    # 2. 민감정보·생체정보
    if type_text:
        sensitive = _has(type_text, *_SENSITIVE)
        findings.append(Finding(
            check="민감정보", level=Level.high if (sensitive or state == "포함") else Level.medium,
            note=("민감정보·생체정보가 포함될 수 있다: " if sensitive else "포함된 개인정보 종류: ") + type_text[:200],
            recommendation="민감정보는 별도 동의·보호 조치가 필요하므로 사용 전에 법무 확인을 받으세요." if sensitive else None,
            **_ev(types)))
    elif state in ("포함", "가능성"):
        findings.append(Finding(check="민감정보", level=Level.unknown,
            note="개인정보 종류가 기록되지 않아 민감정보 포함 여부를 판단할 수 없다.",
            recommendation="포함된 개인정보의 종류를 확인해 주세요."))

    # 3. 수집 근거와 비식별 조치·동의
    creation = _text(f["creation"].value) + _text(f["upstream_sources"].value)
    crawled = _has(creation, "crawl", "크롤", "scrap", "스크래", "common crawl", "웹에서")
    deid_text = _text(deid.value)
    if deid_text:
        done = _has(deid_text, "비식별", "익명", "가명", "anonymi", "pseudonym", "동의 획득", "동의를 받", "informed consent", "consent obtained")
        findings.append(Finding(check="비식별 조치·동의", level=Level.low if done else Level.medium,
            note="제작자가 밝힌 내용: " + deid_text[:200] + ("" if done else " (비식별 조치나 동의 획득이 명확하지 않다)"),
            recommendation=None if done else "동의 범위와 비식별 방법을 제작자 문서로 확인하세요.", **_ev(deid)))
        # 비식별 조치가 있어도 '추정' 근거면 낮음을 주지 않는다
        if done and deid.status == FieldStatus.inferred:
            findings[-1] = findings[-1].model_copy(update={"level": Level.unknown, "note": findings[-1].note + " (카드 항목이 '추정'이라 확인이 필요하다)"})
    elif state != "미포함":
        findings.append(Finding(
            check="비식별 조치·동의", level=Level.medium if crawled else Level.unknown,
            note=("공개 웹에서 수집된 데이터인데 " if crawled else "") + "비식별 조치와 정보주체 동의 여부가 확인되지 않았다.",
            recommendation="제작자가 밝힌 비식별화 방법과 동의 절차를 확인하세요."))

    # 4. 삭제·옵트아웃 채널
    channel = _has(deid_text + _text(f["provider"].value), "삭제 요청", "opt-out", "opt out", "removal request", "takedown", "삭제 채널")
    if state != "미포함":
        findings.append(Finding(
            check="삭제·옵트아웃", level=Level.low if channel else Level.unknown,
            note="정보주체 삭제 요청 채널이 확인된다." if channel else "정보주체 삭제 요청 채널을 찾지 못했다.",
            recommendation=None if channel else "제공자의 삭제 요청 절차와 반영 방식을 확인하세요."))

    level = worst([x.level for x in findings])
    biometric = media and _has(type_text + _text(f["data_type"].value) + _text(f["description"].value), *_BIOMETRIC)
    return AreaOpinion(
        area="개인정보", level=level, findings=findings, not_checked=not_checked,
        summary=_privacy_summary(level, state, findings),
        recommendations=list(dict.fromkeys(x.recommendation for x in findings if x.recommendation and x.level != Level.low)),
        jurisdictions=_privacy_jurisdictions(level, state, biometric),
    )


def _privacy_summary(level: Level, state: str, findings: list[Finding]) -> str:
    if level == Level.low:
        return "개인정보가 없다고 밝혀져 있고 근거도 확인된다."
    top = [x for x in findings if x.level == level]
    return " ".join(dict.fromkeys(x.note.split(". ")[0].rstrip(".") + "." for x in top[:2]))


def _privacy_jurisdictions(level: Level, state: str, biometric: bool) -> list[JurisdictionOpinion]:
    if state == "미포함":
        base = "개인정보가 없다고 밝혀져 있어 별도 쟁점은 낮다. 다만 재식별 가능성은 샘플로 확인되지 않았다."
        return [JurisdictionOpinion(jurisdiction=j, level=level, note=base) for j in ("한국", "미국", "EU")]
    return [
        JurisdictionOpinion(jurisdiction="한국", level=level,
            note="개인정보 보호법상 공개된 개인정보라도 수집 목적 범위를 넘는 이용과 제3자 제공이 제한된다. 민감정보·고유식별정보는 별도로 규율되므로 법무 확인이 필요하다."),
        JurisdictionOpinion(jurisdiction="미국", level=Level.high if biometric and _RANK[level] >= _RANK[Level.unknown] else level,
            note=("얼굴·음성 등 생체정보는 일리노이 BIPA 같은 주별 생체정보법의 대상일 수 있다. " if biometric else "연방 단일 개인정보법은 없어 주별 법(예: 생체정보·아동 개인정보)을 확인해야 한다. ")
            + "정보주체의 소재지에 따라 적용 법이 달라진다."),
        JurisdictionOpinion(jurisdiction="EU", level=level,
            note="정보주체가 EU 거주자일 수 있으면 GDPR이 적용된다. 학습 목적의 적법 근거와 정보주체 권리(삭제·반대) 대응 방안을 확인해야 한다."),
    ]


# --- 종합 ----------------------------------------------------------------------

def analyze(card: DataCard) -> RiskReport:
    areas = [license_opinion(card), privacy_opinion(card)]
    return RiskReport(
        datacard_id=card.id or "", dataset_uid=card.dataset.uid,
        overall=worst([a.level for a in areas]), areas=areas,
    )
