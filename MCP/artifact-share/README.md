# Artifact Share MCP (AgentCore Runtime)

Harness가 호출하는 IAM 인증 산출물 공유 MCP입니다.
`ARTIFACTS_DIR`(`/mnt/workspace/{user_id}/artifacts`)에 저장된 산출물을
프로젝트 S3의 공유 키 `artifacts/{user_id}/...`로 **복사**한 뒤
CloudFront `SHARING_URL` 기반 다운로드 URL을 반환합니다.

S3 Files 마운트 실제 키:

```text
/mnt/workspace/{user_id}/artifacts/file
  → s3://{bucket}/agentcore-sessions/{user_id}/artifacts/file
  → (CopyObject)
  → s3://{bucket}/artifacts/{user_id}/file
  → https://{SHARING_URL}/artifacts/{user_id}/file
```

`installer.py`가 Docker 이미지를 ECR에 푸시하고 AgentCore Runtime(MCP protocol)으로
배포한 뒤, Knowledge Base와 **동일한 project Gateway**에 target으로 붙입니다.

## Local

```bash
cp ../../application/config.json .
# optional: export S3_BUCKET=... SHARING_URL=https://dxxxx.cloudfront.net
python -m mcp_server_artifact_share
```

## Runtime env

| 변수 | 설명 |
|------|------|
| `S3_BUCKET` | 프로젝트 S3 버킷 (S3 Files 세션 스토리지와 동일) |
| `SHARING_URL` | CloudFront base URL |
| `AWS_REGION` | 리전 |
| `SESSION_STORAGE_DIR` | 기본 `/mnt/workspace` (절대경로 → 키 매핑용) |
| `S3_FILES_SESSION_PREFIX` | 기본 `agentcore-sessions/` (S3 Files FS prefix) |
| `S3_FILES_SYNC_MAX_ATTEMPTS` | 기본 `10` — S3 Files flush 대기 HeadObject 재시도 |
| `S3_FILES_SYNC_BASE_DELAY_SEC` | 기본 `3` — 첫 재시도 대기(초), 이후 지수 증가 |
| `S3_FILES_SYNC_MAX_DELAY_SEC` | 기본 `20` — 재시도 대기 상한(초) |

## Tool

`share_artifact(filepath, actor_id)`

- `actor_id` 필수 (별도 Runtime이라 env에서 주입되지 않음; system prompt의 계정 로그인 ID)
- `filepath`: `/mnt/workspace/{actor_id}/artifacts/...` 또는 `artifacts/...`
- 동작: 세션 키 → 공유 키로 `CopyObject` (`aws s3 cp`와 동일)
