---
name: my-vaults
description: ob-note(Obsidian형 vault)에 저장된 마크다운 노트를 조회·검색·생성·수정합니다. 사용자가 "vault", "내 노트", "ob-note", "ob-docs", "위키링크", "백링크", "노트 찾아줘", "메모 저장", "vault에 적어줘", "지식베이스" 등을 요청할 때 사용합니다.
---

# my-vaults (ob-note)

standalone **ob-note** (`https://vault.my-agentic-ai.click`) vault를 API로 읽고 씁니다.  
노트는 `.md`가 Source of Truth입니다. 임의 HTTP를 새로 짜지 말고 아래 스크립트를 실행하세요.

계정(`USER_ID` / email / `ACTOR_ID`)마다 vault가 `vault/{userId}/…`로 분리됩니다.  
스크립트가 보내는 userId의 vault만 보이며, 노트 경로는 항상 **그 계정 vault 기준 상대경로**입니다
(예: `Meeting/Note.md` — path에 email을 넣지 마세요).

## When to Use

- vault / ob-note / 내 노트 / 위키 노트 조회
- 노트 검색, 파일 트리, 백링크·그래프 확인
- 새 노트 작성, 기존 노트 수정·이어쓰기·이름변경·삭제

## Script Location

`bash` / `execute_code`의 cwd는 `artifacts/`이다. 스킬 스크립트는 `$WORKING_DIR/skills/...`로 호출하세요.
(`WORKING_DIR`는 bash 도구가 주입하는 환경변수이며, Runtime에서는 `/app`이다.)

| 스크립트 | 용도 |
| --- | --- |
| `$WORKING_DIR/skills/my-vaults/scripts/read_vault.py` | 조회 (health / tree / list / read / search / graph / backlinks) |
| `$WORKING_DIR/skills/my-vaults/scripts/write_vault.py` | 쓰기 (write / append / upload / embed-images / mkdir / rename / delete / rebuild) |
| `$WORKING_DIR/skills/my-vaults/scripts/lib_vault.py` | HTTP·인증 헬퍼 (직접 실행하지 않음) |

**IMPORTANT**:
- `skills/...` 또는 `scripts/...` 상대경로를 쓰지 마세요. cwd가 `artifacts/`라 실패합니다.
- cde-pilot CloudFront(`SHARING_URL`)로 vault API를 치지 마세요. 기본은 `https://vault.my-agentic-ai.click`입니다.
- use-vault **MCP**가 함께 켜져 있으면 MCP 도구(`vault_read` 등)를 우선하고, 이 스크립트는 폴백으로 쓰세요.

## Critical Rules

