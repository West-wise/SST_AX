# Codex 읽기 전용 영향 분석

독립 CLI가 [수집된 입력](impact-inputs.md)을 Codex에 전달하고
[기존 결과 검증기](impact-analysis.md)로 JSON 형식과 입력 결합을 검사한다.
Slack Runner는 그대로 승인 전달을 담당한다. Python 3.10 이상이며 새 의존성은 없다.

## 실행 경계

- Task는 `ANALYZING`이어야 한다. 본문 해시·Task 식별자를 확인하고, 증적 본문은
  stdin으로 전달한다. 소스 저장소를 Worker 작업 디렉터리로 사용하지 않는다.
- 임시 작업 디렉터리에서 `codex exec --sandbox read-only --json`을 실행한다.
  사용자 config와 execpolicy rules 로드를 제외하고 shell·apps·plugins·web 검색을
  비활성화한다. 모델·승인 모드를 입력 본문이나 사용자 인수로 바꿀 수 없다.
- 저장된 Codex 로그인과 세션 저장소를 사용한다. Controller의 Slack·GitHub 토큰과
  Git 설정 환경변수는 전달하지 않는다. 인증정보 파일을 복사하거나 출력하지 않는다.
- `--ignore-user-config`, `--ignore-rules`, `--output-schema`, `--json`을 신규 실행과
  resume 모두에서 지원해야 한다. 미지원 CLI는 입력 전송 전에 거부한다.
- 생성용 schema는 기존 계약에서 구조적 부분만 추출한다. 기존 검증기가 길이·패턴·
  증적 참조·승인 사유 등 전체 로컬 규칙을 다시 검사한다. 형식 통과는 의미적 정확성이나
  구현 허가를 의미하지 않는다.

관리자가 제공한 Codex 바이너리·시스템 설정·로그인 저장소를 신뢰한다. 동일 OS 계정의
악의적인 파일 교체를 방어하는 격리 도구는 아니다. 운영 시 Controller 상태와 Worker
권한 분리가 필요하며, CLI 설정만으로 OS 계정 격리가 완료됐다고 주장하지 않는다.
Codex 자체 세션 기록에는 전달한 증적이 남으므로 수집 범위와 접근 권한을 검토한다.

## 사용 순서

다음 명령은 OCI 운영자가 실행한다. `<task-id>`와 경로는 실제 수집 결과로 바꾼다.
새 Task를 생성하고 `ANALYZING`으로 전이한 뒤 입력을 수집하는 절차는 기존 문서를 따른다.

```bash
python3 -B scripts/run_codex_impact.py \
  --task-file state/tasks/<task-id>.json \
  --input-directory state/tasks/<task-id>.inputs
```

성공 출력은 `CODEX_IMPACT=VALID`, `AUTHORIZATION=NONE`이다. Task의 상태·위험도를
분석 결과만으로 승격하지 않는다. SSTC 요청에 SSTD protocol/contract 변경이 필요하다는
유효한 결과가 나오면 `PROTOCOL_APPROVAL_REQUIRED`로 중단한다.

산출물은 Task 옆에 저장한다. 원시 JSONL·stderr·오류 본문은 저장하거나 출력하지 않는다.

| 산출물 | 용도 |
|---|---|
| `<task-id>.analysis-N.json` | JSON 파싱·알려진 secret 검사 후 저장한 결과; 유효성은 로그 outcome으로 확인 |
| `<task-id>.log.json`의 `codex_analysis` | 세션 ID, manifest·결과 해시, 시도 횟수, outcome |
| `<task-id>.codex-checkpoint.json` | 호출 전·세션 ID 수신 시·호출 종료 시 Task/log snapshot |
| 기존 `state/checkpoints/<task-id>.json` | 승인·사용량 제한 전이의 기준 snapshot |

`thread.started`의 세션 ID를 받으면 즉시 저장한다. 이후 같은 Task·입력의 분석은
`codex exec resume <정확한 ID>`를 사용한다. `--last`나 실패 시 새 세션 자동 생성은
사용하지 않는다. 입력이 달라지면 새 Task를 생성한다.

