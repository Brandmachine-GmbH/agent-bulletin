"""RedisStore - all Redis access for agent-bulletin.

The model is a per-project news feed (see architecture.md):

  - messages live in one ordered stream per project (a sorted set),
  - readers track "what's new" with a per-(project, agent) watermark,
  - search uses a tiny token inverted index built from plain Redis sets,
  - messages auto-expire after a TTL; feed/thread/index keys carry the TTL of their
    longest-lived message, and dangling entries inside them are cleaned lazily on read.

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
#   quiet     no notification at all; surfaces only on the next check_mailbox
#   normal    one-line notification (subject + snippet)
#   attention notification with the FULL body inline, and an ack is expected
#   halt      same as attention, plus "stop the current line of work and wait"
#
# Every name describes what the READER does about it. That is why the quiet level is not
# called "fyi": "FYI" describes the sender's attitude, and an agent reaching for it means
# "telling you, no action needed" while the level means "nobody is told at all". Naming it
# by tone made the two easy to confuse, and the failure was silent.
#
# Inlining the body is what makes "you must read this" real: once the text is in the
# reader's transcript it has been read, with no compliance mechanism to trust. `halt`
# still cannot preempt a tool call that is already running; it lands at the next
# boundary. See architecture.md.
IMPORTANCE_LEVELS = ("quiet", "normal", "attention", "halt")
DEFAULT_IMPORTANCE = "normal"
# The level from which a read receipt is expected. Keeping this at "attention" (rather
# than halt-only) is deliberate: acks stay rare enough that a MISSING one is noticeable.
ACK_FROM_IMPORTANCE = "attention"
# How long a watcher's presence key survives without a refresh. Comfortably longer than a
# default poll tick, so a slow tick does not make a live watcher flicker out of existence.
WATCHER_TTL = 60

# Words outside the set land on the nearest level instead of being rejected: a post
# refused over a label is a lost announcement. The soft words map UP to `normal` rather
# than down to `quiet` on purpose. Someone writing "fyi" or "low" is saying "this is not
# urgent", not "deliver this to nobody", and of the two ways to be wrong, being seen when
# you meant to be quiet is the recoverable one.
_LEGACY_IMPORTANCE = {
    "fyi": "normal",
    "info": "normal",
    "low": "normal",
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


def project_slug(project: Optional[str]) -> str:
    """The feed slug a project name maps to. Public so callers can de-duplicate names that
    collapse to the same feed before giving each its own cursor."""
    return _slug(project)


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


def _extend_ttl(pipe, key: str, ttl: int) -> None:
    """Make `key` live at least `ttl` more seconds, never shortening it.

    NX then GT, because neither alone does it: GT treats a key with no expiry as infinite
    and so never sets one on a fresh key, while NX alone would let a 1-day message cut the
    lifetime of a feed that also holds 30-day ones. The key then lives exactly as long as
    its longest-lived member, which is what `ttl_days` being per message requires.
    """
    pipe.expire(key, ttl, nx=True)
    pipe.expire(key, ttl, gt=True)


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
        return self._bootstrap_floor(self._k(p, "feed"), max(0, int(keep or 0)))

    def watermark(self, project: str, agent: str) -> int:
        """This reader's last-seen id, 0 if it has never checked here. Read-only.

        Exposed so a watcher can start where the reader actually left off instead of at the
        feed tail. Reading the watermark is safe; only `check_mailbox` may advance it.
        """
        p = _slug(project)
        agent = (agent or "anonymous").strip() or "anonymous"
        raw = self.r.get(self._k(p, "seen", agent))
        try:
            return int(raw) if raw is not None else 0
        except (ValueError, TypeError):
            return 0

    def id_counter(self, project: str) -> int:
        """The highest id ever issued in this project, 0 if none. Read-only.

        This, not the newest id still in the feed, is how a reader detects that the id
        sequence was reset. The feed's newest id can drop below a reader's cursor without
        any reset: `ttl_days` is per message, so the newest post can expire first and be
        removed lazily, and a check against the feed then mistook that for a reset and
        replayed the whole feed. The counter only goes down when the project is flushed.
        """
        raw = self.r.get(self._k(_slug(project), "seq"))
        try:
            return int(raw) if raw is not None else 0
        except (ValueError, TypeError):
            return 0

    # ---------------------------------------------------------------- presence
    def announce_watcher(self, project: str, agent: str, *,
                         min_importance: str = DEFAULT_IMPORTANCE,
                         to_me: bool = False, mode: str = "watch",
                         ttl: int = WATCHER_TTL) -> None:
        """Refresh this watcher's presence. Called on arming and on every poll tick.

        Presence exists to answer one question a poster otherwise cannot: is anyone
        actually listening? Without it, "nobody armed a watch", "the only watcher filters
        above your level", and "everyone was told" are indistinguishable at the moment of
        posting, so an agent reports that it announced something when nothing was
        delivered. That is the exact state this whole feature was built to escape.

        Deliberately NOT an agent registry (which architecture.md rules out): the key
        expires, so it records who is listening right now and nothing else. There is no
        roster, no history, and no identity beyond the name the watcher chose.
        """
        agent = (agent or "anonymous").strip() or "anonymous"
        payload = json.dumps({
            "agent": agent,
            "min_importance": normalize_importance(min_importance),
            "to_me": bool(to_me),
            # "watch" is told immediately; "follow" sees the post at its next tool batch or
            # prompt. A poster needs the difference to say truthfully when it will land.
            "mode": mode,
        })
        # A plain key with a TTL rather than a field in a hash: Redis expires it for us, so
        # a watcher that dies leaves nothing to sweep up and no stale presence to filter.
        self.r.set(self._presence_key(_slug(project), agent, mode), payload,
                   ex=max(1, int(ttl)))

    def _presence_key(self, p: str, agent: str, mode: str) -> str:
        # Followers get their own key prefix. Sharing one key per agent let a live watch's
        # 60-second heartbeat overwrite a follower's entry (and its filter level) under the
        # same name, so posters were told the wrong thing about who would see a post.
        return self._k(p, "follower" if mode == "follow" else "watcher", agent)

    def withdraw_watcher(self, project: str, agent: str, mode: str = "watch") -> None:
        """Drop presence at once, so posters stop hearing about a reader that left."""
        agent = (agent or "anonymous").strip() or "anonymous"
        self.r.delete(self._presence_key(_slug(project), agent, mode))

    def watchers(self, project: str) -> list:
        """Watchers currently live on a project, by name.

        A heartbeat proves a process is running; it does not prove the agent will act on
        what it receives. Read it as "who could be told", not as a guarantee.
        """
        p = _slug(project)
        found = []
        keys = [*self.r.scan_iter(match=self._k(p, "watcher", "*")),
                *self.r.scan_iter(match=self._k(p, "follower", "*"))]
        for key in keys:
            raw = self.r.get(key)
            if not raw:
                continue  # expired between the scan and the read
            try:
                found.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
        return sorted(found, key=lambda w: w.get("agent", ""))

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

        Skipped for fyi/normal so the ordinary read path keeps costing exactly one Redis
        call per message. A board made entirely of attention/halt messages does pay a
        second round trip each; that is the intended trade (loud messages are rare, and
        the ack state is the point of looking at them) rather than an oversight.
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

        feed_key, thread_key = self._k(p, "feed"), self._k(p, "thread", thread_id)
        idx_keys = [self._k(p, "idx", t) for t in _tokenize(f"{subject} {body}")]

        pipe = self.r.pipeline()
        pipe.hset(msg_key, mapping=msg)
        pipe.expire(msg_key, ttl)
        pipe.zadd(feed_key, {msg_id: score})
        pipe.zadd(thread_key, {msg_id: score})
        for idx_key in idx_keys:
            pipe.sadd(idx_key, msg_id)
        # Every key that points at messages dies with the last message it points at. Lazy
        # cleanup alone never removed a key nobody reads again: a quiet project's search
        # index outlived its messages for good (63,904 orphaned idx keys by Oct 2026), and
        # the front page kept listing projects whose every message was gone.
        for key in (feed_key, thread_key, *idx_keys):
            _extend_ttl(pipe, key, ttl)
        pipe.zadd(PROJECTS_KEY, {p: created_at})  # activity time, display/sort only
        pipe.execute()

        # Doorbell. Published only after the write commits, so a watcher woken by this
        # ping always finds the message already in the feed. The payload is just the id:
        # pub/sub is lossy while a subscriber is disconnected, so watchers must never
        # treat a ping as the content. They re-read the feed above their own cursor, which
        # makes a dropped ping cost latency (until the next poll tick) and never a message.
        #
        # Swallowed on failure by the same logic: the message is already committed and
        # readable, so raising here would report a successful post as failed, and the
        # caller's retry would put a duplicate on the board. A lost ping costs one poll
        # interval, which is precisely what the fallback poll is for.
        try:
            self.r.publish(self._k(p, "events"), msg_id)
        except redis.RedisError:
            pass

        result = {"message_id": msg_id, "thread_id": thread_id, "project": p,
                  "importance": msg["importance"],
                  "requires_ack": requires_ack(msg["importance"])}
        # Say so when the requested level was not the one used. Silently coercing teaches
        # nothing: the poster asked for a delivery behaviour and got a different one, and
        # this is the moment it can still be corrected.
        requested = (importance or "").strip().lower()
        if requested and requested != msg["importance"]:
            result["importance_note"] = (
                f"'{requested}' is not a delivery level, so this was posted as "
                f"'{msg['importance']}'. The levels are: "
                f"{', '.join(IMPORTANCE_LEVELS)} (quiet delivers to nobody until they "
                f"check; normal notifies in one line; attention and halt inline the whole "
                f"body and expect an ack)."
            )
        return result

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
        # A message this agent already acked was almost certainly delivered to it as a live
        # notification. The watermark deliberately did not move for that, so it shows up
        # here as unread; flagging it lets a reader say "already seen" instead of reporting
        # the same announcement to the human twice. Costs one SISMEMBER per loud message.
        for summary in messages:
            if summary["requires_ack"]:
                summary["acked_by_me"] = bool(
                    self.r.sismember(self._k(p, "ack", summary["id"]), agent))

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
    def _drop_dangling(self, p: str, key: str, ids: list, kind: str = "zset") -> list:
        """Remove ids whose message has expired from one collection; return the live ones."""
        if not ids:
            return []
        pipe = self.r.pipeline()
        for msg_id in ids:
            pipe.exists(self._k(p, "msg", msg_id))
        alive = pipe.execute()
        dead = [i for i, ok in zip(ids, alive) if not ok]
        if dead:
            (self.r.zrem if kind == "zset" else self.r.srem)(key, *dead)
        return [i for i, ok in zip(ids, alive) if ok]

    def list_projects(self, limit: int = 50) -> dict:
        items = self.r.zrevrange(PROJECTS_KEY, 0, _clamp_limit(limit) - 1, withscores=True)
        projects = []
        for slug, score in items:
            # Counted from live messages, not ZCARD: expired ids stay in the feed until
            # something loads them, so ZCARD kept showing "2 message(s)" on a project whose
            # messages had all been gone for months. Feeds stay small, so checking each id
            # here is cheap and leaves the feed clean for the next caller too.
            feed_key = self._k(slug, "feed")
            feed_size = len(self._drop_dangling(slug, feed_key, self.r.zrange(feed_key, 0, -1)))
            if feed_size == 0:
                self.r.zrem(PROJECTS_KEY, slug)
                continue
            projects.append({
                "slug": slug,
                "last_activity": score,
                "last_activity_iso": _iso(score),
                "message_count": feed_size,
            })
        return {"count": len(projects), "projects": projects}

    # ------------------------------------------------------------------- prune
    def prune(self, dry_run: bool = False) -> dict:
        """Sweep every project once: drop expired ids, delete emptied keys, and give the
        survivors the expiry `post` now sets.

        Needed once for data written before derived keys carried a TTL; after that, expiry
        keeps the store clean on its own and this only tidies dangling ids early. Safe to
        run any time: it only removes ids whose message hash no longer exists, and only
        deletes keys that are left empty by that.
        """
        counts = {"projects": 0, "keys_deleted": 0, "ids_removed": 0, "keys_ttl_set": 0,
                  "registry_dropped": 0}
        ttl_cache: dict = {}

        def msg_ttl(p, msg_id):
            if (p, msg_id) not in ttl_cache:
                ttl_cache[(p, msg_id)] = self.r.ttl(self._k(p, "msg", msg_id))
            return ttl_cache[(p, msg_id)]

        slugs = set(self.r.zrange(PROJECTS_KEY, 0, -1))
        for pattern in ("am:*:feed", "am:*:thread:*", "am:*:idx:*"):
            for key in self.r.scan_iter(match=pattern, count=1000):
                slugs.add(key.split(":")[1])

        for p in sorted(slugs):
            counts["projects"] += 1
            keys = [self._k(p, "feed"),
                    *self.r.scan_iter(match=self._k(p, "thread", "*"), count=1000),
                    *self.r.scan_iter(match=self._k(p, "idx", "*"), count=1000)]
            for key in keys:
                is_set = ":idx:" in key
                ids = list(self.r.smembers(key)) if is_set else self.r.zrange(key, 0, -1)
                ttls = {i: msg_ttl(p, i) for i in ids}
                live = [i for i, t in ttls.items() if t != -2]  # -2: the hash is gone
                counts["ids_removed"] += len(ids) - len(live)
                if dry_run:
                    if not live:
                        counts["keys_deleted"] += 1
                    elif self.r.ttl(key) == -1:
                        counts["keys_ttl_set"] += 1
                    continue
                if not live:
                    counts["keys_deleted"] += self.r.delete(key)
                    continue
                dead = [i for i in ids if i not in set(live)]
                if dead:
                    (self.r.srem if is_set else self.r.zrem)(key, *dead)
                # A message with no expiry (-1) would make the key's lifetime unbounded;
                # fall back to the default rather than leaving the key persistent.
                longest = max((t if t > 0 else DEFAULT_TTL_DAYS * 86400) for t in
                              (ttls[i] for i in live))
                if self.r.ttl(key) == -1:
                    counts["keys_ttl_set"] += 1
                pipe = self.r.pipeline()
                _extend_ttl(pipe, key, longest)
                pipe.execute()
            if not self.r.exists(self._k(p, "feed")):
                if dry_run:
                    counts["registry_dropped"] += int(self.r.zscore(PROJECTS_KEY, p) is not None)
                else:
                    counts["registry_dropped"] += self.r.zrem(PROJECTS_KEY, p)
        return counts

    # ------------------------------------------------------------------- admin
    def flush_project(self, project: str) -> int:
        """Delete every key for a project (used by tests/cleanup)."""
        p = _slug(project)
        keys = list(self.r.scan_iter(match=self._k(p, "*")))
        deleted = self.r.delete(*keys) if keys else 0
        self.r.zrem(PROJECTS_KEY, p)
        return deleted
