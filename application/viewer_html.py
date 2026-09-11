"""CSP-safe HTML helpers for markdown/json/csv viewers (no inline scripts, no CDN)."""

from __future__ import annotations

import csv
import html
import io
import json
import re


def _simple_markdown_to_html(text: str) -> str:
    """Minimal Markdown → HTML fallback (no external deps)."""
    escaped = html.escape(text or "")
    lines = escaped.splitlines()
    out: list[str] = []
    in_code = False
    in_ul = False
    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(line + "\n")
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            level = len(heading.group(1))
            out.append(f"<h{level}>{heading.group(2)}</h{level}>")
            continue
        if re.match(r"^[-*]\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{re.sub(r'^[-*]\s+', '', line)}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if not line.strip():
            out.append("")
            continue
        rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
        out.append(f"<p>{rendered}</p>")
    if in_code:
        out.append("</code></pre>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def markdown_to_safe_html(text: str) -> str:
    """Best-effort Markdown → HTML without client JS (CSP-safe)."""
    try:
        import markdown as md_lib  # type: ignore

        return md_lib.markdown(
            text,
            extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
            output_format="html5",
        )
    except Exception:
        return _simple_markdown_to_html(text)


_MARKDOWN_BODY_CSS = """
    .markdown-body {
      background: transparent;
      color: #e6edf3;
      line-height: 1.6;
      font-size: 15px;
    }
    .markdown-body h1, .markdown-body h2, .markdown-body h3 {
      margin: 1.2em 0 0.5em;
      font-weight: 650;
      border-bottom: 1px solid #30363d;
      padding-bottom: 0.3em;
    }
    .markdown-body p { margin: 0.75em 0; }
    .markdown-body ul, .markdown-body ol { padding-left: 1.5em; }
    .markdown-body code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.9em;
      background: rgba(110, 118, 129, 0.2);
      padding: 0.15em 0.4em;
      border-radius: 4px;
    }
    .markdown-body pre {
      overflow-x: auto;
      padding: 12px 14px;
      border-radius: 8px;
      background: rgba(110, 118, 129, 0.15);
      border: 1px solid #30363d;
    }
    .markdown-body pre code {
      background: transparent;
      padding: 0;
    }
    .markdown-body table {
      border-collapse: collapse;
      width: 100%;
      margin: 1em 0;
      font-size: 14px;
    }
    .markdown-body th, .markdown-body td {
      border: 1px solid #30363d;
      padding: 6px 10px;
      text-align: left;
    }
    .markdown-body a { color: #58a6ff; }
    .markdown-body blockquote {
      margin: 0.75em 0;
      padding: 0 1em;
      border-left: 3px solid #30363d;
      color: #8b949e;
    }
    @media (prefers-color-scheme: light) {
      .markdown-body { color: #1f2328; }
      .markdown-body h1, .markdown-body h2, .markdown-body h3,
      .markdown-body th, .markdown-body td,
      .markdown-body pre, .markdown-body blockquote {
        border-color: #d0d7de;
      }
      .markdown-body blockquote { color: #656d76; }
    }
"""


def build_markdown_viewer_page(
    file_name: str,
    text: str,
    *,
    topbar_right_html: str = "",
) -> str:
    """Full HTML document for markdown preview (CSP-safe)."""
    title = html.escape(file_name)
    body_inner = markdown_to_safe_html(text)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0;
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    .topbar {{
      position: sticky; top: 0; z-index: 2;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 20px;
      border-bottom: 1px solid #30363d;
      background: rgba(13, 17, 23, 0.92);
      backdrop-filter: blur(8px);
    }}
    .topbar h1 {{
      margin: 0; font-size: 14px; font-weight: 600;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    .topbar-actions {{
      display: flex; align-items: center; gap: 14px; flex-shrink: 0;
    }}
    .topbar a.action {{
      color: #58a6ff; text-decoration: none; font-size: 13px; white-space: nowrap;
    }}
    .topbar a.action:hover {{ text-decoration: underline; }}
    .wrap {{
      box-sizing: border-box;
      max-width: 980px;
      margin: 0 auto;
      padding: 24px 20px 64px;
    }}
    {_MARKDOWN_BODY_CSS}
    @media (prefers-color-scheme: light) {{
      body {{ background: #ffffff; color: #1f2328; }}
      .topbar {{ background: rgba(255,255,255,0.92); border-bottom-color: #d0d7de; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>{title}</h1>
    <div class="topbar-actions">{topbar_right_html}</div>
  </div>
  <div class="wrap">
    <article class="markdown-body">{body_inner}</article>
  </div>
</body>
</html>
"""


def build_json_viewer_page(
    file_name: str,
    text: str,
    *,
    topbar_right_html: str = "",
) -> str:
    """Full HTML document for JSON preview (pretty-printed, CSP-safe)."""
    title = html.escape(file_name)
    try:
        data = json.loads(text)
        pretty = html.escape(json.dumps(data, ensure_ascii=False, indent=2))
        badge = "JSON viewer"
    except Exception:
        pretty = html.escape(text)
        badge = "JSON viewer (invalid JSON — raw text)"
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0;
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    .topbar {{
      position: sticky; top: 0; z-index: 2;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 20px;
      border-bottom: 1px solid #30363d;
      background: rgba(13, 17, 23, 0.92);
      backdrop-filter: blur(8px);
    }}
    .topbar h1 {{
      margin: 0; font-size: 14px; font-weight: 600;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    .topbar-actions {{
      display: flex; align-items: center; gap: 14px; flex-shrink: 0;
    }}
    .topbar a.action {{
      color: #58a6ff; text-decoration: none; font-size: 13px; white-space: nowrap;
    }}
    .topbar a.action:hover {{ text-decoration: underline; }}
    .badge {{
      font-size: 12px; color: #8b949e; margin-right: 4px; white-space: nowrap;
    }}
    .wrap {{
      box-sizing: border-box;
      max-width: 1100px;
      margin: 0 auto;
      padding: 20px 16px 64px;
    }}
    pre.json {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px;
      line-height: 1.55;
      padding: 16px 18px;
      border-radius: 8px;
      background: rgba(110, 118, 129, 0.12);
      border: 1px solid #30363d;
    }}
    @media (prefers-color-scheme: light) {{
      body {{ background: #ffffff; color: #1f2328; }}
      .topbar {{ background: rgba(255,255,255,0.92); border-bottom-color: #d0d7de; }}
      pre.json {{ border-color: #d0d7de; background: rgba(175, 184, 193, 0.12); }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>{title}</h1>
    <div class="topbar-actions">
      <span class="badge">{html.escape(badge)}</span>
      {topbar_right_html}
    </div>
  </div>
  <div class="wrap">
    <pre class="json">{pretty}</pre>
  </div>
</body>
</html>
"""


_CSV_MAX_PREVIEW_ROWS = 5000


def build_csv_viewer_page(
    file_name: str,
    text: str,
    *,
    topbar_right_html: str = "",
    max_rows: int = _CSV_MAX_PREVIEW_ROWS,
) -> str:
    """Full HTML document for CSV preview as a table (CSP-safe)."""
    title = html.escape(file_name)
    sample = text[:4096] if text else ""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
    except Exception:
        dialect = csv.excel

    rows: list[list[str]] = []
    truncated = False
    try:
        reader = csv.reader(io.StringIO(text or ""), dialect)
        for i, row in enumerate(reader):
            if i >= max_rows:
                truncated = True
                break
            rows.append([str(cell) for cell in row])
        badge = f"CSV viewer · {len(rows)} rows"
        if truncated:
            badge += f" (first {max_rows} shown)"
    except Exception:
        rows = []
        badge = "CSV viewer (parse failed — raw text)"
        body = f'<pre class="raw">{html.escape(text or "")}</pre>'
        return _csv_shell(title, badge, topbar_right_html, body)

    if not rows:
        body = '<p class="empty">Empty CSV</p>'
        return _csv_shell(title, badge, topbar_right_html, body)

    header = rows[0]
    body_rows = rows[1:] if len(rows) > 1 else []
    thead = "".join(f"<th>{html.escape(c)}</th>" for c in header)
    tbody_parts: list[str] = []
    for row in body_rows:
        # Pad/truncate to header width for aligned columns
        cells = list(row) + [""] * max(0, len(header) - len(row))
        cells = cells[: len(header)] if header else cells
        tbody_parts.append(
            "<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>"
        )
    # If only one row, treat as data without header
    if len(rows) == 1:
        body = (
            '<table><tbody><tr>'
            + "".join(f"<td>{html.escape(c)}</td>" for c in header)
            + "</tr></tbody></table>"
        )
    else:
        body = (
            f"<table><thead><tr>{thead}</tr></thead>"
            f"<tbody>{''.join(tbody_parts)}</tbody></table>"
        )
    return _csv_shell(title, badge, topbar_right_html, body)


def _csv_shell(
    title: str, badge: str, topbar_right_html: str, body_inner: str
) -> str:
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0;
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    .topbar {{
      position: sticky; top: 0; z-index: 2;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 20px;
      border-bottom: 1px solid #30363d;
      background: rgba(13, 17, 23, 0.92);
      backdrop-filter: blur(8px);
    }}
    .topbar h1 {{
      margin: 0; font-size: 14px; font-weight: 600;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    .topbar-actions {{
      display: flex; align-items: center; gap: 14px; flex-shrink: 0;
    }}
    .topbar a.action {{
      color: #58a6ff; text-decoration: none; font-size: 13px; white-space: nowrap;
    }}
    .topbar a.action:hover {{ text-decoration: underline; }}
    .badge {{
      font-size: 12px; color: #8b949e; margin-right: 4px; white-space: nowrap;
    }}
    .wrap {{
      box-sizing: border-box;
      max-width: 100%;
      margin: 0 auto;
      padding: 16px 12px 64px;
      overflow-x: auto;
    }}
    table {{
      border-collapse: collapse;
      width: max-content;
      min-width: 100%;
      font-size: 13px;
    }}
    th, td {{
      border: 1px solid #30363d;
      padding: 6px 10px;
      text-align: left;
      vertical-align: top;
      max-width: 420px;
      white-space: pre-wrap;
      word-break: break-word;
    }}
    thead th {{
      position: sticky; top: 52px; z-index: 1;
      background: #161b22;
      font-weight: 600;
    }}
    tbody tr:nth-child(even) {{ background: rgba(110, 118, 129, 0.08); }}
    p.empty {{ color: #8b949e; padding: 24px; }}
    pre.raw {{
      margin: 0; padding: 16px; white-space: pre-wrap; word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px;
    }}
    @media (prefers-color-scheme: light) {{
      body {{ background: #ffffff; color: #1f2328; }}
      .topbar {{ background: rgba(255,255,255,0.92); border-bottom-color: #d0d7de; }}
      th, td {{ border-color: #d0d7de; }}
      thead th {{ background: #f6f8fa; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>{title}</h1>
    <div class="topbar-actions">
      <span class="badge">{html.escape(badge)}</span>
      {topbar_right_html}
    </div>
  </div>
  <div class="wrap">
    {body_inner}
  </div>
</body>
</html>
"""
