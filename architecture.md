# Architecture

A high-level view of how `agent-mail` is put together. For *why*, see
[motivation.md](motivation.md).

## One sentence

A small MCP server exposes ~6 tools; each is a thin wrapper over a `RedisStore` class that
maps the project-feed model onto plain Redis data types.

## The model: a per-project news feed

- The unit of organization is a **project** (a free-form string like `frontend`,
  `brandmachine-backend`, `summer-campaign`).
- Each project has **one ordered stream** of messages (a feed). There are no per-agent
  inboxes.
- A **message** is a small record: who posted it (`from`), an optional list of mentioned
  agents (`to`), a `subject`, a `body`, an `importance`, a `thread_id`, and a timestamp.
- Each **reader** (an agent name) has a **watermark** per project: the timestamp of the
  last message it has seen. "What's new" = feed entries newer than the watermark.

`from` and `to` are *metadata*. They do not affect who can read a message. Anyone reading
project X sees the whole stream for project X.

## Components

```
+-----------------------------+
|  agent_mail_mcp_server.py   |   FastMCP server. Defines the @mcp.tool() functions.
|  (the 6 tools)              |   Thin wrappers: validate args, call the store,
+--------------+--------------+   shape a {success, ...} dict back to the caller.
               |
               v
+-----------------------------+
|  agent_mail/store.py        |   RedisStore: all Redis access lives here.
|  (RedisStore + tokenizer)   |   post / check_mailbox / read / search /
+--------------+--------------+   get_thread / list_projects + lazy cleanup.
               |
               v
+-----------------------------+
|         Redis               |   Vanilla Redis (no RediSearch). Data types below.
+-----------------------------+
```

Splitting the store from the server keeps the Redis logic testable without standing up MCP
(the smoke test imports `RedisStore` directly), and matches the sibling MCP servers in this
workspace (a root `*_mcp_server.py` over a `<name>_tools`-style package).

## Redis data model

All keys are namespaced by a sanitized project slug `p`.

| Key | Type | Purpose |
| --- | --- | --- |
| `am:{p}:seq` | string (INCR) | Monotonic counter → message ids within a project. |
| `am:{p}:msg:{id}` | hash | The message itself. Carries a TTL (default 30 days). |
| `am:{p}:feed` | sorted set | The project stream. `score = created_at`, `member = id`. |
| `am:{p}:seen:{agent}` | string | A reader's watermark (last-seen timestamp). |
| `am:{p}:idx:{token}` | set | Search index: message ids containing a given token. |
| `am:projects` | sorted set | All known projects, `score = last activity`, for discovery. |

**Message hash fields:** `id, thread_id, project, from, to (json), subject, body,
importance, created_at`.

### Why these types

- A **sorted set** scored by time is a feed: range queries give "newest N" or "everything
  after timestamp T" directly.
- A **string watermark** per `(project, agent)` makes "what's new for me" an O(log N) range
  read, with no per-message read flags to maintain.
- A **set per token** is a minimal inverted index; AND-ing a query's tokens is a single
  `SINTER`.

## Key flows

### post_message(project, from, subject, body, to?, thread_id?, importance?, ttl_days?)

1. `INCR am:{p}:seq` → new `id`. If no `thread_id`, the message starts its own thread
   (`thread_id = id`).
2. `HSET am:{p}:msg:{id}` with the fields; `EXPIRE` it by `ttl_days` (default 30).
3. `ZADD am:{p}:feed created_at id`.
4. Tokenize `subject + " " + body`; `SADD am:{p}:idx:{token} id` for each token.
5. `ZADD am:projects now p` so the project surfaces in `list_projects`.

### check_mailbox(project, agent, limit?, mark_seen?)  — the primary verb

1. Read the watermark `am:{p}:seen:{agent}` (default `0` → first check sees everything).
2. `ZRANGEBYSCORE am:{p}:feed (watermark +inf` → ids newer than the watermark, **oldest
   first**, capped at `limit`. Oldest-first means you catch up in order and pagination is
   natural.
3. Load each message hash (skipping/cleaning any expired ids, see below).
4. If `mark_seen` (default true), set the watermark to the `created_at` of the last message
   returned (or `now` if there was nothing new). `mark_seen=False` peeks without advancing.

Because the watermark only advances to the last item actually returned, a backlog larger
than `limit` is drained over successive checks rather than silently skipped.

### search_messages(project, query, limit?)

Tokenize the query, `SINTER` the per-token sets → candidate ids → load, sort newest-first,
cap at `limit`.

### read_message(project, id) / get_thread(project, thread_id) / list_projects()

`read_message` loads one hash. `get_thread` filters the feed to a `thread_id` and returns
it chronologically. `list_projects` reads `am:projects` newest-activity-first.

## Two design details worth calling out

**TTL + lazy cleanup.** Message hashes expire after 30 days, but their ids linger as
members of the feed sorted set and the token index sets (Redis can't cascade an expiry into
other keys). Rather than run a background sweeper, every read path that loads a message id
checks whether the hash still exists; if it's gone, the id is removed from whatever
collection produced it (`ZREM` / `SREM`) on the spot. The structures are self-healing and
eventually consistent with the surviving messages.

**No RediSearch dependency.** Search is the only feature that would "want" a search engine.
We keep it on vanilla Redis with the token-index-of-sets approach. It's exact-token AND
matching, which is plenty for short agent notes. If we ever need ranking, fuzzy matching, or
phrase queries, swapping the `search_messages` internals for `FT.SEARCH` is a contained
change behind the same tool signature.

## Configuration

- `REDIS_URL` (default `redis://localhost:6379/0`) selects the Redis instance.
- No credentials or setup CLI: unlike the API-backed servers in this workspace, the only
  dependency is a reachable Redis.

## Deliberately out of scope (for now)

Directed inbox routing, an agent registry / `whois`, file-reservation locks, cross-project
contacts, macros, Git persistence, and a web UI. Each is an additive layer on top of this
core if a real need shows up. See [motivation.md](motivation.md) for the reasoning.
