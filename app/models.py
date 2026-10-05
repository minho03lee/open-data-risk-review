"""데이터 카드 스키마 (기획안 5장 양식)."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Platform(str, Enum):
    huggingface = "huggingface"
    kaggle = "kaggle"
    aihub = "aihub"
    web = "web"


class FieldStatus(str, Enum):
    confirmed = "확인됨"
    inferred = "추정"
    missing = "정보 없음"
    user = "사용자 입력"


class CardField(BaseModel):
    """카드의 한 항목: 값 + 근거 URL + 확인 상태."""

    value: Any = None
    evidence_url: str | None = None
    evidence_quote: str | None = None
    status: FieldStatus = FieldStatus.missing

    @classmethod
    def confirmed(cls, value: Any, url: str | None, quote: str | None = None) -> "CardField":
        if value in (None, "", [], {}):
            return cls()
        return cls(value=value, evidence_url=url, evidence_quote=quote, status=FieldStatus.confirmed)

    @property
    def is_missing(self) -> bool:
        return self.status == FieldStatus.missing


class DatasetRef(BaseModel):
    """확정 단위: 플랫폼 + 저장소 + 버전(+커밋/수집일)."""

    platform: Platform
    repo: str
    version: str | None = None
    revision: str | None = None
    url: str | None = None

    @property
    def uid(self) -> str:
        parts = [self.platform.value, self.repo]
        if self.version:
            parts.append(self.version)
        if self.revision:
            parts.append(self.revision)
        return ":".join(parts)


class Source(BaseModel):
    """분석에 쓴 페이지 스냅샷."""

    url: str
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content_type: str = "text/plain"
    content: str = ""


# 카드 항목 키 → (구분, 한국어 항목명, 요청/추가)
CARD_FIELDS: dict[str, tuple[str, str, str]] = {
    "name": ("기본", "데이터명", "요청"),
    "version": ("기본", "버전·서브셋", "추가"),
    "overview": ("기본", "데이터 개요", "요청"),
    "description": ("기본", "Description", "요청"),
    "data_type": ("기본", "데이터 타입", "요청"),
    "size_language": ("기본", "규모·언어", "추가"),
    "provider": ("제공자", "데이터 제공자 정보", "요청"),
    "provider_origin": ("제공자", "소재국·소속·자금 출처", "추가"),
    "creation": ("제작", "데이터 제작 설명", "요청"),
    "upstream_sources": ("제작", "원본 출처 목록", "추가"),
    "source_url": ("출처", "데이터 출처(URL)", "요청"),
    "access": ("출처", "접근 방식", "추가"),
    "license": ("라이선스", "데이터 라이선스", "요청"),
    "license_evidence": ("라이선스", "라이선스 근거(URL)", "요청"),
    "extra_terms": ("라이선스", "추가 이용조건", "추가"),
    "pii_included": ("개인정보", "개인정보 포함 여부", "요청"),
    "pii_types": ("개인정보", "포함된 개인정보 종류", "요청"),
    "deidentification_consent": ("개인정보", "비식별 조치·동의", "추가"),
    "related_papers": ("활용", "활용 연구(논문) 리스트", "요청"),
    "known_issues": ("이력", "알려진 이슈", "추가"),
}

DATA_TYPES = ["텍스트", "오디오", "이미지", "비디오", "멀티모달", "코드", "표"]
ACCESS_TYPES = ["공개", "로그인·동의 후 공개(gated)", "신청제"]
PII_VALUES = ["포함", "미포함", "포함 가능성 있음", "확인 불가"]


class DataCard(BaseModel):
    id: str | None = None
    dataset: DatasetRef
    fields: dict[str, CardField] = Field(default_factory=lambda: {k: CardField() for k in CARD_FIELDS})
    sources: list[Source] = Field(default_factory=list)
    needs_documents: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def set(self, key: str, field: CardField, *, overwrite: bool = False) -> None:
        if key not in CARD_FIELDS:
            raise KeyError(key)
        if field.is_missing:
            return
        if overwrite or self.fields[key].is_missing:
            self.fields[key] = field

    def missing_keys(self) -> list[str]:
        return [k for k, f in self.fields.items() if f.is_missing]


class Candidate(BaseModel):
    ref: DatasetRef
    title: str
    provider: str | None = None
    score: float = 0.0
    reason: str = ""


class IdentifyResult(BaseModel):
    status: Literal["resolved", "confirm", "candidates", "need_info"]
    ref: DatasetRef | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    question: str | None = None
