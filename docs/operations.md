# SST-AX 운영 설계

## 실행 방식

초기 Worker는 Codex CLI의 `codex exec`를 호출한다. Controller의 `SSTD Change Handler` 또는 `SSTC Feature Handler`가 입력과 task prompt를 준비하고, Worker는 전용 workspace에서 분석·수정·검증을 수행한다.

```text
Controller
  ↓
codex exec --sandbox read-only  # 영향 분석
  ↓
승인 또는 자동 진행
  ↓
codex exec --sandbox workspace-write  # 승인된 수정
  ↓
Gradle build/test/lint
  ↓
read-only review
  ↓
Draft PR
```

Codex가 Slack이나 승인 응답을 기다리며 장시간 살아 있지 않도록 한다. 승인 대기 시 checkpoint를 저장하고 프로세스를 종료한 뒤, 승인 결과와 checkpoint로 새 실행을 시작한다.

## 상태 저장

초기 PoC는 local state로 시작할 수 있다. 구현이 안정화되면 SST-AX의 상태 저장 정책을 확정한다.

```json
{
  "task_id": "sstc-feature-<YYYYMMDD>-<sequence>",
  "source_type": "SSTC_FEATURE",
  "source_reference": "<issue-url>",
  "status": "WAITING_APPROVAL",
  "risk_level": "HIGH",
  "created_at": "<ISO-8601 timestamp>",
  "updated_at": "<ISO-8601 timestamp>",
  "attempt": 0,
  "branch": "ax/sstc-sync/<name>",
  "checkpoint_path": "state/checkpoints/<task-id>.json",
  "approval_reason": "UI_CHANGE"
}
```

상태 파일에는 secret을 저장하지 않는다. task ID를 Slack 요청, 실행 로그, branch, PR에 공통으로 사용한다.

## Slack 일일 보고 운영

Report Generator는 Controller의 task state, 실행 로그의 요약 정보, GitHub PR 상태를 집계해 지정된 시간에 Slack 운영 채널로 전송한다. 보고서에서 `SSTD Change Handler` 작업과 `SSTC Feature Handler` 작업을 구분한다. 기본 시간대는 `Asia/Seoul`로 하되 구현 시 설정값으로 명시한다.

권장 보고 형식:

```text
[SST-AX Daily Report] 2026-09-01 (Asia/Seoul)

- SSTD source: v2.0.0 / <commit>
- SSTD change tasks: 2
- SSTC feature tasks: 1
- analyzed: 3
- waiting approval: 1
- validation failed: 0
- Draft PR created: 2
- next actions: UI 승인 1건 확인 필요
```

운영 규칙:

- 승인 요청은 발생 즉시 전송하고 일일 보고에 다시 요약한다.
- 보고에는 task ID, 상태, PR 링크 등 추적 가능한 정보만 포함한다.
- 처리 건수가 0이어도 `변경 없음` 보고를 전송한다.
- 같은 날짜·범위의 재시도는 중복 게시하지 않는다.
- 집계 실패와 Slack 전송 실패는 별도 상태로 기록하고 재시도한다.
- Slack 장애가 자동화 작업의 성공·실패 상태를 변경하지 않도록 분리한다.

## 검증 Gate

SSTD는 기존 GitHub Actions 검증을 기준으로 삼는다.

```text
Configure → Compile → multi-client test → amd64/aarch64 build
```

SSTC 자동 PR은 다음을 통과해야 한다.

```text
Gradle build → unit test → lint → protocol parser test
→ state/ViewModel test → UI test when applicable
```

Writer 실행 뒤에는 요구사항, unrelated change, regression, security, test gap, protocol mismatch를 확인하는 read-only review를 수행한다.

## 실패와 rollback

실패를 숨기고 다음 단계로 진행하지 않는다. 상태의 전체 정의와 허용되는 상태 전이(작업 상태 변경)는
[`Task 상태 규약`](task-state.md)을 기준으로 한다.

