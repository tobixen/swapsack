"""Zcash v4 (Sapling-format) transparent transaction build / sighash / sign.

bitcoinlib cannot produce Zcash signatures (see docs/zcash.md): since
Overwinter the transaction format carries an ``fOverwintered`` header, a
version-group id, an expiry height and shielded sections, and the signature
hash is ZIP-243 — BLAKE2b-256 personalized with the *consensus branch id* of
the active network upgrade, over a BIP143-like-but-different preimage. This
module implements exactly the transparent-only subset the wallet needs: v4
transactions with P2PKH inputs/outputs and empty shielded sections.

Correctness anchors (test_zcash.py): a real mainnet v4 transaction round-trips
byte-identically through ``parse_v4``/``serialize_v4``, and its embedded ECDSA
signature verifies against the ZIP-243 digest computed here — so the sighash
matches what real Zcash wallets sign, not merely our own reading of the spec.
The consensus branch id is never hardcoded in the adapter: it is fetched live
from lightwalletd, because a stale id after a network upgrade would make every
signature invalid.

v4 remains consensus-valid on mainnet (recent blocks still carry v4
transparent txs, which is where the test fixture comes from); moving to
v5/ZIP-244 is only needed if v4 is ever deprecated.
"""

from __future__ import annotations

import dataclasses
import hashlib
import struct

import base58
from coincurve import PrivateKey, PublicKey

V4_HEADER = 0x80000004  # fOverwintered | version 4
V4_VERSION_GROUP = 0x892F2085  # Sapling
SIGHASH_ALL = 1
PREFIX_T1 = b"\x1c\xb8"  # transparent P2PKH ("t1…")


class ZcashTxError(ValueError):
    """A transaction that cannot be parsed/built as transparent-only v4."""


# --- script / address helpers -------------------------------------------------


def address_to_script(address: str) -> bytes:
    """The P2PKH scriptPubKey for a transparent ``t1…`` address."""
    payload = base58.b58decode_check(address)
    if payload[:2] != PREFIX_T1 or len(payload) != 22:
        raise ZcashTxError(f"not a transparent t1 P2PKH address: {address}")
    return b"\x76\xa9\x14" + payload[2:] + b"\x88\xac"


def script_to_address(script: bytes) -> str | None:
    """The ``t1…`` address of a P2PKH scriptPubKey, or None for other scripts."""
    if (
        len(script) == 25
        and script[:3] == b"\x76\xa9\x14"
        and script[23:] == b"\x88\xac"
    ):
        return base58.b58encode_check(PREFIX_T1 + script[3:23]).decode()
    return None


# --- transaction structure ------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TxIn:
    prev_txid: bytes  # 32 bytes, tx-serialization order (little-endian)
    vout: int
    script_sig: bytes = b""
    sequence: int = 0xFFFFFFFF


@dataclasses.dataclass(frozen=True)
class TxOut:
    value: int  # zatoshis
    script: bytes


@dataclasses.dataclass(frozen=True)
class TxV4:
    inputs: tuple[TxIn, ...]
    outputs: tuple[TxOut, ...]
    lock_time: int = 0
    expiry_height: int = 0


def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    raise ZcashTxError(f"varint {n} out of the transparent-tx range")


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    v = b[i]
    if v < 0xFD:
        return v, i + 1
    if v == 0xFD:
        return int.from_bytes(b[i + 1 : i + 3], "little"), i + 3
    raise ZcashTxError("varint too large for a transparent tx")


def serialize_v4(tx: TxV4) -> bytes:
    """Serialize a transparent-only v4 transaction (empty shielded sections)."""
    out = struct.pack("<I", V4_HEADER) + struct.pack("<I", V4_VERSION_GROUP)
    out += _varint(len(tx.inputs))
    for i in tx.inputs:
        out += (
            i.prev_txid
            + struct.pack("<I", i.vout)
            + _varint(len(i.script_sig))
            + i.script_sig
            + struct.pack("<I", i.sequence)
        )
    out += _varint(len(tx.outputs))
    for o in tx.outputs:
        out += struct.pack("<Q", o.value) + _varint(len(o.script)) + o.script
    out += struct.pack("<I", tx.lock_time)
    out += struct.pack("<I", tx.expiry_height)
    out += struct.pack("<q", 0)  # valueBalance (no shielded value)
    out += b"\x00\x00\x00"  # empty vShieldedSpend, vShieldedOutput, vJoinSplit
    return out


