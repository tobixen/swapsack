"""Cardano (ADA): CIP-1852 key derivation, base addresses, Koios balance scan."""

from __future__ import annotations

import pytest

pytest.importorskip("bitcoinlib")

from nacl.signing import VerifyKey  # noqa: E402

from conftest import FakeSession  # noqa: E402
from swapsack.chains.base import AddressInfo  # noqa: E402
from swapsack.chains.cardano import (  # noqa: E402
    LOVELACE_TO_UNITS,
    AdaAdapter,
    parse_address_info,
)
from swapsack.chains.cardano_keys import base_address, derive, root_key  # noqa: E402

# Standard BIP39 test mnemonic. Golden values from pycardano 0.19.2 (generated
# offline, never imported here); the m/.../0/0 base address is also the one
# Yoroi/Eternl show for this mnemonic.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon about"
)
ROOT_XPRV = (
    "60ce7dbec3616e9fc17e0c32578b3f380337b1b61a1f3cb9651aee30670e6f53"
    "970419a23a2e4e4082d12bf78faa8645dfc882cee2ae7179e2b07fe88098abb2"
)
ROOT_CHAIN_CODE = "072310084784c7308182dbbdb1449b2706586f1ff5cbf13d15e9b6e78c15f067"
GOLDEN = {
    "m/1852'/1815'/0'/0/0": (
        "7ea09a34aebb13c9841c71397b1cabfec5ddf950405293dee496cac2f437480a",
        "addr1qy8ac7qqy0vtulyl7wntmsxc6wex80gvcyjy33qffrhm7sh927ysx5sftuw0dlft"
        "05dz3c7revpf7jx0xnlcjz3g69mq4afdhv",
    ),
    "m/1852'/1815'/0'/0/1": (
        "86e24e36b7f0d1ad2afa266179207eb04f02642898bb58a2b6f80e491b658060",
        "addr1qyz85693g4fr8c55mfyxhae8j2u04pydxrgqr73vmwpx3a8927ysx5sftuw0dlft"
        "05dz3c7revpf7jx0xnlcjz3g69mqu2c0f9",
    ),
    "m/1852'/1815'/0'/1/0": (
        "03bc23b895900a001548d2f6ce9da1dd9a43ae15f4af9ee85a5e937b291b39c0",
        "addr1qykhadtnvjpkxh76xgr0mu4huc9tg800x2sxsqemn9uz8jh927ysx5sftuw0dlft"
        "05dz3c7revpf7jx0xnlcjz3g69mq28kufu",
    ),
}
STAKE_PUB = "012f5dc3115b8a07981e6e50f5a671e2c6fbb26c3ffde1cd1dcaf40a7fe8f160"
# pycardano's ExtendedSigningKey.sign(b"swapsack") with the m/.../0/0 key.
GOLDEN_SIGNATURE = (
    "895e1d8c01276fb8261977bc4adadb8647ddc7098bdbbd95f8ade7a953976ac4"
    "fd1e6b442a21ca8a5edb088536f3f6225da1260d915fa70015d6a6c868e8a30a"
)
# With BIP39 passphrase "TREZOR" (Icarus feeds it to PBKDF2 as the password).
GOLDEN_TREZOR_0 = (
    "addr1qx3mdledzm0zs4d05yc7lntytr7lzhqacrdcewlezgdnwf7ye0j3j44kfknjyy7hz4f"
    "kscee0m6e3ng202hvn3k2rk8q2p4dcs"
)


def test_root_key_matches_icarus_golden_vector():
    root = root_key(TEST_MNEMONIC)
    assert (root.kl + root.kr).hex() == ROOT_XPRV
    assert root.chain_code.hex() == ROOT_CHAIN_CODE


@pytest.mark.parametrize("path", sorted(GOLDEN))
def test_derived_public_keys_match_golden(path):
    assert derive(root_key(TEST_MNEMONIC), path).public_key.hex() == GOLDEN[path][0]


