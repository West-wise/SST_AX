# SSTC Worker와 재개

Worker는 검증된 정책 허용 또는 Slack 승인 근거가 있는 IMPLEMENTING Task에서 고정 SSTC revision의 `ax/sstc-sync/*` worktree를 만들고 같은 세션을 재개합니다. source 미커밋 변경을 덮어쓰지 않습니다.

입력·결과·세션·정책·승인을 대조합니다. CRITICAL·UNDETERMINED·NOT_REQUIRED는 구현하지 않습니다. SSTC 요청에 새 SSTD 계약이 필요하면 중단합니다. SSTD_CHANGE는 이미 변경된 계약에 맞춘 SSTC parser/model/필드/단위/UI 적응을 허용하지만 SSTD는 수정하지 않습니다.

고정 commit의 AGENTS·CI를 읽고 지침 해시·검증 계획을 보존합니다. 지원 범위 밖의 dependency·permission·workflow 변경은 승인만으로 후보 허용 범위를 확대하지 않습니다.

```bash
python -B scripts/run_sstc_worker.py \
  --task-file "$TASK_FILE" --input-directory "$INPUT_DIR" \
  --sstc-repository "$SSTC_REPO" --worktree "$SSTC_WORKTREE" \
  --branch "$SSTC_BRANCH" --validation-mode github
```

최초 작업 공간·branch는 새 것이어야 합니다. github 모드는 OCI에서 Android 빌드 없이 구현하고 후보 검사 후 IMPLEMENTED를 기록합니다. 이는 빌드 성공이 아닙니다. Controller가 고정 후보 SHA를 게시하고 Actions 후 Draft PR을 만듭니다. local 모드는 대상 지침에 확인된 Gradle 검증을 실행합니다. Worker의 PUSH_AUTHORIZATION·PR_AUTHORIZATION은 NONE입니다.

기존 실행은 같은 명령에 `--resume`을 추가합니다. 같은 branch·worktree·source·HEAD·diff와 실행 checkpoint를 검증하며 같은 session ID를 사용합니다. recoverable 실패는 예산 안에서 재개하고 사용량 제한은 현재 checkpoint와 DEFERRED_RATE_LIMIT를 남깁니다. 실제 확인한 reset 시각 이후에만 진행하며 최초 승인 snapshot은 실행 근거에 불변 보존합니다.

기존 WORKER_FAILED도 원래 승인·입력·세션과 작업 공간이 복원 검증될 때만 재개합니다. 기록 부족·비정상 종료·3회 실패·예산 초과는 에스컬레이션하고 삭제나 성공 값 편집으로 우회하지 않습니다. [운영](operations.md), [후보 검증](sstc-pipeline.md)을 참조합니다.

이전 버전의 receipt에 실행 예산이 없으면 `--legacy-active-seconds`와
`--legacy-write-steps`에 운영자가 실제 확인한 누적 실행 시간·write/shell 단계 수를 함께
기록해야 합니다. 기록이 없다는 이유로 0을 추측하지 않습니다. 값을 확인할 수 없으면
자동 재개하지 않고 기존 diff를 보존한 상태에서 새 Task·승인을 검토합니다.
