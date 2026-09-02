"""Tests for the advisory external spot-price comparison (pricefeed.py)."""

import pytest

from conftest import FakeResponse, FakeSession
from swapsack.net import RateLimited
from swapsack.pricefeed import (
    COINGECKO_IDS,
    PriceFeed,
    loss_amount,
    loss_vs_market_bps,
    market_out,
    parse_prices,
)


def test_parse_prices_keeps_every_currency():
    payload = {
        "bitcoin": {"usd": 60701, "eur": 53313},
        "dash": {"usd": 33.69, "eur": 29.59},
    }
    assert parse_prices(payload) == {
        "bitcoin": {"usd": 60701.0, "eur": 53313.0},
        "dash": {"usd": 33.69, "eur": 29.59},
    }


def test_parse_prices_skips_malformed_entries():
    payload = {"bitcoin": {"usd": 60072}, "broken": "n/a"}
    assert parse_prices(payload) == {"bitcoin": {"usd": 60072.0}}


def test_loss_amount_is_destination_units_below_market():
    # Market mid would give 4.0; the pool quotes 3.98 -> 0.02 units lost.
    assert loss_amount(3.98, 4.00) == pytest.approx(0.02)
    # Pool priced in our favour -> negative "loss".
    assert loss_amount(4.02, 4.00) == pytest.approx(-0.02)


def test_market_out_uses_the_price_ratio():
    # 0.1 BTC at $60,000 == $6,000; at $1,500/ETH that is 4 ETH.
    assert market_out(0.1, 60_000, 1_500) == pytest.approx(4.0)


def test_market_out_rejects_nonpositive_destination_price():
    with pytest.raises(ValueError):
        market_out(1.0, 60_000, 0)


def test_loss_vs_market_bps_positive_when_quote_below_market():
    # Receiving 3.98 vs a 4.00 market mid == 50 bps of loss.
    assert loss_vs_market_bps(3.98, 4.00) == pytest.approx(50.0)


def test_loss_vs_market_bps_negative_when_pool_favours_you():
    assert loss_vs_market_bps(4.02, 4.00) < 0


def test_loss_vs_market_bps_guards_zero_market():
    assert loss_vs_market_bps(1.0, 0.0) == 0.0


def test_tokens_map_to_the_underlying_asset_regardless_of_chain():
    assert COINGECKO_IDS["USDT-ETH"] == COINGECKO_IDS["USDT-TRON"] == "tether"
    assert COINGECKO_IDS["USDC-ETH"] == "usd-coin"
    # Same token on cheaper chains — same price, so the market line works there
    # too rather than silently dropping out.
    assert COINGECKO_IDS["USDC-AVAX"] == COINGECKO_IDS["USDC-ARB"] == "usd-coin"
    assert COINGECKO_IDS["USDT-AVAX"] == "tether"
    assert COINGECKO_IDS["DASH"] == "dash"
    assert COINGECKO_IDS["ZEC"] == "zcash"
    assert COINGECKO_IDS["CACAO"] == "cacao"
    assert COINGECKO_IDS["RUNE"] == "thorchain"
    # Avalanche's native coin is "avalanche-2", NOT "avalanche" (a different,
    # defunct coin) — a wrong id silently drops the market line rather than
    # erroring, so it is worth pinning.
    assert COINGECKO_IDS["AVAX"] == "avalanche-2"


def test_a_throttled_price_lookup_never_sleeps(monkeypatch):
    """A courtesy line must not delay a money path, however throttled it is.

    The shared HTTP client honours a 429's ``Retry-After`` up to 30s and, with
    the default two retries, would sleep twice — up to a minute in front of a
    swap confirmation that has already asked for the passphrase. The feed's
    every caller treats a failure as "skip the line", so it opts out of
    retrying entirely: the 429 comes straight back as ``RateLimited`` and the
    caller drops the line.
    """
    slept: list[float] = []
    monkeypatch.setattr("swapsack.net.time.sleep", slept.append)
    feed = PriceFeed()
    feed._session = FakeSession(  # type: ignore[assignment]
        FakeResponse(429, Retry_After="30"),
        FakeResponse(429, Retry_After="30"),
        FakeResponse(429, Retry_After="30"),
    )
    with pytest.raises(RateLimited):
        feed.spot(["bitcoin"], vs=("eur",))
    assert slept == []


def test_the_price_feed_opts_out_of_retrying():
    # The declaration behind the test above: retries are the mechanism, and a
    # subclass that quietly inherits DEFAULT_RETRIES reintroduces the stall.
    assert PriceFeed()._retries == 0
