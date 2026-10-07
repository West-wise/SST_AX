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

OCI의 전용 Linux 계정에서 Codex CLI를 실행한다. 인증 플랜의 사용량 한도는 별도이며 무제한을 의미하지 않는다.

GitHub와 Slack credential은 Worker의 작업 디렉터리나 Codex context에 노출하지 않고 Controller의 최소 권한 secret store에서 관리한다. 일일 보고에는 secret, token, 경로 내 민감 정보, 원문 로그, 개인정보를 포함하지 않는다.

Slack Bot Token은 메시지 게시에 필요한 최소 권한만 부여한다. 승인 interaction은 인증된 Socket Mode에서 workspace/app/channel/user/nonce/snapshot을 대조한다. 공개 HTTP endpoint를 사용하지 않는다.

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

승인·실행·변경 branch·검증 결과를 Task에 연결한다. 실패에는 diff·상태·승인·실행 근거를 보존하고 검증된 재개를 시도한다. 삭제로 복구 gate를 우회하지 않는다. 일일 보고는 원본 기록을 대체하지 않는다.
