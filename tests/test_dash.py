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
# A fork using only the corrected spelling for the unconfirmed count (no
# confirmed appearances yet -- an address with a single incoming unconfirmed
# tx).
INSIGHT_CORRECTED_SPELLING_UNCONFIRMED = {
    "addrStr": "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
    "balanceSat": 0,
    "totalReceivedSat": 0,
    "unconfirmedBalanceSat": 50000,
    "unconfirmedTxAppearances": 1,
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


def test_parse_insight_unconfirmed_corrected_spelling_counts_as_history():
    # A fork spelling only "unconfirmedTxAppearances" (not the sic
    # "unconfirmedTxApperances") must still register history, or the
    # gap-limit scan stops early and under-reports the wallet.
    info = parse_insight_addr(INSIGHT_CORRECTED_SPELLING_UNCONFIRMED)
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
    # Inputs opt in to RBF (shared utxo.py builder) so a stuck DASH spend can
    # be bumped by a future release.
    assert all(inp.sequence == 0xFFFFFFFD for inp in prepared.built.tx.inputs)
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


def test_parse_insight_takes_the_larger_of_the_two_spellings():
    """A stale sic key must not mask the corrected one (or vice versa).

    Preferring one spelling breaks on a fork that emits *both* and only keeps
    the corrected one current: the sic key reads 0, history goes unnoticed, and
    the gap-limit scan stops early — under-reporting the wallet's balance and
    hiding spendable UTXOs.
    """
    both_sic_stale = {
        "balanceSat": 0,
        "totalReceivedSat": 0,
        "unconfirmedBalanceSat": 0,
        "txApperances": 0,  # sic, stale
        "txAppearances": 3,  # corrected, current
        "unconfirmedTxApperances": 0,
        "unconfirmedTxAppearances": 0,
    }
    assert parse_insight_addr(both_sic_stale).has_history

    both_sic_stale_unconfirmed = {
        **both_sic_stale,
        "txAppearances": 0,
        "unconfirmedTxApperances": 0,
        "unconfirmedTxAppearances": 2,
    }
    assert parse_insight_addr(both_sic_stale_unconfirmed).has_history

    # ...and the reverse: a fork where only the sic key is current.
    only_sic_current = {**both_sic_stale, "txApperances": 3, "txAppearances": 0}
    assert parse_insight_addr(only_sic_current).has_history


def test_parse_insight_unconfirmed_balance_alone_counts_as_history():
    """Belt and braces: money in the mempool is history whatever the counters say.

    An address whose only activity is an unconfirmed credit must keep the scan
    going even if the appearance counters have not caught up. Negative counts
    too — an unconfirmed *spend* is equally evidence the address is in use.
    """
    for pending in (5_000, -5_000):
        info = parse_insight_addr(
            {
                "balanceSat": 0,
                "totalReceivedSat": 0,
                "unconfirmedBalanceSat": pending,
                "txApperances": 0,
                "unconfirmedTxApperances": 0,
            }
        )
        assert info.has_history
        assert info.pending == pending


def test_fetch_utxos_can_include_unconfirmed(monkeypatch):
    """--allow-unconfirmed opts DASH in too; Dash has no RBF, so no CPFP maths."""

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return INSIGHT_UTXOS

    a = DashAdapter()
    monkeypatch.setattr(a, "_get", lambda url: FakeResp())
    utxos = a.fetch_utxos(
        "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5", include_unconfirmed=True
    )
    assert [(u.value, u.confirmed) for u in utxos] == [(150000, True), (50000, False)]
    # No mempool fee market to price against: the inputs stay unsurcharged.
    assert [u.ancestor_deficit for u in a.cpfp_deficits(utxos, fee_rate=1.0)] == [0, 0]


# --- address transaction history (Insight paging) ----------------------------

INSIGHT_TX = {
    "txid": "ab" * 32,
    "version": 1,
    "locktime": 0,
    "vin": [
        {
            "txid": "cd" * 32,
            "vout": 1,
            "sequence": 4294967293,
            "n": 0,
            "addr": "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
            "valueSat": 150000,
            "value": 0.0015,
        }
    ],
    "vout": [
        {
            "value": "0.00100000",
            "n": 0,
            "scriptPubKey": {
                "hex": "76a914" + "11" * 20 + "88ac",
                "addresses": ["XdAUmwtig27HBG6WfYyHAzP8n6XC9jESEw"],
                "type": "pubkeyhash",
            },
            "spentTxId": None,
        },
        {
            "value": "0.00000000",
            "n": 1,
            "scriptPubKey": {"hex": "6a0568656c6c6f", "type": "nulldata"},
        },
        {
            "value": "0.00049500",
            "n": 2,
            "scriptPubKey": {
                "hex": "76a914" + "22" * 20 + "88ac",
                "addresses": ["XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5"],
                "type": "pubkeyhash",
            },
            "spentTxId": "ef" * 32,
            "spentIndex": 0,
        },
    ],
    "blockhash": "00" * 32,
    "blockheight": 2_100_000,
    "confirmations": 12,
    "time": 1_756_000_000,
    "blocktime": 1_756_000_000,
    "valueOut": 0.001495,
    "size": 260,
    "valueIn": 0.0015,
    "fees": 0.000005,
}


def test_parse_insight_tx_reads_amounts_in_duffs_not_floats():
    """Insight reports DASH as decimal strings/floats. Rounding one through a
    binary float mis-states a balance by base units, so the conversion has to be
    exact."""
    from swapsack.chains.dash import parse_insight_tx

    tx = parse_insight_tx(INSIGHT_TX)
    assert tx.txid == "ab" * 32
    assert tx.confirmed is True
    assert tx.block_height == 2_100_000
    assert tx.block_time == 1_756_000_000
    assert tx.fee == 500
    assert tx.vsize == 260
    assert [o.value for o in tx.outputs] == [100_000, 0, 49_500]
    assert [i.value for i in tx.inputs] == [150_000]
    # Inputs = outputs + fee, or the listing is misreporting a spend.
    assert tx.total_in == tx.total_out + tx.fee


def test_parse_insight_tx_keeps_addresses_the_op_return_and_the_spend():
    from swapsack.chains.dash import parse_insight_tx

    tx = parse_insight_tx(INSIGHT_TX)
    assert [o.address for o in tx.outputs] == [
        "XdAUmwtig27HBG6WfYyHAzP8n6XC9jESEw",
        None,
        "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
    ]
    assert tx.outputs[1].op_return is True
    assert tx.outputs[1].op_return_data == b"hello"
    assert tx.has_op_return
    # Insight names the spender itself; that is better evidence than inference.
    assert tx.outputs[2].spent_by == "ef" * 32
    assert tx.outputs[0].spent_by is None
    # The input keeps its outpoint and its nSequence (the BIP125 signal).
    assert (tx.inputs[0].txid, tx.inputs[0].vout) == ("cd" * 32, 1)
    assert tx.inputs[0].sequence == 4294967293


def test_parse_insight_tx_treats_an_unconfirmed_tx_as_undated():
    from swapsack.chains.dash import parse_insight_tx

    tx = parse_insight_tx(
        {**INSIGHT_TX, "confirmations": 0, "blockheight": -1, "blocktime": None}
    )
    assert tx.confirmed is False
    assert tx.block_height is None
    assert tx.block_time is None


def test_parse_insight_tx_survives_a_coinbase_input_with_no_address():
    from swapsack.chains.dash import parse_insight_tx

    tx = parse_insight_tx(
        {**INSIGHT_TX, "vin": [{"coinbase": "03", "sequence": 4294967295, "n": 0}]}
    )
    assert tx.inputs[0].address is None
    assert tx.inputs[0].value == 0


def test_address_txs_pages_through_insight_and_stops_when_exhausted():
    from swapsack.chains.dash import DashAdapter

    pages = [
        {
            "totalItems": 3,
            "from": 0,
            "to": 2,
            "items": [INSIGHT_TX, {**INSIGHT_TX, "txid": "b1" * 32}],
        },
        {
            "totalItems": 3,
            "from": 2,
            "to": 3,
            "items": [{**INSIGHT_TX, "txid": "b2" * 32}],
        },
        {"totalItems": 3, "from": 3, "to": 3, "items": []},
    ]
    calls = []

    class FakeResp:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    a = DashAdapter()

    def fake_get(url, **kwargs):
        calls.append(kwargs.get("params"))
        return FakeResp(pages[len(calls) - 1])

    a._get = fake_get
    result = a.address_txs("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5")
    assert [t.txid for t in result.transactions] == [
        "ab" * 32,
        "b1" * 32,
        "b2" * 32,
    ]
    assert result.truncated is False
    assert [c["from"] for c in calls] == [0, 2, 3]


def test_address_txs_marks_a_capped_insight_walk_as_truncated():
    from swapsack.chains.dash import DashAdapter

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "totalItems": 9,
                "items": [{**INSIGHT_TX, "txid": f"{i:02x}" * 32} for i in range(4)],
            }

    a = DashAdapter()
    a._get = lambda url, **kwargs: FakeResp()
    result = a.address_txs("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5", limit=2)
    assert len(result.transactions) == 2
    assert result.truncated is True


