# 저장소·Contract·Agent 운영 규칙

## 저장소 구성

```text
SST-Workspace/
├─ SSTD/       # Linux C++ daemon
├─ SSTC/       # Android client
├─ SST-AX/     # AX controller, policy, contract, state
└─ worktrees/  # Agent 전용 작업 공간
```

SST-AX는 SSTD와 SSTC의 코드를 복제하는 저장소가 아니라, 자동화 정책과 연결 계약을 소유하는 control-plane 저장소다.

권장 초기 구조:

```text
SST-AX/
├─ AGENTS.md
├─ README.md
├─ docs/
│  ├─ architecture.md
│  ├─ approval-workflow.md
│  ├─ security-model.md
│  ├─ repository-contract.md
│  ├─ task-state.md
│  ├─ operations.md
│  └─ adr/
├─ contracts/
├─ policies/
├─ templates/
├─ scripts/
├─ .agents/skills/
└─ .codex/agents/
```

`templates/daily-report.md`는 Slack 일일 보고의 표시 형식을 소유한다. 보고 집계 로직과 Slack 전송 설정은 Controller가 소유하며, 운영 환경별 channel ID와 credential은 저장소에 기록하지 않는다.

## Source of truth

```text
Machine-readable contract
        ↓
Architecture / ADR
        ↓
Requirements
        ↓
Implementation
        ↓
Published documentation
```

프로토콜 정의는 자연어 문서에만 두지 않는다. 향후 `contracts/protocol.yaml`과 호환성 검증기를 도입한다. 현재 SSTD의 기준은 protocol 문서 `2.0`, wire header `0x01`, CMake/GitHub Release `2.0.0`이다.

## Agent와 Skill

- `AGENTS.md`: 저장소에 항상 적용되는 장기 규칙
- Agent: 제한된 책임을 가진 실행 주체
- Skill: 반복 가능한 단일 Workflow
- Subagent: read-only 분석·검토처럼 분리 가능한 작업

초기 최소 구성:

```text
SSTD: sstd-dev, sstd-reviewer
SSTC: impact-analyzer, sstc-maintainer
SST-AX: impact-analyzer, protocol-reviewer, security-reviewer
```

동일 코드에 여러 write Agent를 병렬 실행하지 않는다. 분석·보안 검토 Agent는 read-only를 우선한다.

## Handler 입력 Contract

Controller는 작업 출처에 따라 다음 입력을 구분한다.

```text
SSTD Change Handler
- source: SSTD commit 또는 Release
- change_summary: 감지된 변경
- protocol_version: 관련 protocol 버전

SSTC Feature Handler
- source: GitHub Issue 또는 기능 요청
- requirement: 기능 목적과 acceptance criteria
- ui_impact: UI 변경 여부
- protocol_impact: protocol 변경 가능성
```

두 입력은 공통 Task schema로 변환되며, `source_type`으로 후속 처리와 일일 보고에서 구분한다.

## Git 규칙

- 자동화 branch: `ax/sstc-sync/*`
- AI는 `main`에 직접 push하지 않는다.
- AI는 merge하지 않는다.
- 작은 단위의 검토 가능한 commit을 만든다.
- SSTC 수정은 protocol/data model → parser/repository → ViewModel → 승인된 UI → tests 순서를 따른다.

## 일일 보고 Contract

Report Generator는 다음과 같은 구조화된 입력을 만들고, template이 이를 Slack 메시지로 변환한다.

```json
{
  "report_date": "<ISO-8601 date>",
  "timezone": "Asia/Seoul",
  "source_revisions": [],
  "source_types": {
    "sstd_change": 0,
    "sstc_feature": 0
  },
  "tasks": {
    "received": 0,
    "analyzed": 0,
    "waiting_approval": 0,
    "failed": 0,
    "draft_pr_created": 0
  },
  "draft_prs": [],
  "next_actions": []
}
```

보고 schema에는 secret이나 원문 credential을 넣지 않는다. 동일한 `report_date`와 집계 범위로 생성된 보고서는 한 번만 게시하도록 idempotency key를 사용한다.
