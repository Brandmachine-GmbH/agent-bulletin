"""Command line entry point for agent-bulletin.

`watch` is the reason this exists. It turns the bulletin into something an agent is told
about while it works, instead of something someone has to remember to go and read. Every
line it prints to stdout is designed to be one notification in a host that streams a
background command's output into a session (Claude Code's Monitor tool, `tail -f`, a
terminal split, anything line-oriented).

The other subcommands are here because a shell-callable read and write make the same
mechanism usable from hooks, scripts, and cron without an MCP client.
"""

import argparse
import json
import sys
import time

import redis

from .identity import agent_source, resolve_agent, resolve_project
from .store import (IMPORTANCE_LEVELS, RedisStore, WATCHER_TTL, normalize_importance,
                    project_slug, requires_ack)
from .watch import format_message, safe_delivery, should_notify

# Redis errors must never end a watch. A watcher that dies on a blip is worse than one
# that is briefly late, because nobody notices it stopped.
_RECONNECT_DELAY = 5.0
_PAGE = 200
# Below this the loop stops being a poll and becomes a spin: at --interval 0 it burns 80%
# of a core hammering Redis. Sub-second latency comes from the doorbell, not from polling
# harder, so there is nothing to gain below the floor.
_MIN_INTERVAL = 0.5
# How many messages a single drain pass will render individually before it summarizes.
# The host stops monitors that produce too many events, and the trigger for this whole
# feature is "a big batch of changes landed", so a burst must not be able to kill the
# channel it is announcing itself on.
_COALESCE_AT = 3
_MAX_LOUD_IN_BURST = 3
# Messages delivered when arming a watch that is catching up from its own read watermark.
_MAX_CATCHUP = 10


def _emit(line: str) -> None:
    """Write one event. Flushed immediately: a buffered notification is a missed one."""
    print(line, flush=True)


def _warn(msg: str) -> None:
    """Diagnostics go to stderr, which hosts do not turn into notifications."""
    print(f"agent-bulletin: {msg}", file=sys.stderr, flush=True)


def _emit_batch(msgs: list, *, project: str, agent: str) -> None:
    """Render one drain pass, collapsing a burst rather than flooding the session."""
    if not msgs:
        return
    if len(msgs) <= _COALESCE_AT:
        for msg in msgs:
            _emit(format_message(msg, project=project, agent=agent))
        return
    # A burst: the loud ones are why the watch exists, so they still arrive in full. The
    # rest become one line, because a summary the reader can act on beats twenty
    # notifications that get the watcher shut off.
    loud = [m for m in msgs if requires_ack(m.get("importance"))][:_MAX_LOUD_IN_BURST]
    for msg in loud:
        _emit(format_message(msg, project=project, agent=agent))
    shown = {m.get("id") for m in loud}
    rest = [m for m in msgs if m.get("id") not in shown]
    if rest:
        ids = ", ".join("#" + str(m.get("id")) for m in rest[:20])
        more = f" and {len(rest) - 20} more" if len(rest) > 20 else ""
        _emit(f"[bulletin {project}] {len(rest)} further new messages ({ids}{more}). "
              "Call check_mailbox to read them.")


def _initial_cursor(store: RedisStore, project: str, agent: str, backlog) -> int:
    """Where a freshly armed watch starts.

    Default (`backlog` unset) is to catch up from this reader's own `seen` watermark,
    capped. Seeding at the feed tail instead would mean anything posted while no session
    was open is never notified at all, which for a one-session-at-a-time human is the
    common case rather than an edge. Reading the watermark is fine; the invariant is that
    the watcher never *advances* it.
    """
    if backlog is not None:
        return store.backlog_cursor(project, backlog)
    watermark = store.watermark(project, agent)
    if not watermark:
        # This reader has never checked here. Replaying an unknown archive at arming time
        # is worse than starting clean, so start clean.
        return store.backlog_cursor(project, 0)
    return max(watermark, store.backlog_cursor(project, _MAX_CATCHUP))