## 승인 후 동일 세션 재개

결과와 범위를 사람이 검토한 뒤 기존 `update_task_state.py --status WAITING_APPROVAL
--reason ...` 및 [Slack 승인 절차](slack-quickstart.md)를 사용한다. 이 CLI는 Slack
메시지를 보내지 않는다. 승인이 `IMPLEMENTING`으로 반영되면 실행한다.

```bash
python3 -B scripts/run_codex_impact.py \
  --task-file state/tasks/<task-id>.json \
  --input-directory state/tasks/<task-id>.inputs \
  --resume-approved
```

승인 직전 checkpoint, Gateway 승인 감사 기록·요청 snapshot 해시, 승인 후 정확한
Task/log, 세션 ID, 입력·결과 해시를 대조한다. 거절·누락·변조·CRITICAL·판단 유보·
수정 불필요·SSTD protocol 변경 결과는 재개하지 않는다. 세션 ID 자체는 권한 증명이 아니다.

이번 재개는 **동일 세션의 읽기 전용 후속 분석**이다. 승인 여부와 관계없이 소스 쓰기
권한을 주지 않으며, SSTC 구현 Worker와 자동 Draft PR 생성은 후속 범위다.
재개 후 로그가 달라졌으므로 같은 승인으로 다시 호출하면 거부한다. 새 결과나 실패 뒤
추가 재개가 필요하면 사람이 새 승인 주기를 준비해야 한다. 자동 재승인은 없다.

## 실패와 복구

한 번 호출은 최대 10분, 이벤트는 총 8 MiB·한 줄 1 MiB로 제한한다. Task의 `attempt`를
호출마다 증가시키며 최대 3회까지만 실행한다. 자동 재시도는 없다.
JSON 오류·세션 불일치·실행 실패는 `ANALYSIS_FAILED`로 종료한다. 승인 후 실패는
기존 단계에 맞춰 `IMPLEMENTATION_FAILED`로 기록하지만 실제 소스 수정은 수행하지 않는다.
timeout·사용자 중단은 checkpoint와 정제된 사유를 남긴다. 분석 단계에서는 운영자가
예산 내 재실행할 수 있다. 승인 후 재시도에는 새 승인 검토가 필요하다.

사용량 제한이 감지되면 `DEFERRED_RATE_LIMIT`로 전이하고 reset 시각은 추측하지 않는다.
운영자가 계정에서 확인한 시각을 기록한 뒤, 그 시각이 지나야 기존 전이 CLI로 복귀한다.

```bash
python3 -B scripts/update_task_state.py --task-file state/tasks/<task-id>.json \
  --record-reset-at '<실제로 확인한 ISO-8601 시각과 시간대>'
python3 -B scripts/update_task_state.py --task-file state/tasks/<task-id>.json \
  --status ANALYZING
```

위 복귀 명령은 분석 단계에서 중단된 Task용이다. 승인 후 사용량 제한은 기존 승인
snapshot을 자동 재사용하지 않으며 운영자가 새 승인 주기를 검토한다.
pending journal은 기존 `--recover`로 복구한다. 프로세스 강제 종료로 `RUNNING`이 남으면
중복 호출을 거부한다. 로그와 Codex 세션을 확인한 후 별도 Task로 진행하며 receipt를
임의로 성공 처리하지 않는다. 알려진 secret 패턴 검사는 완전한 secret 탐지기가 아니다.

## 검증

```bash
python3 -B -m unittest discover -s tests -p 'test_codex_impact.py' -v
python3 -B -m unittest discover -s tests -v
```

합성 입력, 실제 로컬 자식 프로세스, 가짜 Codex 이벤트와 Slack 응답으로 검사한다.
OCI 실 Codex 호출·sandbox 동작·분석 품질은 별도 운영 검증이며, 성공으로 선기록하지 않는다.
근거: [Codex 비대화형 실행](https://developers.openai.com/codex/noninteractive/),
[설정 항목](https://developers.openai.com/codex/config-reference/), 설치된 CLI의 `exec --help`와
`exec resume --help`.
