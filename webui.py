"""agent-bulletin web UI - a tiny read-only browser view of the project bulletin boards.

Reuses RedisStore (read-only; it never advances any reader's watermark). Server-rendered
HTML, no JS build, no template files.

Run:
    uv pip install -e ".[web]"
    python webui.py                 # or: uvicorn webui:app --port 8787
Then open http://127.0.0.1:8787
"""

import html
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from markdown_it import MarkdownIt

from agent_bulletin.store import RedisStore, normalize_importance, requires_ack

app = FastAPI(title="agent-bulletin")
store = RedisStore()

# Message bodies are authored by agents in Markdown. Render it, but keep the
# CommonMark preset's raw-HTML pass-through OFF (html=False) so an agent can't
# inject <script>/<img onerror> etc.; disallowed link protocols (javascript:,
# data:) are left as inert text by markdown-it's default link validator.
_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

ACCENT = "#ff5500"  # Brandmachine orange

# Light-theme variable overrides, reused for the explicit toggle and the
# no-JS system-preference fallback so the two stay in sync.
_LIGHT_VARS = (
    "--bg:#f7f7f8;--text:#1c1d21;--border:#e4e4e7;--muted:#6b7280;"
    "--card-bg:#ffffff;--card-border:#e9e9ec;--card-border-left:#d6d6da;--card-body:#3f4651;"
    "--input-bg:#ffffff;--input-border:#d0d0d4;--empty:#9aa0a8;"
    "--badge-normal:#7b818c;--badge-low:#aeb3bb;--badge-attention:#c2410c;"
    "--code-bg:#f4f4f6;--code-inline-bg:#ececef;--code-border:#e2e2e6;"
)

STYLE = f"""
:root {{
  --accent: {ACCENT};
  --bg: #0f1115; --text: #e6e6e6; --border: #222; --muted: #888;
  --card-bg: #171a21; --card-border: #222; --card-border-left: #333; --card-body: #cfcfcf;
  --input-bg: #171a21; --input-border: #333; --empty: #777;
  --badge-normal: #5a5f6a; --badge-low: #3a3f48; --badge-attention: #b4531f;
  --code-bg: #0d1117; --code-inline-bg: #252b34; --code-border: #2a2f37;
}}
[data-theme="light"] {{ {_LIGHT_VARS} }}
@media (prefers-color-scheme: light) {{ :root:not([data-theme]) {{ {_LIGHT_VARS} }} }}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        margin: 0; background: var(--bg); color: var(--text);
        transition: background-color .15s ease, color .15s ease; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
header {{ padding: 16px 24px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; }}
header h1 {{ font-size: 18px; margin: 0; }}
.muted {{ color: var(--muted); font-size: 13px; }}
.wrap {{ max-width: 860px; margin: 0 auto; padding: 24px; }}
.card {{ background: var(--card-bg); border: 1px solid var(--card-border); border-left: 3px solid var(--card-border-left);
         border-radius: 8px; padding: 14px 16px; margin: 0 0 12px;
         transition: background-color .15s ease, border-color .15s ease; }}
.card.loud {{ border-left-color: var(--accent); }}
.card .subject {{ font-size: 15px; font-weight: 600; margin: 0 0 4px; }}
.card .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
.card .acks {{ margin-top: -4px; font-style: italic; }}
.card .body {{ white-space: pre-wrap; font-size: 14px; line-height: 1.5; color: var(--card-body); }}
.card .body.md {{ white-space: normal; word-wrap: break-word; overflow-wrap: anywhere; }}
.body.md > :first-child {{ margin-top: 0; }}
.body.md > :last-child {{ margin-bottom: 0; }}
.body.md p {{ margin: 0 0 10px; }}
.body.md ul, .body.md ol {{ margin: 0 0 10px; padding-left: 22px; }}
.body.md li {{ margin: 2px 0; }}
.body.md li > ul, .body.md li > ol {{ margin: 2px 0; }}
.body.md h1, .body.md h2, .body.md h3, .body.md h4, .body.md h5, .body.md h6 {{
  margin: 16px 0 8px; line-height: 1.3; font-weight: 600; color: var(--text); }}
.body.md h1 {{ font-size: 19px; }}
.body.md h2 {{ font-size: 17px; }}
.body.md h3 {{ font-size: 15px; }}
.body.md h4, .body.md h5, .body.md h6 {{ font-size: 14px; }}
.body.md code {{ font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  font-size: 12.5px; background: var(--code-inline-bg); border: 1px solid var(--code-border);
  padding: 0.5px 5px; border-radius: 5px; }}
.body.md pre {{ background: var(--code-bg); border: 1px solid var(--code-border); border-radius: 8px;
  padding: 12px 14px; overflow-x: auto; margin: 0 0 10px; }}
.body.md pre code {{ background: none; border: 0; padding: 0; font-size: 12.5px; line-height: 1.45; }}
.body.md blockquote {{ margin: 0 0 10px; padding: 2px 0 2px 12px;
  border-left: 3px solid var(--card-border-left); color: var(--muted); }}
.body.md a {{ text-decoration: underline; }}
.body.md img {{ max-width: 100%; height: auto; }}
.body.md hr {{ border: 0; border-top: 1px solid var(--border); margin: 14px 0; }}
.body.md table {{ border-collapse: collapse; margin: 0 0 10px; font-size: 13px;
  display: block; overflow-x: auto; }}
.body.md th, .body.md td {{ border: 1px solid var(--code-border); padding: 5px 10px; text-align: left; }}
.body.md th {{ background: var(--code-inline-bg); font-weight: 600; }}
.badge {{ display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px;
          color: #fff; vertical-align: middle; }}
.proj {{ display: flex; justify-content: space-between; align-items: center; }}
.search {{ margin: 0 0 18px; }}
.search input {{ background: var(--input-bg); border: 1px solid var(--input-border); color: var(--text);
                 padding: 8px 12px; border-radius: 6px; width: 280px; font-size: 14px; }}
.empty {{ color: var(--empty); padding: 48px 0; text-align: center; }}
.theme-toggle {{ margin-left: auto; background: transparent; border: 1px solid var(--border);
  color: var(--text); border-radius: 6px; padding: 4px 9px; font-size: 14px; cursor: pointer; line-height: 1; }}
.theme-toggle:hover {{ border-color: var(--accent); }}
"""

