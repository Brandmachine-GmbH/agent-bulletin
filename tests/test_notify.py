"""Tests for the notification layer: importance levels, acks, and the watch primitives.

The behaviours worth protecting here are the ones that would fail quietly in production:
a watcher that consumes the mailbox it is announcing, a loud message that arrives without
its body, or a filter that lets an agent notify itself in a loop.
"""

import pytest
import redis

from agent_bulletin.store import (
    importance_at_least,
    normalize_importance,
    requires_ack,
)
from agent_bulletin.watch import (
    delivery_preview,
    delivery_summary,
    format_message,
    should_notify,
)


# ------------------------------------------------------------------ vocabulary
@pytest.mark.parametrize("given,expected", [
    ("quiet", "quiet"),
    ("NORMAL", "normal"),
    ("  attention ", "attention"),
    ("halt", "halt"),
    ("high", "attention"),      # legacy word from before the closed set
    ("urgent", "halt"),
    ("wat", "normal"),          # unknown: never reject a post over a label
    ("", "normal"),
    (None, "normal"),
])
def test_normalize_importance(given, expected):
    assert normalize_importance(given) == expected


@pytest.mark.parametrize("soft", ["fyi", "low", "info"])
def test_soft_words_map_up_to_normal_not_down_to_silence(soft):
    """An agent writing "fyi" means "no action needed", not "deliver to nobody". Of the two
    ways to be wrong, being seen when you meant to be quiet is the recoverable one."""
    assert normalize_importance(soft) == "normal"


def test_a_coerced_level_is_reported_back(store):
    result = store.post("p", "a", "s", "b", importance="fyi")
    assert result["importance"] == "normal"
    assert "'fyi' is not a delivery level" in result["importance_note"]
    assert "quiet delivers to nobody" in result["importance_note"]


def test_an_exact_level_gets_no_note(store):
    assert "importance_note" not in store.post("p", "a", "s", "b", importance="quiet")


def test_importance_is_ordered():
    assert importance_at_least("halt", "attention")
    assert importance_at_least("normal", "normal")
    assert not importance_at_least("quiet", "normal")


def test_ack_expected_from_attention_up():
    assert not requires_ack("quiet")
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
def test_publish_happens_after_the_write_is_visible(store):
    """The ping must never arrive before the message is readable, or a watcher woken by it
    would query the feed and find nothing.

    Asserting inside the publish call is the only way to observe the ordering: a test that
    checks both after post() returns passes just as happily with the publish moved above
    pipe.execute(), which is exactly the bug it is supposed to catch.
    """
    observed = {}
    real_publish = store.r.publish

    def spy(channel, data):
        observed["readable"] = bool(store.r.hgetall(store._k("p", "msg", data)))
        observed["channel"] = channel
        return real_publish(channel, data)

    store.r.publish = spy
    store.post("p", "a", "s", "b")
    assert observed["readable"] is True
    assert observed["channel"] == store.events_channel("p")


def test_a_failed_doorbell_does_not_fail_the_post(store):
    """The message is already committed, so raising here would report a good post as
    failed and the caller's retry would put a duplicate on the board."""
    def boom(*_a, **_kw):
        raise redis.ConnectionError("pubsub is down")

    store.r.publish = boom
    result = store.post("p", "a", "s", "b")
    assert result["message_id"] == "1"
    assert store.read("p", "1") is not None


def test_acked_messages_are_flagged_in_the_mailbox(store):
    """The watermark deliberately did not move when this arrived as a notification, so the
    reader needs to know it already saw it rather than reporting it to the human twice."""
    store.post("p", "a", "loud", "b", importance="attention")
    store.post("p", "a", "quiet", "b")
    store.ack("p", "1", "me")
    msgs = store.check_mailbox("p", "me", bootstrap="all")["messages"]
    assert msgs[0]["acked_by_me"] is True
    assert "acked_by_me" not in msgs[1]  # only tracked where an ack is expected