def test_stake_key_matches_golden():
    stake = derive(root_key(TEST_MNEMONIC), "m/1852'/1815'/0'/2/0")
    assert stake.public_key.hex() == STAKE_PUB


@pytest.mark.parametrize("path", sorted(GOLDEN))
def test_adapter_derives_golden_base_addresses(path):
    assert AdaAdapter().derive_address(TEST_MNEMONIC, path) == GOLDEN[path][1]


def test_base_address_from_raw_keys():
    payment = bytes.fromhex(GOLDEN["m/1852'/1815'/0'/0/0"][0])
    assert (
        base_address(payment, bytes.fromhex(STAKE_PUB))
        == (GOLDEN["m/1852'/1815'/0'/0/0"][1])
    )


def test_bip39_passphrase_changes_the_address():
    adapter = AdaAdapter(bip39_passphrase="TREZOR")
    assert adapter.derive_address(TEST_MNEMONIC) == GOLDEN_TREZOR_0


def test_extended_key_signature_matches_golden_and_verifies():
    key = derive(root_key(TEST_MNEMONIC), "m/1852'/1815'/0'/0/0")
    signature = key.sign(b"swapsack")
    assert signature.hex() == GOLDEN_SIGNATURE
    # A plain Ed25519 verifier accepts it: what a Cardano node checks.
    VerifyKey(key.public_key).verify(b"swapsack", signature)


def test_path_must_start_at_the_root():
    with pytest.raises(ValueError, match="path"):
        derive(root_key(TEST_MNEMONIC), "1852'/1815'")


class FakeJson:
    """A Koios answer: a 200 carrying ``body``."""

    status_code = 200

    def __init__(self, body: object) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self._body


# --- Koios address_info parsing (units are 1e-8 ADA, Maya's) ----------------


def test_lovelace_scale_to_maya_units():
    assert LOVELACE_TO_UNITS == 100


def test_parse_address_info_absent_address_has_no_history():
    assert parse_address_info([]) == AddressInfo(
        has_history=False, confirmed=0, pending=0
    )


def test_parse_address_info_used_but_empty_counts_as_history():
    row = {"address": "addr1x", "balance": "0", "utxo_set": []}
    assert parse_address_info([row]) == AddressInfo(
        has_history=True, confirmed=0, pending=0
    )


def test_parse_address_info_scales_lovelace_to_maya_units():
    row = {"address": "addr1x", "balance": "2457959347", "utxo_set": []}
    assert parse_address_info([row]).confirmed == 2457959347 * 100


def test_wallet_balance_scans_and_sums(monkeypatch):
    adapter = AdaAdapter()
    funded = {GOLDEN["m/1852'/1815'/0'/0/0"][1]: 5_000_000 * 100}

    def fake_info(address):
        if address in funded:
            return AddressInfo(has_history=True, confirmed=funded[address], pending=0)
        return AddressInfo(has_history=False, confirmed=0, pending=0)

    monkeypatch.setattr(adapter, "address_info", fake_info)
    report = adapter.wallet_balance(TEST_MNEMONIC)
    assert report.symbol == "ADA"
    assert report.decimals == 8
    assert report.confirmed == 5_000_000 * 100
    assert report.addresses == (GOLDEN["m/1852'/1815'/0'/0/0"][1],)


def test_address_info_posts_to_koios():
    adapter = AdaAdapter("https://koios.example.com/api/v1")
    session = FakeSession(
        FakeJson([{"address": "addr1x", "balance": "7", "utxo_set": []}])
    )
    adapter._session = session  # type: ignore[assignment]
    assert adapter.address_info("addr1x").confirmed == 700
    assert session.posts == ["https://koios.example.com/api/v1/address_info"]
    assert session.kwargs == [{"json": {"_addresses": ["addr1x"]}}]


@pytest.mark.network
def test_live_koios_sees_golden_address_history():
    with AdaAdapter() as adapter:
        assert adapter.address_info(GOLDEN["m/1852'/1815'/0'/0/0"][1]).has_history
