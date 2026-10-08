# Codex 읽기 전용 분석과 재개

Controller는 [고정 입력](impact-inputs.md)을 Codex에 전달하고 [결과 검증](impact-analysis.md)으로 구조·근거·입력 결합을 검사합니다. 분석 성공은 실행 허가가 아닙니다.

ANALYZING Task의 본문 해시를 재계산하고 stdin으로 임시 폴더에 전달합니다. `codex exec --sandbox read-only --json`과 output schema를 사용합니다. 사용자 config·execpolicy rules, shell·apps·plugins·web 도구를 제외합니다. `--ignore-rules`를 AGENTS 문서 무효화와 동일시하지 않습니다. 입력·저장소 텍스트는 분석 자료이며 AX 권한을 확대할 수 없습니다.

CLI·시스템 설정·저장된 로그인은 관리자 신뢰 영역입니다. Slack/GitHub token과 Git 제어 환경은 Worker에 전달하지 않습니다. CLI 설정은 OS 권한 격리를 대신하지 않으며 세션 저장소에는 입력이 남을 수 있습니다.

커밋 SHA는 입력 수집기의 Git 명령으로 확정하고, 요청 hash는 snapshot 본문에서 계산합니다. AI는 `task_id`, `source_type`, `input_context`와 영향별 `evidence_ids`를 응답에 작성하지 않습니다. 생성용 schema와 전달 계약에서 Controller가 구성하는 필드를 제외하고 추가 필드도 거부합니다. Controller는 AI 응답의 형식과 증거 선언의 유일성을 검증한 뒤 고정 manifest의 식별자와 아래 증거 연결을 결합하여 기존 최종 결과 schema로 전체 검증합니다. 현재 Git HEAD로 과거 입력을 다시 해석하지 않으며, 결과를 VALID로 기록하기 전에 증거 본문과 manifest를 재검증합니다.

파싱·크기·알려진 secret 검사 후 모델 응답은 `<task-id>.analysis-N.model.json`에, 식별자를 결합한 최종 결과는 기존 `<task-id>.analysis-N.json`에 저장합니다. Task 로그의 `model_result_file`·`model_result_sha256`은 모델 응답을, 기존 `result_file`·`result_sha256`은 검증된 최종 결과를 참조합니다. AI가 식별자를 임의로 반환하면 `INVALID_RESULT`로 거부하고 모델 응답만 보존합니다. 기존 실패 결과의 SHA를 수정하거나 성공 처리하지 않습니다. 모델 응답 형식은 생성 내부 경계이며 최종 결과 계약·승인·세션·입력 해시 검증을 변경하지 않습니다.

분석의 `change_required`는 SSTC 수정 필요성입니다. 위험도·영향·승인 사유는 필요한 SSTC 작업과 그 작업에 필요한 추가 SSTD 변경을 기준으로 판단합니다. 입력에 포함된 SSTD CI·Release·배포·서비스 재시작·서버 전용 dependency만으로 SSTC의 dependency·파괴적 작업·승인·위험을 선언하지 않습니다. protocol/parser/model뿐 아니라 수치·단위·범위·화면 표시의 호환성을 확인합니다. `sstd_change_required`는 이미 입력으로 주어진 SSTD 변경이 아닌, SSTC 작업을 위해 추가로 필요한 SSTD 변경입니다. 이미 변경된 SSTD 계약에 대한 SSTC 적응의 protocol 영향과 SSTC 요청으로 새 SSTD 계약을 만드는 승인 경계는 유지합니다.

AI 응답의 `evidence`에는 ID별로 한 항목을 선언하고 증거별 `reason`과 필수 배열 `impact_names`를 작성합니다. `impact_names`는 `ui_ux`, `protocol_contract`, `dependency`, `android_permission`, `destructive_action`, `sstd_change_required` 중 해당 증거가 뒷받침하는 영향을 중복 없이 나열합니다. 같은 항목에 여러 영향을 적을 수 있고, 전체 요약만 뒷받침하는 독립 증거는 빈 배열로 보존합니다. 예를 들어 기존 Android manifest 증거는 다음처럼 한 번 선언합니다.

