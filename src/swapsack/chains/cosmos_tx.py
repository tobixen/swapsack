"""Minimal Cosmos-SDK transaction assembly + signing for THORChain-family chains.

Chain-agnostic: the same messages serve MayaChain (CACAO) and THORChain (RUNE),
which share the Cosmos-SDK wire format. Pure and dependency-free (no
``grpcio``/``cosmpy`` at runtime): the only protobuf messages needed are small
and fixed, so they are hand-serialized here and the wire format is validated
**byte-for-byte against cosmpy** in the tests (``tests/test_cosmos_tx.py``). Kept
separate from the network-facing adapter so the money-sensitive
serialization/signing is easy to read and test offline.

Signing is SIGN_MODE_DIRECT: the signature covers ``sha256`` of the serialized
``SignDoc`` (body_bytes + auth_info_bytes + chain_id + account_number), as a
64-byte low-S secp256k1 ``r||s`` — the canonical Cosmos signature. See
docs/cacao.md for the surrounding roadmap.
"""

from __future__ import annotations

import hashlib

# Cosmos type URLs (the ``/`` prefix is part of the Any type_url).
PUBKEY_TYPE_URL = "/cosmos.crypto.secp256k1.PubKey"
MSGSEND_TYPE_URL = "/cosmos.bank.v1beta1.MsgSend"
# MayaChain's native deposit message (a THORChain fork; same type name).
MSGDEPOSIT_TYPE_URL = "/types.MsgDeposit"
SIGN_MODE_DIRECT = 1


# --- protobuf wire primitives ----------------------------------------------
# proto3 omits scalar fields at their default (0 / "" / empty); the helpers here
# reproduce that so the bytes match a canonical encoder exactly.


def _varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("varint cannot encode a negative number")
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _delimited(field: int, data: bytes) -> bytes:
    """A length-delimited field (wire type 2): sub-messages, strings, bytes."""
    return _tag(field, 2) + _varint(len(data)) + data


def _string(field: int, value: str) -> bytes:
    return _delimited(field, value.encode()) if value else b""


def _uint64(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value) if value else b""


# --- message builders -------------------------------------------------------


def _coin(denom: str, amount: str) -> bytes:
    return _string(1, denom) + _string(2, amount)


def _any(type_url: str, value: bytes) -> bytes:
    return _string(1, type_url) + _delimited(2, value)


def msg_send(from_addr: str, to_addr: str, denom: str, amount: str) -> bytes:
    """A ``cosmos.bank.v1beta1.MsgSend`` with a single coin (already type-URL free)."""
    return (
        _string(1, from_addr)
        + _string(2, to_addr)
        + _delimited(3, _coin(denom, amount))
    )


def msg_deposit(
    coins: list[tuple[str, str]], memo: str, signer_addr_bytes: bytes
) -> bytes:
    """A MayaChain ``types.MsgDeposit`` (swap / add-liquidity), memo in the message.

    ``coins`` is a list of ``(asset, amount)`` where ``asset`` here is the native
    ``MsgDeposit`` coin: field 1 is a nested Asset message, field 2 the amount
    string. For native CACAO the asset is ``MAYA.CACAO``. ``signer_addr_bytes`` is
    the 20-byte account (the same hash the ``maya1`` address encodes).
    """
    out = b""
    for asset, amount in coins:
        # MsgDeposit.Coin { Asset asset = 1; string amount = 2; }
        # Asset { string chain=1; string symbol=2; string ticker=3; bool synth=4; }
        chain, _, symbol = asset.partition(".")
        ticker = symbol.split("-")[0]
        asset_msg = _string(1, chain) + _string(2, symbol) + _string(3, ticker)
        coin = _delimited(1, asset_msg) + _string(2, amount)
        out += _delimited(1, coin)
    out += _string(2, memo)
    out += _delimited(3, signer_addr_bytes)
    return out


def tx_body(messages: list[bytes], memo: str) -> bytes:
    body = b"".join(_delimited(1, _any(url, value)) for url, value in messages)
    return body + _string(2, memo)


def _pubkey_any(compressed_pubkey: bytes) -> bytes:
    return _any(PUBKEY_TYPE_URL, _delimited(1, compressed_pubkey))


def _mode_info_direct() -> bytes:
    single = _tag(1, 0) + _varint(SIGN_MODE_DIRECT)  # ModeInfo.Single.mode
    return _delimited(1, single)  # ModeInfo.single


def _signer_info(compressed_pubkey: bytes, sequence: int) -> bytes:
    return (
        _delimited(1, _pubkey_any(compressed_pubkey))
        + _delimited(2, _mode_info_direct())
        + _uint64(3, sequence)
    )


