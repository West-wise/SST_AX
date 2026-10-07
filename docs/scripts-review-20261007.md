# 스크립트·사용 여부 감사 — 2026-10-07

기준은 PR #15가 병합된 `main`의 `ddab9d3b7430fd79b80df424ab49ec84d9d25953`이다.
개발 Task는 `sstc-feature-20261007-0001`이며, 감사와 수정은 `ax/scripts-review` branch와 별도 worktree에서 수행했다.
자동 운영, 수동 점검·복구 CLI, 결정적 검증, 합성 fixture, 역사 문서를 구분한다.
Controller에서 호출하지 않는다는 이유만으로 수동 CLI를 삭제하지 않는다.

## 전체 파일 확인

`git ls-files`의 83개를 확인하고 Python AST의 import·함수 호출과 `rg`의 동적 경로·문서·테스트 참조를 함께 추적했다.
상태·worktree·캐시·인증정보 등 미추적 운영 파일은 삭제 대상에 포함하지 않았다.

| 묶음 | 개수 | 확인한 내용 |
|---|---:|---|
| 저장소 루트 | 5 | `.gitignore`, `AGENTS.md`, `README.md`, 초기 아키텍처 계획, `requirements-slack.txt` |
| `config/` | 1 | Controller 절대 경로·poll 설정 예시, `docs/controller.md`에서 사용 |
| `contracts/` | 1 | 입력 manifest·영향 결과 규약, 수집·분석·승인·정책·Sensor에서 사용 |
| `docs/` | 17 | 운영 절차 15개, 초기 하네스 평가·목표 교정 감사 2개 |
| `policies/` | 1 | 실행 권한·예산 기준, 실행 근거의 정책 digest에 결합 |
| `scripts/` | 26 | 아래 caller 표로 전부 확인 |
| `specs/` | 1 | 영향 결과 수용 기준, 검증 문서에서 참조 |
| `state/` | 4 | 상태 schema·Git ignore·tasks/checkpoints 폴더 유지 파일 |
| `templates/` | 1 | Task log 생성 template, `create_task.py`가 동적으로 읽음 |
| `tests/` | 26 | 테스트 모듈 16개, 영향 fixture 6개, 의미 평가 fixture 4개 |

Schema·policy·template은 Python import가 없어도 경로로 읽는다. Fixture 6개도 일부는 이름을 조합해 읽는다.
`.gitignore`와 `.gitkeep`에는 import caller가 없어도 Git의 추적 범위·빈 폴더 유지 역할이 있다.
현재 SST-AX에는 Android build/release workflow가 없으며, 대상 Sensor는 SSTC의 workflow를 검증한다.

## 26개 스크립트의 caller와 역할

| 스크립트 | 활성 caller·사용 근거 | 판정 |
|---|---|---|
| `analyze_impact.py` | `test_task_cli`의 수동 SSTD/SSTC 증적 CLI, 검증 규약·역사 문서 | 유지: legacy Markdown 증적 기능 |
| `codex_impact.py` | Controller 분석, `run_codex_impact`, 정책·승인·후보의 입력/세션 검증 | 유지: 공통 읽기 전용 분석 |
| `collect_impact_inputs.py` | 입력 문서·`test_impact_collection`의 수동 CLI | 유지: 운영 점검 입력 수집 |
| `controller_inputs.py` | Controller의 고정 SHA 확보·대상 context 선택 | 유지 |
| `create_task.py` | Controller, Slack quickstart, Task/Worker 회귀 테스트 | 유지: Task 생성·log template |
| `evaluate_impact.py` | 영향 분석 문서·`test_semantic_evaluation` | 유지: 사람이 정한 의미 사례 비교 |
| `execution_policy.py` | Controller 분기, 분석/Worker/게시의 공통 예산·불변 실행 근거 | 유지 |
| `github_validation.py` | 수동 Sensor CLI, Controller API, pipeline 검증, 정책 API 예산 | 유지 |
| `impact_collection.py` | 수집 CLI·Controller·고정 안내문/내용 검사 | 유지 |
| `impact_validation.py` | 수집·분석·승인·정책·Sensor·의미 평가·검증 CLI | 유지 |
| `request_slack_approval.py` | Slack quickstart·`test_request_slack_approval` | 유지: 대화형 수동 승인 요청 |
| `run_codex_impact.py` | Codex 분석 문서·CLI 경계 회귀 테스트 | 유지: 수동 분석 CLI |
| `run_controller.py` | Controller 운영 문서·CLI 설정 테스트 | 유지: 자동 운영 진입점 |
| `run_github_validation.py` | 원격 Sensor 문서·CLI 테스트 | 유지: 요청·조회·응답 손실 복구 |
| `run_sstc_pipeline.py` | pipeline 운영 문서·CLI 테스트 | 유지: 게시·조회·복구·중단 |
| `run_sstc_worker.py` | Worker/pipeline 운영 문서·deprecated 분석 CLI 안내 | 유지: 구현·명시 재개 |
| `slack_approval.py` | Slack runner, 승인·정책·Worker 회귀 테스트 | 유지: 승인 snapshot·nonce·결정 검증 |
| `slack_runner.py` | Controller의 여러 Task 승인 polling, Slack CLI | 유지 |
| `sst_ax_controller.py` | `run_controller`, Controller 회귀 테스트 | 유지: 두 Handler·Task 단계 연결 |
| `sstc_candidate.py` | Worker/pipeline, 후보 snapshot/tree 회귀 테스트 | 유지: 제한된 Android 후보·Git tree 계산 |
| `sstc_pipeline.py` | Controller·pipeline CLI·게시 회귀 테스트 | 유지: 고정 후보 Actions·Draft PR |
| `sstc_worker.py` | Controller·Worker CLI·pipeline 명령 receipt 검사 | 유지: 승인된 같은 세션 구현 |
| `task_storage.py` | Task 생성·전이·Controller·분석·승인·정책·Sensor·Worker·게시 | 유지: lock·pair journal·checkpoint |
| `update_task_state.py` | Controller 전이·운영 reset/저장 복구·Slack 수동 요청 | 유지 |
| `validate_impact_result.py` | 분석 문서·수집/분석/검증 CLI 테스트 | 유지: 결정적 검증의 수동 진입점 |
| `validate_task_state.py` | 생성·저장·전이·수집·분석 검증, Slack/Task CLI | 유지 |

