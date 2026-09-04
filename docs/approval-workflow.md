# 승인 및 변경 영향 Workflow

## 초기 범위

초기 자동화는 **SSTC Draft PR 생성까지**로 제한한다.

AI는 SSTD 변경을 분석하고 SSTC branch에 수정한 뒤 검증 결과를 포함한 Draft PR을 생성할 수 있다. `main` merge, Release, production 배포는 자동화 범위에 포함하지 않는다.

## 처리 흐름

```text
SSTD main 또는 Release 감지
  ↓
변경 영향 분석
  ├─ NONE/LOW: 사전 승인 없이 분석·검증
  ├─ MEDIUM: 구현 후 Draft PR
  ├─ HIGH: Slack 승인 후 구현
  └─ CRITICAL: AI 제안만 작성, 사람 결정
  ↓
SSTC 수정 → build/test/lint → read-only review
  ↓
ax/sstc-sync/* branch의 Draft PR 생성
  ↓
사람이 review 및 merge
```

## 작업 입력 경로

```text
SSTD main/Release ──→ SSTD Change Handler ──┐
                                           ├→ 공통 Task Workflow
SSTC Issue/기능 요청 → SSTC Feature Handler ─┘
```

`SSTD Change Handler`는 SSTD와 SSTC 사이의 protocol·data model 영향 분석을 수행한다. `SSTC Feature Handler`는 기능 목적, acceptance criteria, UI 영향, protocol/dependency 변경 여부를 입력으로 받아 SSTC 자체 기능 작업을 생성한다.

SSTC 기능 요청이 protocol 변경을 요구하면 일반 기능 작업으로 계속 진행하지 않고 `PROTOCOL_APPROVAL_REQUIRED` 상태로 전환한다.

## 영향 등급

| 등급 | 예시 | 자동화 정책 |
|---|---|---|
| NONE | 내부 logger, build 정리 | 분석만 수행하거나 자동 PR |
| LOW | 문서, 포맷, 테스트 | 자동 수행 가능 |
| MEDIUM | DTO, parser, repository, ViewModel | 구현 후 Draft PR, review 필수 |
| HIGH | protocol, crypto, threading, 저장소 migration, UI 정책 | Slack 승인 후 구현 |
| CRITICAL | 인증 우회, secret, signing, production 배포, destructive migration | AI 자동 실행 금지 |

## UI Decision Gate

UI 영향이 감지되면 기술 계층 수정 전에 작업을 중지하고 Slack 승인 요청을 보낸다.

승인 요청에는 다음을 포함한다.

- SSTD source commit 또는 Release
- 감지된 변경과 영향 계층
- 기존 SSTC 화면
- UI 선택지와 장단점
- 추천안
- 작업 branch와 task ID

승인·거절·수정 요청은 task ID에 연결하며, Codex 프로세스는 대기하지 않고 종료한다. 승인 후 checkpoint를 사용해 새 실행을 시작한다.

## 상태

```text
RECEIVED → ANALYZING → WAITING_APPROVAL
                         ├→ REJECTED
                         └→ IMPLEMENTING → VALIDATING
                                             ├→ FAILED
                                             └→ READY_FOR_REVIEW → COMPLETED
```

## 결과 보고서

모든 작업은 다음 항목을 기록한다.

- Trigger와 입력 commit/Release
- 감지된 변경과 영향 분석
- 사람의 결정이 필요한 항목
- 수정 파일
- 실행한 build/test/lint/security 검증
- 미검증 영역과 Agent confidence
- 최종 Draft PR

## Slack 일일 보고

일일 보고는 승인 요청과 별도의 운영 알림이다. 보고 메시지 자체에는 작업을 승인·거절하는 실행 버튼을 포함하지 않으며, 승인 판단은 별도의 승인 요청 메시지에서만 수행한다.

일일 보고에는 다음을 포함한다.

- 보고 기준일과 시간대
- 새로 수신한 SSTD commit/Release
- 영향 분석 완료·실패 건수
- 승인 대기·거절·승인 건수
- 구현 중·검증 실패·완료 task
- 생성된 Draft PR과 현재 상태
- 다음날 처리 예정과 운영상 주의사항

보고 대상이 없더라도 `변경 없음`을 명시해 Controller가 정상 동작했음을 구분한다. 집계 오류나 Slack 전송 실패는 작업 실패와 구분하여 운영 로그에 기록한다.
