"""Tests for the Dash adapter (Phase 1: address derivation + balance).

The derivation is money-sensitive — a wrong `X…` receive address sends funds to
one the wallet cannot spend, and there is no funded-testnet path to catch it.
The golden addresses below were produced from the standard BIP39 test mnemonic
and independently cross-checked: three implementations (bitcoinlib,
eth-account+coincurve, hdwallet) agree on the compressed pubkey at
``m/44'/5'/0'/0/{0,1}``, and hdwallet independently agrees on the base58check
address encoding. See docs/dash.md.
"""

import pytest

pytest.importorskip("bitcoinlib")

from swapsack.chains.dash import DashAdapter, parse_insight_addr  # noqa: E402
from swapsack.chains.p2pkh import p2pkh_address  # noqa: E402

# Standard BIP39 test mnemonic -> its Dash addresses at m/44'/5'/0'/0/x.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon about"
)
GOLDEN = {
    "m/44'/5'/0'/0/0": "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
    "m/44'/5'/0'/0/1": "XbctnEsgWTn5j1co3emZynemxSFPqkLRKZ",
}
# The pubkey behind GOLDEN[.../0/0], for the raw encoding-layer test.
GOLDEN_PUBKEY = bytes.fromhex(
    "026fa9a6f213b6ba86447965f6b4821264aaadd7521f049f00db9c43a770ea7405"
)

# A trimmed real response from insight.dash.org/insight-api/addr/{a}
# (fetched 2026-07-10; the golden 0/0 address — other users of the standard
# test mnemonic have really used it on-chain).
INSIGHT_USED_EMPTY = {
    "addrStr": "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
    "balanceSat": 0,
    "totalReceivedSat": 1122000,
    "totalSentSat": 1122000,
    "unconfirmedBalanceSat": 0,
    "unconfirmedTxApperances": 0,
    "txApperances": 4,
    "txAppearances": 4,
}
INSIGHT_FRESH = {
    "addrStr": "XbctnEsgWTn5j1co3emZynemxSFPqkLRKZ",
    "balanceSat": 0,
    "totalReceivedSat": 0,
    "totalSentSat": 0,
    "unconfirmedBalanceSat": 0,
    "unconfirmedTxApperances": 0,
    "txApperances": 0,
    "txAppearances": 0,
}
INSIGHT_FUNDED_PENDING = {
    "addrStr": "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
    "balanceSat": 150000,
    "unconfirmedBalanceSat": -50000,
    "unconfirmedTxApperances": 1,
    "txApperances": 2,
    "txAppearances": 2,
}


def test_derive_address_matches_golden_vectors():
    a = DashAdapter()
    for path, address in GOLDEN.items():
        assert a.derive_address(TEST_MNEMONIC, path) == address


def test_p2pkh_encoding_matches_golden_vector():
    assert p2pkh_address(GOLDEN_PUBKEY, b"\x4c") == GOLDEN["m/44'/5'/0'/0/0"]


def test_bip39_passphrase_changes_the_address():
    plain = DashAdapter().derive_address(TEST_MNEMONIC)
    other = DashAdapter(bip39_passphrase="secret").derive_address(TEST_MNEMONIC)
    assert plain != other
    assert other.startswith("X")


def test_parse_insight_used_but_empty_counts_as_history():
    info = parse_insight_addr(INSIGHT_USED_EMPTY)
    assert info.has_history  # keeps the gap-limit scan going past spent addresses
    assert info.confirmed == 0
    assert info.pending == 0


def test_parse_insight_fresh_address_has_no_history():
    info = parse_insight_addr(INSIGHT_FRESH)
    assert not info.has_history
    assert info.confirmed == 0


def test_parse_insight_confirmed_and_pending_are_separate():
    info = parse_insight_addr(INSIGHT_FUNDED_PENDING)
    assert info.confirmed == 150000
    assert info.pending == -50000  # net mempool delta, may be negative
    assert info.has_history


def test_wallet_balance_scans_and_sums(monkeypatch):
    a = DashAdapter()
    funded = a.derive_address(TEST_MNEMONIC)  # 0/0

    def fake_info(address):
        if address == funded:
            return parse_insight_addr(INSIGHT_FUNDED_PENDING)
        return parse_insight_addr({**INSIGHT_FRESH, "addrStr": address})

    monkeypatch.setattr(a, "address_info", fake_info)
    report = a.wallet_balance(TEST_MNEMONIC)
    assert report.symbol == "DASH"
    assert report.decimals == 8
    assert report.confirmed == 150000
    assert report.pending == -50000
    assert report.addresses == (funded,)


def test_broadcast_refuses_loudly():
    # Phase 1 is receive-only: a spend path that silently "succeeds" would be
    # catastrophic, so broadcast must refuse with a pointer to the design note.
    with pytest.raises(NotImplementedError, match="docs/dash.md"):
        DashAdapter().broadcast(["00"])


@pytest.mark.network
def test_live_insight_sees_golden_address_history():
    # The standard-test-mnemonic 0/0 address has real mainnet history (4 txs,
    # all spent) — a stable, read-only guard that the Insight API shape and our
    # parsing still agree.
    with DashAdapter() as a:
        info = a.address_info(GOLDEN["m/44'/5'/0'/0/0"])
    assert info.has_history
    assert info.confirmed == 0
