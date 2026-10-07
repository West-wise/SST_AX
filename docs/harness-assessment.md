# SST-AX Harness Assessment

> 이 문서의 점수·미구현 목록은 각 기준일의 이력이다. 현재 완료 기준은
> [README](../README.md)와 [Controller](controller.md), 2026-10-06
> [목표 감사](original-goal-audit-20261006.md)를 사용한다. 역사적 점수를 현재 운영 성공률로 읽지 않는다.

## 2026-09-10 상태·복구 보강

기존 아래 평가는 초기 기준선이다. 현재 로컬 CLI는 위험도·승인 사유에 따른
구현 진입 차단, 승인 Gateway/검증 실행기 미구현 시 진행 거부, 대기 checkpoint,
reset 시각·재개 단계 검사, OS Task 잠금, journal 기반 state/log 복구를 구현했다.
외부 서비스 없이 승인 우회·검증 증거 누락·checkpoint 누락·동시 쓰기 거부와
저장 실패 주입·반복 복구를 테스트한다.

이는 Task 메타데이터의 복구 증거이며 SSTC 변경 복구나 Slack 인증 증거는 아니다.
전체 운영 성숙도 3/5는 아직 충족하지 않는다. 실제 Sensor, 실행 시간·재시도 한도,
Worker 권한 격리, 단일 Task의 Draft PR 완주 검증이 남아 있다.

