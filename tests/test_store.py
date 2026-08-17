"""Unit tests for RedisStore, backed by fakeredis (no real Redis required)."""

import time

from agent_bulletin.store import _slug, _tokenize


def test_post_returns_ids_and_starts_thread(store):
    r = store.post("proj", "alice", "Hello", "first body")
    assert r["message_id"] == "1"
    assert r["thread_id"] == "1"  # a new message starts its own thread
    assert r["project"] == "proj"


def test_check_mailbox_first_sees_all_oldest_first(store):
    store.post("p", "a", "one", "b1")
    store.post("p", "a", "two", "b2")
    store.post("p", "a", "three", "b3")
    res = store.check_mailbox("p", "reader")
    assert res["count"] == 3
    assert [m["subject"] for m in res["messages"]] == ["one", "two", "three"]


def test_watermark_advances(store):
    store.post("p", "a", "one", "b1")
    assert store.check_mailbox("p", "r")["count"] == 1
    assert store.check_mailbox("p", "r")["count"] == 0  # nothing new
    store.post("p", "a", "two", "b2")
    assert store.check_mailbox("p", "r")["count"] == 1


def test_readers_have_independent_watermarks(store):
    store.post("p", "a", "one", "b1")
    assert store.check_mailbox("p", "reader-1")["count"] == 1
    assert store.check_mailbox("p", "reader-2")["count"] == 1  # independent of reader-1


def test_peek_does_not_advance(store):
    store.post("p", "a", "one", "b1")
    assert store.check_mailbox("p", "r", mark_seen=False)["count"] == 1
    assert store.check_mailbox("p", "r")["count"] == 1  # still unseen after a peek


def test_backlog_drains_in_pages(store):
    for i in range(3):
        store.post("p", "a", f"s{i}", f"body {i}")
    assert store.check_mailbox("p", "r", limit=2)["count"] == 2
    assert store.check_mailbox("p", "r", limit=2)["count"] == 1
    assert store.check_mailbox("p", "r", limit=2)["count"] == 0


def test_search_is_token_and(store):
    store.post("p", "a", "Deploy", "the deploy is frozen")
    store.post("p", "a", "API", "renamed orders endpoint")
    assert store.search("p", "deploy")["count"] == 1
    assert store.search("p", "frozen deploy")["count"] == 1  # both tokens in one message
    assert store.search("p", "deploy orders")["count"] == 0  # tokens span two messages
    assert store.search("p", "nope")["count"] == 0


def test_get_thread_is_chronological(store):
    a = store.post("p", "a", "root", "first")
    store.post("p", "b", "reply", "second", thread_id=a["thread_id"])
    t = store.get_thread("p", a["thread_id"])
    assert t["count"] == 2
    assert [m["subject"] for m in t["messages"]] == ["root", "reply"]
    assert t["messages"][0]["body"] == "first"  # full body, not a snippet


def test_read_message_and_missing(store):
    a = store.post("p", "a", "subj", "the body")
    assert store.read("p", a["message_id"])["body"] == "the body"
    assert store.read("p", "9999") is None


def test_list_projects_sorted_by_activity(store):
    store.post("alpha", "a", "s", "b")
    store.post("beta", "a", "s", "b")
    slugs = [p["slug"] for p in store.list_projects()["projects"]]
    assert slugs == ["beta", "alpha"]  # most recently active first


def test_list_feed_is_newest_first_and_non_mutating(store):
    store.post("p", "a", "one", "b1")
    store.post("p", "a", "two", "b2")
    feed = store.list_feed("p")
    assert [m["subject"] for m in feed["messages"]] == ["two", "one"]  # newest first
    # viewing the feed must NOT advance a reader's watermark
    assert store.check_mailbox("p", "r")["count"] == 2


def test_to_is_metadata_not_routing(store):
    store.post("p", "sender", "subj", "body", to=["x", "y"])
    assert store.read("p", "1")["to"] == ["x", "y"]
    # an agent not in `to` still sees it - it's a project feed, not directed mail
    assert store.check_mailbox("p", "someone-else")["count"] == 1


def test_importance_defaults_and_stored(store):
    store.post("p", "a", "s", "b", importance="high")
    store.post("p", "a", "s2", "b2")
    assert store.read("p", "1")["importance"] == "high"
    assert store.read("p", "2")["importance"] == "normal"  # default


def test_ttl_is_set(store):
    store.post("p", "a", "s", "b", ttl_days=30)
    ttl = store.r.ttl(store._k("p", "msg", "1"))
    assert 2592000 - 120 <= ttl <= 2592000


