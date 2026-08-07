# Knowledge Base MCP (AgentCore Runtime)

Harness가 호출하는 IAM 인증 Knowledge Base retrieve MCP입니다.
`installer.py`가 Docker 이미지를 ECR에 푸시하고 AgentCore Runtime(MCP protocol)으로 배포합니다.

## Local

```bash
cp ../../application/config.json .
./build-docker.sh
./run-docker.sh
```

## Runtime env

| 변수 | 설명 |
|------|------|
| `KNOWLEDGE_BASE_ID` | Bedrock Knowledge Base ID |
| `PROJECT_NAME` | KB 이름 조회 fallback |
| `SHARING_URL` | CloudFront base URL (문서 링크) |
| `AWS_REGION` | 리전 |

## Tool

`retrieve(keyword, actor_id)` — `actor_id`는 필수(별도 Runtime이라 env에서 주입되지 않음). system prompt의 계정 로그인 ID를 그대로 넘기세요. Bedrock retrieve 시 metadata `owner`에 대해 `listContains` 필터를 적용합니다.
