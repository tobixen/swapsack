"""Tests for the THORChain REST client's parsing logic.

Fixtures are trimmed real responses captured from the live API; see README.
"""

import pytest

from cryptoswap_wallet.thorchain import (
    ThorchainClient,
    ThorchainError,
    normalize_txid,
    parse_inbound_addresses,
    parse_liquidity_provider,
    parse_pool_depth,
    parse_quote,
)

BTC_TO_ETH_QUOTE = {
    "inbound_address": "bc1qct4mxayrdy96d4py20l4u02mu06r667f42p9fp",
    "memo": "=:ETH.ETH:0x1111111111111111111111111111111111111111:6700000",
    "fees": {
        "asset": "ETH.ETH",
        "affiliate": "0",
        "outbound": "15820",
        "liquidity": "13590",
        "total": "29410",
        "slippage_bps": 19,
        "total_bps": 43,
    },
    "expiry": 1782589433,
    "dust_threshold": "1000",
    "recommended_min_amount_in": "7761",
    "recommended_gas_rate": "4",
    "gas_rate_units": "satsperbyte",
    "expected_amount_out": "6768430",
    "max_streaming_quantity": 1,
    "streaming_swap_blocks": 1,
    "total_swap_seconds": 606,
}

ETH_TO_TRX_QUOTE = {  # EVM source chains carry a router contract address
    "inbound_address": "0x85034887f6656d610c38ef1710208495791fb146",
    "router": "0xD37BbE5744D730a1d98d8DC97c42F0Ca46aD7146",
    "memo": "=:TRON.TRX:TXxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "fees": {
        "asset": "TRON.TRX",
        "affiliate": "0",
        "outbound": "151819000",
        "liquidity": "25451600",
        "total": "177270600",
        "slippage_bps": 20,
        "total_bps": 137,
    },
    "expiry": 1782589475,
    "dust_threshold": "1000",
    "recommended_min_amount_in": "98391",
    "recommended_gas_rate": "15",
    "gas_rate_units": "gwei",
    "expected_amount_out": "12546254700",
    "max_streaming_quantity": 1,
    "streaming_swap_blocks": 1,
    "total_swap_seconds": 30,
}

INBOUND_ADDRESSES = [
    {
        "chain": "BTC",
        "gas_rate": "3",
        "gas_rate_units": "satsperbyte",
        "outbound_fee": "1058",
        "dust_threshold": "1000",
        "halted": False,
        "global_trading_paused": False,
        "chain_trading_paused": False,
    },
    {
        "chain": "ETH",
        "gas_rate": "15",
        "gas_rate_units": "gwei",
        "outbound_fee": "15821",
        "dust_threshold": "1000",
        "halted": False,
        "global_trading_paused": False,
        "chain_trading_paused": False,
    },
    {
        "chain": "TRON",
        "gas_rate": "25387800",
        "gas_rate_units": "sun",
        "outbound_fee": "151819000",
        "dust_threshold": "10000000",
        "halted": True,
        "global_trading_paused": False,
        "chain_trading_paused": False,
    },
]


def test_parse_quote_btc_to_eth():
    q = parse_quote(BTC_TO_ETH_QUOTE)
    assert q.inbound_address == "bc1qct4mxayrdy96d4py20l4u02mu06r667f42p9fp"
    assert q.expected_amount_out == 6768430
    assert q.recommended_min_amount_in == 7761
    assert q.memo.startswith("=:ETH.ETH:")
    assert q.fees.total == 29410
    assert q.fees.total_bps == 43
    assert q.fees.slippage_bps == 19
    assert q.dust_threshold == 1000
    assert q.router is None


def test_swapfees_breakdown_itemises_slip_outbound_total():
    q = parse_quote(BTC_TO_ETH_QUOTE)
    lines = q.fees.breakdown("ETH")
    body = "\n".join(lines)
    # slip == liquidity fee, shown with its bps; outbound is the flat dest fee;
    # total carries the total_bps. No affiliate line when affiliate is 0.
    assert f"{q.fees.liquidity / 10**8:.8f} ETH" in body
    assert f"({q.fees.slippage_bps} bps)" in body
    assert f"{q.fees.outbound / 10**8:.8f} ETH" in body
    assert f"({q.fees.total_bps} bps of input)" in body
    assert not any("affiliate" in ln for ln in lines)