def test_dangling_entry_cleaned_on_read(store):
    store.post("p", "a", "one", "b1")
    store.post("p", "a", "two", "b2")
    store.r.delete(store._k("p", "msg", "1"))  # simulate message 1 having expired
    res = store.check_mailbox("p", "r")
    assert res["count"] == 1
    assert res["messages"][0]["subject"] == "two"
    assert store.r.zcard(store._k("p", "feed")) == 1  # dangling id lazily removed from feed


def test_slug_normalization():
    assert _slug("My Project!") == "my-project"
    assert _slug("") == "default"
    assert _slug(None) == "default"


def test_underscore_and_dash_are_one_project(store):
    # "layer_align" and "layer-align" must not become two separate feeds
    assert _slug("layer_align") == _slug("layer-align") == "layer-align"
    store.post("layer_align", "a", "one", "b1")
    assert store.check_mailbox("layer-align", "r")["count"] == 1


def test_tokenizer_drops_short_tokens():
    assert _tokenize("Hi the Deploy/Orders v2") == {"hi", "the", "deploy", "orders", "v2"}
    assert "a" not in _tokenize("a big cat")  # single chars dropped


# --- regression tests for the 2026-06-06 audit fixes -------------------------

def test_burst_no_drops_and_in_order(store):
    # ids reach two digits (1..13). With id-based feed scoring, id 9 must precede id 10,
    # and draining in small pages must deliver every message exactly once, in order.
    for i in range(13):
        store.post("p", "a", f"s{i}", f"body {i}")
    seen = []
    while True:
        page = store.check_mailbox("p", "r", limit=3, bootstrap="all")
        if page["count"] == 0:
            break
        seen += [m["subject"] for m in page["messages"]]
    assert seen == [f"s{i}" for i in range(13)]  # all 13, in post order: no drops, no scramble


def test_empty_check_then_immediate_post_is_delivered(store):
    assert store.check_mailbox("p", "r")["count"] == 0  # empty mailbox leaves watermark untouched
    store.post("p", "a", "s", "b")
    assert store.check_mailbox("p", "r")["count"] == 1  # the post is not lost to a wall-clock race


def test_limit_zero_returns_nothing_and_does_not_advance(store):
    store.post("p", "a", "s", "b")
    assert store.check_mailbox("p", "r", limit=0)["count"] == 0
    assert store.check_mailbox("p", "r")["count"] == 1  # limit=0 didn't consume the message


def test_unicode_search(store):
    store.post("p", "a", "Bericht", "Müller war im Café")
    assert store.search("p", "müller")["count"] == 1  # accented, case-insensitive
    assert store.search("p", "café")["count"] == 1
    assert store.search("p", "MÜLLER")["count"] == 1  # casefold


# --- recency: fresh readers, time-scoped catch-up, latest -------------------

def _age(store, project, msg_id, hours):
    """Backdate one message by `hours` (created_at is what since_hours reads)."""
    store.r.hset(store._k(project, "msg", str(msg_id)), "created_at",
                 repr(time.time() - hours * 3600))


def test_fresh_reader_gets_only_recent_messages(store):
    for i in range(10):
        store.post("p", "a", f"s{i}", f"body {i}")
    res = store.check_mailbox("p", "newcomer")
    assert [m["subject"] for m in res["messages"]] == ["s7", "s8", "s9"]  # newest 3 only
    assert res["skipped"] == 7
    assert store.check_mailbox("p", "newcomer")["count"] == 0  # and it is now caught up


def test_renamed_agent_does_not_replay_the_archive(store):
    for i in range(10):
        store.post("p", "a", f"s{i}", f"body {i}")
    store.check_mailbox("p", "agent-v1")
    store.post("p", "a", "fresh", "new one")
    res = store.check_mailbox("p", "agent-v2")  # same reader, different name string
    assert res["count"] == 3  # not 11
    assert res["messages"][-1]["subject"] == "fresh"


def test_bootstrap_all_replays_everything(store):
    for i in range(10):
        store.post("p", "a", f"s{i}", f"body {i}")
    res = store.check_mailbox("p", "r", bootstrap="all")
    assert res["count"] == 10
    assert res["skipped"] == 0


def test_bootstrap_size_is_configurable(store):
    for i in range(10):
        store.post("p", "a", f"s{i}", f"body {i}")
    assert store.check_mailbox("p", "r", bootstrap=2)["count"] == 2


