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

from agent_bulletin.store import RedisStore

app = FastAPI(title="agent-bulletin")
store = RedisStore()

ACCENT = "#ff5500"  # Brandmachine orange

# Light-theme variable overrides, reused for the explicit toggle and the
# no-JS system-preference fallback so the two stay in sync.
_LIGHT_VARS = (
    "--bg:#f7f7f8;--text:#1c1d21;--border:#e4e4e7;--muted:#6b7280;"
    "--card-bg:#ffffff;--card-border:#e9e9ec;--card-border-left:#d6d6da;--card-body:#3f4651;"
    "--input-bg:#ffffff;--input-border:#d0d0d4;--empty:#9aa0a8;"
    "--badge-normal:#7b818c;--badge-low:#aeb3bb;"
)

STYLE = f"""
:root {{
  --accent: {ACCENT};
  --bg: #0f1115; --text: #e6e6e6; --border: #222; --muted: #888;
  --card-bg: #171a21; --card-border: #222; --card-border-left: #333; --card-body: #cfcfcf;
  --input-bg: #171a21; --input-border: #333; --empty: #777;
  --badge-normal: #5a5f6a; --badge-low: #3a3f48;
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
.card.high {{ border-left-color: var(--accent); }}
.card .subject {{ font-size: 15px; font-weight: 600; margin: 0 0 4px; }}
.card .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
.card .body {{ white-space: pre-wrap; font-size: 14px; line-height: 1.5; color: var(--card-body); }}
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

# Set the saved theme before first paint to avoid a flash of the wrong theme.
_HEAD_SCRIPT = (
    "<script>(function(){try{var t=localStorage.getItem('theme');"
    "if(t)document.documentElement.setAttribute('data-theme',t);}catch(e){}})();</script>"
)
# Toggle handler + icon sync; runs on every (auto-refreshing) page load.
_BODY_SCRIPT = (
    "<script>function _bmTheme(){return document.documentElement.getAttribute('data-theme')||"
    "(window.matchMedia&&matchMedia('(prefers-color-scheme: light)').matches?'light':'dark');}"
    "function _bmIcon(){var b=document.getElementById('theme-toggle');"
    "if(b)b.textContent=_bmTheme()==='light'?'\\u2600\\ufe0f':'\\u263d';}"
    "function toggleTheme(){var n=_bmTheme()==='light'?'dark':'light';"
    "document.documentElement.setAttribute('data-theme',n);"
    "try{localStorage.setItem('theme',n);}catch(e){}_bmIcon();}_bmIcon();</script>"
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
        f'onclick="toggleTheme()" aria-label="Toggle light/dark theme" title="Toggle theme"></button>'
        f'</header>'
        f'<div class="wrap">{body}</div>{_BODY_SCRIPT}</body></html>'
    )


def _fmt(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError, OverflowError, TypeError):
        return ""


def _badge(importance: str) -> str:
    colors = {"high": "var(--accent)", "normal": "var(--badge-normal)", "low": "var(--badge-low)"}
    c = colors.get(importance, "var(--badge-normal)")
    return f'<span class="badge" style="background:{c}">{html.escape(importance or "normal")}</span>'


def _card(project: str, msg: dict, *, body_text: str) -> str:
    imp = msg.get("importance", "normal")
    cls = "card high" if imp == "high" else "card"
    subject = html.escape(msg.get("subject") or "(no subject)")
    frm = html.escape(msg.get("from") or "unknown")
    tid = html.escape(str(msg.get("thread_id")))
    sp = html.escape(project)
    return (
        f'<div class="{cls}"><div class="subject">{subject} {_badge(imp)}</div>'
        f'<div class="meta">from <b>{frm}</b> &middot; {_fmt(msg.get("created_at", 0))} '
        f'&middot; <a href="/p/{sp}/thread/{tid}">thread {tid}</a></div>'
        f'<div class="body">{html.escape(body_text or "")}</div></div>'
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
        cards = "".join(_card(project, m, body_text=m.get("snippet", "")) for m in msgs)
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
