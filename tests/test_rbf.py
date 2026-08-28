"""Planning a BIP125 fee-bump replacement — the pure half, no network, no signing.

Every rule here is a way to lose money if it is got wrong: a replacement that
pays a different vault, a different amount, or a mangled memo is an irreversible
mis-send, and one that quietly drops the change output is a donation to a miner.
So the planner is deliberately narrow — it rebuilds the exact shape this wallet
creates and refuses everything else, loudly.
"""

import dataclasses

import pytest

# `parse_tx_summary` lives beside the Esplora layer, whose bitcoinlib import has
# noisy side effects that `filterwarnings = ["error"]` would turn into spurious
# failures. Mirrors the other bitcoinlib-backed suites.
pytest.importorskip("bitcoinlib")

from swapsack.chains.btc import parse_tx_summary  # noqa: E402
from swapsack.chains.coins import P2WPKH  # noqa: E402
from swapsack.chains.rbf import (  # noqa: E402
    NotReplaceable,
    plan_replacement,
    replacement_fee,
)

VAULT = "bc1qct4mxayrdy96d4py20l4u02mu06r667f42p9fp"
SPENDER = "bc1qspender"
CHANGE = "bc1qchange"
RECEIVE_PATH = "m/84'/0'/0'/0/0"
CHANGE_PATH = "m/84'/0'/0'/1/0"
CHANGE_PREFIX = "m/84'/0'/0'/1/"
OWNED = {SPENDER: RECEIVE_PATH, CHANGE: CHANGE_PATH}
MEMO = b"=:ETH.ETH:0x1111111111111111111111111111111111111111"
PARENT = "dd" * 32

# A mempool swap deposit as Esplora reports it: vault output, OP_RETURN memo,
# change back to us. weight 1000 -> vsize 250; fee 250 -> exactly 1 sat/vB.
DEPOSIT = {
    "txid": "cc" * 32,
    "weight": 1000,
    "fee": 250,
    "status": {"confirmed": False},
    "vin": [
        {
            "txid": PARENT,
            "vout": 1,
            "sequence": 0xFFFFFFFD,
            "prevout": {
                "scriptpubkey_type": "v0_p2wpkh",
                "scriptpubkey_address": SPENDER,
                "value": 1_100_250,
            },
        }
    ],
    "vout": [
        {
            "scriptpubkey_type": "v0_p2wpkh",
            "scriptpubkey_address": VAULT,
            "value": 1_000_000,
        },
        {
            "scriptpubkey": (b"\x6a" + bytes([len(MEMO)]) + MEMO).hex(),
            "scriptpubkey_type": "op_return",
            "value": 0,
        },
        {
            "scriptpubkey_type": "v0_p2wpkh",
            "scriptpubkey_address": CHANGE,
            "value": 100_000,
        },
    ],
}


def _tx(**overrides):
    return parse_tx_summary({**DEPOSIT, **overrides})


def _plan(tx=None, **kwargs):
    return plan_replacement(
        tx if tx is not None else _tx(),
        owned=kwargs.pop("owned", OWNED),
        change_prefix=kwargs.pop("change_prefix", CHANGE_PREFIX),
        fee_rate=kwargs.pop("fee_rate", 10.0),
        **kwargs,
    )


# --- what the parser must now carry so a replacement can be rebuilt ---------


def test_tx_summary_carries_the_prevouts_and_sequences():
    """Rebuilding needs the inputs themselves, not just their values.

    ``TxSummary`` was a display type; a replacement spends the *same* outpoints,
    so the outpoint (txid/vout) and the RBF signal have to survive the parse.
    """
    tx = _tx()
    assert [(i.txid, i.vout, i.sequence) for i in tx.inputs] == [
        (PARENT, 1, 0xFFFFFFFD)
    ]
    assert tx.signals_rbf is True
    assert _tx(vin=[{**DEPOSIT["vin"][0], "sequence": 0xFFFFFFFF}]).signals_rbf is False


def test_tx_summary_decodes_the_op_return_payload():
    """The memo must come back byte-identical — a swap's destination is in it."""
    assert [o.op_return_data for o in _tx().outputs] == [None, MEMO, None]


# --- the fee maths ----------------------------------------------------------


def test_replacement_fee_targets_the_requested_rate():
    assert replacement_fee(old_fee=250, vsize=250, fee_rate=10.0) == 2500


def test_replacement_fee_never_falls_under_the_bip125_relay_floor():
    """BIP125 rule 4: pay for the replacement's own bandwidth on top of the old fee.

    Asking for the rate the transaction already pays would otherwise produce an
    identical fee, which every node rejects as a non-replacement.
    """
    assert replacement_fee(old_fee=250, vsize=250, fee_rate=1.0) == 250 + 250
    assert replacement_fee(old_fee=250, vsize=250, fee_rate=0.1) == 500


