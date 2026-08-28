"""Tests for the Chainflip backend: the keyless quote, and the vault swap.

Chainflip is a cross-chain JIT AMM: a second *independent* venue next to
THORChain/Maya, which matters most exactly when those halt (see
``docs/halt-alternatives.md``). Its keyless REST quote price-competes in
``--backend auto``, and a BTC source settles through a *vault swap* — a plain
Bitcoin transaction paying a protocol vault, with the swap parameters in its
OP_RETURN — which is what the ``vault-swap`` executor means.

The interesting work here is fee normalization. ``SwapFees`` is destination-
denominated, but Chainflip itemizes its fees in *three different assets* —
INGRESS in the source, NETWORK in the intermediate (USDC), EGRESS in the
destination — so each has to be converted at the quote's own rates before the
cost display and ``best_quote`` can treat it like any other backend's quote.
"""

from types import SimpleNamespace

import pytest

from conftest import FakeSession
from swapsack.backends import Backend, best_quote, gather_quotes
from swapsack.chainflip import (
    CHAINFLIP_ASSETS,
    VAULT_SWAP_ASSET_IDS,
    ChainflipBackend,
    ChainflipClient,
    ChainflipError,
    ChainflipQuote,
    bitcoin_vault_addresses,
    deposit_units,
    destination_bytes,
    min_output_amount,
    parse_chainflip_quote,
    prepare_vault_swap,
    select_quote,
)
from swapsack.thorchain import THORCHAIN_UNIT, Quote, SwapFees
from swapsack.verify import (
    ChainflipVaultPlan,
    TxOutput,
    verify_chainflip_vault_swap,
)

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


def test_executor_is_vault_swap_so_the_cli_builds_the_tx_itself():
    assert ChainflipBackend.executor == "vault-swap"


def test_can_execute_is_narrower_than_serves():
    # Chainflip lists Tron and quotes BTC -> TRX happily, but a vault swap
    # encodes its destination into the payload and the gate re-derives it —
    # and Tron needs a base58check decoder the gate does not have. Quoting it
    # is useful; routing execution there dead-ends in `destination_bytes`.
    backend = _backend()
    assert backend.serves(BTC, "TRON.TRX")
    assert not backend.can_execute(BTC, "TRON.TRX")
    assert backend.can_execute(BTC, ETH)
    assert set(VAULT_SWAP_ASSET_IDS) <= set(CHAINFLIP_ASSETS)


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


# --- vault swaps (phase B2: execution) --------------------------------------
#
# A vault swap is a plain Bitcoin transaction paying a protocol vault, with the
# swap parameters in the OP_RETURN. No broker, no deposit channel, and the
# destination is ours to encode — which is what makes it gateable.

VAULT = "bc1p5rrs3gd9tlzucafucuj5jgvaj7rdtgn6je28y44wvvrv4d0vpsdslmnctx"
OTHER_VAULT = "bc1p50rzjffd3ac87492wsrefdmyqtyfthfjse9ypeg2pf5l95zclsaq8g9pc5"
DEST = "0x000000000000000000000000000000000000dEaD"
# Recorded live 2026-08-28 for BTC -> ETH, dest above, min output 3 ETH.
LIVE_PAYLOAD_HEX = (
    "0x0101000000000000000000000000000000000000dead"
    "640000002cf61a24a2290000000000000000ff01000200000000"
)


def _vault_rpc_result():
    """The shape cf_get_vault_addresses answers with: per-chain lists of
    (account id, address-as-ASCII-byte-array) pairs."""
    return {
        "bitcoin": [
            ["cFAccountOne", {"Btc": list(VAULT.encode())}],
            ["cFAccountTwo", {"Btc": list(OTHER_VAULT.encode())}],
        ],
        "ethereum": {"Eth": [1] * 20},
    }


