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
    VAULT_SWAP_ASSET_IDS,
    ChainflipBackend,
    ChainflipClient,
    ChainflipRpc,
    bitcoin_vault_addresses,
    parse_chainflip_quote,
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