```json
{"evidence_id":"e0013","reason":"현재 Android permission 구성을 확인했다.","impact_names":["android_permission"]}
```

Controller는 선언 순서대로 각 영향의 `evidence_ids`를 구성하고, 최종 `evidence`에는 원래 ID와 설명을 보존하며 `impact_names`를 제외합니다. AI가 작성한 영향의 상태·이유와 결론·위험도·승인 사유·질문은 그대로 둡니다. 선언되지 않은 증거를 추가하거나 다른 증거를 빈 영향에 배정하지 않습니다. 미등록 ID·중복 선언·중복 또는 알 수 없는 영향명·누락된 배열·AI가 반환한 `evidence_ids`는 거부합니다. 확정 결과의 모든 영향에 근거가 있어야 한다는 기존 검증도 유지합니다. 최종 결과 계약과 기존 VALID 분석의 재검증 형식은 바뀌지 않습니다.

`UNKNOWN` 또는 판단·승인 범위를 막는 미해결 질문이 있으면 `UNDETERMINED`이며, 이 판정에는 차단 사유를 설명하는 질문이 최소 하나 필요합니다. 비차단 운영 관찰은 summary나 증거 이유에 기록합니다. `NOT_REQUIRED`는 위험도 NONE·모든 영향 ABSENT·승인 사유 없음·질문 없음일 때만 허용합니다. 프롬프트가 이 규칙을 안내해도 기존 파서 검증을 통과해야 하며 잘못된 결과를 자동 보정하지 않습니다.

`INVALID_RESULT`에는 Task 로그의 `codex_analysis.validation_errors`로 고정 오류 코드와 필드 위치를 남깁니다. 원시 이벤트·stderr는 저장하지 않으며 기존 분석 JSON·실패 상태·checkpoint는 보존합니다. OCI Task `sstd-sync-20261008-0001`은 증거 ID 중복·판정 모순으로, `sstd-sync-20261008-0002`는 같은 입력의 재분석에서 39자리 잘못된 SSTD SHA 출력으로 실패했습니다. 두 Task의 최초 교정은 사용자 제공 검증 출력에 근거했습니다. 이후 보존된 acceptance 산출물에서 `sstd-sync-20261008-0003`의 식별자 결합·모델 응답 해시는 정상이나 `android_permission`이 참조한 `e0013`의 전체 증거 선언 누락으로 실패한 것을 확인했습니다. 해당 증거의 본문 hash와 `missing=false`, `truncated=false`는 정상이므로 수집 누락을 원인으로 처리하지 않습니다. 세 Task의 실패 기록을 수정하거나 자동 재개하지 않습니다.

