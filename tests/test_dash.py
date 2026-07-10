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


# --- Phase 2: send / sweep (legacy P2PKH build + sign) -----------------------


def test_bitcoinlib_dash_network_agrees_with_pinned_derivation():
    # The signer derives its keys through bitcoinlib's registered "dash"
    # network; the receive addresses come from the independent golden-vector
    # path (chains/p2pkh.py). The two must agree, or we'd sign for keys that
    # don't own the scanned UTXOs.
    from bitcoinlib.keys import HDKey
    from bitcoinlib.mnemonic import Mnemonic

    seed = Mnemonic().to_seed(TEST_MNEMONIC, "")
    for path, address in GOLDEN.items():
        key = HDKey.from_seed(seed, network="dash").key_for_path(path)
        assert key.address(script_type="p2pkh", encoding="base58") == address


# A trimmed real response shape from insight-api /addr/{a}/utxo.
INSIGHT_UTXOS = [
    {
        "address": "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
        "txid": "cc" * 32,
        "vout": 1,
        "satoshis": 150000,
        "confirmations": 12,
    },
    {
        "address": "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
        "txid": "dd" * 32,
        "vout": 0,
        "satoshis": 50000,
        "confirmations": 0,  # unconfirmed: must be excluded (fail closed)
    },
]


def test_fetch_utxos_excludes_unconfirmed(monkeypatch):
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return INSIGHT_UTXOS

    a = DashAdapter()
    monkeypatch.setattr(a, "_get", lambda url: FakeResp())
    utxos = a.fetch_utxos("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5")
    assert [(u.txid, u.vout, u.value) for u in utxos] == [("cc" * 32, 1, 150000)]


def test_built_send_passes_verify_gate_and_signs():
    from swapsack.chains.coins import Utxo

    a = DashAdapter()
    path0, path1 = "m/44'/5'/0'/0/0", "m/44'/5'/0'/0/1"
    addr0, addr1 = (a.derive_address(TEST_MNEMONIC, p) for p in (path0, path1))
    utxos = [
        Utxo(txid="cc" * 32, vout=1, value=150000, address=addr0, path=path0),
        Utxo(txid="dd" * 32, vout=0, value=90000, address=addr1, path=path1),
    ]
    recipient = "XdAUmwtig27HBG6WfYyHAzP8n6XC9jESEw"  # foreign X-address
    prepared = a.build_and_verify_send(
        recipient=recipient,
        amount=200000,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=1.0,
        change_address=addr0,
        max_fee=10_000,
    )
    assert prepared.problems == []
    # Both inputs sign (across two derivation paths) and the tx verifies.
    raws = a.sign(prepared.built)
    assert len(raws) == 1
    assert all(inp.signatures for inp in prepared.built.tx.inputs)
    # value is conserved: inputs = recipient + change + fee
    outputs_total = sum(o.value for o in prepared.built.outputs)
    assert outputs_total + prepared.built.fee == 240000


def test_built_send_folds_legacy_subdust_change_into_fee():
    from swapsack.chains.coins import Utxo

    a = DashAdapter()
    path0 = "m/44'/5'/0'/0/0"
    addr0 = a.derive_address(TEST_MNEMONIC, path0)
    # 1-in 2-out legacy @1 duff/vB = 227 fee; change would be 400 < dust 546.
    utxos = [Utxo(txid="cc" * 32, vout=1, value=100627, address=addr0, path=path0)]
    prepared = a.build_and_verify_send(
        recipient="XdAUmwtig27HBG6WfYyHAzP8n6XC9jESEw",
        amount=100000,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=1.0,
        change_address=addr0,
        max_fee=10_000,
    )
    assert prepared.problems == []
    assert prepared.built.fee == 627
    assert len(prepared.built.outputs) == 1  # no change output


