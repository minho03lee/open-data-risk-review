# 수동 제재·감시 목록

자동으로 내려받을 수 없는 목록(미 국방부 1260H, UFLPA Entity List, FCC Covered List, 한국 정부 제재 명단 등)을
법무 담당자가 직접 정리해 넣는 곳이다. `*.csv` 파일은 모두 읽는다.

헤더: `list,jurisdiction,name,aliases,program,source_url,note`

- `list`: 목록 이름 (예: `DoD 1260H`)
- `jurisdiction`: `미국` / `EU` / `한국` / `기타`
- `aliases`: 별칭은 `;`로 구분
- `source_url`: 해당 목록의 공식 게시 위치 (필수. 근거로 화면에 표시된다)

이 저장소에는 예시 외의 기관명을 넣지 않는다. 항목은 원문 확인과 법무 검토를 거쳐 추가한다.