def test_watermark_is_readable_without_advancing(store):
    store.post("p", "a", "s", "b")
    assert store.watermark("p", "me") == 0
    assert store.check_mailbox("p", "me", bootstrap="all")["count"] == 1
    assert store.watermark("p", "me") == 1
    assert store.watermark("p", "me") == 1  # reading it is not an advance


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
    assert not should_notify(_msg(importance="quiet"), agent="me")
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
    assert "| line one" in out and "| line two" in out
    assert "[bulletin backend #1 ATTENTION]" in out
    assert 'ack_message(project="backend", message_id="1", agent="me")' in out
    assert "untrusted message body" in out


def test_the_action_rule_does_not_hedge():
    """"Do not start changes on your own" reads as "unless the message says otherwise",
    which is the loophole a forged or over-reaching post drives through."""
    for level in ("attention", "halt"):
        out = format_message(_msg(importance=level), project="p", agent="me")
        assert "whatever it says" in out
        assert "on your own" not in out


def test_halt_says_to_stop_and_persist_the_constraint():
    out = format_message(_msg(importance="halt", body="schema changed"),
                         project="backend", agent="me")
    assert "[bulletin backend #1 HALT]" in out
    assert "stop the current line of work" in out
    assert "survives context compaction" in out  # a halt must outlive the notification
    assert "wait for them" in out


def test_attention_and_halt_are_distinguishable():
    """If both levels produce the same instruction, there is no reason for two levels."""
    att = format_message(_msg(importance="attention"), project="p", agent="me")
    halt = format_message(_msg(importance="halt"), project="p", agent="me")
    assert "finish the step you are on" in att and "stop the current line" not in att
    assert "stop the current line of work" in halt


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


# ------------------------------------------------------------------- presence
def test_a_watcher_announces_itself_and_expires(store):
    """Presence is a heartbeat, not a registry: it says who is listening right now."""
    store.announce_watcher("p", "claude-frontend", min_importance="attention")
    live = store.watchers("p")
    assert live == [{"agent": "claude-frontend", "min_importance": "attention",
                     "to_me": False, "mode": "watch"}]
    assert 0 < store.r.ttl(store._k("p", "watcher", "claude-frontend")) <= 60
    store.r.delete(store._k("p", "watcher", "claude-frontend"))   # simulate expiry
    assert store.watchers("p") == []


def test_watchers_are_per_project(store):
    store.announce_watcher("backend", "a")
    assert store.watchers("frontend") == []


def test_delivery_preview_answers_will_this_reach_anyone(store):
    store.announce_watcher("p", "picky", min_importance="halt")
    store.announce_watcher("p", "listening", min_importance="normal")
    msg = {"importance": "normal", "from": "poster", "to": []}
    preview = delivery_preview(msg, store.watchers("p"))
    assert {w["agent"]: w["will_notify"] for w in preview} == {
        "listening": True, "picky": False}
    assert "will notify listening" in delivery_summary(preview)
    assert "1 filtered it out" in delivery_summary(preview)


def test_delivery_summary_says_when_nobody_is_listening():
    """The failure this exists to expose: the write succeeded and nobody was told."""
    assert "nobody is watching" in delivery_summary([])


def test_delivery_summary_says_when_everyone_filtered_it_out(store):
    store.announce_watcher("p", "picky", min_importance="attention")
    preview = delivery_preview({"importance": "normal", "from": "x", "to": []},
                               store.watchers("p"))
    assert "none will be notified" in delivery_summary(preview)


def test_a_poster_is_not_counted_as_reachable_by_their_own_post(store):
    store.announce_watcher("p", "me")
    preview = delivery_preview({"importance": "halt", "from": "me", "to": []},
                               store.watchers("p"))
    assert preview[0]["will_notify"] is False


def test_to_me_watchers_are_previewed_correctly(store):
    store.announce_watcher("p", "targeted", to_me=True)
    reachable = delivery_preview({"importance": "normal", "from": "x", "to": ["targeted"]},
                                 store.watchers("p"))
    missed = delivery_preview({"importance": "normal", "from": "x", "to": ["someone-else"]},
                              store.watchers("p"))
    assert reachable[0]["will_notify"] is True
    assert missed[0]["will_notify"] is False
