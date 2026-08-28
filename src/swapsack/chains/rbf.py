"""Planning a BIP125 fee-bump replacement — pure maths, no I/O and no signing.

A stuck low-fee transaction can be replaced by one that spends the *same*
inputs and pays a higher fee. Every spend this wallet builds already signals
opt-in RBF (``RBF_SEQUENCE`` in :mod:`swapsack.chains.utxo`); this module works
out what the replacement must look like, and :meth:`UtxoTxBuilder.build_replacement`
constructs it.

The one rule everything here serves: **only the change output may move.** A
THORChain/Maya deposit's vault output and OP_RETURN memo are what the protocol
matched the quote against — shift either and the swap fails or refunds — and a
plain send's recipient must obviously get what they were promised. So the bump
is taken out of the change and nothing else is touched, which is also why a
transaction with no change output cannot be bumped here at all.

Deliberately narrow: it re-plans the exact shape this wallet builds (one
recipient/vault output, an optional OP_RETURN, one change output) and refuses
anything else rather than guessing. Guessing wrong is an irreversible mis-send.
"""

from __future__ import annotations

import dataclasses
import math
from typing import TYPE_CHECKING

from swapsack.chains.coins import P2WPKH, ScriptParams, Utxo, estimate_vsize
from swapsack.verify import TxOutput

if TYPE_CHECKING:
    from collections.abc import Mapping

    from swapsack.chains.btc import TxSummary

# An nSequence at or above this does not signal BIP125 opt-in RBF; below it
# does. (0xffffffff also disables nLockTime, but the signalling boundary is the
# only thing that matters here.)
RBF_SEQUENCE_MAX = 0xFFFFFFFE

# BIP125 rule 4: on top of paying more in absolute terms, the replacement must
# pay for its own relay bandwidth at the node's incremental relay fee — 1 sat/vB
# in Bitcoin Core's default policy. A replacement that misses this is rejected
# by every standard node, so it is a floor, not a preference.
INCREMENTAL_RELAY_FEE = 1.0


class NotReplaceable(RuntimeError):
    """This transaction cannot be fee-bumped, and why.

    Always a refusal to build, never a warning: each case is one where a
    replacement would either be rejected by the network or would have to change
    something the user did not ask to change.
    """


@dataclasses.dataclass(frozen=True)
class Replacement:
    """The rebuilt transaction: same inputs, same outputs, more fee, less change."""

    inputs: list[Utxo]  # the original's outpoints, with our derivation paths
    outputs: list[TxOutput]  # the original's outputs, in order, change reduced
    fee: int  # the new absolute fee, in sats
    old_fee: int
    vsize: int  # the original's, as the chain measured it
    change_index: int
    change_address: str
    recipient: str  # the vault (a swap deposit) or the payee (a plain send)
    amount: int
    memo: bytes | None  # the OP_RETURN payload verbatim; None for a plain send

    @property
    def change(self) -> int:
        return self.outputs[self.change_index].value

    @property
    def bump(self) -> int:
        return self.fee - self.old_fee

    @property
    def fee_rate(self) -> float:
        return self.fee / self.vsize if self.vsize else 0.0

    @property
    def old_fee_rate(self) -> float:
        return self.old_fee / self.vsize if self.vsize else 0.0


def replacement_fee(
    *,
    old_fee: int,
    vsize: int,
    fee_rate: float,
    extra_fee: int = 0,
    replacement_vsize: int | None = None,
    incremental_relay_fee: float = INCREMENTAL_RELAY_FEE,
) -> int:
    """The fee a replacement must pay to reach ``fee_rate`` and still relay.

    ``vsize`` is the original's, as the chain measured it, and prices the
    requested rate. The BIP125 relay floor is a different quantity: rule 4 is
    stated in terms of the **replacement's** size, and the replacement is not
    signed yet — a DER signature is 71 or 72 bytes depending on the nonce, so
    the two need not agree to the vbyte. ``replacement_vsize`` is an upper bound
    on what is about to be built (:func:`plan_replacement` derives it from the
    shape); without one the original's size is assumed, which is the historical
    behaviour and can come a sat short.

    ``extra_fee`` is the child-pays-for-parent surcharge for any inputs whose
    own parents are still unconfirmed — bumping this transaction alone would
    otherwise leave the package under the targeted rate, which is precisely the
    stall being fixed.
    """
    target = math.ceil(vsize * fee_rate) + extra_fee
    relay_vsize = vsize if replacement_vsize is None else replacement_vsize
    floor = old_fee + math.ceil(relay_vsize * incremental_relay_fee)
    return max(target, floor)


