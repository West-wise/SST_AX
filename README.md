# SST-AX

SST-AX는 [SSTD](https://github.com/West-wise/Server_State_Telemetry_Demon)와 [SSTC](https://github.com/West-wise/Server_State_Telemetry_Client)를 연결하는 AI 기반 변경 영향 분석·자동화 저장소입니다.

SSTD의 프로토콜이나 데이터 모델이 변경되었을 때 SSTC에 미치는 영향을 분석하거나, SSTC 자체 기능 요청을 처리하여 승인된 범위에서 Codex CLI가 SSTC를 수정·검증하고 Draft Pull Request를 생성하는 것을 목표로 합니다.

## 핵심 원칙

- AI는 구현을 지원하지만 최종 책임과 의사결정권은 사람에게 있습니다.
- 프로토콜, 보안 정책, UI/UX, `main` merge, Release, 운영 배포는 사람의 통제 영역입니다.
- 모든 자동화 변경은 별도 branch와 Pull Request로 검토 가능하게 남깁니다.
- 승인 대기가 필요한 작업은 checkpoint로 상태를 저장하고, 프로세스를 종료한 뒤 승인 결과에 따라 재개합니다.

## 현재 자동화 범위

현재 목표 범위는 **SSTC Draft PR 생성까지**입니다.

```text
SSTD main/Release 또는 SSTC Issue
        ↓
SSTD Change Handler / SSTC Feature Handler
        ↓
SST-AX 변경 영향·요구사항 분석
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

## 개발 예정 구성

```text
SST-AX/
├─ contracts/       # machine-readable protocol contract
├─ policies/        # 권한·승인·위험도 정책
├─ templates/       # 영향 분석·PR·승인 요청 템플릿
├─ scripts/         # Controller 및 검증 스크립트
├─ .agents/skills/  # 반복 자동화 Workflow
└─ .codex/agents/   # 역할별 Codex Agent 설정
```

초기 실행 환경은 OCI 서버의 전용 계정과 Codex CLI/`codex exec`를 기준으로 검토합니다. Slack은 승인 요청과 응답을 전달하고 일일 변경사항을 정리 및 요약해서 보고하는 채널이며, 외부 API 호출과 승인 검증은 Controller/Gateway가 담당합니다.

## 도입 로드맵

1. SST-AX 저장소 규칙과 문서 구조 확정
2. SSTD·SSTC 저장소 계약 및 protocol contract 정의
3. read-only 변경 영향 분석 PoC 구현
4. local Controller와 checkpoint/state schema 구현
5. OCI Codex Worker 실행 검증
6. Slack 승인 및 작업 재개 Workflow 구현
7. SSTC branch 수정·검증·Draft PR 생성
8. 실패 복구, 감사 로그, 운영 지표 추가

## 문서

- [전체 아키텍처 계획](SST_AX_Codex_CLI_Architecture_Plan.md)
- [아키텍처](docs/architecture.md)
- [승인 및 변경 영향 Workflow](docs/approval-workflow.md)
- [보안 모델](docs/security-model.md)
- [저장소·Contract·Agent 운영 규칙](docs/repository-contract.md)
- [Task 상태 규약](docs/task-state.md)
- [운영 설계](docs/operations.md)

## 현재 상태

SSTD의 GitHub Actions 테스트·빌드·Release와 Jenkins 기반 배포는 운영 검증이 완료되었습니다. 기존 SSTC가 SSTD 데이터를 정상적으로 수신하는 것도 확인되었습니다.

SST-AX Controller, OCI AX Runner, Slack Gateway, machine-readable protocol contract, SSTC 자동 동기화는 아직 구현 전입니다.
