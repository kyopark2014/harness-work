---
name: s3-sharing
description: 로컬 결과 파일을 프로젝트 S3에 업로드하고 CloudFront 공유 다운로드 URL을 반환합니다. 사용자가 파일 공유, 다운로드 링크, S3 업로드, 산출물(PDF/이미지/문서) URL, "링크 줘", "공유해줘"를 요청할 때 사용합니다.
---

# S3 Sharing

로컬 파일을 프로젝트 S3 버킷에 업로드하고 공개 다운로드 URL을 반환합니다.
`upload_file_to_s3`와 동일하게 `artifacts|images|docs/{user_id}/...` 키로 `put_object` 한 뒤 CloudFront `sharing_url`을 사용합니다.

## When to Use

- 생성한 산출물(PDF, DOCX, PNG, CSV 등)을 사용자에게 링크로 전달할 때
- "다운로드 링크", "공유 URL", "S3에 올려줘" 요청
- `/mnt/workspace`에 파일이 있고 CloudFront로 공유해야 할 때

## Script Location (AgentCore Harness)

S3 스킬은 아래 경로에 마운트됩니다. **이 절대 경로만** 사용하세요.

| 스크립트 | 용도 |
| --- | --- |
| `/home/.agents/skills/s3/s3-sharing/scripts/upload_file_to_s3.py` | 로컬 파일 → S3 업로드 → 다운로드 URL 출력 |

**IMPORTANT**:
- `$WORKING_DIR/skills/...`, `skills/...`, `scripts/...` 상대경로를 쓰지 마세요. 파일이 없습니다.
- cwd가 `artifacts/`이므로 상대경로로 실행하면 실패합니다.

경로가 없을 때만 아래로 폴백하세요.

```bash
SKILL_SCRIPTS="/home/.agents/skills/s3/s3-sharing/scripts"
if [ ! -f "$SKILL_SCRIPTS/upload_file_to_s3.py" ]; then
  SKILL_SCRIPTS="$WORKING_DIR/skills/s3-sharing/scripts"
fi
```

## Critical Rules

1. 결과 파일이 있으면 **반드시** 이 스크립트로 업로드한 뒤 URL을 사용자에게 전달하세요.
2. 업로드 대상은 `artifacts/` 아래 또는 `SESSION_STORAGE_DIR`(`/mnt/workspace`) 안의 파일만 사용하세요.
3. stdout의 `Upload complete: <url>` URL만 사용자에게 보여 주세요. stderr 로그는 숨기세요.
4. 임의로 다른 버킷/키 접두사를 만들지 마세요. 허용 접두사: `artifacts/`, `images/`, `docs/`.

## Quick Start

```bash
SCRIPTS=/home/.agents/skills/s3/s3-sharing/scripts

python "$SCRIPTS/upload_file_to_s3.py" artifacts/report.pdf
python "$SCRIPTS/upload_file_to_s3.py" /mnt/workspace/<user>/artifacts/chart.png
```

성공 시 stdout 예:

```text
Upload complete: https://dxxxx.cloudfront.net/artifacts/<user_id>/report.pdf
```

## Usage (agent)

```python
import os
import subprocess

SCRIPTS = "/home/.agents/skills/s3/s3-sharing/scripts"
if not os.path.isfile(os.path.join(SCRIPTS, "upload_file_to_s3.py")):
    SCRIPTS = os.path.join(
        os.environ.get("WORKING_DIR", "/app"), "skills", "s3-sharing", "scripts"
    )

UPLOAD = os.path.join(SCRIPTS, "upload_file_to_s3.py")
result = subprocess.run(
    ["python", UPLOAD, "artifacts/report.pdf"],
    capture_output=True,
    text=True,
)
print(result.stdout or result.stderr)
```

## Environment

| 변수 | 필수 | 설명 |
| --- | --- | --- |
| `S3_BUCKET` | 예 | 프로젝트 S3 버킷 |
| `SHARING_URL` | 권장 | CloudFront base URL (`https://dxxxx.cloudfront.net`) |
| `AWS_REGION` | 아니오 | 기본 `us-west-2` |
| `AGENTCORE_USER_ID` / `ACTOR_ID` | 권장 | S3 키의 `{user_id}` 세그먼트 |
| `SESSION_STORAGE_DIR` | 아니오 | 기본 `/mnt/workspace` |
| `ARTIFACTS_DIR` | 아니오 | 있으면 `artifacts/` 경로 해석에 사용 |

CLI 오버라이드: `--bucket`, `--sharing-url`, `--region`, `--user-id`, `--session-dir`

## S3 Key Rules

- `artifacts/report.pdf` → `artifacts/{user_id}/report.pdf`
- `images/chart.png` → `images/{user_id}/chart.png`
- `ARTIFACTS_DIR` 아래 파일 → `artifacts/{user_id}/...`
- `{SESSION_STORAGE_DIR}/{user}/artifacts/...` → `artifacts/{user}/...`

## Dependencies

- `boto3` (Harness / AWS 런타임에 포함)
- Harness execution role에 `s3:PutObject` (artifacts|images|docs/*) 필요
- CloudFront OAI가 동일 버킷 GetObject 가능해야 공유 URL이 동작

## Troubleshooting

### No such file or directory / 스크립트를 찾을 수 없음

반드시 `/home/.agents/skills/s3/s3-sharing/scripts/upload_file_to_s3.py` 절대 경로를 사용하세요.

### S3 bucket is not configured

`S3_BUCKET` 환경변수가 없습니다. installer가 Harness env에 주입했는지 확인하거나 `--bucket`을 넘기세요.

### Upload failed: AccessDenied

Harness execution role에 `s3:PutObject`가 없습니다. installer 재실행으로 IAM을 갱신하세요.

### File not found

`artifacts/...` 또는 `/mnt/workspace/...` 절대 경로로 다시 시도하세요. cwd 기준 상대경로는 실패하기 쉽습니다.

### URL이 403

CloudFront `SHARING_URL`이 없거나, 객체가 허용 접두사(`artifacts/` 등) 밖에 업로드된 경우입니다. 스크립트 stdout의 키/URL을 확인하세요.