자동 운영 경로는 `run_controller → sst_ax_controller → impact_collection / codex_impact → execution_policy → sstc_worker → sstc_pipeline → github_validation`이다.
`slack_runner → slack_approval`은 필요한 승인 결정을 처리한다.
수동 wrapper는 이 경로와 같은 library를 사용하면서 운영 점검·복구 진입점을 제공하므로 독립적인 불필요 파일이 아니다.

## 정리 판정

| 후보 | 근거 | 처리 |
|---|---|---|
| `codex_impact.run(resume_approved=True)` 내부 경로 | 공개 CLI는 이미 실행 전 차단, Controller는 기본 분석만 호출; 실제 승인 후 구현은 Worker가 수행 | 내부 승인 후 재분석 경로 제거. `approved_session`은 정책/Worker에 필요해 유지 |
| `sstc_pipeline`의 `approved_session`, `bundle` import | AST에서 이름 사용 없음, 공통 `execution_context`로 이미 대체 | import 제거 |
| `sstc_worker`의 `tempfile`, `approved_session`, `canonical`, `validate_pair` import | AST에서 이름 사용 없음 | 제거 |
| `execution_policy`의 `timezone` import | AST에서 이름 사용 없음 | 제거 |
| `analyze_impact.py`와 `.impact.md` | 현재 Controller/Sensor는 Markdown을 읽지 않지만 수동 CLI 결과와 두 회귀 테스트는 존재 | 파일 유지. 삭제하면 legacy CLI의 관측 가능한 동작이 사라짐 |
| 초기 계획·`harness-assessment.md` | 실행 권한 파일이 아닌 역사 기록; 현재 기준은 README/Controller라는 안내 존재 | 유지. 현재 운영 문서와 같은 권한 기준으로 사용하지 않음 |
| 의미 평가·검증 CLI와 fixture | 구조 성공이 의미 성공을 뜻하지 않아 별도 평가·검증이 필요; 실제 테스트/운영 참조 존재 | 유지 |
| 미추적 Task·dirty worktree·인증 파일 | 기존 승인·중단·운영 증거, 사용자 변경 | 정리 대상 제외 |

전체 파일 단위에서는 삭제해도 모든 지원 동작이 그대로라는 근거가 확인된 파일이 없었다.
파일 삭제는 0개이며, 차단된 내부 실행 분기와 사용하지 않는 import 7개를 제거했다.
운영 경로는 Controller의 두 Handler로 유지하고 수동 CLI는 같은 검증·복구 library를 사용한다.
상태 저장은 공통 journal, 실행 허가는 불변 authority, 재시도는 누적 budget으로 연결해 중복 판단을 줄였다.
새 서비스나 운영 진입점, 별도 패키지 구조는 추가하지 않았다.

