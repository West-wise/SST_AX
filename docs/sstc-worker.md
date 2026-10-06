# 승인된 SSTC Worker

`run_sstc_worker.py`는 `IMPLEMENTING` Task만 받아 SSTC 저장소의 고정 revision에서 새
`ax/sstc-sync/*` worktree를 만들고, 승인된 Codex 세션을 `workspace-write`로 재개한다.
기존 SSTC worktree의 미커밋 변경은 보존한다. 새 worktree와 branch만 사용하며 `main`에
push하거나 PR을 만들지 않는다.

## 실행 조건

- `codex_impact.py`가 기록한 유효 결과와 입력 manifest가 있어야 한다.
- Slack Gateway가 만든 `WAITING_APPROVAL → IMPLEMENTING` 전이, checkpoint, 승인 nonce와
  snapshot hash를 `approved_session`이 다시 대조한다.
- 결과가 `CRITICAL`, `UNDETERMINED`, `NOT_REQUIRED`이거나 SSTC 요청에
  `protocol_contract`·`sstd_change_required` 영향이 있으면 실행하지 않는다.
- branch는 `ax/sstc-sync/<topic>` 형식이며 worktree 경로는 존재하지 않는 새 디렉터리여야 한다.
- SSTC source worktree가 dirty여도 이를 덮어쓰지 않는다. 분석 입력의 고정 revision에서
  별도 worktree를 만들며, source의 변경은 후속 사람이 검토한다.

## 실행

OCI 운영자가 실제 Task와 입력 경로를 사용해 실행한다.

```bash
python3 -B scripts/run_sstc_worker.py \
  --task-file state/tasks/<task-id>.json \
  --input-directory state/tasks/<task-id>.inputs \
  --sstc-repository /srv/SSTC \
  --worktree /srv/worktrees/<task-id> \
  --branch ax/sstc-sync/<topic>
```

기본값 `--validation-mode local`은 승인된 분석의 범위만 구현하고
`./gradlew testDebugUnitTest assembleDebug`를 실행한다. 출력 원문은 Task log에 저장하지 않고
exit code와 명령 receipt만 기록한다.

성공 출력은 `SSTC_WORKER=VALIDATED`, `PUSH_AUTHORIZATION=NONE`,
`PR_AUTHORIZATION=NONE`이다. 이 결과만으로 Draft PR을 만들거나 `READY_FOR_REVIEW`로
전이하지 않는다.

OCI에서 Android 빌드 환경 없이 구현하려면 같은 명령에 `--validation-mode github`을 추가한다.
이 모드는 기존 Slack 승인과 Codex 세션을 그대로 검증하며, 분석 manifest의 고정 SSTC revision만
사용한다. 분석 기록의 revision이 manifest와 다르면 작업을 시작하지 않는다. Worker와 Codex는
로컬 Android build·test·lint를 실행하지 않고, Controller가 GitHub Actions 검증을 요청한다.

Codex 구현이 완료되면 HEAD·branch·변경 파일을 검사하고 신규 파일과 삭제 파일을 포함한
후보 목록을 `codex_worker.candidate_files`에 기록한다. 성공 출력은 `SSTC_WORKER=IMPLEMENTED`이며
`validation`은 `null`, `validation_mode`는 `github`이다. Task는 `IMPLEMENTING`에 남고
`PUSH_AUTHORIZATION=NONE`, `PR_AUTHORIZATION=NONE`도 유지된다. 이 결과는 빌드·테스트 성공이나
push·PR 권한을 뜻하지 않는다. 후속 Controller가 승인 기록과 후보 파일을 다시 대조한 뒤
커밋·토픽 branch push·Actions 검증·Draft PR 생성을 진행한다.

실패하면 `codex_worker.outcome`과 validation exit code를 기록한다. timeout·중단·Codex
실패에는 자동 새 세션이나 자동 재승인을 사용하지 않는다. worktree를 삭제하지 않으며,
사람이 diff와 로그를 검토한 뒤 재시도 또는 새 승인 주기를 결정한다.