1. **반드시 스크립트(또는 MCP)** 로 조회·수정하세요. `curl`로 ad-hoc API를 새로 작성하지 마세요.
2. 경로는 vault 상대경로입니다. 예: `AI/Ontology.md`, `Meeting/Weekly-Sync.md`
3. 덮어쓰기 전에 `read`로 현재 내용을 확인하세요. 부분 추가는 `append`를 우선 사용하세요.
4. 출력은 JSON입니다. 사용자에게는 한국어로 요약하고, 본문이 길면 핵심만 인용하세요.  
   **노트 path를 말할 때는 JSON의 `url`(deep link)을 함께 전달하세요.** (아래 [Note deep links](#note-deep-links))
5. 인증은 스크립트가 `USER_ID`/`ACTOR_ID`와 vault-agent-token으로 처리합니다. 쿠키를 수동으로 만들지 마세요.
6. **노트 본문 형식**: YAML frontmatter를 **넣지 마세요**. `# 제목` 한 줄로 시작하고 바로 본문을 이어서 쓰세요.
7. **새 노트는 vault 루트에 두지 마세요.** 주제 폴더 아래(`Category/Note.md`)에만 저장합니다.
8. **결과는 가능한 하나의 마크다운에 모으세요.** 같은 주제 산출물을 작은 `.md` 여러 개로 쪼개지 마세요. 분량이 많으면 제목 아래 **목차(TOC)** 를 넣으세요.
9. **이미지는 CloudFront/외부 URL을 그대로 넣지 마세요.** `/artifacts/*` 는 signed cookie가 필요해 vault에서 깨집니다. 노트 작성 시 `--embed-images`로 같은 폴더에 업로드하고 **상대경로**로 링크하세요. (아래 [Images](#images))

```markdown
# Claude Tag

## 목차
- [개요](#개요)
- [세부](#세부)

## 개요

본문…

## 세부

본문…
```

## Folder Organization

새 노트 작성 시 **반드시** 카테고리 폴더에 넣습니다. 루트에 `Something.md`를 만들지 마세요.

### 절차

1. `tree` 또는 `list`로 기존 폴더를 확인합니다.
2. 노트 주제에 맞는 폴더가 **이미 있으면 그 폴더를 재사용**합니다.
3. 없으면 `mkdir`로 새 카테고리 폴더를 만든 뒤 그 안에 노트를 저장합니다.
4. 경로 형식: `<Category>/<NoteTitle>.md` (예: `Meeting/Sprint-Review.md`)

### 카테고리 예시

| 폴더 | 넣을 노트 |
| --- | --- |
| `Productivity` | 할 일, 습관, GTD, 워크플로 |
| `AI` | 모델, 프롬프트, LLM, AI 도구 |
| `Agent` | 에이전트·스킬·런타임·자동화 로그 |
| `Meeting` | 회의록, 싱크, 액션 아이템 |
| `Project` | 프로젝트 메모·계획·로드맵 |
| `Research` | 조사, 논문, 벤치마크, POC |
| `Personal` | 개인 메모, 목표 |
| `Reference` | 치트시트, 컨벤션 |
| `Architecture` | 시스템 설계, ADR |
| `DevOps` | CI/CD, 인프라, 배포 |
| `Cloud` | AWS/GCP/Azure 메모 |
| `Security` | 위협 모델, 권한·시크릿 |
| `Product` | 요구사항, 유저 스토리 |
| `Engineering` | 구현 메모, 버그 분석 |
| `Learning` | 공부 노트, 강의 요약 |
| `Ideas` | 아이디어, 가설 |
| `Writing` | 초안, 발표 원고 |
| `Decision` | 의사결정 로그 |
| `Runbook` | 운영 절차, 장애 대응 |
| `Archive` | 완료·폐기 노트 |

위 목록에 없으면 짧은 PascalCase/영문 폴더명을 새로 만드세요. 한 노트에 주제가 겹치면 **가장 구체적인 하나**만 고릅니다.

### 금지 / 권장

- ❌ vault 루트에 직접 저장
- ❌ 같은 주제를 `Overview.md` / `Details.md`처럼 잘게 쪼개기
- ✅ 기존 `AI/`가 있으면 `AI/New-Topic.md`
- ✅ 없으면 `mkdir Meeting` 후 `Meeting/Standup-2026-03-17.md`

## Quick Start

```bash
SCRIPTS="$WORKING_DIR/skills/my-vaults/scripts"

# 연결 확인
python "$SCRIPTS/read_vault.py" health

# 노트 목록 / 트리
python "$SCRIPTS/read_vault.py" list
python "$SCRIPTS/read_vault.py" list --prefix Productivity
python "$SCRIPTS/read_vault.py" tree

# 읽기 / 검색
python "$SCRIPTS/read_vault.py" read Productivity/Convention.md
python "$SCRIPTS/read_vault.py" read Productivity/Convention.md --raw
python "$SCRIPTS/read_vault.py" search "온톨로지"

# 그래프 / 백링크
python "$SCRIPTS/read_vault.py" graph
python "$SCRIPTS/read_vault.py" backlinks Productivity/Convention.md
```

쓰기 (본문은 항상 `# 제목`으로 시작 — frontmatter 금지. **루트 저장 금지**):

```bash
SCRIPTS="$WORKING_DIR/skills/my-vaults/scripts"

python "$SCRIPTS/read_vault.py" tree
python "$SCRIPTS/write_vault.py" mkdir Meeting
python "$SCRIPTS/write_vault.py" write Meeting/Sprint-Review.md --content "# Sprint Review\n\n본문"

python "$SCRIPTS/write_vault.py" write AI/Prompt-Patterns.md --content "# Prompt Patterns\n\n본문"
python "$SCRIPTS/write_vault.py" write Research/Report.md --file "$ARTIFACTS_DIR/report.md"

# 이미지 포함 노트: 원격/로컬 그림을 노트 폴더에 넣고 상대경로로 저장
python "$SCRIPTS/write_vault.py" write Architecture/DXF-Strategy.md \
  --file "$ARTIFACTS_DIR/dxf_note.md" --embed-images

# 또는 이미지를 먼저 업로드한 뒤 상대경로로 작성
python "$SCRIPTS/write_vault.py" upload Architecture/dxf_analysis.png --file "$ARTIFACTS_DIR/dxf_analysis.png"

printf '\n## Update\n- item\n' | python "$SCRIPTS/write_vault.py" append Agent/Runtime-Log.md --stdin

python "$SCRIPTS/write_vault.py" rename Meeting/Draft.md Productivity/Draft.md
python "$SCRIPTS/write_vault.py" delete Meeting/Draft.md
```

## Images

마크다운에 `![alt](https://…cloudfront…/artifacts/…)` 를 넣으면 vault에서 **403**이 납니다.  
문서 저장 시 이미지를 **노트와 같은 폴더**에 두고 상대경로로 링크하세요.

### 권장 흐름

1. 본문에 임시로 URL 또는 파일명을 넣은 `.md`를 준비합니다.
2. `write … --embed-images` 로 저장합니다. 스크립트가 이미지를 찾아 업로드하고 링크를 `![alt](filename.png)` 로 바꿉니다.
3. 소스가 CloudFront artifacts URL이면 **로컬 `$ARTIFACTS_DIR`/파일명** 또는 S3 `artifacts/…` 를 먼저 찾고, 공개 HTTP만 직접 다운로드합니다.
4. 로컬 경로를 명시하려면 `--map 'URL=로컬경로'` 를 씁니다.

```bash
SCRIPTS="$WORKING_DIR/skills/my-vaults/scripts"

# A) 저장하면서 임베드 (권장)
python "$SCRIPTS/write_vault.py" write Architecture/DXF-Strategy.md \
  --content "# DXF Strategy\n\n![분석](https://d1jl….cloudfront.net/artifacts/ksdyb/dxf_analysis.png)\n" \
  --embed-images

# B) 로컬 파일 매핑 (CloudFront 403 대비)
python "$SCRIPTS/write_vault.py" write Architecture/DXF-Strategy.md \
  --file ./note.md --embed-images \
  --map "https://d1jl….cloudfront.net/artifacts/ksdyb/dxf_analysis.png=$ARTIFACTS_DIR/dxf_analysis.png" \
  --map "dxf_2d_drawing.png=$ARTIFACTS_DIR/dxf_2d_drawing.png"

# C) 이미지만 업로드
python "$SCRIPTS/write_vault.py" upload Architecture/dxf_analysis.png \
  --file "$ARTIFACTS_DIR/dxf_analysis.png"

# D) 기존 노트의 원격 이미지를 폴더로 끌어오기
python "$SCRIPTS/write_vault.py" embed-images Architecture/DXF-Strategy.md \
  --map "dxf_analysis.png=$ARTIFACTS_DIR/dxf_analysis.png"
```

저장 후 본문 예:

```markdown
![DXF 엔티티 분석](dxf_analysis.png)
![2D 도면](dxf_2d_drawing.png)
```

**금지**: vault 노트에 cde-pilot `SHARING_URL`/`/artifacts/*` CloudFront URL을 그대로 남기기.

### Agent usage

```python
import os
import subprocess

SCRIPTS = os.path.join(os.environ["WORKING_DIR"], "skills/my-vaults/scripts")
READ = f"{SCRIPTS}/read_vault.py"
WRITE = f"{SCRIPTS}/write_vault.py"

r = subprocess.run(["python", READ, "tree"], capture_output=True, text=True)
print(r.stdout)

r = subprocess.run(["python", READ, "search", "온톨로지"], capture_output=True, text=True)
print(r.stdout)

subprocess.run(["python", WRITE, "mkdir", "Agent"], capture_output=True, text=True)
r = subprocess.run(
    ["python", WRITE, "append", "Agent/Runtime-Log.md", "--content", "- done\\n"],
    capture_output=True, text=True,
)
print(r.stdout)
```

## Subcommands

### `read_vault.py`

| 명령 | 설명 |
| --- | --- |
| `health` | 서비스 상태 |
| `session` | 인증된 user_id 확인 |
| `tree` | 폴더 트리 |
| `list [--prefix] [--ext md\|\*]` | 플랫 파일 목록 |
| `read <path> [--raw]` | 노트 본문(+메타/백링크) |
| `search <query> [--limit N]` | 전문/메타 검색 |
| `graph` | 위키링크 그래프 |
| `backlinks <path>` | 해당 노트를 가리키는 링크 |

### `write_vault.py`

| 명령 | 설명 |
| --- | --- |
| `write <path> (--content\|--file\|--stdin) [--embed-images] [--map URL=LOCAL]` | 덮어쓰기 저장 |
| `append <path> (--content\|--file\|--stdin) [--embed-images] [--map URL=LOCAL]` | 이어쓰기 (`--no-create`로 신규 금지) |
| `upload <path> --file <local>` | 이미지/바이너리를 vault 경로에 업로드 |
| `embed-images <path> [--map URL=LOCAL]` | 기존 노트의 원격 이미지를 같은 폴더로 가져와 링크 수정 |
| `mkdir <path>` | 폴더 생성 |
| `rename <from> <to>` | 이동/이름변경 |
| `delete <path>` | 파일·폴더 삭제 |
| `rebuild` | 검색/그래프 인덱스 재생성 |

공통 옵션: `--user-id` (기본은 `USER_ID` / `CURRENT_USER_ID` / `ACTOR_ID`)

API는 ob-note 사이트 루트의 `/api/...` 입니다.

## Environment

| 변수 | 기본 | 설명 |
| --- | --- | --- |
| `OB_DOCS_URL` / `VAULT_API_URL` | `https://vault.my-agentic-ai.click` | vault base URL (cde-pilot `SHARING_URL` 사용 금지) |
| `USER_ID` / `ACTOR_ID` | (세션) | vault 소유자 — **프로덕션에서는 로그인 email** |
| `VAULT_AGENT_TOKEN` | Secrets Manager `{project}/vault-agent-token` 또는 `ob-note/vault-agent-token` (legacy `ob-docs/…`) | Agent HMAC |
| `OB_DOCS_VAULT_AGENT_SECRET` | (없음) | 토큰 시크릿 이름 강제 지정 |

`skills/my-vaults/config.json`의 `ob_docs_url` / `project_name`이 env 미설정 시 fallback입니다.

## Note deep links

쓰기·읽기·검색 JSON에는 vault 상대경로 `path`와 함께 **로그인 후 해당 노트로 이동하는** deep link `url`이 붙습니다. public share(`/s/{token}`)가 아닙니다.

```text
https://vault.my-agentic-ai.click/?note=Cloud%2FAWS+Azure+GCP+%EA%B0%80%EA%B2%A9+%EB%B9%84%EA%B5%90.md
```

- 형식: `{OB_DOCS_URL}/?note=<urlencoded vault path>`
- 스크립트가 `url` 필드를 자동으로 넣습니다.
- **사용자 응답에는 path와 클릭 가능한 `url`을 함께** 보여 주세요.

## Response tips

- `list`/`search` 결과는 path(+ `url`) 표로 정리하세요.
- `read`는 title·tags·backlinks를 함께 보여준 뒤 본문 요약을 하세요. 상단에 deep link를 넣으세요.
- 쓰기 성공 시 path · `url` · `ok: true`를 짧게 확인하면 됩니다.
- 새 노트는 frontmatter 없이 `# 제목` + 본문만, **카테고리 폴더 경로**와 deep link를 사용자에게 알려 주세요.
- 본문에 그림이 있으면 **반드시 `--embed-images`(또는 `upload` + 상대경로)** 를 쓰고, CloudFront artifacts URL을 남기지 마세요.

## Troubleshooting

### `Vault auth unavailable`

Runtime 역할에 `{project}/vault-agent-token` 또는 `ob-note/vault-agent-token` GetSecretValue가 필요합니다.  
installer가 ob-note와 동일 값으로 `{project}/vault-agent-token`을 동기화합니다.

### `Failed to reach ob-note`

`OB_DOCS_URL`이 `https://vault.my-agentic-ai.click`인지 확인하세요.  
cde-pilot CloudFront로 치면 실패합니다.

### 빈 tree / `File not found`

`USER_ID`/`ACTOR_ID`가 실제 Google email인지 확인하세요. 노트는 `vault/{email}/…` 아래에만 있습니다.
