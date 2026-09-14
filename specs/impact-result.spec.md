# 공통 영향 분석 결과 규약 — 요구사항

작성일: 2026-09-12 (KST, UTC+09:00)
상태: 공통 규약·로컬 검증기 구현. OCI 실서버 재검증과 실제 Codex 연결은 별도 단계다.
이 문서는 실행 권한이나 승인 정책을 변경하지 않는다.

## 목적과 범위

운영자가 SSTD 변경과 SSTC 자체 요청의 분석을 같은 기준으로 검토하고, Controller가
잘못된 형식·다른 작업의 결과·명시적 모순을 거부할 수 있게 한다.

- 선택한 범위: `SSTD_CHANGE`, `SSTC_FEATURE` 공통 규약·검증기·두 유형의 fixture.
- 구현 단위는 schema, 로컬 검증 CLI, 테스트다. 사용법은 [검증 문서](../docs/impact-analysis.md)를 따른다.
- 실제 Codex 호출은 후속 단계이며 첫 시연은 SSTD 변경 분석으로 한다.
- 제외: Slack 연결, 일일 보고 구현, 자동 상태 변경, SSTC 수정, Draft PR 생성.
- 새로운 외부 패키지 없이 Python 표준 라이브러리를 기준으로 한다.

사용자 요구: 운영자로서 입력·근거·불확실성과 승인 사유를 확인하여,
잘못된 AI 결과로 자동 작업이 진행되는 것을 막고 싶다.

## 현재 기준선