def test_swapfees_breakdown_shows_affiliate_when_nonzero():
    from cryptoswap_wallet.thorchain import SwapFees

    fees = SwapFees(
        asset="ETH.ETH",
        outbound=15442,
        affiliate=5000,
        liquidity=767328,
        total=787770,
        slippage_bps=20,
        total_bps=21,
    )
    assert any("affiliate" in ln for ln in fees.breakdown("ETH"))


def test_asset_unit_defaults_to_1e8_but_cacao_is_1e10():
    from cryptoswap_wallet.thorchain import THORCHAIN_UNIT, asset_unit

    assert asset_unit("BTC.BTC") == THORCHAIN_UNIT == 10**8
    assert asset_unit("ZEC.ZEC") == 10**8
    # Maya's native CACAO is the one asset that deviates: 10 decimals.
    assert asset_unit("MAYA.CACAO") == 10**10


def test_swapfees_breakdown_scales_cacao_by_1e10():
    from cryptoswap_wallet.thorchain import SwapFees

    # 4578.75867893 CACAO liquidity fee == 45787586789300 in 1e10 base units.
    fees = SwapFees(
        asset="MAYA.CACAO",
        outbound=0,
        affiliate=0,
        liquidity=45_787_586_789_300,
        total=45_787_586_789_300,
        slippage_bps=16,
        total_bps=16,
    )
    body = "\n".join(fees.breakdown("CACAO"))
    # Must divide by 1e10, not 1e8 (which would print 4_578_758.68 CACAO).
    assert "4578.75867893 CACAO" in body
    assert "4578758.6" not in body


def test_parse_quote_evm_source_has_router():
    q = parse_quote(ETH_TO_TRX_QUOTE)
    assert q.router == "0xD37BbE5744D730a1d98d8DC97c42F0Ca46aD7146"


def test_parse_quote_without_memo():
    payload = dict(BTC_TO_ETH_QUOTE)
    del payload["memo"]
    assert parse_quote(payload).memo is None


def test_parse_quote_error_raises():
    with pytest.raises(ThorchainError):
        parse_quote({"error": "swap too small; recommended minimum: 7761"})


def test_parse_inbound_addresses():
    chains = parse_inbound_addresses(INBOUND_ADDRESSES)
    assert chains["BTC"].gas_rate == 3
    assert chains["BTC"].outbound_fee == 1058
    assert chains["BTC"].tradable is True
    assert chains["TRON"].tradable is False  # halted


def test_parse_inbound_addresses_tolerates_missing_optional_fields():
    # A partial/degraded thornode entry (missing gas_rate, dust_threshold, …)
    # must not raise KeyError mid-swap-prep — it degrades to defaults, and the
    # halt/pause flags still gate tradability.
    chains = parse_inbound_addresses([{"chain": "BTC", "halted": True}])
    assert chains["BTC"].gas_rate == 0
    assert chains["BTC"].gas_rate_units == ""
    assert chains["BTC"].outbound_fee == 0
    assert chains["BTC"].dust_threshold == 0
    assert chains["BTC"].tradable is False


def test_quote_swap_default_tolerance_matches_protocol():
    # The client's default must equal the ThorchainLike protocol default, so the
    # backend selected on the default tolerance is the same one prepare_swap
    # re-quotes against.
    import inspect

    from cryptoswap_wallet.swap import DEFAULT_TOLERANCE_BPS

    default = (
        inspect.signature(ThorchainClient.quote_swap)
        .parameters["tolerance_bps"]
        .default
    )
    assert default == DEFAULT_TOLERANCE_BPS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # EVM hashes are quoted 0x-prefixed (explorers, our broadcast output),
        # but thornode/mayanode index them without the prefix.
        ("0x3a8927cc190f91d9", "3a8927cc190f91d9"),
        ("0X3A8927CC190F91D9", "3A8927CC190F91D9"),
        # already in the indexed form -> untouched.
        ("3a8927cc190f91d9", "3a8927cc190f91d9"),
        # UTXO/Cosmos txids have no 0x prefix -> no-op (no regression).
        ("ABCDEF0123456789CABBAGE", "ABCDEF0123456789CABBAGE"),
    ],
)
def test_normalize_txid_strips_evm_prefix(raw, expected):
    assert normalize_txid(raw) == expected