유지한 `analyze_impact.py`도 별도로 보안 검토했다. 예전 Git 호출은 상속 설정·출력 크기·시간을
제한하지 않았으며 이동 가능한 ref를 여러 번 읽고 Git 오류·commit 메시지를 그대로 출력/저장했다.
같은 수동 Markdown 기능을 유지하면서 `impact_collection.Git`의 환경·명령·출력·시간 제한을 재사용하고,
한 번 해석한 SHA만 읽도록 교정했다. 내용 검사는 truncation 전에 수행하며 인식 가능한 token이 든
합성 commit 메시지는 보고서를 저장하지 않고 고정 오류 코드로 거부한다. 기존 수동 CLI 두 테스트를 보존했다.

## 제기된 8개 항목의 기준 코드 판정

| 지적 | 기준 코드 판정 | 완료한 교정·검증 |
|---|---|---|
| 1. 승인 후 재분석에서 checkpoint/attempt 충돌 | 내부 `resume_approved`에는 남아 있으나 공개 CLI는 PR #15에서 차단. 실제 Worker는 불변 승인 근거를 별도로 사용 | 내부 분기 제거. Worker 재개가 같은 세션·입력·승인·worktree·diff에 결합됨을 검증 |
| 2. ANALYZING의 ATTEMPT_LIMIT/RUNNING/재시도 | 다음 호출의 attempt 제한은 이미 실패 전이. RUNNING 크래시의 명시 복구는 미구현 | TIMEOUT/CODEX_FAILED/INTERRUPTED의 세 번째 실패를 즉시 종결. RUNNING은 종료 PID·관측 예산에 근거한 명시 복구 또는 abort. 자동 중복 호출 금지 |
| 3. evidence ID 경로 탈출 | manifest shape의 `$ref`와 anchored pattern이 실제 적용되어 `../escape`는 기존 테스트에서도 거부 | 경로 조합 직전에 독립 fullmatch 검사 추가. schema 검사를 모의로 우회한 경우에도 경로 탈출을 거부 |
| 4. non-JSON/error 이벤트 | 공식 0.153.2 소스에서 최상위 `error`는 치명 오류, `item.type=error`는 비치명 알림. 기존 parser도 후자를 실패로 처리하지 않음 | 이 분류를 유지하고 회귀 테스트 추가. 비 JSON stdout은 계속 거부. 도구 이벤트를 started/updated에서도 차단. 실제 CLI 재연결 출력은 미검증 |
| 5. 오류 분류와 preflight/launch | 실행 원인이 하나의 코드로 축약되고 환경 실패 처리도 비대칭 | 허용한 고정 원인 코드만 보존. preflight/실행 전 환경 실패는 모델 attempt 미소모. Controller 오류 한도에서 실패 종료 |
| 6. VALID 재호출 | 직접 `run`/CLI는 Codex를 다시 호출하고 레코드를 덮어씀 | 입력·결과·세션 재검증 후 기존 VALID 반환. 호출·attempt·budget·기록 변경 없음 |
| 7. pair/session checkpoint 저장·인터럽트 | 별도 Codex checkpoint 쓰기와 pair 사이에 크래시 틈 존재 | 표준/Codex checkpoint를 같은 pending journal로 저장·복구. PID와 누적 예산 보존. 인터럽트 및 thread 시작 실패에서 자식 프로세스 종료 |
| 8. 환경·빈 승인·schema·상위 링크 | proxy/CA env 누락, 빈 approvals raw 예외. 현재 nullable는 정상. 상위 링크 거부는 의도한 제한 | 신뢰된 proxy/CA 허용 목록, APPROVAL_REQUIRED, nullable 보존·미지원 schema keyword 명시 거부를 검증. 링크 제한은 유지·문서화 |

현재 nullable schema가 깨진다는 주장이나 evidence ID가 검증 없이 탈출한다는 주장은 기준 코드에서 재현되지 않는다.
상태 복구·VALID 재호출·고정 원인 코드 문제는 교정했다.
실제 인증된 Codex/OCI 실행 여부는 합성 transport 테스트와 구분해 기록한다.

상태/outcome별 다음 동작은 [분석 전이 표](codex-impact.md), [Worker 결과 표](sstc-worker.md),
[게시·검증 복구](sstc-pipeline.md)에 정리했다. 실패 종결 뒤에는 자동 재시도하지 않으며,
실행 결과가 불확실한 중단은 승인·관측 예산에 근거한 명시 복구가 필요하다.

## 전체 스크립트 리뷰에서 추가로 교정한 문제