def parse_v4(raw: bytes) -> TxV4:
    """Parse a transparent-only v4 transaction; reject anything shielded."""
    i = 0
    header = int.from_bytes(raw[i : i + 4], "little")
    vgroup = int.from_bytes(raw[i + 4 : i + 8], "little")
    i += 8
    if header != V4_HEADER or vgroup != V4_VERSION_GROUP:
        raise ZcashTxError(f"not a v4/Sapling tx (header {header:#x})")
    n_in, i = _read_varint(raw, i)
    inputs = []
    for _ in range(n_in):
        prev_txid = raw[i : i + 32]
        vout = int.from_bytes(raw[i + 32 : i + 36], "little")
        i += 36
        slen, i = _read_varint(raw, i)
        script_sig = raw[i : i + slen]
        i += slen
        sequence = int.from_bytes(raw[i : i + 4], "little")
        i += 4
        inputs.append(TxIn(prev_txid, vout, script_sig, sequence))
    n_out, i = _read_varint(raw, i)
    outputs = []
    for _ in range(n_out):
        value = int.from_bytes(raw[i : i + 8], "little")
        i += 8
        slen, i = _read_varint(raw, i)
        outputs.append(TxOut(value, raw[i : i + slen]))
        i += slen
    lock_time = int.from_bytes(raw[i : i + 4], "little")
    expiry_height = int.from_bytes(raw[i + 4 : i + 8], "little")
    value_balance = int.from_bytes(raw[i + 8 : i + 16], "little", signed=True)
    i += 16
    n_ss, i = _read_varint(raw, i)
    n_so, i = _read_varint(raw, i)
    n_js, i = _read_varint(raw, i)
    if value_balance != 0 or n_ss or n_so or n_js:
        raise ZcashTxError("tx carries shielded components; transparent-only here")
    if i != len(raw):
        raise ZcashTxError(f"trailing bytes after tx ({len(raw) - i})")
    return TxV4(tuple(inputs), tuple(outputs), lock_time, expiry_height)


def txid(raw: bytes) -> str:
    """The v4 txid: double-SHA256, displayed byte-reversed (as explorers show)."""
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()


# --- ZIP-243 sighash + signing --------------------------------------------------


def _blake2b256(person: bytes, data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32, person=person).digest()


def sighash_zip243(
    tx: TxV4, index: int, script_code: bytes, value: int, branch_id: int
) -> bytes:
    """The SIGHASH_ALL digest for ``tx.inputs[index]`` per ZIP-243.

    ``script_code``/``value`` are the spent output's scriptPubKey and amount;
    ``branch_id`` is the ACTIVE consensus branch id (from lightwalletd) — it is
    baked into the BLAKE2b personalization, so a stale id changes every digest.
    """
    prevouts = b"".join(i.prev_txid + struct.pack("<I", i.vout) for i in tx.inputs)
    sequences = b"".join(struct.pack("<I", i.sequence) for i in tx.inputs)
    outs = b"".join(
        struct.pack("<Q", o.value) + _varint(len(o.script)) + o.script
        for o in tx.outputs
    )
    zero32 = b"\x00" * 32
    inp = tx.inputs[index]
    preimage = (
        struct.pack("<I", V4_HEADER)
        + struct.pack("<I", V4_VERSION_GROUP)
        + _blake2b256(b"ZcashPrevoutHash", prevouts)
        + _blake2b256(b"ZcashSequencHash", sequences)
        + _blake2b256(b"ZcashOutputsHash", outs)
        + zero32  # hashJoinSplits (none)
        + zero32  # hashShieldedSpends (none)
        + zero32  # hashShieldedOutputs (none)
        + struct.pack("<I", tx.lock_time)
        + struct.pack("<I", tx.expiry_height)
        + struct.pack("<q", 0)  # valueBalance
        + struct.pack("<I", SIGHASH_ALL)
        + inp.prev_txid
        + struct.pack("<I", inp.vout)
        + _varint(len(script_code))
        + script_code
        + struct.pack("<Q", value)
        + struct.pack("<I", inp.sequence)
    )
    person = b"ZcashSigHash" + struct.pack("<I", branch_id)
    return _blake2b256(person, preimage)


def sign_transparent(
    tx: TxV4,
    spent: list[tuple[bytes, int]],  # per input: (scriptPubKey, value) being spent
    privkeys: list[bytes],
    branch_id: int,
) -> TxV4:
    """Sign every input (SIGHASH_ALL) and return the tx with scriptSigs filled.

    Refuses to return anything that does not verify: each produced signature is
    checked against the digest with the corresponding public key before the
    transaction is considered signed (mirrors the bitcoinlib-path safeguard).
    """
    if not (len(tx.inputs) == len(spent) == len(privkeys)):
        raise ZcashTxError("inputs, spent outputs and keys must align 1:1")
    signed = []
    for index, (inp, (script_code, value), priv) in enumerate(
        zip(tx.inputs, spent, privkeys, strict=True)
    ):
        if script_to_address(script_code) is None:
            raise ZcashTxError(f"input {index} spends a non-P2PKH output")
        digest = sighash_zip243(tx, index, script_code, value, branch_id)
        key = PrivateKey(priv)
        der = key.sign(digest, hasher=None)
        pub = key.public_key.format(compressed=True)
        if not PublicKey(pub).verify(der, digest, hasher=None):
            raise ZcashTxError(f"self-check failed: input {index} signature invalid")
        sig = der + bytes([SIGHASH_ALL])
        script_sig = bytes([len(sig)]) + sig + bytes([len(pub)]) + pub
        signed.append(dataclasses.replace(inp, script_sig=script_sig))
    return dataclasses.replace(tx, inputs=tuple(signed))
