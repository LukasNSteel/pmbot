"""Tests for market scanner safety behavior."""

from pmbot import gamma


def test_fee_fetch_fails_closed(monkeypatch):
    class BadClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            raise RuntimeError("fee api down")

    monkeypatch.setattr(gamma.httpx, "Client", BadClient)

    assert gamma._fetch_fee_bps("token1", {}) is None

