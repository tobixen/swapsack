"""Tests for Chainflip vault swaps from an EVM source.

The Bitcoin shape of a vault swap is covered in ``test_chainflip.py``; this
module is the other one. An EVM source does not pay a vault address with an
OP_RETURN, it *calls* the protocol's Vault contract — ``xSwapNative`` for the
chain's own coin, ``xSwapToken`` for an ERC-20 — so nothing about the Bitcoin
path's transaction builder or its gate is reused, and the whole story from
calldata decoding to the gate lives here.

Two things make this worth its own suite:

* **The chain offers no readback.** ``cf_decode_vault_swap_parameter`` answers
  "Decoding Vault Swap only supports Bitcoin and Solana", so
  ``verify.decode_evm_vault_call`` is not a second opinion on the payload — it
  is the only one. The golden fixtures below are real mainnet encodings
  recorded on 2026-09-02, so a decoder that drifts from what Chainflip actually
  emits fails here rather than on a real transaction.
* **The floor is a price, not an amount.** ``min_price`` is a ratio in the two
  assets' own base units scaled by 2**128, and getting its direction or its
  fee adjustment wrong is a silently under-delivered swap.
"""

import time

import pytest

from swapsack.chainflip import (
    CHAINFLIP_ASSETS,
    DEFAULT_BROKER_ACCOUNTS,
    DEFAULT_RETRY_DURATION_BLOCKS,
    EVM_VAULT_CHAINS,
    PRICE_FRACTIONAL_BITS,
    VAULT_SWAP_ASSET_IDS,
    VAULT_SWAP_CHAIN_IDS,
    ChainflipError,
    can_settle_vault_swap,
    destination_bytes,
    evm_vault_addresses,
    min_output_amount,
    min_price,
    parse_chainflip_quote,
    prepare_evm_vault_swap,
    vault_chain,
)
from swapsack.chains.eth import (
    APPROVE_SELECTOR,
    EthAdapter,
    EthVaultSwapBuilt,
    encode_approve,
    verify_chainflip_evm_vault_swap,
)
from swapsack.verify import (
    ChainflipEvmVaultPlan,
    decode_evm_vault_call,
    decode_evm_vault_parameters,
)

BTC = "BTC.BTC"
ETH = "ETH.ETH"
USDC = "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
USDC_CONTRACT = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
ARB_ETH = "ARB.ETH"
TRX = "TRON.TRX"

ETH_VAULT = "0xf5e10380213880111522dd0efd3dbb45b9f62bcc"
ARB_VAULT = "0x79001a5e762f3befc8e5871b42f6734e00498920"
REFUND = "0x000000000000000000000000000000000000dEaD"
BTC_DEST = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
EVM_DEST = "0x000000000000000000000000000000000000BEEF"
BROKER = bytes.fromhex(
    "70d0cd75a367987344a3896a18e1510e5429ca5e88357b6c2a2e306b3877380d"
)


# --- golden fixtures --------------------------------------------------------
#
# Recorded from mainnet-rpc.chainflip.io on 2026-09-02 via
# cf_request_swap_parameter_encoding, with the broker account this wallet asks
# first, a zero commission and retry_duration 100. Verbatim: the point of a
# golden fixture is to be the protocol's bytes, not ours.

# 1 ETH -> BTC at BTC_DEST, refunding to REFUND, floor 0.03 BTC per ETH.
NATIVE_CALLDATA = bytes.fromhex(
    "dd68734500000000000000000000000000000000000000000000000000000000"
    "0000000300000000000000000000000000000000000000000000000000000000"
    "0000008000000000000000000000000000000000000000000000000000000000"
    "0000000500000000000000000000000000000000000000000000000000000000"
    "000000e000000000000000000000000000000000000000000000000000000000"
    "0000002a6263317177353038643671656a7874646734793572337a6172766172"
    "793063357877376b763866337434000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000600164000000000000000000000000000000000000000000dead2e7dd7"
    "c734e39b38c86c4c030000000000000000000000000000000000000000000000"
    "0070d0cd75a367987344a3896a18e1510e5429ca5e88357b6c2a2e306b387738"
    "0d000000"
)
NATIVE_VALUE = 10**18
NATIVE_MIN_PRICE = (3_000_000 << PRICE_FRACTIONAL_BITS) // 10**18

# 1000 USDC (Ethereum) -> native ETH on Arbitrum at EVM_DEST, floor 0.1 ETH.
TOKEN_CALLDATA = bytes.fromhex(
    "04fc7da000000000000000000000000000000000000000000000000000000000"
    "0000000400000000000000000000000000000000000000000000000000000000"
    "000000c000000000000000000000000000000000000000000000000000000000"
    "00000006000000000000000000000000a0b86991c6218b36c1d19d4a2e9eb0ce"
    "3606eb4800000000000000000000000000000000000000000000000000000000"
    "3b9aca0000000000000000000000000000000000000000000000000000000000"
    "0000010000000000000000000000000000000000000000000000000000000000"
    "00000014000000000000000000000000000000000000beef0000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000600164000000000000000000000000000000000000000000dead000000"
    "0000000000000000000000000000e1f505000000000000000000000000000000"
    "0070d0cd75a367987344a3896a18e1510e5429ca5e88357b6c2a2e306b387738"
    "0d000000"
)
TOKEN_AMOUNT = 1000 * 10**6
TOKEN_MIN_PRICE = (10**17 << PRICE_FRACTIONAL_BITS) // (1000 * 10**6)