def test_built_sweep_spends_everything():
    from swapsack.chains.coins import P2PKH as P2PKH_SCRIPT
    from swapsack.chains.coins import Utxo, sweep_amount

    a = DashAdapter()
    path0 = "m/44'/5'/0'/0/0"
    addr0 = a.derive_address(TEST_MNEMONIC, path0)
    utxos = [Utxo(txid="cc" * 32, vout=1, value=150000, address=addr0, path=path0)]
    amount, fee = sweep_amount(150000, 1, 1.0, memo_len=0, script=P2PKH_SCRIPT)
    prepared = a.build_and_verify_send(
        recipient="XdAUmwtig27HBG6WfYyHAzP8n6XC9jESEw",
        amount=amount,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=1.0,
        change_address=addr0,
        max_fee=10_000,
        sweep=True,
    )
    assert prepared.problems == []
    assert amount + prepared.built.fee == 150000
    assert len(prepared.built.outputs) == 1  # nothing left behind


def test_verify_gate_blocks_foreign_change():
    a = DashAdapter()
    path0 = "m/44'/5'/0'/0/0"
    addr0 = a.derive_address(TEST_MNEMONIC, path0)
    # The builder's owned set is change_address ∪ utxo addresses, so a wrongly
    # routed change output has to be simulated at the verify layer: the gate
    # must reject change paid to a stranger.
    from swapsack.verify import SendPlan, TxOutput, verify_btc_send

    problems = verify_btc_send(
        [
            TxOutput(address="XdAUmwtig27HBG6WfYyHAzP8n6XC9jESEw", value=100000),
            TxOutput(address="XsomeStrangerAddressAAAAAAAAAAAAAA", value=399000),
        ],
        fee=1000,
        plan=SendPlan(recipient="XdAUmwtig27HBG6WfYyHAzP8n6XC9jESEw", amount=100000),
        owned_addresses={addr0},
        max_fee=10_000,
    )
    assert any("non-owned" in p for p in problems)


def test_built_deposit_carries_memo_and_signs():
    # The Phase-3 swap-from/LP shape: vault output + OP_RETURN memo + change,
    # all legacy. Exercises the OP_RETURN path of the shared builder on a
    # legacy (non-segwit) transaction, gated by verify_btc_swap.
    from swapsack.chains.coins import Utxo

    a = DashAdapter()
    path0 = "m/44'/5'/0'/0/0"
    addr0 = a.derive_address(TEST_MNEMONIC, path0)
    utxos = [Utxo(txid="cc" * 32, vout=1, value=500000, address=addr0, path=path0)]
    memo = "=:BTC.BTC:bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    vault = "XdAUmwtig27HBG6WfYyHAzP8n6XC9jESEw"
    prepared = a.build_and_verify_deposit(
        vault=vault,
        memo=memo,
        amount=200000,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=1.0,
        change_address=addr0,
        max_fee=10_000,
    )
    assert prepared.problems == []
    op_returns = [o for o in prepared.built.outputs if o.op_return_data is not None]
    assert [o.op_return_data for o in op_returns] == [memo.encode()]
    raws = a.sign(prepared.built)
    assert len(raws) == 1
    # The raw legacy tx carries the memo bytes verbatim.
    assert memo.encode().hex() in raws[0]


def test_broadcast_posts_to_insight(monkeypatch):
    sent = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"txid": "ee" * 32}

    def fake_post(url, **kwargs):
        sent["url"] = url
        sent["json"] = kwargs.get("json")
        return FakeResp()

    a = DashAdapter()
    monkeypatch.setattr(a, "_post", fake_post)
    txid = a.broadcast(["deadbeef"])
    assert txid == "ee" * 32
    assert sent["url"].endswith("/tx/send")
    assert sent["json"] == {"rawtx": "deadbeef"}


@pytest.mark.network
def test_live_insight_sees_golden_address_history():
    # The standard-test-mnemonic 0/0 address has real mainnet history (4 txs,
    # all spent) — a stable, read-only guard that the Insight API shape and our
    # parsing still agree.
    with DashAdapter() as a:
        info = a.address_info(GOLDEN["m/44'/5'/0'/0/0"])
    assert info.has_history
    assert info.confirmed == 0
