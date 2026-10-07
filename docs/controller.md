# Controller 실행

`run_controller.py`가 한 프로세스에서 GitHub 입력 확인과 저장 Task의 다음 단계를 처리합니다. OCI 명령은 운영자가 실행합니다. 초기 설정 이후 매 Task의 파일·단계·세션을 사람이 연결하는 방식은 기본 운영 경로가 아닙니다.

## 준비

Python 3.10 이상·Git·Codex CLI 저장된 로그인·인증된 GitHub CLI와 SSTD/SSTC 로컬 저장소가 필요합니다. SSTC main에는 `sstc-validation.yml`, 의미 검증 테스트, `[AX]` Issue 양식이 있어야 합니다.

GitHub는 SSTD 읽기와 SSTC Issue 읽기·Contents/Actions/Pull requests 쓰기에 필요한 권한만 사용합니다. Slack 설정은 [기존 안내](slack-quickstart.md)를 따릅니다. 토큰은 config·Task·로그·저장소에 넣지 않습니다. 운영 설정·상태는 운영자만 쓰도록 관리하고 Worker의 OS 권한을 분리합니다.

## 설정

`config/controller.example.json`을 저장소 밖에 복사하고 절대 경로를 지정합니다.

| 항목 | 의미 |
|---|---|
| sstd_repository / sstc_repository | 소스 로컬 저장소 |
| state_directory | Task·입력 journal·cursor |
| worktree_directory | SSTC 작업 공간 부모 |
| poll_seconds | 15~3600초 확인 주기 |
| bootstrap_sstd_base | 최초 비교 기준 전체 SHA 또는 ROOT |

최초 범위는 사람이 정합니다. 현재 main SHA를 지정하면 그 이후 변경부터 처리하고, 과거 SHA는 그 이후 범위가 대상입니다. 예시 ROOT는 전체 과거 이력을 의도할 때만 사용합니다. 이후에는 저장된 cursor를 사용합니다. 최초 조회의 과거 Release는 기준 목록으로만 저장하고 새로 발행된 Release부터 접수하여 이전 계약으로 되돌리는 작업을 만들지 않습니다.

```bash
cd "$HOME/workspace/SST_AX"
python -B scripts/run_controller.py --config /absolute/path/controller.json --once
```

한 차례 확인으로 설정·상태를 점검하고 준비되면 `--once`를 제거하여 주기 실행합니다. 승인 대기에 Codex를 유지하지 않고 여러 대기 Task를 하나의 bounded Socket Mode 연결로 처리합니다. 기존 대기 승인은 무거운 실행 단계 전에 확인합니다. Worker 실행 중 새 결정의 반영은 다음 polling 단계까지 지연될 수 있습니다. systemd 설치·자동 시작은 운영자가 수행합니다.

## 입력과 개입

SSTD main의 고정 범위와 Release commit을 읽고 journal·입력 식별자로 중복을 막습니다. 관측한 main에 이미 포함된 과거 commit의 새 Release는 Task를 추가하지 않습니다. 관계를 확인할 수 없으면 중단하여 검토를 요구합니다. SHA 관계는 [GitHub commit compare](https://docs.github.com/en/rest/commits/commits#compare-two-commits)의 고정 SHA 응답으로 확인합니다. 필요한 Git 객체는 확보하되 source checkout·미커밋 변경을 바꾸지 않습니다. SSTC에서는 AX UI/UX Issue 양식의 `[AX]` 제목에 요청·제약·확인 기준을 작성합니다. 본문 수정은 새 snapshot이며 이미 승인한 입력을 덮어쓰지 않습니다.

운영자는 Slack 승인·거절, 판단 유보·문맥 상한·반복 실패·게시 응답 유실의 검토와 실제 reset 시각 확인, PR review·merge·기기 확인을 수행합니다. OCI에서 두 입력의 분기, 같은 세션·worktree 재개, 후보 SHA와 Actions·Draft PR 일치, 거절·실패·한도에서 PR 생성 차단을 확인해야 합니다. 로컬 테스트를 OCI 무인 운영 성공으로 기록하지 않습니다.
