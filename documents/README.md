# Documents / 문서 동기화

Per-user project & drawing document staging (ESS subset) for agentic-work.

## Layout

```
.session_storage/{user}/documents/
  projects/              uploaded sources + extracted {stem}.md / {stem}.json
  project_list.json
  drawings/
  drawings_list.json
  out/
    converted/           FMP intermediates (.pdf_pages)
    manifest.json
    .documents_sync_status.json
```

## Modules

| File | Role |
|------|------|
| `doc_list.py` | Registry for **projects** + **drawings** only |
| `pdf2text.py` | Shared PDF → text / Foundation Model Parser extractor |
| `sync_documents.py` | Sync CLI: PDF/docs → markdown next to sources |

## Usage

```bash
python documents/sync_documents.py --user alice
python documents/sync_documents.py --user alice --full --model "Claude 4.6 Sonnet"
```

Settings (per-user `settings.json`):

- `documents_foundation_model_parser_enabled` (default: `true`)
- `documents_parallel_processing_enabled` (default: `true`)

API prefix: `/api/documents` (Configure / Projects / Drawings / Sync).

## 한국어 요약

사용자별 **Projects**·**Drawings** PDF/문서를 업로드하고 Sync하면 마크다운으로 추출합니다.
Regulations / Test Cases는 포함하지 않습니다.
