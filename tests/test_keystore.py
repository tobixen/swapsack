"""Tests for the encrypted keystore.

KDF cost (``n``) is forced low here so the suite stays fast; production uses the
module default.
"""

import base64
import json

import pytest

from cryptoswap_wallet.keystore import HdKey, Keystore, KeystoreError, RawKey, Secret

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


def test_v1_keystore_strips_bip39_passphrase(tmp_path):
    # A v1 keystore stored a BIP-39 passphrase but never applied it to
    # derivation, so funds are at empty-passphrase addresses. On load the
    # passphrase must be dropped, so v2 derivation keeps deriving those same
    # addresses (nothing in view shifts when the bug is fixed).
    path = tmp_path / "ks.json"
    ks = Keystore()
    ks.add_hd("withpw", MNEMONIC, passphrase="extra-word")
    ks.save(path, PW, n=LOW_N)
    env = json.loads(path.read_text())
    env["version"] = 1  # simulate a legacy (pre-fix) keystore
    path.write_text(json.dumps(env))

    loaded = Keystore.load(path, PW)
    assert loaded.entries[0].passphrase is None


def test_v1_passphrase_strip_warns_on_stderr(tmp_path, capsys):
    # The strip is deliberate but destroys a stored secret on the next save —
    # it must not be silent: name the key and say why the passphrase was
    # dropped so the user can note it down before it is gone.
    path = tmp_path / "ks.json"
    ks = Keystore()
    ks.add_hd("withpw", MNEMONIC, passphrase="extra-word")
    ks.save(path, PW, n=LOW_N)
    env = json.loads(path.read_text())
    env["version"] = 1
    path.write_text(json.dumps(env))

    Keystore.load(path, PW)
    err = capsys.readouterr().err
    assert "withpw" in err
    assert "passphrase" in err
    assert "never applied" in err


def test_v1_keystore_without_passphrase_loads_silently(tmp_path, capsys):
    path = tmp_path / "ks.json"
    ks = Keystore()
    ks.add_hd("plain", MNEMONIC)
    ks.save(path, PW, n=LOW_N)
    env = json.loads(path.read_text())
    env["version"] = 1
    path.write_text(json.dumps(env))

    Keystore.load(path, PW)
    assert capsys.readouterr().err == ""


def test_load_honours_stored_key_length(tmp_path):
    # The KDF key length is persisted in kdf_params; load must derive with the
    # stored value, not a hardcoded constant, so a keystore written under a
    # different KEY_LEN still decrypts. Craft an envelope with a non-default
    # length and confirm it round-trips.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    from cryptoswap_wallet import keystore as ks_mod

    path = tmp_path / "ks.json"
    make().save(path, PW, n=LOW_N)
    env = json.loads(path.read_text())

    length = 16  # deliberately not the default KEY_LEN (32)
    salt = base64.b64decode(env["salt"])
    nonce = base64.b64decode(env["nonce"])

    def key_of(key_len: int) -> bytes:
        kdf = Scrypt(
            salt=salt, length=key_len, n=LOW_N, r=ks_mod.SCRYPT_R, p=ks_mod.SCRYPT_P
        )
        return kdf.derive(PW.encode())

    plaintext = AESGCM(key_of(ks_mod.KEY_LEN)).decrypt(
        nonce, base64.b64decode(env["ciphertext"]), None
    )
    env["kdf_params"]["length"] = length
    env["ciphertext"] = base64.b64encode(
        AESGCM(key_of(length)).encrypt(nonce, plaintext, None)
    ).decode()
    path.write_text(json.dumps(env))

    loaded = Keystore.load(path, PW)
    assert loaded.labels() == ["trustwallet", "paper-btc"]


def test_save_writes_version_2(tmp_path):
    path = tmp_path / "ks.json"
    make().save(path, PW, n=LOW_N)
    assert json.loads(path.read_text())["version"] == 2


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