def _drain(store: RedisStore, project: str, cursors: dict, on_page) -> None:
    """Deliver everything above the cursor, committing progress page by page.

    The cursor advances past messages that were filtered out as well as delivered ones, so
    a suppressed message is never re-examined, and it is held in memory only: the watcher
    must not touch `am:{p}:seen:{agent}`, or being notified would consume the mailbox and
    the agent would find it empty when it actually looked.

    Committed per page and only after that page has been emitted. A failure mid-drain then
    re-delivers at most one page rather than everything sent so far this cycle, and can
    never skip a page that was never emitted.
    """
    # An id counter below our cursor means the id sequence was reset (a flushed project, or
    # a Redis restart with no persistence). Without this the `>` comparison can never be
    # satisfied again and the watch goes silently deaf for the rest of the session. The
    # counter, not the feed's newest id, is the signal: see `RedisStore.id_counter`.
    issued = store.id_counter(project)
    if issued < cursors[project]:
        # Reset to 0 rather than to the counter: the whole id sequence is new, so everything
        # currently in the feed is unseen. The feed is necessarily small (we notice within
        # one poll interval) and a burst gets coalesced anyway.
        _warn(f"{project}: feed id sequence reset (highest id {issued} is below cursor "
              f"{cursors[project]}); restarting from the beginning of the new sequence")
        cursors[project] = 0
    while True:
        page = store.since(project, cursors[project], limit=_PAGE)
        if page["cursor"] <= cursors[project]:
            return
        on_page(page["messages"])
        cursors[project] = page["cursor"]


def _drain_all(store: RedisStore, projects: list, cursors: dict, handler_for) -> list:
    """Drain every watched project, isolating failures to the project that caused them.

    Without the per-project boundary, one feed in a permanently bad state (a WRONGTYPE key,
    an OOM reply) aborts the whole cycle every tick, and every other watched project is
    never drained again for the life of the session. Reconnecting cannot fix that, and
    because diagnostics go to stderr the session sees nothing at all.
    """
    failed = []
    for project in projects:
        try:
            _drain(store, project, cursors, handler_for(project))
        except redis.RedisError as e:
            failed.append(project)
            _warn(f"{project}: {e}; other projects continue, will retry on the next tick")
    return failed


def cmd_watch(args) -> int:
    store = RedisStore()
    agent = resolve_agent(args.agent)
    # Slugged and de-duplicated: `my_project` and `My Project` are one feed, and without
    # this each spelling would carry its own cursor and deliver its own copy of everything.
    projects = list(dict.fromkeys(
        project_slug(resolve_project(p)) for p in (args.project or [None])))
    interval = max(_MIN_INTERVAL, float(args.interval))
    # Presence must outlast several polls or it flickers on a slow tick.
    heartbeat_ttl = max(WATCHER_TTL, int(interval * 4))

    def page_handler(project):
        def handle(messages):
            keepers = [m for m in messages
                       if should_notify(m, agent=agent, min_importance=args.min_importance,
                                        include_self=args.include_self, to_me_only=args.to_me)]
            _emit_batch(keepers, project=project, agent=agent)
        return handle

    for project in projects:
        # Arming twice for the same reader doubles every notification, and nothing else in
        # the system would catch it.
        if any(w.get("agent") == agent and w.get("mode", "watch") == "watch"
               for w in store.watchers(project)):
            _emit(f"[bulletin] warning: {agent} already has a live watch on {project}. "
                  "Two watches deliver every message twice; stop one of them.")

    cursors = {p: _initial_cursor(store, p, agent, args.backlog) for p in projects}
    _emit(f"[bulletin] watching {', '.join(projects)} as {agent} "
          f"(min-importance={args.min_importance}"
          f"{', to-me only' if args.to_me else ''})")
    if agent_source(args.agent) == "fallback":
        # Worth a notification rather than a stderr line: this is the failure that makes
        # an agent's own posts ping it back and its acks land under a stranger's name.
        _emit(f"[bulletin] warning: agent name '{agent}' was guessed from the hostname and "
              "directory. Set BULLETIN_AGENT (or pass --agent) to the same name this "
              "session uses for check_mailbox, or self-filtering and acks will not match.")

    pubsub = None
    while True:
        try:
            if pubsub is None:
                pubsub = store.r.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(*[store.events_channel(p) for p in projects])
            # Refreshed every tick so a poster can see who is listening. The TTL outlives
            # several ticks, so a slow one does not make this watcher flicker away.
            for project in projects:
                store.announce_watcher(project, agent, min_importance=args.min_importance,
                                       to_me=args.to_me, ttl=heartbeat_ttl)
            # The doorbell ping and the poll interval are the same wait: a ping wakes us
            # immediately, and a timeout falls through to the same read. That is what makes
            # a dropped or never-published ping cost latency instead of a lost message, so
            # the watcher also works against a publisher running older code.
            pubsub.get_message(timeout=interval)
            while pubsub.get_message(timeout=0) is not None:
                pass  # collapse a burst of pings into one read
        except redis.RedisError as e:
            _warn(f"redis error on the event channel: {e}; reconnecting in "
                  f"{_RECONNECT_DELAY:.0f}s")
            try:
                if pubsub is not None:
                    pubsub.close()
            except Exception:
                pass
            pubsub = None
            try:
                time.sleep(_RECONNECT_DELAY)
            except KeyboardInterrupt:
                return 0
            continue
        except KeyboardInterrupt:
            return 0

        try:
            _drain_all(store, projects, cursors, page_handler)
        except KeyboardInterrupt:
            return 0


