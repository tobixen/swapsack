"""Tests for wallet transaction/output history assembly (pure, injected fetch).

The assembly is deliberately free of I/O: an adapter hands it already-parsed
``TxSummary`` objects, so the netting, the spent-output linking and the ordering
can all be tested offline against hand-built transactions.
"""

import pytest

from swapsack.chains.base import TxEntry, TxSummary
from swapsack.chains.history import (
    AddressTxs,
    Output,
    WalletTx,
    collect_pages,
    wallet_history,
)
from swapsack.net import HostUnreachable, RateLimited

# Two wallet addresses (one receive, one change) and one stranger.
RECV = "bc1qrecv"
CHANGE = "bc1qchange"
VAULT = "bc1qvault"
RECORDS = (("m/84'/0'/0'/0/0", RECV), ("m/84'/0'/0'/1/0", CHANGE))

FUNDING = TxSummary(
    txid="aa" * 32,
    confirmed=True,
    block_height=900_000,
    block_time=1_756_000_000,
    fee=200,
    vsize=110,
    inputs=(TxEntry(value=1_000_000, address="bc1qstranger", txid="00" * 32, vout=0),),
    outputs=(TxEntry(value=500_000, address=RECV),),
)

# Spends the funding output into a Chainflip-style vault deposit with a memo,
# and returns the remainder to the wallet's own change address.
DEPOSIT = TxSummary(
    txid="bb" * 32,
    confirmed=False,
    block_height=None,
    block_time=None,
    fee=1_000,
    vsize=200,
    inputs=(TxEntry(value=500_000, address=RECV, txid="aa" * 32, vout=0),),
    outputs=(
        TxEntry(value=300_000, address=VAULT),
        TxEntry(value=0, op_return=True, op_return_data=b"\x00swap-request"),
        TxEntry(value=199_000, address=CHANGE),
    ),
)


def fetch(mapping, truncated=()):
    """An ``address_txs`` stub: which transactions each address takes part in."""

    def address_txs(address):
        return AddressTxs(
            transactions=list(mapping.get(address, ())),
            truncated=address in truncated,
        )

    return address_txs


def run(mapping, records=RECORDS):
    return wallet_history(records=records, address_txs=fetch(mapping))


def test_nets_each_transaction_against_the_wallet():
    history = run({RECV: [DEPOSIT, FUNDING], CHANGE: [DEPOSIT]})
    by_txid = {tx.txid: tx for tx in history.transactions}
    assert by_txid["aa" * 32].received == 500_000
    assert by_txid["aa" * 32].sent == 0
    assert by_txid["aa" * 32].net == 500_000
    # The deposit spends 500k of ours and returns 199k of it as change.
    assert by_txid["bb" * 32].sent == 500_000
    assert by_txid["bb" * 32].received == 199_000
    assert by_txid["bb" * 32].net == -301_000


def test_a_transaction_touching_two_wallet_addresses_is_listed_once():
    history = run({RECV: [DEPOSIT, FUNDING], CHANGE: [DEPOSIT]})
    assert [tx.txid for tx in history.transactions].count("bb" * 32) == 1


def test_spent_outputs_name_the_transaction_that_spent_them():
    history = run({RECV: [DEPOSIT, FUNDING], CHANGE: [DEPOSIT]})
    by_outpoint = {o.outpoint: o for o in history.outputs}
    spent = by_outpoint[f"{'aa' * 32}:0"]
    assert spent.spent and spent.spent_by == "bb" * 32
    unspent = by_outpoint[f"{'bb' * 32}:2"]
    assert not unspent.spent and unspent.spent_by is None


def test_outputs_carry_their_derivation_path():
    history = run({RECV: [DEPOSIT, FUNDING], CHANGE: [DEPOSIT]})
    paths = {o.outpoint: o.path for o in history.outputs}
    assert paths[f"{'aa' * 32}:0"] == "m/84'/0'/0'/0/0"
    assert paths[f"{'bb' * 32}:2"] == "m/84'/0'/0'/1/0"


def test_only_wallet_outputs_become_utxo_records():
    """The vault output and the OP_RETURN are not ours; they must not be listed
    as spendable (nor as spent) money."""
    history = run({RECV: [DEPOSIT, FUNDING], CHANGE: [DEPOSIT]})
    assert {o.outpoint for o in history.outputs} == {
        f"{'aa' * 32}:0",
        f"{'bb' * 32}:2",
    }


def test_a_memo_marks_the_transaction_as_a_swap_deposit():
    history = run({RECV: [DEPOSIT, FUNDING], CHANGE: [DEPOSIT]})
    by_txid = {tx.txid: tx for tx in history.transactions}
    assert by_txid["bb" * 32].memo == b"\x00swap-request"
    assert by_txid["bb" * 32].has_op_return
    assert not by_txid["aa" * 32].has_op_return
    assert by_txid["aa" * 32].memo is None