def test_replacement_fee_adds_the_cpfp_surcharge():
    """A bump of a spend of unconfirmed money still has to drag its parents."""
    assert replacement_fee(old_fee=250, vsize=250, fee_rate=10.0, extra_fee=800) == 3300


# --- planning the replacement ----------------------------------------------


def test_plan_takes_the_bump_out_of_the_change():
    plan = _plan()
    assert plan.fee == 2500
    assert plan.old_fee == 250
    assert plan.bump == 2250
    assert plan.change == 100_000 - 2250
    assert plan.vsize == 250
    assert round(plan.fee_rate, 4) == 10.0
    # Inputs and outputs must still balance against the new fee.
    assert (
        sum(i.value for i in plan.inputs)
        == sum(o.value for o in plan.outputs) + plan.fee
    )


def test_plan_keeps_the_vault_output_and_memo_byte_identical():
    """The whole point: only the change value may move.

    A THORChain/Maya memo carries a min-out limit and the payout address, and
    the vault expects the amount the memo was quoted for — change either and the
    deposit fails or refunds.
    """
    plan = _plan()
    assert plan.recipient == VAULT
    assert plan.amount == 1_000_000
    assert plan.memo == MEMO
    assert [(o.address, o.value, o.op_return_data) for o in plan.outputs] == [
        (VAULT, 1_000_000, None),
        (None, 0, MEMO),
        (CHANGE, 97_750, None),
    ]
    assert plan.change_address == CHANGE


def test_plan_carries_the_inputs_with_their_derivation_paths():
    """Without the path the replacement cannot be signed."""
    (utxo,) = _plan().inputs
    assert (utxo.txid, utxo.vout, utxo.value) == (PARENT, 1, 1_100_250)
    assert utxo.address == SPENDER
    assert utxo.path == RECEIVE_PATH


def test_plan_of_a_plain_send_has_no_memo():
    tx = _tx(vout=[DEPOSIT["vout"][0], DEPOSIT["vout"][2]])
    plan = _plan(tx)
    assert plan.memo is None
    assert [o.op_return_data for o in plan.outputs] == [None, None]


def test_plan_adds_the_cpfp_surcharge_it_is_given():
    assert _plan(extra_fee=800).fee == 3300


# --- everything it must refuse ---------------------------------------------


def test_refuses_a_confirmed_transaction():
    tx = _tx(status={"confirmed": True, "block_height": 959260})
    with pytest.raises(NotReplaceable, match="959260"):
        _plan(tx)


def test_refuses_a_transaction_that_does_not_signal_rbf():
    """A non-signalling tx is rejected by standard node policy, not by us alone.

    Better to say so than to build, sign and broadcast a replacement every peer
    will drop.
    """
    tx = _tx(vin=[{**DEPOSIT["vin"][0], "sequence": 0xFFFFFFFF}])
    with pytest.raises(NotReplaceable, match="does not signal"):
        _plan(tx)


def test_refuses_an_input_this_wallet_cannot_sign():
    with pytest.raises(NotReplaceable, match=SPENDER):
        _plan(owned={CHANGE: CHANGE_PATH})


def test_refuses_a_sweep_with_no_change_output():
    """Nothing to take the bump from without adding an input."""
    tx = _tx(vout=DEPOSIT["vout"][:2])
    with pytest.raises(NotReplaceable, match="no change output"):
        _plan(tx)


def _with_change(value: int):
    return _tx(vout=[*DEPOSIT["vout"][:2], {**DEPOSIT["vout"][2], "value": value}])


def test_refuses_when_the_bump_would_push_the_change_under_dust():
    """Refuse with the highest rate that *would* work, rather than half-doing it.

    Dropping the change output instead would pay the whole remainder to the
    miner — a much bigger bump than was asked for, decided by us.
    """
    with pytest.raises(NotReplaceable) as excinfo:
        _plan(_with_change(1000))
    message = str(excinfo.value)
    assert "dust" in message
    # Headroom is 1000 - 294 dust = 706 sats on top of the old 250-sat fee,
    # i.e. 956 sats over 250 vB -> 3.82 sat/vB once rounded down.
    assert "3.82" in message
    assert "--fee-rate" in message


def test_the_highest_workable_rate_is_actually_workable():
    """The rate the dust refusal suggests must itself plan, not refuse again.

    Rounding the suggestion up, or deriving it without the BIP125 relay floor,
    would send the user round in a circle.
    """
    plan = _plan(_with_change(1000), fee_rate=3.82)
    assert plan.change >= P2WPKH.dust
    assert plan.fee > 250


