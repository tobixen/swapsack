"""Opt-in integration tests against the live Chainflip quote API.

Excluded by default; run with `uv run pytest -m network`. These catch API drift
(field renames, a changed response envelope, an asset the service stops
serving) that unit tests against a recorded fixture cannot — which matters more
here than usual, because the whole point of this backend is to still answer when
THORChain and Maya do not.

Read-only: quoting is keyless and moves no funds.
"""

import time

import pytest

# The vault-swap tests build a real unsigned transaction, so bitcoinlib gets
# imported; its first import emits a SQLAlchemy deprecation that
# `filterwarnings = ["error"]` would turn into a collection error. Mirrors the
# other bitcoinlib-backed test modules.
pytest.importorskip("bitcoinlib")

from swapsack.chainflip import (  # noqa: E402
    CHAINFLIP_ASSETS,
    DEFAULT_BROKER_ACCOUNTS,
    VAULT_SWAP_ASSET_IDS,
    VAULT_SWAP_CHAIN_IDS,
    ChainflipBackend,
    ChainflipClient,
    ChainflipRpc,
    NoBrokerAvailable,
    _bitcoin_extra_parameters,
    _request_encoding,
    bitcoin_vault_addresses,
    deposit_units,
    evm_vault_addresses,
    parse_chainflip_quote,
    prepare_evm_vault_swap,
    prepare_vault_swap,
)
from swapsack.chains.btc import BtcAdapter  # noqa: E402
from swapsack.chains.coins import Utxo  # noqa: E402
from swapsack.cli import ASSET  # noqa: E402
from swapsack.verify import (  # noqa: E402
    ChainflipVaultPlan,
    decode_vault_swap_payload,
)

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


# --- vault swaps ------------------------------------------------------------
#
# Still read-only: encoding a vault swap moves nothing and commits to nothing.
# What it proves is the part unit tests cannot — that the payload layout this
# wallet decodes is the layout Chainflip actually encodes today. A silent change
# there would otherwise only surface on a real, irreversible transaction.

DEST = "0x000000000000000000000000000000000000dEaD"


def test_vault_swap_round_trips_through_our_own_decoder_live():
    with ChainflipClient() as client:
        quote = parse_chainflip_quote(
            client.quote(
                CHAINFLIP_ASSETS[BTC][:2], CHAINFLIP_ASSETS[ETH][:2], 10_000_000
            ),
            from_asset=BTC,
            to_asset=ETH,
        )
    with ChainflipRpc() as rpc:
        swap = prepare_vault_swap(
            rpc, from_asset=BTC, to_asset=ETH, destination=DEST, quote=quote, bps=250
        )
    decoded = decode_vault_swap_payload(swap.payload)
    assert decoded is not None, "live payload no longer decodes — layout changed"
    assert decoded.destination == bytes.fromhex(DEST[2:])
    assert decoded.asset_id == VAULT_SWAP_ASSET_IDS[ETH]
    assert decoded.min_output_amount >= swap.min_output_amount
    assert decoded.broker_fee == 0
    assert decoded.boost_fee == 0
    assert decoded.affiliates == 0
    assert decoded.dca_chunks == 1


# The account this wallet named until 2026-08-31, when it began enforcing a
# 5 bps minimum commission. Kept as the live specimen of a broker refusal: the
# fallback is only as good as its ability to *recognise* one, and nothing else
# in the suite exercises that path.
RETIRED_BROKER = "cFJZVRaybb2PBwxTiAiRLiQfHY4KPB3RpJK22Q7Fhqk979aCH"

# 3 ETH, comfortably above any dust rule and irrelevant to what these assert.
FLOOR = 3 * 10**18


def test_the_broker_fallback_chain_has_not_run_out_live():
    """Early warning: how many of our brokers still encode at zero commission.

    A broker can set a minimum commission at any time, and one that does is one
    this wallet cannot use — a commission is a skim
    ``verify.verify_chainflip_vault_swap`` refuses. On 2026-08-31 exactly six of
    the 134 registered brokers could encode a Bitcoin vault swap at all, and the
    account this wallet had hardcoded was the one of them demanding 5 bps.

    Two are required rather than one so that a single broker tightening does not
    fail CI, while a chain worn down to its last account does — at which point
    the choice is a wider list or paying a (disclosed, gated) commission, and
    that is a decision worth being woken up for.
    """
    usable, payloads, addresses, refused = [], set(), set(), []
    with ChainflipRpc() as rpc:
        vaults = bitcoin_vault_addresses(rpc)
        for account in DEFAULT_BROKER_ACCOUNTS:
            try:
                # Through the wallet's own encoder, one account at a time: a
                # test that hand-rolled the RPC parameters could keep passing
                # while the code sent something else, which is the one thing a
                # live test is here to catch.
                result = _request_encoding(
                    rpc,
                    from_asset=BTC,
                    to_asset=ETH,
                    destination=DEST,
                    extra=_bitcoin_extra_parameters(floor=FLOOR),
                    accounts=(account,),
                )
            except NoBrokerAvailable as exc:
                # Only a refusal this wallet recognises lands here; anything
                # else propagates rather than being counted as a tightening
                # broker.
                refused.append(f"{account}: {exc}")
                continue
            usable.append(account)
            payloads.add(str(result["nulldata_payload"]))
            addresses.add(str(result["deposit_address"]))
            # Whichever broker answers, the address it names has to be one the
            # protocol publishes — the fallback must not widen what we pay.
            assert str(result["deposit_address"]) in vaults

    assert len(usable) >= 2, (
        f"only {len(usable)} of {len(DEFAULT_BROKER_ACCOUNTS)} brokers still "
        f"encode at zero commission: {refused}"
    )
    # The claim the fallback rests on, and the exact extent of it: the payload
    # is identical whoever answers, and the deposit address is not — each broker
    # has its own private channel into a vault, which is why the address is
    # checked against the published list above rather than trusted.
    assert len(payloads) == 1, "the payload now depends on the broker account"
    assert len(addresses) == len(usable), "brokers now share a deposit address"


