"""Opt-in FULL-LOOP send tests on public testnets (build -> sign -> broadcast).

These prove the account/UTXO *spending* path end to end against a real network,
which recorded-fixture unit tests cannot. They move real (valueless) testnet
coins, so each is gated on a funded testnet account supplied via env — like the
Nile TRC-20 loop in ``test_tron.py`` — and skips for everyone else. Provide the
account our wallet DERIVES. The funding addresses are documented in
``docs/testnet.md`` (and each test prints its address on the send line):

  BTC signet (default; override the network via env for testnet3/4):
    CRYPTOSWAP_WALLET_BTC_TESTNET_MNEMONIC   seed of a funded account
    CRYPTOSWAP_WALLET_BTC_TESTNET_NETWORK    optional; "signet" (default)/"testnet"
    CRYPTOSWAP_WALLET_BTC_TESTNET_ESPLORA    optional Esplora base URL
    CRYPTOSWAP_WALLET_BTC_TESTNET_RECIPIENT   optional; defaults to a self-send

  ETH Sepolia:
    CRYPTOSWAP_WALLET_ETH_SEPOLIA_MNEMONIC   seed of a funded Sepolia account
    CRYPTOSWAP_WALLET_ETH_SEPOLIA_RPC        optional JSON-RPC URL
    CRYPTOSWAP_WALLET_ETH_SEPOLIA_RECIPIENT   optional; defaults to a self-send

Run with ``pytest -m network``.
"""

from __future__ import annotations

import dataclasses
import os
import time

import pytest

# Import bitcoinlib (a hard dependency) at collection time, not inside a test:
# its first import has noisy side effects (a leaked file handle + a SQLAlchemy
# deprecation warning) that `filterwarnings = ["error"]` would otherwise turn
# into a spurious in-test failure. Mirrors the other bitcoinlib-backed tests.
pytest.importorskip("bitcoinlib")

from cryptoswap_wallet.chains.btc import ACCOUNT, BtcAdapter  # noqa: E402
from cryptoswap_wallet.chains.coins import (  # noqa: E402
    InsufficientFunds,
    sweep_amount,
)
from cryptoswap_wallet.chains.eth import EthAdapter  # noqa: E402
from cryptoswap_wallet.chains.scan import scan_account  # noqa: E402

pytestmark = pytest.mark.network

BTC_TESTNET_MNEMONIC = os.environ.get("CRYPTOSWAP_WALLET_BTC_TESTNET_MNEMONIC")
# Default to signet (stable, reliable faucet); testnet3 is being deprecated and
# its faucets are chronically drained. Override with the NETWORK env (e.g.
# "testnet"/"testnet4") — the Esplora default follows it (blockstream hosts each
# under the same path). Signet and testnet3 share the tb1 address format, so the
# funded address is the same either way.
BTC_TESTNET_NETWORK = (
    os.environ.get("CRYPTOSWAP_WALLET_BTC_TESTNET_NETWORK") or "signet"
)
BTC_TESTNET_ESPLORA = (
    os.environ.get("CRYPTOSWAP_WALLET_BTC_TESTNET_ESPLORA")
    or f"https://blockstream.info/{BTC_TESTNET_NETWORK}/api"
)

ETH_SEPOLIA_MNEMONIC = os.environ.get("CRYPTOSWAP_WALLET_ETH_SEPOLIA_MNEMONIC")
ETH_SEPOLIA_RPC = (
    os.environ.get("CRYPTOSWAP_WALLET_ETH_SEPOLIA_RPC")
    or "https://ethereum-sepolia-rpc.publicnode.com"
)
SEPOLIA_CHAIN_ID = 11155111


