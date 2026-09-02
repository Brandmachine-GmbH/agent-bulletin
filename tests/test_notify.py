"""Tests for the notification layer: importance levels, acks, and the watch primitives.

The behaviours worth protecting here are the ones that would fail quietly in production:
a watcher that consumes the mailbox it is announcing, a loud message that arrives without
its body, or a filter that lets an agent notify itself in a loop.
"""

import pytest

from agent_bulletin.store import (
    importance_at_least,
    normalize_importance,
    requires_ack,
)
from agent_bulletin.watch import format_message, should_notify


# ------------------------------------------------------------------ vocabulary
@pytest.mark.parametrize("given,expected", [
    ("fyi", "fyi"),
    ("NORMAL", "normal"),
    ("  attention ", "attention"),
    ("halt", "halt"),
    ("high", "attention"),      # legacy word from before the closed set
    ("low", "fyi"),
    ("urgent", "halt"),
    ("wat", "normal"),          # unknown: never reject a post over a label
    ("", "normal"),
    (None, "normal"),
])
def test_normalize_importance(given, expected):
    assert normalize_importance(given) == expected


def test_importance_is_ordered():
    assert importance_at_least("halt", "attention")
    assert importance_at_least("normal", "normal")
    assert not importance_at_least("fyi", "normal")


def test_ack_expected_from_attention_up():
    assert not requires_ack("fyi")
    assert not requires_ack("normal")
    assert requires_ack("attention")
    assert requires_ack("halt")


def test_legacy_importance_normalized_on_read(store):
    """A message written before the vocabulary existed still ranks correctly."""
    store.post("p", "a", "s", "b")
    store.r.hset(store._k("p", "msg", "1"), "importance", "high")
    assert store.read("p", "1")["importance"] == "attention"


def test_summary_carries_requires_ack(store):
    store.post("p", "a", "s", "b", importance="halt")
    msg = store.check_mailbox("p", "r")["messages"][0]
    assert msg["importance"] == "halt"
    assert msg["requires_ack"] is True


# ------------------------------------------------------------------- doorbell
def test_post_publishes_after_the_write_commits(store):
    """The ping must never arrive before the message is readable, or a watcher woken by
    it would query the feed and find nothing."""
    pubsub = store.r.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(store.events_channel("p"))
    store.post("p", "a", "s", "b")
    # The first get_message can return the swallowed subscribe confirmation, so drain a
    # few. The watch loop is immune to this by construction: whatever get_message returns,
    # it re-reads the feed above its own cursor.
    pings = [pubsub.get_message(timeout=0.2) for _ in range(5)]
    data = [p["data"] for p in pings if p and p.get("type") == "message"]
    assert data == ["1"]
    assert store.read("p", "1") is not None


# ---------------------------------------------------------------------- since
def test_since_does_not_touch_the_watermark(store):
    """The whole design rests on this: being notified must not consume the mailbox."""
    store.post("p", "a", "one", "b1")
    assert store.since("p", 0)["count"] == 1
    assert store.since("p", 0)["count"] == 1          # repeatable
    assert store.check_mailbox("p", "reader")["count"] == 1  # still unread


def test_since_returns_full_bodies(store):
    store.post("p", "a", "s", "the whole body")
    assert store.since("p", 0)["messages"][0]["body"] == "the whole body"


def test_since_cursor_advances_past_expired_ids(store):
    """An expired message must not wedge a watcher's cursor."""
    store.post("p", "a", "one", "b1")
    store.post("p", "a", "two", "b2")
    store.r.delete(store._k("p", "msg", "1"))  # simulate TTL expiry of the older message
    page = store.since("p", 0)
    assert [m["subject"] for m in page["messages"]] == ["two"]
    assert page["cursor"] == 2


def test_backlog_cursor_starts_at_the_tail(store):
    for i in range(5):
        store.post("p", "a", f"s{i}", "b")
    assert store.backlog_cursor("p", 0) == 5           # deliver only what happens next
    assert store.since("p", store.backlog_cursor("p", 0))["count"] == 0
    assert store.since("p", store.backlog_cursor("p", 2))["count"] == 2


def test_backlog_cursor_on_empty_project(store):
    assert store.backlog_cursor("nothing-here", 0) == 0


# ------------------------------------------------------------------------ ack
def test_ack_records_the_reader(store):
    store.post("p", "a", "s", "b", importance="attention")
    result = store.ack("p", "1", "reader-1")
    assert result["acked_by"] == ["reader-1"]
    store.ack("p", "1", "reader-2")
    assert store.acked_by("p", "1") == ["reader-1", "reader-2"]


