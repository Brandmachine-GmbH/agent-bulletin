"""RedisStore - all Redis access for agent-bulletin.

The model is a per-project news feed (see architecture.md):

  - messages live in one ordered stream per project (a sorted set),
  - readers track "what's new" with a per-(project, agent) watermark,
  - search uses a tiny token inverted index built from plain Redis sets,
  - messages auto-expire after a TTL; feed/index entries are cleaned lazily on read.

No RediSearch / Redis Stack is required - this runs on vanilla Redis.
"""

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Union

import redis

DEFAULT_TTL_DAYS = 30
MAX_TTL_DAYS = 3650   # 10 years: upper bound so ttl_days can't be set to something absurd
MAX_LIMIT = 500       # upper bound on messages returned by a single call
DEFAULT_BOOTSTRAP = 3  # messages a first-time reader gets instead of the whole archive
PROJECTS_KEY = "am:projects"

# Importance is a CLOSED vocabulary, ordered quietest to loudest, because each value
# drives real delivery behaviour in the watcher rather than being a label:
#
#   fyi       no notification at all; surfaces only on the next check_mailbox
#   normal    one-line notification (subject + snippet)
#   attention notification with the FULL body inline, and an ack is expected
#   halt      same as attention, plus "stop the current line of work and wait"
#
# Inlining the body is what makes "you must read this" real: once the text is in the
# reader's transcript it has been read, with no compliance mechanism to trust. `halt`
# still cannot preempt a tool call that is already running; it lands at the next
# boundary. See architecture.md.
IMPORTANCE_LEVELS = ("fyi", "normal", "attention", "halt")
DEFAULT_IMPORTANCE = "normal"
# The level from which a read receipt is expected. Keeping this at "attention" (rather
# than halt-only) is deliberate: acks stay rare enough that a MISSING one is noticeable.
ACK_FROM_IMPORTANCE = "attention"

# Pre-vocabulary posts, and humans reaching for words the set does not have, land on the
# nearest level instead of being rejected.
_LEGACY_IMPORTANCE = {
    "low": "fyi",
    "info": "fyi",
    "high": "attention",
    "urgent": "halt",
    "critical": "halt",
}
_IMPORTANCE_RANK = {name: i for i, name in enumerate(IMPORTANCE_LEVELS)}

# Unicode-aware: \w matches letters/digits/underscore across scripts (accents, CJK, ...).
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
# Everything that is not a letter or digit collapses to "-", so "my_project", "my project"
# and "my-project" are one feed rather than three.
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SNIPPET_LEN = 160


def _clamp_limit(limit: int) -> int:
    """Coerce a caller-supplied limit into [1, MAX_LIMIT]."""
    return min(max(1, int(limit)), MAX_LIMIT)


def _slug(project: Optional[str]) -> str:
    """Sanitize a free-form project name into a key-safe slug."""
    s = (project or "default").strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or "default"


def _tokenize(text: str) -> set:
    """Tokenize for search: NFKC-normalize, casefold, split on non-word characters, drop
    tokens shorter than 2 chars. Unicode-aware, so accented and non-Latin text (e.g.
    "Mueller"/"Müller", "café", CJK) tokenizes sensibly instead of being shredded to ASCII."""
    text = unicodedata.normalize("NFKC", text or "")
    return {t for t in _TOKEN_RE.findall(text.casefold()) if len(t) >= 2}


def _bootstrap_keep(value: Union[None, int, str]) -> Optional[int]:
    """How many recent messages a first-time reader should be given.

    None means "no floor at all", i.e. replay the project from the very beginning; only
    the explicit string "all" asks for that. 0 means the opposite, "start caught up": the
    first check delivers nothing. Anything unparseable, None included (read as "not
    specified"), falls back to the default rather than dumping an archive on a reader that
    never asked for one.
    """
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("all", "everything"):
            return None
        if s in ("", "recent", "default"):
            return DEFAULT_BOOTSTRAP
        value = s
    if value is None:
        return DEFAULT_BOOTSTRAP
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BOOTSTRAP
    return max(0, n)


