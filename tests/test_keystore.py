"""Tests for the encrypted keystore.

KDF cost (``n``) is forced low here so the suite stays fast; production uses the
module default.
"""

import base64
import json

import pytest

from cryptoswap.keystore import HdKey, Keystore, KeystoreError, RawKey, Secret

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)
WIF = "L1aW4aubDFB7yfras2S1mN3bqg9nwySY8nkoLmJebSLD5BWv3ENZ"
PW = "correct horse battery staple"
LOW_N = 1024  # fast KDF for tests only


def make() -> Keystore:
    ks = Keystore()
    ks.add_hd("trustwallet", MNEMONIC)
    ks.add_raw("paper-btc", "BTC", WIF)
    return ks


def test_roundtrip(tmp_path):
    path = tmp_path / "ks.json"
    make().save(path, PW, n=LOW_N)
    ks = Keystore.load(path, PW)
    hd = next(e for e in ks.entries if isinstance(e, HdKey))
    raw = next(e for e in ks.entries if isinstance(e, RawKey))
    assert hd.label == "trustwallet"
    assert hd.mnemonic.reveal() == MNEMONIC
    assert hd.passphrase is None
    assert raw.chain == "BTC"
    assert raw.secret.reveal() == WIF


def test_hd_passphrase_roundtrip(tmp_path):
    path = tmp_path / "ks.json"
    ks = Keystore()
    ks.add_hd("withpw", MNEMONIC, passphrase="extra-word")
    ks.save(path, PW, n=LOW_N)
    loaded = Keystore.load(path, PW)
    assert loaded.entries[0].passphrase.reveal() == "extra-word"


def test_wrong_passphrase_raises(tmp_path):
    path = tmp_path / "ks.json"
    make().save(path, PW, n=LOW_N)
    with pytest.raises(KeystoreError):
        Keystore.load(path, "wrong passphrase")


def test_corrupted_ciphertext_raises(tmp_path):
    path = tmp_path / "ks.json"
    make().save(path, PW, n=LOW_N)
    env = json.loads(path.read_text())
    blob = bytearray(base64.b64decode(env["ciphertext"]))
    blob[0] ^= 0xFF
    env["ciphertext"] = base64.b64encode(bytes(blob)).decode()
    path.write_text(json.dumps(env))
    with pytest.raises(KeystoreError):
        Keystore.load(path, PW)


def test_file_permissions_are_0600(tmp_path):
    path = tmp_path / "ks.json"
    make().save(path, PW, n=LOW_N)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "ks.json"
    make().save(path, PW, n=LOW_N)
    make().save(path, PW, n=LOW_N)  # overwrite an existing keystore
    assert Keystore.load(path, PW).labels() == ["trustwallet", "paper-btc"]
    # No temp/partial files left behind in the directory.
    assert [p.name for p in tmp_path.iterdir()] == ["ks.json"]


def test_duplicate_label_rejected():
    ks = Keystore()
    ks.add_hd("dup", MNEMONIC)
    with pytest.raises(KeystoreError):
        ks.add_raw("dup", "BTC", WIF)


def test_secrets_absent_from_repr():
    assert "supersecret" not in repr(Secret("supersecret"))
    hd = HdKey("l", Secret(MNEMONIC))
    assert MNEMONIC not in repr(hd)
    raw = RawKey("l", "BTC", Secret(WIF))
    assert WIF not in repr(raw)
