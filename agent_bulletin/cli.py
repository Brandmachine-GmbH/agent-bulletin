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

from .store import IMPORTANCE_LEVELS, RedisStore
from .watch import format_message, resolve_agent, resolve_project, should_notify

# Redis errors must never end a watch. A watcher that dies on a blip is worse than one
# that is briefly late, because nobody notices it stopped.
_RECONNECT_DELAY = 5.0
_PAGE = 200


def _emit(line: str) -> None:
    """Write one event. Flushed immediately: a buffered notification is a missed one."""
    print(line, flush=True)


def _warn(msg: str) -> None:
    """Diagnostics go to stderr, which hosts do not turn into notifications."""
    print(f"agent-bulletin: {msg}", file=sys.stderr, flush=True)


def _drain(store: RedisStore, project: str, cursor: int, on_message) -> int:
    """Deliver everything above `cursor`, returning the new cursor.

    The cursor advances past messages that were filtered out as well as delivered ones, so
    a suppressed message is never re-examined, and it is held in memory only: the watcher
    must not touch `am:{p}:seen:{agent}`, or being notified would consume the mailbox and
    the agent would find it empty when it actually looked.
    """
    while True:
        page = store.since(project, cursor, limit=_PAGE)
        if page["cursor"] <= cursor:
            return cursor
        cursor = page["cursor"]
        for msg in page["messages"]:
            on_message(msg)


def cmd_watch(args) -> int:
    store = RedisStore()
    agent = resolve_agent(args.agent)
    projects = [resolve_project(p) for p in (args.project or [None])]

    def deliver(project):
        def handler(msg):
            if should_notify(msg, agent=agent, min_importance=args.min_importance,
                             include_self=args.include_self, to_me_only=args.to_me):
                _emit(format_message(msg, project=project, agent=agent))
        return handler

    cursors = {p: store.backlog_cursor(p, args.backlog) for p in projects}
    _emit(f"[bulletin] watching {', '.join(projects)} as {agent} "
          f"(min-importance={args.min_importance}"
          f"{', to-me only' if args.to_me else ''})")

    pubsub = None
    while True:
        try:
            if pubsub is None:
                pubsub = store.r.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(*[store.events_channel(p) for p in projects])
            # The doorbell ping and the poll interval are the same wait: a ping wakes us
            # immediately, and a timeout falls through to the same read. That is what makes
            # a dropped or never-published ping cost latency instead of a lost message, so
            # the watcher also works against a publisher running older code.
            pubsub.get_message(timeout=args.interval)
            while pubsub.get_message(timeout=0) is not None:
                pass  # collapse a burst of pings into one read
            for project in projects:
                cursors[project] = _drain(store, project, cursors[project], deliver(project))
        except redis.RedisError as e:
            _warn(f"redis error: {e}; reconnecting in {_RECONNECT_DELAY:.0f}s")
            try:
                if pubsub is not None:
                    pubsub.close()
            except Exception:
                pass
            pubsub = None
            time.sleep(_RECONNECT_DELAY)
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
        _emit(format_message(msg, project=project, agent=agent, oneline=True))
    if not result["messages"]:
        _emit(f"[bulletin] nothing new in {project} for {agent}")
    return 0


def cmd_post(args) -> int:
    store = RedisStore()
    body = args.body if args.body is not None else sys.stdin.read()
    result = store.post(
        resolve_project(args.project), resolve_agent(args.from_agent),
        args.subject, body, to=args.to, thread_id=args.thread_id,
        importance=args.importance,
    )
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-bulletin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    w = sub.add_parser("watch", help="stream new messages, one notification per message")
    w.add_argument("--project", action="append",
                   help="project to watch; repeat for several (default: $BULLETIN_PROJECT "
                        "or the current directory name)")
    w.add_argument("--agent", help="this reader's name (default: $BULLETIN_AGENT or host-dir)")
    w.add_argument("--min-importance", choices=IMPORTANCE_LEVELS, default="normal",
                   help="quietest level worth interrupting for (default: normal, which "
                        "means fyi posts never notify)")
    w.add_argument("--to-me", action="store_true",
                   help="only messages addressed to this agent, plus broadcasts")
    w.add_argument("--include-self", action="store_true",
                   help="also notify about this agent's own posts (off by default)")
    w.add_argument("--interval", type=float, default=5.0,
                   help="seconds between fallback polls when no ping arrives (default: 5)")
    w.add_argument("--backlog", type=int, default=0,
                   help="how many existing messages to deliver on arming (default: 0)")
    w.set_defaults(func=cmd_watch)

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
    p.add_argument("--importance", choices=IMPORTANCE_LEVELS, default="normal")
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


if __name__ == "__main__":
    sys.exit(main())
