# SST-AX

SST-AX는 [SSTD](https://github.com/West-wise/Server_State_Telemetry_Demon)의 변경이 [SSTC](https://github.com/West-wise/Server_State_Telemetry_Client)에 미치는 영향을 분석하고, 필요한 SSTC 수정을 검증하여 Draft PR로 만드는 제어 프로그램입니다. 독립 SSTC UI/UX 요청도 같은 Task 흐름으로 처리합니다.

## 기본 완료 기준

```text
SSTD main / Release ─┐
                    ├→ Task → 고정 입력·문맥 → Codex 읽기 전용 분석
SSTC [AX] Issue ─────┘                            ↓
                                    결과·근거·입력 결합 검증
                                    ├─ 수정 불필요 → COMPLETED
                                    ├─ 판단 유보 → 중단·추가 정보 요청
                                    └─ 수정 필요 → 정책 판정·필요 시 Slack 승인
                                                       ↓
                                      같은 세션으로 SSTC 구현
                                                       ↓
                                      후보 SHA의 Actions 검증
                                                       ↓
                                      Draft PR → READY_FOR_REVIEW
```

SSTD 내부 변경에 SSTC 영향이 없으면 PR을 만들지 않습니다. 계약뿐 아니라 수치·단위·범위·화면 표시 영향도 분석합니다. 이미 변경된 SSTD 계약에 맞추는 SSTC 수정과, SSTC 요청 때문에 SSTD 계약을 새로 작성하는 일은 구분합니다. 후자는 `PROTOCOL_APPROVAL_REQUIRED`에서 멈춥니다.

## 실행과 권한

- 하나의 Controller가 GitHub를 주기적으로 확인하고 두 Handler와 저장된 Task를 처리합니다. 웹 서버·DB·외부 큐를 요구하지 않습니다.
- 검증된 LOW/MEDIUM 중 승인 대상 영향이 없는 작업만 자동 구현합니다. HIGH, UI/protocol/dependency/permission/destructive 영향에는 Slack 승인이 필요합니다. CRITICAL은 자동 구현하지 않습니다.
- 입력·결과·세션·정책·승인 근거와 실행 중 바뀌는 checkpoint를 분리합니다. 같은 Task 재개는 기존 세션·branch·worktree·diff를 검증합니다. 입력이나 승인 범위가 바뀌면 이전 권한을 재사용하지 않습니다.
- Worker는 SSTC 작업 공간을 수정하고, Controller가 후보 게시·Actions·Draft PR을 처리합니다. 현재 후보 범위 밖의 dependency·permission 변경은 승인만으로 허용 범위를 넓히지 않습니다.
- AI의 main push·PR merge·Release·운영 배포는 금지합니다. 사람은 코드 review·merge와 실제 기기 UI 확인, Release·배포를 수행합니다.

## Slack 제품 범위와 구현 상태

원래 기획은 Slack 승인과 일일 변경 리포트, SSTD 변경과 독립적인 SSTC 기능 요청을 포함합니다. 개인 요청의 접수 채널은 사용자의 2026-10-09 요구에 따라 Slack으로 명확히 합니다. 전체 제품의 완료 보고에 다음 기능의 구현 상태도 포함합니다.

| 기능 | 현재 상태 |
|---|---|
| Slack 승인·거절 | 실제 메시지와 결정·실행 gate 확인 |
| Slack 일일 변경 리포트 | 미구현. 당일 변경·자동화 처리·검증·PR·실패·승인 대기 집계와 예약 전송 필요 |
| Slack 개인 요청 접수 | 미구현. 독립 SSTC 요청은 현재 GitHub `[AX]` Issue로 접수하며, Slack 입력 → SSTC Feature Handler → 공통 Task 연결 필요 |

일일 보고는 [초기 계획의 확정 결정·로드맵](SST_AX_Codex_CLI_Architecture_Plan.md), 독립 기능 요청과 보고 집계 항목은 [저장소 Contract](docs/repository-contract.md)에 명시돼 있습니다. Slack에서 받은 요청도 기존 분석·승인·검증·Draft PR 정책을 따릅니다.

## 검증 상태

로컬 회귀 테스트는 임시 Git·저장 복구와 가짜 Codex/GitHub/Slack 응답을 사용합니다. 결과 형식 검증, 사람이 정한 의미 평가 사례, SSTC 패킷·단위 테스트는 각각 다른 오류를 찾습니다. 컴파일이나 JSON 성공은 실제 AI 판단 정확도를 보장하지 않습니다.

2026-10-09(KST) 검증에서 실제 SSTD 고정 입력의 새 Task 재검증은 `VALID → COMPLETED / VERIFIED_NO_CLIENT_IMPACT`로 종료했습니다. 독립 SSTC UI 요청은 정상 Controller 접수 → 실제 AI 분석 → Slack 승인 → 같은 세션의 구현 → 후보 SHA의 Actions → [SSTC Draft PR #5](https://github.com/West-wise/Server_State_Telemetry_Client/pull/5) → `READY_FOR_REVIEW`까지 확인했습니다. 별도 새 Task의 실제 Slack 거절은 `REJECTED`로 종료했고 Worker·Draft PR 생성과 거절 후 실행이 차단됐습니다.

로컬 회귀 335개 통과는 [#20](https://github.com/West-wise/SST_AX/pull/20)·[#21](https://github.com/West-wise/SST_AX/pull/21)을 포함한 `d2804ac` 기준 기록입니다. 서버 변경 전체·숫자 범위·기대 동작을 명시한 새 가상 사례 두 개는 실제 AI 평가에서 MATCH였습니다. 원본 네 사례의 1 MATCH·3 MISMATCH와 기존 실패 기록은 보존했습니다. 테스트 수, 가상 평가, 실제 경로 완료 증거를 각각 구분합니다.

전체 1차 완료 판정은 보류합니다. 수정이 필요한 실제 SSTD 변경의 승인·구현·Actions·Draft PR 경로, 원본 의미 평가 불일치, 기기 화면 확인, 운영 계정 격리·장시간 운용 검증이 남아 있습니다. 상세 revision·Task·검증 범위는 [1차 검증 현황](docs/acceptance-20261009.md)에 기록했습니다. 로컬 Android 빌드 환경 실패는 후보 SHA의 Actions로 검증하며 미실행 테스트를 성공으로 기록하지 않습니다.

## 문서

- [Controller 설정·자동 실행](docs/controller.md) · [아키텍처](docs/architecture.md)
- [분석 분기·조건부 승인](docs/approval-workflow.md) · [Task 상태](docs/task-state.md)
- [입력 문맥](docs/impact-inputs.md) · [결과 검증과 의미 평가](docs/impact-analysis.md)
- [Codex 세션](docs/codex-impact.md) · [Worker·재개](docs/sstc-worker.md)
- [후보 검증·Draft PR](docs/sstc-pipeline.md) · [GitHub Sensor](docs/github-validation.md)
- [Slack 설정](docs/slack-quickstart.md) · [운영·복구](docs/operations.md) · [보안](docs/security-model.md)
- [원래 목표에 대한 감사와 교정](docs/original-goal-audit-20261006.md)
- [1차 검증 현황과 남은 항목](docs/acceptance-20261009.md)

`contracts/`는 입력·결과 계약, `policies/`는 실행 권한, `scripts/`는 실행기, `state/`는 로컬 상태, `tests/`는 회귀·평가 사례입니다. 실제 상태와 인증정보는 commit하지 않습니다. Slack 일일 리포트와 개인 요청 접수는 위 구현 현황에서 추적합니다. [초기 전체 계획](SST_AX_Codex_CLI_Architecture_Plan.md)은 구상 기록이며 현재 동작은 코드와 위 문서를 기준으로 합니다.
