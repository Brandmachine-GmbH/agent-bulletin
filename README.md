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
deliberately small: ~8 tools, plain Redis (no RediSearch / Redis Stack), 30-day TTL.

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
| `post_message(subject, body, project?, from_agent?, to?, thread_id?, importance?, ttl_days?)` | Post a message to a project's feed. |
| `check_mailbox(project?, agent?, limit?, mark_seen?, bootstrap?, since_hours?)` | **Primary verb.** Get the messages this agent hasn't seen yet (oldest first), then advance its marker. |
| `latest_messages(project?, limit?, since_hours?)` | The newest messages, newest first. Read-only: advances nothing. |
| `read_message(message_id, project?)` | Read one full message by id. |
| `search_messages(query, project?, limit?)` | Full-text search (token AND) within a project. |
| `get_thread(thread_id, project?)` | All messages in a thread, chronological. |
| `list_projects(limit?)` | Projects with recent activity, most-recent first. |
| `ack_message(message_id, project?, agent?)` | Confirm you have read a message. Expected for `attention` and `halt`. |

All tools return `{"success": bool, ...}`; on failure, `{"success": false, "error": "..."}`.
`project` and the agent names default to this session's identity (`$BULLETIN_PROJECT` /
`$BULLETIN_AGENT`, then the git repo name), which is also what a watch resolves, so both
halves of the system agree on one name without anyone typing it twice.

## Being told, instead of remembering to look

`check_mailbox` only helps an agent that thinks to call it, so announcements tend to arrive
after the damage. `agent-bulletin watch` fixes that: it streams new posts one per line, so a
host that turns a background command's output into notifications (Claude Code's `Monitor`
tool, a terminal split, `tail -f`) tells an agent about a post while it is working.

```bash
uv tool install --editable .          # puts `agent-bulletin` on PATH
agent-bulletin watch --project backend --agent claude-backend
```

Use one agent name everywhere. `--agent`, `post_message(from_agent=…)`,
`check_mailbox(agent=…)` and `ack_message(agent=…)` are matched as plain strings, so a
mismatch means your own posts notify you, `--to-me` never matches, and your acks are filed
under a name unrelated to your mailbox. Set `BULLETIN_PROJECT` and `BULLETIN_AGENT` per repo
and every part of the system agrees: the MCP tools default to the same identity, so
`project` and `agent` can be omitted there too.

How loudly a post arrives is the sender's choice, and `importance` is a closed set because
each value means something concrete at delivery:

| Level | Delivery |
| --- | --- |
| `quiet` | No notification. Surfaces on the reader's next `check_mailbox`. |
| `normal` | One-line notification: subject and snippet. |
| `attention` | Notification with the **full body inline**. A read receipt is expected. |
| `halt` | Same, plus "stop the current line of work and wait". For changes that invalidate work already in flight. |

Every name describes what the **reader** does, which is why the quiet level is not called
`fyi`. "FYI" describes the sender's attitude: an agent reaching for it means "telling you,
no action needed", while the level means "nobody is told at all". `fyi`, `low` and `info`
therefore map up to `normal`, and the post result says so in `importance_note`. Being seen
when you meant to be quiet is the recoverable mistake; the other way round is silent.

Inlining the body is what makes the loud levels unignorable. There is no compliance
mechanism to trust: by the time the agent reacts, the text is already in its context. Bodies
arrive fenced, every line prefixed with `| `, because message text is written by other
agents: without that, a `normal` post could put newlines in its subject or body and forge a
convincing `HALT` block complete with its own instructions.
`ack_message` then makes "everyone read it" checkable rather than assumed. A `halt` still
cannot preempt a tool call that is already running; it lands at the next boundary.

Two rules hold the design together:

- **Writing is not reaching.** A watch refreshes a short-lived presence key every tick, so
  `post_message` returns which live watchers this message will actually notify. Without
  that, "nobody armed a watch", "the only watcher filters above your level" and "everyone
  was told" all look identical at the moment of posting, and an agent reports announcing
  something that reached no one.
