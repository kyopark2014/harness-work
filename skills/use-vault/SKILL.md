---
name: use-vault
description: >
  ob-note(Obsidian형 vault)에 저장된 마크다운 노트를 조회·검색·생성·수정합니다.
  사용자가 "vault", "내 노트", "ob-docs", "위키링크", "백링크", "노트 찾아줘",
  "메모 저장", "vault에 적어줘", "지식베이스" 등을 요청할 때 사용합니다.
---

# use-vault (ob-note)

standalone **ob-note** (`https://vault.my-agentic-ai.click`) vault를 API로 읽고 씁니다.  
노트는 `.md`가 Source of Truth입니다. 임의 HTTP를 새로 짜지 말고 아래 스크립트를 실행하세요.

계정(`USER_ID` / email / `ACTOR_ID`)마다 vault가 `vault/{userId}/…`로 분리됩니다.  
스크립트가 보내는 userId의 vault만 보이며, 노트 경로는 항상 **그 계정 vault 기준 상대경로**입니다
(예: `Meeting/Note.md` — path에 email을 넣지 마세요).

## When to Use

- vault / ob-docs / 내 노트 / 위키 노트 조회
- 노트 검색, 파일 트리, 백링크·그래프 확인
- 새 노트 작성, 기존 노트 수정·이어쓰기·이름변경·삭제

## Script Location (AgentCore Harness)

S3 스킬은 아래 경로에 마운트됩니다. **이 절대 경로만** 사용하세요.

| 스크립트 | 용도 |
| --- | --- |
| `/home/.agents/skills/s3/use-vault/scripts/read_vault.py` | 조회 (health / tree / list / read / search / graph / backlinks) |
| `/home/.agents/skills/s3/use-vault/scripts/write_vault.py` | 쓰기 (write / append / mkdir / rename / delete / rebuild) |
| `/home/.agents/skills/s3/use-vault/scripts/lib_vault.py` | HTTP·인증 헬퍼 (직접 실행하지 않음) |

**IMPORTANT**:
- `$WORKING_DIR/skills/...`, `skills/...`, `scripts/...` 상대경로를 쓰지 마세요.
- cwd가 `artifacts/`이므로 상대경로로 실행하면 실패합니다.
- harness CloudFront(`SHARING_URL`)로 vault API를 치지 마세요. 기본은 `https://vault.my-agentic-ai.click`입니다.

경로가 없을 때만 아래로 폴백하세요.

```bash
SKILL_SCRIPTS="/home/.agents/skills/s3/use-vault/scripts"
if [ ! -f "$SKILL_SCRIPTS/read_vault.py" ]; then
  SKILL_SCRIPTS="$WORKING_DIR/skills/use-vault/scripts"
fi
```

## Critical Rules

1. **반드시 스크립트로** 조회·수정하세요. `curl`로 ad-hoc API를 새로 작성하지 마세요.
2. 경로는 vault 상대경로입니다. 예: `AI/Ontology.md`, `Meeting/Weekly-Sync.md`
3. 덮어쓰기 전에 `read`로 현재 내용을 확인하세요. 부분 추가는 `append`를 우선 사용하세요.
4. 출력은 JSON입니다. 사용자에게는 한국어로 요약하고, 본문이 길면 핵심만 인용하세요.
5. 인증은 스크립트가 `USER_ID`/`ACTOR_ID`와 vault-agent-token으로 처리합니다. 쿠키를 수동으로 만들지 마세요.
6. **노트 본문 형식**: YAML frontmatter를 **넣지 마세요**. `# 제목` 한 줄로 시작하고 바로 본문을 이어서 쓰세요.
7. **새 노트는 vault 루트에 두지 마세요.** 주제 폴더 아래(`Category/Note.md`)에만 저장합니다.
8. **결과는 가능한 하나의 마크다운에 모으세요.** 같은 주제 산출물을 작은 `.md` 여러 개로 쪼개지 마세요. 분량이 많으면 제목 아래 **목차(TOC)** 를 넣으세요.

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
| `AI` | 모델, 프롬프트, LLM 개념 |
| `Agent` | 에이전트·스킬·런타임·자동화 로그 |
| `Meeting` | 회의록, 싱크, 액션 아이템 |
| `Project` | 프로젝트 메모·계획·로드맵 |
| `Research` | 조사, 논문, 벤치마크 |
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
SCRIPTS=/home/.agents/skills/s3/use-vault/scripts

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
SCRIPTS=/home/.agents/skills/s3/use-vault/scripts