def _build_payload(dest20, asset_id=1, min_out=3 * 10**18, retry=100, **over):
    """The 48-byte layout Chainflip encodes, rebuilt locally.

    Pinned against a recorded live encoding by the test below, so a stub that
    drifted from the real thing would fail loudly rather than validate a layout
    Chainflip does not use.
    """
    fields = dict(oracle=255, chunks=1, interval=2, boost=0, broker=0, affiliates=0)
    fields.update(over)
    return (
        bytes([1, asset_id])
        + dest20
        + retry.to_bytes(2, "little")
        + min_out.to_bytes(16, "little")
        + bytes([fields["oracle"]])
        + fields["chunks"].to_bytes(2, "little")
        + fields["interval"].to_bytes(2, "little")
        + bytes([fields["boost"], fields["broker"], fields["affiliates"]])
    )


def test_the_stub_payload_matches_a_live_chainflip_encoding():
    live = bytes.fromhex(LIVE_PAYLOAD_HEX[2:])
    assert _build_payload(bytes.fromhex("00" * 18 + "dead")) == live


class _StubRpc:
    """Stands in for ChainflipRpc's transport; records the calls it is given.

    Encodes what it is *asked* for rather than replaying a fixture, so the
    round trip (ask for a floor -> get a payload carrying it -> gate it) is
    really exercised. Pass ``encoding`` to force a specific — usually bad —
    response instead.
    """

    def __init__(self, encoding=None, vaults=None, error=None):
        self.encoding = encoding
        self.vaults = vaults if vaults is not None else _vault_rpc_result()
        self.error = error
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        if method == "cf_get_vault_addresses":
            return self.vaults
        if method == "cf_request_swap_parameter_encoding":
            if self.encoding is not None:
                return self.encoding
            _broker, _src, dst, dest, _commission, extra = params
            return {
                "chain": "Bitcoin",
                "nulldata_payload": "0x"
                + _build_payload(
                    bytes.fromhex(dest[2:]),
                    asset_id=VAULT_SWAP_ASSET_IDS[
                        next(
                            k
                            for k, v in CHAINFLIP_ASSETS.items()
                            if list(v[:2]) == [dst["chain"], dst["asset"]]
                            and k in VAULT_SWAP_ASSET_IDS
                        )
                    ],
                    min_out=int(extra["min_output_amount"], 16),
                    retry=extra["retry_duration"],
                ).hex(),
                "deposit_address": VAULT,
            }
        raise AssertionError(f"unexpected method {method}")

    def close(self):
        pass


# --- destination encoding ---------------------------------------------------


def test_destination_bytes_of_an_evm_address():
    assert destination_bytes(ETH, DEST) == bytes.fromhex(
        "000000000000000000000000000000000000dead"
    )


def test_destination_bytes_is_case_insensitive_for_evm():
    assert destination_bytes(ETH, DEST.lower()) == destination_bytes(ETH, DEST.upper())


def test_destination_bytes_rejects_a_malformed_address():
    with pytest.raises(ChainflipError):
        destination_bytes(ETH, "0xnothex")


def test_destination_bytes_rejects_a_wrong_length_address():
    with pytest.raises(ChainflipError):
        destination_bytes(ETH, "0xdead")


def test_destination_bytes_refuses_an_asset_whose_layout_we_cannot_decode():
    # The gate decodes the payload itself; an address encoding it cannot
    # reproduce must be refused rather than trusted. Tron and Solana are the
    # live cases (Tron needs base58check, Solana has a 32-byte address).
    with pytest.raises(ChainflipError, match="cannot verify"):
        destination_bytes("TRON.TRX", "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8")


def test_every_vault_swap_asset_id_names_a_known_asset():
    assert set(VAULT_SWAP_ASSET_IDS) <= set(CHAINFLIP_ASSETS)


# --- the on-chain floor -----------------------------------------------------


def test_min_output_amount_applies_the_tolerance_to_the_quote():
    q = _quote()
    assert min_output_amount(q, 250) == q.egress_amount * 9750 // 10000