| 상태 | 의미 | 처리 | rollback 기준 |
|---|---|---|---|
| `ANALYSIS_FAILED` | 신뢰할 수 있는 영향 분석 실패 | 증적 보존 후 사람에게 에스컬레이션 | 대상 저장소 변경 없음 |
| `IMPLEMENTATION_FAILED` | SSTC 구현 실패 | 마지막 checkpoint와 실패 출력을 보존 | 격리된 worktree만 정리 가능 |
| `BUILD_FAILED` | 빌드 검증 실패 | Draft PR 생성 금지, 실패 증적 보존 | `main` rollback 없음 |
| `TEST_FAILED` | 테스트 검증 실패 | Draft PR 생성 금지, 실패 증적 보존 | `main` rollback 없음 |
| `SECURITY_REVIEW_FAILED` | 보안 검토 실패 | 작업 중단 및 사람 검토 요청 | 변경은 격리 상태 유지 |
| `WAITING_APPROVAL` | 사람 승인 대기 | checkpoint와 승인 사유 저장 후 프로세스 종료 | 실행 변경 없음 |
| `DEFERRED_RATE_LIMIT` | Codex 사용량 제한 | checkpoint 저장 후 reset 시각 이후 재개 | busy-retry 금지 |
| `PROTOCOL_APPROVAL_REQUIRED` | SSTD protocol/contract 결정 필요 | SSTC 구현 중지 후 사람 결정 요청 | 실행 변경 없음 |
| `READY_FOR_REVIEW` | 검증을 통과한 Draft PR 대기 | 사람 review로 전달 | 자동 rollback 없음 |

자동화 변경은 branch/worktree에 격리한다. 실패 시 먼저 diff·로그·checkpoint를 보존한 뒤 해당 worktree와 branch를 정리할 수 있으며, `main`에 대한 rollback은 수행하지 않는다.

## SSTD Release와의 관계

SSTD의 Release 및 production 배포는 SST-AX가 수행하지 않는다.

```text
SSTD main
  ↓
GitHub Actions test/build/package/Release
  ↓
Jenkins가 Release asset 다운로드·검증·배포
```

SST-AX는 SSTD Release 또는 승인된 source commit을 입력으로 받아 SSTC Draft PR을 생성하는 역할에 집중한다.

## 운영 지표

- 영향 분석부터 Draft PR까지의 소요 시간
- 자동화 작업 성공·실패율
- 검증 실패 유형
- 사람 승인 대기 시간
- PR 수정·반려 비율
- Codex 실행 횟수와 사용량
- false positive/negative 영향 분석 비율
- 일일 보고 생성·전송 성공률
- 보고 지연 시간과 재시도 횟수
# 로컬 Task 입력 (구현 완료)

첫 번째 실행 가능 SST-AX 경로는 로컬 전용이다. Codex, Slack, GitHub를 호출하지 않으며 SSTD/SSTC 대상 저장소에도 쓰지 않는다.

SSTC 기능 요청 Task를 생성하고 상태를 검증한 뒤 초기 read-only 증적 보고서를 생성한다.

```bash
python3 scripts/create_task.py \
  --source-type SSTC_FEATURE \
  --source-reference https://github.com/West-wise/Server_State_Telemetry_Client/issues/123 \
  --risk-level MEDIUM

python3 scripts/validate_task_state.py state/tasks/sstc-feature-YYYYMMDD-0001.json
python3 scripts/analyze_impact.py --task-file state/tasks/sstc-feature-YYYYMMDD-0001.json
```

SSTD 변경은 `analyze_impact.py`에 로컬로 clone된 SSTD 저장소와 source reference로 사용할 Git commit 또는 ref를 추가로 제공해야 한다.

```bash
python3 scripts/analyze_impact.py \
  --task-file state/tasks/sstd-sync-YYYYMMDD-0001.json \
  --source-repository ../Server_State_Telemetry_Demon
```

