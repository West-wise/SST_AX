# 모델 라우팅

AGENTS.md는 원칙, `policies/model-routing/balanced-v1.json`은 고정 매핑,
`scripts/model_routing.py`와 분석·Worker 실행 코드는 실제 CLI 선택을 담당한다.
사용자 config를 무시하는 기존 격리를 유지하면서 `--model`과
`-c model_reasoning_effort=...`를 초기 분석과 같은 세션의 resume에 모두 전달한다.

| 작업 | 모델 | 추론 강도 |
|---|---|---|
| 필수 영향 분석·일반 구현 | gpt-6.1-sol | medium |
| HIGH·CRITICAL 분석, 승인된 HIGH 구현 | gpt-6.1-sol | high |
| 필요한 단순 문서·요약 | gpt-6-luna | low |
| 트리거·상태·정형 일일 보고 | Python | AI 호출 없음 |

이 변경은 새 문서·보고용 모델 호출을 추가하지 않는다. text 경로는 정책과 검증된
선택 함수만 제공하며 기존 필수 분석이나 구현을 대체하지 않는다. HIGH/CRITICAL은
신뢰된 Task 위험도와 검증된 분석 결과로 결정한다. 요청 문구로 모델·권한을 고르지 않는다.
복잡하다는 이유만으로 자동 권한을 확대하지 않으며 CRITICAL 자동 구현 금지는 유지한다.

새 Task log에 profile과 정책 원문 SHA-256을 고정하고 분석·구현 기록에 요청한
모델·추론 강도·stage를 남긴다. pin이 없는 기존 Task는 원래 CLI 명령과 세션 동작을
유지한다. pin이나 기록 변경은 실행 전에 거부하며 재시도에서 모델을 자동 교체하지 않는다.
향후 정책은 새 버전으로 추가하고 이 스냅샷을 수정하지 않는다.

CLI가 제공한 `turn.completed.usage`의 숫자 카운터만 보존한다. 토큰 필드가 없으면
미관측으로 기록한다. 요청 모델과 공급자가 확인한 실제 모델은 구분해야 하며, 이
기록은 요청 모델만 증명한다. 입력·출력·cached 입력 수를 임의로 합산하거나 reasoning
토큰을 중복 합산하지 않는다. 원문 이벤트·인증 로그는 저장하지 않는다.

AGENTS.md 수정이나 Draft PR 작성은 운영 적용이 아니다. 사람이 코드 검토·병합 후
승인된 운영 절차로 새 코드를 적용해야 한다. 기존 config·cursor·실패 Task를 이 변경의
성공 기록으로 갱신하지 않는다.

검증은 기존 `python3 -B -m unittest discover -s tests -v`를 사용한다. 로컬 모의 회귀,
실제 모델 사용 가능 여부·AI VALID, 의미 MATCH, 정상 Controller 전체 경로와 절감 효과는
각각 별도 증거다. 같은 고정 입력에서 새 false NOT_REQUIRED·승인 누락 없이 판단을
유지하는지와 토큰·시간을 비교하기 전에는 절감률을 주장하지 않는다.

선택 근거: [OpenAI Docs 모델 선택](https://developers.openai.com/api/docs/guides/model-selection),
[Codex 설정](https://learn.chatgpt.com/docs/config-file/config-reference).
