"""Encrypted keystore holding HD seeds and raw private keys.

Secrets are encrypted at rest with AES-256-GCM under a key derived from a
passphrase via scrypt. The plaintext is a JSON document of entries; the on-disk
file is a JSON envelope carrying the KDF parameters, salt, nonce and ciphertext.

Security notes:
  - Secret material is wrapped in :class:`Secret` so it never appears in reprs
    or tracebacks.
  - The file is written with ``0600`` permissions.
  - This is a hot-wallet keystore for *small* funds: decrypting needs the
    passphrase, so any automated process holding the passphrase effectively
    holds the keys. Do not store meaningful funds here.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import os
import tempfile
from pathlib import Path
from typing import Any, ClassVar

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ENVELOPE_VERSION = 1
DEFAULT_N = 2**15  # scrypt cost; ~32 MB, sub-100ms to derive
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12


class KeystoreError(RuntimeError):
    """Raised on a wrong passphrase, corrupted file, or invalid entry."""


class Secret:
    """A string secret that refuses to reveal itself in reprs/tracebacks."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclasses.dataclass
class HdKey:
    """An HD wallet entry: a BIP-39 mnemonic with optional passphrase."""

    label: str
    mnemonic: Secret
    passphrase: Secret | None = None
    kind: ClassVar[str] = "hd"


@dataclasses.dataclass
class RawKey:
    """A standalone private key for a single chain (e.g. a BTC WIF)."""

    label: str
    chain: str
    secret: Secret
    kind: ClassVar[str] = "raw"


KeyEntry = HdKey | RawKey


def _entry_to_dict(entry: KeyEntry) -> dict[str, Any]:
    if isinstance(entry, HdKey):
        return {
            "kind": "hd",
            "label": entry.label,
            "mnemonic": entry.mnemonic.reveal(),
            "passphrase": entry.passphrase.reveal() if entry.passphrase else None,
        }
    return {
        "kind": "raw",
        "label": entry.label,
        "chain": entry.chain,
        "secret": entry.secret.reveal(),
    }


def _entry_from_dict(data: dict[str, Any]) -> KeyEntry:
    kind = data.get("kind")
    if kind == "hd":
        pw = data.get("passphrase")
        return HdKey(
            label=data["label"],
            mnemonic=Secret(data["mnemonic"]),
            passphrase=Secret(pw) if pw else None,
        )
    if kind == "raw":
        return RawKey(
            label=data["label"], chain=data["chain"], secret=Secret(data["secret"])
        )
    raise KeystoreError(f"unknown key entry kind {kind!r}")


@dataclasses.dataclass
class Keystore:
    """A collection of key entries, encryptable to / decryptable from disk."""

    entries: list[KeyEntry] = dataclasses.field(default_factory=list)

    def labels(self) -> list[str]:
        return [e.label for e in self.entries]

    def _require_unique(self, label: str) -> None:
        if label in self.labels():
            raise KeystoreError(f"duplicate key label {label!r}")

    def add_hd(self, label: str, mnemonic: str, passphrase: str | None = None) -> HdKey:
        self._require_unique(label)
        entry = HdKey(
            label=label,
            mnemonic=Secret(mnemonic),
            passphrase=Secret(passphrase) if passphrase else None,
        )
        self.entries.append(entry)
        return entry

    def add_raw(self, label: str, chain: str, secret: str) -> RawKey:
        self._require_unique(label)
        entry = RawKey(label=label, chain=chain, secret=Secret(secret))
        self.entries.append(entry)
        return entry

    def save(
        self, path: str | os.PathLike[str], passphrase: str, *, n: int = DEFAULT_N
    ) -> None:
        plaintext = json.dumps(
            {"entries": [_entry_to_dict(e) for e in self.entries]}
        ).encode()
        salt = os.urandom(SALT_LEN)
        nonce = os.urandom(NONCE_LEN)
        key = _derive_key(passphrase, salt, n=n)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
        envelope = {
            "version": ENVELOPE_VERSION,
            "kdf": "scrypt",
            "kdf_params": {"n": n, "r": SCRYPT_R, "p": SCRYPT_P, "length": KEY_LEN},
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        }
        data = json.dumps(envelope, indent=2).encode()
        _atomic_write(Path(path), data)

    @classmethod
    def load(cls, path: str | os.PathLike[str], passphrase: str) -> Keystore:
        try:
            envelope = json.loads(Path(path).read_text())
            salt = base64.b64decode(envelope["salt"])
            nonce = base64.b64decode(envelope["nonce"])
            ciphertext = base64.b64decode(envelope["ciphertext"])
            params = envelope["kdf_params"]
        except (OSError, ValueError, KeyError) as exc:
            raise KeystoreError(f"cannot read keystore: {exc}") from exc

        key = _derive_key(passphrase, salt, n=params["n"], r=params["r"], p=params["p"])
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise KeystoreError("wrong passphrase or corrupted keystore") from exc

        payload = json.loads(plaintext)
        return cls(entries=[_entry_from_dict(d) for d in payload["entries"]])


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp file in same dir + os.replace).

    A crash, full disk, or ^C can no longer leave a truncated/corrupt keystore:
    the old file stays intact until the fully-written, fsync'd temp is renamed
    over it. The temp is created 0600 so secrets are never world-readable.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _derive_key(
    passphrase: str,
    salt: bytes,
    *,
    n: int = DEFAULT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> bytes:
    kdf = Scrypt(salt=salt, length=KEY_LEN, n=n, r=r, p=p)
    return kdf.derive(passphrase.encode())
