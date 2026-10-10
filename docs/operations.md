# 운영과 복구

기본 실행은 [Controller](controller.md)이며 수동 CLI는 점검·복구에 사용합니다. 각 Task는 별도 branch·worktree를 사용합니다.

| 기록 | 용도 |
|---|---|
| Task state / log | 상태·전이·정제된 명령 receipt·미해결 사항 |
| manifest / evidence | 고정 SHA·요청·실제 본문 해시 |
| 분석 / Codex checkpoint | 결과·세션·시도·입력 결합 |
| 승인 checkpoint / 감사 기록 | 승인 당시 범위와 사람의 결정 |
| `*.slack-feedback.json` | 승인·거절 답장 outbox와 전송 결과 |
| execution-authority | 정책 허용 또는 Slack 승인·입력·결과·세션 결합 |
| 적용 정책 profile / snapshot | Task에 고정한 정책 버전·신뢰된 본문 해시·예산 기준 |
| execution checkpoint | 누적 예산·재시도·작업 공간 재개 |
| pipeline / GitHub receipt | 후보·run·attempt·artifact·Draft PR 결합 |

최초 승인 checkpoint·요청·승인 직후 pair는 execution-authority에 불변 보존하고 매번 재검증합니다. 표준 checkpoint는 현재 중단·재개 상태로 갱신되지만 원래 승인 근거를 대신하지 않습니다. pair 저장 중단의 `.pending.json`은 다음 명령으로 roll-forward합니다. 저장 복구는 실행 권한을 새로 만들지 않습니다.

state·log와 함께 갱신해야 하는 표준/Codex checkpoint는 같은 pending journal에 저장합니다. 어느 파일 교체 지점에서 중단돼도 `--recover`가 고정 경로의 모든 파일을 같은 snapshot으로 복구하며, 이전 두 파일 형식의 journal도 지원합니다.

```bash
python3 -B scripts/update_task_state.py --task-file state/tasks/<task-id>.json --recover
```

같은 Task의 재개는 입력·결과·세션·정책·승인과 branch·worktree·source·HEAD·diff를 검사합니다. 범위가 다르면 이전 권한을 쓰지 않습니다. 삭제·이름 변경·성공 값 편집으로 gate를 우회하지 않습니다.

pair 복구 뒤에도 `RUNNING`이 남으면 기존 실행 결과가 불확실한 상태입니다. 분석과 Worker의 명시적 interruption reconciliation을 사용합니다. [Worker 복구](sstc-worker.md)는 저장된 PID의 종료·원래 실행 권한·현재 후보 범위·실제 관측한 누적 예산을 검증하고 `INTERRUPTED`만 기록합니다. 실행 재개는 별도 명령이며, PID가 없는 이전 기록은 종료 후 별도 Task 판단으로 넘깁니다.

새 Task의 `balanced-v2`는 분석·구현별 최초 실행 포함 최대 3회와 두 단계 추가 재시도
합계 2회를 적용합니다. 활성 실행 누적 30분·write 20회는 유지하며 승인·Actions
대기는 제외합니다. 검증된 Controller 읽기 점검은 별도 512회 한도이고, 임의 shell은
write 단계입니다. Worker는 공유 write 합계 15회까지만 사용하며 나머지 5회를
Controller 게시·검증 요청·Draft PR 생성에 남깁니다. 후보 파일 상한은 별도
제한입니다. profile 없는 기존 Task는 `legacy-v1`의 카운터·재시도·권한을 유지합니다.

재개는 적용 정책 snapshot·입력·승인·실행 범위와 모든 누적 카운터를 확인하며
예산을 초기화하지 않습니다. 정책 문서 업데이트로 기존 Task를 새 profile로
이관하지 않습니다. 동일 오류의 연속 횟수와 전체 실패 횟수는 별도로 보존합니다.
사용량 제한은 DEFERRED_RATE_LIMIT이며 실제 확인한 reset 시각 전에는 호출하지
않습니다. 분석은 기존 reset 절차, 구현은 Worker 실행 checkpoint를 사용하는 재개
절차를 따릅니다.

지연을 조사할 때 활성 실행과 승인·Actions 대기 시간을 나누어 기록합니다. 다음
수정 전에 환경·지원 검증 범위·완료 예산을 확인하고, 같은 revision의 유효한
receipt는 소스 변경이나 새 문제가 없을 때 사용합니다. 로컬 테스트 통과 수를 실제
AI 분석·Slack 수신·Android Actions·패키징 또는 Draft PR 완료 증거로 대신하지
않습니다. 미지원 dependency·permission 검증은 별도 교정 범위이며 승인만으로
실행하지 않습니다.

VALIDATING에서는 Actions를 확인하고 Codex를 다시 실행하지 않습니다. 게시 응답 유실은 PUBLISH_UNCERTAIN, PR 응답 유실은 PR_UNCERTAIN으로 보존합니다. 불확실한 POST를 반복하지 않고 정확한 원격 상태로 재조정할 수 있을 때만 이어갑니다. 검증 실패에는 Draft PR을 만들지 않습니다.

Slack 승인 수신은 인증정보가 이미 제공된 운영 프로세스에서 다음 명시 모드로 계속 유지할 수 있습니다. 이 명령의 추가는 기존 Gateway 배포·OS 권한·서비스 설치 또는 Controller cursor를 변경하지 않습니다. 인증정보를 shell 명령이나 로그에 넣지 않습니다.

```bash
python3 -B scripts/slack_runner.py serve --tasks-directory /absolute/controller-owned/tasks
```

`SLACK_CONNECTED`와 `SLACK_WAITING_APPROVAL`은 연결·수신 준비를 나타냅니다. 버튼 결정의 실제 접수는 Task 감사 기록과 원래 Slack 스레드의 승인/거절 접수 답장으로 확인합니다. `SLACK_FEEDBACK=SENT`는 답장 전송 성공, `UNCERTAIN` 또는 `SLACK_FEEDBACK_REVIEW_REQUIRED`는 답장 확인 필요를 뜻합니다. Slack의 버튼 느낌표만으로 승인 기록을 변경하지 않습니다.

답장 outbox의 `PENDING`은 저장된 결정에 한해 재개하고, 크래시 후 남은 `SENDING`은 `UNCERTAIN`으로 보존합니다. `SENT`와 `UNCERTAIN`은 자동 재전송하지 않습니다. 과거 수동 답장이나 outbox가 없는 과거 결정도 시작 시 자동 전송하지 않습니다. 불확실한 답장은 Task 결정·원래 Slack 스레드·전송 기록을 읽어 확인하며, 성공 값 편집이나 outbox 삭제로 재전송을 강제하지 않습니다. SIGINT/SIGTERM은 연결을 닫고 전송 중인 답장 worker가 마친 뒤 종료합니다.

기존 OCI `sstc-feature-20260916-0001`의 dirty worktree와 WORKER_FAILED를 보존합니다. 현재 main 빌드 성공은 그 UI 후보의 검증이 아닙니다. 원래 승인·분석·manifest·세션·branch·diff가 복원 검증될 때만 같은 Task를 재개하고, 근거 부족이나 범위 변경은 사람의 새 승인·Task 판단으로 넘깁니다.

SSTD CI는 테스트·빌드, 수동 Release workflow는 패키징을 담당합니다. Jenkins 배포는 기본 OFF인 선택 옵션에 따릅니다. AX는 SSTD 변경·Release·운영 배포를 수행하지 않습니다. 자동 종료는 수정 없는 COMPLETED 또는 검증된 Draft PR의 READY_FOR_REVIEW입니다. 일일 보고·운영 지표는 이 종료 조건과 분리합니다.
