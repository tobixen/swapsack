"""Tests for the CoW Protocol backend (same-chain ETH-token swaps).

CoW is an intent backend: instead of paying a vault with a memo, the wallet
signs a structured EIP-712 order and posts it to the keyless orderbook API.
These tests cover quote parsing/normalization (to THORChain-style 1e8 units so
``--backend auto`` can price-compare), order building (the modern "fee folded
into sellAmount, feeAmount=0" rule), EIP-712 signing (cross-checked against a
hand-rolled digest, not just round-tripped through eth-account), the pre-sign
verify gate, and routing next to the thornode backends.
"""

import datetime

import pytest

from swapsack.backends import Backend, best_quote, gather_quotes
from swapsack.cow import (
    COW_ASSETS,
    DEFAULT_COW_TOLERANCE_BPS,
    MAX_ORDER_VALIDITY,
    SETTLEMENT_CONTRACT,
    ZERO_APP_DATA,
    CowBackend,
    CowError,
    build_order,
    order_typed_data,
    parse_cow_quote,
    sign_order,
)
from swapsack.thorchain import Quote, SwapFees
from swapsack.verify import CowOrderPlan, verify_cow_order

USDT_ASSET = "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7"
USDC_ASSET = "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
USDT_CONTRACT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
USDC_CONTRACT = "0xA0b86991c6218B36c1d19D4a2e9Eb0cE3606eB48"
RECEIVER = "0x40A50cf069e992AA4536211B23F286eF88752187"

# Recorded live from POST api.cow.fi/mainnet/api/v1/quote on 2026-07-11
# (100 USDT -> USDC). Note sellAmount + feeAmount == sellAmountBeforeFee.
QUOTE_PAYLOAD = {
    "quote": {
        "sellToken": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "buyToken": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "receiver": "0x40a50cf069e992aa4536211b23f286ef88752187",
        "sellAmount": "99912174",
        "buyAmount": "99850845",
        "validTo": 1783749448,
        "appData": ZERO_APP_DATA,
        "feeAmount": "87826",
        "kind": "sell",
        "partiallyFillable": False,
        "sellTokenBalance": "erc20",
        "buyTokenBalance": "erc20",
        "signingScheme": "eip712",
    },
    "from": "0x40a50cf069e992aa4536211b23f286ef88752187",
    "expiration": "2026-07-11T05:29:28.152309192Z",
    "id": 1239054471,
    "verified": True,
}
# 05:29:28Z that day, fractional seconds dropped.
QUOTE_EXPIRY = 1783747768


def _quote():
    return parse_cow_quote(QUOTE_PAYLOAD, to_asset=USDC_ASSET, buy_decimals=6)


# --- quote parsing / normalization ------------------------------------------


def test_parse_cow_quote_amounts():
    q = _quote()
    assert q.sell_amount == 99912174
    assert q.fee_amount == 87826
    assert q.sell_amount_total == 100_000_000  # fee folded back == before-fee
    assert q.buy_amount == 99850845
    assert q.valid_to == 1783749448
    assert q.quote_id == 1239054471
    assert q.verified is True
    assert q.sell_token == "0xdac17f958d2ee523a2206206994597c13d831ec7"
    assert q.buy_token == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    assert q.receiver == "0x40a50cf069e992aa4536211b23f286ef88752187"


def test_parse_cow_quote_normalizes_to_1e8():
    # USDC has 6 decimals; the shared best_quote comparison speaks 1e8.
    assert _quote().expected_amount_out == 9_985_084_500


def test_parse_cow_quote_expiry_epoch():
    assert _quote().expiry == QUOTE_EXPIRY
    # Sanity: recompute rather than trust the constant.
    expected = int(
        datetime.datetime(2026, 7, 11, 5, 29, 28, tzinfo=datetime.UTC).timestamp()
    )
    assert QUOTE_EXPIRY == expected


