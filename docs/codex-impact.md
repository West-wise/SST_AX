# Codex 읽기 전용 분석과 재개

Controller는 [고정 입력](impact-inputs.md)을 Codex에 전달하고 [결과 검증](impact-analysis.md)으로 구조·근거·입력 결합을 검사합니다. 분석 성공은 실행 허가가 아닙니다.

ANALYZING Task의 본문 해시를 재계산하고 stdin으로 임시 폴더에 전달합니다. `codex exec --sandbox read-only --json`과 output schema를 사용합니다. 사용자 config·execpolicy rules, shell·apps·plugins·web 도구를 제외합니다. `--ignore-rules`를 AGENTS 문서 무효화와 동일시하지 않습니다. 입력·저장소 텍스트는 분석 자료이며 AX 권한을 확대할 수 없습니다.

CLI·시스템 설정·저장된 로그인은 관리자 신뢰 영역입니다. Slack/GitHub token과 Git 제어 환경은 Worker에 전달하지 않습니다. CLI 설정은 OS 권한 격리를 대신하지 않으며 세션 저장소에는 입력이 남을 수 있습니다.

```bash
python -B scripts/run_codex_impact.py \
  --task-file state/tasks/<task-id>.json \
  --input-directory state/tasks/<task-id>.inputs
```

성공 출력은 CODEX_IMPACT=VALID, AUTHORIZATION=NONE입니다. 실행 정책이 입력·결과·세션을 재검증하여 완료·판단 유보·조건부 승인을 분기합니다.

thread.started의 ID를 즉시 checkpoint에 저장하고 정확한 ID로 재개합니다. `--last`나 자동 새 세션을 쓰지 않습니다. 정상 승인 후에는 Worker가 같은 세션으로 구현합니다. 과거 `--resume-approved` CLI는 승인 기록을 재분석으로 바꾸는 함정을 막기 위해 거부합니다. 후속 영향 재분석은 새 Task로 진행하고, 이전 구현 승인을 새 결과에 재사용하지 않습니다.

immutable 실행 근거와 mutable 실행 checkpoint를 분리하여 로그 추가만으로 승인을 폐기하지 않습니다. 동일 입력·범위·작업 공간을 재검증한 뒤 재개합니다.

분석 1회는 최대 10분, 이벤트 총 8 MiB·한 줄 1 MiB이며 Task 누적 예산도 적용합니다. 원시 JSONL·stderr를 저장하지 않습니다. 누락·잘림·UNKNOWN·질문은 확정 판단을 막고 사용량 제한은 DEFERRED_RATE_LIMIT입니다. 계정에서 확인한 실제 reset 시각을 기록한 뒤 그 시각 이후에만 재개합니다.

```bash
python -B scripts/update_task_state.py --task-file state/tasks/<task-id>.json \
  --record-reset-at '<실제로 확인한 ISO-8601 시각과 시간대>'
```

구현 중단은 [Worker 재개](sstc-worker.md), 저장 중단은 `--recover`를 사용합니다. 로컬 fake 응답 테스트·의미 사례 비교는 실제 AI 정확도나 OCI sandbox 검증을 대신하지 않습니다.
