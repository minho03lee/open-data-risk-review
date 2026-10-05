"""LLM 보강: 수집한 문서에서 카드의 빈 항목을 근거 인용과 함께 채운다.

모든 주장에는 수집한 문서 URL과 원문 인용을 요구하고, 인용이 실제 문서에 없으면
'추정'으로 낮춘다 (기획안 9·10장: 근거 강제, 보수적 기본값).
"""
from __future__ import annotations

import re
from typing import Literal

import anthropic
from pydantic import BaseModel

from .models import CARD_FIELDS, CardField, DataCard, FieldStatus

MODEL = "claude-opus-5-5"

FieldKey = Literal[
    "name", "version", "overview", "description", "data_type", "size_language", "provider",
    "provider_origin", "creation", "upstream_sources", "source_url", "access", "license",
    "license_evidence", "extra_terms", "pii_included", "pii_types", "deidentification_consent",
    "related_papers", "known_issues",
]


class ExtractedField(BaseModel):
    key: FieldKey
    value: str
    evidence_url: str
    evidence_quote: str


class Extraction(BaseModel):
    fields: list[ExtractedField]
    description_summary_ko: str
    requested_documents: list[str]


SYSTEM = """당신은 AI 학습용 오픈데이터셋의 데이터 카드를 작성하는 분석가입니다.
주어진 문서(<document> 태그)에 적힌 내용만 근거로 항목을 채웁니다.

규칙:
- 각 항목의 evidence_url은 해당 내용이 실린 문서의 url 속성 값을 그대로 씁니다.
- evidence_quote는 그 문서의 원문을 글자 그대로 짧게(300자 이내) 인용합니다. 번역하거나 고쳐 쓰지 않습니다.
- 문서에 근거가 없는 항목은 아예 내지 않습니다. 추측으로 채우지 않습니다.
- value는 한국어로 간결하게 씁니다. 고유명사, 라이선스명, URL은 원문 표기를 유지합니다.
- 항목별 기준:
  - data_type: 텍스트 / 오디오 / 이미지 / 비디오 / 멀티모달 / 코드 / 표 중에서
  - provider: 기관명, 유형(기업/대학/정부/개인), 연락처
  - provider_origin: 본사 국가, 모회사, 펀딩 기관, 공동 제작 기관
  - creation: 수집·정제·라벨링 방법, GitHub·논문 링크
  - upstream_sources: 크롤링 대상 사이트, 상위 데이터셋, 합성 데이터 생성 모델
  - access: 공개 / 로그인·동의 후 공개(gated) / 신청제
  - license: SPDX 식별자(예: CC-BY-4.0), 커스텀 라이선스면 명칭과 핵심 조건
  - extra_terms: 이용약관, 사용 제한 조항(AUP), 출처 간 라이선스 불일치
  - pii_included: 포함 / 미포함 / 포함 가능성 있음 / 확인 불가
  - pii_types: 이름, 얼굴, 음성, 연락처, 식별번호, 민감정보(건강·생체 등)
  - deidentification_consent: 비식별화 방법, 동의 획득 여부, 삭제 요청 채널
  - known_issues: 철회·수정 이력, 소송, 언론 보도
- description_summary_ko: 데이터셋 설명을 한국어 2~3문장으로 요약합니다. 문서가 없으면 빈 문자열.
- requested_documents: 문서만으로 채울 수 없는 중요한 항목(라이선스, 이용조건, 개인정보)을 확인하려면
  사용자가 어떤 문서를 올려야 하는지 구체적으로 적습니다."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _build_prompt(card: DataCard, keys: list[str]) -> str:
    docs = "\n".join(
        f'<document url="{s.url}" fetched_at="{s.fetched_at.isoformat()}">\n{s.content}\n</document>'
        for s in card.sources
        if s.content
    )
    wanted = "\n".join(f"- {k}: {CARD_FIELDS[k][1]}" for k in keys)
    return f"{docs}\n\n데이터셋: {card.dataset.uid}\n\n채울 항목:\n{wanted}"


def apply_extraction(card: DataCard, extraction: Extraction, keys: list[str]) -> DataCard:
    """LLM 결과를 검증해 카드에 반영한다. 근거가 검증되지 않으면 '추정'."""
    by_url = {s.url: _norm(s.content) for s in card.sources}
    for f in extraction.fields:
        if f.key not in keys or not f.value.strip():
            continue
        text = by_url.get(f.evidence_url)
        if text is None:
            status, url = FieldStatus.inferred, None
        else:
            quote = _norm(f.evidence_quote)
            status = FieldStatus.confirmed if quote and quote in text else FieldStatus.inferred
            url = f.evidence_url
        card.set(f.key, CardField(value=f.value, evidence_url=url, evidence_quote=f.evidence_quote or None, status=status))

    if extraction.description_summary_ko:
        desc = card.fields["description"]
        if not desc.is_missing and isinstance(desc.value, dict):
            desc.value = {**desc.value, "summary_ko": extraction.description_summary_ko}
    for doc in extraction.requested_documents:
        if doc not in card.needs_documents:
            card.needs_documents.append(doc)
    return card


def enrich(card: DataCard, client: anthropic.Anthropic | None = None) -> DataCard:
    """빈 항목과 Description 한국어 요약을 LLM으로 보강한다."""
    keys = card.missing_keys()
    if not any(s.content for s in card.sources):
        return card
    client = client or anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium"},
        system=SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(card, keys)}],
        output_format=Extraction,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        card.needs_documents.append("자동 항목 추출이 실패해 일부 항목을 직접 확인해야 합니다.")
        return card
    return apply_extraction(card, response.parsed_output, keys)
