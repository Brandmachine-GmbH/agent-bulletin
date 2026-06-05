"""Smoke test for agent-bulletin's RedisStore against a live Redis.

Runs end-to-end (post / check_mailbox / search / get_thread / read / list_projects),
asserting expected behaviour, then cleans up its own keys. Uses a throwaway project so
it never touches real data.

Run:  .venv/bin/python test_smoke.py
"""

import sys

from agent_bulletin.store import RedisStore

PROJECT = "_smoketest"


def main() -> int:
    store = RedisStore()
    store.ping()
    store.flush_project(PROJECT)  # start clean

    failures = []

    def check(name, cond):
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            failures.append(name)

    # --- post three messages -------------------------------------------------
    r1 = store.post(PROJECT, "backend", "Deploy frozen",
                    "The deploy is frozen until 3pm today.", importance="high")
    r2 = store.post(PROJECT, "backend", "API change",
                    "Renamed /orders to /v2/orders, update your calls.")
    r3 = store.post(PROJECT, "research", "Constraint found",
                    "Vendor rate limit is 100 rpm.")
    check("post returns ids", all(r.get("message_id") for r in (r1, r2, r3)))
    check("new message starts its own thread", r1["thread_id"] == r1["message_id"])

    # --- first check sees all three, oldest first ----------------------------
    c1 = store.check_mailbox(PROJECT, "ui-agent")
    check("ui-agent sees 3 new", c1["count"] == 3)
    check("oldest-first order",
          [m["subject"] for m in c1["messages"]] == ["Deploy frozen", "API change", "Constraint found"])

    # --- second check sees nothing (watermark advanced) ----------------------
    c2 = store.check_mailbox(PROJECT, "ui-agent")
    check("second check sees 0", c2["count"] == 0)

    # --- a different agent has an independent watermark ----------------------
    c3 = store.check_mailbox(PROJECT, "other-agent")
    check("independent watermark: other-agent sees 3", c3["count"] == 3)

    # --- post a fourth; ui-agent sees only the new one -----------------------
    store.post(PROJECT, "backend", "Deploy unfrozen", "Deploy is open again.")
    c4 = store.check_mailbox(PROJECT, "ui-agent")
    check("ui-agent sees 1 new after 4th",
          c4["count"] == 1 and c4["messages"][0]["subject"] == "Deploy unfrozen")

    # --- peek (mark_seen=False) does not advance the watermark ---------------
    p1 = store.check_mailbox(PROJECT, "peeker", mark_seen=False)
    check("peek sees 4", p1["count"] == 4)
    p2 = store.check_mailbox(PROJECT, "peeker")
    check("peek did not advance watermark (still 4)", p2["count"] == 4)
    p3 = store.check_mailbox(PROJECT, "peeker")
    check("after a real check, 0 new", p3["count"] == 0)

    # --- search (token AND) --------------------------------------------------
    check("search 'deploy' finds 2", store.search(PROJECT, "deploy")["count"] == 2)
    check("search 'orders' finds 1", store.search(PROJECT, "orders")["count"] == 1)
    check("search 'rate limit' (AND) finds 1", store.search(PROJECT, "rate limit")["count"] == 1)
    check("search miss finds 0", store.search(PROJECT, "nonexistentwordxyz")["count"] == 0)

    # --- threads -------------------------------------------------------------
    store.post(PROJECT, "ui-agent", "Re: API change",
               "Updated the frontend calls.", thread_id=r2["thread_id"])
    t = store.get_thread(PROJECT, r2["thread_id"])
    check("thread has 2 messages", t["count"] == 2)
    check("thread is chronological", t["messages"][0]["subject"] == "API change")
    check("thread messages carry full body", t["messages"][0].get("body", "").startswith("Renamed"))

    # --- read one message ----------------------------------------------------
    rd = store.read(PROJECT, r1["message_id"])
    check("read returns full body", rd is not None and rd["body"].startswith("The deploy is frozen"))
    check("read missing returns None", store.read(PROJECT, "999999") is None)

    # --- list projects -------------------------------------------------------
    slugs = [p["slug"] for p in store.list_projects()["projects"]]
    check("_smoketest appears in list_projects", PROJECT in slugs)

    # --- TTL is ~30 days -----------------------------------------------------
    ttl = store.r.ttl(store._k(PROJECT, "msg", r1["message_id"]))
    check("message TTL is ~30 days", 2592000 - 120 <= ttl <= 2592000)

    # --- cleanup -------------------------------------------------------------
    store.flush_project(PROJECT)
    remaining = list(store.r.scan_iter(match=f"am:{PROJECT}:*"))
    check("cleanup removed all project keys", len(remaining) == 0)

    print()
    if failures:
        print(f"{len(failures)} test(s) FAILED: {failures}")
        return 1
    print("All smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