교정 후 OCI 확인은 운영자가 같은 SSTD base/head와 SSTC revision, 증거 ID·본문 hash를 유지한 별도 새 Task로 실행합니다. 로컬 합성 회귀 통과와 실제 AI 완료 증거는 구분합니다. 이번 무영향 입력의 종료 기준은 새 실제 분석의 `VALID`와 정책 재검증에 따른 `COMPLETED / VERIFIED_NO_CLIENT_IMPACT` 기록입니다. 적합한 `UNDETERMINED`는 VALID일 수 있어도 완료로 취급하지 않습니다. 기존 실패 기록과 Controller cursor를 보존하며, 이후 두 입력의 전체 경로는 기존 [완료 검증 기준](original-goal-audit-20261006.md)과 [의미 평가](impact-analysis.md#의미-평가)를 따릅니다.

```bash
python3 -B scripts/run_codex_impact.py \
  --task-file state/tasks/<task-id>.json \
  --input-directory state/tasks/<task-id>.inputs
```

성공 출력은 CODEX_IMPACT=VALID, AUTHORIZATION=NONE입니다. 실행 정책이 입력·결과·세션을 재검증하여 완료·판단 유보·조건부 승인을 분기합니다.

thread.started의 ID를 즉시 checkpoint에 저장하고 정확한 ID로 재개합니다. `--last`나 자동 새 세션을 쓰지 않습니다. 이미 VALID인 분석에 같은 명령을 실행하면 입력·결과·세션을 재검증하고 기존 결과를 반환합니다. Codex 호출과 시도 횟수 증가는 없습니다. 정상 승인 후에는 Worker가 같은 세션으로 구현합니다. 과거 `--resume-approved` CLI는 실행 전에 거부하고 내부 승인 후 재분석 분기도 제거했습니다. 후속 영향 재분석은 새 Task로 진행하고, 이전 구현 승인을 새 결과에 재사용하지 않습니다.

immutable 실행 근거와 mutable 실행 checkpoint를 분리하여 로그 추가만으로 승인을 폐기하지 않습니다. 동일 입력·범위·작업 공간을 재검증한 뒤 재개합니다.

분석 1회는 최대 10분, 이벤트 총 8 MiB·한 줄 1 MiB이며 Task 누적 예산도 적용합니다. 원시 JSONL·stderr를 저장하지 않습니다. 누락·잘림·UNKNOWN·질문은 확정 판단을 막고 사용량 제한은 DEFERRED_RATE_LIMIT입니다. 계정에서 확인한 실제 reset 시각을 기록한 뒤 그 시각 이후에만 재개합니다.

```bash
python3 -B scripts/update_task_state.py --task-file state/tasks/<task-id>.json \
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
python3 -B scripts/run_codex_impact.py --task-file "$TASK_FILE" --input-directory "$INPUT_DIR" \
  --reconcile-interrupted --observed-active-seconds "$OBSERVED_ACTIVE_SECONDS" \
  --observed-write-steps "$OBSERVED_WRITE_STEPS"
```

관측값은 이전 기록보다 작을 수 없습니다. 기록된 PID가 살아 있거나 확인에 실패하면 복구·실패 종결을 거부하며 운영자가 먼저 실제 프로세스를 중단해야 합니다. PID가 없는 옛 RUNNING 기록은 자동 재개하지 않습니다. 추가 실행을 허용하지 않는 `--abort-interrupted`로 증거를 보존한 채 ANALYSIS_FAILED를 기록하고 새 Task를 판단합니다. 이 명령은 OS 프로세스를 종료하지 않습니다.

비 JSON stdout은 공식 JSONL 계약 위반으로 거부합니다. Codex 0.153.2 공식 소스의 최상위 `error`는 치명 오류이고 `item.type=error`는 비치명 알림이므로, 후자 뒤 정상 완료는 허용합니다. 원시 메시지는 저장하지 않고 고정 오류 코드만 남깁니다. 프록시·CA의 신뢰된 시스템 환경은 전달하되 Slack/GitHub 인증 환경은 제외합니다. 상위 디렉터리의 symlink/reparse point 거부는 유지하므로 입력·결과 저장 경로는 실제 디렉터리를 사용해야 합니다.

현재 nullable 계약은 `type: [string, null]`이며 CLI schema에서도 보존합니다. 지원하지 않는 조합 키워드를 추가하면 schema 생성 단계에서 명시적으로 거부합니다. 공식 [JSONL 동작](https://developers.openai.com/codex/noninteractive), [구조화 출력](https://developers.openai.com/api/docs/guides/structured-outputs), [0.153.2 이벤트 정의](https://github.com/openai/codex/blob/rust-v0.153.2/codex-rs/exec/src/exec_events.rs)를 기준으로 검토했습니다. 실제 OCI CLI 호출은 운영자가 확인해야 하며 로컬 응답 테스트·의미 사례 비교가 실제 AI 정확도나 OCI sandbox 검증을 대신하지 않습니다. 구현 중단은 [Worker 재개](sstc-worker.md)를 따릅니다.