def test_bootstrap_zero_starts_caught_up(store):
    # 0 must mean "no history", not "all history": it is the input a caller reaches for
    # when it wants to start clean, and the archive is the one thing it must never get
    for i in range(50):
        store.post("p", "a", f"s{i}", f"body {i}")
    assert store.check_mailbox("p", "r", bootstrap=0)["count"] == 0
    store.post("p", "a", "after", "b")
    res = store.check_mailbox("p", "r")  # but it is caught up, not deaf
    assert [m["subject"] for m in res["messages"]] == ["after"]


def test_bootstrap_none_falls_back_to_the_default(store):
    for i in range(10):
        store.post("p", "a", f"s{i}", f"body {i}")
    assert store.check_mailbox("p", "r", bootstrap=None)["count"] == 3
    assert store.check_mailbox("p", "r2", bootstrap="nonsense")["count"] == 3


def test_bootstrap_only_applies_to_the_first_check(store):
    for i in range(10):
        store.post("p", "a", f"s{i}", f"body {i}")
    store.check_mailbox("p", "r", bootstrap=1)
    for i in range(5):
        store.post("p", "a", f"n{i}", f"body {i}")
    res = store.check_mailbox("p", "r", bootstrap=1)  # bootstrap no longer applies
    assert res["count"] == 5
    assert res["skipped"] == 0


def test_bootstrap_peek_does_not_strand_the_backlog(store):
    for i in range(10):
        store.post("p", "a", f"s{i}", f"body {i}")
    assert store.check_mailbox("p", "r", mark_seen=False)["count"] == 3
    assert store.check_mailbox("p", "r", bootstrap="all")["count"] == 10  # peek consumed nothing


def test_since_hours_skips_older_messages(store):
    store.post("p", "a", "yesterday", "b")
    store.post("p", "a", "this-morning", "b")
    _age(store, "p", 1, 50)
    _age(store, "p", 2, 2)
    res = store.check_mailbox("p", "r", since_hours=24, bootstrap="all")
    assert [m["subject"] for m in res["messages"]] == ["this-morning"]
    assert res["skipped"] == 1


def test_since_hours_with_a_stale_watermark(store):
    # the trap: a reader far behind on a big backlog asking "anything today?" must get
    # today's message, not a page of old ones (and not "nothing new")
    for i in range(30):
        store.post("p", "a", f"old{i}", "b")
        _age(store, "p", i + 1, 100)
    store.check_mailbox("p", "r", limit=1, bootstrap="all")  # watermark parked at the start
    store.post("p", "a", "today", "b")
    res = store.check_mailbox("p", "r", since_hours=24, limit=20)
    assert [m["subject"] for m in res["messages"]] == ["today"]
    assert res["skipped"] == 29


def test_since_hours_survives_an_expired_message_mid_feed(store):
    # ttl_days is per message, so a short-lived message can expire while OLDER neighbours
    # are still alive. The cutoff search must skip the dangling entry, not treat it as a
    # boundary: getting this wrong floors the watermark past live unread messages, and the
    # floor is persisted, so they are lost for good.
    for name, hours in [("older-live", 40), ("expired", 30), ("newer-live", 28)]:
        r = store.post("p", "a", name, "b")
        _age(store, "p", r["message_id"], hours)
    store.r.delete(store._k("p", "msg", "2"))  # the short-TTL one expires first

    res = store.check_mailbox("p", "r", since_hours=48, bootstrap="all")
    assert [m["subject"] for m in res["messages"]] == ["older-live", "newer-live"]
    assert res["skipped"] == 0


def test_since_hours_with_nothing_recent_stays_empty(store):
    store.post("p", "a", "old", "b")
    _age(store, "p", 1, 100)
    assert store.check_mailbox("p", "r", since_hours=24)["count"] == 0
    # skipped stays skipped: it must not reappear on the next, unfiltered check
    assert store.check_mailbox("p", "r")["count"] == 0


def test_latest_is_newest_first_and_read_only(store):
    for i in range(5):
        store.post("p", "a", f"s{i}", f"body {i}")
    res = store.latest("p", limit=2)
    assert [m["subject"] for m in res["messages"]] == ["s4", "s3"]  # newest first
    assert res["total"] == 5
    assert store.check_mailbox("p", "r", bootstrap="all")["count"] == 5  # consumed nothing


def test_latest_since_hours(store):
    store.post("p", "a", "old", "b")
    store.post("p", "a", "new", "b")
    _age(store, "p", 1, 100)
    assert [m["subject"] for m in store.latest("p", since_hours=24)["messages"]] == ["new"]


def test_latest_on_empty_project(store):
    res = store.latest("nothing-here")
    assert res["count"] == 0 and res["total"] == 0 and res["messages"] == []
