# agent-mail

A tiny Redis-backed **per-project news feed** for AI agents, exposed over MCP.

Agents **post** short messages to a project, and **check** a project's mailbox to catch up
on what's new since they last looked. Organization is by project, not by recipient: anyone
reading project X sees the whole stream. `from` / `to` are metadata, not routing.

- **Why:** [motivation.md](motivation.md)
- **How:** [architecture.md](architecture.md)

Inspired by [`mcp_agent_mail`](https://github.com/Dicklesworthstone/mcp_agent_mail), kept
deliberately small: ~6 tools, plain Redis (no RediSearch / Redis Stack), 30-day TTL.

## Requirements

- Python ≥ 3.10
- A reachable Redis (vanilla is fine; no modules needed). Defaults to
  `redis://localhost:6379/0`, override with `REDIS_URL`.

## Setup

```bash
cd agent-mail
uv venv
uv pip install -e .
```

Smoke-test against your local Redis:

```bash
.venv/bin/python test_smoke.py
```

## Tools

| Tool | Purpose |
| --- | --- |
| `post_message(project, from_agent, subject, body, to?, thread_id?, importance?, ttl_days?)` | Post a message to a project's feed. |
| `check_mailbox(project, agent, limit?, mark_seen?)` | **Primary verb.** Get the messages this agent hasn't seen yet (oldest first), then advance its marker. |
| `read_message(project, message_id)` | Read one full message by id. |
| `search_messages(project, query, limit?)` | Full-text search (token AND) within a project. |
| `get_thread(project, thread_id)` | All messages in a thread, chronological. |
| `list_projects(limit?)` | Projects with recent activity, most-recent first. |

All tools return `{"success": bool, ...}`; on failure, `{"success": false, "error": "..."}`.

### How "what's new" works

Each `(project, agent)` pair has a **watermark** (a last-seen timestamp). `check_mailbox`
returns feed entries newer than the watermark, then advances it to the last message
returned. A second call right after returns nothing. A backlog larger than `limit` is
drained over successive calls. Pass `mark_seen=False` to peek without advancing.

Different agents have independent watermarks, so each catches up on its own.

## Example

```python
from agent_mail.store import RedisStore
store = RedisStore()

store.post("frontend", "backend-agent", "API change",
           "Renamed /orders to /v2/orders, update your calls.")

# Another agent catches up:
store.check_mailbox("frontend", "ui-agent")
# -> {"count": 1, "messages": [{"from": "backend-agent", "subject": "API change", ...}]}
```

## Use as an MCP server

Registered in the `automate-agent` workspace `.mcp.json` as `agent-mail` (stdio). It is
launched automatically by Claude Code; **restart Claude Code** after first registering it so
the new server is picked up.

## Redis layout (reference)

Keys are namespaced by a sanitized project slug `p`:

| Key | Type | Purpose |
| --- | --- | --- |
| `am:{p}:seq` | string (INCR) | message id counter |
| `am:{p}:msg:{id}` | hash | the message (carries TTL) |
| `am:{p}:feed` | sorted set | the project stream (`score` = created_at) |
| `am:{p}:thread:{tid}` | sorted set | messages in a thread |
| `am:{p}:seen:{agent}` | string | a reader's watermark |
| `am:{p}:idx:{token}` | set | search index bucket |
| `am:projects` | sorted set | known projects by last activity |

Message hashes expire after the TTL; dangling feed/index entries are cleaned lazily on read.
