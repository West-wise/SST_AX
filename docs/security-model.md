# SST-AX 보안 모델

## 권한 경계

```text
SSTD: read-only
SSTC: branch write + Draft PR create
SST-AX: policy/state/template 관리
main merge: Human only
Release/production: Human 또는 기존 승인된 배포 시스템
```

Codex Worker가 Slack, GitHub, Jenkins API를 직접 자유롭게 호출하지 않도록 한다. 외부 API 호출과 승인 검증은 Controller/Gateway가 담당한다.

## 인증

MVP는 OCI의 전용 Linux 계정에서 Codex CLI를 실행하는 방식을 검토한다. ChatGPT 인증을 사용하더라도 플랜 사용량 제한이 적용되며, 무제한 또는 무비용을 의미하지 않는다. 장기 무인 운영과 서비스 계정이 필요해지는 시점에는 별도 인증 방식을 재검토한다.

GitHub와 Slack credential은 Worker의 작업 디렉터리나 Codex context에 노출하지 않고 Controller의 최소 권한 secret store에서 관리한다. 일일 보고에는 secret, token, 경로 내 민감 정보, 원문 로그, 개인정보를 포함하지 않는다.

Slack Bot Token은 메시지 게시에 필요한 최소 권한만 부여하고 승인 interaction 수신 endpoint는 Slack 서명 검증을 통과한 요청만 처리한다. 보고 채널과 승인 채널의 접근 권한은 필요한 사용자로 제한한다.

## 최상위 규칙

1. secret, token, credential, private key, signing material을 코드·문서·로그에 기록하지 않는다.
2. `.env`, keystore, signing key를 불필요하게 Agent context에 제공하지 않는다.
3. `main` 직접 push와 자동 merge를 금지한다.
4. release credential과 production 권한을 AI에 부여하지 않는다.
5. 외부 dependency 도입은 사람 승인을 요구한다.
6. destructive command는 사람 승인 없이는 실행하지 않는다.
7. 외부 입력과 protocol parser 입력은 모두 untrusted로 취급한다.
8. 테스트 skip, lint disable, warning suppression으로 실패를 우회하지 않는다.
9. 테스트 통과 주장은 실제 command output으로 확인한다.

## 보안 검토 항목

SSTD는 buffer boundary, integer overflow, lifetime, resource leak, privilege, 입력 검증, Noise/AEAD 검증 순서, replay protection, timestamp, secret exposure, log injection을 검토한다.

SSTC는 protocol parser, 저장 데이터, Android permission, dependency, 인증 상태와 UI가 민감 정보를 노출하는지 검토한다.

## 감사와 복구

승인 요청·응답, 일일 보고 집계, 실행 task, credential 사용 주체, 변경 branch, 검증 결과를 task ID와 report date로 연결한다. 일일 보고는 원본 데이터를 대체하지 않으며, 재현 가능한 집계 기준을 유지한다. 변경은 항상 별도 branch/worktree에서 수행하므로 실패 시 해당 worktree와 branch를 제거해 작업 환경을 복구할 수 있어야 한다.
