"""Tests for the Chainflip backend (quote-only, phase B1).

Chainflip is a cross-chain JIT AMM: a second *independent* venue next to
THORChain/Maya, which matters most exactly when those halt (see
``docs/halt-alternatives.md``). This phase wires its keyless REST quote in as a
read-only price source for ``--backend auto``; execution is a separate phase, so
the backend advertises a ``vault-swap`` executor the CLI refuses to run.

The interesting work here is fee normalization. ``SwapFees`` is destination-
denominated, but Chainflip itemizes its fees in *three different assets* —
INGRESS in the source, NETWORK in the intermediate (USDC), EGRESS in the
destination — so each has to be converted at the quote's own rates before the
cost display and ``best_quote`` can treat it like any other backend's quote.
"""

import pytest

from swapsack.backends import Backend, best_quote, gather_quotes
from swapsack.chainflip import (
    CHAINFLIP_ASSETS,
    ChainflipBackend,
    ChainflipError,
    ChainflipQuote,
    deposit_units,
    parse_chainflip_quote,
    select_quote,
)
from swapsack.thorchain import THORCHAIN_UNIT, Quote, SwapFees

BTC = "BTC.BTC"
ETH = "ETH.ETH"
USDC_ETH = "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
USDT_ETH = "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7"

# Recorded live from GET chainflip-swap.chainflip.io/v2/quote on 2026-08-28
# (0.1 BTC -> ETH). The real response is a JSON *array*; each entry may also
# carry a "boostQuote" variant, dropped here (boost buys faster confirmation
# for an extra fee — not the default route).
QUOTE_PAYLOAD = {
    "intermediateAmount": "7962767940",
    "egressAmount": "3175019159293365307",
    "recommendedSlippageTolerancePercent": 2.5,
    "includedFees": [
        {"chain": "Bitcoin", "asset": "BTC", "amount": "186", "type": "INGRESS"},
        {"chain": "Ethereum", "asset": "USDC", "amount": "7970739", "type": "NETWORK"},
        {
            "chain": "Ethereum",
            "asset": "ETH",
            "amount": "22202614140000",
            "type": "EGRESS",
        },
    ],
    "lowLiquidityWarning": False,
    "estimatedDurationSeconds": 1014,
    "estimatedPrice": "31.75100418775294527478",
    "type": "REGULAR",
    "srcAsset": {"chain": "Bitcoin", "asset": "BTC"},
    "destAsset": {"chain": "Ethereum", "asset": "ETH"},
    "depositAmount": "10000000",
}

EGRESS_WEI = 3175019159293365307
DEPOSIT_SATS = 10_000_000
INTERMEDIATE = 7_962_767_940


def _quote(payload=None, *, from_asset=BTC, to_asset=ETH) -> ChainflipQuote:
    return parse_chainflip_quote(
        payload if payload is not None else QUOTE_PAYLOAD,
        from_asset=from_asset,
        to_asset=to_asset,
    )


# --- quote parsing / normalization ------------------------------------------


def test_expected_amount_out_is_1e8_of_destination():
    # 3.175019159… ETH, truncated to 1e8 units so best_quote can compare it
    # against a thornode quote.
    assert _quote().expected_amount_out == EGRESS_WEI * THORCHAIN_UNIT // 10**18
    assert _quote().expected_amount_out == 317_501_915


def test_native_amounts_are_kept_verbatim():
    q = _quote()
    assert q.egress_amount == EGRESS_WEI
    assert q.deposit_amount == DEPOSIT_SATS
    assert q.intermediate_amount == INTERMEDIATE
    assert q.estimated_duration_seconds == 1014
    assert q.low_liquidity_warning is False
    assert q.recommended_slippage_bps == 250


def test_egress_fee_needs_no_conversion():
    # EGRESS is already charged in the destination asset.
    assert _quote().fees.egress == 22202614140000 * THORCHAIN_UNIT // 10**18


def test_ingress_fee_converts_at_the_quotes_own_end_to_end_rate():
    # 186 sats of a 10_000_000 sat deposit, valued in the ETH that deposit buys.
    expected_wei = 186 * EGRESS_WEI // DEPOSIT_SATS
    assert _quote().fees.ingress == expected_wei * THORCHAIN_UNIT // 10**18


def test_network_fee_converts_via_the_intermediate_leg():
    # The NETWORK fee is charged in USDC on the intermediate leg, so it converts
    # at intermediate->destination, not at the end-to-end rate.
    expected_wei = 7970739 * EGRESS_WEI // INTERMEDIATE
    assert _quote().fees.network == expected_wei * THORCHAIN_UNIT // 10**18