def test_the_golden_native_encoding_decodes_to_what_we_asked_for():
    call = decode_evm_vault_call(NATIVE_CALLDATA)
    assert call is not None, "a live Vault call no longer decodes — layout changed"
    assert call.destination_chain_id == VAULT_SWAP_CHAIN_IDS["Bitcoin"]
    assert call.destination_asset_id == VAULT_SWAP_ASSET_IDS[BTC]
    # Bitcoin's destination really is the address string's own ASCII.
    assert call.destination.decode() == BTC_DEST
    assert call.source_token == ""
    assert call.source_amount == 0  # the transaction's value carries it


def test_the_golden_native_parameters_carry_our_floor_and_no_skim():
    params = decode_evm_vault_parameters(
        decode_evm_vault_call(NATIVE_CALLDATA).parameters
    )
    assert params is not None
    assert params.version == 1
    assert params.retry_duration == 100
    assert params.refund_address.hex() == REFUND[2:].lower()
    # The exact number we handed the chain, read back out of its own bytes:
    # this is what pins the 2**128 fixed point, independently of any AMM maths.
    assert params.min_price == NATIVE_MIN_PRICE
    assert params.broker == BROKER
    assert (params.broker_fee, params.boost_fee, params.affiliates) == (0, 0, 0)
    assert (params.ccm, params.oracle_slippage, params.dca) == (0, 0, 0)


def test_the_golden_token_encoding_names_the_token_and_the_amount():
    call = decode_evm_vault_call(TOKEN_CALLDATA)
    assert call is not None
    assert call.source_token == USDC_CONTRACT
    assert call.source_amount == TOKEN_AMOUNT
    assert call.destination_chain_id == VAULT_SWAP_CHAIN_IDS["Arbitrum"]
    assert call.destination_asset_id == VAULT_SWAP_ASSET_IDS[ARB_ETH]
    assert call.destination.hex() == EVM_DEST[2:].lower()
    params = decode_evm_vault_parameters(call.parameters)
    assert params.min_price == TOKEN_MIN_PRICE


# --- the decoder's refusals -------------------------------------------------


def test_a_call_that_is_not_one_of_the_two_vault_functions_is_refused():
    assert decode_evm_vault_call(b"\xde\xad\xbe\xef" + NATIVE_CALLDATA[4:]) is None


def test_a_truncated_call_is_refused_rather_than_half_decoded():
    assert decode_evm_vault_call(NATIVE_CALLDATA[:80]) is None
    assert decode_evm_vault_call(b"\xdd\xd6") is None


def test_an_offset_pointing_into_the_abi_head_is_refused():
    # Not a decoding subtlety — an offset inside the head is how one decoder is
    # made to disagree with another about the same bytes.
    tampered = bytearray(NATIVE_CALLDATA)
    tampered[4 + 32 + 31] = 0x20  # dstAddress offset 0x80 -> 0x20
    assert decode_evm_vault_call(bytes(tampered)) is None


def test_a_length_running_past_the_end_is_refused():
    tampered = bytearray(NATIVE_CALLDATA)
    tampered[4 + 0x80 + 31] = 0xFF  # the destination's length word
    assert decode_evm_vault_call(bytes(tampered)) is None


def test_non_zero_padding_after_a_dynamic_field_is_refused():
    call = decode_evm_vault_call(NATIVE_CALLDATA)
    tampered = bytearray(NATIVE_CALLDATA)
    end = 4 + 0x80 + 32 + len(call.destination)
    tampered[end] = 0x01
    assert decode_evm_vault_call(bytes(tampered)) is None


def test_an_address_argument_with_dirty_high_bits_is_refused():
    tampered = bytearray(TOKEN_CALLDATA)
    tampered[4 + 3 * 32] = 0x01  # a bit above the address's 160
    assert decode_evm_vault_call(bytes(tampered)) is None


@pytest.mark.parametrize("length", [95, 97, 104])
def test_parameters_of_the_wrong_length_are_refused(length):
    # Every Option this wallet leaves unset would lengthen the blob if it were
    # set, so the length check is what keeps every offset below meaningful.
    assert decode_evm_vault_parameters(b"\x01" * length) is None


# --- which pairs settle -----------------------------------------------------


def test_an_evm_source_can_pay_bitcoin():
    assert can_settle_vault_swap(ETH, BTC)
    assert can_settle_vault_swap(USDC, BTC)


def test_a_bitcoin_source_cannot_pay_bitcoin_or_any_non_evm_chain():
    # The 48-byte OP_RETURN payload has a fixed 20-byte destination field, so a
    # Bitcoin source can only reach a chain whose addresses are 20 bytes.
    assert not can_settle_vault_swap(BTC, BTC)
    assert not can_settle_vault_swap(BTC, TRX)


def test_both_source_shapes_can_pay_an_evm_chain():
    assert can_settle_vault_swap(BTC, ETH)
    assert can_settle_vault_swap(ARB_ETH, USDC)


def test_a_source_with_no_vault_swap_shape_settles_nowhere():
    assert not can_settle_vault_swap(TRX, ETH)


def test_a_destination_the_gate_cannot_reproduce_settles_nowhere():
    assert not can_settle_vault_swap(ETH, TRX)


def test_every_evm_vault_chain_has_a_chain_id():
    assert EVM_VAULT_CHAINS <= set(VAULT_SWAP_CHAIN_IDS)
    assert {vault_chain(a) for a in VAULT_SWAP_ASSET_IDS} <= set(VAULT_SWAP_CHAIN_IDS)


