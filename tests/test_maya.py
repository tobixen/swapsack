"""Tests for the MayaChain adapter (Phase 1: address derivation + balance).

The derivation is money-sensitive — a wrong ``maya1`` address sends funds to one
the wallet cannot spend, and there is no Maya testnet to catch it. The golden
address below was produced from the standard BIP39 test mnemonic and
independently cross-checked: three BIP32 implementations (bitcoinlib,
eth-account, hdwallet) agree on the compressed pubkey at ``m/44'/931'/0'/0/0``,
and the bech32 step is exercised against a real on-chain address.
"""

import pytest

pytest.importorskip("bitcoinlib")

from cryptoswap_wallet.chains.maya import (  # noqa: E402
    MayaAdapter,
    bech32_decode,
    bech32_encode,
    parse_balances,
)

# Standard BIP39 test mnemonic -> its maya1 address at m/44'/931'/0'/0/0.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon about"
)
GOLDEN_MAYA_ADDRESS = "maya1gm00vwsfcp48enm4uv9e5dhm37jtd0ye2fs0sl"

# A real, in-use mainnet maya1 address (a BTC-pool LP), for the bech32 round-trip.
REAL_MAYA_ADDRESS = "maya10sy79jhw9hw9sqwdgu0k4mw4qawzl7czewzs47"


def test_derive_address_matches_golden_vector():
    addr = MayaAdapter().derive_address(TEST_MNEMONIC)
    assert addr == GOLDEN_MAYA_ADDRESS


def test_derive_address_is_deterministic_and_maya_prefixed():
    addr = MayaAdapter().derive_address(TEST_MNEMONIC)
    assert addr.startswith("maya1")
    assert MayaAdapter().derive_address(TEST_MNEMONIC) == addr


def test_bip39_passphrase_changes_the_address():
    plain = MayaAdapter().derive_address(TEST_MNEMONIC)
    passworded = MayaAdapter(bip39_passphrase="secret").derive_address(TEST_MNEMONIC)
    assert plain != passworded


def test_bech32_roundtrips_a_real_address():
    hrp, data = bech32_decode(REAL_MAYA_ADDRESS)
    assert hrp == "maya"
    assert len(data) == 20  # ripemd160(sha256(pubkey))
    assert bech32_encode(hrp, data) == REAL_MAYA_ADDRESS


def test_bech32_decode_rejects_a_corrupted_checksum():
    bad = REAL_MAYA_ADDRESS[:-1] + ("q" if REAL_MAYA_ADDRESS[-1] != "q" else "p")
    with pytest.raises(ValueError):
        bech32_decode(bad)


def test_parse_balances_sums_cacao_and_ignores_others():
    payload = {
        "balances": [
            {"denom": "cacao", "amount": "5000000000"},
            {"denom": "maya", "amount": "12345"},
        ],
        "pagination": {"total": "2"},
    }
    assert parse_balances(payload) == 5_000_000_000


def test_parse_balances_of_fresh_account_is_zero():
    assert parse_balances({"balances": [], "pagination": {"total": "0"}}) == 0


def test_wallet_balance_reports_cacao_at_1e10(monkeypatch):
    adapter = MayaAdapter()
    monkeypatch.setattr(adapter, "fetch_balance", lambda address: 27_983_000_000_000)
    report = adapter.wallet_balance(TEST_MNEMONIC)
    assert report.symbol == "CACAO"
    assert report.decimals == 10
    assert report.confirmed == 27_983_000_000_000
    assert report.addresses == (GOLDEN_MAYA_ADDRESS,)
    # 27_983_000_000_000 / 1e10 == 2798.3 CACAO
    assert "2798.30000000" in report.format()


def test_build_and_verify_send_passes_gate_and_signs_validly(monkeypatch):
    import base64
    import hashlib

    from eth_keys import keys

    from cryptoswap_wallet.chains import maya_tx

    adapter = MayaAdapter()
    # Avoid the network: pin account + chain id.
    monkeypatch.setattr(adapter, "fetch_account", lambda address: (4, 11))
    monkeypatch.setattr(adapter, "fetch_chain_id", lambda: "mayachain-mainnet-v1")

    prepared = adapter.build_and_verify_send(
        recipient=REAL_MAYA_ADDRESS, amount=15_000_000_000, mnemonic=TEST_MNEMONIC
    )
    assert prepared.problems == []  # gate is happy with a well-formed send

    # sign() -> a base64 TxRaw whose signature verifies over the SignDoc.
    (tx_b64,) = adapter.sign(prepared.built)
    tx_raw = base64.b64decode(tx_b64)
    fields = maya_tx._read_fields(tx_raw)
    body_bytes, auth_bytes = fields[1][0], fields[2][0]
    signature = fields[3][0]
    assert len(signature) == 64
    doc = maya_tx.sign_doc(body_bytes, auth_bytes, "mayachain-mainnet-v1", 4)
    digest = hashlib.sha256(doc).digest()
    signer = keys.PrivateKey(prepared.built.private_key).public_key
    recovered = [
        keys.Signature(signature + bytes([v])).recover_public_key_from_msg_hash(digest)
        for v in (0, 1)
    ]
    assert signer in recovered
