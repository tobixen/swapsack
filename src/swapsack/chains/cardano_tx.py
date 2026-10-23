"""Cardano transactions: CBOR body, fees, min-ADA, metadata memo, witnesses.

Only the slice of the ledger format a pure-ADA wallet needs: inputs, plain
``[address, lovelace]`` outputs, fee, TTL, and — for a Maya deposit — a CIP-20
message (metadata label 674, ``{"msg": [≤64-byte chunks]}``) bound into the body
by its hash. No native tokens, scripts, datums or certificates: the adapter
never selects a UTXO holding tokens, so none can be dropped by accident.

Sets (inputs, vkey witnesses) are written with CBOR tag 258, which Conway
accepts and later eras will require. The transaction id is the blake2b-256 of
the body's bytes, and is what every witness signs.

:func:`parse` is the neutral reader for the verify gate: it works from the
serialized bytes — hashing the body exactly as it was written rather than a
re-encoding — so a build bug cannot hide behind the builder's own inputs.
Pinned in the tests by a real Maya deposit from mainnet.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
from typing import Any

import cbor2

from swapsack.bech32 import bech32_decode, bech32_encode

SET_TAG = 258
CIP20_LABEL = 674
METADATA_CHUNK = 64  # bytes: the ledger's limit on one metadata string
# Every mainnet Shelley address has network id 1 in its header's low nibble.
MAINNET = 0x01
# Babbage+ min-ADA: every output carries (this + its serialized size) bytes
# worth of ``coins_per_utxo_byte``.
UTXO_ENTRY_OVERHEAD = 160
# Fee sizing for a tx whose recipient is not yet known: the longest common
# Shelley address (base: header + two 28-byte hashes).
LONGEST_ADDRESS = bytes([MAINNET]) + bytes(56)


@dataclasses.dataclass(frozen=True)
class ProtocolParams:
    """The protocol parameters a simple payment's fee and min-ADA depend on."""

    min_fee_a: int  # lovelace per byte of the signed tx
    min_fee_b: int  # lovelace, flat
    coins_per_utxo_byte: int
    max_tx_size: int = 16384  # bytes; the mainnet value since Alonzo


@dataclasses.dataclass(frozen=True)
class TxIn:
    txid: bytes  # 32 bytes, as displayed (Cardano does not byte-reverse)
    index: int


@dataclasses.dataclass(frozen=True)
class TxOut:
    address: bytes  # raw address bytes (header + hashes)
    lovelace: int


@dataclasses.dataclass(frozen=True)
class UnsignedTx:
    inputs: tuple[TxIn, ...]
    outputs: tuple[TxOut, ...]
    fee: int
    ttl: int  # last slot the tx is valid in
    memo: str | None = None


# --- addresses ---------------------------------------------------------------


def address_bytes(address: str) -> bytes:
    """Decode a mainnet Shelley ``addr1…`` address to its raw bytes."""
    hrp, raw = bech32_decode(address)
    if hrp != "addr" or not raw or raw[0] & 0x0F != MAINNET:
        raise ValueError(f"not a mainnet Shelley address: {address!r}")
    return raw


def address_string(raw: bytes) -> str:
    return bech32_encode("addr", raw)


def payment_key_hash(raw: bytes) -> bytes:
    """The payment credential of a key-hash address (types 0, 2, 4, 6)."""
    if raw[0] >> 4 not in (0, 2, 4, 6):
        raise ValueError(f"address type {raw[0] >> 4} has no payment key hash")
    return raw[1:29]


# --- memo (CIP-20) -----------------------------------------------------------


def memo_chunks(memo: str) -> list[str]:
    """Split ``memo`` into ≤64-byte strings without cutting a UTF-8 character.

    Mirrors Maya's own ``Memo.Split``: a cut inside a multibyte character
    would make an invalid metadata string the ledger rejects.
    """
    chunks: list[str] = []
    current = ""
    for char in memo:
        if current and len((current + char).encode()) > METADATA_CHUNK:
            chunks.append(current)
            current = ""
        current += char
    chunks.append(current)
    return chunks


def metadata_for_memo(memo: str) -> dict:
    return {CIP20_LABEL: {"msg": memo_chunks(memo)}}


def memo_from_metadata(metadata: object) -> bytes | None:
    """The CIP-20 message in decoded auxiliary data, or None if there is none."""
    if not isinstance(metadata, dict):
        return None
    message = metadata.get(CIP20_LABEL)
    if not isinstance(message, dict) or not isinstance(message.get("msg"), list):
        return None
    return "".join(message["msg"]).encode()


# --- serialization -----------------------------------------------------------


def _blake2b_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()


def encode_aux(memo: str) -> bytes:
    return cbor2.dumps(metadata_for_memo(memo))