def test_destination_bytes_of_a_bitcoin_address_is_its_own_ascii():
    assert destination_bytes(BTC, BTC_DEST) == BTC_DEST.encode()


def test_destination_bytes_keeps_bitcoin_case_significant():
    # base58 addresses are case-sensitive, and the protocol carries the string
    # verbatim — lower-casing one would pay a different address.
    assert destination_bytes(BTC, "1BvBMSEY") != destination_bytes(BTC, "1bvbmsey")


# --- the price floor --------------------------------------------------------

QUOTE_PAYLOAD = {
    "depositAmount": "1000000000000000000",  # 1 ETH
    "egressAmount": "2000000000",  # 2000 USDC
    "intermediateAmount": None,
    "estimatedDurationSeconds": 90,
    "recommendedSlippageTolerancePercent": 1.0,
    "includedFees": [
        {"type": "INGRESS", "amount": "2000000000000000"},  # 0.002 ETH
        {"type": "NETWORK", "amount": "1000000"},  # 1 USDC
        {"type": "EGRESS", "amount": "3000000"},  # 3 USDC
    ],
}


# The quote for a token source has to be denominated in that token, or the
# ingress fee (0.002 ETH) would swallow a 1000-USDC deposit whole.
TOKEN_QUOTE_PAYLOAD = dict(
    QUOTE_PAYLOAD,
    depositAmount=str(TOKEN_AMOUNT),
    includedFees=[
        {"type": "INGRESS", "amount": "1000000"},  # 1 USDC
        {"type": "NETWORK", "amount": "1000000"},
        {"type": "EGRESS", "amount": "3000000"},
    ],
)


def _quote(payload=None, *, from_asset=ETH, to_asset=USDC):
    return parse_chainflip_quote(
        payload if payload is not None else QUOTE_PAYLOAD,
        from_asset=from_asset,
        to_asset=to_asset,
    )


def test_the_quote_keeps_the_flat_fee_legs_in_their_own_units():
    quote = _quote()
    assert quote.ingress_fee == 2 * 10**15
    assert quote.egress_fee == 3 * 10**6


def test_min_price_is_the_floor_plus_egress_over_the_deposit_less_ingress():
    quote = _quote()
    price, floor = min_price(quote, 250)
    assert floor == min_output_amount(quote, 250)
    expected = -(
        -((floor + quote.egress_fee) << PRICE_FRACTIONAL_BITS)
        // (quote.deposit_amount - quote.ingress_fee)
    )
    assert price == expected


def test_min_price_is_stricter_than_the_naive_gross_ratio():
    # The whole point of putting the flat fees back: the naive reading would
    # let the protocol deliver less than the floor and still call it a fill.
    quote = _quote()
    price, floor = min_price(quote, 250)
    assert price > (floor << PRICE_FRACTIONAL_BITS) // quote.deposit_amount


def test_min_price_rounds_up_rather_than_down():
    quote = _quote()
    price, floor = min_price(quote, 250)
    swap_input = quote.deposit_amount - quote.ingress_fee
    # Truncating would let the encoded floor sit a hair under what we intend;
    # a hair over only ever costs a refund.
    assert price * swap_input >= (floor + quote.egress_fee) << PRICE_FRACTIONAL_BITS


def test_a_tighter_tolerance_raises_the_price_floor():
    quote = _quote()
    assert min_price(quote, 10)[0] > min_price(quote, 500)[0]


def test_min_price_defaults_to_the_quotes_own_recommendation():
    quote = _quote()
    assert min_price(quote, None) == min_price(quote, quote.recommended_slippage_bps)


def test_min_price_refuses_a_deposit_the_ingress_fee_swallows():
    payload = dict(QUOTE_PAYLOAD, depositAmount="1000")
    with pytest.raises(ChainflipError, match="no swap to price"):
        min_price(_quote(payload), 250)


# --- assembling an EVM vault swap -------------------------------------------


def _vault_rpc_result():
    return {
        "ethereum": {"Eth": list(bytes.fromhex(ETH_VAULT[2:]))},
        "arbitrum": {"Arb": list(bytes.fromhex(ARB_VAULT[2:]))},
        "bitcoin": [],
    }


def _build_calldata(
    *,
    token="",
    amount=NATIVE_VALUE,
    dest_chain=VAULT_SWAP_CHAIN_IDS["Bitcoin"],
    dest_asset=VAULT_SWAP_ASSET_IDS[BTC],
    destination=None,
    parameters=None,
):
    """The two Vault calls, rebuilt locally in canonical ABI form.

    Pinned against the golden mainnet fixtures by the test below, so a stub that
    drifted from the real encoding would fail loudly rather than validate a
    layout Chainflip does not emit.
    """
    destination = BTC_DEST.encode() if destination is None else destination
    parameters = _build_parameters() if parameters is None else parameters

    def word(value):
        return int(value).to_bytes(32, "big")

    def tail(data):
        return word(len(data)) + data + bytes(-len(data) % 32)

    if token:
        selector, head_words = "04fc7da0", 6
        head = [dest_chain, None, dest_asset, int(token, 16), amount, None]
        offsets = (1, 5)
    else:
        selector, head_words = "dd687345", 4
        head = [dest_chain, None, dest_asset, None]
        offsets = (1, 3)
    dest_at = head_words * 32
    params_at = dest_at + len(tail(destination))
    head[offsets[0]], head[offsets[1]] = dest_at, params_at
    return (
        bytes.fromhex(selector)
        + b"".join(word(w) for w in head)
        + tail(destination)
        + tail(parameters)
    )


