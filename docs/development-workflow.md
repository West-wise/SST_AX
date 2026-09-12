# 개발 작업 및 PR 분리

저장소 기여자의 운영 절차다. [AGENTS.md](../AGENTS.md), [실행 정책](../policies/agent-execution-policy.md), [승인 절차](approval-workflow.md), [Task 상태 규약](task-state.md)의 권한·승인·검증·중단 조건을 변경하지 않는다.

1. 쓰기 전에 Task state를 생성하고 주제별 범위·담당 파일·검증 방법을 정한다. 주 에이전트가 생성한 Task를 공유하는 위임 작업은 해당 Task와 연결하고, 작업마다 별도 branch/worktree를 사용한다.
2. 독립 PR은 파일 집합을 분리하고 각각 합의한 공통 base를 사용한다. 유용하면 병렬 위임하되, 같은 파일 수정이 필요하면 담당을 조정하거나 의존 PR로 전환한다. 별도 브랜치만으로 충돌 방지가 보장되지는 않는다.
3. 의존 PR은 선행 PR의 head를 후속 PR의 base로 지정한다. 예: 선행 `base: main / head: ax/topic-a`, 후속 `base: ax/topic-a / head: ax/topic-b`. 각 PR에 base/head·선행 PR·적용 순서를 명시하고, 선행 커밋을 후속 브랜치에 중복 cherry-pick하지 않는다.
4. 각 PR의 base 대비 커밋 목록과 파일 diff를 확인해 해당 주제의 변경만 포함하는지 검증한다. 사람이 선행 PR을 merge한 뒤에는 후속 브랜치의 base와 커밋 범위를 다시 확인하고, 필요하면 자신의 후속 커밋만 새 base에 재배치해 중복 노출을 피한다.
5. 합의한 범위의 실행·필수 검증이 완료되면 추가 확인 없이 주제별 Draft PR을 만든다. 제목·본문은 한국어로 작성하고 목적·변경 파일·검증 결과·미검증 사항·의존 관계를 기록한다. 위임 시 commit/push/PR 생성 등 명시적으로 제한된 단계는 수행하지 않고 주 에이전트에 diff와 검증 결과를 인계한다.

필수 사용자 결정·인증·권한 확대 또는 기존 정책의 승인·실패·한도 조건에 도달하면 checkpoint와 중단 이유·재개 조건을 남긴다. 이 절차는 승인 우회나 `main` 직접 push, PR merge, Release 생성, production 배포를 허용하지 않는다.
