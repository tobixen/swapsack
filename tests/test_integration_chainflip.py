"""Opt-in integration tests against the live Chainflip quote API.

Excluded by default; run with `uv run pytest -m network`. These catch API drift
(field renames, a changed response envelope, an asset the service stops
serving) that unit tests against a recorded fixture cannot — which matters more
here than usual, because the whole point of this backend is to still answer when
THORChain and Maya do not.

Read-only: quoting is keyless and moves no funds.
"""

import pytest

from swapsack.chainflip import (
    CHAINFLIP_ASSETS,
    ChainflipBackend,
    ChainflipClient,
    parse_chainflip_quote,
)
from swapsack.cli import ASSET

pytestmark = pytest.mark.network

BTC = ASSET["BTC"]
ETH = ASSET["ETH"]
USDC = ASSET["USDC-ETH"]


def test_btc_to_eth_quote_live():
    with ChainflipClient() as client:
        payload = client.quote(
            CHAINFLIP_ASSETS[BTC][:2], CHAINFLIP_ASSETS[ETH][:2], 10_000_000
        )
    quote = parse_chainflip_quote(payload, from_asset=BTC, to_asset=ETH)
    assert quote.expected_amount_out > 0
    assert quote.deposit_amount == 10_000_000
    # Every fee leg the live API itemises must survive normalisation into
    # destination units — a silently dropped leg under-reports the cost.
    assert quote.fees.egress > 0
    assert quote.fees.network > 0
    assert (
        quote.fees.total == quote.fees.ingress + quote.fees.network + quote.fees.egress
    )
    # Chainflip's explicit fees are a handful of bps, not a percentage point; a
    # wildly different number means a conversion broke, not that prices moved.
    assert 0 < quote.fees.total_bps < 200


def test_backend_quotes_through_the_shared_surface_live():
    backend = ChainflipBackend(ChainflipClient())
    try:
        quote = backend.try_quote(BTC, ETH, 10_000_000, None)
    finally:
        backend.client.close()
    assert quote is not None
    # ~31 ETH per BTC at the time of writing; the bound only has to catch a
    # units error (1e8 vs 1e18), not track the market.
    assert 10**8 < quote.expected_amount_out < 10**12


def test_same_chain_token_pair_quotes_live():
    backend = ChainflipBackend(ChainflipClient())
    try:
        assert backend.try_quote(ETH, USDC, 10_000_000, None) is not None
    finally:
        backend.client.close()


def test_unservable_pair_declines_rather_than_raising_live():
    backend = ChainflipBackend(ChainflipClient())
    try:
        assert backend.try_quote(BTC, ASSET["CACAO"], 10_000_000, None) is None
    finally:
        backend.client.close()