def test_counterparties_exclude_our_own_change_and_the_op_return():
    """ "Where did it go" must name the vault, not the change coming back to us —
    that is the whole question when a swap deposit goes missing."""
    history = run({RECV: [DEPOSIT, FUNDING], CHANGE: [DEPOSIT]})
    by_txid = {tx.txid: tx for tx in history.transactions}
    assert by_txid["bb" * 32].counterparties == (VAULT,)
    assert by_txid["aa" * 32].counterparties == ()


def test_unconfirmed_first_then_newest_block_first():
    older = TxSummary(
        txid="cc" * 32,
        confirmed=True,
        block_height=800_000,
        block_time=1_700_000_000,
        fee=100,
        vsize=110,
        inputs=(),
        outputs=(TxEntry(value=1, address=RECV),),
    )
    history = run({RECV: [DEPOSIT, FUNDING, older], CHANGE: [DEPOSIT]})
    assert [tx.txid for tx in history.transactions] == [
        "bb" * 32,  # unconfirmed
        "aa" * 32,  # block 900000
        "cc" * 32,  # block 800000
    ]


def test_an_explicit_spent_by_from_the_source_is_kept():
    """Insight reports ``spentTxId`` directly; it must win over local inference,
    since it also covers a spend whose transaction we never fetched."""
    funding = TxSummary(
        txid="dd" * 32,
        confirmed=True,
        block_height=900_001,
        block_time=1_756_000_100,
        fee=200,
        vsize=110,
        inputs=(),
        outputs=(TxEntry(value=7, address=RECV, spent_by="ee" * 32),),
    )
    history = run({RECV: [funding]})
    assert history.outputs[0].spent_by == "ee" * 32


def test_confirmed_flag_and_height_ride_along_to_the_outputs():
    history = run({RECV: [DEPOSIT, FUNDING], CHANGE: [DEPOSIT]})
    by_outpoint = {o.outpoint: o for o in history.outputs}
    assert by_outpoint[f"{'aa' * 32}:0"].confirmed
    assert by_outpoint[f"{'aa' * 32}:0"].block_height == 900_000
    assert not by_outpoint[f"{'bb' * 32}:2"].confirmed
    assert by_outpoint[f"{'bb' * 32}:2"].block_height is None


def test_balance_of_the_unspent_outputs_matches_the_netting():
    history = run({RECV: [DEPOSIT, FUNDING], CHANGE: [DEPOSIT]})
    assert history.unspent_total == 199_000
    assert sum(tx.net for tx in history.transactions) == 199_000


def test_a_truncated_address_history_is_flagged_not_guessed():
    """If a page limit cut the history short, an output we saw no spend for may
    simply have been spent out of sight — say so rather than calling it money."""
    history = wallet_history(
        records=RECORDS,
        address_txs=fetch({RECV: [FUNDING]}, truncated=(RECV,)),
    )
    assert history.truncated == (RECV,)
    assert not run({RECV: [FUNDING]}).truncated


def test_empty_wallet_yields_nothing_rather_than_failing():
    history = run({})
    assert history.transactions == []
    assert history.outputs == []
    assert history.unspent_total == 0


def test_types_are_what_the_cli_renders():
    history = run({RECV: [FUNDING]})
    assert isinstance(history.transactions[0], WalletTx)
    assert isinstance(history.outputs[0], Output)


# --- a throttled walk degrades rather than dying -------------------------------


def _pages(*batches: object):
    """A ``fetch_page`` over scripted pages; an exception entry is raised."""
    script = list(batches)

    def fetch_page(_cursor):
        outcome = script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, (outcome[-1].txid if outcome else None)

    return fetch_page


def test_a_throttled_page_ends_the_walk_as_truncated():
    """Every explorer saying "not now" halfway through a walk is exactly what
    ``truncated`` exists for: what we have, marked incomplete, beats a
    traceback that loses the pages already fetched."""
    walked = collect_pages(_pages([FUNDING], RateLimited("throttled")))
    assert [tx.txid for tx in walked.transactions] == [FUNDING.txid]
    assert walked.truncated


def test_a_throttle_on_the_very_first_page_is_still_only_incomplete():
    walked = collect_pages(_pages(RateLimited("throttled")))
    assert walked.transactions == []
    assert walked.truncated


def test_an_unreachable_host_still_raises():
    # Only a *throttle* degrades. A host that never answered is not a short
    # history, it is no history — and `history` must not imply otherwise.
    with pytest.raises(HostUnreachable):
        collect_pages(_pages(HostUnreachable("no answer")))