def _inputs_with_paths(tx: TxSummary, owned: Mapping[str, str]) -> list[Utxo]:
    """The original's inputs as spendable UTXOs, or refuse if we can't sign one."""
    utxos: list[Utxo] = []
    for entry in tx.inputs:
        if entry.txid is None or entry.vout is None:
            raise NotReplaceable(
                "the explorer did not report this transaction's input outpoint; "
                "a replacement must spend the very same outputs and there is "
                "nothing here to spend"
            )
        path = owned.get(entry.address or "")
        if path is None:
            raise NotReplaceable(
                f"input {entry.txid}:{entry.vout} pays from {entry.address}, which "
                "is not an address of this wallet — the replacement could not be "
                "signed"
            )
        utxos.append(
            Utxo(
                txid=entry.txid,
                vout=entry.vout,
                value=entry.value,
                address=entry.address or "",
                path=path,
            )
        )
    if not utxos:
        raise NotReplaceable("transaction has no inputs to re-spend")
    return utxos


def _change_index(tx: TxSummary, owned: Mapping[str, str], change_prefix: str) -> int:
    """Which output is our change — the only one the bump is allowed to shrink.

    Any output paying us is a candidate. Usually there is exactly one; a send to
    our *own* receive address leaves two, and there the internal (change) branch
    of the HD account settles it. Anything still ambiguous is refused: picking
    the wrong one shrinks a payment the user meant to make.
    """
    ours = [
        i
        for i, o in enumerate(tx.outputs)
        if o.op_return_data is None and not o.op_return and o.address in owned
    ]
    if not ours:
        raise NotReplaceable(
            "this transaction has no change output to take the bump from (a "
            "sweep spends everything). Raising its fee would mean adding "
            "another input, which changes what is being spent — not something "
            "this command will do behind your back"
        )
    if len(ours) > 1:
        internal = [
            i for i in ours if owned[tx.outputs[i].address].startswith(change_prefix)
        ]
        if len(internal) != 1:
            raise NotReplaceable(
                f"ambiguous change: {len(ours)} outputs pay this wallet and "
                f"{len(internal)} of them are on the change branch, so which one "
                "to shrink is a guess"
            )
        ours = internal
    return ours[0]


def _too_little_change(
    tx: TxSummary,
    old_change: int,
    fee: int,
    script: ScriptParams,
    *,
    extra_fee: int = 0,
    replacement_vsize: int | None = None,
    incremental_relay_fee: float = INCREMENTAL_RELAY_FEE,
) -> str:
    """Why the change output cannot fund this bump — and the best rate that fits.

    The suggested rate is rounded *down* to the two decimals the user would type
    back, and checked against the BIP125 relay floor, so re-running with it
    plans instead of refusing again. When even the floor does not fit there is
    no such rate, and saying so beats sending the user round in a circle.

    ``extra_fee`` has to be subtracted from the ceiling before a rate is derived
    from it: the CPFP surcharge is paid on top of the rate, not out of it, so a
    rate worked out as if it were absent is one the next run refuses with the
    identical number — dead-ending the very case ``bump`` exists for.
    """
    relay_vsize = tx.vsize if replacement_vsize is None else replacement_vsize
    headroom = old_change - script.dust
    ceiling = tx.fee + headroom  # the largest fee the change can still pay for
    floor = tx.fee + math.ceil(relay_vsize * incremental_relay_fee)
    over = (
        f"bumping to {fee} sats would leave {old_change - (fee - tx.fee)} sats of "
        f"change, under the {script.dust}-sat dust limit. The change output has "
        f"only {headroom} sats of headroom above dust"
    )
    if extra_fee > ceiling:
        return (
            f"{over}, which is less than the {extra_fee} sats its own unconfirmed "
            "parent(s) need to reach this rate (child-pays-for-parent) — so this "
            "transaction cannot be fee-bumped. Spending its change with "
            "--allow-unconfirmed lifts the whole package instead"
        )
    if floor > ceiling:
        return (
            f"{over}, which is less than the {floor - tx.fee} sats BIP125 requires "
            "on top of the old fee just to relay a replacement — so this "
            "transaction cannot be fee-bumped. Child-pays-for-parent (spend its "
            "change with --allow-unconfirmed) is the remaining option"
        )
    # What is left for the rate itself, once the surcharge has taken its cut.
    for_rate = ceiling - extra_fee
    best_rate = math.floor(for_rate / tx.vsize * 100) / 100 if tx.vsize else 0.0
    return (
        f"{over}, so the most it can be bumped to is {best_rate:.2f} sat/vB "
        f"(--fee-rate {best_rate:.2f})"
    )


