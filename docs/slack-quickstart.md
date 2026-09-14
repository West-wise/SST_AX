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

기존 작업과 분리한 임시 Task를 만든다. 출력의 Task ID를 `AX_TASK_FILE`에 사용한다.

```bash
AX_TEST_ROOT=$(mktemp -d /tmp/sst-ax-slack.XXXXXX)
python -B scripts/create_task.py --source-type SSTC_FEATURE \
  --source-reference local:slack-smoke --risk-level MEDIUM \
  --task-directory "$AX_TEST_ROOT/tasks"
AX_TASK_FILE="$AX_TEST_ROOT/tasks/출력된TASK_ID.json"
python -B scripts/update_task_state.py --task-file "$AX_TASK_FILE" --status ANALYZING
python -B scripts/update_task_state.py --task-file "$AX_TASK_FILE" \
  --status WAITING_APPROVAL --reason UI_CHANGE
python -B scripts/slack_runner.py listen --task-file "$AX_TASK_FILE"
python -B scripts/validate_task_state.py "$AX_TASK_FILE"
```

Slack에서 승인하면 `IMPLEMENTING`, 거절하면 `REJECTED`를 출력하고 종료한다.
상태만 변경하며 소스 코드를 수정하거나 Codex를 실행하지 않는다. 메시지에는 Task ID와
만료 시각만 보내므로 실제 UI/UX 승인에서는 별도로 검토한 화면·선택지·작업 범위가
필요하다. 이 실험 메시지만으로 실서비스 UI 변경을 승인하지 않는다.

## 재실행과 검증 범위

현재 state·log·checkpoint에 묶인 요청은 24시간 동안 유효하다. 중단 후 같은 명령을
재실행하면 유효한 기존 메시지를 재사용한다. 전송 성공 직후 기록 전에 중단되면 재실행 시
메시지가 중복될 수 있지만, 기록되지 않은 메시지의 버튼은 승인으로 인정하지 않는다.
Task·checkpoint 변경 후 이전 메시지, 만료 요청, 다른 사용자·채널·App의 응답은 무시한다.
`CRITICAL`은 승인 버튼으로 자동 구현을 허용하지 않는다.

상태 저장 중 실패하여 pending journal이 있으면 기존 복구 절차를 사용한다.

```bash
python -B scripts/update_task_state.py --task-file "$AX_TASK_FILE" --recover
python -B scripts/validate_task_state.py "$AX_TASK_FILE"
python -B -m unittest discover -s tests -v
```

복구 결과가 이미 `IMPLEMENTING`/`REJECTED`라면 다시 요청하지 않는다. 로컬 테스트는
가짜 Slack 응답으로 승인 경계를 검사하며 실제 OCI 연결 성공을 대신하지 않는다.
Socket 인증은 CLI가 담당하며 payload 파일을 받아 승인하는 CLI는 제공하지 않는다.
같은 OS 계정이 Controller 파일을 임의 변경할 수 있는 환경은 격리 경계가 아니다.