@pytest.mark.skipif(
    not BTC_TESTNET_MNEMONIC,
    reason="set CRYPTOSWAP_WALLET_BTC_TESTNET_MNEMONIC (a funded testnet account) "
    "to run the BTC testnet broadcast loop",
)
def test_btc_testnet_send_broadcast():
    """Sweep the wallet's testnet UTXOs to itself: build -> verify -> sign ->
    broadcast, then confirm the network accepted the tx (Esplora sees it)."""
    receive_path = "m/84'/0'/0'/0/0"
    change_path = "m/84'/0'/0'/1/0"
    with BtcAdapter(
        esplora_url=BTC_TESTNET_ESPLORA, network=BTC_TESTNET_NETWORK
    ) as adapter:
        recipient = os.environ.get(
            "CRYPTOSWAP_WALLET_BTC_TESTNET_RECIPIENT"
        ) or adapter.derive_address(BTC_TESTNET_MNEMONIC, receive_path)
        records = scan_account(
            derive_address=lambda p: adapter.derive_address(BTC_TESTNET_MNEMONIC, p),
            probe=adapter.address_info,
            account=ACCOUNT,
        )
        utxos = [
            dataclasses.replace(u, path=path)
            for path, address, info in records
            if info.confirmed > 0
            for u in adapter.fetch_utxos(address)
        ]
        if not utxos:
            # Skip (not fail): an unfunded / still-confirming account is an
            # environmental condition, not a code regression — so a dry faucet or
            # testnet3's irregular blocks never turn CI red.
            pytest.skip(
                "no confirmed testnet UTXOs — fund "
                + adapter.derive_address(BTC_TESTNET_MNEMONIC, receive_path)
            )

        fee_rate = adapter.fetch_fee_rate()
        total = sum(u.value for u in utxos)
        try:
            amount, _ = sweep_amount(total, len(utxos), fee_rate, memo_len=0)
        except InsufficientFunds as exc:
            # Skip (not fail): a dust balance that can't cover the (often spiky)
            # testnet3 fee is an environmental condition like an unfunded account,
            # not a code regression. Top up the address printed above to run it.
            pytest.skip(f"testnet account too small to sweep after fee: {exc}")
        prepared = adapter.build_and_verify_send(
            recipient=recipient,
            amount=amount,
            now=int(time.time()),
            mnemonic=BTC_TESTNET_MNEMONIC,
            scanned_utxos=utxos,
            fee_rate=fee_rate,
            change_address=adapter.derive_address(BTC_TESTNET_MNEMONIC, change_path),
            max_fee=100_000,
            sweep=True,
        )
        assert prepared.safe, prepared.problems
        txid = adapter.broadcast(adapter.sign(prepared.built))
        assert txid

        seen = False
        for _ in range(20):  # wait for mempool acceptance (not a full confirmation)
            resp = adapter._get(f"{adapter.esplora_url}/tx/{txid}")
            if resp.status_code == 200:
                seen = True
                break
            time.sleep(3)
        assert seen, f"tx {txid} not seen by Esplora"


@pytest.mark.skipif(
    not ETH_SEPOLIA_MNEMONIC,
    reason="set CRYPTOSWAP_WALLET_ETH_SEPOLIA_MNEMONIC (a funded Sepolia account) "
    "to run the ETH Sepolia broadcast loop",
)
def test_eth_sepolia_send_broadcast_and_confirm():
    """Self-send a tiny amount of Sepolia ETH: build -> verify -> sign ->
    broadcast -> confirm the receipt shows success on chain id 11155111."""
    with EthAdapter(rpc_url=ETH_SEPOLIA_RPC, chain_id=SEPOLIA_CHAIN_ID) as adapter:
        sender = adapter.derive_address(ETH_SEPOLIA_MNEMONIC)
        # Skip (not fail) when unfunded: keeps CI green if the faucet drips dry.
        # 0.001 ETH is sent + gas; require a little headroom.
        if adapter.fetch_balance(sender) < 2 * 10**15:
            pytest.skip(f"Sepolia account unfunded — fund {sender}")
        recipient = os.environ.get("CRYPTOSWAP_WALLET_ETH_SEPOLIA_RECIPIENT") or sender
        nonce = adapter.get_nonce(sender)
        max_fee_per_gas, max_priority_fee_per_gas = adapter.fetch_fees()
        prepared = adapter.build_and_verify_send(
            recipient=recipient,
            amount=100_000,  # 0.001 ETH in 1e8 units
            asset="ETH.ETH",
            mnemonic=ETH_SEPOLIA_MNEMONIC,
            nonce=nonce,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
            max_fee_wei=10**16,
        )
        assert prepared.safe, prepared.problems
        assert prepared.built.chain_id == SEPOLIA_CHAIN_ID
        txid = adapter.broadcast(adapter.sign(prepared.built))
        assert txid

        receipt = None
        for _ in range(30):  # ~12s blocks; wait up to ~90s for inclusion
            receipt = adapter._rpc("eth_getTransactionReceipt", [txid])
            if receipt:
                break
            time.sleep(3)
        assert receipt, f"tx {txid} not confirmed in time"
        assert int(receipt["status"], 16) == 1  # 1 = success, 0 = reverted