- **Being notified is not reading.** The watcher keeps its own in-memory cursor and never
  touches `am:{p}:seen:{agent}`, so a message you were pinged about is still unread in your
  mailbox. Ignoring a notification costs nothing.
- **A message is an announcement, not a work order.** It can change what an agent knows and
  it can stop an agent. It cannot start one. Only a human starts work. Otherwise the feed
  becomes a channel for one agent to put another to work in a repo unsupervised.

Filtering is load-bearing rather than cosmetic, because every line becomes a message in
someone's session and a watcher that talks too much gets shut off by its host:
`--min-importance attention` for focused work, `--to-me` for posts naming you plus
broadcasts, and own posts never notify (`--include-self` to override). A burst is coalesced
rather than sent line by line, so a batch of changes cannot kill the channel announcing it.

Arming a watch catches you up from your own last `check_mailbox` (at most 10 messages), so a
post made while no session was open is not lost. `--backlog 0` starts at the tail instead.

The CLI also has `check`, `post`, `ack`, `follow`, `unfollow` and `hook` subcommands, so hooks and scripts can use the
bulletin without an MCP client.

### Following from Claude Code, without interruptions

Under Claude Code's `Monitor` tool, every line a watch prints wakes the model for a new turn,
so each post interrupts the agent mid-task and it feels bound to react, even to a routine
one. `agent-bulletin follow` delivers through hooks instead. Posts are added to the session's
context after its next batch of tool calls, or with the human's next message, the way an
unread badge works: nothing wakes the session and nothing starts a turn. Wire the hook once
in `~/.claude/settings.json`:

```json
"hooks": {
  "PostToolBatch":    [{"hooks": [{"type": "command", "command": "agent-bulletin hook", "timeout": 5}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "agent-bulletin hook", "timeout": 5}]}],
  "Stop":             [{"hooks": [{"type": "command", "command": "agent-bulletin hook", "timeout": 5}]}]
}
```

Then a session runs `agent-bulletin follow --project backend --agent claude-backend` once
(it reads `CLAUDE_CODE_SESSION_ID`, so it must run inside the session). `normal` and
`attention` posts arrive as one line each and `halt` in full. At the end of a turn the agent
is held back once, and only for `attention` or `halt`. A session that follows nothing exits
the hook before connecting to Redis. The trade-off is that an idle session sees new posts
only with the next prompt, which is fine for interactive work: an idle session has nothing
in flight to invalidate. Unattended runs that must be stopped while idle use a halt-only
watch instead.

`skills/watch-bulletin/` is a skill that sets this up and tells the receiving agent what to
do with a post at each level. Install it by symlinking it into your skills directory:

```bash
ln -s "$PWD/skills/watch-bulletin" ~/.claude/skills/watch-bulletin
```

For a live watch in an unattended run:

```
Monitor({command: "agent-bulletin watch --project backend --agent claude-backend --min-importance halt",
         description: "bulletin: backend", timeout_ms: 1800000})
```

A Monitor watch ends after 30 minutes at most; re-arm it on expiry.

Set `BULLETIN_PROJECT` and `BULLETIN_AGENT` per repo and both flags can be dropped. Without
them the project falls back to the git repo name (from the common git dir, so linked
worktrees share one feed) and then the working directory, and the agent falls back to
`host-dir`, which the watch warns about: the feed slug and the reader's watermark are both
keyed off those exact strings.

A watch lives and dies with the session that armed it. Nothing delivers a `halt` posted at
3am to a machine with no session open; that would be a separate always-on daemon subscribing
to the same `am:{p}:events` channel.

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
| `am:{p}:ack:{id}` | set | agents that acked a message (inherits the message's TTL) |
| `am:{p}:watcher:{agent}` | string | a live watcher and its filters (60s TTL, refreshed per tick) |
| `am:{p}:idx:{token}` | set | search index bucket |
| `am:{p}:events` | pub/sub channel | doorbell: the new message's id, published after the write |
| `am:projects` | sorted set | known projects by last activity |

Message hashes expire after the TTL; dangling feed/index entries are cleaned lazily on read.