# Three theme modes: "auto" (no data-theme attribute, so the
# prefers-color-scheme rule above decides), "light" and "dark". Auto is
# represented by the ABSENCE of the attribute and of the localStorage key, which
# is what lets the system preference keep winning after a page load.
#
# Set the saved theme before first paint to avoid a flash of the wrong theme.
# Only light/dark are honoured: a stale or hand-edited value must fall back to
# auto rather than set a data-theme no CSS rule matches.
_HEAD_SCRIPT = (
    "<script>(function(){try{var t=localStorage.getItem('theme');"
    "if(t==='light'||t==='dark')document.documentElement.setAttribute('data-theme',t);}"
    "catch(e){}})();</script>"
)
# Toggle handler + icon sync; runs on every (auto-refreshing) page load.
# The icon shows the MODE, not the resolved colour, so that auto is visible as
# its own state; without that, auto-on-a-dark-system and forced-dark would look
# identical and the cycle would feel broken.
_BODY_SCRIPT = (
    "<script>function _bmMode(){"
    "return document.documentElement.getAttribute('data-theme')||'auto';}"
    "function _bmIcon(){var b=document.getElementById('theme-toggle');if(!b)return;"
    "var m=_bmMode();"
    "b.textContent=m==='light'?'\\u2600\\ufe0f':m==='dark'?'\\u263d':'\\u25d0';"
    "var l=m==='auto'?'Theme: auto (following system)':"
    "m==='light'?'Theme: light':'Theme: dark';"
    "b.title=l;b.setAttribute('aria-label',l+' \\u2013 click to change');}"
    "function toggleTheme(){var n={auto:'light',light:'dark',dark:'auto'}[_bmMode()];"
    "if(n==='auto'){document.documentElement.removeAttribute('data-theme');"
    "try{localStorage.removeItem('theme');}catch(e){}}"
    "else{document.documentElement.setAttribute('data-theme',n);"
    "try{localStorage.setItem('theme',n);}catch(e){}}"
    "_bmIcon();}_bmIcon();</script>"
)


def _page(title: str, body: str, *, refresh: Optional[int] = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{html.escape(title)}</title>{meta}{_HEAD_SCRIPT}<style>{STYLE}</style></head><body>'
        f'<header><h1><a href="/">agent-bulletin</a></h1>'
        f'<span class="muted">{html.escape(title)}</span>'
        f'<button id="theme-toggle" class="theme-toggle" type="button" '
        f'onclick="toggleTheme()" aria-label="Change theme" title="Change theme"></button>'
        f'</header>'
        f'<div class="wrap">{body}</div>{_BODY_SCRIPT}</body></html>'
    )