> **평가 기준일:** 2026-09-04
> **목적:** SST-AX 하네스의 현재 성숙도를 기록하고, 외부 평가 전에 필요한 증거를 명확히 한다.
> **참고 프레임워크:** [Harness Engineering 6-Layer Guide](https://theaxlabs.com/blog/harness-engineering-6-layer-guide)

## 평가 범위와 기준

이 평가는 Agent 모델의 성능이 아니라, Agent가 안전하고 재현 가능하게 작업하도록 만드는 실행 환경을 평가한다.

| 등급 | 의미 |
|---|---|
| 0 | 미정의 |
| 1 | 문서상 원칙만 정의 |
| 2 | 설계와 처리 흐름이 정의됨 |
| 3 | 최소 구현 및 수동 검증 완료 |
| 4 | 자동 검증·실패 복구·운영 로그 확인 |
| 5 | 반복 운영과 측정으로 안정성 확인 |

이 문서의 평가는 저장소에 존재하는 문서와 구현물을 기준으로 한다. 향후 실제 실행 결과가 생기면 설계 점수가 아니라 증거로 재평가한다.

## 결론

현재 SST-AX는 **설계 성숙도 2/5, 운영 하네스 성숙도 1/5** 단계다. 최소 실행 하네스의 파일 기반을 추가했지만, 실제 Controller 실행과 검증 증거가 없으므로 운영 성숙도는 아직 올리지 않는다.

- 강점: 사람 승인 경계, 최소 권한, branch 기반 격리, checkpoint, Slack 보고, SSTD/SSTC 이중 진입점이 설계되어 있다.
- 한계: 실행 가능한 Controller, 강제되는 권한 정책, 구조화 로그, 실제 checkpoint, 자동 센서, 재시도·시간·사용량 한도가 아직 구현되지 않았다.
- 외부 평가 상태: **아키텍처·보안 설계 리뷰는 가능**하지만, **무인 자동화 또는 운영 신뢰성 평가는 아직 불가**하다.

## 6계층 평가

| 계층 | 현재 상태 | 점수 | 문서상 근거 | 외부 평가용 부족 증거 |
|---|---|---:|---|---|
| Guide | 저장소 규칙 파일 존재, 대상 저장소 명령 검증 전 | 2/5 | `AGENTS.md`, `architecture.md`, `security-model.md` | SSTD/SSTC에서 검증된 build/test/lint 명령 |
| Sensor | SSTD/SSTC 검증 Gate 설계 | 2/5 | `operations.md` | CI/로컬 실행 로그, schema·parser 테스트, 실패 검출 사례 |
| Agentic loop | Handler → 분석 → 승인 → 구현 → 검증 → Draft PR 설계 | 2/5 | `approval-workflow.md`, `architecture.md` | Controller 실행, 재시도 상한, timeout, 실패 에스컬레이션 결과 |
| Memory | schema와 checkpoint 보관 위치 정의 | 2/5 | `state/task-state.schema.json`, `operations.md` | 실제 state 파일, 중단 후 재개 복구 테스트 |
| Permission/Budget | 최소 권한·한도 정책 파일 정의 | 2/5 | `policies/agent-execution-policy.md`, `security-model.md` | 실행 가능한 allow/ask/deny 정책, 사용량 한도 처리, 권한 거부 시험 |
| Observability | Slack 승인·일일 보고·운영 지표 설계 | 2/5 | `operations.md`, `approval-workflow.md` | 구조화 로그, 보고 샘플, 중복 방지, 전송 실패 재시도, 트립와이어 |

## 현재 설계에서 잘된 점

### 1. 사람의 책임 경계가 명확하다

AI는 SSTC Draft PR까지 수행하고 `main` merge, Release, production 배포는 수행하지 않는다. 이 경계는 자동화의 폭발 반경을 제한하며, 외부 평가에서도 긍정적으로 볼 수 있는 핵심 근거다.

### 2. 입력 출처를 분리했다

`SSTD Change Handler`와 `SSTC Feature Handler`를 구분했다. SSTD 변경에 따른 동기화와 SSTC 자체 기능 요청을 같은 규칙으로 섞지 않으면서, 이후 공통 Task Workflow를 공유하도록 설계했다.

### 3. 승인 대기를 상태 기반으로 설계했다

Slack 승인 대기 중 Codex 프로세스를 유지하지 않고 checkpoint를 저장한 뒤 종료한다. 승인 후 새 실행으로 재개하는 방식은 장시간 프로세스 대기와 사용량 낭비를 줄인다.

### 4. 결정적 검증을 우선하는 방향이다

SSTC는 Gradle build, unit test, lint, parser/state/ViewModel test를 Gate로 정의했다. AI review는 이 검증을 대체하지 않고 보완해야 한다는 방향이 적절하다.

### 5. 사용량 한도에 대한 안전한 정지 원칙이 있다

Codex 사용량 한도 도달 시 반복 호출 대신 상태를 보존하고 리셋 후 재개하는 방향을 채택했다. 구현 시에는 이를 `DEFERRED_RATE_LIMIT` 상태로 강제해야 한다.

## 현재 설계에서 부족한 점

### Guide가 실행 가능한 규칙이 아니다

`AGENTS.md`는 추가됐지만 SST-AX 자체에는 build/test/lint 대상이 없다. 실제 명령은 SSTD와 SSTC의 저장소별 `AGENTS.md`와 CI에서 확인·검증한 뒤 입력해야 한다.

### Sensor가 설계에 머물러 있다

검증 Gate가 정의되어도 실행 명령, 성공 조건, 로그 보관 위치가 없으면 외부 평가자는 재현할 수 없다. AI self-review보다 build/test/lint/schema 검증을 먼저 구현해야 한다.

### 실행 한도가 없다

재시도 횟수, task timeout, tool 호출 수, 작업별 사용량 한도, 최대 파일 변경 범위가 아직 정의·강제되지 않았다. Plus 사용량 한도 도달 시 작업을 미루는 정책도 Controller 상태 전이로 구현되어야 한다.

### 권한이 문서상 정책이다

현재 최소 권한 원칙은 있으나, Worker의 파일 시스템·GitHub 토큰·Slack 권한이 실제로 제한된다는 증거가 없다. 권한은 prompt가 아니라 OS, token scope, Controller 정책으로 강제해야 한다.

### 관찰가능성이 운영 증거로 이어지지 않는다

일일 Slack 보고의 형식은 정의됐지만 실제 task log, report idempotency, 전송 실패 처리, 경보 조건은 아직 없다.

## 외부 평가 준비 상태

### 지금 제시할 수 있는 것

- SSTD의 Release 기반 CI/CD와 Jenkins 배포가 운영 검증되었다는 프로젝트 기준선
- SST-AX의 역할 분리와 승인 경계 설계
- SSTD/SSTC 이중 Handler와 공통 Workflow 설계
- 보안·상태·보고·검증 정책 문서

### 지금 제시하면 안 되는 주장

- SST-AX가 이미 무인 운영된다는 주장
- 멀티에이전트가 실제로 자율 협업한다는 주장
- Slack 승인·일일 보고가 실제 서비스에서 동작한다는 주장
- checkpoint 재개, 사용량 한도 대기, 권한 차단이 검증됐다는 주장
- 자동 생성 PR의 품질·성공률 수치

### 외부 평가자가 요구할 최소 증거

| 항목 | 합격 증거 |
|---|---|
| Guide | `AGENTS.md`와 실제 실행에 성공한 build/test/lint 로그 |
| Sensor | 의도적으로 실패시킨 변경을 test/lint/schema 검증이 차단한 기록 |
| Loop | 분석부터 Draft PR 생성까지의 단일 task 실행 기록 |
| Memory | 실행 중단 후 checkpoint에서 재개해 중복 작업 없이 완료한 기록 |
| Permission | main push·Release·secret 접근을 거부한 정책 또는 테스트 결과 |
| Budget | `DEFERRED_RATE_LIMIT` 전이와 리셋 후 재개를 보여 주는 task 기록 |
| Observability | 구조화 task log, Slack 일일 보고 샘플, 전송 실패 재시도 기록 |

## 외부 평가 전 최소 구현 순서

1. SSTD/SSTC 저장소별 `AGENTS.md`에 정확한 build/test/lint 명령을 실제로 검증해 기록한다.
2. read-only Impact Analyzer가 SSTD 변경 또는 SSTC Issue를 분석해 `task-state.schema.json`을 만족하는 task를 생성하게 한다.
3. SSTD/SSTC 검증 명령을 Controller가 실행하고 `task-log.json` 형식으로 결과를 저장한다.
4. task별 재시도, timeout, 사용량 한도, `DEFERRED_RATE_LIMIT` 상태를 구현한다.
5. GitHub와 Slack의 최소 권한 credential을 Controller에만 부여한다.
6. 구조화 로그와 Slack 일일 보고를 연결하고, 보고 중복·전송 실패를 검증한다.
7. 승인된 작업 하나를 분석부터 SSTC Draft PR까지 완주해 증거 묶음을 만든다.

## 첫 번째 외부 평가 시나리오

평가용 시나리오는 위험도가 낮고 재현 가능한 변경 하나로 제한한다.

```text
입력: SSTD의 문서화된 호환 가능한 telemetry field 추가
  ↓
SSTD Change Handler가 task 생성
  ↓
Impact Analyzer가 SSTC 영향 보고서 작성
  ↓
필요 시 Slack 승인
  ↓
SSTC Maintainer가 별도 branch에서 수정
  ↓
Gradle build/test/lint
  ↓
read-only Reviewer 검토
  ↓
Draft PR + 구조화 로그 + Slack 일일 보고
```

성공 기준은 “AI가 코드를 많이 만들었다”가 아니라 아래 네 가지다.

1. 사람이 요구사항을 다시 설명하지 않아도 task가 재개된다.
2. 검증 실패 시 Draft PR을 만들지 않는다.
3. 승인·권한·사용량 한도에 걸리면 안전하게 멈춘다.
4. 입력·결정·검증·산출물을 외부 평가자가 추적할 수 있다.

## 재평가 기준

다음 조건이 충족되면 운영 하네스 성숙도를 3/5로 재평가한다.

- `AGENTS.md`가 존재하고 실제 명령이 검증됨
- task state와 checkpoint 복구 시험 성공
- 최소 하나의 결정적 Sensor가 Controller에서 자동 실행됨
- 권한·재시도·timeout·사용량 한도가 코드 또는 설정으로 강제됨
- 단일 task가 Draft PR까지 도달한 구조화된 증거가 존재함

4/5 이상 평가는 서로 다른 입력 유형(SSTD 변경, SSTC 기능 요청)에서 반복 실행 성공률과 실패 복구 기록이 축적된 뒤에만 검토한다.

## 2026-09-05 구현 갱신

- `scripts/create_task.py`가 schema-valid 로컬 task state와 task log를 생성한다.
- `scripts/validate_task_state.py`가 task state에 사용되는 JSON Schema 부분 집합을 검증한다.
- `scripts/analyze_impact.py`가 SSTD 변경의 read-only Git 증적을 수집하거나 SSTC 기능 요청에 필요한 다음 분석 입력을 기록한다.
- `scripts/update_task_state.py`가 허용된 상태 전이만 적용하고 전이 이력과 종료 사유를 task log에 남긴다.
- 잘못된 SSTC 작업 branch prefix 거부를 포함한 집중 통합 테스트로 경로를 검증했다.

이는 **Memory** 구성 요소에 최소 실행 경로가 생겼다는 증적이다. 다만 대상 저장소 검증, checkpoint 복구, 권한 강제, Slack 전송, Codex 의미 분석, Draft PR 경로는 아직 실행되지 않았으므로 전체 운영 하네스 평가는 3/5로 올리지 않는다.
