"""Tests for BookTracker incremental resubscription."""

import asyncio

from pmbot.books import BookTracker


def test_resubscribe_keeps_survivors_and_primes_only_new(monkeypatch):
    tracker = BookTracker(["t1", "t2"])
    tracker.books["t1"].bids[0.50] = 10.0  # state that must survive

    primed: list[str] = []

    async def fake_refresh(token_ids):
        primed.extend(token_ids)
        for t in token_ids:
            tracker.books[t].asks[0.60] = 5.0

    monkeypatch.setattr(tracker, "_rest_refresh", fake_refresh)

    asyncio.run(tracker.resubscribe(["t1", "t3"]))

    # t2 dropped, t1 kept with its book intact, t3 added and primed.
    assert set(tracker.books) == {"t1", "t3"}
    assert tracker.books["t1"].bids == {0.50: 10.0}
    assert tracker.books["t3"].asks == {0.60: 5.0}
    assert primed == ["t3"]  # only the genuinely new token hit the network


def test_resubscribe_carries_book_objects_when_provided(monkeypatch):
    tracker = BookTracker(["t1"])

    async def fake_refresh(token_ids):
        return None

    monkeypatch.setattr(tracker, "_rest_refresh", fake_refresh)

    from pmbot.books import Book
    carried = Book("t9")
    carried.bids[0.42] = 7.0
    asyncio.run(tracker.resubscribe(["t1", "t9"], carry={"t9": carried}))

    assert tracker.books["t9"] is carried  # reused, not re-primed
