"""Opt-in integration tests against live THORChain (read-only, no funds moved).

Excluded by default; run with `uv run pytest -m network`. They catch API drift
and stale hard-coded asset strings (e.g. the USDT contracts) that unit tests
with recorded fixtures cannot.
"""

import pytest

from swapsack.cli import ASSET
from swapsack.thorchain import ThorchainClient, ThorchainError

pytestmark = pytest.mark.network

ETH_DEST = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
BTC_DEST = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
TRON_DEST = "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"
LTC_DEST = "ltc1qjmxnz78nmc8nq77wuxh25n2es7rzm5c2rkk4wh"
DOGE_DEST = "DH5yaieqoZN36fDVciNyRueRGvGLR3mr7L"
BCH_DEST = "qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a"


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
    # tolerance_bps=None disables the slippage price limit: these small swaps cost
    # more than the default 3% in fees, which is unrelated to what we're guarding
    # (the asset contract strings) and is covered separately by
    # test_small_high_fee_swap_rejected_at_default_tolerance_live.
    with ThorchainClient() as thor:
        to_tron = thor.quote_swap(
            ASSET["BTC"], ASSET["USDT-TRON"], 178100, TRON_DEST, tolerance_bps=None
        )
        to_eth = thor.quote_swap(
            ASSET["BTC"], ASSET["USDT-ETH"], 178100, ETH_DEST, tolerance_bps=None
        )
        to_usdc_eth = thor.quote_swap(
            ASSET["BTC"], ASSET["USDC-ETH"], 178100, ETH_DEST, tolerance_bps=None
        )
    assert to_tron.expected_amount_out > 0
    assert to_eth.expected_amount_out > 0
    assert to_usdc_eth.expected_amount_out > 0


def test_mimir_exposes_lp_pause_toggle_live():
    # The add-liquidity pre-flight reads PAUSELP; guard the endpoint + key.
    with ThorchainClient() as thor:
        mimir = thor.mimir()
    assert "PAUSELP" in mimir
    assert isinstance(mimir["PAUSELP"], int)


def test_liquidity_provider_live():
    # The `balance` LP report parses pool/.../liquidity_provider/<addr>. Guard
    # the field names against API drift by parsing a real, live position. Many
    # listed providers are fully withdrawn (units linger, nothing redeemable ->
    # None), so probe a handful until one with redeemable value turns up.
    with ThorchainClient() as thor:
        providers = thor._get_with_fallback(
            f"{thor.path_prefix}/pool/BTC.BTC/liquidity_providers"
        ).json()
        position = None
        for lp in providers[:50]:
            position = thor.liquidity_provider("BTC.BTC", lp["asset_address"])
            if position is not None:
                break
    assert position is not None
    assert position.asset_redeem_value > 0 or position.protocol_redeem_value > 0


def test_liquidity_provider_no_position_is_none_live():
    # A never-provided address answers 200 with units 0 -> None (not an error).
    with ThorchainClient() as thor:
        assert thor.liquidity_provider("BTC.BTC", BTC_DEST) is None


def test_small_high_fee_swap_rejected_at_default_tolerance_live():
    # A small TRX->USDT-TRON swap costs well over 3% (the fixed TRON outbound fee
    # dominates a ~$25 swap), so the default tolerance makes THORChain refuse the
    # quote. Guards both that behaviour and the 'price limit' substring that
    # swap._explain_quote_error keys on to produce its actionable abort message.
    with ThorchainClient() as thor:
        with pytest.raises(ThorchainError, match="price limit"):
            thor.quote_swap(
                ASSET["TRX"],
                ASSET["USDT-TRON"],
                8_669_000_000,
                TRON_DEST,
                tolerance_bps=300,
            )


def test_eth_usdt_source_quote_live():
    # tolerance_bps=None disables the slippage price limit: this small swap's
    # fixed fees can exceed the default tolerance as the market moves, and the
    # test only checks the quote's shape (router present), not its price — so a
    # price-limit rejection would be a spurious failure (matches the siblings).
    with ThorchainClient() as thor:
        quote = thor.quote_swap(
            ASSET["USDT-ETH"], "BTC.BTC", 5_000_000_000, BTC_DEST, tolerance_bps=None
        )
    assert quote.router  # token source needs the router for depositWithExpiry
    assert quote.expected_amount_out > 0


def test_tron_usdt_source_quote_live():
    # USDT-TRON as a swap source: routerless (TRON has no THORChain router), so
    # the deposit is a plain TRC-20 transfer to inbound_address with the memo in
    # the tx data. Guards that mechanism and the memo paying the destination.
    # tolerance_bps=None disables the slippage price limit (this small swap's fees
    # exceed the default 3%, which is unrelated to the routerless mechanism here).
    with ThorchainClient() as thor:
        quote = thor.quote_swap(
            ASSET["USDT-TRON"], "BTC.BTC", 2_000_000_000, BTC_DEST, tolerance_bps=None
        )
    assert quote.router is None  # routerless — direct transfer, not depositWithExpiry
    assert quote.inbound_address  # the vault the transfer must pay
    assert quote.expected_amount_out > 0
    assert quote.memo and BTC_DEST in quote.memo


@pytest.mark.parametrize(
    ("asset", "dest"),
    [("LTC", LTC_DEST), ("DOGE", DOGE_DEST), ("BCH", BCH_DEST)],
)
def test_destination_only_assets_quote_live(asset, dest):
    # Item 3: BTC -> LTC/DOGE/BCH to an external address. Confirms the pool is
    # live and the quoted memo actually pays the destination we asked for.
    with ThorchainClient() as thor:
        quote = thor.quote_swap(ASSET["BTC"], ASSET[asset], 5_000_000, dest)
    assert quote.expected_amount_out > 0
    assert quote.memo and dest in quote.memo
