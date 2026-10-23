"""Cardano keys: Icarus master key, BIP32-Ed25519 derivation, Shelley addresses.

Cardano does not use BIP32/secp256k1. Shelley wallets (Yoroi, Eternl, Lace,
Daedalus) derive along CIP-1852 (``m/1852'/1815'/account'/role/index``) with
BIP32-Ed25519 "V2" (Khovratovich & Law), from a master key built the *Icarus*
way: PBKDF2 over the mnemonic's **entropy**, not over the BIP39 seed every other
chain here starts from. An extended private key is ``(kL, kR)`` — ``kL`` is the
Ed25519 scalar itself, not a seed it is hashed from — so signing cannot go
through a standard Ed25519 API and is done here on libsodium's scalar and point
primitives (via PyNaCl). The result is a plain Ed25519 signature: anything that
verifies Ed25519 verifies it, which is what the tests and :meth:`sign` lean on.

Golden vectors (pycardano, generated offline) pin every step in
``tests/test_cardano.py``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac

from bitcoinlib.mnemonic import Mnemonic
from nacl import bindings
from nacl.signing import VerifyKey

from swapsack.bech32 import bech32_encode

HARDENED = 0x8000_0000
ICARUS_ITERATIONS = 4096
# Shelley address header: type 0 (payment key hash + stake key hash) in the
# high nibble, network id 1 (mainnet) in the low one.
BASE_ADDRESS_MAINNET = 0x01
ADDRESS_HRP = "addr"


def _reduce(scalar: bytes) -> bytes:
    """``scalar`` mod the group order L, as 32 little-endian bytes."""
    return bindings.crypto_core_ed25519_scalar_reduce(scalar.ljust(64, b"\0"))


def _point(scalar: bytes) -> bytes:
    """``scalar``·B, unclamped (``kL`` is already clamped at the root)."""
    return bindings.crypto_scalarmult_ed25519_base_noclamp(_reduce(scalar))


def _le(data: bytes) -> int:
    return int.from_bytes(data, "little")


def blake2b_224(data: bytes) -> bytes:
    """The 28-byte hash Cardano uses for key hashes (and so for addresses)."""
    return hashlib.blake2b(data, digest_size=28).digest()


@dataclasses.dataclass(frozen=True)
class ExtendedKey:
    """A BIP32-Ed25519 extended private key: scalar ``kL``, nonce key ``kR``."""

    kl: bytes
    kr: bytes
    chain_code: bytes

    @property
    def public_key(self) -> bytes:
        return _point(self.kl)

    def child(self, index: int) -> ExtendedKey:
        """BIP32-Ed25519 V2 child: hardened from the private key, soft from A."""
        i = index.to_bytes(4, "little")
        if index >= HARDENED:
            data = self.kl + self.kr + i
            z = hmac.digest(self.chain_code, b"\x00" + data, "sha512")
            c = hmac.digest(self.chain_code, b"\x01" + data, "sha512")
        else:
            data = self.public_key + i
            z = hmac.digest(self.chain_code, b"\x02" + data, "sha512")
            c = hmac.digest(self.chain_code, b"\x03" + data, "sha512")
        kl = 8 * _le(z[:28]) + _le(self.kl)
        if kl >= 2**256:  # V2 does no reduction; overflow means a broken key
            raise ValueError("BIP32-Ed25519 derivation overflowed kL")
        kr = (_le(z[32:]) + _le(self.kr)) % 2**256
        return ExtendedKey(
            kl=kl.to_bytes(32, "little"),
            kr=kr.to_bytes(32, "little"),
            chain_code=c[32:],
        )

    def sign(self, message: bytes) -> bytes:
        """Ed25519-sign ``message`` with the extended key; self-verified.

        The standard construction with the nonce taken from ``kR`` instead of
        from a hashed seed: r = H(kR‖M), R = r·B, S = r + H(R‖A‖M)·kL (mod L).
        """
        public = self.public_key
        r = _reduce(hashlib.sha512(self.kr + message).digest())
        big_r = bindings.crypto_scalarmult_ed25519_base_noclamp(r)
        h = _reduce(hashlib.sha512(big_r + public + message).digest())
        s = bindings.crypto_core_ed25519_scalar_add(
            r, bindings.crypto_core_ed25519_scalar_mul(h, _reduce(self.kl))
        )
        signature = big_r + s
        # Raises BadSignatureError: a key bug must not reach a broadcast.
        VerifyKey(public).verify(message, signature)
        return signature


def root_key(mnemonic: str, passphrase: str = "") -> ExtendedKey:
    """The Icarus master key (CIP-3) for ``mnemonic``.

    The BIP39 passphrase is the PBKDF2 password, as raw UTF-8 — what Yoroi,
    Eternl and pycardano do — so a wallet restored there with the same words
    and passphrase sees the same addresses.
    """
    entropy = Mnemonic().to_entropy(mnemonic)
    data = bytearray(
        hashlib.pbkdf2_hmac(
            "sha512", passphrase.encode(), bytes(entropy), ICARUS_ITERATIONS, 96
        )
    )
    data[0] &= 0b1111_1000
    data[31] &= 0b0001_1111
    data[31] |= 0b0100_0000
    return ExtendedKey(
        kl=bytes(data[:32]), kr=bytes(data[32:64]), chain_code=bytes(data[64:])
    )


def parse_path(path: str) -> list[int]:
    """``m/1852'/1815'/0'/0/3`` → child indices (``'`` or ``h`` = hardened)."""
    parts = path.split("/")
    if parts[0] != "m":
        raise ValueError(f"derivation path must start at 'm': {path!r}")
    indices = []
    for part in parts[1:]:
        hardened = part.endswith(("'", "h"))
        index = int(part[:-1] if hardened else part)
        indices.append(index + HARDENED if hardened else index)
    return indices


def derive(root: ExtendedKey, path: str) -> ExtendedKey:
    key = root
    for index in parse_path(path):
        key = key.child(index)
    return key


def base_address(payment_pub: bytes, stake_pub: bytes) -> str:
    """The mainnet Shelley base address (``addr1q…``) for two public keys."""
    payload = (
        bytes([BASE_ADDRESS_MAINNET])
        + blake2b_224(payment_pub)
        + blake2b_224(stake_pub)
    )
    return bech32_encode(ADDRESS_HRP, payload)
