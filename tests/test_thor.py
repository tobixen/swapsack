"""Tests for the THORChain adapter (native RUNE).

RUNE reuses the shared CosmosAdapter (see test_maya.py / test_cosmos_tx.py for the
protobuf + signing coverage); this module pins the RUNE-specific config: the
``thor1`` derivation (same key as maya, different HRP) against a golden vector,
and the 1e8 balance reporting.
"""

import pytest

pytest.importorskip("bitcoinlib")

from cryptoswap_wallet.chains.thor import ThorAdapter  # noqa: E402

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon about"
)
# Same account bytes as the maya1 golden vector (coin type 931), HRP "thor".
GOLDEN_THOR_ADDRESS = "thor1gm00vwsfcp48enm4uv9e5dhm37jtd0ye27wrx0"


def test_derive_address_matches_golden_vector():
    assert ThorAdapter().derive_address(TEST_MNEMONIC) == GOLDEN_THOR_ADDRESS


def test_rune_is_1e8_not_1e10():
    # RUNE follows THORChain's standard 1e8 (unlike Maya's 1e10 CACAO).
    assert ThorAdapter.decimals == 8


def test_wallet_balance_reports_rune_at_1e8(monkeypatch):
    adapter = ThorAdapter()
    monkeypatch.setattr(adapter, "fetch_balance", lambda address: 1_250_000_000)
    report = adapter.wallet_balance(TEST_MNEMONIC)
    assert report.symbol == "RUNE"
    assert report.decimals == 8
    assert report.addresses == (GOLDEN_THOR_ADDRESS,)
    assert "12.50000000" in report.format()  # 1_250_000_000 / 1e8


def test_build_and_verify_send_signs_validly_for_rune(monkeypatch):
    import base64
    import hashlib

    from eth_keys import keys

    from cryptoswap_wallet.chains import cosmos_tx

    adapter = ThorAdapter()
    monkeypatch.setattr(adapter, "fetch_account", lambda address: (7, 3))
    monkeypatch.setattr(adapter, "fetch_chain_id", lambda: "thorchain-1")

    prepared = adapter.build_and_verify_send(
        recipient=GOLDEN_THOR_ADDRESS, amount=100_000_000, mnemonic=TEST_MNEMONIC
    )
    assert prepared.problems == []
    decoded = cosmos_tx.decode_msg_send_body(prepared.built.body_bytes)
    assert decoded["denom"] == "rune"
    assert decoded["amount"] == "100000000"

    (tx_b64,) = adapter.sign(prepared.built)
    fields = cosmos_tx._read_fields(base64.b64decode(tx_b64))
    doc = cosmos_tx.sign_doc(fields[1][0], fields[2][0], "thorchain-1", 7)
    digest = hashlib.sha256(doc).digest()
    signer = keys.PrivateKey(prepared.built.private_key).public_key
    recovered = [
        keys.Signature(fields[3][0] + bytes([v])).recover_public_key_from_msg_hash(
            digest
        )
        for v in (0, 1)
    ]
    assert signer in recovered
