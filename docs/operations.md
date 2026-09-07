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
  "task_id": "sstc-sync-<timestamp>",
  "status": "WAITING_APPROVAL",
  "source": "<sstd-commit-or-release>",
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

실패를 숨기고 다음 단계로 진행하지 않는다. 다음 상태를 구분해 기록한다.

```text
ANALYSIS_FAILED
IMPLEMENTATION_FAILED
BUILD_FAILED
TEST_FAILED
SECURITY_REVIEW_FAILED
UI_APPROVAL_REQUIRED
PROTOCOL_APPROVAL_REQUIRED
READY_FOR_REVIEW
```

자동화 변경은 branch/worktree에 격리한다. 실패 시 해당 worktree와 branch를 정리하며 `main`에 대한 rollback은 수행하지 않는다.

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