# --- the Insight offset cursor vs. a history that moves under the walk -------


class _InsightPages:
    """Scripted Insight /addrs/<a>/txs responses, recording the offsets asked for."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(kwargs.get("params", {}).get("from"))
        payload = self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        return Resp()


def _page(total, *txids):
    return {
        "totalItems": total,
        "items": [{**INSIGHT_TX, "txid": t} for t in txids],
    }


def test_address_txs_rewalks_when_the_history_moved_under_it():
    """Insight sorts /addrs/<a>/txs newest-first and pages by numeric offset, so
    a transaction arriving mid-walk shifts every later item down one and the
    item at the window boundary is silently never fetched.

    A skip is not a repeat, so the dedupe cannot see it — but ``totalItems``
    growing between two pages of one walk says the list moved. Start over
    rather than return a history with a hole in it: the spend inference is only
    sound on a complete one.
    """
    from swapsack.chains.dash import DashAdapter

    pages = _InsightPages(
        _page(2, "a1" * 32),  # attempt 1, page 1
        _page(3, "a2" * 32),  # attempt 1, page 2: a transaction arrived — raced
        _page(3, "b1" * 32),  # attempt 2, page 1
        _page(3, "b2" * 32),  # attempt 2, page 2: stable
        _page(3),  # attempt 2, page 3: exhausted
    )
    a = DashAdapter()
    a._get = pages
    result = a.address_txs("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5")

    assert result.truncated is False
    assert [t.txid for t in result.transactions] == ["b1" * 32, "b2" * 32]


def test_address_txs_gives_up_and_says_incomplete_if_it_keeps_racing():
    """A busy address may never hold still. Returning what we have is fine;
    returning it as if it were the whole history is not."""
    from swapsack.chains.dash import DashAdapter

    pages = _InsightPages(
        _page(10, "a1" * 32),
        _page(11, "a2" * 32),
        _page(12, "a3" * 32),
        _page(13, "a4" * 32),
        _page(14, "a5" * 32),
        _page(15, "a6" * 32),
        _page(16, "a7" * 32),
        _page(17, "a8" * 32),
    )
    a = DashAdapter()
    a._get = pages
    result = a.address_txs("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5")
    assert result.truncated is True


def test_a_quiet_address_is_not_accused_of_racing():
    """The common case must not pay for this: an address whose history fits in
    one page, and which nothing arrives on, walks once and comes back clean."""
    from swapsack.chains.dash import DashAdapter

    pages = _InsightPages(_page(1, "a1" * 32), _page(1))
    a = DashAdapter()
    a._get = pages
    result = a.address_txs("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5")
    assert result.truncated is False
    assert [t.txid for t in result.transactions] == ["a1" * 32]
    assert pages.calls == [0, 1]  # no re-walk


def test_a_raced_walk_stops_paging_instead_of_finishing_a_doomed_pass():
    """Once the list has moved, everything still to be paged will be discarded.
    Reading it anyway costs a request per page on exactly the busy addresses
    where the race happens in the first place."""
    from swapsack.chains.dash import DashAdapter

    pages = _InsightPages(
        _page(2, "a1" * 32),  # attempt 1, page 1
        _page(3, "a2" * 32),  # attempt 1, page 2: moved — abandon the attempt
        _page(3, "b1" * 32),  # attempt 2, page 1
        _page(3),  # attempt 2, page 2: exhausted
    )
    a = DashAdapter()
    a._get = pages
    a.address_txs("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5")
    # Attempt 1 stops at the page that disagreed; it does not walk on.
    assert pages.calls == [0, 1, 0, 1]


def test_an_insight_fork_without_totalitems_still_walks():
    """``totalItems`` is a drift detector, not a dependency: a fork that omits
    it degrades to the old behaviour rather than looping or crying INCOMPLETE."""
    from swapsack.chains.dash import DashAdapter

    pages = _InsightPages(
        {"items": [{**INSIGHT_TX, "txid": "a1" * 32}]},
        {"items": [{**INSIGHT_TX, "txid": "a2" * 32}]},
        {"items": []},
    )
    a = DashAdapter()
    a._get = pages
    result = a.address_txs("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5")
    assert result.truncated is False
    assert [t.txid for t in result.transactions] == ["a1" * 32, "a2" * 32]


# --- Blockbook, and falling over to it when Insight is down ------------------

BLOCKBOOK = "https://bb.example.com/api/v2"
INSIGHT = "https://insight.example.com/insight-api"

# Trimmed real responses from dash.atomicwallet.io/api/v2 for the golden 0/0
# address (fetched 2026-10-29, while insight.dash.org answered 502).
BLOCKBOOK_USED_EMPTY = {
    "address": "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
    "balance": "0",
    "totalReceived": "1122000",
    "totalSent": "1122000",
    "unconfirmedTxs": 0,
    "txs": 4,
}
BLOCKBOOK_TX = {
    "txid": "359445e0908226dea508794eb8e5dee16a778ccfe7a9a7f90b868a91b670c485",
    "vin": [
        {
            # Blockbook omits zero-valued fields: no "vout" key means vout 0.
            "txid": "77dc9192a4176903fde535c7dc38432ce2089a47d461d3da7d38d2c5f43db3f7",
            "sequence": 4294967295,
            "n": 0,
            "addresses": ["XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5"],
            "isAddress": True,
            "value": "122000",
        }
    ],
    "vout": [
        {
            "value": "117200",
            "n": 0,
            "hex": "76a914f7428a48ad30725069624a35874523e5157c19c188ac",
            "addresses": ["XyEESvmAig9ypH3yuHXLTuJZsAU9fKxj4W"],
            "isAddress": True,
        }
    ],
    "blockHeight": 1988337,
    "confirmations": 574752,
    "blockTime": 1702665932,
    "size": 191,
    "value": "117200",
    "valueIn": "122000",
    "fees": "4800",
}


class _Resp:
    """A response with a status code, raising like niquests does on >= 400."""

    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import niquests

            raise niquests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


def test_parse_blockbook_used_but_empty_counts_as_history():
    from swapsack.chains.dash import parse_blockbook_addr

    info = parse_blockbook_addr(BLOCKBOOK_USED_EMPTY)
    assert info.has_history
    assert (info.confirmed, info.pending) == (0, 0)


def test_parse_blockbook_fresh_address_has_no_history():
    from swapsack.chains.dash import parse_blockbook_addr

    info = parse_blockbook_addr(
        {"address": "X", "balance": "0", "totalReceived": "0", "txs": 0}
    )
    assert not info.has_history


def test_parse_blockbook_pending_spend_alone_counts_as_history():
    from swapsack.chains.dash import parse_blockbook_addr

    info = parse_blockbook_addr(
        {"balance": "5000", "unconfirmedBalance": "-5000", "txs": 0}
    )
    assert info.has_history
    assert (info.confirmed, info.pending) == (5000, -5000)


def test_parse_blockbook_tx_reads_a_real_transaction():
    from swapsack.chains.dash import parse_blockbook_tx

    tx = parse_blockbook_tx(BLOCKBOOK_TX)
    assert tx.txid == BLOCKBOOK_TX["txid"]
    assert (tx.confirmed, tx.block_height, tx.block_time) == (
        True,
        1988337,
        1702665932,
    )
    assert (tx.fee, tx.vsize) == (4800, 191)
    (spend,) = tx.inputs
    assert (spend.txid, spend.vout, spend.value, spend.address) == (
        "77dc9192a4176903fde535c7dc38432ce2089a47d461d3da7d38d2c5f43db3f7",
        0,
        122000,
        "XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",
    )
    assert spend.sequence == 4294967295
    assert [(o.value, o.address) for o in tx.outputs] == [
        (117200, "XyEESvmAig9ypH3yuHXLTuJZsAU9fKxj4W")
    ]


def test_parse_blockbook_tx_keeps_the_memo_and_survives_a_coinbase():
    from swapsack.chains.dash import parse_blockbook_tx

    memo = b"=:ETH.ETH:0xabc"
    script = "6a" + f"{len(memo):02x}" + memo.hex()
    tx = parse_blockbook_tx(
        {
            "txid": "cb" * 32,
            "vin": [{"n": 0, "coinbase": "03aa", "isAddress": False}],
            "vout": [
                {
                    "value": "0",
                    "n": 0,
                    "hex": script,
                    "isAddress": False,
                    "addresses": [f"OP_RETURN {memo.hex()}"],
                },
                {
                    "value": "10",
                    "n": 1,
                    "hex": "76a9",
                    "isAddress": True,
                    "addresses": ["Xout"],
                    "spentTxId": "dd" * 32,
                },
            ],
            "blockHeight": -1,
            "confirmations": 0,
            "blockTime": 1700000000,  # first-seen time, not a block's
            "fees": "0",
            "size": 100,
        }
    )
    (coinbase,) = tx.inputs
    assert (coinbase.address, coinbase.value, coinbase.txid) == (None, 0, None)
    data, paid = tx.outputs
    assert (data.op_return, data.address, data.op_return_data) == (True, None, memo)
    assert (paid.address, paid.spent_by) == ("Xout", "dd" * 32)
    assert (tx.confirmed, tx.block_height, tx.block_time) == (False, None, None)


def test_blockbook_endpoint_reads_balance_and_utxos():
    a = DashAdapter(BLOCKBOOK)
    urls = []

    def fake_get(url, **kwargs):
        urls.append(url)
        if "/utxo/" in url:
            return _Resp(
                [
                    {
                        "txid": "cc" * 32,
                        "vout": 1,
                        "value": "150000",
                        "confirmations": 3,
                    },
                    {"txid": "dd" * 32, "value": "9", "confirmations": 0},
                ]
            )
        return _Resp(BLOCKBOOK_USED_EMPTY)

    a._get = fake_get
    assert a.address_info("Xa").has_history
    assert [(u.txid, u.vout, u.value) for u in a.fetch_utxos("Xa")] == [
        ("cc" * 32, 1, 150000)
    ]
    # An unconfirmed output with Blockbook's omitted vout 0.
    unconf = a.fetch_utxos("Xa", include_unconfirmed=True)[1]
    assert (unconf.vout, unconf.confirmed) == (0, False)
    assert urls[:2] == [f"{BLOCKBOOK}/address/Xa", f"{BLOCKBOOK}/utxo/Xa"]


def test_blockbook_walk_pages_by_number_and_rewalks_on_drift():
    a = DashAdapter(BLOCKBOOK)

    def page(n, total, *txids, pages=2):
        return {
            "page": n,
            "totalPages": pages,
            "txs": total,
            "transactions": [{**BLOCKBOOK_TX, "txid": t} for t in txids],
        }

    script = [
        page(1, 2, "a1" * 32),
        page(2, 3, "a2" * 32),  # a transaction arrived mid-walk
        page(1, 3, "b1" * 32),
        page(2, 3, "b2" * 32),  # the last page: no third request
    ]
    asked = []

    def fake_get(url, **kwargs):
        asked.append(kwargs["params"]["page"])
        return _Resp(script[len(asked) - 1])

    a._get = fake_get
    result = a.address_txs("Xa")
    assert [t.txid for t in result.transactions] == ["b1" * 32, "b2" * 32]
    assert result.truncated is False
    assert asked == [1, 2, 1, 2]


def test_blockbook_broadcast_posts_the_raw_hex():
    a = DashAdapter(BLOCKBOOK)
    sent = {}

    def fake_post(url, **kwargs):
        sent.update(url=url, data=kwargs.get("data"))
        return _Resp({"result": "ee" * 32})

    a._post = fake_post
    assert a.broadcast(["deadbeef"]) == "ee" * 32
    assert sent == {"url": f"{BLOCKBOOK}/sendtx/", "data": "deadbeef"}


def test_a_down_insight_falls_over_to_blockbook_and_stays_there(capsys):
    """insight.dash.org answered every request with a Cloudflare 502 on
    2026-10-29. A 5xx is the host saying it is broken, not an answer about the
    address, so the next endpoint is asked — and pinned, so a 60-address scan
    does not knock on the dead one 60 times."""
    a = DashAdapter((INSIGHT, BLOCKBOOK))
    urls = []

    def fake_get(url, **kwargs):
        urls.append(url)
        return (
            _Resp(status=502)
            if url.startswith(INSIGHT)
            else _Resp(BLOCKBOOK_USED_EMPTY)
        )

    a._get = fake_get
    assert a.address_info("Xa").has_history
    assert a.address_info("Xb").has_history
    assert urls == [
        f"{INSIGHT}/addr/Xa",
        f"{BLOCKBOOK}/address/Xa",
        f"{BLOCKBOOK}/address/Xb",
    ]
    err = capsys.readouterr().err
    assert "insight.example.com" in err and "bb.example.com" in err
    assert "Xa" not in err  # host, never the path: it is the user's address


def test_an_unreachable_insight_falls_over_too():
    from swapsack.net import HostUnreachable

    a = DashAdapter((INSIGHT, BLOCKBOOK))

    def fake_get(url, **kwargs):
        if url.startswith(INSIGHT):
            raise HostUnreachable("insight.example.com: timed out")
        return _Resp(BLOCKBOOK_USED_EMPTY)

    a._get = fake_get
    assert a.address_info("Xa").has_history


def test_a_real_error_answer_does_not_fall_over():
    """A 4xx is the endpoint answering — a bad address, say. Asking a second
    operator would only hand them the address for the same answer."""
    import niquests

    a = DashAdapter((INSIGHT, BLOCKBOOK))
    urls = []

    def fake_get(url, **kwargs):
        urls.append(url)
        return _Resp(status=400)

    a._get = fake_get
    with pytest.raises(niquests.exceptions.HTTPError):
        a.address_info("Xa")
    assert urls == [f"{INSIGHT}/addr/Xa"]


def test_every_endpoint_down_raises_host_unreachable():
    from swapsack.net import HostUnreachable

    a = DashAdapter((INSIGHT, BLOCKBOOK))
    a._get = lambda url, **kwargs: _Resp(status=503)
    with pytest.raises(HostUnreachable):
        a.address_info("Xa")


def test_broadcast_falls_over_when_insight_is_down():
    """Safe to repeat: a signed transaction has one txid wherever it is sent."""
    a = DashAdapter((INSIGHT, BLOCKBOOK))

    def fake_post(url, **kwargs):
        if url.startswith(INSIGHT):
            return _Resp(status=502)
        return _Resp({"result": "ee" * 32})

    a._post = fake_post
    assert a.broadcast(["deadbeef"]) == "ee" * 32


def test_default_endpoints_are_insight_then_blockbook():
    from swapsack.chains.dash import DEFAULT_DASH_API, DEFAULT_DASH_NODES, is_blockbook

    assert DEFAULT_DASH_NODES[0] == DEFAULT_DASH_API
    assert [is_blockbook(u) for u in DEFAULT_DASH_NODES] == [False, True]
    assert DashAdapter().api_url == DEFAULT_DASH_API


@pytest.mark.network
def test_live_blockbook_sees_golden_address_history():
    from swapsack.chains.dash import DEFAULT_DASH_BLOCKBOOK

    with DashAdapter(DEFAULT_DASH_BLOCKBOOK) as a:
        info = a.address_info(GOLDEN["m/44'/5'/0'/0/0"])
        txs = a.address_txs(GOLDEN["m/44'/5'/0'/0/0"])
    assert info.has_history
    assert info.confirmed == 0
    assert len(txs.transactions) == 4


# The signed raw of BLOCKBOOK_TX (a real mainnet transaction), for the txid.
BLOCKBOOK_TX_RAW = (
    "0100000001f7b33df4c5d2387ddad361d4479a08e22c4338dcc735e5fd036917a49291dc77"
    "000000006a473044022062dba38f906387add7a13588e33742c02c181cf1bfdd124a9d4fad"
    "4401c88a9c02202d4fb714decc6d12c666e6e7d6a558d2fc2473be1eff0a35fe746b085df7"
    "726e0121026fa9a6f213b6ba86447965f6b4821264aaadd7521f049f00db9c43a770ea7405"
    "ffffffff01d0c90100000000001976a914f7428a48ad30725069624a35874523e5157c19c1"
    "88ac00000000"
)


class _TextResp(_Resp):
    def __init__(self, text, status):
        super().__init__(None, status)
        self.text = text


def test_a_tx_the_network_already_has_counts_as_broadcast():
    """Insight relays the transaction, then its proxy times out with a 502. The
    resend to Blockbook is refused as already known — which means it went out.
    Calling that a failure invites a re-run that pays a second time from other
    outputs."""
    a = DashAdapter((INSIGHT, BLOCKBOOK))

    def fake_post(url, **kwargs):
        if url.startswith(INSIGHT):
            return _Resp(status=502)
        return _TextResp('{"error":"-26: txn-already-in-mempool"}', status=400)

    a._post = fake_post
    assert a.broadcast([BLOCKBOOK_TX_RAW]) == BLOCKBOOK_TX["txid"]


def test_a_broadcast_that_may_have_gone_out_says_so():
    """Every endpoint failed, but one of them only after it may have relayed the
    transaction: the user needs the txid to check before trying again."""
    from swapsack.swap import BroadcastError

    a = DashAdapter((INSIGHT, BLOCKBOOK))

    def fake_post(url, **kwargs):
        if url.startswith(INSIGHT):
            return _Resp(status=502)
        return _TextResp('{"error":"-25: bad-txns-inputs-missingorspent"}', 400)

    a._post = fake_post
    with pytest.raises(BroadcastError) as caught:
        a.broadcast([BLOCKBOOK_TX_RAW])
    assert BLOCKBOOK_TX["txid"] in str(caught.value)
    assert "before retrying" in str(caught.value)


def test_a_plain_rejection_is_not_reported_as_maybe_sent():
    """A node that refused the transaction outright, with nothing transient
    before it, did not send it: no "may already be in flight" scare."""
    import niquests

    a = DashAdapter(BLOCKBOOK)
    a._post = lambda url, **kwargs: _TextResp('{"error":"-26: dust"}', 400)
    with pytest.raises(niquests.exceptions.HTTPError):
        a.broadcast([BLOCKBOOK_TX_RAW])


def test_a_blockbook_error_body_is_a_broadcast_error_not_a_keyerror():
    from swapsack.swap import BroadcastError

    a = DashAdapter(BLOCKBOOK)
    a._post = lambda url, **kwargs: _Resp({"error": {"message": "rejected"}})
    with pytest.raises(BroadcastError, match="rejected"):
        a.broadcast([BLOCKBOOK_TX_RAW])


def test_blockbook_without_totalpages_walks_until_nothing_new():
    """``totalPages`` steers the walk, but a fork that omits it must fall back to
    the nothing-new stop, not end after page one with a hole in the history."""
    a = DashAdapter(BLOCKBOOK)
    script = [
        {"txs": 2, "transactions": [{**BLOCKBOOK_TX, "txid": "a1" * 32}]},
        {"txs": 2, "transactions": [{**BLOCKBOOK_TX, "txid": "a2" * 32}]},
        {"txs": 2},
    ]
    asked = []

    def fake_get(url, **kwargs):
        asked.append(kwargs["params"]["page"])
        return _Resp(script[len(asked) - 1])

    a._get = fake_get
    result = a.address_txs("Xa")
    assert [t.txid for t in result.transactions] == ["a1" * 32, "a2" * 32]
    assert asked == [1, 2, 3]


def test_blockbook_rewalks_when_only_the_mempool_count_moved():
    a = DashAdapter(BLOCKBOOK)

    def page(n, unconfirmed, txid):
        return {
            "page": n,
            "totalPages": 2,
            "txs": 2,
            "unconfirmedTxs": unconfirmed,
            "transactions": [{**BLOCKBOOK_TX, "txid": txid}],
        }

    script = [page(1, 1, "a1" * 32), page(2, 0, "a2" * 32)]
    script += [page(1, 0, "b1" * 32), page(2, 0, "b2" * 32)]
    asked = []

    def fake_get(url, **kwargs):
        asked.append(kwargs["params"]["page"])
        return _Resp(script[len(asked) - 1])

    a._get = fake_get
    result = a.address_txs("Xa")
    assert [t.txid for t in result.transactions] == ["b1" * 32, "b2" * 32]


def test_a_walk_that_fails_midway_restarts_from_page_one_elsewhere():
    """Insight dies on page 2: the history comes wholly from Blockbook, never a
    first page from one explorer glued to a second page from the other."""
    a = DashAdapter((INSIGHT, BLOCKBOOK))
    insight_pages = [{"totalItems": 2, "items": [{**INSIGHT_TX, "txid": "1a" * 32}]}]
    bb = {
        "page": 1,
        "totalPages": 1,
        "txs": 2,
        "transactions": [
            {**BLOCKBOOK_TX, "txid": "b1" * 32},
            {**BLOCKBOOK_TX, "txid": "b2" * 32},
        ],
    }

    def fake_get(url, **kwargs):
        if url.startswith(INSIGHT):
            return _Resp(insight_pages.pop(0)) if insight_pages else _Resp(status=502)
        return _Resp(bb)

    a._get = fake_get
    result = a.address_txs("Xa")
    assert [t.txid for t in result.transactions] == ["b1" * 32, "b2" * 32]


def test_the_failover_wraps_back_to_the_first_endpoint():
    a = DashAdapter((INSIGHT, BLOCKBOOK))
    a.api_url = BLOCKBOOK  # pinned there by an earlier failure
    urls = []

    def fake_get(url, **kwargs):
        urls.append(url)
        if url.startswith(BLOCKBOOK):
            return _Resp(status=503)
        return _Resp(INSIGHT_USED_EMPTY)

    a._get = fake_get
    assert a.address_info("Xa").has_history
    assert urls == [f"{BLOCKBOOK}/address/Xa", f"{INSIGHT}/addr/Xa"]
    assert a.api_url == INSIGHT


def test_a_throttled_insight_falls_over():
    from swapsack.net import RateLimited

    a = DashAdapter((INSIGHT, BLOCKBOOK))

    def fake_get(url, **kwargs):
        if url.startswith(INSIGHT):
            raise RateLimited("insight.example.com: HTTP 429")
        return _Resp(BLOCKBOOK_USED_EMPTY)

    a._get = fake_get
    assert a.address_info("Xa").has_history
