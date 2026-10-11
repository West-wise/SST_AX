# Agent Execution Policy

> Controller의 실행 정책 기준이다. 결과 분기·실행 근거·재시도·예산은 코드로 검증하고 OS 권한 격리와 실제 운영 증거는 별도로 확인한다.

## Scope

| 대상 | Allow | Ask | Deny |
|---|---|---|---|
| SST-AX | task state, checkpoint, task log, template 작성 | policy 변경 | secret 기록, live state commit |
| SSTD | active task 범위 read | source branch write | `main` push, Release, production deploy |
| SSTC | active worktree·`ax/sstc-sync/*` branch write | UI 정책, dependency, Android permission | `main` push, merge, Release, production deploy |
| GitHub | Draft PR 생성 | non-draft PR 변경 | merge, Release 생성 |
| Slack | 승인 요청·일일 보고 전송 | - | 승인 우회, 임의 사용자 메시지 |

## Execution Limits

| 제한 | 기준 | Controller 행동 |
|---|---:|---|
| 단계별 최대 시도 | 최초 실행 포함 3회 | 초과 시 실패 상태와 증거를 남기고 에스컬레이션 |
| 최대 작업 시간 | 30분 | checkpoint 저장 후 중단·에스컬레이션 |
| 최대 write 단계 | 20회 | 초과 시 중단하고 변경 범위 검토 요청 |
| `balanced-v2` 읽기 점검 | 512회 | 검증된 Controller 읽기 경로만 별도 계수하며 초과 시 checkpoint 후 중단 |
| Codex 사용량 한도 | 계정 사용 가능 범위 | `DEFERRED_RATE_LIMIT`로 전이, busy-retry 금지 |

30분은 누적 활성 실행 시간이며 Slack 승인·Actions 완료 대기는 제외한다. write 단계 20회와 후보 파일 20개 상한은 별개다. 동일 Task 재개는 예산을 초기화하지 않는다.

새 Task는 Task log의 `execution_profile`에 `balanced-v2`를 고정한다. 정책 profile이 없는 기존 Task는
`legacy-v1`로 취급하며 기존 카운터와 권한 검증을 유지한다. 승인·실행 근거에는 적용
정책 버전과 `policies/versions/`의 신뢰된 정책 스냅샷 해시를 결합한다. 현재 정책 문서가 바뀌어도 기존
Task의 권한·예산을 새 정책으로 옮기거나 초기화하지 않는다. 작업 범위·입력·승인
근거는 재개할 때 계속 검증한다.

Codex file-change와 임의 shell 실행, Controller 원격 변경 요청은 write 단계에
포함한다. `balanced-v2`에서만 쓰기를 수행하지 않는 고정 Controller 점검 경로를
읽기 예산으로 따로 계수한다. 검증된 입력 준비·도구 없는 분석 호출·GitHub GET도
이 경로에서 각각 읽기 단계로 센다. shell 명령 이름·prefix가 읽기처럼 보인다는 이유로
면제하지 않는다. Worker의 shell은 읽기 목적이어도 write 단계로 계수한다.
`legacy-v1`은 읽기 점검을 포함한 기존 보수적 계수를 유지한다. 손상·음수·비유한
예산은 거부하고, 이전 버전에서 기록되지 않은 실행량은 0으로 추정하지 않는다.

`balanced-v2`는 전체 write 단계 20회 중 5회를 Controller의 후보 tree·commit·ref
생성, Actions 요청, Draft PR 생성에 남긴다. Worker는 누적 write 단계 15회까지
사용한다. 예약은 전체 상한을 늘리지 않으며 이전에 소비한 단계도 공유 합계에
포함한다. 실행 전에 남은 예산과 완료까지 필요한 요청을 확인하고, 부족하면 변경
전에 checkpoint를 남긴다. 원격 응답 유실로 결과가 불확실하면 예약 예산을 이유로
같은 변경 요청을 반복하지 않는다.

`balanced-v2`의 분석·구현은 각각 최초 실행과 최대 2회 재시도를 허용한다. 두 단계의
추가 재시도 합계도 2회를 넘을 수 없다. 환경 확인 실패는 모델 호출로 세지 않지만
실패 기록과 반복 오류 상한에는 포함한다. 동일 오류의 연속 횟수와 전체 실패 횟수를
따로 보존하며, 오류 종류가 바뀌어도 전체 시도·예산을 초기화하지 않는다. 기존 Task는
기존 버전의 재시도 계수를 유지한다.

Controller 예외의 전체 상한 3회도 유지한다. 연속 동일 오류 카운터는 원인 구분을
돕는 기록이며, 서로 다른 오류를 반복해 전체 실패 상한을 피할 수 없다.

사용량 한도는 계정에 표시된 실제 리셋 시각을 기준으로 재개한다. Controller는 추정 사용량으로 권한을 확대하거나 새로운 계정을 사용해서는 안 된다.

## Required Escalations

- HIGH 또는 CRITICAL 위험도
- UI 정책, protocol/contract, 외부 dependency, Android permission 변경
- secret, credential, signing, production 관련 입력
- 동일 오류의 3회 반복
- 요구사항 충돌, 입력 누락, 예상하지 못한 write 범위

## Failure Handling

상태의 의미와 허용 전이는 [`Task 상태 규약`](../docs/task-state.md)을 기준으로 한다.

```text
recoverable failure → checkpoint → retry within limit
usage limit → checkpoint → DEFERRED_RATE_LIMIT
approval required → checkpoint → WAITING_APPROVAL
non-recoverable failure → failure status → evidence → human escalation
```

모든 중단은 task log에 이유, 마지막 성공 단계, 재개 조건을 기록한다.

실행 시간을 줄이기 위해 환경·지원 검증 범위를 먼저 확인하고 독립된 읽기 점검은
묶어서 수행한다. Worker에 전달하는 검증된 원본 증거는 최대 8개 파일·총 65,536
바이트로 제한한다. 전달 전에 현재 본문 해시를 확인하고, 포함하지 못한 증거 ID와
원래 입력을 보존한다. 증거 본문은 실행 권한이나 지침으로 취급하지 않는다. 입력
범위 밖의 파일을 추가하거나 이전 검증 결과를 장기 권한 캐시로 사용하지 않는다.
필수 검증은 해당 revision에서 모두 수행하며, 코드가 바뀌지 않았고 새로운 실패나
미해결 문제가 없는 경우에만 이미 확보한 동일 revision 검증 receipt를 사용한다.