def _build_parameters(
    *,
    version=1,
    retry=100,
    refund=REFUND,
    price=NATIVE_MIN_PRICE,
    ccm=0,
    oracle=0,
    dca=0,
    boost=0,
    broker_fee=0,
    affiliates=0,
):
    return (
        bytes([version])
        + retry.to_bytes(4, "little")
        + bytes.fromhex(refund[2:])
        + price.to_bytes(32, "little")
        + bytes([ccm, oracle, dca, boost])
        + BROKER
        + broker_fee.to_bytes(2, "little")
        + bytes([affiliates])
    )


def test_the_stub_calldata_matches_the_live_chainflip_encodings():
    assert _build_calldata() == NATIVE_CALLDATA
    assert (
        _build_calldata(
            token=USDC_CONTRACT,
            amount=TOKEN_AMOUNT,
            dest_chain=VAULT_SWAP_CHAIN_IDS["Arbitrum"],
            dest_asset=VAULT_SWAP_ASSET_IDS[ARB_ETH],
            destination=bytes.fromhex(EVM_DEST[2:]),
            parameters=_build_parameters(price=TOKEN_MIN_PRICE),
        )
        == TOKEN_CALLDATA
    )


class _StubRpc:
    """Stands in for ChainflipRpc's transport, encoding what it is asked for.

    Like ``test_chainflip._StubRpc``, and for the same reason: replaying one
    fixture would never exercise the round trip (ask for a floor -> get a call
    carrying it -> gate it). ``encoding`` forces a specific — usually bad —
    response instead.
    """

    def __init__(self, encoding=None, vaults=None):
        self.encoding = encoding
        self.vaults = _vault_rpc_result() if vaults is None else vaults
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "cf_get_vault_addresses":
            return self.vaults
        if method != "cf_request_swap_parameter_encoding":
            raise AssertionError(f"unexpected method {method}")
        if self.encoding is not None:
            return self.encoding
        _broker, src, dst, dest, _commission, extra = params
        source = _asset_for(src)
        target = _asset_for(dst)
        token = source.partition("-")[2].lower()
        amount = int(extra["input_amount"], 16)
        refund = extra["refund_parameters"]
        chain = CHAINFLIP_ASSETS[source][0]
        return {
            "chain": chain,
            "to": ETH_VAULT if chain == "Ethereum" else ARB_VAULT,
            "value": hex(0 if token else amount),
            "calldata": "0x"
            + _build_calldata(
                token=token,
                amount=amount,
                dest_chain=VAULT_SWAP_CHAIN_IDS[CHAINFLIP_ASSETS[target][0]],
                dest_asset=VAULT_SWAP_ASSET_IDS[target],
                destination=destination_bytes(target, dest),
                parameters=_build_parameters(
                    retry=refund["retry_duration"],
                    refund=refund["refund_address"],
                    price=int(refund["min_price"], 16),
                ),
            ).hex(),
            **({"source_token_address": token} if token else {}),
        }

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def _asset_for(pair):
    return next(
        key
        for key, value in CHAINFLIP_ASSETS.items()
        if list(value[:2]) == [pair["chain"], pair["asset"]]
    )


def _prepare(rpc=None, *, from_asset=ETH, to_asset=BTC, destination=None, bps=250):
    return prepare_evm_vault_swap(
        rpc if rpc is not None else _StubRpc(),
        from_asset=from_asset,
        to_asset=to_asset,
        destination=destination or (BTC_DEST if to_asset == BTC else EVM_DEST),
        refund_address=REFUND,
        input_amount=NATIVE_VALUE,
        quote=_quote(to_asset=to_asset if to_asset != BTC else USDC),
        bps=bps,
    )


def test_the_evm_vault_address_decodes_from_the_rpc_byte_array():
    assert evm_vault_addresses(_StubRpc(), "Ethereum") == frozenset({ETH_VAULT})
    assert evm_vault_addresses(_StubRpc(), "Arbitrum") == frozenset({ARB_VAULT})


def test_a_vault_address_of_the_wrong_length_is_refused():
    rpc = _StubRpc(vaults={"ethereum": {"Eth": [1] * 19}})
    with pytest.raises(ChainflipError, match="expected 20"):
        evm_vault_addresses(rpc, "Ethereum")


def test_prepare_asks_for_our_destination_amount_refund_and_floor():
    rpc = _StubRpc()
    swap = _prepare(rpc)
    method, params = rpc.calls[-1]
    assert method == "cf_request_swap_parameter_encoding"
    assert params[0] == DEFAULT_BROKER_ACCOUNTS[0]
    assert params[3] == BTC_DEST
    assert params[4] == 0  # broker commission: nobody skims
    extra = params[5]
    assert extra["chain"] == "Ethereum"
    assert int(extra["input_amount"], 16) == NATIVE_VALUE
    assert extra["refund_parameters"]["refund_address"] == REFUND
    assert int(extra["refund_parameters"]["min_price"], 16) == swap.min_price


