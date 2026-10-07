# Codex 읽기 전용 분석과 재개

Controller는 [고정 입력](impact-inputs.md)을 Codex에 전달하고 [결과 검증](impact-analysis.md)으로 구조·근거·입력 결합을 검사합니다. 분석 성공은 실행 허가가 아닙니다.

ANALYZING Task의 본문 해시를 재계산하고 stdin으로 임시 폴더에 전달합니다. `codex exec --sandbox read-only --json`과 output schema를 사용합니다. 사용자 config·execpolicy rules, shell·apps·plugins·web 도구를 제외합니다. `--ignore-rules`를 AGENTS 문서 무효화와 동일시하지 않습니다. 입력·저장소 텍스트는 분석 자료이며 AX 권한을 확대할 수 없습니다.

CLI·시스템 설정·저장된 로그인은 관리자 신뢰 영역입니다. Slack/GitHub token과 Git 제어 환경은 Worker에 전달하지 않습니다. CLI 설정은 OS 권한 격리를 대신하지 않으며 세션 저장소에는 입력이 남을 수 있습니다.

```bash
python -B scripts/run_codex_impact.py \
  --task-file state/tasks/<task-id>.json \
  --input-directory state/tasks/<task-id>.inputs
```

성공 출력은 CODEX_IMPACT=VALID, AUTHORIZATION=NONE입니다. 실행 정책이 입력·결과·세션을 재검증하여 완료·판단 유보·조건부 승인을 분기합니다.

thread.started의 ID를 즉시 checkpoint에 저장하고 정확한 ID로 재개합니다. `--last`나 자동 새 세션을 쓰지 않습니다. 이미 VALID인 분석에 같은 명령을 실행하면 입력·결과·세션을 재검증하고 기존 결과를 반환합니다. Codex 호출과 시도 횟수 증가는 없습니다. 정상 승인 후에는 Worker가 같은 세션으로 구현합니다. 과거 `--resume-approved` CLI는 실행 전에 거부하고 내부 승인 후 재분석 분기도 제거했습니다. 후속 영향 재분석은 새 Task로 진행하고, 이전 구현 승인을 새 결과에 재사용하지 않습니다.

immutable 실행 근거와 mutable 실행 checkpoint를 분리하여 로그 추가만으로 승인을 폐기하지 않습니다. 동일 입력·범위·작업 공간을 재검증한 뒤 재개합니다.

분석 1회는 최대 10분, 이벤트 총 8 MiB·한 줄 1 MiB이며 Task 누적 예산도 적용합니다. 원시 JSONL·stderr를 저장하지 않습니다. 누락·잘림·UNKNOWN·질문은 확정 판단을 막고 사용량 제한은 DEFERRED_RATE_LIMIT입니다. 계정에서 확인한 실제 reset 시각을 기록한 뒤 그 시각 이후에만 재개합니다.

```bash
python -B scripts/update_task_state.py --task-file state/tasks/<task-id>.json \
  --record-reset-at '<실제로 확인한 ISO-8601 시각과 시간대>'
```

| 분석 outcome / 상황 | Task 상태 | 다음 동작 |
|---|---|---|
| VALID | ANALYZING | 정책 판정; 재호출은 기존 결과 재검증 |
| TIMEOUT / CODEX_FAILED / INTERRUPTED | ANALYZING | 동일 입력·세션으로 제한 내 재시도 |
| 재시도 가능한 오류의 세 번째 시도 | ANALYSIS_FAILED | ATTEMPT_LIMIT 기록; 새 Task 판단 |
| RATE_LIMIT | DEFERRED_RATE_LIMIT | 관측한 reset 시각 이후에만 재개 |
| 실행 파일 없음·미지원 CLI·preflight timeout | ANALYZING | 시도 횟수 미소모, 환경 교정; Controller 오류 한도 적용 |
| 잘못된 결과·도구 사용·세션/입력 변경·출력 제한 | ANALYSIS_FAILED | 고정 원인 코드 보존; 자동 재시도 없음 |
| SSTC 요청의 새로운 SSTD 계약 필요 | PROTOCOL_APPROVAL_REQUIRED | 별도 사람 판단 |
| 누적 예산 초과 | ANALYSIS_FAILED | 예산 보존 후 종료 |
| 크래시 뒤 RUNNING | ANALYZING, 자동 호출 거부 | 기록된 PID 종료 및 실제 누적 예산 확인 후 명시적 복구, 또는 실패 종결 |

Task pair와 분석 checkpoint는 하나의 pending journal로 저장합니다. 저장 중단은 먼저 `update_task_state.py --recover`로 복구합니다. 분석 도중 프로세스가 죽은 경우에는 기록된 PID의 종료와 실제 누적 실행량을 확인해야 합니다. 아래 명령은 Codex를 실행하지 않으며, 복구 후 일반 분석 명령을 별도로 실행합니다.

```bash
python -B scripts/run_codex_impact.py --task-file "$TASK_FILE" --input-directory "$INPUT_DIR" \
  --reconcile-interrupted --observed-active-seconds "$OBSERVED_ACTIVE_SECONDS" \
  --observed-write-steps "$OBSERVED_WRITE_STEPS"
```

관측값은 이전 기록보다 작을 수 없습니다. PID가 없는 옛 RUNNING 기록이나 종료를 확인할 수 없는 작업은 자동 재개하지 않습니다. 증거를 보존한 채 `--abort-interrupted`로 ANALYSIS_FAILED를 기록하고 새 Task를 판단합니다. 실행 중인 프로세스는 이 명령으로 중단하지 않습니다.

비 JSON stdout은 공식 JSONL 계약 위반으로 거부합니다. Codex 0.153.2 공식 소스의 최상위 `error`는 치명 오류이고 `item.type=error`는 비치명 알림이므로, 후자 뒤 정상 완료는 허용합니다. 원시 메시지는 저장하지 않고 고정 오류 코드만 남깁니다. 프록시·CA의 신뢰된 시스템 환경은 전달하되 Slack/GitHub 인증 환경은 제외합니다. 상위 디렉터리의 symlink/reparse point 거부는 유지하므로 입력·결과 저장 경로는 실제 디렉터리를 사용해야 합니다.

현재 nullable 계약은 `type: [string, null]`이며 CLI schema에서도 보존합니다. 지원하지 않는 조합 키워드를 추가하면 schema 생성 단계에서 명시적으로 거부합니다. 공식 [JSONL 동작](https://developers.openai.com/codex/noninteractive), [구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs), [0.153.2 이벤트 정의](https://github.com/openai/codex/blob/rust-v0.153.2/codex-rs/exec/src/exec_events.rs)를 기준으로 검토했습니다. 실제 OCI CLI 호출은 운영자가 확인해야 하며 로컬 응답 테스트·의미 사례 비교가 실제 AI 정확도나 OCI sandbox 검증을 대신하지 않습니다. 구현 중단은 [Worker 재개](sstc-worker.md)를 따릅니다.
