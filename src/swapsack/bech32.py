"""bech32 and bech32m (BIP-173 / BIP-350) — a minimal, self-contained codec.

Two callers with different needs share this: Cosmos-SDK account addresses
(``maya1…`` / ``thor1…``, always plain bech32) in :mod:`swapsack.chains.cosmos`,
and the destination-address checksum check in :mod:`swapsack.addresses`, which
additionally has to accept segwit — where the checksum constant depends on the
witness version (v0 uses bech32, v1+ / taproot uses bech32m).

Deliberately dependency-free: the address validator runs before any chain
library is needed, and importing one there would pull in bitcoinlib (and
SQLAlchemy) just to check a recipient for typos. Verified against the reference
test vectors and real on-chain addresses for every HRP in use.
"""

from __future__ import annotations

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

# The final polymod a well-formed string produces, per spec variant.
BECH32 = 1
BECH32M = 0x2BC830A3


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def convertbits(data: bytes | list[int], frm: int, to: int, pad: bool) -> list[int]:
    """Regroup ``data`` from ``frm``-bit to ``to``-bit values (8↔5 for bech32)."""
    acc = bits = 0
    ret: list[int] = []
    maxv = (1 << to) - 1
    for value in data:
        if value < 0 or value >> frm:
            raise ValueError("invalid value for bech32 base conversion")
        acc = (acc << frm) | value
        bits += frm
        while bits >= to:
            bits -= to
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (to - bits)) & maxv)
    elif bits >= frm or (acc << (to - bits)) & maxv:
        raise ValueError("invalid padding in bech32 base conversion")
    return ret


def split(address: str) -> tuple[str, list[int], int]:
    """Split into ``(hrp, data values incl. checksum, the variant it matches)``.

    Raises :class:`ValueError` unless the checksum is valid under *one* of the
    two variants; which one is left to the caller, since only the caller knows
    which is expected.
    """
    pos = address.rfind("1")
    if pos < 1 or pos + 7 > len(address):
        raise ValueError(f"not a bech32 address: {address!r}")
    hrp = address[:pos]
    try:
        values = [CHARSET.index(c) for c in address[pos + 1 :]]
    except ValueError:
        raise ValueError(f"invalid bech32 character in {address!r}") from None
    variant = _polymod(_hrp_expand(hrp) + values)
    if variant not in (BECH32, BECH32M):
        raise ValueError(f"bad bech32 checksum in {address!r}")
    return hrp, values, variant


def bech32_encode(hrp: str, data: bytes) -> str:
    """Encode a byte string (e.g. a 20-byte account hash) as a bech32 address."""
    values = convertbits(data, 8, 5, pad=True)
    checksum_input = _hrp_expand(hrp) + values + [0, 0, 0, 0, 0, 0]
    polymod = _polymod(checksum_input) ^ BECH32
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in values + checksum)


def bech32_decode(address: str) -> tuple[str, bytes]:
    """Inverse of :func:`bech32_encode`; raises :class:`ValueError` on bad checksum.

    Plain bech32 only — a bech32m string is rejected, since the two encode
    different things and silently accepting either would defeat the point.
    """
    hrp, values, variant = split(address)
    if variant != BECH32:
        raise ValueError(f"bad bech32 checksum in {address!r} (it is bech32m)")
    return hrp, bytes(convertbits(values[:-6], 5, 8, pad=False))


def segwit_ok(address: str, hrp: str) -> bool:
    """True if ``address`` is a well-formed segwit address for ``hrp``.

    Checks the whole BIP-173/350 shape, not just the polymod: the witness
    version selects which checksum constant is correct (v0 → bech32, v1+ →
    bech32m), so a taproot address with a v0 checksum is malformed even though
    its polymod is "valid".
    """
    try:
        got_hrp, values, variant = split(address)
    except ValueError:
        return False
    if got_hrp != hrp or len(values) < 7:
        return False
    witness_version = values[0]
    if witness_version > 16:
        return False
    if variant != (BECH32 if witness_version == 0 else BECH32M):
        return False
    try:
        program = convertbits(values[1:-6], 5, 8, pad=False)
    except ValueError:
        return False
    if not 2 <= len(program) <= 40:
        return False
    # v0 is only ever P2WPKH (20 bytes) or P2WSH (32); later versions are open.
    return witness_version != 0 or len(program) in (20, 32)