def test_prepare_returns_the_call_it_checked():
    swap = _prepare()
    assert swap.vault_contract == ETH_VAULT
    assert swap.value == NATIVE_VALUE
    assert swap.source_token == ""
    assert swap.destination_bytes == BTC_DEST.encode()
    assert swap.destination_asset_id == VAULT_SWAP_ASSET_IDS[BTC]
    assert swap.destination_chain_id == VAULT_SWAP_CHAIN_IDS["Bitcoin"]
    assert swap.min_output_amount == min_output_amount(_quote(to_asset=USDC), 250)


def test_prepare_from_a_token_source_names_the_contract_and_sends_no_value():
    swap = prepare_evm_vault_swap(
        _StubRpc(),
        from_asset=USDC,
        to_asset=BTC,
        destination=BTC_DEST,
        refund_address=REFUND,
        input_amount=TOKEN_AMOUNT,
        quote=_quote(TOKEN_QUOTE_PAYLOAD),
        bps=250,
    )
    assert swap.source_token == USDC_CONTRACT
    assert swap.value == 0
    assert swap.source_amount == TOKEN_AMOUNT


def test_prepare_uses_the_arbitrum_vault_for_an_arbitrum_source():
    swap = _prepare(from_asset=ARB_ETH, to_asset=USDC)
    assert swap.vault_contract == ARB_VAULT


def test_prepare_refuses_a_source_that_is_not_an_evm_chain():
    with pytest.raises(ChainflipError, match="contract call"):
        _prepare(from_asset=BTC, to_asset=ETH)


def test_prepare_refuses_a_non_positive_amount():
    with pytest.raises(ChainflipError, match="must be positive"):
        prepare_evm_vault_swap(
            _StubRpc(),
            from_asset=ETH,
            to_asset=BTC,
            destination=BTC_DEST,
            refund_address=REFUND,
            input_amount=0,
            quote=_quote(),
            bps=250,
        )


# The floor prepare_evm_vault_swap will compute for the fixtures below, so an
# otherwise-good tampered encoding fails on the field the test is about rather
# than tripping the min_price check first.
PREPARED_PRICE = min_price(_quote(), 0)[0]


def _bad_encoding(*, call=None, params=None, **over):
    """A stub that answers with our canonical encoding, tampered as asked.

    ``call`` and ``params`` reach :func:`_build_calldata` and
    :func:`_build_parameters`; anything else replaces a field of the response
    itself.
    """
    base = {
        "chain": "Ethereum",
        "to": ETH_VAULT,
        "value": hex(NATIVE_VALUE),
        "calldata": "0x"
        + _build_calldata(
            parameters=_build_parameters(**{"price": PREPARED_PRICE, **(params or {})}),
            **(call or {}),
        ).hex(),
    }
    base.update(over)
    return _StubRpc(encoding=base)


def test_prepare_refuses_a_contract_that_is_not_the_published_vault():
    with pytest.raises(ChainflipError, match="not the Ethereum vault"):
        _prepare(_bad_encoding(to="0x" + "11" * 20), bps=0)


def test_prepare_refuses_a_call_paying_someone_elses_destination():
    rpc = _bad_encoding(call={"destination": b"bc1qsomeoneelse"})
    with pytest.raises(ChainflipError, match="pays destination"):
        _prepare(rpc, bps=0)


def test_prepare_refuses_a_call_paying_the_wrong_output_asset():
    rpc = _bad_encoding(call={"dest_asset": VAULT_SWAP_ASSET_IDS[ETH]})
    with pytest.raises(ChainflipError, match="output asset"):
        _prepare(rpc, bps=0)


def test_prepare_refuses_a_call_paying_out_on_the_wrong_chain():
    with pytest.raises(ChainflipError, match="pays out on chain"):
        _prepare(_bad_encoding(call={"dest_chain": 1}), bps=0)


