# 승인된 구현부터 SSTC Draft PR까지

`run_sstc_pipeline.py`는 Controller에서 실행한다. Worker의 GitHub 인증정보와
push/PR 권한을 확대하지 않으며, Slack 승인 checkpoint와 기존 세션 ID를 재검증한다.
[Controller](controller.md)가 자동 연결한다. 아래 CLI는 점검·복구에도 사용한다.

## 조건

- SSTC의 `sstc-validation.yml`이 `main`에 있어야 한다.
- Controller 계정의 `gh` 인증에 SSTC Contents 쓰기, Actions 쓰기, Pull requests 쓰기 권한이 필요하다.
  인증정보는 Task·저장소·요청·로그에 기록하지 않는다.
- 입력·결과가 유효하고 실행 정책이 허용하거나 Slack Gateway가 승인한 `IMPLEMENTING` Task여야 한다.
- Worker는 새 worktree에서 같은 Codex 세션을 재개한다. OCI는 `--validation-mode github`를 사용한다.
  로컬 검증을 선택한 Worker도 성공한 명령·exit code receipt와 후보 파일을 보존한 `IMPLEMENTED`만 게시할 수 있다.
  로컬 성공 여부와 관계없이 게시한 후보 SHA의 Actions 검증은 필수다.
  실제 `thread.started` ID도 승인된 세션 ID와 같아야 한다.
- 소스 revision은 분석 manifest의 전체 SHA로 고정한다. source HEAD로 대체하지 않는다.

## 실행 순서

기존 Task·입력 디렉터리·SSTC 경로와 **존재하지 않는 새 worktree/branch**를 지정한다.

```bash
python -B scripts/run_sstc_worker.py \
  --task-file "$TASK_FILE" --input-directory "$INPUT_DIR" \
  --sstc-repository "$SSTC_REPO" --worktree "$SSTC_WORKTREE" \
  --branch "$SSTC_BRANCH" --validation-mode github
```

`SSTC_WORKER=IMPLEMENTED`는 구현·후보 파일 검사를 완료했다는 뜻이다.
Task는 `IMPLEMENTING`이고, Worker의 `PUSH_AUTHORIZATION`·`PR_AUTHORIZATION`은 `NONE`이다.
OCI에서 Java·Android SDK·Gradle을 실행하지 않는다.

```bash
python -B scripts/run_sstc_pipeline.py publish \
  --task-file "$TASK_FILE" --input-directory "$INPUT_DIR"
```

Controller는 실행 근거에 저장한 정책 허용 또는 승인 snapshot·nonce·입력·결과를 대조한다.
Worker가 완료한 변경과 현재 worktree가 같으면 Git Data API로 후보 tree와 commit을 만들고,
**새 `ax/sstc-sync/*` 원격 브랜치**를 등록한다. 기존 원격 브랜치를 갱신하거나 force push하지 않는다.
Git worktree의 HEAD·index·미커밋 변경은 보존하며, 원격 commit SHA를 후보로 기록한다.
Task를 `VALIDATING`으로 전이한 뒤 해당 SHA의 Actions 검증을 한 번 요청한다.

`SSTC_PIPELINE=PENDING`과 run ID가 나오면 Actions 완료 후 확인한다.

```bash
python -B scripts/run_sstc_pipeline.py check --task-file "$TASK_FILE"
```

`check`는 한 번만 조회한다. `PENDING`이면 완료 후 `check`만 다시 실행한다.
이 동안 Task·로그·승인 checkpoint·입력·결과·worktree를 변경하지 않는다.
검증 run/attempt·workflow SHA·후보 SHA·artifact digest·JSON과 원격 브랜치를 대조하고,
모두 통과하면 한국어 Draft PR을 생성한 뒤 `READY_FOR_REVIEW`로 전이한다.
일반 `update_task_state.py`로 이 상태를 지정하는 경로는 계속 거부한다.

## 후보 범위와 증거

- tracked·staged·untracked 파일, 삭제·이름 변경을 모두 후보에 포함한다.
- 초기 지원 범위는 `app/src/` 아래의 `.kt`, `.java`, `.xml` 일반 UTF-8 파일이다.
  `AndroidManifest.xml`, workflow, 빌드 설정, 지침 파일, 링크, submodule, binary,
  눈에 띄는 secret 경로·내용은 게시 전에 거부한다.
