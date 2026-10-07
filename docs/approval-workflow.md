# 승인 및 변경 영향 Workflow

SSTD main/Release와 독립 SSTC `[AX]` Issue는 Task 생성 이후 수집·분석·정책 판정·구현·검증·Draft PR을 공유합니다. JSON 검증의 성공만으로 실행하지 않습니다.

| 검증된 결과 | 처리 |
|---|---|
| NOT_REQUIRED | SSTC 수정 없이 COMPLETED, 근거 보존 |
| UNDETERMINED / UNKNOWN / 미해결 질문 | 중단·부족 문맥 기록, 자동 완료·구현 금지 |
| REQUIRED, 승인 영향 없는 LOW/MEDIUM | 검증된 정책 실행 근거로 구현 |
| HIGH 또는 UI/protocol/dependency/permission/destructive | 사유·checkpoint 저장 후 WAITING_APPROVAL |
| CRITICAL | 자동 구현 금지·사람 검토 |
| SSTC 요청에 새 SSTD 계약 필요 | PROTOCOL_APPROVAL_REQUIRED |

이미 바뀐 SSTD 계약에 맞추는 SSTC 수정은 protocol 승인 후 허용합니다. SSTD 계약 쓰기는 허용하지 않습니다. 후보 게시 범위 밖의 dependency·permission 등은 승인만으로 허용 범위를 확대하지 않습니다.

Slack 메시지는 승인 snapshot에 결합된 결과를 다시 검사하여 출처·base/head·SSTC 기준·위험도·사유·요약·영향·질문을 표시합니다. 원시 로그나 인증정보를 보내지 않고, 분석에 없는 추천안을 만들어 표시하지 않습니다. 하나의 Socket Mode 연결에서 대기 Task별 nonce를 구분합니다.

workspace·app·channel·허용 사용자·message ID·nonce·24시간 만료·snapshot이 맞을 때만 결정 저장 후 ack합니다. 승인 후 같은 구현 세션을 재개하며 거절은 REJECTED입니다. 승인 근거와 실행 기록은 분리하고, 입력·결과·범위가 바뀌면 이전 권한을 재사용하지 않습니다.

후보 SHA의 Actions와 run·attempt·artifact receipt가 모두 맞아야 Draft PR을 만듭니다. 승인이나 형식·컴파일 성공은 의미적 정확도, merge·Release·배포 권한이 아닙니다. 패킷·단위 테스트와 의미 평가를 사용하고 실제 기기 UI 확인은 사람에게 남깁니다.
