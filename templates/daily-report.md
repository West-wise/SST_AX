# SST-AX 일일 보고 표시 계약

한국 시간 매일 09:00 이후 전날 00:00 이상 다음 날 00:00 미만의 기록을 한 번 보고한다. `scripts/daily_report.py`의 `build_report`가 구조화된 입력을 생성하고 `render_report`가 아래 형식의 고정 한국어 Slack plain-text blocks와 fallback text를 만든다. 이 파일의 문장을 실행 코드로 해석하지 않는다.

```text
SST-AX 일일 보고 · YYYY-MM-DD
한국 시간 전날 00:00~24:00 활동 / 상태는 집계 시점 기준
활동 Task N개 · 대기/실패 N개 · 기록 확인 N개
분석 VALID 재검증 N개 · Actions 증거 대조 N개 · Draft PR 증거 대조 N개

<검증된 Task ID> · SSTD 변경 또는 독립 기능 요청 · 현재 상태
당일 활동 또는 이전부터 대기/실패 · 분석 검증 등급 · Actions 검증 등급
후보 SHA: <증거를 대조한 40자리 SHA, 있는 경우>
Draft PR: <공식 SSTC GitHub URL, 증거를 대조한 경우>
```

`CREATED`, `TRANSITION`, `COMMAND` 중 하나라도 해당 기간에 있으면 당일 활동 Task다. 같은 Task의 여러 이벤트는 Task 수를 중복 증가시키지 않는다. 활동이 없는 승인 대기·실패·사용량 제한 Task는 별도 backlog로 표시한다. 두 집계는 겹칠 수 있다. 현재 상태를 과거 시점의 상태로 추정하지 않는다. 변화와 backlog가 모두 없는 날에도 변경 사항 없음 보고를 한 번 전송한다.

분석 `VALID`는 입력 본문·결과 hash·계약·세션을 `execution_policy.validated_analysis`로 다시 확인한 경우다. Actions와 Draft PR은 후보·run/attempt·Sensor receipt·producer snapshot·분석/실행 근거의 결합을 대조한 `RECEIPT_VERIFIED`다. 보고 시점에 GitHub를 다시 조회했다는 뜻은 아니다. 상태 이름만으로 검증 성공을 추정하지 않는다. 원문 분석 요약·요청·로그·명령·경로·사용자 정보는 출력하지 않는다.

`contracts/daily-report.schema.json`과 `validate_report`는 닫힌 필드 집합을 사용한다. `validate_report`는 날짜 범위·Task ID 중복·집계값·분류와 증거 등급의 필드 간 일관성도 확인한다. 불완전하거나 변조된 Task는 고정 오류 표시로 남기며, 원본 Task·log·cursor를 수정하지 않는다. Slack 한도를 넘는 보고는 일부 항목을 생략하지 않고 전송 전에 중단한다.

날짜별 immutable snapshot을 먼저 보존하고 잠금 아래 전송 ledger에 `SENDING`을 기록한 뒤 Slack에 게시한다. 확실한 미전송만 동일 snapshot으로 최대 3회 시도한다. 응답 유실·알 수 없는 SDK 오류·채널/ts 불일치·중단된 `SENDING`은 `UNCERTAIN`이며 자동 재전송하지 않는다. `SENT` 뒤에는 현재 Task가 바뀌어도 snapshot과 게시 메시지를 보존한다. 기존 날짜의 팀/채널 변경이나 미전송 snapshot 변경은 거부한다. 토큰과 SDK 응답/예외 원문은 저장하지 않는다.

회귀 테스트 수·분석 의미 정확도·실제 기기·merge·Release·배포 완료는 이 보고로 확인하지 않는다.