def encode_body(tx: UnsignedTx) -> bytes:
    inputs = sorted((i.txid, i.index) for i in tx.inputs)
    body: dict[int, object] = {
        0: cbor2.CBORTag(SET_TAG, [list(i) for i in inputs]),
        1: [[o.address, o.lovelace] for o in tx.outputs],
        2: tx.fee,
        3: tx.ttl,
    }
    if tx.memo is not None:
        body[7] = _blake2b_256(encode_aux(tx.memo))
    return cbor2.dumps(body)


def txid(tx: UnsignedTx) -> bytes:
    return _blake2b_256(encode_body(tx))


def serialize(tx: UnsignedTx, witnesses: list[tuple[bytes, bytes]]) -> bytes:
    """The signed transaction: ``[body, witness_set, valid, auxiliary_data]``.

    The body is spliced in as the exact bytes :func:`txid` hashed.
    """
    witness_set = cbor2.dumps(
        {0: cbor2.CBORTag(SET_TAG, [list(w) for w in sorted(witnesses)])}
    )
    aux = encode_aux(tx.memo) if tx.memo is not None else cbor2.dumps(None)
    return b"\x84" + encode_body(tx) + witness_set + cbor2.dumps(True) + aux


# --- fees and min-ADA --------------------------------------------------------


def min_fee(size: int, params: ProtocolParams) -> int:
    return params.min_fee_a * size + params.min_fee_b


def min_lovelace(output: TxOut, params: ProtocolParams) -> int:
    """The least an output may hold (Babbage+ ``coinsPerUTxOByte`` rule)."""
    size = len(cbor2.dumps([output.address, output.lovelace]))
    return (UTXO_ENTRY_OVERHEAD + size) * params.coins_per_utxo_byte


def required_fee(tx: UnsignedTx, n_witnesses: int, params: ProtocolParams) -> int:
    """The minimum fee for ``tx`` once signed by ``n_witnesses`` keys.

    Sized on placeholder witnesses (a vkey and a signature are fixed-length),
    and iterated because the fee's own CBOR length is part of the size it
    pays for; it settles within a couple of rounds.
    """
    dummy = [(bytes([i]) * 32, bytes(64)) for i in range(n_witnesses)]
    fee = 0
    while True:
        size = len(serialize(dataclasses.replace(tx, fee=fee), dummy))
        needed = min_fee(size, params)
        if needed <= fee:
            return fee
        fee = needed


# --- the neutral reader ------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ParsedTx:
    txid: bytes
    inputs: tuple[TxIn, ...]
    outputs: tuple[tuple[str, int], ...]  # (bech32 address, lovelace)
    fee: int
    memo: bytes | None
    witnesses: tuple[tuple[bytes, bytes], ...]


def _set_items(value: Any) -> list:  # noqa: ANN401 (whatever cbor2 decoded)
    if isinstance(value, cbor2.CBORTag) and value.tag == SET_TAG:
        value = value.value
    return list(value)  # a list, or a frozenset cbor2 made of a tagged set


def parse(raw: bytes) -> ParsedTx:
    """Decode a serialized transaction, checking its memo is bound to the body.

    The txid is hashed over the body bytes as found in ``raw``. Auxiliary data
    the body does not commit to (or commits to with a different hash) raises:
    a node would reject it, and a memo that is not bound is not the memo.
    """
    if raw[:1] != b"\x84":
        raise ValueError("not a 4-element Cardano transaction")
    stream = io.BytesIO(raw[1:])
    decoder = cbor2.CBORDecoder(stream)
    body = decoder.decode()
    body_bytes = raw[1 : 1 + stream.tell()]
    witness_set = decoder.decode()
    decoder.decode()  # is_valid
    aux_start = stream.tell()
    aux = decoder.decode()
    aux_bytes = raw[1 + aux_start : 1 + stream.tell()]

    memo = None
    if aux is not None:
        if body.get(7) != _blake2b_256(aux_bytes):
            raise ValueError("auxiliary data does not match the body's hash")
        memo = memo_from_metadata(aux)
    elif 7 in body:
        raise ValueError("body commits to auxiliary data the tx does not carry")
    outputs = []
    for output in body[1]:
        # Only the plain [address, lovelace] shape this module writes; a map
        # output or a multi-asset value is not something to half-read.
        if not (
            isinstance(output, list)
            and len(output) == 2
            and isinstance(output[0], bytes)
            and isinstance(output[1], int)
        ):
            raise ValueError(f"unsupported output shape: {output!r}")
        outputs.append((address_string(output[0]), output[1]))
    return ParsedTx(
        txid=_blake2b_256(body_bytes),
        inputs=tuple(TxIn(bytes(t), i) for t, i in _set_items(body[0])),
        outputs=tuple(outputs),
        fee=body[2],
        memo=memo,
        witnesses=tuple((bytes(k), bytes(s)) for k, s in _set_items(witness_set[0])),
    )