- Controller가 같은 poll에서 발견한 여러 입력 중 일부만 journal에 저장한 뒤 중단하면
  Release/Issue를 유실할 수 있었다. 전체 batch를 먼저 예약하고 고정 Task ID로 이어간다.
  반복 환경·전송 실패도 현재 단계의 실패 상태와 checkpoint로 종결한다.
- Worker 작업 공간 생성·로컬 빌드 실패가 `IMPLEMENTING`에 남았다. 실패를 분류해 종결하고,
  로컬 성공은 `IMPLEMENTED`로 통일했다. Gradle 실행 비트는 검증 뒤 원래 모드로 복원한다.
  로컬 성공도 후보 Actions 검증과 Draft PR 조건을 대체하지 않는다.
- Slack 수동 launcher가 분석 없는 Task를 승인 대기로 만들 수 있었다. 연결 전과 checkpoint 전
  검증된 REQUIRED 분석·입력·세션을 대조하며 CRITICAL/새 SSTD 계약 요청을 거부한다.
  메시지는 Task와 결과 중 높은 위험도를 표시하고 기존 승인 사유의 token도 전송을 막는다.
- 인식 가능한 `xapp` token이 기존 scanner에서 빠져 있었다. Task 생성·상태 변경의 문자열,
  분석·증적·Slack 표시를 검사하고 고정 오류를 사용한다. 테스트에는 합성 token만 사용했다.
- Git replace object·외부 diff/textconv·hook·checkout filter로 고정 SHA의 관측 내용이나 실행이
  바뀔 수 있었다. 실제 객체 조회와 후보 diff 경계에서 차단하고 checkout filter는 사람 검토로 중단한다.
- 분석 reader thread 시작 실패가 자식 종료 없이 `Popen.__exit__`에서 대기하는 현상을
  실제 Python sleep 프로세스로 재현했다. 자식을 종료한 뒤 고정 원인 코드로 반환한다.

scanner는 인식 가능한 credential 형식을 탐지하는 경계이며 모든 종류의 비밀을 탐지한다고
보장하지 않는다. raw stderr/이벤트를 저장하지 않는 정책과 인증 환경 필터를 함께 유지한다.

## 추가로 확인한 게시 단계 복구

`PUBLISH_UNCERTAIN`은 기존 `publish`와 `check` 모두에서 다음 단계로 갈 수 없었다.
tree/commit/ref POST가 이미 전달됐을 가능성 때문에 재요청을 차단한 판단은 유지하고, 명시 복구와 종료 경로를 추가했다.

| 저장된 outcome | 허용된 다음 경로 | 상태/권한 |
|---|---|---|
| `PUBLISH_UNCERTAIN`, 후보 SHA·원격 ref 확정 | `run_sstc_pipeline reconcile`로 같은 tree·parent·ref·실행 근거·worktree 검사 | `VALIDATING` 전이만 복구, 새 원격 POST 없음 |
| `PUBLISH_UNCERTAIN`, 후보/ref 확인 불가 | `run_sstc_pipeline abort` | `IMPLEMENTATION_FAILED`, journal·승인·원격 artifact 보존 |
| `DISPATCH_UNCERTAIN`, Sensor run ID 없음 | 성공 완료된 run ID를 `run_github_validation reconcile`에 지정해 전체 receipt 대조 | 다른 Task/후보/attempt·대기/실패 run은 거부; 기존 journal 불변 |
| `DISPATCH_UNCERTAIN`, Sensor run ID 있음 | `check` | 같은 실행 조회, redispatch 없음 |
| `PR_UNCERTAIN` | 동일 head/base 단일 Draft PR을 `check`로 검증 또는 명시 `abort` | PR 중복 POST 없음; 확인 불가면 `BUILD_FAILED` 종료 |
| 게시 pair 저장 뒤 pipeline journal 갱신 중단 | 사전에 기록한 정확한 publication pair/snapshot으로 `reconcile` | 전이 중복 없음, 원래 승인 보존 |
| 명시 중단 journal 저장 뒤 pair 저장 전 중단 | 원래 snapshot·종료 pair를 `check` 또는 `abort`에서 대조 후 동일 pair 복구 | Sensor·원격 요청 없이 실패 종료; 승인/Task identity 변경 거부 |

local Worker 성공은 `IMPLEMENTED`와 명령·exit code·검증 계획·후보를 보존하면 게시할 수 있다.
원격 후보 Actions Sensor는 계속 필요하며, local 성공으로 Draft PR 생성 조건을 생략하지 않는다.
