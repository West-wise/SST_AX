# 영향 분석 결과 검증

이 기능은 AI 분석 결과를 로컬 입력과 비교하는 검증기다. Codex를 호출하거나
Slack 승인을 처리하지 않으며, Task 상태·위험도·승인 정보도 변경하지 않는다.

## 파일과 역할

| 파일 | 역할 |
|---|---|
| [요구사항](../specs/impact-result.spec.md) | 공통 범위·수용 기준·분석 한계 |
| [결과 schema](../contracts/impact-analysis.schema.json) | 루트는 결과 형식, `$defs.manifest`는 입력 manifest 형식 |
| [검증 CLI](../scripts/validate_impact_result.py) | 입력 로드, 오류 코드와 판정 출력 |
| [검증 규칙](../scripts/impact_validation.py) | 중첩 형식·입력 결합·명시적 모순 검사 |
| [테스트 fixture](../tests/fixtures/impact) | 실제 서비스 정보가 아닌 합성 Task·manifest·결과 묶음 |

## 사용법

운영자/Controller가 준비한 Task와 manifest, 별도로 받은 분석 결과가 필요하다.
다음 경로는 사용자가 준비한 파일의 예시이며 자동으로 생성되지 않는다.

```bash
python3 scripts/validate_impact_result.py \
  --task-file state/tasks/<task-id>.json \
  --manifest-file state/tasks/<task-id>.manifest.json \
  --result-file state/tasks/<task-id>.analysis.json
```

규약에 맞는 결과는 다음처럼 출력한다.

```text
VALID_IMPACT=UNDETERMINED
AUTHORIZATION=NONE
```

| 판정/종료 코드 | 의미 |
|---|---|
| `REQUIRED`, 0 | 수정 필요라는 결과가 규약에 맞음; 구현 허가 아님 |
| `NOT_REQUIRED`, 0 | 수정 불필요라는 결과가 규약에 맞음; 자동 완료 허가 아님 |
| `UNDETERMINED`, 0 | 질문·누락 정보를 포함한 판단 유보가 규약에 맞음 |
| 1 | 형식 오류·중복 key·입력 불일치·증적/승인 사유 모순 |
| 2 | 파일 읽기·JSON 파싱·자원 제한·CLI 사용 오류 |
| 130 | 검증 중 사용자 중단 |

성공 결과는 stdout, 오류 코드와 필드 위치는 stderr로 출력한다. 알 수 없는 key·값과
입력 원문을 오류 메시지에 복사하지 않는다. 실패 결과를 자동 보정하거나 재시도하지 않는다.

## 신뢰할 입력 준비

로컬 저장소·요청 snapshot에서 입력을 준비하는 방법은 [입력 수집](impact-inputs.md)을
따른다. 수집기는 본문과 해시를 생성하지만 Worker 권한 격리와 의미적 충분성을 보장하지 않는다.

manifest를 결과 작성자에게 자유롭게 수정하게 해서는 안 된다. Task와 manifest를
Controller가 관리하고 Worker 쓰기 영역에서 분리해야 한다. 초기 구현에는 이 권한 격리나
Worker 실행 연결이 포함되지 않으므로, 검증기의 성공을 인증 증명으로 사용하지 않는다.

- commit은 소문자 40자리 Git SHA로 고정한다. SHA-256 Git 저장소 형식은 현재 지원하지 않는다.
- SSTD는 기준·대상 commit과 SSTC commit을 기록한다. 최초 commit만 기준을 null로 둔다.
- SSTC 요청은 SSTC commit과 요청 본문 UTF-8 snapshot의 SHA-256을 기록한다. URL만으로 본문 동일성을 보장할 수 없다.
- 해당 유형에서 사용하지 않는 context 값은 null이다. 필수 revision/snapshot이 미확보이면 null로 기록하고 판단을 유보한다.
- 필수 증적은 SSTD의 `source_diff` 또는 SSTC의 `request`, 공통 `sstc_context`·`target_instructions`다. 이 항목들은 `required: true`로 기록한다.
- 증적별로 출처·revision·내용 hash·경로·누락/잘림 여부를 기록한다. 누락 증적은 `missing: true`, `content_sha256: null`이다. 요청 증적의 내용 hash는 요청 snapshot hash와 같아야 한다.
- 증적 경로는 `/`를 구분자로 하는 저장소 상대 경로다. 요청과 집계 diff는 path가 null일 수 있다. 삭제 파일은 기준 revision에 연결한다.

검증기는 manifest에 기록된 참조와 hash의 일관성을 검사하지만 실제 저장소·증적 본문을
열어 hash를 재계산하지 않는다. 실제 수집기의 정확성과 보호는 별도 검증 대상이다.
기존 `analyze_impact.py`의 Markdown은 manifest를 대신하지 않으며, 파일 목록·stat만으로
의미 분석에 충분한 증적이 준비되었다고 간주하지 않는다.

## 검증 범위와 한계

중복 key, 중첩 필수/추가 필드, enum/type, Task·입력 식별, 증적 참조, 명시적 모순을
검사한다. JSON당 최대 1 MiB, 컨테이너 중첩 깊이 32, 규약 배열당 최대 256개다.
정수 토큰은 최대 4,300자리로 제한해 Python 버전별 기본 제한 차이를 없앤다.
JSON Schema 전체 구현은 아니며 저장소 schema에서 사용하는 부분집합과 별도 교차 규칙을
적용한다. 일반 JSON Schema 검사만으로는 교차 규칙까지 검증되지 않는다.

