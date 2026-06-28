"""Opt-in integration tests against live THORChain (read-only, no funds moved).

Excluded by default; run with `uv run pytest -m network`. They catch API drift
and stale hard-coded asset strings (e.g. the USDT contracts) that unit tests
with recorded fixtures cannot.
"""

import pytest

from cryptoswap.cli import ASSET
from cryptoswap.thorchain import ThorchainClient

pytestmark = pytest.mark.network

ETH_DEST = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
BTC_DEST = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
TRON_DEST = "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"


def test_inbound_addresses_live():
    with ThorchainClient() as thor:
        chains = thor.inbound_addresses()
    assert chains["BTC"].tradable
    assert chains["BTC"].address  # vault address present (used by LP)


def test_btc_to_eth_quote_live():
    with ThorchainClient() as thor:
        quote = thor.quote_swap("BTC.BTC", "ETH.ETH", 178100, ETH_DEST)
    assert quote.memo and quote.memo.startswith("=:")
    assert quote.expected_amount_out > 0


def test_hardcoded_usdt_assets_still_quote_live():
    # Guards the contract strings baked into cli.ASSET against THORChain changes.
    with ThorchainClient() as thor:
        to_tron = thor.quote_swap(ASSET["BTC"], ASSET["USDT-TRON"], 178100, TRON_DEST)
        to_eth = thor.quote_swap(ASSET["BTC"], ASSET["USDT-ETH"], 178100, ETH_DEST)
    assert to_tron.expected_amount_out > 0
    assert to_eth.expected_amount_out > 0


def test_eth_usdt_source_quote_live():
    with ThorchainClient() as thor:
        quote = thor.quote_swap(ASSET["USDT-ETH"], "BTC.BTC", 5_000_000_000, BTC_DEST)
    assert quote.router  # token source needs the router for depositWithExpiry
    assert quote.expected_amount_out > 0
