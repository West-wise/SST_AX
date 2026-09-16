# SST-AX

SST-AX는 [SSTD](https://github.com/West-wise/Server_State_Telemetry_Demon)와 [SSTC](https://github.com/West-wise/Server_State_Telemetry_Client)를 연결하는 AI 기반 변경 영향 분석·자동화 저장소입니다.

SSTD의 프로토콜이나 데이터 모델이 변경되었을 때 SSTC에 미치는 영향을 분석하거나, SSTC 자체 기능 요청을 처리하여 승인된 범위에서 Codex CLI가 SSTC를 수정·검증하고 Draft Pull Request를 생성하는 것을 목표로 합니다.

## 핵심 원칙

- AI는 구현을 지원하지만 최종 책임과 의사결정권은 사람에게 있습니다.
- 프로토콜, 보안 정책, UI/UX, `main` merge, Release, 운영 배포는 사람의 통제 영역입니다.
- 모든 자동화 변경은 별도 branch와 Pull Request로 검토 가능하게 남깁니다.
- 승인 대기가 필요한 작업은 checkpoint로 상태를 저장하고, 프로세스를 종료한 뒤 승인 결과에 따라 재개합니다.

## 구현 범위와 목표

변경 입력 수집, 분석 결과 검증, Task 상태 복구, Slack 승인 Gateway와
Codex 읽기 전용 분석 CLI를 구현했습니다. 분석 CLI는 입력 증적의 해시를 확인하고
결과 JSON을 검증하며, Task 로그에 Codex 세션 ID를 연결합니다.
승인 후에는 checkpoint·승인 기록·입력·결과를 대조한 뒤 같은 세션을 읽기 전용으로 재개합니다.

최종 목표는 **SSTC Draft PR 생성까지**입니다. 아래는 목표 흐름이며,
SSTC 소스 수정·build/test/lint·Draft PR 자동 생성 연결은 후속 단계입니다.

```text
SSTD main/Release 또는 SSTC Issue
        ↓
SSTD Change Handler / SSTC Feature Handler
        ↓
입력 수집 → Codex 읽기 전용 분석 → 결과 JSON 검증
        ↓
필요 시 Slack 승인
        ↓
Codex CLI가 SSTC 수정
        ↓
Gradle build / test / lint
        ↓
SSTC Draft PR 생성
        ↓
사람의 review 및 merge
```

AI는 `main`에 직접 push하거나 PR을 merge하지 않습니다. Release와 production 배포는 기존 SSTD GitHub Actions·Jenkins 파이프라인의 책임입니다.

## 저장소 역할

```text
SST-Workspace/
├─ SSTD/       # Linux C++ 서버 데몬
├─ SSTC/       # Android 모바일 클라이언트
├─ SST-AX/     # AI 자동화 Controller, 정책, Contract, 상태
└─ worktrees/  # Agent 전용 작업 공간
```

SST-AX는 SSTD나 SSTC의 소스 코드를 복제하는 저장소가 아니라, 두 저장소 사이의 자동화 정책과 연결 계약을 관리하는 control-plane 저장소입니다.

## 현재 구성

```text
SST-AX/
├─ contracts/       # 영향 분석 결과·입력 manifest 규약
├─ policies/        # 권한·승인·위험도 정책
├─ templates/       # Task log 템플릿
├─ scripts/         # 수집·분석·검증·상태·승인 CLI
├─ state/           # Task schema, 로컬 Task·checkpoint
└─ tests/           # 합성 입력과 로컬 회귀 테스트
```

OCI 전용 계정을 실행 환경으로 사용합니다. Slack Gateway는 승인 응답을 검증하고,
독립 분석 CLI는 `codex exec`를 호출합니다. 승인·거절과 Task 형식은 OCI에서 확인했으며,
새 Codex 연결의 OCI 실실행과 분석 품질 검증은 운영자가 수행할 다음 단계입니다.
Slack 일일 보고와 상시 Controller 통합은 후속 범위입니다.

## 도입 로드맵

1. 구현: 저장소 규칙, Task 상태·checkpoint·저장 복구
2. 구현: 입력 수집, 영향 분석 결과 규약·검증 CLI
3. 구현·OCI 확인: Slack 승인 Gateway와 승인·거절 상태 전이
4. 구현·로컬 검증: Codex 읽기 전용 분석, Task·세션 연결, 승인 후 읽기 전용 재개
5. 다음: OCI Codex 실실행·sandbox 동작·분석 품질 검증
6. 후속: SSTD protocol contract 구체화, SSTC 수정·검증·Draft PR 자동 생성 연결
7. 후속: 상시 Controller, Slack 일일 보고, 운영 지표

## 문서

- [전체 아키텍처 계획](SST_AX_Codex_CLI_Architecture_Plan.md)
- [아키텍처](docs/architecture.md)
- [승인 및 변경 영향 Workflow](docs/approval-workflow.md)
- [보안 모델](docs/security-model.md)
- [저장소·Contract·Agent 운영 규칙](docs/repository-contract.md)
- [Task 상태 규약](docs/task-state.md)
- [영향 분석 입력 수집](docs/impact-inputs.md)
- [영향 분석 결과 검증](docs/impact-analysis.md)
- [Codex 읽기 전용 분석과 세션 재개](docs/codex-impact.md)
- [Slack 승인 실행 안내](docs/slack-quickstart.md)
- [운영 설계](docs/operations.md)

## 현재 상태

SSTD의 GitHub Actions 테스트·빌드·Release와 Jenkins 기반 배포는 운영 검증이 완료되었습니다. 기존 SSTC가 SSTD 데이터를 정상적으로 수신하는 것도 확인되었습니다.

입력 수집부터 읽기 전용 분석·결과 검증까지 독립 CLI로 실행할 수 있습니다.
결과 검증 성공이나 세션 ID는 구현 권한을 부여하지 않습니다. 권한 판단은 Controller가
보관하는 checkpoint와 승인 기록을 기준으로 하며, 최종 판단과 운영 배포 책임은 사람이 유지합니다.
