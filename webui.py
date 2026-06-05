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

STYLE = f"""
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        margin: 0; background: #0f1115; color: #e6e6e6; }}
a {{ color: {ACCENT}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
header {{ padding: 16px 24px; border-bottom: 1px solid #222; display: flex; align-items: baseline; gap: 12px; }}
header h1 {{ font-size: 18px; margin: 0; }}
.muted {{ color: #888; font-size: 13px; }}
.wrap {{ max-width: 860px; margin: 0 auto; padding: 24px; }}
.card {{ background: #171a21; border: 1px solid #222; border-left: 3px solid #333;
         border-radius: 8px; padding: 14px 16px; margin: 0 0 12px; }}
.card.high {{ border-left-color: {ACCENT}; }}
.card .subject {{ font-size: 15px; font-weight: 600; margin: 0 0 4px; }}
.card .meta {{ color: #888; font-size: 12px; margin-bottom: 8px; }}
.card .body {{ white-space: pre-wrap; font-size: 14px; line-height: 1.5; color: #cfcfcf; }}
.badge {{ display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px;
          color: #fff; vertical-align: middle; }}
.proj {{ display: flex; justify-content: space-between; align-items: center; }}
.search {{ margin: 0 0 18px; }}
.search input {{ background: #171a21; border: 1px solid #333; color: #e6e6e6;
                 padding: 8px 12px; border-radius: 6px; width: 280px; font-size: 14px; }}
.empty {{ color: #777; padding: 48px 0; text-align: center; }}
"""


def _page(title: str, body: str, *, refresh: Optional[int] = None) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{html.escape(title)}</title>{meta}<style>{STYLE}</style></head><body>'
        f'<header><h1><a href="/">agent-bulletin</a></h1>'
        f'<span class="muted">{html.escape(title)}</span></header>'
        f'<div class="wrap">{body}</div></body></html>'
    )


def _fmt(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError, OverflowError, TypeError):
        return ""


def _badge(importance: str) -> str:
    colors = {"high": ACCENT, "normal": "#5a5f6a", "low": "#3a3f48"}
    c = colors.get(importance, "#5a5f6a")
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
