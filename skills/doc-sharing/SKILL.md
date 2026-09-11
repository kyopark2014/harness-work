---
name: doc-sharing
description: >
  생성된 artifact(xlsx/pptx/docx/pdf/png/csv/md/json 등)를 harness-work S3의
  artifacts/{actor_id}/… 로 업로드하고 CloudFront 다운로드 URL을 반환한다.
  Markdown(.md) / JSON(.json) / CSV(.csv)은 추가로 Web UI viewer URL(viewer_url)도 반환한다.
  MCP가 아니라 code 인터프리터에서 로컬 파일을 직접 S3로 PutObject 한다.
  트리거: doc-sharing, share_artifact, 산출물 공유, CloudFront URL, 다운로드 링크,
  artifact 업로드, 결과 파일 공유, markdown viewer, json viewer, csv viewer.
---

# doc-sharing

Code Interpreter에서 만든 산출물을 **프로젝트 S3 + CloudFront**로 공유한다.  
세션 로컬 경로(`/mnt/workspace/...`)는 사용자가 열 수 없으므로, 최종 답변 전에 반드시 이 skill로 URL을 만든다.

예전 **artifact-share MCP**(`share_artifact` 도구)를 대체한다. MCP를 호출하지 말고 이 skill 스크립트를 실행한다.

## 환경

| 변수 | 의미 |
|------|------|
| `S3_BUCKET` | harness-work 버킷 (예: `storage-for-harness-work-…`) |
| `SHARING_URL` | harness-work CloudFront base (예: `https://d3z6idizzi5kk.cloudfront.net`) |
| `APP_URL` | Web UI base (예: `https://d196….cloudfront.net`) — `.md`/`.json`/`.csv` viewer_url 생성에 사용 |
| `ARTIFACTS_DIR` | `/mnt/workspace/{actor_id}/artifacts` |
| `ACTOR_ID` / `actor_id` | 시스템 프롬프트에 주어진 actor id |

`skills/doc-sharing/config.json`이 있으면 env가 비어 있을 때 fallback으로 사용한다
(`s3_bucket`, `sharing_url`, `app_url`, `region`).

## 워크플로우

```
- [ ] 1. 산출물을 ARTIFACTS_DIR (또는 images/docs) 아래에 저장
- [ ] 2. doc-sharing skill 동기화 (필요 시)
- [ ] 3. share_artifact.py 실행 → stdout JSON의 url / viewer_url 확인
- [ ] 4. 최종 답변에 공유 URL 포함 (로컬 경로만 안내 금지)
```

### 1. skill 동기화

```bash
aws s3 sync s3://$S3_BUCKET/skills/doc-sharing/ /tmp/doc-sharing/
```

### 2. 공유 실행

```bash
python3 /tmp/doc-sharing/scripts/share_artifact.py \
  --filepath "$ARTIFACTS_DIR/<file>.xlsx" \
  --actor-id "<actor_id>"
```

성공 시 stdout 예:

```json
{
  "ok": true,
  "bucket": "storage-for-harness-work-…",
  "key": "artifacts/<actor_id>/<file>.xlsx",
  "url": "https://d3z6idizzi5kk.cloudfront.net/artifacts/<actor_id>/<file>.xlsx",
  "viewer_url": null,
  "actor_id": "<actor_id>"
}
```

Markdown / JSON / CSV 예:

```json
{
  "ok": true,
  "key": "artifacts/<actor_id>/report.md",
  "url": "https://…cloudfront.net/artifacts/<actor_id>/report.md",
  "viewer_url": "https://<app_url>/api/artifacts/view/report.md"
}
```

- **일반 파일:** `url`(CloudFront)을 답변에 포함
- **Markdown / JSON / CSV:** `viewer_url`이 있으면 **viewer_url을 우선** 안내하고, 필요 시 `url`(raw)도 함께 안내
- 파일이 여러 개면 **파일마다** 스크립트를 실행한다.

## 규칙

- 최종 답변 전에 반드시 URL을 확보한다. URL 없이 "생성 완료"만 하면 실패다.
- 로컬 경로(`/mnt/workspace/...`, `ARTIFACTS_DIR/...`)만 안내하는 것은 **금지**.
- `filepath`는 실제 저장된 절대 경로를 넘긴다.
- `actor_id`는 시스템 프롬프트 값을 그대로 사용한다 (닉네임·추측 금지).

## 금지

- MCP `share_artifact` / `artifact-share` 호출을 시도하지 말 것 (제거됨)
- UI CloudFront(`app_url`)를 일반 파일 다운로드 URL로 쓰지 말 것 — 다운로드는 `SHARING_URL`
- Markdown/JSON/CSV만 `viewer_url`(APP_URL)을 우선 사용
- S3 콘솔 URL만 주고 CloudFront URL을 빼먹지 말 것 (`SHARING_URL`이 있으면 반드시 CF URL)