def _fee(coins: list[tuple[str, str]], gas_limit: int) -> bytes:
    body = b"".join(_delimited(1, _coin(d, a)) for d, a in coins)
    return body + _uint64(2, gas_limit)


def auth_info(
    compressed_pubkey: bytes,
    sequence: int,
    fee_coins: list[tuple[str, str]],
    gas_limit: int,
) -> bytes:
    return _delimited(1, _signer_info(compressed_pubkey, sequence)) + _delimited(
        2, _fee(fee_coins, gas_limit)
    )


def sign_doc(
    body_bytes: bytes, auth_info_bytes: bytes, chain_id: str, account_number: int
) -> bytes:
    return (
        _delimited(1, body_bytes)
        + _delimited(2, auth_info_bytes)
        + _string(3, chain_id)
        + _uint64(4, account_number)
    )


def tx_raw(body_bytes: bytes, auth_info_bytes: bytes, signature: bytes) -> bytes:
    return (
        _delimited(1, body_bytes)
        + _delimited(2, auth_info_bytes)
        + _delimited(3, signature)
    )


# --- decoding (for the pre-broadcast verify gate) ---------------------------
# Enough of a protobuf reader to parse back the messages we build, so the verify
# gate binds what was *actually serialized*, not just the inputs we passed in.


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def _read_fields(data: bytes) -> dict[int, list]:
    """Parse protobuf bytes into ``{field_number: [values]}``.

    Varint fields decode to ``int``; length-delimited to ``bytes``. Sufficient
    for the fixed messages this module builds (no groups/fixed32/fixed64).
    """
    fields: dict[int, list] = {}
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(data, i)
        elif wire == 2:
            length, i = _read_varint(data, i)
            value = data[i : i + length]
            i += length
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.setdefault(field, []).append(value)
    return fields


def decode_msg_deposit_body(body_bytes: bytes) -> dict:
    """Decode a single-``MsgDeposit`` :func:`tx_body` back to its fields.

    Returns ``{type_url, coins: [(asset, amount)], memo, signer}`` where ``asset``
    is ``CHAIN.SYMBOL`` and ``signer`` is the raw account bytes.
    """
    body = _read_fields(body_bytes)
    memo = body.get(2, [b""])[0].decode()
    any_msg = _read_fields(body[1][0])
    type_url = any_msg[1][0].decode()
    msg = _read_fields(any_msg[2][0])
    coins = []
    for coin_bytes in msg.get(1, []):
        coin = _read_fields(coin_bytes)
        asset = _read_fields(coin[1][0])
        chain = asset[1][0].decode()
        symbol = asset[2][0].decode()
        coins.append((f"{chain}.{symbol}", coin[2][0].decode()))
    return {
        "type_url": type_url,
        "coins": coins,
        "memo": msg.get(2, [b""])[0].decode(),
        "signer": msg[3][0] if 3 in msg else b"",
        "outer_memo": memo,
    }


def decode_msg_send_body(body_bytes: bytes) -> dict:
    """Decode a single-``MsgSend`` :func:`tx_body` back to its fields.

    Returns ``{type_url, from_addr, to_addr, denom, amount, memo}``.
    """
    body = _read_fields(body_bytes)
    memo = body.get(2, [b""])[0].decode()
    any_msg = _read_fields(body[1][0])
    type_url = any_msg[1][0].decode()
    msg = _read_fields(any_msg[2][0])
    coin = _read_fields(msg[3][0])
    return {
        "type_url": type_url,
        "from_addr": msg[1][0].decode(),
        "to_addr": msg[2][0].decode(),
        "denom": coin[1][0].decode(),
        "amount": coin[2][0].decode(),
        "memo": memo,
    }


# --- signing ----------------------------------------------------------------


def sign_direct(private_key: bytes, sign_doc_bytes: bytes) -> bytes:
    """Sign ``sha256(sign_doc_bytes)`` -> 64-byte low-S secp256k1 ``r||s``.

    Uses eth-keys (already a dependency) for a canonical low-S ECDSA signature —
    exactly what Cosmos verifies against the account's secp256k1 pubkey.
    """
    from eth_keys import keys

    digest = hashlib.sha256(sign_doc_bytes).digest()
    signature = keys.PrivateKey(private_key).sign_msg_hash(digest)
    # eth-keys yields r(32) || s(32) || v(1); Cosmos wants just r||s, low-S
    # (which eth-keys already enforces).
    return signature.to_bytes()[:64]
