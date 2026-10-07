# Slack 승인 연결 실험

Python 3.10 이상, 한 Task당 한 프로세스로 승인·거절을 검증한다. 공식 Slack SDK의
Socket Mode를 사용하므로 OCI에 공개 HTTP 수신 포트를 열 필요가 없다.
Codex 실행·일일 보고·상시 서비스화는 이 CLI에 포함하지 않는다.

## Slack App 설정

1. Socket Mode를 활성화한다. Basic Information → App-Level Tokens에서
   `connections:write` 토큰을 만든다.
2. Interactivity & Shortcuts를 활성화한다. Bot Token Scopes에는 `chat:write`를
   추가하고 워크스페이스에 설치·재설치한다. 승인 채널에 봇을 초대한다.
3. App ID, Workspace ID, 채널 ID, 승인자 Member ID를 확인한다.
   이름 대신 ID를 사용하며 승인자는 쉼표로 구분한다.

근거: [Socket Mode SDK](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/),
[메시지 전송 권한](https://docs.slack.dev/reference/methods/chat.postMessage/).

## OCI 실행

`ax-runner` 계정으로 저장소에서 실행한다. 토큰은 채팅·명령줄 인수·Git 파일에 넣지
않는다. 아래 `read -s` 입력은 화면과 shell history에 토큰을 남기지 않는다.
환경은 현재 shell에만 유지된다. Controller의 환경과 state는 Worker에 노출하지 않는다.

```bash
python3 -m venv /home/ax-runner/.venvs/sst-ax
source /home/ax-runner/.venvs/sst-ax/bin/activate
python -m pip install -r requirements-slack.txt

read -rsp 'Bot token: ' SLACK_BOT_TOKEN; printf '\n'
read -rsp 'App token: ' SLACK_APP_TOKEN; printf '\n'
export SLACK_BOT_TOKEN SLACK_APP_TOKEN
export SLACK_TEAM_ID='T실제ID'
export SLACK_CHANNEL_ID='C실제ID'
export SLACK_APP_ID='A실제ID'
export SLACK_APPROVER_IDS='U실제ID'
python -B scripts/slack_runner.py check
```

`SLACK_CONNECTED`는 Bot 인증과 Socket 연결 성공을 의미한다. 채널 전송·승인자
검증은 아래 실제 버튼 실험으로 확인한다. `check`는 메시지를 전송하지 않는다.

실제 Task는 연결 확인, checkpoint 생성, 승인 Listener 실행을 하나의 명령으로 처리한다.
토큰과 ID 환경변수가 없으면 터미널에서 입력을 요청한다. 토큰은 숨김 입력하며 파일,
명령줄 인자, Task 로그에 저장하지 않는다. 비대화식 실행에서는 모든 값을 환경변수나
비밀 저장소를 통해 주입해야 하며 누락된 값이 있으면 Task를 변경하기 전에 실패한다.

```bash
python -B scripts/request_slack_approval.py \
  --task-file "$TASK_FILE" \
  --reason UI_CHANGE
```

Task는 `ANALYZING` 또는 동일한 사유로 이미 생성된 `WAITING_APPROVAL` 상태여야 한다.
두 상태 모두 입력 증적·결과 hash·세션을 다시 검증한 `VALID` 분석과 `REQUIRED` 결과가
필요하다. 분석이 없거나 결과가 변했으면 Slack 연결과 Task 변경 전에 거부한다.
`CRITICAL`, 판단 유보, 수정 불필요 결과와 SSTC 요청에서 새 SSTD 계약이 필요한 결과는
구현 승인 대상으로 보내지 않는다. `ANALYZING`에서는 Slack 연결 확인에 성공한 뒤
`WAITING_APPROVAL` checkpoint를 만들고, 기존 대기 Task에서는 요청을 재사용한다.

버튼 검증에는 [Controller](controller.md)가 실제 입력을 분석하여 만든 승인 대기 Task를
사용한다. 수동 경로에서는 [입력 수집](impact-inputs.md)과 [읽기 전용 분석](codex-impact.md)을
완료한 실제 Task를 `$TASK_FILE`에 지정한다. 분석 없이 임시 Task의 상태만 승인 대기로
바꾸거나 `VALID` 레코드를 직접 작성하지 않는다. `--reason`에는 기존 승인 사유와 같은
값을 사용한다. Controller가 대기 승인을 처리 중이면 같은 Task의 별도 Listener를 실행하지 않는다.

Slack에서 승인하면 `IMPLEMENTING`, 거절하면 `REJECTED`를 출력하고 종료한다.
메시지는 검증된 분석 요약·영향 범위·출처와 고정 SHA·미해결 질문·만료 시각을 표시하며,
위험도는 Task와 분석 결과 중 높은 값을 사용한다. 승인은 구현 단계 진입만 허용한다.
이 CLI는 소스를 수정하거나 Codex를 실행하지 않는다. 화면 선택과 실제 기기 확인은
사람이 수행하며 merge·Release·배포 권한은 승인 버튼으로 부여하지 않는다.

## 재실행과 검증 범위

현재 state·log·checkpoint에 묶인 요청은 24시간 동안 유효하다. 중단 후 같은 명령을
재실행하면 유효한 기존 메시지를 재사용한다. 전송 성공 직후 기록 전에 중단되면 재실행 시
메시지가 중복될 수 있지만, 기록되지 않은 메시지의 버튼은 승인으로 인정하지 않는다.
Task·checkpoint 변경 후 이전 메시지, 만료 요청, 다른 사용자·채널·App의 응답은 무시한다.
`CRITICAL`은 승인 버튼으로 자동 구현을 허용하지 않는다.

상태 저장 중 실패하여 pending journal이 있으면 기존 복구 절차를 사용한다.

```bash
python -B scripts/update_task_state.py --task-file "$TASK_FILE" --recover
python -B scripts/validate_task_state.py "$TASK_FILE"
python -B -m unittest discover -s tests -v
```

복구 결과가 이미 `IMPLEMENTING`/`REJECTED`라면 다시 요청하지 않는다. 로컬 테스트는
가짜 Slack 응답으로 승인 경계를 검사하며 실제 OCI 연결 성공을 대신하지 않는다.
Socket 인증은 CLI가 담당하며 payload 파일을 받아 승인하는 CLI는 제공하지 않는다.
같은 OS 계정이 Controller 파일을 임의 변경할 수 있는 환경은 격리 경계가 아니다.
