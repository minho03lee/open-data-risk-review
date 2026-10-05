# 오픈데이터 리스크 검토 (open-data-risk-review)

AI 학습에 쓰려는 오픈데이터셋의 URL이나 이름을 넣으면 데이터셋을 하나로 확정하고, 근거 링크가 붙은 데이터 카드를 만든 뒤 라이선스·개인정보·평판·기타 리스크를 평가하는 사내 전용 도구입니다. 법률 자문이 아닌 1차 검토용 참고 분석입니다.

## 현재 단계: 식별 + 데이터 카드 생성

| 입력 | 처리 |
|---|---|
| Hugging Face URL | Hub API + README + LICENSE 파일로 카드 작성, gated 동의 문구 수집 |
| Kaggle URL | Kaggle API(키 필요), 키가 없으면 페이지 메타데이터만 쓰고 자료 요청 |
| AI허브 URL | 공개 소개 페이지와 이용약관 기준, 접근 방식은 신청제 |
| 제공자 자체 사이트 URL | 일반 웹페이지 수집 후 LLM이 항목 추출 |
| 데이터셋 이름 | Hugging Face·Kaggle 검색 → 확정 / 후보 선택 / 추가 정보 요청 |

카드의 모든 항목은 값, 근거 URL, 확인 상태(확인됨 / 추정 / 정보 없음 / 사용자 입력)를 갖습니다. LLM이 채운 항목은 인용문이 실제 수집 문서에 있을 때만 '확인됨'이고, 아니면 '추정'입니다. 분석에 쓴 페이지는 수집 시각과 함께 스냅샷으로 저장합니다.

리스크 분석(4개 영역), 계보 추적, 제재 목록 대조는 다음 단계에서 추가합니다.

## 구조

```
app/
  main.py          FastAPI 앱, API와 내부용 화면(/)
  identify.py      URL 파서, 이름 검색, 확정 기준
  connectors/      huggingface / kaggle / aihub / web 수집기
  extract.py       Claude로 빈 항목 추출 (근거 인용 검증)
  licenses.py      라이선스 표기 → SPDX 정규화
  store.py         Supabase Postgres(odr 스키마) 또는 메모리 저장소
supabase/migrations/  odr 스키마 생성 SQL
render.yaml        Render 새 웹 서비스 정의
```

## API

- `POST /api/identify` `{"query": "..."}` → `resolved | confirm | candidates | need_info`
- `POST /api/datacards` `{"query": "..."}` 또는 `{"ref": {...}}` → 카드 생성 (확정되지 않으면 409와 후보 목록)
- `GET /api/datacards`, `GET /api/datacards/{id}`
- `PATCH /api/datacards/{id}` `{"fields": {"pii_included": "포함 가능성 있음"}}` → '사용자 입력'으로 표시

## 로컬 실행

```bash
pip install -r requirements-dev.txt
cp .env.example .env   # 값이 없으면 메모리 저장소, LLM 보강 생략
uvicorn app.main:app --reload
pytest
```

## 배포

기존 ai-lawsuit-monitor의 Render 서비스와 Supabase 데이터는 건드리지 않습니다.

1. Supabase SQL Editor에서 `supabase/migrations/20261005000000_odr_datacards.sql` 실행 (새 `odr` 스키마만 만듦)
2. Render에서 이 저장소로 새 Blueprint 또는 Web Service 생성 (`render.yaml`)
3. 환경 변수 `DATABASE_URL`, `ANTHROPIC_API_KEY`, `APP_ACCESS_TOKEN`, (선택) `KAGGLE_USERNAME`/`KAGGLE_KEY` 입력