def test_refuses_outright_when_the_change_cannot_even_fund_the_relay_floor():
    """Below the BIP125 increment there is no bump to offer — say so, don't hint.

    The floor is 1 sat/vB of the replacement's own size on top of the old fee;
    a change output smaller than that cannot pay it at any requested rate.
    """
    with pytest.raises(NotReplaceable) as excinfo:
        _plan(_with_change(500))
    message = str(excinfo.value)
    assert "cannot be fee-bumped" in message
    assert "--fee-rate" not in message  # no rate would work; do not suggest one


def test_refuses_a_shape_it_did_not_build():
    """Two external outputs is not a shape this wallet creates — do not guess."""
    tx = _tx(
        vout=[
            *DEPOSIT["vout"],
            {
                "scriptpubkey_type": "v0_p2wpkh",
                "scriptpubkey_address": "bc1qstranger",
                "value": 1000,
            },
        ]
    )
    with pytest.raises(NotReplaceable, match="one recipient"):
        _plan(tx)


def test_a_self_send_takes_the_change_from_the_internal_branch():
    """Paying our own receive address leaves two owned outputs; only one is change."""
    tx = _tx(
        vout=[
            {**DEPOSIT["vout"][0], "scriptpubkey_address": SPENDER},
            DEPOSIT["vout"][1],
            DEPOSIT["vout"][2],
        ]
    )
    plan = _plan(tx)
    assert plan.recipient == SPENDER
    assert plan.change_address == CHANGE
    assert plan.change == 97_750


def test_refuses_when_the_change_output_is_ambiguous():
    """Two internal-branch outputs: guessing which to shrink is not our call."""
    second = "bc1qchange2"
    tx = _tx(
        vout=[
            *DEPOSIT["vout"],
            {
                "scriptpubkey_type": "v0_p2wpkh",
                "scriptpubkey_address": second,
                "value": 50_000,
            },
        ]
    )
    with pytest.raises(NotReplaceable, match="ambiguous"):
        _plan(tx, owned={**OWNED, second: "m/84'/0'/0'/1/1"})


def test_refuses_a_prevout_the_explorer_did_not_report():
    """Without the outpoint there is nothing to re-spend — never invent one."""
    tx = _tx()
    stripped = dataclasses.replace(
        tx, inputs=(dataclasses.replace(tx.inputs[0], txid=None),)
    )
    with pytest.raises(NotReplaceable, match="outpoint"):
        _plan(stripped)


# --- sizing the relay floor, and suggesting a rate that actually plans -------


def test_the_relay_floor_is_sized_from_the_replacement_not_the_original():
    """BIP125 rule 4 is about the *replacement's* size, which is not measured yet.

    The replacement is unsigned when this is planned, and a DER signature is a
    byte or two shorter or longer run to run — so the floor is taken from an
    upper bound on the shape about to be built, never from what the chain
    happened to measure for the original. Sizing it from the original is how a
    minimum-increment bump comes up a sat short and the node rejects it.
    """
    tx = _tx(weight=600)  # vsize 150, under the 204 vB this shape estimates at
    plan = _plan(tx, fee_rate=0.1)  # far below the floor: the floor decides
    assert plan.fee == 250 + 204


def test_replacement_fee_floors_on_the_replacement_vsize_when_given_one():
    assert (
        replacement_fee(old_fee=250, vsize=150, fee_rate=0.1, replacement_vsize=204)
        == 250 + 204
    )


def test_the_suggested_rate_accounts_for_the_cpfp_surcharge():
    """The one scenario `bump` exists for must not dead-end.

    With unconfirmed ancestors the fee carries a CPFP surcharge on top of the
    rate, so a ceiling worked out from the rate alone is unreachable — and
    re-running with the suggestion is refused with the identical number.
    """
    with pytest.raises(NotReplaceable) as excinfo:
        _plan(_with_change(1000), extra_fee=400)
    message = str(excinfo.value)
    assert "2.22" in message  # (956 sats of ceiling - 400 surcharge) / 250 vB
    plan = _plan(_with_change(1000), fee_rate=2.22, extra_fee=400)
    assert plan.change >= P2WPKH.dust


def test_a_surcharge_bigger_than_the_change_cannot_be_bumped_at_all():
    """No rate works when the surcharge alone overruns the change's headroom."""
    with pytest.raises(NotReplaceable) as excinfo:
        _plan(_with_change(1000), extra_fee=1200)
    message = str(excinfo.value)
    assert "cannot be fee-bumped" in message
    assert "--fee-rate" not in message