생성되는 task state, task log, impact report는 `state/tasks/` 아래에 남으며 Git에서 무시된다. 이는 저장소 산출물이 아닌 로컬 실행 증적이다. Impact report는 결정론적 Git 증적만 담고, Codex의 의미 분석은 다음 단계에서 추가한다.

## Task 상태 전이

상태 순서 검사 외에 [실행 조건](task-state.md)을 적용한다. 현재 승인 Gateway와
검증 결과 생성기가 없으므로 승인 필수 구현 및 `READY_FOR_REVIEW`는 차단된다.

`scripts/update_task_state.py`는 현재 상태에서 허용된 다음 상태로만 전이한다. 종료 상태, 승인 대기, 사용량 제한 대기는 `--reason`을 필수로 요구하며, 전이 이력과 종료 사유는 짝을 이루는 task log에 기록한다.

SSTD 변경의 분석을 시작하고, SSTC 영향이 없다는 결정론적 또는 승인된 분석 결과로 종료하는 예시는 다음과 같다.

```bash
python3 scripts/update_task_state.py \
  --task-file state/tasks/sstd-sync-YYYYMMDD-0001.json \
  --status ANALYZING

python3 scripts/update_task_state.py \
  --task-file state/tasks/sstd-sync-YYYYMMDD-0001.json \
  --status COMPLETED \
  --reason "SSTC 영향 없음"
```

`RECEIVED → COMPLETED`처럼 단계를 건너뛰는 전이와 종료 상태에서의 재개는 거부된다.

## 로컬 checkpoint와 저장 복구

사용량 제한 시 실제 확인한 reset 시각을 한국 표준시(KST, UTC+09:00)로 환산해
지정한다. 아래는 한국 시각 2026년 9월 11일 오후 2시를 나타내는 예시이며,
끝의 `+09:00`은 UTC보다 9시간 빠른 한국 시각임을 뜻한다.

```bash
python3 scripts/update_task_state.py \
  --task-file state/tasks/sstc-feature-YYYYMMDD-0001.json \
  --status DEFERRED_RATE_LIMIT --reason "Codex usage unavailable" \
  --deferred-until 2026-09-11T14:00:00+09:00
```

checkpoint는 `state/checkpoints/<task-id>.json`에 저장하며 입력 reference,
대기 상태, 전체 전이 로그, 중단했던 단계를 담는다. 커스텀 task 디렉터리에서는
그 부모의 `checkpoints/`에 저장한다. reset 이후 기존 `--status` 명령으로
중단했던 단계에 재진입하면 checkpoint 일치와 시각을 검사한다.

Task 생성·전이는 OS 파일 잠금으로 같은 Task의 동시 쓰기를 거부한다.
state/log를 바꾸기 전에 `<task-id>.pending.json`에 변경할 쌍을 기록한다.
저장 중단으로 pending 파일이 남으면 추가 전이를 거부하므로 다음으로 복구한다.

```bash
python3 scripts/update_task_state.py \
  --task-file state/tasks/sstc-feature-YYYYMMDD-0001.json --recover
```

복구는 pending의 state/log를 완성하는 roll-forward이며, 새 전이를 추가하거나
Codex를 실행하지 않는다. 반복 실행해도 전이 로그가 늘어나지 않는다.
checkpoint 불일치·손상은 임의 복원하지 않고 중단한다. checkpoint 저장 직후
pending 작성 전에 종료됐다면 기존 state/log가 유지되며 전이를 재요청할 수 있다.

현재 보장 범위는 로컬 Task 메타데이터와 프로세스 중단 복구다. SSTC Git revision,
diff, worktree 복원, 운영체제 장애·전원 손실 내구성, Slack 승인자 인증은 미검증이다.
상태 저장 영역은 Controller 전용으로 관리해야 하며 Worker 쓰기 권한에서 제외한다.