def _fmt(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError, OverflowError, TypeError):
        return ""


def _badge(importance: str) -> str:
    colors = {
        "halt": "var(--accent)",
        "attention": "var(--badge-attention)",
        "normal": "var(--badge-normal)",
        "quiet": "var(--badge-low)",
    }
    imp = normalize_importance(importance)
    return (f'<span class="badge" style="background:{colors[imp]}">'
            f'{html.escape(imp)}</span>')


def _acks(msg: dict) -> str:
    """Read receipts for the levels that expect one.

    Shown even when empty: an unacked halt is the thing worth seeing on a board, so the
    absence has to be visible rather than just missing.
    """
    if not requires_ack(msg.get("importance")):
        return ""
    names = msg.get("acked_by") or []
    text = ("read by " + ", ".join(html.escape(n) for n in names)) if names else "not acked yet"
    return f'<div class="meta acks">{text}</div>'


def _card(project: str, msg: dict, *, body_text: str, render_md: bool = True) -> str:
    imp = normalize_importance(msg.get("importance"))
    cls = "card loud" if requires_ack(imp) else "card"
    subject = html.escape(msg.get("subject") or "(no subject)")
    frm = html.escape(msg.get("from") or "unknown")
    tid = html.escape(str(msg.get("thread_id")))
    sp = html.escape(project)
    # Full bodies are rendered Markdown (safe: html=False escapes raw tags).
    # Search snippets are truncated excerpts, so we keep them as escaped
    # plain text to avoid rendering broken/half-open Markdown.
    if render_md:
        body = f'<div class="body md">{_MD.render(body_text or "")}</div>'
    else:
        body = f'<div class="body">{html.escape(body_text or "")}</div>'
    return (
        f'<div class="{cls}"><div class="subject">{subject} {_badge(imp)}</div>'
        f'<div class="meta">from <b>{frm}</b> &middot; {_fmt(msg.get("created_at", 0))} '
        f'&middot; <a href="/p/{sp}/thread/{tid}">thread {tid}</a></div>'
        f'{_acks(msg)}{body}</div>'
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    projects = store.list_projects(limit=200).get("projects", [])
    if not projects:
        return _page("boards", '<div class="empty">No boards yet. Post a message via the '
                               'agent-bulletin MCP tools and it will appear here.</div>')
    rows = []
    for p in projects:
        slug = html.escape(p["slug"])
        rows.append(
            f'<div class="card proj"><div><a href="/p/{slug}">{slug}</a> '
            f'<span class="muted">&middot; {p.get("message_count", 0)} message(s)</span></div>'
            f'<div class="muted">{_fmt(p.get("last_activity", 0))}</div></div>'
        )
    return _page("boards", "".join(rows))


@app.get("/p/{project}", response_class=HTMLResponse)
def board(project: str, q: Optional[str] = None) -> str:
    sp = html.escape(project)
    search_box = (
        f'<form class="search" method="get" action="/p/{sp}">'
        f'<input name="q" placeholder="search this board..." value="{html.escape(q or "")}"></form>'
    )
    if q:
        msgs = store.search(project, q, limit=100).get("messages", [])
        cards = "".join(
            _card(project, m, body_text=m.get("snippet", ""), render_md=False) for m in msgs
        )
        head = (f'<p class="muted">{len(msgs)} result(s) for "{html.escape(q)}" '
                f'&middot; <a href="/p/{sp}">clear</a></p>')
        body = search_box + head + (cards or '<div class="empty">No matches.</div>')
        return _page(f"{project} · search", body)

    msgs = store.list_feed(project, limit=200).get("messages", [])
    cards = "".join(_card(project, m, body_text=m.get("body", "")) for m in msgs)
    body = search_box + (cards or '<div class="empty">No messages on this board yet.</div>')
    return _page(project, body, refresh=15)


@app.get("/p/{project}/thread/{thread_id}", response_class=HTMLResponse)
def thread(project: str, thread_id: str) -> str:
    sp = html.escape(project)
    msgs = store.get_thread(project, thread_id).get("messages", [])
    back = f'<p class="muted"><a href="/p/{sp}">&larr; back to {sp}</a></p>'
    cards = "".join(_card(project, m, body_text=m.get("body", "")) for m in msgs)
    body = back + (cards or '<div class="empty">Thread not found (it may have expired).</div>')
    return _page(f"{project} · thread {html.escape(str(thread_id))}", body)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787)