def test_fees_total_is_the_three_legs_and_bps_is_input_relative():
    f = _quote().fees
    assert f.total == f.ingress + f.network + f.egress
    # Chainflip's explicit fees are dominated by the 0.1% network fee.
    assert f.total_bps == pytest.approx(10, abs=1)
    assert f.asset == ETH
    assert f.affiliate == 0


def test_outbound_maps_to_egress_and_liquidity_to_the_rest():
    # The generic SwapFees surface still has to mean something: `outbound` is
    # the flat destination-chain delivery fee (EGRESS), everything else is the
    # cost of getting through the pools.
    f = _quote().fees
    assert f.outbound == f.egress
    assert f.liquidity == f.ingress + f.network


def test_slippage_bps_is_zero_because_chainflip_does_not_quote_realised_slip():
    # recommendedSlippageTolerancePercent is a *tolerance*, not realised slip —
    # reporting it as slip would overstate the cost. The market comparison line
    # is what surfaces the pool-vs-market spread.
    assert _quote().fees.slippage_bps == 0


def test_breakdown_names_chainflips_own_fee_legs():
    lines = "\n".join(_quote().fees.breakdown("ETH"))
    assert "ingress" in lines
    assert "network" in lines
    assert "egress" in lines
    assert "quoted total" in lines
    # THORChain's slip/swap wording would be a lie here — Chainflip's slip is in
    # the price, not in an itemised fee.
    assert "slip/swap" not in lines


def test_breakdown_matches_swapfees_shape():
    # _print_swap_costs is duck-typed over both; keep the surface identical.
    assert set(f.name for f in SwapFees.__dataclass_fields__.values()) <= set(
        _quote().fees.__dataclass_fields__
    )


# --- the list envelope and the boost variant --------------------------------


def test_select_quote_picks_the_regular_entry_from_the_array():
    boost = {**QUOTE_PAYLOAD, "type": "BOOST", "egressAmount": "1"}
    assert select_quote([boost, QUOTE_PAYLOAD])["type"] == "REGULAR"


def test_select_quote_accepts_a_bare_object():
    assert select_quote(QUOTE_PAYLOAD) is QUOTE_PAYLOAD


def test_select_quote_falls_back_to_the_first_entry():
    only = {**QUOTE_PAYLOAD, "type": "SOMETHING_NEW"}
    assert select_quote([only]) is only


def test_boost_quote_is_ignored():
    # A nested boostQuote must not leak into the parsed numbers: it costs an
    # extra fee for faster confirmation and is not the route we would take.
    payload = {**QUOTE_PAYLOAD, "boostQuote": {**QUOTE_PAYLOAD, "egressAmount": "1"}}
    assert _quote(payload).egress_amount == EGRESS_WEI


# --- error handling ---------------------------------------------------------


def test_error_payload_raises():
    with pytest.raises(ChainflipError, match="insufficient liquidity"):
        _quote({"message": "insufficient liquidity for requested amount"})


def test_malformed_payload_raises_chainflip_error_not_keyerror():
    # A gateway error page served as a 200 must abort cleanly, not surface as a
    # bare KeyError out of the execute path.
    with pytest.raises(ChainflipError, match="malformed"):
        _quote({"egressAmount": "3", "includedFees": []})


def test_empty_array_raises():
    with pytest.raises(ChainflipError):
        select_quote([])


# --- unit scaling -----------------------------------------------------------


def test_deposit_units_scales_from_the_wallet_wide_1e8():
    assert deposit_units(10_000_000, 8) == 10_000_000  # BTC: identity
    assert deposit_units(10_000_000, 18) == 10_000_000 * 10**10  # ETH
    assert deposit_units(10_000_000, 6) == 100_000  # USDC


# --- the backend surface ----------------------------------------------------


class _StubClient:
    """Stands in for ChainflipClient; records calls, returns canned payloads."""

    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else QUOTE_PAYLOAD
        self.error = error
        self.calls = []
        self.closed = False

    def quote(self, src, dst, amount):
        self.calls.append((src, dst, amount))
        if self.error is not None:
            raise self.error
        return self.payload

    def close(self):
        self.closed = True


def _backend(**kw) -> ChainflipBackend:
    return ChainflipBackend(_StubClient(**kw))


def test_serves_known_cross_chain_pair():
    assert _backend().serves(BTC, ETH)
    assert _backend().serves(ETH, BTC)


