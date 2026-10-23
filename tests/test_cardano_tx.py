"""Cardano transaction format: anchored to mainnet and to pycardano.

``ANCHOR_TX`` is a real Maya deposit (ADA -> BTC, CIP-20 memo) fetched from
Koios: tx 6b714fd9e2fba152266987efa0419c0ff77a7d636f8a30fb160f850a33ef38f1.
``GOLDEN_TX`` was built and signed by this module, then checked offline with
pycardano 0.19.2: same txid, valid aux-data hash, verifying signature, and a
byte-identical re-encoding.
"""

from __future__ import annotations

import dataclasses

import pytest

pytest.importorskip("bitcoinlib")

from nacl.signing import VerifyKey  # noqa: E402

from swapsack.chains.cardano_keys import derive, root_key  # noqa: E402
from swapsack.chains.cardano_tx import (  # noqa: E402
    ProtocolParams,
    TxIn,
    TxOut,
    UnsignedTx,
    address_bytes,
    memo_chunks,
    min_fee,
    min_lovelace,
    parse,
    payment_key_hash,
    required_fee,
    serialize,
    txid,
)

ANCHOR_TX = bytes.fromhex(
    "84a400d901028182582098b0802e7805cf82207fe6223be68a78102e38fce75d"
    "f2c50ea411ff3deb466400018282581d61013315fa8d558e5a47e592c0c28c1a"
    "ab9dcfbbf4c627dc65c4b077fa1a17d78400825839018999bedb3e480e39003d"
    "e28d20f1a4b66da8a6426bbefa686ca4159ff3fe5d217e69464299b33132649c"
    "73ee9318d0fb829027d9040eb4901a81f65a06021a00029f6907582061c1bf4c"
    "cf167fe762fd8292c27ed4bb7235618a58f9f8f5ed59eca745207e8aa100d901"
    "0281825820724d01865187a93725bff90a6099cd36ac0ec091d84b6ff7b20846"
    "19b901a53c584032cf67b1f5393bd394340b3c78bb710ac56cb7be831e0e9172"
    "71020d249ae4129d66c30341b63390f154a45c3c57f8c76d82606ed181789896"
    "f91bc0e333ac03f5a11902a2a1636d736781783f3d3a623a626331717366656e"
    "6c37646b65716b6b61707175773239713839396d67656e36636e777a73617177"
    "36673a3131323834312f312f303a64683a3130"
)
GOLDEN_TX = bytes.fromhex(
    "84a500d901028182582098b0802e7805cf82207fe6223be68a78102e38fce75d"
    "f2c50ea411ff3deb466401018282581d61013315fa8d558e5a47e592c0c28c1a"
    "ab9dcfbbf4c627dc65c4b077fa1a004c4b40825839012d7eb5736483635fda32"
    "06fdf2b7e60ab41def32a068033b997823cae557890352095f1cf6fd2b7d1a28"
    "e3c3cb029f48cf34ff890a28d1761a003d0900021a00029ee5031a0bff982007"
    "5820f965b0917ce84f133ba4d5ef0374405e358580ed1e555477a85086cfebd2"
    "b6efa100d90102818258207ea09a34aebb13c9841c71397b1cabfec5ddf95040"
    "5293dee496cac2f437480a5840a8300a6d31fc023375ce2828bff01cabad6b5f"
    "b6c9637bfb76042068fe9145148787448655bea63abd418c83c6e53522331944"
    "7441912fe981b70dbaa067430ff5a11902a2a1636d736781783a3d3a4554482e"
    "4554483a30783030303030303030303030303030303030303030303030303030"
    "30303030303030303030303030303a302f312f30"
)

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon about"
)
VAULT = "addr1vyqnx906342cukj8ukfvps5vr24emnam7nrz0hr9cjc807say5nlw"
CHANGE = (
    "addr1qykhadtnvjpkxh76xgr0mu4huc9tg800x2sxsqemn9uz8jh927ysx5sftuw0dlft"
    "05dz3c7revpf7jx0xnlcjz3g69mq28kufu"
)
MEMO = "=:ETH.ETH:0x0000000000000000000000000000000000000000:0/1/0"
# Mainnet values on 2026-10-23 (epoch 663).
PARAMS = ProtocolParams(min_fee_a=44, min_fee_b=155381, coins_per_utxo_byte=4310)


def _golden_unsigned(fee: int = 171749) -> UnsignedTx:
    return UnsignedTx(
        inputs=(
            TxIn(
                bytes.fromhex(
                    "98b0802e7805cf82207fe6223be68a78102e38fce75df2c50ea411ff3deb4664"
                ),
                1,
            ),
        ),
        outputs=(
            TxOut(address_bytes(VAULT), 5_000_000),
            TxOut(address_bytes(CHANGE), 4_000_000),
        ),
        fee=fee,
        ttl=201_300_000,
        memo=MEMO,
    )