- 최대 20개 파일, 파일당 64 KiB, 합계 1 MiB로 제한한다. 범위 밖의 변경은 사람이 검토한다.
- private Git index로 계산한 전체 tree SHA와 GitHub tree SHA가 같아야 한다.
- 검증한 후보 SHA와 Draft PR의 head SHA가 같아야 한다. head가 바뀌면 진행하지 않는다.
- secret 검사는 알려진 패턴 검사이며 완전한 탐지나 변경의 의미상 정확성을 보장하지 않는다.
  UI 기기·화면 방향 확인과 코드 검토는 사람에게 남는다.

`<task>.pipeline.json`에 단계·후보·API 명령 receipt·승인 pair·Task snapshot을 보존한다.
`<task>.github-validation.json`은 기존 원격 Sensor의 결과 기록이다.
최초 승인 checkpoint·요청·승인 직후 pair는 execution-authority에 불변 보존하고 매번 재검증한다.
표준 checkpoint는 현재 중단·재개 상태를 담으며 원래 승인 근거를 대신하지 않는다.

## 중단과 복구

- 빌드/테스트 실패는 `BUILD_FAILED`로 기록하고 PR을 만들지 않는다.
- POST 전에 실행 checkpoint를 저장한다. tree/commit/ref 응답을 잃으면
  `PUBLISH_UNCERTAIN`에서 멈추고 사람이 원격 상태를 확인한다. `publish`를 반복하지 않는다.
  저장된 후보 SHA와 원격 branch·commit·tree·parent, 현재 실행 근거·worktree가 모두 같으면
  `reconcile`로 `VALIDATING` 전이만 복구한다. 이 명령은 원격 POST를 수행하지 않는다.
- dispatch 응답에 run ID가 남지 않으면 자동 재요청하지 않는다. Sensor에 정확한 run ID가
  저장된 경우에는 `check`가 그 실행을 이어서 확인한다. run ID를 확인했으면 Sensor의
  `reconcile`로 성공 완료된 실행의 Task·후보·workflow·attempt·artifact를 검증한 뒤 연결한다.
  실행 대기·실패·다른 Task의 receipt는 연결하지 않으며 기존 journal을 바꾸지 않는다.
- PR 응답을 잃으면 `PR_UNCERTAIN`을 보존한다. 다음 `check`는 동일 head/base의 단일 PR을
  조회해 Task·후보 SHA·Draft 여부·본문을 검증한다. PR POST를 반복하지 않는다.
- Task pair 저장 중단으로 `.pending.json`이 있으면 기존 `update_task_state.py --recover`
  절차로 복구하고 `check`를 재개한다. PR 생성 후 상태 기록 중단도 완료 receipt로 이어간다.
- OCI의 기존 `sstc-feature-20260916-0001`은 `WORKER_FAILED`이고 dirty worktree가 있다.
  Worker의 `--resume`가 원래 승인·입력·세션·branch·source·diff를 검증할 때 같은 Task를 이어간다.
  복구 근거가 없거나 범위가 바뀌면 사람이 새 승인 또는 새 Task를 결정한다.

응답을 잃은 게시 요청에서 이미 등록된 후보를 확인한 경우:

```bash
python -B scripts/run_sstc_pipeline.py reconcile --task-file "$TASK_FILE"
```

`DISPATCH_UNCERTAIN`으로 복구했지만 `<task>.github-validation.json`이 아직 없으면,
출력된 `CANDIDATE_SHA`를 사용해 `run_github_validation.py request`를 한 번 실행한다.
Sensor journal이 이미 있으면 재요청하지 않는다.

dispatch 응답을 잃어 run ID가 비어 있고, Actions에서 해당 실행 ID를 확인한 경우:

```bash
python -B scripts/run_github_validation.py reconcile \
  --task-file "$TASK_FILE" --run-id <확인한-run-id>
python -B scripts/run_sstc_pipeline.py check --task-file "$TASK_FILE"
```

후보 SHA가 확정되지 않았거나 원격 기록을 안전하게 연결할 수 없으면 명시적으로 중단한다:

```bash
python -B scripts/run_sstc_pipeline.py abort --task-file "$TASK_FILE"
```

`abort`는 `IMPLEMENTING`을 `IMPLEMENTATION_FAILED`, `VALIDATING`을 `BUILD_FAILED`로 끝낸다.
Task·pipeline/Sensor journal·승인 근거·원격 branch/PR을 보존하고 원격 요청을 보내지 않는다.
실패 Task에서 구현 승인을 다시 사용하지 않는다. 후속 수정은 별도 Task로 진행한다.

자동화 종료는 Draft PR과 `READY_FOR_REVIEW`다. merge·Release·production 배포는 사람이 수행한다.