필수 증적의 누락·잘림, UNKNOWN 영향, 미해결 질문은 확정 결론을 막는다.
UI/UX·protocol·dependency·Android permission·파괴적 작업·SSTD 추가 변경과 HIGH/CRITICAL
위험도에는 대응 사유가 필요하다. 이는 사유 누락을 잡는 검사이지 UI 영향의 진위를
자동 판정하는 기능이 아니다. AI가 잘못된 ABSENT와 그럴듯한 근거를 제출할 가능성은 남는다.

## 로컬 검증과 다음 단계

```bash
python3 -m unittest discover -s tests -p 'test_impact_validation.py' -v
python3 -m unittest discover -s tests -v
```

fixture는 `task`, `manifest`, `result`를 담는 테스트용 묶음이다. 테스트가 각각을
임시 파일로 분리해 CLI를 호출하므로 네트워크·실제 계정·SSTD/SSTC clone이 필요 없다.

2026-09-12 검증: Windows Python 3.13.2와 로컬 WSL Ubuntu 22.04 Python 3.10.12에서
각각 전체 51개(영향 검증 38개 + 기존 13개)가 통과했다. OCI 실서버 재검증은 별도다.
잘못된 Task URL의 오류 원문 노출, 정수 파싱 제한, 배열 상한 초과 처리, SHA 끝 개행에
대한 독립 검토 지적을 수정하고 회귀 사례를 포함했다. Python 버전에 따라 다른 bracketed
host 처리는 새 검증기 경계에서 일관되게 검사하며 기존 Task CLI는 바꾸지 않았다.

수집한 입력을 [Codex 읽기 전용 분석 CLI](codex-impact.md)에 전달하면 이 검증기로
결과를 검사하고 Task 로그에 세션과 결과 해시를 기록한다. 분석 품질은 사람이 정한
평가 사례와 별도로 비교해야 한다. SSTC 수정·Draft PR은 [Controller](controller.md)가 연결하며,
검증기 자체는 Task 권한을 변경하지 않는다. 사용량 리셋 대기와 checkpoint 운영은
[실행 안내](codex-impact.md)와 기존 [운영 설계](operations.md)를 따른다.

## 의미 평가

`tests/fixtures/semantic/`은 고정 SSTD/SSTC 코드에 근거한 가상 변경 사례와 사람이 정한 예상 분류입니다. 실제 운영 commit이라고 주장하지 않습니다. uptime 단위·packed 필드 폭 변경, daemon 내부 로그, decoder 증적 누락을 구분합니다.

해당 사례의 입력으로 얻은 실제 분석 결과를 다음처럼 예상 분류와 비교합니다.

```bash
python3 -B scripts/evaluate_impact.py \
  --case-file tests/fixtures/semantic/uptime-unit.json \
  --result-file /absolute/path/case-analysis.json
```

`MATCH`는 해당 사례의 예상 change/impact/approval 분류와 일치한다는 뜻이며 실행 권한이 아닙니다. 다른 입력의 결과를 비교한 점수는 의미 없습니다. 로컬 테스트는 형식에 맞는 잘못된 NOT_REQUIRED가 거부되는지 확인합니다. 실제 모델 정확도는 별도 replay 결과로 측정합니다.

완성된 입력을 비교하는 별도 사례도 제공합니다. 기존 네 사례와 기대값은 유지합니다.

| 새 사례 | 완성된 서버 변경과 기대 동작 | 예상 분류 |
|---|---|---|
| `uptime-unit-complete` | uint32 밀리초의 오버플로 방지·포화 처리, 필드 문서화, 클라이언트 초 정규화와 상한의 `49일 이상` 표시 | REQUIRED, protocol/UI PRESENT, 추가 SSTD ABSENT |
| `field-width-complete` | uint64 선언·138바이트 크기 검사·수집 코드 수정, 지원 범위는 0..UINT32_MAX 초, 클라이언트 decoder 크기·offset 수정, 기존 UI 유지 | REQUIRED, protocol PRESENT, UI/추가 SSTD ABSENT |

`source_replacements`는 고정 SSTD 코드에 적용할 가상 변경 전체입니다. 실제 평가 시 각 `before`가 원본에 정확히 한 번 나타나는지 확인하고, 변경한 파일의 after 본문과 unified diff를 함께 제공합니다. `TcpServer.cpp`와 `PacketUtil.cpp`의 동적 크기 직렬화 코드도 포함합니다. 기본 SHA는 원본 문맥 식별자이며 가상 after 코드의 실제 commit SHA가 아닙니다. 숫자 경계와 기대 클라이언트 동작은 모델 입력에 포함하되 `expected` 분류는 모델에게 제공하지 않습니다.

원본 실제 AI 평가에서는 `uptime-unit`, `field-width`, `missing-decoder`가 MISMATCH이고 `daemon-log-only`만 MATCH였습니다. 단위 상한·서버 후속 코드가 명시된 새 사례의 MATCH는 원본 불일치를 해결하거나 실제 SSTD 변경 경로를 통과했다는 뜻이 아닙니다. 실제 GitHub 감지 → 분석 → 승인 → 구현 → Actions → Draft PR 증거, 실제 거절 분기와 기기 검증은 별도로 확인합니다.