def _key():
    return derive(root_key(TEST_MNEMONIC), "m/1852'/1815'/0'/0/0")


# --- the mainnet anchor --------------------------------------------------------


def test_anchor_txid_is_the_hash_of_the_body_as_written():
    assert parse(ANCHOR_TX).txid.hex() == (
        "6b714fd9e2fba152266987efa0419c0ff77a7d636f8a30fb160f850a33ef38f1"
    )


def test_anchor_witness_signs_the_txid():
    parsed = parse(ANCHOR_TX)
    [(vkey, signature)] = parsed.witnesses
    VerifyKey(vkey).verify(parsed.txid, signature)


def test_anchor_reads_the_vault_payment_fee_and_cip20_memo():
    parsed = parse(ANCHOR_TX)
    assert parsed.outputs[0] == (VAULT, 400_000_000)
    assert parsed.fee == 171_881
    assert (
        parsed.memo
        == b"=:b:bc1qsfenl7dkeqkkapquw29q899mgen6cnwzsaqw6g:112841/1/0:dh:10"
    )


# --- building ------------------------------------------------------------------


def test_built_tx_matches_the_pycardano_checked_golden():
    tx = _golden_unsigned()
    key = _key()
    assert serialize(tx, [(key.public_key, key.sign(txid(tx)))]) == GOLDEN_TX


def test_built_tx_parses_back_to_what_was_built():
    parsed = parse(GOLDEN_TX)
    tx = _golden_unsigned()
    assert parsed.txid == txid(tx)
    assert parsed.outputs == ((VAULT, 5_000_000), (CHANGE, 4_000_000))
    assert parsed.fee == 171_749
    assert parsed.memo == MEMO.encode()
    [(vkey, signature)] = parsed.witnesses
    VerifyKey(vkey).verify(parsed.txid, signature)


def test_required_fee_pays_for_the_signed_size_and_no_more():
    fee = required_fee(dataclasses.replace(_golden_unsigned(), fee=0), 1, PARAMS)
    assert fee == min_fee(len(GOLDEN_TX), PARAMS) == 171_749


def test_a_plain_send_carries_no_auxiliary_data():
    tx = dataclasses.replace(_golden_unsigned(), memo=None)
    key = _key()
    parsed = parse(serialize(tx, [(key.public_key, key.sign(txid(tx)))]))
    assert parsed.memo is None


def test_a_memo_not_bound_to_the_body_is_rejected():
    # Change one memo character after signing: the body's aux hash no longer
    # matches, which a node rejects — and so must the gate's reader.
    tampered = GOLDEN_TX.replace(b"0/1/0", b"0/1/9")
    with pytest.raises(ValueError, match="hash"):
        parse(tampered)


# --- memo chunks, min-ADA, addresses ---------------------------------------------


def test_memo_chunks_respect_the_64_byte_metadata_limit():
    chunks = memo_chunks("x" * 130)
    assert [len(c) for c in chunks] == [64, 64, 2]
    assert "".join(chunks) == "x" * 130


def test_memo_chunks_never_split_a_utf8_character():
    chunks = memo_chunks("x" * 63 + "é")  # 'é' is two bytes
    assert chunks == ["x" * 63, "é"]


def test_short_memo_is_one_chunk():
    assert memo_chunks(MEMO) == [MEMO]


def test_min_lovelace_for_a_base_address_output():
    # [57-byte address, 5-byte uint] serializes to 65 bytes: (160 + 65) * 4310.
    assert min_lovelace(TxOut(address_bytes(CHANGE), 4_000_000), PARAMS) == 969_750


def test_address_bytes_rejects_testnet_and_stake_addresses():
    from swapsack.bech32 import bech32_encode

    payload = address_bytes(CHANGE)
    # Same credentials, testnet network id (0), under either HRP: both refused,
    # the second by the header nibble alone.
    testnet = bytes([payload[0] & 0xF0]) + payload[1:]
    for address in (
        bech32_encode("addr_test", testnet),
        bech32_encode("addr", testnet),
    ):
        with pytest.raises(ValueError, match="mainnet"):
            address_bytes(address)
    with pytest.raises(ValueError, match="mainnet"):
        address_bytes("stake1u8j40zgr2gy4788kl54h6x3gu0pukq5lfr8nflufpg5dzaskqlx2l")


def test_payment_key_hash_is_the_first_credential():
    from swapsack.chains.cardano_keys import blake2b_224

    first = (
        "addr1qy8ac7qqy0vtulyl7wntmsxc6wex80gvcyjy33qffrhm7sh927ysx5sftuw0dlft"
        "05dz3c7revpf7jx0xnlcjz3g69mq4afdhv"
    )
    assert payment_key_hash(address_bytes(first)) == blake2b_224(_key().public_key)
