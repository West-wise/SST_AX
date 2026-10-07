# 운영과 복구

기본 실행은 [Controller](controller.md)이며 수동 CLI는 점검·복구에 사용합니다. 각 Task는 별도 branch·worktree를 사용합니다.

| 기록 | 용도 |
|---|---|
| Task state / log | 상태·전이·정제된 명령 receipt·미해결 사항 |
| manifest / evidence | 고정 SHA·요청·실제 본문 해시 |
| 분석 / Codex checkpoint | 결과·세션·시도·입력 결합 |
| 승인 checkpoint / 감사 기록 | 승인 당시 범위와 사람의 결정 |
| execution-authority | 정책 허용 또는 Slack 승인·입력·결과·세션 결합 |
| execution checkpoint | 누적 예산·재시도·작업 공간 재개 |
| pipeline / GitHub receipt | 후보·run·attempt·artifact·Draft PR 결합 |

최초 승인 checkpoint·요청·승인 직후 pair는 execution-authority에 불변 보존하고 매번 재검증합니다. 표준 checkpoint는 현재 중단·재개 상태로 갱신되지만 원래 승인 근거를 대신하지 않습니다. pair 저장 중단의 `.pending.json`은 다음 명령으로 roll-forward합니다. 저장 복구는 실행 권한을 새로 만들지 않습니다.

state·log와 함께 갱신해야 하는 표준/Codex checkpoint는 같은 pending journal에 저장합니다. 어느 파일 교체 지점에서 중단돼도 `--recover`가 고정 경로의 모든 파일을 같은 snapshot으로 복구하며, 이전 두 파일 형식의 journal도 지원합니다.

```bash
python -B scripts/update_task_state.py --task-file state/tasks/<task-id>.json --recover
```

같은 Task의 재개는 입력·결과·세션·정책·승인과 branch·worktree·source·HEAD·diff를 검사합니다. 범위가 다르면 이전 권한을 쓰지 않습니다. 삭제·이름 변경·성공 값 편집으로 gate를 우회하지 않습니다.

pair 복구 뒤에도 `RUNNING`이 남으면 기존 실행 결과가 불확실한 상태입니다. 분석과 Worker의 명시적 interruption reconciliation을 사용합니다. [Worker 복구](sstc-worker.md)는 저장된 PID의 종료·원래 실행 권한·현재 후보 범위·실제 관측한 누적 예산을 검증하고 `INTERRUPTED`만 기록합니다. 실행 재개는 별도 명령이며, PID가 없는 이전 기록은 종료 후 별도 Task 판단으로 넘깁니다.

최대 재시도 3회, 실제 활성 실행 누적 30분, write 단계 20회를 적용하며 승인·Actions 대기는 제외합니다. 후보 파일 상한은 별도 제한입니다. 재개는 예산을 초기화하지 않습니다. 사용량 제한은 DEFERRED_RATE_LIMIT이며 실제 확인한 reset 시각 전에는 호출하지 않습니다. 분석은 기존 reset 절차, 구현은 Worker 실행 checkpoint를 사용하는 재개 절차를 따릅니다.

VALIDATING에서는 Actions를 확인하고 Codex를 다시 실행하지 않습니다. 게시 응답 유실은 PUBLISH_UNCERTAIN, PR 응답 유실은 PR_UNCERTAIN으로 보존합니다. 불확실한 POST를 반복하지 않고 정확한 원격 상태로 재조정할 수 있을 때만 이어갑니다. 검증 실패에는 Draft PR을 만들지 않습니다.

기존 OCI `sstc-feature-20260916-0001`의 dirty worktree와 WORKER_FAILED를 보존합니다. 현재 main 빌드 성공은 그 UI 후보의 검증이 아닙니다. 원래 승인·분석·manifest·세션·branch·diff가 복원 검증될 때만 같은 Task를 재개하고, 근거 부족이나 범위 변경은 사람의 새 승인·Task 판단으로 넘깁니다.

SSTD CI는 테스트·빌드, 수동 Release workflow는 패키징을 담당합니다. Jenkins 배포는 기본 OFF인 선택 옵션에 따릅니다. AX는 SSTD 변경·Release·운영 배포를 수행하지 않습니다. 자동 종료는 수정 없는 COMPLETED 또는 검증된 Draft PR의 READY_FOR_REVIEW입니다. 일일 보고·운영 지표는 이 종료 조건과 분리합니다.
