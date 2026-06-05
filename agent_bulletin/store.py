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
from datetime import datetime, timezone
from typing import Optional, Union

import redis

DEFAULT_TTL_DAYS = 30
PROJECTS_KEY = "am:projects"

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")
_SNIPPET_LEN = 160


def _slug(project: Optional[str]) -> str:
    """Sanitize a free-form project name into a key-safe slug."""
    s = (project or "default").strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or "default"


def _tokenize(text: str) -> set:
    """Lowercase, split on non-alphanumerics, drop tokens shorter than 2 chars."""
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 2}


def _normalize_agents(value: Union[None, str, list]) -> list:
    """Accept None, a single name, or a list of names; return a clean list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


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
        "importance": data.get("importance", "normal"),
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

    @staticmethod
    def _now() -> float:
        return time.time()

    def ping(self) -> bool:
        return self.r.ping()

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
            "importance": (importance or "normal").strip() or "normal",
            "created_at": repr(created_at),
        }
        msg_key = self._k(p, "msg", msg_id)
        ttl = int(max(1, ttl_days) * 86400)

        pipe = self.r.pipeline()
        pipe.hset(msg_key, mapping=msg)
        pipe.expire(msg_key, ttl)
        pipe.zadd(self._k(p, "feed"), {msg_id: created_at})
        pipe.zadd(self._k(p, "thread", thread_id), {msg_id: created_at})
        for token in _tokenize(f"{subject} {body}"):
            pipe.sadd(self._k(p, "idx", token), msg_id)
        pipe.zadd(PROJECTS_KEY, {p: created_at})
        pipe.execute()

        return {"message_id": msg_id, "thread_id": thread_id, "project": p}

    # ----------------------------------------------------------- check_mailbox
    def check_mailbox(self, project: str, agent: str, limit: int = 20,
                      mark_seen: bool = True) -> dict:
        p = _slug(project)
        agent = (agent or "anonymous").strip() or "anonymous"
        feed_key = self._k(p, "feed")
        seen_key = self._k(p, "seen", agent)

        raw = self.r.get(seen_key)
        try:
            watermark = float(raw) if raw is not None else 0.0
        except (ValueError, TypeError):
            watermark = 0.0

        # ids strictly newer than the watermark, oldest first, capped at limit
        ids = self.r.zrangebyscore(
            feed_key, f"({watermark}", "+inf", start=0, num=max(1, int(limit))
        )

        messages = []
        last_ts = watermark
        for msg_id in ids:
            msg = self._load(p, msg_id, source_zset=feed_key)
            if msg is None:
                continue
            messages.append(_summarize(msg))
            if msg["created_at"] > last_ts:
                last_ts = msg["created_at"]

        if mark_seen:
            # advance only to the last message actually returned, so a backlog
            # bigger than `limit` is drained over successive checks rather than skipped
            new_watermark = last_ts if messages else self._now()
            self.r.set(seen_key, repr(new_watermark))
            self.r.expire(seen_key, DEFAULT_TTL_DAYS * 86400)

        return {"project": p, "agent": agent, "count": len(messages), "messages": messages}

    # -------------------------------------------------------------------- read
    def read(self, project: str, message_id: str) -> Optional[dict]:
        p = _slug(project)
        return self._load(p, str(message_id))

    # --------------------------------------------------------------- list_feed
    def list_feed(self, project: str, limit: int = 200, offset: int = 0) -> dict:
        """Read a project's feed newest-first WITHOUT advancing any watermark.

        Used by read-only viewers (e.g. the web UI). Unlike check_mailbox, this never
        mutates per-reader state.
        """
        p = _slug(project)
        feed_key = self._k(p, "feed")
        start = max(0, int(offset))
        stop = start + max(1, int(limit)) - 1
        ids = self.r.zrevrange(feed_key, start, stop)  # newest first
        msgs = []
        for msg_id in ids:
            msg = self._load(p, msg_id, source_zset=feed_key)
            if msg is None:
                continue
            msgs.append(msg)
        return {"project": p, "count": len(msgs), "total": self.r.zcard(feed_key), "messages": msgs}

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
        msgs = msgs[: max(1, int(limit))]
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

        msgs = msgs[: max(1, int(limit))]
        return {"project": p, "thread_id": str(thread_id), "count": len(msgs), "messages": msgs}

    # ----------------------------------------------------------- list_projects
    def list_projects(self, limit: int = 50) -> dict:
        items = self.r.zrevrange(PROJECTS_KEY, 0, max(1, int(limit)) - 1, withscores=True)
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
