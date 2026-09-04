# SST AX Codex CLI 아키텍처 계획

> SST-AX의 전체 설계 개요와 단계별 로드맵입니다. 세부 정책은 주제별 문서에서 관리합니다.

## 목표

SSTD 변경을 기준으로 SSTC의 영향을 분석하고, 승인된 범위에서 Codex CLI가 SSTC를 수정·검증하여 Draft PR을 생성하도록 합니다. 최종 책임과 merge/release 권한은 사람에게 둡니다.

## 현재 상태

| 항목 | 상태 |
|---|---|
| SSTD 실행 파일·프로토콜 문서 정비 | 완료(PR #9) |
| GitHub Release 기반 배포 | 완료(PR #10) |
| Release 테스트 키 권한 수정 | 완료(PR #11) |
| GitHub Actions Release 생성·검증 | 완료 |
| Jenkins Release 배포·health check·rollback | 완료 |
| 기존 SSTC의 SSTD 데이터 수신 | 정상 확인 |
| SST-AX 별도 저장소 | 구성 결정, 구현 전 |
| Slack 승인 Gateway | 미구현 |
| OCI AX Runner | 미구현 |
| SSTC 자동 동기화 | 미구현 |

## 확정 결정

- 승인 채널은 Slack으로 한다.
- Slack은 승인 요청뿐 아니라 일일 변경사항·자동화 상태 요약을 운영 채널에 보고한다.
- SSTD 변경과 SSTC 자체 기능 요청을 각각 `SSTD Change Handler`와 `SSTC Feature Handler`로 수신한다.
- 두 Handler는 초기에는 하나의 SST-AX Controller 내부에서 동작하며 공통 Task Workflow를 사용한다.
- 초기 자동화 범위는 SSTC Draft PR 생성까지다.
- SST-AX는 SSTD와 SSTC에서 분리된 별도 저장소로 만든다.
- 초기 실행 모델은 Codex CLI/`codex exec` 중심으로 검토한다.
- 승인 대기는 checkpoint 저장 후 프로세스를 종료하고, 승인 후 새 실행으로 재개한다.
- AI는 `main` merge, Release, production 배포를 수행하지 않는다.

## 구조

```text
SSTD main/Release
        ↓
SST-AX Controller
        ├─ Impact Analyzer
        ├─ Policy / Contract / State
        ├─ Slack Gateway
        └─ Codex Worker
                 ↓
          SSTC branch + Draft PR
                 ↓
             Human review
```

## 구현 로드맵

1. SST-AX 저장소의 `AGENTS.md`, README, 문서 구조 확정
2. SSTD/SSTC 저장소 계약과 machine-readable protocol contract 정의
3. read-only `sst-impact-analysis` PoC 구현
4. local Controller와 checkpoint/state schema 구현
5. OCI 전용 계정에서 Codex CLI 실행 검증
6. Slack 승인 요청·서명 검증·재개 workflow 구현
7. SSTC branch 생성, Gradle build/test/lint, Draft PR 생성
8. 실패 복구·감사 로그·운영 지표 추가
9. Slack 일일 보고 generator, schema, 중복 방지, 전송 실패 재시도 구현
10. SSTD Change Handler와 SSTC Feature Handler 입력·공통 Task schema 구현

## 세부 문서

- [아키텍처](docs/architecture.md)
- [승인 및 변경 영향 Workflow](docs/approval-workflow.md)
- [보안 모델](docs/security-model.md)
- [저장소·Contract·Agent 규칙](docs/repository-contract.md)
- [운영 설계](docs/operations.md)

## 미결정 사항

1. SST-AX state의 최종 저장 위치와 보존 기간
2. Codex 인증 방식: ChatGPT 기반 MVP와 장기 운영용 인증의 전환 시점
3. SST-AX의 GitHub App/Token 권한과 배포 방식
4. `contracts/protocol.yaml`의 실제 schema와 SSTD/SSTC 검증기
5. 일일 보고 발송 시각, Slack 채널 분리 여부, 보고 보존 기간