python "$SCRIPTS/read_vault.py" tree
python "$SCRIPTS/write_vault.py" mkdir Meeting
python "$SCRIPTS/write_vault.py" write Meeting/Sprint-Review.md --content "# Sprint Review\n\n본문"

python "$SCRIPTS/write_vault.py" write AI/Prompt-Patterns.md --content "# Prompt Patterns\n\n본문"
python "$SCRIPTS/write_vault.py" write Research/Report.md --file "$ARTIFACTS_DIR/report.md"

printf '\n## Update\n- item\n' | python "$SCRIPTS/write_vault.py" append Agent/Runtime-Log.md --stdin

python "$SCRIPTS/write_vault.py" rename Meeting/Draft.md Productivity/Draft.md
python "$SCRIPTS/write_vault.py" delete Meeting/Draft.md
```

### Agent usage

```python
import subprocess

SCRIPTS = "/home/.agents/skills/s3/use-vault/scripts"
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
| `write <path> (--content\|--file\|--stdin)` | 덮어쓰기 저장 |
| `append <path> (--content\|--file\|--stdin)` | 이어쓰기 (`--no-create`로 신규 금지) |
| `mkdir <path>` | 폴더 생성 |
| `rename <from> <to>` | 이동/이름변경 |
| `delete <path>` | 파일·폴더 삭제 |
| `rebuild` | 검색/그래프 인덱스 재생성 |

공통 옵션: `--user-id` (기본은 `USER_ID` / `CURRENT_USER_ID` / `ACTOR_ID`)

API는 ob-note 사이트 루트의 `/api/...` 입니다.

## Environment

| 변수 | 기본 | 설명 |
| --- | --- | --- |
| `OB_DOCS_URL` / `VAULT_API_URL` | `https://vault.my-agentic-ai.click` | ob-note base URL (harness `SHARING_URL` 사용 금지) |
| `USER_ID` / `ACTOR_ID` | (세션) | vault 소유자 — **프로덕션에서는 로그인 email** |
| `VAULT_AGENT_TOKEN` | Secrets Manager `ob-note/vault-agent-token` (legacy `ob-docs/…`) | Agent HMAC |
| `OB_DOCS_VAULT_AGENT_SECRET` | (없음) | 토큰 시크릿 이름 강제 지정 |

`skills/use-vault/config.json`의 `ob_docs_url` / `project_name`이 env 미설정 시 fallback입니다.

## Response tips

- `list`/`search` 결과는 path 표로 정리하세요.
- `read`는 title·tags·backlinks를 함께 보여준 뒤 본문 요약을 하세요.
- 쓰기 성공 시 path와 `ok: true`만 짧게 확인하면 됩니다.
- 새 노트는 frontmatter 없이 `# 제목` + 본문만, **카테고리 폴더 경로**를 사용자에게 알려 주세요.
- 조사·요약·리포트·회의록 등은 **한 파일**에 섹션으로 모으세요.

## Troubleshooting

### `Vault auth unavailable`

Harness 실행 역할에 다음 GetSecretValue가 필요합니다:
- `ob-note/vault-agent-token` (또는 legacy `ob-docs/…`)
- `harness-work/vault-agent-token` (installer가 ob-note와 동일 값으로 동기화)

두 시크릿 문자열이 동일해야 ob-note 검증에 성공합니다.  
AgentCore에서는 `session-signing-key`를 쓸 수 없습니다.

### `Failed to reach ob-note`

`OB_DOCS_URL`이 `https://vault.my-agentic-ai.click`인지 확인하세요.  
harness CloudFront(`d196…`)로 치면 실패합니다.

### 빈 tree / `File not found`

`USER_ID`/`ACTOR_ID`가 실제 Google email인지 확인하세요. 노트는 `vault/{email}/…` 아래에만 있습니다.