def test_tx_status_queries_without_0x_prefix(monkeypatch):
    """Regression: a 0x-prefixed hash must not be sent verbatim, or Maya/THOR
    return an empty 'never observed' status for an already-confirmed inbound."""
    client = ThorchainClient("https://node.example", path_prefix="mayachain")
    captured: dict[str, str] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"ok": True}

    def fake_get(url: str, **_kw: object) -> _Resp:
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(client, "_get", fake_get)
    client.tx_status("0x3a8927cc190f91d9")
    assert (
        captured["url"] == "https://node.example/mayachain/tx/status/3a8927cc190f91d9"
    )


# --- liquidity-provider parsing (for the `balance` LP report) ----------------
# Trimmed real responses from /thorchain (and /mayachain) pool/.../
# liquidity_provider/<addr>. An address with no position answers HTTP 200 with
# units "0" (not a 404), so "no position" is detected by nothing being
# redeemable, not by an error.

THOR_LP = {  # single-sided asset add that has accrued a RUNE side over time
    "asset": "BTC.BTC",
    "asset_address": "bc1qprovider",
    "units": "775667659",
    "pending_asset": "0",
    "pending_rune": "0",
    "asset_deposit_value": "180000",
    "rune_deposit_value": "0",
    "asset_redeem_value": "190000",
    "rune_redeem_value": "5000000",
}

MAYA_LP = {  # Maya names the protocol side cacao_*; this one is symmetric
    "asset": "BTC.BTC",
    "asset_address": "bc1qprovider",
    "units": "2391734936428",
    "pending_asset": "0",
    "pending_cacao": "0",
    "asset_deposit_value": "63200",
    "cacao_deposit_value": "3727448215686",
    "asset_redeem_value": "63040",
    "cacao_redeem_value": "3736696648698",
}

EMPTY_LP = {  # an address that never provided: 200 OK, everything zero
    "asset": "BTC.BTC",
    "asset_address": "bc1qnope",
    "units": "0",
    "pending_asset": "0",
    "asset_redeem_value": "0",
    "rune_redeem_value": "0",
}


def test_parse_liquidity_provider_thorchain():
    pos = parse_liquidity_provider(THOR_LP)
    assert pos is not None
    assert pos.pool == "BTC.BTC"
    assert pos.asset_address == "bc1qprovider"
    assert pos.asset_redeem_value == 190000
    assert pos.asset_deposit_value == 180000  # what was provided (cost basis)
    assert pos.pending_asset == 0
    assert pos.protocol_redeem_value == 5000000  # rune side


def test_parse_liquidity_provider_maya_uses_cacao_field():
    pos = parse_liquidity_provider(MAYA_LP)
    assert pos is not None
    assert pos.asset_redeem_value == 63040
    assert pos.protocol_redeem_value == 3736696648698  # from cacao_redeem_value
    assert pos.protocol_deposit_value == 3727448215686  # from cacao_deposit_value


def test_liquidity_position_format_deposit_is_asset_equiv_not_raw_cacao():
    # The protocol-side deposit (cacao) must never surface as a raw "+ N CACAO"
    # the user can't add to ETH; it's converted to asset and folded in. With
    # price 2e-8: deposited = 63200 + 3727448215686*2e-8 = 137748.96 -> 0.00137749.
    line = parse_liquidity_provider(MAYA_LP).format(
        "maya", protocol="CACAO", protocol_price_in_asset=2e-8
    )
    assert "deposited ~0.00137749" in line
    assert "CACAO" in line  # appears only as the in-asset breakdown ("via CACAO")
    assert line.count("CACAO") == 1
    assert "via CACAO" in line