def test_min_output_amount_of_zero_tolerance_is_the_whole_quote():
    q = _quote()
    assert min_output_amount(q, 0) == q.egress_amount


def test_min_output_amount_defaults_to_the_quotes_own_recommendation():
    q = _quote()
    assert min_output_amount(q, None) == min_output_amount(
        q, q.recommended_slippage_bps
    )


def test_min_output_amount_refuses_a_nonsensical_tolerance():
    with pytest.raises(ChainflipError):
        min_output_amount(_quote(), 10_000)


# --- assembling a vault swap ------------------------------------------------


def test_vault_addresses_decode_from_the_rpc_byte_arrays():
    rpc = _StubRpc()
    assert bitcoin_vault_addresses(rpc) == frozenset({VAULT, OTHER_VAULT})


def test_prepare_vault_swap_asks_for_our_destination_and_floor():
    rpc = _StubRpc()
    prepare_vault_swap(
        rpc, from_asset=BTC, to_asset=ETH, destination=DEST, quote=_quote(), bps=250
    )
    method, params = rpc.calls[-1]
    assert method == "cf_request_swap_parameter_encoding"
    assert params[3] == DEST
    assert params[4] == 0  # broker commission: nobody skims
    assert int(params[5]["min_output_amount"], 16) == min_output_amount(_quote(), 250)


def test_prepare_vault_swap_returns_a_gateable_plan():
    # The integration that matters: what the builder produces must satisfy the
    # gate. A field the two disagree about would otherwise only show up on a
    # real, irreversible transaction.
    swap = prepare_vault_swap(
        rpc=_StubRpc(),
        from_asset=BTC,
        to_asset=ETH,
        destination=DEST,
        quote=_quote(),
        bps=250,
    )
    plan = ChainflipVaultPlan(
        deposit_address=swap.deposit_address,
        amount=178100,
        payload=swap.payload,
        expiry=9_999_999_999,
        destination_asset_id=swap.destination_asset_id,
        destination_bytes=swap.destination_bytes,
        min_output_amount=swap.min_output_amount,
        known_vaults=swap.known_vaults,
    )
    outputs = [
        TxOutput(address=swap.deposit_address, value=178100),
        TxOutput(address=None, value=0, op_return_data=swap.payload),
        TxOutput(address="bc1qchange", value=1000),
    ]
    assert (
        verify_chainflip_vault_swap(
            outputs,
            fee=500,
            plan=plan,
            owned_addresses={"bc1qchange"},
            now=0,
            max_fee=100_000,
        )
        == []
    )


def test_prepare_vault_swap_refuses_a_deposit_address_outside_the_vault_list():
    # Belt and braces: the gate checks this too, but failing early gives a
    # better message than a gate problem list.
    rpc = _StubRpc(
        encoding={
            "chain": "Bitcoin",
            "nulldata_payload": LIVE_PAYLOAD_HEX,
            "deposit_address": "bc1qnotavault",
        }
    )
    with pytest.raises(ChainflipError, match="vault"):
        prepare_vault_swap(
            rpc, from_asset=BTC, to_asset=ETH, destination=DEST, quote=_quote(), bps=250
        )


def test_prepare_vault_swap_refuses_a_non_bitcoin_source():
    with pytest.raises(ChainflipError, match="Bitcoin"):
        prepare_vault_swap(
            _StubRpc(),
            from_asset=ETH,
            to_asset=USDC_ETH,
            destination=DEST,
            quote=_quote(),
            bps=250,
        )


def test_prepare_vault_swap_rejects_a_payload_that_is_not_hex():
    rpc = _StubRpc(
        encoding={
            "chain": "Bitcoin",
            "nulldata_payload": "not-hex",
            "deposit_address": VAULT,
        }
    )
    with pytest.raises(ChainflipError):
        prepare_vault_swap(
            rpc, from_asset=BTC, to_asset=ETH, destination=DEST, quote=_quote(), bps=250
        )