def _normalize_agents(value: Union[None, str, list]) -> list:
    """Accept None, a single name, or a list of names; return a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


def normalize_importance(value: Optional[str]) -> str:
    """Coerce a caller-supplied importance onto the closed vocabulary.

    Unknown words fall back to the default rather than raising: a rejected post is a lost
    announcement, and the whole point of the loud levels is that they arrive. Callers that
    want to know they were misunderstood can compare the returned value.
    """
    s = (value or "").strip().lower()
    if not s:
        return DEFAULT_IMPORTANCE
    s = _LEGACY_IMPORTANCE.get(s, s)
    return s if s in _IMPORTANCE_RANK else DEFAULT_IMPORTANCE


def importance_at_least(value: Optional[str], minimum: Optional[str]) -> bool:
    """True when `value` is at least as loud as `minimum` on the importance scale."""
    return _IMPORTANCE_RANK[normalize_importance(value)] >= _IMPORTANCE_RANK[
        normalize_importance(minimum)
    ]


def requires_ack(value: Optional[str]) -> bool:
    """True for levels where the sender is entitled to a read receipt."""
    return importance_at_least(value, ACK_FROM_IMPORTANCE)


def _iso(ts: float) -> str:
    """Render a unix timestamp as a UTC ISO-8601 string for human readability."""
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


def _hydrate(data: dict) -> dict:
    """Turn a raw Redis hash into a friendly message dict."""
    try:
        to_list = json.loads(data.get("to", "[]"))
    except (ValueError, TypeError):
        to_list = []
    try:
        created_at = float(data.get("created_at", "0"))
    except (ValueError, TypeError):
        created_at = 0.0
    return {
        "id": data.get("id"),
        "thread_id": data.get("thread_id"),
        "project": data.get("project"),
        "from": data.get("from"),
        "to": to_list,
        "subject": data.get("subject", ""),
        "body": data.get("body", ""),
        # Normalized on read as well as on write, so messages posted before the closed
        # vocabulary existed still rank and filter correctly.
        "importance": normalize_importance(data.get("importance")),
        "created_at": created_at,
        "created_at_iso": _iso(created_at),
    }


def _summarize(msg: dict) -> dict:
    """A lightweight view of a message (body replaced by a snippet)."""
    body = msg.get("body", "") or ""
    snippet = body if len(body) <= _SNIPPET_LEN else body[: _SNIPPET_LEN - 3] + "..."
    return {
        "id": msg["id"],
        "thread_id": msg["thread_id"],
        "from": msg["from"],
        "to": msg["to"],
        "subject": msg["subject"],
        "importance": msg["importance"],
        "requires_ack": requires_ack(msg["importance"]),
        "created_at": msg["created_at"],
        "created_at_iso": msg["created_at_iso"],
        "snippet": snippet,
    }


class RedisStore:
    """All Redis operations for the agent-bulletin news feed."""

    def __init__(self, url: Optional[str] = None, client=None):
        if client is not None:
            self.r = client
        else:
            url = url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            self.r = redis.Redis.from_url(url, decode_responses=True)

    # ------------------------------------------------------------------ keys
    def _k(self, p: str, *parts: str) -> str:
        return ":".join(["am", p, *parts])

    def events_channel(self, project: str) -> str:
        """Pub/sub channel a watcher subscribes to for this project's doorbell pings."""
        return self._k(_slug(project), "events")

    @staticmethod
    def _now() -> float:
        return time.time()

    def ping(self) -> bool:
        return self.r.ping()

    # ------------------------------------------------------- watermark floors
    def _created_at(self, p: str, msg_id: str) -> Optional[float]:
        """Timestamp of one message, or None if its hash has expired."""
        raw = self.r.hget(self._k(p, "msg", msg_id), "created_at")
        try:
            return float(raw) if raw is not None else None
        except (ValueError, TypeError):
            return None

    def _bootstrap_floor(self, feed_key: str, keep: int) -> int:
        """Exclusive lower bound that leaves only the newest `keep` messages unseen.

        Used for a reader's very first look at a project: a fresh (or renamed) agent
        should get a short orientation, not a 30-day archive replayed oldest-first.
        `keep=0` floors at the newest message, so the reader simply starts caught up.
        """
        if keep <= 0:
            newest = self.r.zrevrange(feed_key, 0, 0)
            return int(newest[0]) if newest else 0
        tail = self.r.zrevrange(feed_key, 0, _clamp_limit(keep) - 1)  # newest first
        return int(tail[-1]) - 1 if tail else 0

    def _since_floor(self, p: str, feed_key: str, since_hours: float) -> int:
        """Exclusive lower bound that leaves only messages newer than `since_hours` unseen.

        Walks the feed newest-first and stops at the first message older than the cutoff.
        A binary search over ranks would be cheaper, but it is not safe here: `ttl_days` is
        per message, so an expired hash can sit anywhere in the feed rather than only at
        the old end, and a search that has to guess which side of the cutoff a dangling
        entry falls on can floor the watermark past live, unread messages, which the
        persisted floor then hides forever. Walking lets a dangling entry simply be
        skipped: it can never be delivered, so it can never be the boundary. Feeds stay
        small (the TTL keeps them in the hundreds) and the walk stops at the cutoff rather
        than running the whole feed.

        Returns 0 when everything is recent enough, or the newest id when nothing is.
        """
        cutoff = self._now() - max(0.0, float(since_hours)) * 3600.0
        for msg_id in self.r.zrevrange(feed_key, 0, -1):  # newest first
            ts = self._created_at(p, msg_id)
            if ts is None:
                continue
            if ts < cutoff:
                return int(msg_id)
        return 0

    # ----------------------------------------------------------------- loading
    def _load(self, p: str, msg_id: str, *, source_zset: Optional[str] = None,
              source_sets: Optional[list] = None) -> Optional[dict]:
        """Load one message. If its hash has expired, lazily remove the dangling id
        from whatever collection produced it and return None."""
        data = self.r.hgetall(self._k(p, "msg", msg_id))
        if not data:
            if source_zset:
                self.r.zrem(source_zset, msg_id)
            if source_sets:
                for s in source_sets:
                    self.r.srem(s, msg_id)
            return None
        return _hydrate(data)

    # -------------------------------------------------------------- paging
    def _page_after(self, p: str, feed_key: str, after_id: int, lim: int):
        """One page of messages strictly newer than `after_id`, oldest first.

        The single ordering path shared by check_mailbox (which persists a watermark) and
        since()/watch (which do not), so a notifier and a reader can never disagree about
        what "newer than N" means.

        Returns (messages, last_id, scanned). `last_id` advances past every id scanned,
        including ones whose hash had expired, so an expired tail cannot wedge a cursor.
        `scanned` distinguishes "found nothing" from "found only dangling ids".
        """
        ids = self.r.zrangebyscore(feed_key, f"({after_id}", "+inf", start=0, num=lim)
        msgs = []
        last_id = after_id
        for msg_id in ids:
            last_id = max(last_id, int(msg_id))
            msg = self._load(p, msg_id, source_zset=feed_key)
            if msg is None:
                continue
            msgs.append(msg)
        return msgs, last_id, bool(ids)

    def since(self, project: str, after_id: Union[int, str] = 0,
              limit: int = MAX_LIMIT) -> dict:
        """Full messages strictly newer than `after_id`, oldest first, with NO side effects.

        The read primitive behind the watcher: it never touches `am:{p}:seen:{agent}`, so
        being notified about a message does not consume it. The mailbox stays the only
        thing that marks anything read.
        """
        p = _slug(project)
        try:
            after = max(0, int(after_id))
        except (TypeError, ValueError):
            after = 0
        msgs, last_id, _ = self._page_after(p, self._k(p, "feed"), after, _clamp_limit(limit))
        return {"project": p, "cursor": last_id, "count": len(msgs), "messages": msgs}

    def backlog_cursor(self, project: str, keep: int = 0) -> int:
        """Starting cursor for a watcher: leaves only the newest `keep` messages to deliver.

        `keep=0` means "start at the tail", so arming a watch delivers what happens next
        rather than replaying an archive nobody asked for.
        """
        p = _slug(project)
        return self._bootstrap_floor(self._k(p, "feed"), max(0, int(keep)))

    # --------------------------------------------------------------------- ack
    def ack(self, project: str, message_id: str, agent: str) -> Optional[dict]:
        """Record that `agent` has read a message. Returns None if it no longer exists.

        Acks are how "everybody read this" becomes checkable instead of hoped for: the
        poster (and the human) can see who is caught up on a change. The receipt inherits
        the message's remaining TTL, so it dies with the message rather than outliving it.
        """
        p = _slug(project)
        msg_key = self._k(p, "msg", str(message_id))
        ttl = self.r.ttl(msg_key)
        if ttl == -2 or not self.r.exists(msg_key):  # -2: key does not exist
            return None
        agent = (agent or "anonymous").strip() or "anonymous"
        ack_key = self._k(p, "ack", str(message_id))
        self.r.sadd(ack_key, agent)
        if ttl and ttl > 0:
            self.r.expire(ack_key, ttl)
        return {"project": p, "message_id": str(message_id), "agent": agent,
                "acked_by": self.acked_by(p, message_id)}

    def acked_by(self, project: str, message_id: str) -> list:
        """Agent names that have acked a message, sorted for a stable display order."""
        p = _slug(project)
        return sorted(self.r.smembers(self._k(p, "ack", str(message_id))) or [])

    def _with_acks(self, p: str, msg: Optional[dict]) -> Optional[dict]:
        """Attach the read receipts to a full message, but only where one is expected.

        Skipped for fyi/normal so the common read path keeps costing exactly one Redis
        call per message.
        """
        if msg is None or not requires_ack(msg.get("importance")):
            return msg
        msg["acked_by"] = self.acked_by(p, msg["id"])
        return msg

    # -------------------------------------------------------------------- post
    def post(self, project: str, from_agent: str, subject: str, body: str,
             to: Union[None, str, list] = None, thread_id: Optional[str] = None,
             importance: str = "normal", ttl_days: int = DEFAULT_TTL_DAYS) -> dict:
        p = _slug(project)
        msg_id = str(self.r.incr(self._k(p, "seq")))
        thread_id = str(thread_id) if thread_id else msg_id
        created_at = self._now()
        to_list = _normalize_agents(to)

        msg = {
            "id": msg_id,
            "thread_id": thread_id,
            "project": p,
            "from": (from_agent or "unknown").strip() or "unknown",
            "to": json.dumps(to_list),
            "subject": subject or "",
            "body": body or "",
            "importance": normalize_importance(importance),
            "created_at": repr(created_at),
        }
        msg_key = self._k(p, "msg", msg_id)
        ttl = min(max(1, int(ttl_days)), MAX_TTL_DAYS) * 86400
        # Score the feed/thread by the unique monotonic id, NOT the timestamp. Ids never
        # tie, so chronological ordering and the per-reader watermark stay exact even for
        # same-instant bursts (timestamp scores collide and Redis then orders ties by
        # lexicographic member, which scrambles order and silently drops messages).
        score = int(msg_id)

        pipe = self.r.pipeline()
        pipe.hset(msg_key, mapping=msg)
        pipe.expire(msg_key, ttl)
        pipe.zadd(self._k(p, "feed"), {msg_id: score})
        pipe.zadd(self._k(p, "thread", thread_id), {msg_id: score})
        for token in _tokenize(f"{subject} {body}"):
            pipe.sadd(self._k(p, "idx", token), msg_id)
        pipe.zadd(PROJECTS_KEY, {p: created_at})  # activity time, display/sort only
        pipe.execute()

        # Doorbell. Published only after the write commits, so a watcher woken by this
        # ping always finds the message already in the feed. The payload is just the id:
        # pub/sub is lossy while a subscriber is disconnected, so watchers must never
        # treat a ping as the content. They re-read the feed above their own cursor, which
        # makes a dropped ping cost latency (until the next poll tick) and never a message.
        self.r.publish(self._k(p, "events"), msg_id)

        return {"message_id": msg_id, "thread_id": thread_id, "project": p,
                "importance": msg["importance"], "requires_ack": requires_ack(msg["importance"])}

    # ----------------------------------------------------------- check_mailbox
    def check_mailbox(self, project: str, agent: str, limit: int = 20,
                      mark_seen: bool = True, bootstrap: Union[int, str, None] = DEFAULT_BOOTSTRAP,
                      since_hours: Optional[float] = None) -> dict:
        p = _slug(project)
        agent = (agent or "anonymous").strip() or "anonymous"
        feed_key = self._k(p, "feed")
        seen_key = self._k(p, "seen", agent)

        # limit < 1 means "show nothing": return without touching the watermark, so a
        # caller can never consume/lose messages by asking for zero.
        if int(limit) < 1:
            return {"project": p, "agent": agent, "count": 0, "skipped": 0, "messages": []}
        lim = _clamp_limit(limit)

        # The watermark is the last message id this agent has seen. Ids are unique and
        # monotonic, so "newer than" is exact - no timestamp ties, no silent skips.
        raw = self.r.get(seen_key)
        first_visit = raw is None
        try:
            watermark = int(raw) if raw is not None else 0
        except (ValueError, TypeError):
            watermark = 0  # tolerate a legacy/garbage value by re-reading from the start

        # Both options below only ever push the watermark FORWARD, so they can skip old
        # messages but never re-deliver ones already seen.
        stored = watermark
        keep = _bootstrap_keep(bootstrap)
        if first_visit and keep is not None:
            watermark = max(watermark, self._bootstrap_floor(feed_key, keep))
        if since_hours is not None:
            watermark = max(watermark, self._since_floor(p, feed_key, since_hours))
        skipped = (self.r.zcount(feed_key, f"({stored}", watermark)
                   if watermark > stored else 0)

        msgs, last_id, scanned = self._page_after(p, feed_key, watermark, lim)
        messages = [_summarize(m) for m in msgs]

        if mark_seen and (scanned or watermark > stored):
            # advance to the last id scanned this page; a backlog bigger than `lim` is
            # drained over successive checks rather than skipped. Nothing scanned and no
            # floor applied -> the watermark is left untouched (no wall-clock fallback to
            # race against posts). A floor IS persisted even when it returned nothing, so
            # "skipped" stays skipped instead of reappearing on the next check; a floor is
            # always derived from an id already in the feed, and a concurrent post takes a
            # strictly higher id from INCR, so it can never land below the floor.
            self.r.set(seen_key, str(last_id))
            self.r.expire(seen_key, DEFAULT_TTL_DAYS * 86400)

        return {"project": p, "agent": agent, "count": len(messages),
                "skipped": skipped, "messages": messages}

    # -------------------------------------------------------------------- read
    def read(self, project: str, message_id: str) -> Optional[dict]:
        p = _slug(project)
        return self._with_acks(p, self._load(p, str(message_id)))

    # --------------------------------------------------------------- list_feed
    def list_feed(self, project: str, limit: int = 200, offset: int = 0) -> dict:
        """Read a project's feed newest-first WITHOUT advancing any watermark.

        Used by read-only viewers (e.g. the web UI). Unlike check_mailbox, this never
        mutates per-reader state.
        """
        p = _slug(project)
        feed_key = self._k(p, "feed")
        start = max(0, int(offset))
        stop = start + _clamp_limit(limit) - 1
        ids = self.r.zrevrange(feed_key, start, stop)  # newest first
        msgs = []
        for msg_id in ids:
            msg = self._load(p, msg_id, source_zset=feed_key)
            if msg is None:
                continue
            msgs.append(self._with_acks(p, msg))
        return {"project": p, "count": len(msgs), "total": self.r.zcard(feed_key), "messages": msgs}

    # ------------------------------------------------------------------ latest
    def latest(self, project: str, limit: int = 5,
               since_hours: Optional[float] = None) -> dict:
        """The most recent messages in a project, newest first, WITHOUT touching any
        watermark.

        The orientation verb: a fresh context can see what is going on without consuming
        anyone's unread backlog, and asking twice gives the same answer.
        """
        p = _slug(project)
        feed_key = self._k(p, "feed")
        cutoff = (self._now() - max(0.0, float(since_hours)) * 3600.0
                  if since_hours is not None else None)

        ids = self.r.zrevrange(feed_key, 0, _clamp_limit(limit) - 1)  # newest first
        msgs = []
        for msg_id in ids:
            msg = self._load(p, msg_id, source_zset=feed_key)
            if msg is None:
                continue
            if cutoff is not None and msg["created_at"] < cutoff:
                continue
            msgs.append(_summarize(msg))
        return {"project": p, "count": len(msgs), "total": self.r.zcard(feed_key),
                "messages": msgs}

    # ------------------------------------------------------------------ search
    def search(self, project: str, query: str, limit: int = 20) -> dict:
        p = _slug(project)
        tokens = _tokenize(query)
        if not tokens:
            return {"project": p, "count": 0, "messages": []}

        idx_keys = [self._k(p, "idx", t) for t in tokens]
        ids = self.r.sinter(idx_keys)  # ids containing ALL query tokens

        msgs = []
        for msg_id in ids:
            msg = self._load(p, msg_id, source_sets=idx_keys)
            if msg is None:
                continue
            msgs.append(msg)

        msgs.sort(key=lambda m: m["created_at"], reverse=True)  # newest first
        msgs = msgs[: _clamp_limit(limit)]
        return {"project": p, "count": len(msgs), "messages": [_summarize(m) for m in msgs]}

    # -------------------------------------------------------------- get_thread
    def get_thread(self, project: str, thread_id: str, limit: int = 200) -> dict:
        p = _slug(project)
        thread_key = self._k(p, "thread", str(thread_id))
        ids = self.r.zrange(thread_key, 0, -1)  # oldest first

        msgs = []
        for msg_id in ids:
            msg = self._load(p, msg_id, source_zset=thread_key)
            if msg is None:
                continue
            msgs.append(msg)

        msgs = [self._with_acks(p, m) for m in msgs[: _clamp_limit(limit)]]
        return {"project": p, "thread_id": str(thread_id), "count": len(msgs), "messages": msgs}

    # ----------------------------------------------------------- list_projects
    def list_projects(self, limit: int = 50) -> dict:
        items = self.r.zrevrange(PROJECTS_KEY, 0, _clamp_limit(limit) - 1, withscores=True)
        projects = []
        for slug, score in items:
            feed_size = self.r.zcard(self._k(slug, "feed"))
            if feed_size == 0:
                # no live feed entries left (everything expired); drop from the registry
                self.r.zrem(PROJECTS_KEY, slug)
                continue
            projects.append({
                "slug": slug,
                "last_activity": score,
                "last_activity_iso": _iso(score),
                "message_count": feed_size,
            })
        return {"count": len(projects), "projects": projects}

    # ------------------------------------------------------------------- admin
    def flush_project(self, project: str) -> int:
        """Delete every key for a project (used by tests/cleanup)."""
        p = _slug(project)
        keys = list(self.r.scan_iter(match=self._k(p, "*")))
        deleted = self.r.delete(*keys) if keys else 0
        self.r.zrem(PROJECTS_KEY, p)
        return deleted