기준 코드: main의 `987798d` (PR #4 병합).

| 항목 | 확인된 수준 | 아직 미구현/미검증 |
|---|---|---|
| Task | 생성·검증 CLI | 외부 요청 자동 수신 |
| SSTD 증적 | analyze_impact.py의 파일 목록·Git 요약 | patch 의미 분석·AI 결과 검증 |
| SSTC 요청 | 요청 본문·저장소 지침 필요 안내 | Issue 자동 수집·의미 분석 |
| 상태·복구 | 잠금, state/log journal, checkpoint와 재개 조건 | 실제 Git/worktree 복원 |
| 승인·검증 | 검증된 승인·검증 결과 생산자가 없으면 관련 진행 차단 | Slack Gateway·자동 위험 분류 |
| OCI | 사용자 출력에서 Python 3.10.12 테스트 13개 성공, 모의 reset 후 ANALYZING 재개와 state 검증 성공 | 실제 Codex 한도 감지·예약 실행 |

OCI 확인은 사용자 제공 출력에 근거하며 문서 작성 중 직접 재검증한 결과가 아니다.

## 입력과 신뢰 경계

운영자/Controller가 고정한 Task·입력 manifest·증적과 비신뢰 분석 결과를 비교한다.
manifest는 분석에 전달한 입력 목록이다. AI가 결과와 함께 만든 manifest를 신뢰 기준으로
사용하지 않는다. 초기 테스트는 사람이 준비한 고정 fixture를 사용하며 수집기 연결은 후속이다.

| 입력 | 고정할 식별 정보 |
|---|---|
| 공통 | Task ID, source type/reference, SSTC 대상 commit SHA, 증적 ID 목록 |
| SSTD_CHANGE | SSTD 비교 기준·대상 commit SHA; tag/Release는 commit으로 해석 |
| SSTC_FEATURE | 요청 reference, 분석에 사용한 요청 본문 snapshot의 SHA-256 |

merge commit은 비교 기준을 명시한다. 초기 commit은 기준을 명시적 null로 표현하고 전체
생성 diff를 사용한다. 이동 가능한 ref나 Issue URL만으로 입력 동일성을 판정하지 않는다.
요청 hash는 저장한 UTF-8 snapshot의 원본 바이트를 기준으로 한다.

증적에는 ID, 출처, revision/snapshot 식별값, 코드의 저장소 상대 경로, 종류, 내용 SHA-256,
필수 여부와 누락·잘림 여부를 둔다. 삭제 파일은 기준 revision을 참조할 수 있다.
manifest는 로컬 파일을 수정할 수 있는 공격자에 대한 인증 장치가 아니다.

## 결과 필드 초안

세부 JSON 모양은 [schema](../contracts/impact-analysis.schema.json)와 fixture로 고정한다.

| 필드 | 의미 |
|---|---|
| schema_version | 지원하는 규약 버전 |
| task_id, source_type | Task·manifest와 일치 |
| input_context | 고정된 입력 식별 정보와 일치 |
| change_required | REQUIRED / NOT_REQUIRED / UNDETERMINED |
| summary | 비어 있지 않은 결론과 이유 |
| risk_level | NONE / LOW / MEDIUM / HIGH / CRITICAL; AI 제안값 |
| evidence | manifest 증적 ID 참조와 근거 설명 |
| impacts | 아래 범주별 PRESENT / ABSENT / UNKNOWN, 이유와 증적 참조 |
| approval_reasons | 영향에 대응하는 사유 목록 |
| unresolved_questions | 결론·승인 범위를 확정하지 못하게 하는 질문·누락 정보 |

| PRESENT 영향 또는 위험도 | 필요한 사유 |
|---|---|
| ui_ux | UI_CHANGE |
| protocol_contract | PROTOCOL_CHANGE |
| dependency | DEPENDENCY_CHANGE |
| android_permission | ANDROID_PERMISSION_CHANGE |
| destructive_action | DESTRUCTIVE_ACTION |
| sstd_change_required | SSTD_CHANGE_REQUIRED |
| HIGH | HIGH_RISK |
| CRITICAL | CRITICAL_RISK |

sstd_change_required는 요청을 충족하려면 SSTD를 추가로 수정해야 하는지를 뜻한다.
이 목록은 기존 Task의 approval_reason 문자열을 교체하지 않는다.
CRITICAL_RISK도 승인하면 자동 구현해도 된다는 의미가 아니다.

## 기능 요구사항 (EARS)

- **FR-001:** 두 Handler의 결과를 받을 때, 검증기는 공통 규약과 source type별 입력 식별 조건을 적용해야 한다.
- **FR-002:** JSON을 읽을 때, 검증기는 중복 key, 미지원 버전·필드·enum, 잘못된 타입, 필수 필드 누락, 공백뿐인 필수 설명을 중첩 객체까지 거부해야 한다.
- **FR-003:** 결과 식별 정보가 Task/manifest와 다를 때, 검증기는 거부하고 기준 입력을 결과에 맞춰 갱신해서는 안 된다.
- **FR-004:** 확정 결론을 제출할 때, 결과는 하나 이상의 유효한 증적 참조와 결론 이유를 포함해야 한다. 미등록·다른 입력의 증적을 받으면 거부해야 한다.
- **FR-005:** 필수 입력 누락·잘림, UNKNOWN 영향, 미해결 질문 중 하나라도 있을 때, 결과는 UNDETERMINED와 비어 있지 않은 미해결 질문을 포함해야 한다. 이를 어긴 확정 결론은 거부해야 한다.
- **FR-006:** NOT_REQUIRED일 때, 모든 영향은 ABSENT, 위험도는 NONE, 승인 사유·미해결 질문은 빈 목록이어야 한다. 모순은 거부해야 한다.
- **FR-007:** 영향이 PRESENT이거나 위험도가 HIGH/CRITICAL일 때, 대응 사유가 없으면 거부해야 한다. ABSENT 영향에 대응 사유를 함께 기록한 경우도 거부해야 한다.
- **FR-008:** 모든 실행에서, 검증기는 Task·위험도·승인·저장소 파일을 변경하거나 Codex·Slack·GitHub·구현 명령을 실행해서는 안 된다.
- **FR-009:** 읽기·파싱·검증 실패 시, CLI는 비정상 종료와 필드 위치 중심 오류를 반환하고 비신뢰 원문을 통째로 출력하거나 성공 표시해서는 안 된다.
- **FR-010:** 적합한 UNDETERMINED를 받을 때, 검증기는 규약 유효와 판단 유보를 구별하고 구현 가능 판정으로 표현해서는 안 된다.

특별 승인 범주를 건드리지 않는 내부 수정은 REQUIRED이면서 모든 영향이 ABSENT일 수 있다.
CI 파일 경로라는 사실만으로 NOT_REQUIRED를 정당화할 수는 없다.

## 기존 정책과의 관계

상태 전이(작업 상태 변경)는 [Task 상태 규약](../docs/task-state.md)과
[승인 Workflow](../docs/approval-workflow.md)를 따른다. 이번 검증기는 상태를 선택하지 않는다.

- AI의 낮은 위험도 제안으로 기존 위험도·승인 요구를 낮추지 않는다.
- SSTC 요청이 SSTD protocol/contract 변경을 요구하면 PROTOCOL_APPROVAL_REQUIRED로 중단하는 기존 정책을 유지한다.
- UI/UX 등 승인 대상은 Slack 결정 전 구현하지 않는다. CRITICAL은 자동 구현하지 않는다.
- UNDETERMINED는 입력 보완·사람 판단 대상이며 구현·완료 근거가 아니다.
- NOT_REQUIRED 형식 검증만으로 COMPLETED를 자동 허용하지 않는다. 상태 연결과 증거 요구 강화는 후속 범위다.

## 비기능 요구사항

- 호환성: OCI Python 3.10.12와 Windows에서 동일 fixture의 판정이 같아야 한다.
- 결정성: 동일 입력은 모델·네트워크·현재 시각 없이 동일 판정과 종료 코드를 내야 한다.
- 자원 제한: 초기 제안값은 JSON 파일당 1 MiB, 중첩 깊이 32다. 초과 시 명시적으로 거부한다. 실행 속도는 OCI에서 측정하며 측정 전 수치를 보장하지 않는다.
- 보안: 결과의 명령·URL을 실행하거나 열지 않는다. 코드 증적은 저장소 상대 경로만 허용하고 절대 경로·상위 경로 탈출을 거부한다. 참조 경로의 파일을 임의로 읽지 않는다.
- 기밀성: fixture는 합성 데이터만 사용한다. credentials·private key·credential-bearing 원문 로그를 결과·오류·Git에 남기지 않는다. 완전한 secret 탐지기로 주장하지 않는다.
- 호환 변경 최소화: 기존 Task schema·CLI를 바꾸지 않는다. 현재 validator의 JSON Schema 부분집합을 완전한 중첩 검증기로 간주하지 않는다.

## 수용 기준 (Given / When / Then)

| ID | Given — 준비 | When — 실행 | Then — 기대 결과 |
|---|---|---|---|
| AC-01 | 두 source type × 필요·불필요·유보 fixture 6개 | 공통 검증 | 모두 유효, 결론 구별 |
| AC-02 | Task ID·type·revision·요청 hash를 각각 바꾼 결과 | manifest와 비교 | 각각 거부 |
| AC-03 | 근거 없는 확정 결론·미등록 증적 | 검증 | 거부 |
| AC-04 | 필수 입력 누락·잘림·질문·UNKNOWN | 확정 결론 검증 | 거부; 적합한 유보는 허용 |
| AC-05 | NOT_REQUIRED와 PRESENT·승인 사유·HIGH의 조합 | 검증 | 거부 |
| AC-06 | 각 PRESENT 영향의 대응 사유 제거 | 검증 | 각각 거부 |
| AC-07 | HIGH/CRITICAL의 사유 누락 | 검증 | 거부; 사유가 있어도 실행 허가는 아님 |
| AC-08 | 중복 key·추가 필드·중첩 필수 필드 누락·잘못된 타입/버전 | 검증 | 모두 거부 |
| AC-09 | 손상 JSON·없는 파일·크기/깊이 초과 | CLI 실행 | 비정상 종료, 안전한 오류, 성공 표시 없음 |
| AC-10 | 경로 탈출·명령 형태 설명 | 검증 | 경로 거부, 명령 실행 없음 |
| AC-11 | 성공·실패·유보 입력과 실행 전 파일 hash | 실행 후 비교 | Task/log/checkpoint·SSTD/SSTC 무변경, 외부 호출 없음 |
| AC-12 | 기존·신규 테스트 | Python 3.10.12와 Windows 실행 | 기존 13개와 신규 테스트 성공 |

형식 검증은 의미적 정확도를 보증하지 않는다. AI가 영향을 잘못 ABSENT로 쓰거나
그럴듯한 근거를 붙이는 문제는 구조 검사만으로 잡을 수 없다. 실제 Codex 연결 단계에는
사람이 정답을 정한 CI-only 변경·protocol 변경·UI 요청·정보 부족 사례로 별도 평가한다.
그 평가 전에는 분석 품질 점수나 하네스 성숙도 3/5 달성을 주장하지 않는다.

## 오류 처리

| 조건 | CLI 결과 | 후속 처리 |
|---|---|---|
| 적합한 확정 결과 | 종료 0, 규약 유효와 결론 표시 | 검토만 가능, 상태 변경 없음 |
| 적합한 유보 | 종료 0, UNDETERMINED 명시 | 질문 검토, 구현 진행 금지 |
| 형식·입력 불일치·모순 | 종료 1, 오류 코드·필드 위치 | 소비 중단, 자동 수정·재시도 없음 |
| 읽기·파싱·자원 제한·CLI 사용 오류 | 종료 2, 안전한 요약 | 입력/환경 점검, 상태 변경 없음 |

초기 CLI는 판정만 출력하고 성공 artifact를 생성하지 않는다.
대상 hash·종료 코드를 감사 로그에 기록하는 Controller 연결은 후속 단계다.

## 구현 체크리스트

- [x] manifest·증적과 결과 schema의 정확한 형태·필수 필드를 고정한다.
- [x] 두 Handler의 정상 6개와 AC-02~10 실패 사례를 작성하고 검증한다.
- [x] 중복 key·자원 제한 로더와 중첩 검증을 구현한다.
- [x] 입력 일치·증적 참조·영향/사유/결론 교차 검증을 구현한다.
- [x] read-only CLI, 종료 코드, 파일 무변경·외부 호출 금지 테스트를 검증한다.
- [x] 기존·신규 테스트의 Windows·로컬 Linux 검증을 수행한다 (각 51개 통과).
- [ ] OCI 실서버에서 신규 테스트를 재검증한다.
- [x] 운영 문서에 검증 범위와 미구현 연결부를 반영한다.

문서 단계의 완료는 공통 범위·요구사항·수용 기준·현재 구현과의 경계를 남기는 것이다.
위 구현 완료와 혼동하지 않는다. 크기·깊이 제한과 세부 JSON 모양은 구현 시 fixture로
검토할 기본값이며, 승인·권한·외부 의존성 정책 변경은 별도 사용자 결정이 필요하다.