def test_prepare_vault_swap_rejects_a_payload_that_pays_elsewhere():
    # The exact attack the local decode exists for: the node encodes someone
    # else's address and reports success.
    theirs = "0x" + _build_payload(bytes.fromhex("11" * 20)).hex()
    rpc = _StubRpc(
        encoding={
            "chain": "Bitcoin",
            "nulldata_payload": theirs,
            "deposit_address": VAULT,
        }
    )
    with pytest.raises(ChainflipError, match="destination"):
        prepare_vault_swap(
            rpc, from_asset=BTC, to_asset=ETH, destination=DEST, quote=_quote(), bps=250
        )


def _gateable_plan_and_outputs():
    """The plan/outputs pair `test_prepare_vault_swap_returns_a_gateable_plan`
    proves the gate accepts, so a test below can take one output away."""
    swap = prepare_vault_swap(
        rpc=_StubRpc(),
        from_asset=BTC,
        to_asset=ETH,
        destination=DEST,
        quote=_quote(),
        bps=250,
    )
    plan = ChainflipVaultPlan(
        deposit_address=swap.deposit_address,
        amount=178100,
        payload=swap.payload,
        expiry=9_999_999_999,
        destination_asset_id=swap.destination_asset_id,
        destination_bytes=swap.destination_bytes,
        min_output_amount=swap.min_output_amount,
        known_vaults=swap.known_vaults,
    )
    outputs = [
        TxOutput(address=swap.deposit_address, value=178100),
        TxOutput(address=None, value=0, op_return_data=swap.payload),
        TxOutput(address="bc1qchange", value=1000),
    ]
    return plan, outputs


def test_a_vault_swap_without_a_change_output_is_refused():
    # The change output *is* the refund address: Chainflip pays a swap that
    # never clears its floor back to it. A selection that folds a sub-dust
    # change into the fee (coins._select does, over a ~294-sat window) would
    # otherwise sign a vault + OP_RETURN transaction with nowhere to refund to
    # — and the gate's inherited "change must be ours" loop passes vacuously
    # when there is no change output at all.
    plan, outputs = _gateable_plan_and_outputs()
    problems = verify_chainflip_vault_swap(
        outputs[:2],
        fee=1500,
        plan=plan,
        owned_addresses={"bc1qchange"},
        now=0,
        max_fee=100_000,
    )
    assert any("refund" in p for p in problems), problems


def test_a_change_output_below_dust_is_refused_too():
    # Chainflip requires the refund output above the dust limit; a dust one
    # would be unspendable by the protocol even though it exists.
    plan, outputs = _gateable_plan_and_outputs()
    outputs[2] = TxOutput(address="bc1qchange", value=100)
    problems = verify_chainflip_vault_swap(
        outputs,
        fee=1500,
        plan=plan,
        owned_addresses={"bc1qchange"},
        now=0,
        max_fee=100_000,
    )
    assert any("dust" in p for p in problems), problems


def test_the_quote_request_carries_the_pair_and_the_amount():
    # The query parameters *are* the request: a bare GET /v2/quote cannot be
    # answered, and `try_quote` turns the resulting error into "no quote" — so
    # a client that drops them takes the whole backend offline in silence.
    client = ChainflipClient("https://chainflip.invalid/v2")
    client._session = FakeSession(
        SimpleNamespace(status_code=200, json=lambda: QUOTE_PAYLOAD)
    )
    client.quote(("Bitcoin", "BTC"), ("Ethereum", "ETH"), 500_000)
    session = client._session
    assert session.gets == ["https://chainflip.invalid/v2/quote"]
    assert session.kwargs == [
        {
            "params": {
                "srcChain": "Bitcoin",
                "srcAsset": "BTC",
                "destChain": "Ethereum",
                "destAsset": "ETH",
                "amount": "500000",
            }
        }
    ]