def test_a_broker_refusal_is_still_recognised_live():
    """The fallback's blind spot: a refusal it does not recognise as one.

    ``BROKER_REFUSALS`` matches on the text of a ``DispatchError``. If the
    runtime ever renders one differently, every refusal starts propagating as a
    hard error instead of moving to the next broker, the fallback is silently
    dead, and the suite above would not notice — it only ever looks at brokers
    that *succeed*.
    """
    with ChainflipRpc() as rpc:
        try:
            _request_encoding(
                rpc,
                from_asset=BTC,
                to_asset=ETH,
                destination=DEST,
                extra=_bitcoin_extra_parameters(floor=FLOOR),
                accounts=(RETIRED_BROKER,),
            )
        except NoBrokerAvailable as exc:
            # What we want: the refusal was recognised and turned into the
            # give-up error, carrying the chain's own words.
            assert RETIRED_BROKER in str(exc)
            return
        pytest.skip(
            f"{RETIRED_BROKER} no longer refuses to encode at a zero "
            f"commission — this canary needs a new specimen"
        )


def test_the_deposit_address_is_a_published_protocol_vault_live():
    with ChainflipRpc() as rpc:
        vaults = bitcoin_vault_addresses(rpc)
        with ChainflipClient() as client:
            quote = parse_chainflip_quote(
                client.quote(
                    CHAINFLIP_ASSETS[BTC][:2], CHAINFLIP_ASSETS[ETH][:2], 10_000_000
                ),
                from_asset=BTC,
                to_asset=ETH,
            )
        swap = prepare_vault_swap(
            rpc, from_asset=BTC, to_asset=ETH, destination=DEST, quote=quote, bps=250
        )
    assert vaults
    assert all(v.startswith("bc1") for v in vaults)
    assert swap.deposit_address in vaults


def test_a_built_vault_swap_passes_the_gate_live():
    # The end-to-end shape, short of broadcasting: quote, encode against
    # mainnet, build a real unsigned transaction from a throwaway key's UTXO,
    # and require the gate to pass on it.
    adapter = BtcAdapter()
    mnemonic = (
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )
    path = "m/84'/0'/0'/0/0"
    address = adapter.derive_address(mnemonic, path)
    utxos = [Utxo(txid="aa" * 32, vout=0, value=2_000_000, address=address, path=path)]

    with ChainflipClient() as client:
        quote = parse_chainflip_quote(
            client.quote(
                CHAINFLIP_ASSETS[BTC][:2], CHAINFLIP_ASSETS[ETH][:2], 1_000_000
            ),
            from_asset=BTC,
            to_asset=ETH,
        )
    with ChainflipRpc() as rpc:
        swap = prepare_vault_swap(
            rpc, from_asset=BTC, to_asset=ETH, destination=DEST, quote=quote, bps=250
        )

    now = int(time.time())
    plan = ChainflipVaultPlan(
        deposit_address=swap.deposit_address,
        amount=1_000_000,
        payload=swap.payload,
        expiry=now + 600,
        destination_asset_id=swap.destination_asset_id,
        destination_bytes=swap.destination_bytes,
        min_output_amount=swap.min_output_amount,
        known_vaults=swap.known_vaults,
    )
    prepared = adapter.build_and_verify_vault_swap(
        plan=plan,
        now=now,
        mnemonic=mnemonic,
        scanned_utxos=utxos,
        fee_rate=2,
        change_address=adapter.derive_address(mnemonic, adapter.change_path),
        max_fee=100_000,
    )
    assert prepared.problems == []


# --- vault swaps from an EVM source -----------------------------------------
#
# Also read-only. What these prove is what no unit test can: that the calldata
# layout `verify.decode_evm_vault_call` reads is the layout Chainflip encodes
# today. It matters more here than on the Bitcoin side, because the chain
# refuses to decode an EVM vault swap for us at all
# (`cf_decode_vault_swap_parameter`: "only supports Bitcoin and Solana"), so
# there is no second opinion to fall back on if this drifts.

ONE_ETH = 10**18
EVM_REFUND = "0x000000000000000000000000000000000000dEaD"
BTC_DEST = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


