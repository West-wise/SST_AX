# Slack Gateway와 일일 보고

별도 Linux 계정의 Gateway가 Slack 인증을 보유하고 Controller는 Unix 소켓으로 Task ID만 전달한다. 기존 Socket Mode 승인 검증을 재사용하며 임의 메시지·채널·승인 payload API는 없다. 개인 수정 요청 채널과 직접 접수는 사용자 결정에 따라 보류한다.

## 보고와 전송

한국 시간 매일 **09:00**부터 전날 **00:00 이상, 다음 날 00:00 미만**의 접수·전이·명령 활동을 집계한다. 상태는 집계 시점 기준이고 이전부터 승인 대기·사용량 제한·실패인 Task는 별도 표시한다. 활동 없는 날에도 한 번 보고한다. 09시 이후 시작하면 전날 보고를 확인하며 중단 기간의 모든 날짜를 자동 소급하지 않는다.

고정 한국어 문구, Task ID, 분류, 상태, 검증된 후보 SHA와 SSTC PR 링크를 표시한다. 원문 요청·AI 설명·명령·로그·파일 경로·인증 값은 포함하지 않는다. 분석 VALID는 보존 입력·결과 해시를 재검증하고 Actions·Draft PR은 producer/Sensor 보존 증거를 대조한다. 원격 재조회·실제 기기·merge·Release·배포 완료와 구분한다. [계약](../contracts/daily-report.schema.json)과 [표시 템플릿](../templates/daily-report.md)이 형식을 정의한다.

날짜별 immutable snapshot과 delivery ledger를 보존하고 POST 전에 `SENDING`을 기록한다. workspace·channel·message timestamp를 대조해 성공을 확정한다. `SENT`인 날은 재전송하지 않는다. 응답 유실·중단·불일치는 `UNCERTAIN`으로 두고 자동 재전송하지 않는다. 확실한 미전송만 같은 snapshot으로 최대 3회 시도하며 인증·집계 실패도 checkpoint에 누적해 3회 이후 확인 대기로 둔다. 보고 대상 40개·스캔 10,000개·Slack 크기 한도를 넘으면 전송 전에 중단한다.

## 인증 파일과 설치

Ubuntu가 관리하는 `/etc/sst_ax/.env`는 소유자 전용 `0600`으로 둔다. 필드는 `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_TEAM_ID`, `SLACK_CHANNEL_ID`, `SLACK_DAILY_REPORT_CHANNEL_ID`, `SLACK_APP_ID`, `SLACK_APPROVER_IDS`다. 승인자는 공백 없이 쉼표로 구분한다. 실제 값은 저장소·Task·채팅에 기록하지 않는다.

Gateway는 systemd `LoadCredential=slack.env:/etc/sst_ax/.env`로 받은 전용 파일을 읽는다. shell/source/확장/외부 dotenv dependency 없이 파싱하고 알 수 없는 필드·중복·잘못된 ID·열린 파일 권한·symlink·hardlink·과대 파일을 거부한다. daemon은 `CREDENTIALS_DIRECTORY`를 요구하며 인증 값을 환경변수로 export하지 않는다.

[서비스 템플릿](../config/sst-ax-slack.service)과 [설정 예시](../config/slack-gateway.example.json)를 검토한 뒤 **Ubuntu 운영자가** 설치한다. 이 PR은 서비스를 설치·enable하거나 운영 Controller 설정/cursor를 변경하지 않는다.