def plan_replacement(
    tx: TxSummary,
    *,
    owned: Mapping[str, str],
    change_prefix: str,
    fee_rate: float,
    extra_fee: int = 0,
    script: ScriptParams = P2WPKH,
    incremental_relay_fee: float = INCREMENTAL_RELAY_FEE,
) -> Replacement:
    """Plan a fee-bumped rebuild of the mempool transaction ``tx``.

    ``owned`` maps this wallet's addresses to their derivation paths (what a
    gap-limit account scan returns); ``change_prefix`` is the account's internal
    branch, e.g. ``m/84'/0'/0'/1/``. Raises :class:`NotReplaceable` — with the
    reason — for anything that cannot or should not be replaced.
    """
    if tx.confirmed:
        raise NotReplaceable(
            f"already confirmed in block {tx.block_height} — a mined transaction "
            "cannot be replaced"
        )
    if not tx.signals_rbf:
        raise NotReplaceable(
            "this transaction does not signal BIP125 opt-in RBF, so standard "
            "nodes will not accept a replacement for it. Child-pays-for-parent "
            "(spend its output with --allow-unconfirmed) is the way to unstick it"
        )

    inputs = _inputs_with_paths(tx, owned)
    change_index = _change_index(tx, owned, change_prefix)

    memos = [o for o in tx.outputs if o.op_return]
    if len(memos) > 1:
        raise NotReplaceable(
            f"transaction carries {len(memos)} OP_RETURN outputs; this wallet "
            "builds at most one"
        )
    if memos and memos[0].op_return_data is None:
        raise NotReplaceable(
            "could not read this transaction's OP_RETURN payload, and a swap "
            "memo must be reproduced byte-for-byte"
        )
    memo = memos[0].op_return_data if memos else None

    payees = [
        i for i, o in enumerate(tx.outputs) if i != change_index and not o.op_return
    ]
    if len(payees) != 1:
        raise NotReplaceable(
            f"expected one recipient output besides the change, found "
            f"{len(payees)} — this is not a transaction shape this wallet builds"
        )
    payee = tx.outputs[payees[0]]

    # An upper bound on the replacement's own size, for the BIP125 relay floor:
    # the shape is the original's (same inputs, same outputs), but the signatures
    # are not made yet, and `estimate_vsize` sizes them at their longest. The
    # measured original is kept as the lower bound — an estimator that came out
    # under it would weaken a floor the network enforces.
    n_value_outputs = len(tx.outputs) - (1 if memo is not None else 0)
    replacement_vsize = max(
        tx.vsize,
        estimate_vsize(len(inputs), n_value_outputs, len(memo or b""), script=script),
    )

    fee = replacement_fee(
        old_fee=tx.fee,
        vsize=tx.vsize,
        fee_rate=fee_rate,
        extra_fee=extra_fee,
        replacement_vsize=replacement_vsize,
        incremental_relay_fee=incremental_relay_fee,
    )
    old_change = tx.outputs[change_index].value
    change = old_change - (fee - tx.fee)
    if change < script.dust:
        raise NotReplaceable(
            _too_little_change(
                tx,
                old_change,
                fee,
                script,
                extra_fee=extra_fee,
                replacement_vsize=replacement_vsize,
                incremental_relay_fee=incremental_relay_fee,
            )
        )

    outputs = [
        TxOutput(address=o.address, value=o.value, op_return_data=o.op_return_data)
        for o in tx.outputs
    ]
    outputs[change_index] = dataclasses.replace(outputs[change_index], value=change)

    return Replacement(
        inputs=inputs,
        outputs=outputs,
        fee=fee,
        old_fee=tx.fee,
        vsize=tx.vsize,
        change_index=change_index,
        change_address=outputs[change_index].address or "",
        recipient=payee.address or "",
        amount=payee.value,
        memo=memo,
    )