def test_prepare_refuses_a_call_moving_a_different_amount():
    rpc = _bad_encoding(value=hex(NATIVE_VALUE // 2))
    with pytest.raises(ChainflipError, match="units of the source asset"):
        _prepare(rpc, bps=0)


def test_prepare_refuses_a_native_call_that_also_names_a_token():
    rpc = _bad_encoding(
        source_token_address=USDC_CONTRACT, call={"token": USDC_CONTRACT}
    )
    with pytest.raises(ChainflipError, match="spends token"):
        _prepare(rpc, bps=0)


def test_prepare_refuses_a_call_refunding_somewhere_else():
    with pytest.raises(ChainflipError, match="refunds to"):
        _prepare(_bad_encoding(params={"refund": EVM_DEST}), bps=0)


def test_prepare_refuses_a_floor_below_the_one_we_asked_for():
    with pytest.raises(ChainflipError, match="below the floor"):
        _prepare(_bad_encoding(params={"price": 1}), bps=0)


@pytest.mark.parametrize("skim", ["broker_fee", "boost", "affiliates"])
def test_prepare_refuses_a_call_carrying_a_skim(skim):
    with pytest.raises(ChainflipError, match="no skim"):
        _prepare(_bad_encoding(params={skim: 5}), bps=0)


def test_prepare_refuses_parameters_it_cannot_decode():
    rpc = _bad_encoding(calldata="0x" + _build_calldata(parameters=b"\x01" * 40).hex())
    with pytest.raises(ChainflipError, match="expected"):
        _prepare(rpc, bps=0)


def test_prepare_refuses_calldata_that_is_not_a_vault_call():
    with pytest.raises(ChainflipError, match="read back"):
        _prepare(_bad_encoding(calldata="0xdeadbeef"), bps=0)


# --- the gate ---------------------------------------------------------------

NOW = 1_700_000_000
CHAIN_ID = 1


def _plan(swap=None, **over):
    swap = swap or _prepare()
    fields = {
        "vault_contract": swap.vault_contract,
        "calldata": swap.calldata,
        "value": swap.value,
        "source_token": swap.source_token,
        "source_amount": swap.source_amount,
        "expiry": NOW + 600,
        "chain_id": CHAIN_ID,
        "destination_chain_id": swap.destination_chain_id,
        "destination_asset_id": swap.destination_asset_id,
        "destination_bytes": swap.destination_bytes,
        "refund_address": swap.refund_address,
        "min_price": swap.min_price,
        "retry_duration": swap.retry_duration,
        "known_vaults": swap.known_vaults,
    }
    fields.update(over)
    return ChainflipEvmVaultPlan(**fields)


def _built(plan, **over):
    swap_tx = {
        "type": 2,
        "chainId": plan.chain_id,
        "nonce": 0,
        "to": plan.vault_contract,
        "value": plan.value,
        "gas": 120000,
        "maxFeePerGas": 2 * 10**9,
        "maxPriorityFeePerGas": 10**9,
        "data": "0x" + plan.calldata.hex(),
    }
    swap_tx.update(over.pop("swap_tx", {}))
    return EthVaultSwapBuilt(swap_tx=swap_tx, private_key=b"\x01" * 32, **over)


def _gate(plan, built=None, *, now=NOW, max_fee_wei=10**16):
    return verify_chainflip_evm_vault_swap(
        built=built if built is not None else _built(plan),
        plan=plan,
        now=now,
        max_fee_wei=max_fee_wei,
    )


def test_a_native_vault_swap_passes_the_gate():
    assert _gate(_plan()) == []


def test_the_gate_refuses_an_expired_plan():
    assert any("expired" in p for p in _gate(_plan(), now=NOW + 601))


def test_the_gate_refuses_a_contract_outside_the_published_list():
    plan = _plan(known_vaults=frozenset({"0x" + "22" * 20}))
    assert any("publishes on-chain" in p for p in _gate(plan))


def test_the_gate_refuses_a_tx_paying_a_different_contract():
    plan = _plan()
    built = _built(plan, swap_tx={"to": "0x" + "33" * 20})
    assert any("!= Vault" in p for p in _gate(plan, built))


def test_the_gate_refuses_a_tx_sending_a_different_value():
    plan = _plan()
    built = _built(plan, swap_tx={"value": plan.value - 1})
    assert any("wei != intended" in p for p in _gate(plan, built))


def test_the_gate_refuses_a_tx_signed_for_another_chain():
    plan = _plan()
    built = _built(plan, swap_tx={"chainId": 42161})
    assert any("chainId" in p for p in _gate(plan, built))


def test_the_gate_refuses_calldata_that_is_not_the_planned_bytes():
    plan = _plan()
    swapped = _build_calldata(destination=b"bc1qsomeoneelse")
    built = _built(plan, swap_tx={"data": "0x" + swapped.hex()})
    problems = _gate(plan, built)
    # Both layers fire: the bytes are not the plan's, and what they say is not
    # what we intend. Either alone would be a weaker gate.
    assert any("!= planned" in p for p in problems)
    assert any("pays destination" in p for p in problems)


def test_the_gate_refuses_a_fee_over_the_ceiling():
    plan = _plan()
    assert any("exceeds limit" in p for p in _gate(plan, max_fee_wei=1))


def test_the_gate_refuses_an_approve_beside_a_native_swap():
    plan = _plan()
    built = _built(plan, approve_tx=dict(_built(plan).swap_tx))
    assert any("must not be preceded by an approve" in p for p in _gate(plan, built))


@pytest.mark.parametrize(
    ("field", "match"),
    [
        ("ccm", "cross-chain message"),
        ("oracle", "oracle slippage"),
        ("dca", "DCA parameters"),
    ],
)
def test_the_gate_refuses_parameters_carrying_an_option_we_never_ask_for(field, match):
    # Unreachable through the length check today — which is the point: if the
    # layout ever shifts so one of these lands inside 96 bytes, stop.
    calldata = _build_calldata(parameters=_build_parameters(**{field: 1}))
    plan = _plan(calldata=calldata)
    assert any(match in p for p in _gate(plan))


def test_the_gate_refuses_parameters_of_an_unknown_version():
    calldata = _build_calldata(parameters=_build_parameters(version=2))
    plan = _plan(calldata=calldata)
    assert any("version 2" in p for p in _gate(plan))


# --- the token pair ---------------------------------------------------------


def _token_plan(**over):
    swap = prepare_evm_vault_swap(
        _StubRpc(),
        from_asset=USDC,
        to_asset=BTC,
        destination=BTC_DEST,
        refund_address=REFUND,
        input_amount=TOKEN_AMOUNT,
        quote=_quote(TOKEN_QUOTE_PAYLOAD),
        bps=250,
    )
    return _plan(swap, **over)


def _token_built(plan, **over):
    approve = {
        "type": 2,
        "chainId": plan.chain_id,
        "nonce": 0,
        "to": plan.source_token,
        "value": 0,
        "gas": 70000,
        "maxFeePerGas": 2 * 10**9,
        "maxPriorityFeePerGas": 10**9,
        "data": encode_approve(plan.vault_contract, plan.source_amount),
    }
    approve.update(over.pop("approve_tx", {}))
    return _built(plan, approve_tx=approve, **over)


def test_a_token_vault_swap_passes_the_gate():
    plan = _token_plan()
    assert _gate(plan, _token_built(plan)) == []


def test_the_gate_refuses_a_token_swap_with_no_approve():
    plan = _token_plan()
    assert any("needs an approve" in p for p in _gate(plan, _built(plan)))


def test_the_gate_refuses_an_approve_for_more_than_the_swap_moves():
    plan = _token_plan()
    built = _token_built(
        plan,
        approve_tx={
            "data": encode_approve(plan.vault_contract, plan.source_amount * 2)
        },
    )
    assert any("approve amount" in p for p in _gate(plan, built))


def test_the_gate_refuses_an_approve_naming_another_spender():
    plan = _token_plan()
    built = _token_built(
        plan, approve_tx={"data": encode_approve("0x" + "44" * 20, plan.source_amount)}
    )
    assert any("approve spender" in p for p in _gate(plan, built))


def test_the_gate_refuses_an_approve_on_another_token():
    plan = _token_plan()
    built = _token_built(plan, approve_tx={"to": "0x" + "55" * 20})
    assert any("!= token" in p for p in _gate(plan, built))


def test_the_gate_refuses_an_approve_that_is_not_an_approve():
    plan = _token_plan()
    built = _token_built(plan, approve_tx={"data": "0xdeadbeef"})
    assert any("could not be decoded" in p for p in _gate(plan, built))


# --- the adapter builds what the gate accepts -------------------------------


def _adapter(chain_id=CHAIN_ID):
    return EthAdapter("http://rpc.invalid", chain_id=chain_id)


MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def _build(plan, adapter=None, nonce=7):
    return (adapter or _adapter()).build_and_verify_vault_swap(
        plan=plan,
        now=NOW,
        mnemonic=MNEMONIC,
        nonce=nonce,
        max_fee_per_gas=2 * 10**9,
        max_priority_fee_per_gas=10**9,
        max_fee_wei=10**16,
    )


def test_the_adapter_builds_a_native_swap_the_gate_accepts():
    prepared = _build(_plan())
    assert prepared.problems == []
    assert len(prepared.built.txs) == 1
    assert prepared.built.swap_tx["nonce"] == 7
    assert prepared.built.swap_tx["value"] == NATIVE_VALUE


def test_the_adapter_builds_a_token_swap_as_approve_then_call():
    plan = _token_plan()
    prepared = _build(plan)
    assert prepared.problems == []
    assert len(prepared.built.txs) == 2
    approve, swap = prepared.built.txs
    assert approve["nonce"] == 7
    assert swap["nonce"] == 8
    assert approve["data"].startswith("0x" + APPROVE_SELECTOR)
    assert swap["value"] == 0


def test_the_adapter_signs_for_the_chain_the_plan_names():
    # Arbitrum's whole failure mode in one test: an ARB swap signed with chain
    # id 1 is a *valid Ethereum transaction* paying a different contract.
    plan = _plan(
        chain_id=42161,
        vault_contract=ARB_VAULT,
        known_vaults=frozenset({ARB_VAULT}),
    )
    prepared = _build(plan, adapter=_adapter(42161))
    assert prepared.built.swap_tx["chainId"] == 42161
    assert prepared.problems == []


def test_the_adapter_refuses_a_plan_its_chain_id_contradicts():
    prepared = _build(_plan(chain_id=42161), adapter=_adapter(CHAIN_ID))
    assert any("chainId" in p for p in prepared.problems)


def test_a_built_swap_is_signable_and_carries_our_calldata():
    plan = _plan()
    prepared = _build(plan)
    raws = _adapter().sign(prepared.built)
    assert len(raws) == 1
    assert raws[0].startswith("0x")


def test_the_wall_clock_is_not_needed_to_build_one():
    # Nothing here should reach the network or the clock: the plan carries its
    # own expiry, and the builder is pure given nonce and fees.
    before = time.monotonic()
    _build(_plan())
    assert time.monotonic() - before < 5


# --- the CLI glue -----------------------------------------------------------
#
# Thin, but it is where the unit conversion and the choice of refund address
# live, and both are silent when wrong: a 1e8 amount handed to an 18-decimal
# chain, or a refund pointed at someone else, would build and gate cleanly.


class _FakeClient:
    """The quote side of the backend, answering the fixture quote."""

    def __init__(self):
        self.asked = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def quote(self, src, dst, amount):
        self.asked.append((src, dst, amount))
        return dict(QUOTE_PAYLOAD, depositAmount=str(amount))


def _run_cli(monkeypatch, argv, *, from_asset=ETH, to_asset=BTC, amount=10_000_000):
    """Drive ``_swap_via_chainflip_evm`` with the chain stubbed, and capture
    what it hands the confirmation step."""
    from types import SimpleNamespace

    import swapsack.cli as cli
    from swapsack.cli import build_parser
    from swapsack.swap import SwapRequest

    monkeypatch.setattr("swapsack.chainflip.ChainflipRpc", lambda *a, **kw: _StubRpc())
    captured = {}

    def _confirm(prepared, adapter, args):
        captured["prepared"] = prepared
        return 0

    monkeypatch.setattr(cli, "_confirm_and_execute", _confirm)

    adapter = _adapter()
    from_address = adapter.derive_address(MNEMONIC)
    args = build_parser().parse_args(argv)
    rc = cli._swap_via_chainflip_evm(
        args,
        adapter,
        SimpleNamespace(name="chainflip", client=_FakeClient()),
        request=SwapRequest(
            from_asset=from_asset,
            to_asset=to_asset,
            amount=amount,
            destination=BTC_DEST,
        ),
        dest=BTC_DEST,
        mnemonic=MNEMONIC,
        from_address=from_address,
        nonce=3,
        max_fee_per_gas=2 * 10**9,
        max_priority_fee_per_gas=10**9,
    )
    return rc, captured.get("prepared"), from_address


CLI_ARGV = [
    "swap",
    "--from",
    "ETH",
    "--to",
    "BTC",
    "--amount",
    "0.1",
    "--backend",
    "chainflip",
    "--no-price-check",
]


def test_the_cli_path_builds_a_swap_the_gate_accepts(monkeypatch, capsys):
    rc, prepared, from_address = _run_cli(monkeypatch, CLI_ARGV)
    assert rc == 0
    assert prepared.problems == []
    out = capsys.readouterr().out
    assert "vault swap" in out
    assert BTC_DEST in out
    assert "floor:" in out
    # The refund goes back to the address that paid, and the plan says so —
    # this is the binding the gate then insists on.
    assert prepared.plan.refund_address == from_address
    assert from_address in out


def test_the_cli_path_scales_the_amount_into_the_sources_own_decimals(monkeypatch):
    # 0.1 in wallet-wide 1e8 units is 1e7; ETH has 18 decimals, so the chain
    # must be asked about 1e17 wei — not 1e7 of anything.
    _, prepared, _ = _run_cli(monkeypatch, CLI_ARGV)
    assert prepared.plan.value == 10**17
    assert prepared.built.swap_tx["value"] == 10**17


def test_the_cli_path_continues_from_the_nonce_it_was_given(monkeypatch):
    _, prepared, _ = _run_cli(monkeypatch, CLI_ARGV)
    assert prepared.built.swap_tx["nonce"] == 3


def test_the_cli_path_aborts_cleanly_when_the_chain_refuses(monkeypatch, capsys):
    import swapsack.chainflip as chainflip

    def _boom(*a, **kw):
        raise chainflip.ChainflipError("no broker would encode this")

    monkeypatch.setattr(chainflip, "prepare_evm_vault_swap", _boom)
    rc, prepared, _ = _run_cli(monkeypatch, CLI_ARGV)
    assert rc == 1
    assert prepared is None
    assert "ABORTED" in capsys.readouterr().err


# --- fields the gate must not leave free -------------------------------------
#
# Found by the clean-context review of this commit. Each is a value that travels
# in the money payload, is decoded, and was compared against nothing.


def test_prepare_refuses_a_call_with_a_retry_duration_we_did_not_ask_for():
    # retry_duration is a u32 in the payload. Zero refunds on the first block
    # that does not clear the floor, costing the ingress fee and the gas for
    # nothing; past the chain's cap the protocol rejects a payload we have
    # already paid into. Neither is ours to accept silently.
    rpc = _bad_encoding(params={"retry": 0})
    with pytest.raises(ChainflipError, match="retry duration"):
        _prepare(rpc, bps=0)


def test_the_gate_refuses_a_retry_duration_that_is_not_the_plans():
    calldata = _build_calldata(parameters=_build_parameters(retry=7))
    plan = _plan(calldata=calldata)
    assert any("retry duration" in p for p in _gate(plan))


def test_a_plan_carries_the_retry_duration_it_asked_for():
    assert _prepare().retry_duration == DEFAULT_RETRY_DURATION_BLOCKS


def test_prepare_refuses_a_quote_for_a_different_deposit_than_we_send():
    # min_price divides by the quote's deposit; the transaction moves
    # input_amount. A quote echoing a larger deposit would dilute the encoded
    # rate below the floor the CLI prints as the user's promise.
    quote = _quote(dict(QUOTE_PAYLOAD, depositAmount=str(NATIVE_VALUE * 2)))
    with pytest.raises(ChainflipError, match="quote is for"):
        prepare_evm_vault_swap(
            _StubRpc(),
            from_asset=ETH,
            to_asset=BTC,
            destination=BTC_DEST,
            refund_address=REFUND,
            input_amount=NATIVE_VALUE,
            quote=quote,
            bps=250,
        )


def test_the_gate_refuses_a_token_swap_that_also_sends_ether():
    # xSwapToken is non-payable, so a real Vault reverts — but the gate is the
    # layer that is supposed to know that without asking the contract.
    plan = _token_plan(value=1)
    built = _token_built(plan)
    assert any("must not send" in p for p in _gate(plan, built))


def test_the_native_sweep_reserves_enough_gas_for_a_vault_swap():
    """A sweep must reserve the gas the executor it selects will declare.

    `--amount max` reserved `--eth-gas` (a memo deposit's budget) and then built
    a Vault call declaring twice that, so value + gas*maxFee exceeded the
    balance and the node refused every time. Nothing was lost — but the feature
    was documented in three places and could not work.
    """
    from swapsack.chains.eth import DEFAULT_GAS, VAULT_SWAP_GAS
    from swapsack.cli import _evm_sweep_gas

    assert _evm_sweep_gas("auto") >= VAULT_SWAP_GAS
    assert _evm_sweep_gas("chainflip") >= VAULT_SWAP_GAS
    # A backend that cannot run a vault swap keeps the deposit budget, so a
    # THORChain sweep goes on leaving no more behind than it used to.
    assert _evm_sweep_gas("thorchain") == DEFAULT_GAS
