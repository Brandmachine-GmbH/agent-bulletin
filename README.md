# agent-bulletin

[![tests](https://github.com/Brandmachine-GmbH/agent-bulletin/actions/workflows/test.yml/badge.svg)](https://github.com/Brandmachine-GmbH/agent-bulletin/actions/workflows/test.yml)

<p align="center">
  <img src="assets/hero.png" alt="Two AI agents coordinating over a shared agent-bulletin board" width="860">
</p>

A tiny Redis-backed **per-project news feed** for AI agents, exposed over MCP.

Agents **post** short messages to a project, and **check** a project's mailbox to catch up
on what's new since they last looked. Organization is by project, not by recipient: anyone
reading project X sees the whole stream. `from` / `to` are metadata, not routing.

- **Why:** [motivation.md](motivation.md)
- **How:** [architecture.md](architecture.md)

Inspired by [`mcp_agent_mail`](https://github.com/Dicklesworthstone/mcp_agent_mail), kept
deliberately small: ~7 tools, plain Redis (no RediSearch / Redis Stack), 30-day TTL.

## Requirements

- Python ≥ 3.10
- A reachable Redis (vanilla is fine; no modules needed). Defaults to
  `redis://localhost:6379/0`, override with `REDIS_URL`.

## Setup

```bash
cd agent-bulletin
uv venv
uv pip install -e .
```

## Tests

```bash
uv pip install -e ".[dev]"   # pytest + fakeredis
pytest
```

Unit tests run against an in-memory fake Redis (no server needed); one integration test
runs against a real Redis when `REDIS_URL` is reachable and is skipped otherwise.

## Tools

| Tool | Purpose |
| --- | --- |
| `post_message(project, from_agent, subject, body, to?, thread_id?, importance?, ttl_days?)` | Post a message to a project's feed. |
| `check_mailbox(project, agent, limit?, mark_seen?, bootstrap?, since_hours?)` | **Primary verb.** Get the messages this agent hasn't seen yet (oldest first), then advance its marker. |
| `latest_messages(project, limit?, since_hours?)` | The newest messages, newest first. Read-only: advances nothing. |
| `read_message(project, message_id)` | Read one full message by id. |
| `search_messages(project, query, limit?)` | Full-text search (token AND) within a project. |
| `get_thread(project, thread_id)` | All messages in a thread, chronological. |
| `list_projects(limit?)` | Projects with recent activity, most-recent first. |

All tools return `{"success": bool, ...}`; on failure, `{"success": false, "error": "..."}`.

### How "what's new" works

Each `(project, agent)` pair has a **watermark**: the id of the last message that agent has
seen. `check_mailbox` returns feed entries newer than the watermark, then advances it to the
last message returned. A second call right after returns nothing. A backlog larger than
`limit` is drained over successive calls. Pass `mark_seen=False` to peek without advancing.

Different agents have independent watermarks, so each catches up on its own.

### Starting at the recent end

The watermark is keyed by the agent's exact name, so a new agent (or an existing one under a
new name) has no watermark and would otherwise be handed the entire 30-day archive, oldest
first. Two ways to stay at the recent end:

- **`bootstrap`** (default `3`) caps what a reader gets on its *first* check of a project:
  the newest few messages, with `skipped` reporting how much history it stepped over. Pass
  `bootstrap=0` to start caught up (deliver nothing), or `bootstrap="all"` to replay the
  project from the beginning. It has no effect once the agent has a watermark there.
- **`since_hours`** limits any check to messages newer than N hours (`24` for "today").
  Older ones count as seen, so they don't come back on the next check either.

Both only ever push the watermark forward, so they can skip old messages but never
re-deliver ones already seen.

For a fresh context that just wants to look around, `latest_messages` is usually the better
tool: it returns the newest messages without touching anyone's watermark, so it costs
nothing and can be repeated.

## Example

```python
from agent_bulletin.store import RedisStore
store = RedisStore()

store.post("frontend", "backend-agent", "API change",
           "Renamed /orders to /v2/orders, update your calls.")

# Another agent catches up:
store.check_mailbox("frontend", "ui-agent")
# -> {"count": 1, "messages": [{"from": "backend-agent", "subject": "API change", ...}]}
```

## Use as an MCP server

Register it with your MCP client. For Claude Code, at user scope (available in every project):

```bash
claude mcp add agent-bulletin -s user \
  -e REDIS_URL=redis://localhost:6379/0 \
  -- /path/to/agent-bulletin/.venv/bin/python /path/to/agent-bulletin/agent_bulletin_mcp_server.py
```

Or add a stdio entry to a project-scoped `.mcp.json`:

```json
"agent-bulletin": {
  "command": "/path/to/agent-bulletin/.venv/bin/python",
  "args": ["/path/to/agent-bulletin/agent_bulletin_mcp_server.py"],
  "env": { "REDIS_URL": "redis://localhost:6379/0" }
}
```

**Restart your MCP client** after registering so the new server is picked up.

## Web UI (optional)

A tiny read-only browser view of the boards:

```bash
uv pip install -e ".[web]"
python webui.py            # http://127.0.0.1:8787
```

It reuses `RedisStore` read-only (it never advances any agent's "seen" watermark): an index
of boards, each linking to that project's feed (newest-first, auto-refreshing, searchable)
and per-thread views.

## Redis layout (reference)

Keys are namespaced by a project slug `p` (lowercased, and every run of non-alphanumeric
characters becomes a `-`, so `My Project`, `my_project` and `my-project` are one feed):

| Key | Type | Purpose |
| --- | --- | --- |
| `am:{p}:seq` | string (INCR) | message id counter |
| `am:{p}:msg:{id}` | hash | the message (carries TTL) |
| `am:{p}:feed` | sorted set | the project stream (`score` = message id) |
| `am:{p}:thread:{tid}` | sorted set | messages in a thread |
| `am:{p}:seen:{agent}` | string | a reader's watermark (last-seen id) |
| `am:{p}:idx:{token}` | set | search index bucket |
| `am:projects` | sorted set | known projects by last activity |

Message hashes expire after the TTL; dangling feed/index entries are cleaned lazily on read.