def test_parse_cow_quote_fees_denominated_in_destination():
    # The CoW fee is charged in the *sell* token; SwapFees is destination-
    # denominated (1e8), so it is converted at the quote's own marginal price:
    # 87826 * 99850845 // 99912174 = 87772 native -> 8777200 in 1e8.
    fees = _quote().fees
    assert isinstance(fees, SwapFees)
    assert fees.asset == USDC_ASSET
    assert fees.outbound == 8_777_200
    assert fees.total == 8_777_200
    assert fees.liquidity == 0
    assert fees.affiliate == 0
    assert fees.slippage_bps == 0
    assert fees.total_bps == 8  # 87826 of 100000000 input


def test_parse_cow_quote_error_payload_raises():
    with pytest.raises(CowError):
        parse_cow_quote(
            {"errorType": "NoLiquidity", "description": "no route"},
            to_asset=USDC_ASSET,
            buy_decimals=6,
        )


# --- order building -----------------------------------------------------------


def test_build_order_folds_fee_and_floors_buy_amount():
    order = build_order(_quote(), tolerance_bps=50)
    # Modern CoW rule: submitted orders carry feeAmount=0 with the fee folded
    # into sellAmount (the orderbook rejects non-zero fees).
    assert order["sellAmount"] == "100000000"
    assert order["feeAmount"] == "0"
    # buyAmount is the on-chain enforced floor: quote minus tolerance.
    assert order["buyAmount"] == str(99850845 * (10000 - 50) // 10000)
    assert order["buyAmount"] == "99351590"
    assert order["validTo"] == 1783749448
    assert order["appData"] == ZERO_APP_DATA
    assert order["kind"] == "sell"
    assert order["partiallyFillable"] is False
    assert order["sellTokenBalance"] == "erc20"
    assert order["buyTokenBalance"] == "erc20"
    assert order["sellToken"] == "0xdac17f958d2ee523a2206206994597c13d831ec7"
    assert order["buyToken"] == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    assert order["receiver"] == "0x40a50cf069e992aa4536211b23f286ef88752187"


def test_build_order_default_tolerance():
    order = build_order(_quote())
    expected = 99850845 * (10000 - DEFAULT_COW_TOLERANCE_BPS) // 10000
    assert order["buyAmount"] == str(expected)


# --- EIP-712 signing ----------------------------------------------------------


def _keccak(data: bytes) -> bytes:
    from Crypto.Hash import keccak

    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def _independent_order_digest(order: dict) -> bytes:
    """Hand-rolled EIP-712 digest — deliberately NOT using eth-account, so the
    typed-data structure sign_order builds is cross-checked against the spec."""
    domain_typehash = _keccak(
        b"EIP712Domain(string name,string version,uint256 chainId,"
        b"address verifyingContract)"
    )
    domain = _keccak(
        domain_typehash
        + _keccak(b"Gnosis Protocol")
        + _keccak(b"v2")
        + (1).to_bytes(32, "big")
        + bytes.fromhex(SETTLEMENT_CONTRACT[2:]).rjust(32, b"\0")
    )
    order_typehash = _keccak(
        b"Order(address sellToken,address buyToken,address receiver,"
        b"uint256 sellAmount,uint256 buyAmount,uint32 validTo,bytes32 appData,"
        b"uint256 feeAmount,string kind,bool partiallyFillable,"
        b"string sellTokenBalance,string buyTokenBalance)"
    )

    def addr(a: str) -> bytes:
        return bytes.fromhex(a[2:]).rjust(32, b"\0")

    def uint(n: int) -> bytes:
        return int(n).to_bytes(32, "big")

    struct = _keccak(
        order_typehash
        + addr(order["sellToken"])
        + addr(order["buyToken"])
        + addr(order["receiver"])
        + uint(int(order["sellAmount"]))
        + uint(int(order["buyAmount"]))
        + uint(order["validTo"])
        + bytes.fromhex(order["appData"][2:])
        + uint(int(order["feeAmount"]))
        + _keccak(order["kind"].encode())
        + uint(1 if order["partiallyFillable"] else 0)
        + _keccak(order["sellTokenBalance"].encode())
        + _keccak(order["buyTokenBalance"].encode())
    )
    return _keccak(b"\x19\x01" + domain + struct)


def test_order_typed_data_matches_independent_digest():
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    order = build_order(_quote())
    signable = encode_typed_data(full_message=order_typed_data(order))
    digest = _keccak(b"\x19\x01" + signable.header + signable.body)
    assert digest == _independent_order_digest(order)

    # And the signature recovers to the signing address.
    acct = Account.from_key(b"\x01" * 32)
    signature = sign_order(order, acct.key)
    assert signature.startswith("0x") and len(signature) == 2 + 65 * 2
    assert Account.recover_message(signable, signature=signature) == acct.address


# --- backend: serves / try_quote / routing ------------------------------------


class FakeCowClient:
    def __init__(self, payload=None, exc=None):
        self.calls = []
        self._payload = payload or QUOTE_PAYLOAD
        self._exc = exc

    def quote(self, sell_token, buy_token, sell_amount, *, from_address, receiver):
        self.calls.append((sell_token, buy_token, sell_amount, from_address, receiver))
        if self._exc:
            raise self._exc
        return self._payload

    def close(self):
        pass


def test_cow_assets_cover_the_eth_token_pairs():
    assert USDT_ASSET in COW_ASSETS
    assert USDC_ASSET in COW_ASSETS
    assert "ETH.ETH" in COW_ASSETS  # buy side only (native unwrap at settlement)
    assert COW_ASSETS[USDT_ASSET][1] == 6
    assert COW_ASSETS["ETH.ETH"][1] == 18


def test_serves_same_chain_token_pairs_only():
    backend = CowBackend(FakeCowClient())
    assert backend.serves(USDT_ASSET, USDC_ASSET)
    assert backend.serves(USDC_ASSET, USDT_ASSET)
    assert backend.serves(USDT_ASSET, "ETH.ETH")  # buy native ETH: supported
    assert not backend.serves("ETH.ETH", USDT_ASSET)  # sell native: needs eth-flow
    assert not backend.serves(USDT_ASSET, USDT_ASSET)
    assert not backend.serves("BTC.BTC", "ETH.ETH")
    assert not backend.serves(USDT_ASSET, "BTC.BTC")
    assert not backend.serves(
        "TRON.USDT-TR7NHQJEKQXGTCI8Q8ZY4PL8OTSZGJLJ6T", USDC_ASSET
    )


def test_try_quote_converts_1e8_to_native_and_normalizes():
    client = FakeCowClient()
    backend = CowBackend(client)
    # 100 USDT in THORChain 1e8 units -> 100e6 native (USDT has 6 decimals).
    quote = backend.try_quote(USDT_ASSET, USDC_ASSET, 10_000_000_000, RECEIVER)
    assert quote is not None
    assert quote.expected_amount_out == 9_985_084_500
    (sell_token, buy_token, sell_amount, from_address, receiver) = client.calls[0]
    assert sell_token.lower() == USDT_CONTRACT.lower()
    assert buy_token.lower() == USDC_CONTRACT.lower()
    assert sell_amount == 100_000_000
    assert receiver == RECEIVER


def test_try_quote_refuses_streaming_unserved_and_missing_destination():
    backend = CowBackend(FakeCowClient())
    ok = backend.try_quote(USDT_ASSET, USDC_ASSET, 10_000_000_000, RECEIVER)
    assert ok is not None
    assert (
        backend.try_quote(
            USDT_ASSET, USDC_ASSET, 10_000_000_000, RECEIVER, streaming_interval=1
        )
        is None
    )
    assert backend.try_quote("BTC.BTC", "ETH.ETH", 10_000_000_000, RECEIVER) is None
    assert backend.try_quote(USDT_ASSET, USDC_ASSET, 10_000_000_000, None) is None


def test_try_quote_swallows_cow_errors():
    backend = CowBackend(FakeCowClient(exc=CowError("SellAmountDoesNotCoverFee")))
    assert backend.try_quote(USDT_ASSET, USDC_ASSET, 10_000_000_000, RECEIVER) is None


def _thor_quote(out):
    return Quote(
        inbound_address="vault",
        expected_amount_out=out,
        memo="=:e:0xdest",
        fees=SwapFees("ETH.ETH", 0, 0, 0, 0, 0, 0),
        recommended_min_amount_in=1000,
        expiry=10**12,
        dust_threshold=0,
        recommended_gas_rate=1,
        gas_rate_units="x",
        router=None,
        max_streaming_quantity=1,
        streaming_swap_blocks=1,
        total_swap_seconds=1,
        raw={},
    )


class FakeThorClient:
    def __init__(self, quote):
        self._quote = quote

    def quote_swap(self, *args, **kwargs):
        return self._quote


def test_gather_quotes_mixes_thornode_and_cow_and_best_wins():
    # thornode routes USDT->USDC through two pool legs and loses; CoW's
    # 9_985_084_500 (1e8) must win the auto comparison.
    backends = [
        Backend("thorchain", FakeThorClient(_thor_quote(9_900_000_000))),
        CowBackend(FakeCowClient()),
    ]
    results = gather_quotes(backends, USDT_ASSET, USDC_ASSET, 10_000_000_000, RECEIVER)
    assert [b.name for b, _ in results] == ["thorchain", "cow"]
    backend, quote = best_quote(results)
    assert backend.name == "cow"
    assert quote.expected_amount_out == 9_985_084_500


def test_gather_quotes_skips_cow_for_unserved_pair():
    backends = [
        Backend("thorchain", FakeThorClient(_thor_quote(5_000_000))),
        CowBackend(FakeCowClient()),
    ]
    results = gather_quotes(backends, "BTC.BTC", "ETH.ETH", 178100, "0xdest")
    assert [b.name for b, _ in results] == ["thorchain"]


# --- the verify gate -----------------------------------------------------------


NOW = 1783747000  # before the recorded validTo 1783749448


def _plan(**overrides):
    kwargs = dict(
        sell_token=USDT_CONTRACT,  # checksummed on purpose: gate compares casefolded
        buy_token=USDC_CONTRACT,
        receiver=RECEIVER,
        sell_amount=100_000_000,
        min_buy_amount=99_351_590,
        expiry=QUOTE_EXPIRY,
    )
    kwargs.update(overrides)
    return CowOrderPlan(**kwargs)


def test_verify_cow_order_clean():
    order = build_order(_quote(), tolerance_bps=50)
    assert verify_cow_order(order=order, plan=_plan(), now=NOW) == []


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("sellToken", USDC_CONTRACT, "sell token"),
        ("buyToken", USDT_CONTRACT, "buy token"),
        ("receiver", "0x" + "11" * 20, "receiver"),
        ("sellAmount", "100000001", "sell amount"),
        ("buyAmount", "99351589", "buy amount"),
        ("feeAmount", "1", "fee"),
        ("kind", "buy", "kind"),
        ("partiallyFillable", True, "partial"),
        ("sellTokenBalance", "external", "balance"),
        ("buyTokenBalance", "internal", "balance"),
        ("appData", "0x" + "11" * 32, "appData"),
        ("validTo", NOW - 1, "validTo"),
        ("validTo", NOW + MAX_ORDER_VALIDITY + 10, "validTo"),
    ],
)
def test_verify_cow_order_rejects_tampering(field, value, needle):
    order = build_order(_quote(), tolerance_bps=50)
    order[field] = value
    problems = verify_cow_order(order=order, plan=_plan(), now=NOW)
    assert problems, f"tampered {field} passed the gate"
    assert any(needle in p for p in problems)


def test_verify_cow_order_rejects_zero_receiver():
    # A zero receiver means "pay the order owner" on-chain, but this wallet
    # always sets an explicit receiver — a zero here is a build bug.
    order = build_order(_quote(), tolerance_bps=50)
    order["receiver"] = "0x" + "00" * 20
    problems = verify_cow_order(
        order=order, plan=_plan(receiver="0x" + "00" * 20), now=NOW
    )
    assert any("receiver" in p for p in problems)


def test_verify_cow_order_accepts_case_insensitive_addresses():
    order = build_order(_quote(), tolerance_bps=50)
    order["sellToken"] = order["sellToken"].upper().replace("0X", "0x")
    assert verify_cow_order(order=order, plan=_plan(), now=NOW) == []