def test_parse_liquidity_provider_no_position_is_none():
    assert parse_liquidity_provider(EMPTY_LP) is None


def test_parse_liquidity_provider_error_is_none():
    assert parse_liquidity_provider({"error": "pool does not exist"}) is None


def test_parse_liquidity_provider_dead_units_is_none():
    # Withdrawn position can linger with units but nothing redeemable -> skip it.
    payload = {**EMPTY_LP, "units": "123"}
    assert parse_liquidity_provider(payload) is None


def test_parse_liquidity_provider_pending_only_is_reported():
    payload = {**EMPTY_LP, "pending_asset": "12345"}
    pos = parse_liquidity_provider(payload)
    assert pos is not None
    assert pos.pending_asset == 12345


def test_liquidity_position_format_without_price_flags_uncounted_side():
    # No pool price available -> fall back to asset side only, but say so rather
    # than silently dropping the protocol side.
    line = parse_liquidity_provider(THOR_LP).format("thorchain")
    assert "thorchain BTC.BTC" in line
    assert "0.00190000" in line  # asset_redeem_value / 1e8
    assert "RUNE" in line and "not counted" in line


def test_liquidity_position_format_with_price_shows_total_value():
    # protocol_price_in_asset = asset per 1 protocol unit. The RUNE side
    # (5_000_000) is worth 5_000_000 * 0.01 = 50_000 -> total 240_000 (0.0024).
    # The CACAO/RUNE side is converted to asset *before* summing, so the total
    # is a single clean asset figure (you can't add raw RUNE to BTC).
    line = parse_liquidity_provider(THOR_LP).format(
        "thorchain", protocol_price_in_asset=0.01
    )
    assert "0.00240000 redeemable" in line  # asset + RUNE side, in BTC
    assert "0.00190000" in line  # asset-side breakdown
    assert "0.00050000" in line  # RUNE side valued in BTC
    assert "not counted" not in line  # it IS counted now
    # cost basis as one asset-equiv figure (asset_deposit 180000, no rune leg)
    assert "deposited ~0.00180000" in line


def test_liquidity_position_format_maya_labels_cacao_and_pending():
    payload = {**MAYA_LP, "pending_asset": "1000000"}
    line = parse_liquidity_provider(payload).format("maya", protocol="CACAO")
    assert "maya BTC.BTC" in line
    assert "0.01000000 pending" in line
    assert "CACAO" in line


def test_liquidity_provider_client_builds_url(monkeypatch):
    client = ThorchainClient("https://node.example", path_prefix="mayachain")
    captured: dict[str, str] = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return EMPTY_LP

    def fake_get(url: str, **_kw: object) -> _Resp:
        captured["url"] = url
        return _Resp()

    monkeypatch.setattr(client, "_get", fake_get)
    assert client.liquidity_provider("BTC.BTC", "bc1qnope") is None
    assert captured["url"] == (
        "https://node.example/mayachain/pool/BTC.BTC/liquidity_provider/bc1qnope"
    )


def test_parse_pool_depth_thorchain():
    depth = parse_pool_depth(
        {"asset": "BTC.BTC", "balance_asset": "200", "balance_rune": "1000"}
    )
    assert depth.balance_asset == 200
    assert depth.balance_protocol == 1000
    assert depth.asset_per_protocol == 0.2  # asset per 1 RUNE/CACAO


def test_parse_pool_depth_maya_uses_cacao_balance():
    depth = parse_pool_depth(
        {"asset": "BTC.BTC", "balance_asset": "50", "balance_cacao": "100"}
    )
    assert depth.balance_protocol == 100


def test_pool_depth_empty_pool_has_zero_price():
    depth = parse_pool_depth({"balance_asset": "0", "balance_rune": "0"})
    assert depth.asset_per_protocol == 0.0
