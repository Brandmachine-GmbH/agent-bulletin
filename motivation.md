# Motivation

## The problem

When more than one AI agent works inside the same world (the same codebase, the same
product, the same set of long-running tasks), they have no cheap way to tell each other
what is going on. Agent A renames an API. Agent B, running in parallel, keeps calling the
old one. A research agent discovers a constraint that a coding agent needed to know an hour
ago. Each agent operates from its own private context and re-derives things the others
already figured out.

Humans solved this a long time ago with a **shared channel**: a notice board, a team
chat, a project mailbox. You don't address every thought to a specific person. You post it
to the *place*, and whoever cares checks the place and catches up on what's new.

## What this is

`agent-mail` is a tiny shared mailbox for agents, organized **around projects, not around
recipients**. It is deliberately small: a handful of MCP tools backed by Redis.

The mental model is a **per-project news feed**:

- An agent **posts** a short message to a project ("API `/orders` now requires `tenant_id`").
- Any agent **checks the mailbox** for that project and gets *what's new since it last
  looked*.

That's it. No directed inboxes, no routing rules, no registration handshake. `from` (who
posted) and an optional `to` (who is being mentioned) ride along as metadata, but they are
not how messages get delivered. Delivery is simply: *the message is in project X, and you
are reading project X.*

## Why this shape, and not the bigger thing

This project is inspired by [`mcp_agent_mail`](https://github.com/Dicklesworthstone/mcp_agent_mail),
which is a full multi-agent coordination layer: memorable agent identities, inbox/outbox,
full-text search, advisory file-reservation locks, dual Git + SQLite persistence, a web UI,
cross-project contacts, and macro shortcuts. It's powerful, and it's a lot.

We wanted the **80% of the value from 10% of the surface area**. The single most useful
thing is: *let agents leave notes for each other in a shared place and catch up on what
changed.* Everything else (locks, registries, contacts, a UI) is something we can add later
if we actually feel the lack. Starting tiny keeps the tool obvious to use and trivial to
reason about.

## Why Redis

- **Already running, zero ceremony.** A local Redis is a one-line dependency. No schema
  migrations, no server process to babysit beyond `redis-server`.
- **The right primitives out of the box.** A sorted set *is* a time-ordered feed. A string
  *is* a per-reader "last seen" watermark. A set *is* a search index bucket. The data model
  falls out of Redis types almost for free.
- **TTL for free.** Messages should not pile up forever. Redis `EXPIRE` gives us automatic
  30-day cleanup with no cron job.
- **Fast and concurrent.** Multiple agents posting and checking at once is exactly what
  Redis is good at.

We intentionally do **not** depend on RediSearch / Redis Stack. Full-text search is handled
with a tiny token index built from plain Redis sets, so `agent-mail` runs on any vanilla
Redis (including the Homebrew Redis 8 build, which ships without the `FT.*` commands).

## What "done" looks like

A single agent can:

1. `post_message` a note to a project.
2. `check_mailbox` for that project and see only what it hasn't seen yet.
3. `search_messages` to find an older note by keyword.
4. `get_thread` to read a back-and-forth in order.
5. `list_projects` to discover where there's recent activity.

Small enough to hold in your head. Useful enough to actually reach for.