def cmd_check(args) -> int:
    store = RedisStore()
    project = resolve_project(args.project)
    agent = resolve_agent(args.agent)
    result = store.check_mailbox(project, agent, limit=args.limit, mark_seen=not args.peek)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for msg in result["messages"]:
        # Compact even for loud levels: unlike the watcher, this reader came here on
        # purpose and can call read_message for the body.
        line = format_message(msg, project=project, agent=agent, oneline=True)
        if msg.get("acked_by_me"):
            line += "  (already acked: you saw this one as a notification)"
        _emit(line)
    if not result["messages"]:
        _emit(f"[bulletin] nothing new in {project} for {agent}")
    return 0


def cmd_post(args) -> int:
    store = RedisStore()
    body = args.body if args.body is not None else sys.stdin.read()
    project = resolve_project(args.project)
    sender = resolve_agent(args.from_agent)
    result = store.post(
        project, sender, args.subject, body, to=args.to, thread_id=args.thread_id,
        importance=args.importance,
    )
    result.update(safe_delivery(store, project, result["importance"], sender, args.to))
    print(json.dumps(result, indent=2))
    return 0


def cmd_ack(args) -> int:
    store = RedisStore()
    project = resolve_project(args.project)
    result = store.ack(project, args.message_id, resolve_agent(args.agent))
    if result is None:
        _warn(f"message {args.message_id} not found in {project} (it may have expired)")
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_follow(args) -> int:
    from . import follow
    session_id = follow.session_id_from_env(args.session)
    if not session_id:
        _warn("no session id: run this from inside a Claude Code session (it reads "
              "CLAUDE_CODE_SESSION_ID), or pass --session")
        return 1
    agent = resolve_agent(args.agent)
    projects = [resolve_project(p) for p in (args.project or [None])]
    state = follow.follow(RedisStore(), session_id, projects, agent,
                          min_importance=args.min_importance, to_me=args.to_me,
                          backlog=args.backlog)
    _emit(f"[bulletin] following {', '.join(state['projects'])} as {agent} "
          f"(min-importance={state['min_importance']}). New posts appear after your next "
          "tool call or with the user's next message; nothing wakes this session.")
    if agent_source(args.agent) == "fallback":
        _emit(f"[bulletin] warning: agent name '{agent}' was guessed from the hostname and "
              "directory. Pass --agent with the name this session uses for check_mailbox.")
    return 0


def cmd_unfollow(args) -> int:
    from . import follow
    session_id = follow.session_id_from_env(args.session)
    if not session_id:
        _warn("no session id: pass --session or run inside a Claude Code session")
        return 1
    state = follow.unfollow(RedisStore(), session_id, args.project)
    remaining = ", ".join(state["projects"]) if state else ""
    _emit(f"[bulletin] still following {remaining}" if remaining
          else "[bulletin] following nothing in this session")
    return 0


