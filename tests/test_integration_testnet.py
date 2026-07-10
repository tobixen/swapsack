"""Opt-in FULL-LOOP send tests on public testnets (build -> sign -> broadcast).

These prove the account/UTXO *spending* path end to end against a real network,
which recorded-fixture unit tests cannot. They move real (valueless) testnet
coins, so each is gated on a funded testnet account supplied via env — like the
Nile TRC-20 loop in ``test_tron.py`` — and skips for everyone else. Provide the
account our wallet DERIVES. The funding addresses are documented in
``docs/testnet.md`` (and each test prints its address on the send line):

  BTC signet (default; override the network via env for testnet3/4):
    SWAPSACK_BTC_TESTNET_MNEMONIC   seed of a funded account
    SWAPSACK_BTC_TESTNET_NETWORK    optional; "signet" (default)/"testnet"
    SWAPSACK_BTC_TESTNET_ESPLORA    optional Esplora base URL
    SWAPSACK_BTC_TESTNET_RECIPIENT   optional; defaults to a self-send

  ETH Sepolia:
    SWAPSACK_ETH_SEPOLIA_MNEMONIC   seed of a funded Sepolia account
    SWAPSACK_ETH_SEPOLIA_RPC        optional JSON-RPC URL
    SWAPSACK_ETH_SEPOLIA_RECIPIENT   optional; defaults to a self-send

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

from swapsack.chains.btc import ACCOUNT, BtcAdapter  # noqa: E402
from swapsack.chains.coins import (  # noqa: E402
    InsufficientFunds,
    sweep_amount,
)
from swapsack.chains.eth import EthAdapter  # noqa: E402
from swapsack.chains.scan import scan_account  # noqa: E402

pytestmark = pytest.mark.network

BTC_TESTNET_MNEMONIC = os.environ.get("SWAPSACK_BTC_TESTNET_MNEMONIC")
# Default to signet (stable, reliable faucet); testnet3 is being deprecated and
# its faucets are chronically drained. Override with the NETWORK env (e.g.
# "testnet"/"testnet4") — the Esplora default follows it (blockstream hosts each
# under the same path). Signet and testnet3 share the tb1 address format, so the
# funded address is the same either way.
BTC_TESTNET_NETWORK = os.environ.get("SWAPSACK_BTC_TESTNET_NETWORK") or "signet"
BTC_TESTNET_ESPLORA = (
    os.environ.get("SWAPSACK_BTC_TESTNET_ESPLORA")
    or f"https://blockstream.info/{BTC_TESTNET_NETWORK}/api"
)

ETH_SEPOLIA_MNEMONIC = os.environ.get("SWAPSACK_ETH_SEPOLIA_MNEMONIC")
ETH_SEPOLIA_RPC = (
    os.environ.get("SWAPSACK_ETH_SEPOLIA_RPC")
    or "https://ethereum-sepolia-rpc.publicnode.com"
)
SEPOLIA_CHAIN_ID = 11155111


@pytest.mark.skipif(
    not BTC_TESTNET_MNEMONIC,
    reason="set SWAPSACK_BTC_TESTNET_MNEMONIC (a funded testnet account) "
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
            "SWAPSACK_BTC_TESTNET_RECIPIENT"
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
    reason="set SWAPSACK_ETH_SEPOLIA_MNEMONIC (a funded Sepolia account) "
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
        recipient = os.environ.get("SWAPSACK_ETH_SEPOLIA_RECIPIENT") or sender
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


DASH_MNEMONIC = os.environ.get("SWAPSACK_DASH_MNEMONIC")


@pytest.mark.skipif(
    not DASH_MNEMONIC,
    reason="set SWAPSACK_DASH_MNEMONIC (a funded MAINNET account) to run the "
    "DASH broadcast loop",
)
def test_dash_mainnet_send_broadcast():
    """Sweep the wallet's DASH UTXOs to itself: build -> verify -> sign ->
    broadcast, then confirm Insight sees the tx.

    Unlike the BTC/ETH loops there is no funded-testnet path for Dash (see
    docs/dash.md), so this runs on MAINNET — the coins moved are real, but it
    is a self-send and Dash fees are ~half a cent. This is the one place the
    legacy spend path gets exercised against a real network before users do.
    """
    from swapsack.chains.coins import P2PKH
    from swapsack.chains.dash import ACCOUNT as DASH_ACCOUNT
    from swapsack.chains.dash import DashAdapter

    receive_path = "m/44'/5'/0'/0/0"
    change_path = "m/44'/5'/0'/1/0"
    api = os.environ.get("SWAPSACK_DASH_API")
    with DashAdapter(api) if api else DashAdapter() as adapter:
        recipient = os.environ.get("SWAPSACK_DASH_RECIPIENT") or adapter.derive_address(
            DASH_MNEMONIC, receive_path
        )
        records = scan_account(
            derive_address=lambda p: adapter.derive_address(DASH_MNEMONIC, p),
            probe=adapter.address_info,
            account=DASH_ACCOUNT,
        )
        utxos = [
            dataclasses.replace(u, path=path)
            for path, address, info in records
            if info.confirmed > 0
            for u in adapter.fetch_utxos(address)
        ]
        if not utxos:
            pytest.skip(
                "no confirmed DASH UTXOs — fund "
                + adapter.derive_address(DASH_MNEMONIC, receive_path)
            )

        fee_rate = adapter.fetch_fee_rate()
        total = sum(u.value for u in utxos)
        try:
            amount, _ = sweep_amount(
                total, len(utxos), fee_rate, memo_len=0, script=P2PKH
            )
        except InsufficientFunds as exc:
            pytest.skip(f"DASH account too small to sweep after fee: {exc}")
        prepared = adapter.build_and_verify_send(
            recipient=recipient,
            amount=amount,
            now=int(time.time()),
            mnemonic=DASH_MNEMONIC,
            scanned_utxos=utxos,
            fee_rate=fee_rate,
            change_address=adapter.derive_address(DASH_MNEMONIC, change_path),
            max_fee=100_000,
            sweep=True,
        )
        assert prepared.safe, prepared.problems
        txid = adapter.broadcast(adapter.sign(prepared.built))
        assert txid

        seen = False
        for _ in range(20):  # wait for mempool acceptance, not a confirmation
            resp = adapter._get(f"{adapter.api_url}/tx/{txid}")
            if resp.status_code == 200:
                seen = True
                break
            time.sleep(3)
        assert seen, f"tx {txid} not seen by Insight"


ZEC_MNEMONIC = os.environ.get("SWAPSACK_ZEC_MNEMONIC")


@pytest.mark.skipif(
    not ZEC_MNEMONIC,
    reason="set SWAPSACK_ZEC_MNEMONIC (a funded MAINNET account) to run the "
    "ZEC broadcast loop",
)
def test_zec_mainnet_send_broadcast():
    """Sweep the wallet's transparent ZEC UTXOs to itself: build -> verify ->
    sign (bespoke v4/ZIP-243) -> broadcast via lightwalletd.

    Like DASH there is no funded-testnet path (see docs/zcash.md), so this runs
    on MAINNET — a self-send whose ZIP-317 fee is 10,000 zat (≈ half a cent).
    This is the one place the bespoke Zcash signer meets a real validator
    before users do.
    """
    from swapsack.chains.zcash import ACCOUNT as ZEC_ACCOUNT
    from swapsack.chains.zcash import ZecAdapter

    receive_path = "m/44'/133'/0'/0/0"
    change_path = "m/44'/133'/0'/1/0"
    lwd = os.environ.get("SWAPSACK_ZEC_LWD")
    with ZecAdapter(lwd) if lwd else ZecAdapter() as adapter:
        recipient = os.environ.get("SWAPSACK_ZEC_RECIPIENT") or adapter.derive_address(
            ZEC_MNEMONIC, receive_path
        )
        records = scan_account(
            derive_address=lambda p: adapter.derive_address(ZEC_MNEMONIC, p),
            probe=adapter.address_info,
            account=ZEC_ACCOUNT,
        )
        utxos = [
            dataclasses.replace(u, path=path)
            for path, address, info in records
            if info.confirmed > 0
            for u in adapter.fetch_utxos(address)
        ]
        if not utxos:
            pytest.skip(
                "no confirmed transparent ZEC UTXOs — fund "
                + adapter.derive_address(ZEC_MNEMONIC, receive_path)
            )

        total = sum(u.value for u in utxos)
        try:
            amount, _ = adapter.sweep_send_amount(total, len(utxos), 0.0)
        except InsufficientFunds as exc:
            pytest.skip(f"ZEC account too small to sweep after fee: {exc}")
        prepared = adapter.build_and_verify_send(
            recipient=recipient,
            amount=amount,
            now=int(time.time()),
            mnemonic=ZEC_MNEMONIC,
            scanned_utxos=utxos,
            fee_rate=0.0,
            change_address=adapter.derive_address(ZEC_MNEMONIC, change_path),
            max_fee=100_000,
            sweep=True,
        )
        assert prepared.safe, prepared.problems
        txid = adapter.broadcast(adapter.sign(prepared.built))
        assert len(txid) == 64
        # lightwalletd's SendTransaction only returns after the node accepted
        # the tx into its mempool, so a zero errorCode IS the acceptance check.
