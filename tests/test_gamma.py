"""Tests for market scanner safety behavior."""

from datetime import datetime, timedelta, timezone

from pmbot import gamma


def _mk(cid, pool, liquidity, volume_24h, band=3.0, mid=0.5):
    end = datetime.now(timezone.utc) + timedelta(hours=48)
    return gamma.Market(
        question=f"market {cid}", condition_id=cid,
        yes_token=f"{cid}-y", no_token=f"{cid}-n",
        min_size=50.0, max_spread_cents=band, daily_pool=pool,
        liquidity=liquidity, volume_24h=volume_24h, tick=0.01,
        end_date=end, neg_risk=False, best_bid=mid - 0.01, best_ask=mid + 0.01,
    )


def _scan_cfg(**scanner_overrides):
    sc = {
        "mid_range": [0.15, 0.85], "min_hours_to_end": 14,
        "exclude_keywords": [], "min_pool_per_day": 25,
        "max_min_size_shares": 100, "min_pool_to_liquidity": 0.01,
        "max_fee_bps": 0, "fee_penalty_mult": 0.5, "top_n_markets": 5,
    }
    sc.update(scanner_overrides)
    return {"scanner": sc, "quoting": {"max_capital_per_market": 50}}


def test_turnover_penalty_demotes_high_churn_market(monkeypatch):
    # Two markets, identical reward density (pool/liquidity), but one churns 20x
    # more volume — the toxicity penalty should rank the calm one first.
    calm = _mk("calm", pool=100, liquidity=5000, volume_24h=5000)
    churn = _mk("churn", pool=100, liquidity=5000, volume_24h=100000)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [churn, calm])
    monkeypatch.setattr(gamma, "_fetch_fee_bps", lambda *a: 0)
    ranked = gamma.scan(_scan_cfg(toxicity_turnover_penalty=0.05, band_room_bonus=0.0))
    assert [m.condition_id for m in ranked] == ["calm", "churn"]


def test_band_room_bonus_prefers_wider_band(monkeypatch):
    narrow = _mk("narrow", pool=100, liquidity=5000, volume_24h=5000, band=1.0)
    wide = _mk("wide", pool=100, liquidity=5000, volume_24h=5000, band=4.0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [narrow, wide])
    monkeypatch.setattr(gamma, "_fetch_fee_bps", lambda *a: 0)
    ranked = gamma.scan(_scan_cfg(toxicity_turnover_penalty=0.0, band_room_bonus=0.10))
    assert ranked[0].condition_id == "wide"


def test_zero_weights_reproduce_density_ranking(monkeypatch):
    # Graceful fallback: with both weights 0, ranking is pure reward density.
    a = _mk("a", pool=200, liquidity=5000, volume_24h=999999)  # higher density
    b = _mk("b", pool=100, liquidity=5000, volume_24h=0)
    monkeypatch.setattr(gamma, "fetch_reward_markets", lambda: [b, a])
    monkeypatch.setattr(gamma, "_fetch_fee_bps", lambda *a: 0)
    ranked = gamma.scan(_scan_cfg(toxicity_turnover_penalty=0.0, band_room_bonus=0.0))
    assert ranked[0].condition_id == "a"  # density wins, turnover ignored


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

