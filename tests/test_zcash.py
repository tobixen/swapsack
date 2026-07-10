"""Tests for the Zcash adapter (Phase 1: t-addr derivation + balance).

The derivation is money-sensitive — a wrong ``t1…`` receive address sends funds
to one the wallet cannot spend, and there is no funded-testnet path to catch
it. The golden addresses below were produced from the standard BIP39 test
mnemonic and independently cross-checked: three implementations (bitcoinlib,
eth-account+coincurve, hdwallet) agree on the compressed pubkey at
``m/44'/133'/0'/0/{0,1}``, and hdwallet independently agrees on the two-byte
base58check encoding. See docs/zcash.md.

The lightwalletd wire format (hand-rolled protobuf, like cosmos_tx) is pinned
against byte-literal golden messages assembled from the proto definitions in
zcash/librustzcash `service.proto`.
"""

import pytest

pytest.importorskip("bitcoinlib")
pytest.importorskip("grpc")

from swapsack.chains.p2pkh import p2pkh_address  # noqa: E402
from swapsack.chains.zcash import (  # noqa: E402
    ZecAdapter,
    decode_balance,
    decode_block_id_height,
    encode_address_list,
    encode_block_filter,
)

# Standard BIP39 test mnemonic -> its Zcash t-addrs at m/44'/133'/0'/0/x.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon about"
)
GOLDEN = {
    "m/44'/133'/0'/0/0": "t1XVXWCvpMgBvUaed4XDqWtgQgJSu1Ghz7F",
    "m/44'/133'/0'/0/1": "t1aQ2b1XszNVo15BguYLbQGqETBL9QZA8Jq",
}
# The pubkey behind GOLDEN[.../0/0], for the raw two-byte-prefix encoding test.
GOLDEN_PUBKEY = bytes.fromhex(
    "03db98d8f87716269ed31879aef19bdadbc869a9ea67729e36332d023b916cbcc9"
)

# The golden 0/0 address is NOT fresh on mainnet: other users of the standard
# test mnemonic really used it (two 2018-era Overwinter txs, long emptied) —
# on-chain history never disappears, which makes it a stable live target for
# the used-but-emptied path that keeps the gap-limit scan going.


def test_derive_address_matches_golden_vectors():
    a = ZecAdapter()
    for path, address in GOLDEN.items():
        assert a.derive_address(TEST_MNEMONIC, path) == address


def test_p2pkh_encoding_handles_the_two_byte_prefix():
    assert p2pkh_address(GOLDEN_PUBKEY, b"\x1c\xb8") == GOLDEN["m/44'/133'/0'/0/0"]


def test_bip39_passphrase_changes_the_address():
    plain = ZecAdapter().derive_address(TEST_MNEMONIC)
    other = ZecAdapter(bip39_passphrase="secret").derive_address(TEST_MNEMONIC)
    assert plain != other
    assert other.startswith("t1")


# --- lightwalletd wire format (hand-rolled protobuf) -------------------------


def test_encode_address_list_single_address():
    addr = GOLDEN["m/44'/133'/0'/0/0"]
    # AddressList { repeated string addresses = 1; }
    assert encode_address_list([addr]) == b"\x0a\x23" + addr.encode()


def test_decode_balance_varint():
    # Balance { int64 valueZat = 1; } — 12345 = varint b9 60
    assert decode_balance(b"\x08\xb9\x60") == 12345


def test_decode_balance_empty_message_is_zero():
    # proto3: a zero int64 is omitted from the wire entirely.
    assert decode_balance(b"") == 0


def test_encode_block_filter_nests_the_range():
    # TransparentAddressBlockFilter { string address = 1; BlockRange range = 2; }
    # BlockRange { BlockID start = 1; BlockID end = 2; }
    # BlockID { uint64 height = 1; }
    got = encode_block_filter("t1abc", start=1, end=300)
    start_msg = b"\x08\x01"
    end_msg = b"\x08\xac\x02"  # varint(300) = ac 02
    block_range = b"\x0a\x02" + start_msg + b"\x12\x03" + end_msg
    expected = b"\x0a\x05t1abc" + b"\x12\x09" + block_range
    assert got == expected


def test_decode_block_id_height():
    # BlockID { uint64 height = 1; bytes hash = 2; } — hash is ignored.
    assert decode_block_id_height(b"\x08\xac\x02\x12\x02\xab\xcd") == 300


# --- balance / scan -----------------------------------------------------------


def test_wallet_balance_scans_and_sums(monkeypatch):
    from swapsack.chains.base import AddressInfo

    a = ZecAdapter()
    funded = a.derive_address(TEST_MNEMONIC)  # 0/0

    def fake_info(address):
        if address == funded:
            return AddressInfo(has_history=True, confirmed=250000, pending=0)
        return AddressInfo(has_history=False, confirmed=0, pending=0)

    monkeypatch.setattr(a, "address_info", fake_info)
    report = a.wallet_balance(TEST_MNEMONIC)
    assert report.symbol == "ZEC"
    assert report.decimals == 8
    assert report.confirmed == 250000
    assert report.addresses == (funded,)


def test_broadcast_refuses_loudly():
    # Phase 1 is receive-only: a spend path that silently "succeeds" would be
    # catastrophic, so broadcast must refuse with a pointer to the design note.
    with pytest.raises(NotImplementedError, match="docs/zcash.md"):
        ZecAdapter().broadcast(["00"])


def test_lp_backends_is_maya_only():
    # ZEC.ZEC exists only on Maya; THORChain answers unknown-pool LP probes
    # with a 500, so `balance` must not probe it (same as DASH).
    assert ZecAdapter.lp_backends == ("maya",)


@pytest.mark.network
def test_live_lightwalletd_roundtrip():
    # Read-only guards against lightwalletd API drift, all in one connection:
    # the chain tip is past the 2026-07 height, the golden 0/0 address shows
    # history-but-no-funds (the case that must keep a scan going), and a random
    # never-used address shows neither. The random address is fresh with
    # overwhelming probability (a 160-bit collision would break Zcash itself).
    import secrets

    fresh_addr = p2pkh_address(secrets.token_bytes(33), b"\x1c\xb8")
    with ZecAdapter() as a:
        assert a.latest_height() > 3_400_000
        used = a.address_info(GOLDEN["m/44'/133'/0'/0/0"])
        fresh = a.address_info(fresh_addr)
    assert used.has_history
    assert used.confirmed == 0
    assert not fresh.has_history
    assert fresh.confirmed == 0