def test_ack_is_idempotent(store):
    store.post("p", "a", "s", "b", importance="halt")
    store.ack("p", "1", "reader")
    store.ack("p", "1", "reader")
    assert store.acked_by("p", "1") == ["reader"]


def test_ack_on_missing_message_returns_none(store):
    assert store.ack("p", "999", "reader") is None


def test_ack_does_not_mark_anything_seen(store):
    store.post("p", "a", "s", "b", importance="attention")
    store.ack("p", "1", "reader")
    assert store.check_mailbox("p", "reader")["count"] == 1  # acking is not reading


def test_ack_inherits_the_message_ttl(store):
    store.post("p", "a", "s", "b", importance="attention", ttl_days=1)
    store.ack("p", "1", "reader")
    ttl = store.r.ttl(store._k("p", "ack", "1"))
    assert 0 < ttl <= 86400  # dies with the message rather than outliving it


def test_read_exposes_acks_only_where_expected(store):
    store.post("p", "a", "loud", "b", importance="attention")
    store.post("p", "a", "quiet", "b")
    store.ack("p", "1", "reader")
    assert store.read("p", "1")["acked_by"] == ["reader"]
    assert "acked_by" not in store.read("p", "2")


def test_thread_exposes_acks(store):
    store.post("p", "a", "loud", "b", importance="halt")
    store.ack("p", "1", "reader")
    thread = store.get_thread("p", "1")
    assert thread["messages"][0]["acked_by"] == ["reader"]


# --------------------------------------------------------------------- filters
def _msg(**kw):
    base = {"id": "1", "from": "other", "to": [], "subject": "s", "body": "b",
            "snippet": "b", "importance": "normal"}
    base.update(kw)
    return base


def test_min_importance_suppresses_quieter_messages():
    assert not should_notify(_msg(importance="fyi"), agent="me")
    assert should_notify(_msg(importance="normal"), agent="me")
    assert not should_notify(_msg(importance="normal"), agent="me",
                             min_importance="attention")
    assert should_notify(_msg(importance="halt"), agent="me", min_importance="attention")


def test_own_posts_do_not_notify_by_default():
    mine = _msg(**{"from": "me"})
    assert not should_notify(mine, agent="me")  # otherwise post_message pings you back
    assert should_notify(mine, agent="me", include_self=True)


def test_to_me_passes_broadcasts_and_direct_mentions():
    assert should_notify(_msg(to=[]), agent="me", to_me_only=True)
    assert should_notify(_msg(to=["me", "you"]), agent="me", to_me_only=True)
    assert not should_notify(_msg(to=["you"]), agent="me", to_me_only=True)


# ------------------------------------------------------------------ formatting
def test_quiet_levels_are_one_line():
    out = format_message(_msg(subject="Renamed /orders", snippet="details here"),
                         project="backend", agent="me")
    assert "\n" not in out
    assert "[bulletin backend #1 normal]" in out
    assert "Renamed /orders" in out


def test_loud_levels_inline_the_whole_body():
    """Inlining is the mechanism: once it is in the transcript it has been read."""
    body = "line one\nline two"
    out = format_message(_msg(importance="attention", body=body), project="backend",
                         agent="me")
    assert body in out
    assert "[bulletin backend #1 ATTENTION]" in out
    assert 'ack_message(project="backend", message_id="1", agent="me")' in out
    assert "not a work order" in out


def test_halt_says_to_stop_and_wait():
    out = format_message(_msg(importance="halt", body="schema changed"),
                         project="backend", agent="me")
    assert "[bulletin backend #1 HALT]" in out
    assert "stop the current line of work" in out
    assert "wait for them before continuing" in out


def test_long_bodies_are_truncated_with_a_pointer():
    out = format_message(_msg(importance="attention", body="x" * 9000),
                         project="backend", agent="me")
    assert "[body truncated, read_message for the rest]" in out
    assert len(out) < 5000


def test_oneline_forces_the_compact_form():
    out = format_message(_msg(importance="halt", body="long body"), project="backend",
                         agent="me", oneline=True)
    assert "\n" not in out
    assert "[bulletin backend #1 halt]" in out


def test_a_long_normal_post_stays_one_line():
    """Full records reach the watcher without a snippet; an untrimmed body would put a
    whole post on a single notification line."""
    out = format_message(_msg(body="y" * 900, snippet=None), project="backend", agent="me")
    assert "\n" not in out
    assert out.endswith("...")
    assert len(out) < 300
