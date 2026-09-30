# SSTC GitHub Actions 검증 연결

`run_github_validation.py`는 SSTC의 `sstc-validation.yml`을 `main`에서 실행하고,
후보 commit SHA의 빌드 결과를 확인하는 독립 Sensor다. 저장소 push, commit,
PR 생성, Codex 실행, Task 전이 또는 승인 갱신을 수행하지 않는다.

## 실행 조건과 인증

- SSTC PR #2의 workflow가 기본 브랜치에 있어야 한다.
- Python 3.10 이상과 GitHub CLI `gh`가 필요하다. 외부 Python 의존성은 없다.
- Controller 계정의 GitHub CLI 인증을 사용한다. 해당 SSTC 저장소의
  Contents 읽기와 Actions 쓰기(실행 요청 포함) 권한이 필요하다.
- 운영자는 서버 터미널에서 `gh auth login`으로 인증한다. 자동 실행에서는
  해당 계정의 안전한 인증 저장소나 `GH_TOKEN` 환경변수를 사용할 수 있다.
  토큰을 요청 본문·저장소·Task·로그 또는 채팅에 기록하지 않는다.
- 이 CLI를 Codex Worker에서 호출하지 않는다. Worker의 인증정보 제거와
  `PUSH_AUTHORIZATION=NONE`, `PR_AUTHORIZATION=NONE` 제한을 유지한다.

## 요청과 결과 확인

기존 Task와 이미 SSTC 원격 저장소에서 접근 가능한 **40자리 후보 commit SHA**를 사용한다.
미커밋 diff나 브랜치명을 SHA 대신 전달할 수 없다. 후보 커밋 준비의 승인 절차는 별도다.

```bash
python -B scripts/run_github_validation.py request \
  --task-file state/tasks/<task-id>.json \
  --candidate-sha <40-character-sstc-commit-sha>

python -B scripts/run_github_validation.py check \
  --task-file state/tasks/<task-id>.json
```

요청 전에 `<task-id>.github-validation.json`에 Task·로그·기존 승인 checkpoint의
해시, workflow ID/SHA와 후보 SHA를 기록한다. 원래 Task·로그·checkpoint 파일은
변경하지 않는다. 실행 요청의 응답에서 run ID를 받아 같은 파일에 기록한다.
이 파일은 검증 요청의 checkpoint이며, 기존 승인 checkpoint를 대체하지 않는다.

`check`는 한 번만 조회하며 대기 루프를 만들지 않는다. `PENDING`이면 Actions
실행이 끝난 후 다시 확인한다. 실패 run은 `FAILED`와 종료 코드 1을 반환한다.
입력·권한·API·식별자·JSON 오류는 종료 코드 2와 원문 없는 오류 코드를 반환한다.

성공 출력은 `GITHUB_VALIDATION=VALIDATED`, `RUN_ID=...`, `AUTHORIZATION=NONE`이다.
`VALIDATED`는 빌드 Sensor의 결과이며 구현·push·PR·상태 전이 권한을 부여하지 않는다.

## 성공 인정 기준

- 현재 Task·로그·승인 checkpoint가 요청 snapshot과 일치한다.
- API run의 저장소·workflow 경로/ID·event·main 브랜치·workflow SHA·run ID/attempt가 일치한다.
- run과 `prepare`, `build`, `receipt` 세 job이 모두 완료·성공했다.
- 정확한 run ID/attempt의 artifact가 하나 있고 만료되지 않았으며 API SHA-256 digest와 ZIP이 일치한다.
- ZIP에는 크기 제한 내의 `validation.json`만 있다. 중복 JSON 키와 잘못된 JSON은 거부한다.
- JSON의 모든 필드가 요청과 검증 명령/JDK에 일치한다. 추가 필드도 허용하지 않는다.

수동 실행의 run head SHA는 workflow SHA다. 후보 SHA와 같다고 가정하지 않는다.
사용자가 run을 재실행하면 attempt가 달라지므로 기존 요청으로 인정하지 않는다.
실행 중 main 또는 Task snapshot이 바뀌어도 결과를 인정하지 않는다.

## 중단과 후속 작업

POST 전 기록한 `DISPATCH_UNCERTAIN`이 남으면 요청이 전송됐는지 불명확한 상태다.
다시 요청해 중복 실행하지 않는다. 운영자가 Actions와 기록을 대조해 판단한다.
기존 요청 파일이 있으면 재요청을 거부한다. 실패 기록을 삭제하거나 고쳐 성공으로 만들지 않는다.

OCI의 기존 `sstc-feature-20260916-0001` UI Task는 별도 복구 대상이다.
기존 diff, Codex 세션 ID와 승인 기록을 보존한다. 이 Sensor 성공만으로 실패 Worker를
재실행하거나 미커밋 변경을 push하지 않는다. Worker 구현/검증 분리, 승인된 후보 커밋 준비,
원격 검증 결과를 사용하는 Draft PR 생성기는 후속 작업이다.
UI의 실제 기기 세로·가로 확인은 사람이 수행한다.

기준: [workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[artifact API](https://docs.github.com/en/rest/actions/artifacts).
