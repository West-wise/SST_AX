# Agent Execution Policy

> 이 문서는 SST-AX Controller가 강제해야 할 정책 기준이다. Controller 구현 전에는 설계 정책이며, 실행 환경에서 자동 강제된다고 주장하지 않는다.

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
| 최대 재시도 | 3회 | 초과 시 실패 상태와 증거를 남기고 에스컬레이션 |
| 최대 작업 시간 | 30분 | checkpoint 저장 후 중단·에스컬레이션 |
| 최대 write 단계 | 20회 | 초과 시 중단하고 변경 범위 검토 요청 |
| Codex 사용량 한도 | 계정 사용 가능 범위 | `DEFERRED_RATE_LIMIT`로 전이, busy-retry 금지 |

사용량 한도는 ChatGPT Plus의 표시된 리셋 시각을 기준으로 재개한다. Controller는 추정 사용량으로 권한을 확대하거나 새로운 계정을 사용해서는 안 된다.

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