def _evm_quote(from_asset, to_asset, amount):
    with ChainflipClient() as client:
        return parse_chainflip_quote(
            client.quote(
                CHAINFLIP_ASSETS[from_asset][:2],
                CHAINFLIP_ASSETS[to_asset][:2],
                amount,
            ),
            from_asset=from_asset,
            to_asset=to_asset,
        )


def test_an_eth_source_encodes_a_vault_call_our_decoder_reads_back_live():
    from swapsack.verify import decode_evm_vault_call, decode_evm_vault_parameters

    quote = _evm_quote(ETH, BTC, ONE_ETH)
    with ChainflipRpc() as rpc:
        swap = prepare_evm_vault_swap(
            rpc,
            from_asset=ETH,
            to_asset=BTC,
            destination=BTC_DEST,
            refund_address=EVM_REFUND,
            input_amount=ONE_ETH,
            quote=quote,
            bps=250,
        )
    call = decode_evm_vault_call(swap.calldata)
    assert call is not None, "a live Vault call no longer decodes — layout changed"
    # A Bitcoin payout from an EVM source: the destination is the address
    # string's own ASCII, which is the whole reason this pair is settleable.
    assert call.destination.decode() == BTC_DEST
    assert call.destination_chain_id == VAULT_SWAP_CHAIN_IDS["Bitcoin"]
    assert call.destination_asset_id == VAULT_SWAP_ASSET_IDS[BTC]
    assert swap.value == ONE_ETH
    params = decode_evm_vault_parameters(call.parameters)
    assert params is not None, "the cfParameters layout changed"
    assert params.refund_address.hex() == EVM_REFUND[2:].lower()
    assert params.min_price >= swap.min_price
    assert (params.broker_fee, params.boost_fee, params.affiliates) == (0, 0, 0)


def test_a_token_source_encodes_an_xswaptoken_naming_the_contract_live():
    from swapsack.verify import decode_evm_vault_call

    amount = 1000 * 10**6  # 1000 USDC
    quote = _evm_quote(USDC, BTC, amount)
    with ChainflipRpc() as rpc:
        swap = prepare_evm_vault_swap(
            rpc,
            from_asset=USDC,
            to_asset=BTC,
            destination=BTC_DEST,
            refund_address=EVM_REFUND,
            input_amount=amount,
            quote=quote,
            bps=250,
        )
    assert swap.value == 0, "a token vault swap must send no ether"
    assert swap.source_token == USDC.partition("-")[2].lower()
    call = decode_evm_vault_call(swap.calldata)
    assert call is not None
    assert call.source_amount == amount


def test_the_evm_vault_contract_is_the_one_published_on_chain_live():
    quote = _evm_quote(ETH, BTC, ONE_ETH)
    with ChainflipRpc() as rpc:
        published = evm_vault_addresses(rpc, "Ethereum")
        swap = prepare_evm_vault_swap(
            rpc,
            from_asset=ETH,
            to_asset=BTC,
            destination=BTC_DEST,
            refund_address=EVM_REFUND,
            input_amount=ONE_ETH,
            quote=quote,
            bps=250,
        )
    assert len(published) == 1
    assert swap.vault_contract in published


def test_a_built_evm_vault_swap_passes_the_gate_live():
    # The end-to-end shape, short of broadcasting: quote, encode against
    # mainnet, build a real unsigned transaction from a throwaway key, and
    # require the gate to pass on it. The Bitcoin sibling of this test is
    # test_a_built_vault_swap_passes_the_gate_live above.
    from swapsack.chains.eth import EthAdapter
    from swapsack.verify import ChainflipEvmVaultPlan

    mnemonic = (
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )
    adapter = EthAdapter("http://rpc.invalid")  # nothing here touches the node
    refund = adapter.derive_address(mnemonic)

    amount = deposit_units(10_000_000, CHAINFLIP_ASSETS[ETH][2])  # 0.1 ETH
    quote = _evm_quote(ETH, BTC, amount)
    with ChainflipRpc() as rpc:
        swap = prepare_evm_vault_swap(
            rpc,
            from_asset=ETH,
            to_asset=BTC,
            destination=BTC_DEST,
            refund_address=refund,
            input_amount=amount,
            quote=quote,
            bps=250,
        )

    now = int(time.time())
    prepared = adapter.build_and_verify_vault_swap(
        plan=ChainflipEvmVaultPlan(
            vault_contract=swap.vault_contract,
            calldata=swap.calldata,
            value=swap.value,
            source_token=swap.source_token,
            source_amount=swap.source_amount,
            expiry=now + 600,
            chain_id=adapter.chain_id,
            destination_chain_id=swap.destination_chain_id,
            destination_asset_id=swap.destination_asset_id,
            destination_bytes=swap.destination_bytes,
            refund_address=refund,
            min_price=swap.min_price,
            retry_duration=swap.retry_duration,
            known_vaults=swap.known_vaults,
        ),
        now=now,
        mnemonic=mnemonic,
        nonce=0,
        max_fee_per_gas=2 * 10**9,
        max_priority_fee_per_gas=10**9,
        max_fee_wei=10**16,
    )
    assert prepared.problems == []