def test_serves_same_chain_token_pair():
    # Ethereum USDT -> USDC quotes fine on Chainflip; it is not thornode-only.
    assert _backend().serves(USDT_ETH, USDC_ETH)


def test_does_not_serve_unknown_or_identical_assets():
    assert not _backend().serves(BTC, "MAYA.CACAO")
    assert not _backend().serves("MAYA.CACAO", ETH)
    assert not _backend().serves(BTC, BTC)


def test_executor_is_vault_swap_so_the_cli_will_not_route_a_swap_here_yet():
    assert ChainflipBackend.executor == "vault-swap"


def test_try_quote_scales_the_amount_to_source_native_units():
    backend = _backend()
    backend.try_quote(BTC, ETH, 10_000_000, None)
    src, dst, amount = backend.client.calls[0]
    assert src == CHAINFLIP_ASSETS[BTC][:2]
    assert dst == CHAINFLIP_ASSETS[ETH][:2]
    assert amount == 10_000_000


def test_try_quote_returns_a_comparable_quote():
    q = _backend().try_quote(BTC, ETH, 10_000_000, None)
    assert q is not None
    assert q.expected_amount_out == 317_501_915


def test_try_quote_needs_no_destination():
    # Unlike CoW (whose API wants a from/receiver), the Chainflip quote is
    # purely a price — so `quote` works before a --dest is known.
    assert _backend().try_quote(BTC, ETH, 10_000_000, None) is not None


def test_try_quote_declines_a_pair_it_cannot_serve():
    backend = _backend()
    assert backend.try_quote(BTC, "MAYA.CACAO", 10_000_000, None) is None
    assert backend.client.calls == []


def test_try_quote_declines_a_streaming_request():
    # Streaming is a thornode concept; ruling the backend out beats silently
    # ignoring the flag and quoting a route the swap would not take.
    backend = _backend()
    assert backend.try_quote(BTC, ETH, 10_000_000, None, streaming_interval=1) is None
    assert backend.client.calls == []


def test_try_quote_swallows_an_api_error():
    backend = _backend(error=ChainflipError("insufficient liquidity"))
    assert backend.try_quote(BTC, ETH, 10_000_000, None) is None


def test_try_quote_swallows_a_malformed_body():
    assert _backend(payload={"nonsense": True}).try_quote(BTC, ETH, 1000, None) is None


def test_try_quote_declines_a_dust_amount():
    # Scaling to native units must not floor to zero and ask for a 0 quote.
    backend = _backend()
    assert backend.try_quote(USDC_ETH, ETH, 1, None) is None
    assert backend.client.calls == []


# --- routing next to the thornode backends ----------------------------------


def _thor_quote(out: int) -> Quote:
    return Quote(
        inbound_address="bc1qvault",
        expected_amount_out=out,
        memo="=:ETH.ETH:0xdead",
        fees=SwapFees(ETH, 1, 0, 1, 2, 3, 4),
        recommended_min_amount_in=0,
        expiry=0,
        dust_threshold=0,
        recommended_gas_rate=0,
        gas_rate_units="satsperbyte",
        router=None,
        max_streaming_quantity=0,
        streaming_swap_blocks=0,
        total_swap_seconds=0,
        raw={},
    )


class _StubThor:
    def __init__(self, out):
        self.out = out

    def quote_swap(self, *a, **kw):
        return _thor_quote(self.out)

    def close(self):
        pass


def test_gather_quotes_prices_chainflip_alongside_thornode():
    backends = [Backend("thorchain", _StubThor(1)), _backend()]
    results = gather_quotes(backends, BTC, ETH, 10_000_000, "0xdead")
    assert [b.name for b, _ in results] == ["thorchain", "chainflip"]


def test_best_quote_routes_to_chainflip_when_it_pays_more():
    backends = [Backend("thorchain", _StubThor(1)), _backend()]
    results = gather_quotes(backends, BTC, ETH, 10_000_000, "0xdead")
    backend, quote = best_quote(results)
    assert backend.name == "chainflip"
    assert quote.expected_amount_out == 317_501_915


def test_best_quote_still_routes_to_thornode_when_it_pays_more():
    backends = [Backend("thorchain", _StubThor(10**12)), _backend()]
    backend, _ = best_quote(gather_quotes(backends, BTC, ETH, 10_000_000, "0xdead"))
    assert backend.name == "thorchain"


def test_chainflip_is_in_the_default_swap_backends():
    from swapsack.backends import swap_backends

    backends = swap_backends()
    try:
        assert "chainflip" in [b.name for b in backends]
    finally:
        for b in backends:
            b.client.close()