def cmd_hook(args) -> int:
    from . import follow
    return follow.hook_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bulletin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    w = sub.add_parser("watch", help="stream new messages, one notification per message")
    w.add_argument("--project", action="append",
                   help="project to watch; repeat for several (default: $BULLETIN_PROJECT, "
                        "then the git repo name, then the current directory)")
    w.add_argument("--agent", help="this reader's name (default: $BULLETIN_AGENT or host-dir)")
    w.add_argument("--min-importance", type=normalize_importance, default="normal",
                   help="quietest level worth interrupting for (default: normal, which "
                        "means quiet posts never notify)")
    w.add_argument("--to-me", action="store_true",
                   help="only messages addressed to this agent, plus broadcasts")
    w.add_argument("--include-self", action="store_true",
                   help="also notify about this agent's own posts (off by default)")
    w.add_argument("--interval", type=float, default=5.0,
                   help=f"seconds between fallback polls when no ping arrives "
                        f"(default: 5, minimum: {_MIN_INTERVAL})")
    w.add_argument("--backlog", type=int, default=None,
                   help=f"how many existing messages to deliver on arming. Default is to "
                        f"catch up from your own last check, at most {_MAX_CATCHUP}; pass 0 "
                        f"to start at the tail and ignore anything already posted")
    w.set_defaults(func=cmd_watch)

    f = sub.add_parser("follow", help="show new posts to this Claude Code session at its "
                                      "next step, through hooks, without waking it")
    f.add_argument("--project", action="append",
                   help="project to follow; repeat for several (default: $BULLETIN_PROJECT, "
                        "then the git repo name)")
    f.add_argument("--agent", help="this reader's name (default: $BULLETIN_AGENT or host-dir)")
    f.add_argument("--min-importance", type=normalize_importance, default="normal")
    f.add_argument("--to-me", action="store_true")
    f.add_argument("--backlog", type=int, default=None,
                   help=f"existing messages to show first (default: catch up from your own "
                        f"last check, at most {_MAX_CATCHUP}; 0 ignores everything so far)")
    f.add_argument("--session", help="Claude Code session id (default: $CLAUDE_CODE_SESSION_ID)")
    f.set_defaults(func=cmd_follow)

    u = sub.add_parser("unfollow", help="stop following (all projects unless --project)")
    u.add_argument("--project", action="append")
    u.add_argument("--session")
    u.set_defaults(func=cmd_unfollow)

    h = sub.add_parser("hook", help="Claude Code hook entry point (reads the hook JSON on "
                                    "stdin); wire it to PostToolBatch, UserPromptSubmit, Stop")
    h.set_defaults(func=cmd_hook)

    c = sub.add_parser("check", help="read this agent's unseen messages (advances the watermark)")
    c.add_argument("--project")
    c.add_argument("--agent")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("--peek", action="store_true", help="do not mark anything seen")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_check)

    p = sub.add_parser("post", help="post a message")
    p.add_argument("--project")
    p.add_argument("--from", dest="from_agent")
    p.add_argument("--subject", required=True)
    p.add_argument("--body", help="message body (omit to read from stdin)")
    p.add_argument("--to", action="append")
    p.add_argument("--thread-id")
    # No `choices`: a post refused over a label is a lost announcement. The store maps
    # legacy words (fyi, high, urgent) and says so in `importance_note`.
    p.add_argument("--importance", default="normal",
                   help=f"one of {', '.join(IMPORTANCE_LEVELS)} (other words are mapped)")
    p.set_defaults(func=cmd_post)

    a = sub.add_parser("ack", help="record that this agent has read a message")
    a.add_argument("--project")
    a.add_argument("--message-id", required=True)
    a.add_argument("--agent")
    a.set_defaults(func=cmd_ack)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except redis.RedisError as e:
        _warn(f"could not talk to Redis: {e}. Is redis-server running, and is REDIS_URL "
              "correct (default redis://localhost:6379/0)?")
        return 1
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        # The host closed our stdout (session ended, monitor stopped). Nothing to report to,
        # so exit quietly instead of dumping a traceback into someone's log.
        return 0


if __name__ == "__main__":
    sys.exit(main())
