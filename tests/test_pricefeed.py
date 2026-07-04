"""Tests for the advisory external spot-price comparison (pricefeed.py)."""

import pytest

from cryptoswap_wallet.pricefeed import (
    COINGECKO_IDS,
    loss_amount,
    loss_vs_market_bps,
    market_out,
    parse_prices,
    parse_spot,
)


def test_parse_spot_extracts_usd_and_skips_malformed():
    payload = {
        "bitcoin": {"usd": 60072},
        "ethereum": {"usd": 1612.35},
        "broken": {},  # no usd key -> dropped
    }
    assert parse_spot(payload) == {"bitcoin": 60072.0, "ethereum": 1612.35}


def test_parse_prices_keeps_every_currency():
    payload = {
        "bitcoin": {"usd": 60701, "eur": 53313},
        "dash": {"usd": 33.69, "eur": 29.59},
    }
    assert parse_prices(payload) == {
        "bitcoin": {"usd": 60701.0, "eur": 53313.0},
        "dash": {"usd": 33.69, "eur": 29.59},
    }


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
    assert COINGECKO_IDS["DASH"] == "dash"
    assert COINGECKO_IDS["ZEC"] == "zcash"
    assert COINGECKO_IDS["CACAO"] == "cacao"
