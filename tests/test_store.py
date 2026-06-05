"""Unit tests for RedisStore, backed by fakeredis (no real Redis required)."""

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


def test_tokenizer_drops_short_tokens():
    assert _tokenize("Hi the Deploy/Orders v2") == {"hi", "the", "deploy", "orders", "v2"}
    assert "a" not in _tokenize("a big cat")  # single chars dropped
