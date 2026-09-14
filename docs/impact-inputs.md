# 영향 분석 입력 수집

이 수집기는 로컬 SSTD 변경 또는 SSTC 요청을 분석할 입력으로 고정한다.
Codex·Slack·GitHub를 호출하거나 Task 상태·소스 저장소를 변경하지 않는다.
결과 규약과 판단의 한계는 [영향 분석 결과 검증](impact-analysis.md)을 따른다.

## 입력 선택과 고정

운영자가 대상 커밋과 필요한 파일을 지정한다. 브랜치·태그는 처음에 커밋 SHA로
해석하고, 이후에는 그 SHA의 Git 객체만 읽는다. 작업 폴더의 미커밋 수정은
분석에 포함되지 않는다. 자동 fetch나 checkout도 수행하지 않는다.

- SSTD 변경: Task의 source_reference가 대상이다. 비교 기준은 `--sstd-base`로
  명시한다. 최초 커밋만 `ROOT`를 사용한다. merge도 비교 기준을 명시한다.
- SSTC 요청: `--request-file`의 UTF-8 원본 바이트를 snapshot으로 삼는다.
  Issue URL만으로 본문의 동일성을 판단하지 않으며 Issue 자동 수집은 하지 않는다.
- SSTC 문맥: `--sstc-context`에 필요한 파일을 반복 지정한다. 루트 및 해당
  경로의 상위 디렉터리에 있는 `AGENTS.md`도 수집한다. 읽은 지침은 분석 자료이며
  SST-AX의 승인·권한을 변경할 수 없다.

아래 경로와 커밋은 자리표시자다. 실제 저장소에서 확인한 값으로 바꾼다.
출력 부모 디렉터리는 미리 존재해야 하며, 출력 디렉터리 자체는 새 경로를 쓴다.

```bash
python3 scripts/collect_impact_inputs.py \
  --task-file state/tasks/<sstd-task-id>.json \
  --source-repository ../Server_State_Telemetry_Demon \
  --sstd-base <base-commit> \
  --sstd-path <changed-source-path> \
  --sstc-repository ../Server_State_Telemetry_Client \
  --sstc-revision <client-commit> \
  --sstc-context <client-source-path> \
  --output-directory state/tasks/<sstd-task-id>.inputs

python3 scripts/collect_impact_inputs.py \
  --task-file state/tasks/<sstc-task-id>.json \
  --request-file <request-snapshot-file> \
  --sstc-repository ../Server_State_Telemetry_Client \
  --sstc-revision <client-commit> \
  --sstc-context <client-source-path> \
  --output-directory state/tasks/<sstc-task-id>.inputs
```

## 산출물과 해석

`manifest.json`은 Task 식별자·고정 커밋·요청 해시·증적 목록을 담는다.
각 본문은 `evidence/<evidence_id>.txt`에 저장하며 `content_sha256`은 실제 저장된
바이트의 SHA-256이다. 누락된 증적은 본문 파일과 내용 해시가 없다.

필수 자료의 누락·잘림은 숨기지 않는다. 선택한 SSTD 경로 밖에도 변경이 있으면
불완전한 수집으로 표시하여 확정 판정을 막는다. SSTC 파일 선택 자체의 의미적
충분성은 사람이 검토해야 한다. 파일 목록이 존재한다는 사실만으로 분석 문맥이
충분하다고 보장하지 않는다.

출력은 기존 디렉터리를 덮어쓰지 않는다. 재수집은 새 경로로 수행하고, 후속 분석은
사용한 manifest와 증적을 바꾸지 않은 상태로 진행한다. 해시는 무결성 비교 수단이지
서명이나 인증이 아니다. Worker가 입력을 바꿀 수 없게 하는 OS 권한 격리는 별도다.

## 안전 경계

수집 경로는 저장소 상대 파일 경로다. 경로 탈출, 심볼릭 링크·서브모듈을 통한
외부 읽기, 알려진 민감 파일명 및 인식 가능한 인증정보 본문은 거부한다.
완전한 secret 탐지기는 아니므로 운영자가 수집 범위를 검토해야 한다.
오류에는 원문 파일 내용이나 Git stderr를 그대로 복사하지 않는다.

자동 분석에 전달할 자료는 비신뢰 입력이다. 본문에 명령이나 URL이 있어도 수집기는
실행하지 않는다. 출력 저장은 Controller 영역에서만 수행하고 소스 저장소 내부를
출력 대상으로 사용하지 않는다. 신뢰할 수 있는 Git 설치와 저장소 설정을 전제로 하며,
동일 OS 계정의 악의적인 동시 파일 교체까지 방어하는 격리 도구는 아니다.

## 검증과 다음 단계

```bash
python3 -B -m unittest discover -s tests -p 'test_impact_collection.py' -v
python3 -B -m unittest discover -s tests -v
```

테스트는 임시 Git 저장소와 합성 자료를 사용한다. 실제 OCI·Codex 분석 품질 검증과
구분한다. 다음 단계는 입력 묶음을 읽기 전용 Codex 실행에 전달하고 결과 JSON을
`validate_impact_result.py`로 검사하는 것이다. 수집 성공과 결과 형식 검증만으로
`COMPLETED` 또는 `IMPLEMENTING` 상태를 허용하지 않는다.