1. `sst-ax-slack` 전용 계정, 소켓용 `sst-ax-bridge`, 상태 공유용 `sst-ax-state` 그룹을 만든다. Controller는 bridge/state 그룹, Gateway는 state 그룹에 속한다. 새 그룹은 새 로그인/서비스 실행에 적용한다.
2. 검토한 revision의 `scripts/`, `contracts/`, `templates/`, `policies/`, `state/task-state.schema.json`, `requirements-slack.txt`를 관리자 소유 `/opt/sst_ax`에 설치한다. Worker가 코드와 전용 venv를 수정할 수 없어야 한다. `python3 -m venv /opt/sst_ax/venv`, 그 venv의 `python3 -m pip install -r /opt/sst_ax/requirements-slack.txt`로 기존 pinned SDK를 설치한다.
3. 관리자 소유 `/etc/sst_ax/slack-gateway.json`의 `client_uid`를 실제 Controller UID로, `tasks_directory`를 **현재 state_directory/tasks**로 지정한다. 기록·경로·cursor를 옮기거나 덮어쓰지 않는다. Gateway에 해당 Task/입력/결과와 checkpoint 접근만 부여한다. private home에는 필요한 상위 경로 traversal만 별도로 허용하고 SSTC 작업 공간 전체를 열지 않는다.
4. Task/checkpoint 디렉터리는 state 그룹의 setgid `2770`, 입력 하위 디렉터리는 필요한 그룹 읽기/실행 권한만 사용한다. 기존 파일의 필요한 그룹 권한도 운영자가 검토한다. 새 shared JSON은 parent가 그룹 쓰기를 허용하면 `0660`, 읽기 전용이면 `0640`, private parent에서는 기존 `0600`으로 원자적으로 저장한다. 권한 변경 전후 원본 내용 해시를 대조한다. 운영 Controller에도 `UMask=0007`을 적용한다.
5. 서비스의 `ReadWritePaths`를 실제 공유 state 경로로 맞춘다. 보고 ledger는 전용 `/var/lib/sst-ax-slack/reports`에 둔다. 기존 Controller 설정에 아래 **두 필드만 추가**하고 다른 설정·cursor는 유지한다.

```json
{
  "slack_gateway_socket": "/run/sst-ax-slack/gateway.sock",
  "slack_gateway_uid": 1234
}
```

1234는 실제 Gateway UID로 바꾼다. 두 필드 모두 없으면 기존 환경변수 연결을 유지한다. 한 필드만 있거나 동일 UID이면 거부한다. 양쪽에서 소켓 소유자와 `SO_PEERCRED` UID를 확인한다. Worker에 토큰을 전달하지 않는다.

이는 Slack 인증 파일과 Gateway를 Worker UID에서 분리한다. **Controller와 Worker가 같은 UID로 Task 기록에 쓰기 가능한 경우의 승인 기록 보호, GitHub credential 분리, 전체 운영 격리 완료를 입증하지 않는다.** [보안 모델](security-model.md)의 별도 수용 기준으로 검증한다.

## 설치 전 실제 연결 검증

Ubuntu의 private 디렉터리에 검토한 코드를 복사하고 전용 venv를 만든다. ax-runner가 이 코드와 디렉터리를 수정할 수 없어야 한다. 동일 UID Gateway는 인증 파일 읽기 전에 거부된다. `--credentials-file`은 한 번 실행하는 아래 검증에서만 허용하고 daemon에서는 금지한다.

```bash
python3 -B scripts/run_slack_gateway.py --config /absolute/path/acceptance-gateway.json \
  --credentials-file /etc/sst_ax/.env --check
```

actual source를 읽어 검증한 safe report projection을 먼저 생성한 뒤 Ubuntu에서 다음을 실행할 수 있다. 운영 서비스 설치가 아닌 일회 전송 검증이다. snapshot은 current due date와 보고 계약을 검사하며 source/증거 재검증 여부는 생성 Task의 검증 기록으로 확인한다.

```bash
python3 -B scripts/run_slack_gateway.py --config /absolute/path/acceptance-gateway.json \
  --credentials-file /etc/sst_ax/.env --report-once \
  --report-snapshot /absolute/path/verified-daily-report.json
```

실제 `SLACK_GATEWAY_CHECK=OK`, `SLACK_DAILY_REPORT=SENT`, ledger timestamp와 Slack 한국어 표시를 확인한다. 재실행은 `ALREADY_SENT`여야 한다. fake SDK 회귀, 실제 연결·전송, 별도 UID OS 격리, 상시 09시 운용 증거를 구분한다. `UNCERTAIN`은 운영자가 Slack과 ledger를 대조해 확인하며 기록을 삭제·수정해 성공으로 취급하지 않는다.
