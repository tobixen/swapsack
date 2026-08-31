"""UTXO coin selection and OP_RETURN encoding — pure, dependency-free logic.

Kept separate from the bitcoinlib-backed adapters so it can be tested without
the ``btc`` extra, and so the money-sensitive selection/fee maths is easy to
read. All amounts are in the chain's base units (sats/duffs). The maths is
parameterized by script type — native segwit (BTC) and legacy P2PKH (DASH)
share one code path, not copies.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Iterable

OP_RETURN_MAX_BYTES = 80
OP_PUSHDATA1 = 0x4C
OP_RETURN_OPCODE = 0x6A

TX_OVERHEAD_VB = 11

# An nSequence at or above this does not signal BIP125 opt-in RBF; below it
# does. (0xffffffff also disables nLockTime, but the signalling boundary is the
# only thing that matters here.) It lives in this leaf module rather than in
# chains/rbf.py because both the bump planner and the neutral transaction model
# in chains/base.py read it, and base.py may not import the planner.
RBF_SEQUENCE_MAX = 0xFFFFFFFE


@dataclasses.dataclass(frozen=True)
class ScriptParams:
    """Per-script-type sizing constants for the fee/dust maths (vbytes, base units)."""

    input_vb: int
    output_vb: int
    dust: int  # an output's value is not worth keeping below this


# Native segwit (BTC): witness-discounted sizes, dust 294.
P2WPKH = ScriptParams(input_vb=68, output_vb=31, dust=294)
# Legacy pay-to-pubkey-hash (DASH; pre-segwit sizes, no witness discount):
# input 32 txid + 4 vout + 1 len + ~107 scriptSig + 4 sequence, output 8 + 1 + 25.
P2PKH = ScriptParams(input_vb=148, output_vb=34, dust=546)


class InsufficientFunds(RuntimeError):
    """Raised when the available UTXOs cannot cover the amount plus fee."""


@dataclasses.dataclass(frozen=True)
class Utxo:
    txid: str
    vout: int
    value: int
    address: str
    path: str | None = None  # HD derivation path, set by the address scanner
    # False = still in the mempool. Spending one is opt-in (--allow-unconfirmed):
    # the parent can still be replaced or evicted, which would invalidate this
    # spend along with it.
    confirmed: bool = True
    # Base units this input's unconfirmed parent is short of the target fee
    # rate; a spend that selects it pays that on top (child-pays-for-parent) so
    # the parent+child package reaches the rate. 0 for a confirmed input.
    ancestor_deficit: int = 0


@dataclasses.dataclass(frozen=True)
class Selection:
    utxos: list[Utxo]
    fee: int
    change: int


# --- child-pays-for-parent -------------------------------------------------
#
# An unconfirmed input is only as fast as the transaction that created it: a
# miner takes the two together or neither. So when we spend one, we pay its
# parent's shortfall on top of our own fee, and the parent+child *package*
# reaches the rate we targeted.


def cpfp_deficit(parent_fee: int, parent_vsize: int, fee_rate: float) -> int:
    """Base units a child must add so its parent reaches ``fee_rate``.

    Zero when the parent already pays the rate — a well-fee'd parent needs no
    help, and CPFP never asks for a refund.
    """
    return max(0, math.ceil(parent_vsize * fee_rate) - parent_fee)


def cpfp_surcharge(utxos: Iterable[Utxo]) -> int:
    """The total deficit of ``utxos``' unconfirmed parents, one parent counted once.

    Two outputs of the same mempool transaction are two inputs but one parent to
    lift; charging per input would overpay by a whole parent fee.
    """
    per_parent = {u.txid: u.ancestor_deficit for u in utxos if not u.confirmed}
    return sum(per_parent.values())


def memo_bytes(memo: str | bytes | None) -> bytes:
    """The OP_RETURN payload for ``memo``; ``None`` (a plain send) gives ``b""``.

    A THORChain/Maya memo is text (``=:ETH.ETH:0x…``), but a Chainflip vault
    swap's parameters are SCALE-encoded *binary* — passing those through
    ``str.encode`` would mangle them, so a memo that is already bytes is taken
    verbatim. One helper rather than the same two-line branch in each UTXO
    builder: a chain that got it wrong would silently broadcast a corrupt memo.
    """
    if memo is None:
        return b""
    return memo.encode() if isinstance(memo, str) else bytes(memo)


def encode_op_return(data: bytes) -> bytes:
    """Encode ``data`` as an OP_RETURN script (``OP_RETURN <push> <data>``)."""
    if len(data) > OP_RETURN_MAX_BYTES:
        raise ValueError(
            f"OP_RETURN data {len(data)} bytes exceeds {OP_RETURN_MAX_BYTES}"
        )
    if len(data) < OP_PUSHDATA1:
        return bytes([OP_RETURN_OPCODE, len(data)]) + data
    return bytes([OP_RETURN_OPCODE, OP_PUSHDATA1, len(data)]) + data


def decode_op_return(script: bytes) -> bytes:
    """Inverse of :func:`encode_op_return`; raises on a non-OP_RETURN script."""
    if not script or script[0] != OP_RETURN_OPCODE:
        raise ValueError("not an OP_RETURN script")
    if len(script) < 2:
        raise ValueError("OP_RETURN script carries no data push")
    if script[1] == OP_PUSHDATA1:
        if len(script) < 3:
            raise ValueError("truncated OP_PUSHDATA1 OP_RETURN script")
        return script[3 : 3 + script[2]]
    return script[2 : 2 + script[1]]


def _op_return_vb(data_len: int) -> int:
    # 8-byte value + 1-byte script-length varint + script (opcode + push + data).
    return 8 + 1 + 2 + data_len


def estimate_vsize(
    n_inputs: int,
    n_outputs: int,
    op_return_len: int,
    *,
    script: ScriptParams = P2WPKH,
) -> int:
    """Estimate transaction vsize for ``script``-type inputs/outputs + OP_RETURN."""
    vsize = TX_OVERHEAD_VB + n_inputs * script.input_vb + n_outputs * script.output_vb
    if op_return_len:
        vsize += _op_return_vb(op_return_len)
    return vsize


def sweep_amount(
    total: int,
    n_inputs: int,
    fee_rate: float,
    memo_len: int = OP_RETURN_MAX_BYTES,
    *,
    script: ScriptParams = P2WPKH,
    extra_fee: int = 0,
) -> tuple[int, int]:
    """Return ``(send_amount, fee)`` for sweeping ``total`` into one output.

    Spends every input into a single (vault/recipient) output plus the
    OP_RETURN memo, with no change. ``memo_len`` defaults to the maximum so the
    fee is never underestimated. ``extra_fee`` is the CPFP surcharge for any
    unconfirmed inputs (a sweep spends all of them, so the caller can price it
    up front with :func:`cpfp_surcharge`); it comes out of the swept amount,
    since there is no change output to take it from.
    """
    fee = (
        math.ceil(estimate_vsize(n_inputs, 1, memo_len, script=script) * fee_rate)
        + extra_fee
    )
    send = total - fee
    if send < script.dust:
        raise InsufficientFunds(f"balance {total} too small to sweep after fee {fee}")
    return send, fee


def token_sweep_amount(balance: int, decimals: int) -> int:
    """THORChain 1e8 amount that sends an entire token ``balance``.

    ``balance`` is in the token's native base units (``decimals`` of them per
    whole token). Unlike a UTXO/native sweep, a token sweep is *exact*: gas is
    paid in the chain's native coin, not the token, so the whole balance goes
    out. Raises :class:`InsufficientFunds` if there is nothing to sweep.
    """
    amount = balance * 10**8 // 10**decimals
    if amount <= 0:
        raise InsufficientFunds(f"token balance {balance} too small to sweep")
    return amount


def _select(
    utxos: list[Utxo],
    send_amount: int,
    dust: int,
    fee_fn: Callable[[list[Utxo], int], int],
) -> Selection:
    """The greedy largest-first selection core, fee-model agnostic.

    ``fee_fn(chosen, n_outputs)`` prices a candidate transaction shape: one
    recipient/vault output (plus any memo the model accounts for internally)
    and an optional change output. It is handed the chosen inputs rather than
    their count because an unconfirmed one costs more than its vbytes — see
    :func:`cpfp_surcharge`. Change below ``dust`` is dropped and folded into
    the fee.

    Confirmed coins are taken first, then largest-first within each group: an
    unconfirmed input is both riskier and (via CPFP) dearer, so it is only
    reached for once the confirmed ones cannot cover the spend.
    """
    chosen: list[Utxo] = []
    total = 0
    for utxo in sorted(utxos, key=lambda x: (x.confirmed, x.value), reverse=True):
        chosen.append(utxo)
        total += utxo.value

        # With a change output.
        fee_with_change = fee_fn(chosen, 2)
        change = total - send_amount - fee_with_change
        if change >= dust:
            return Selection(utxos=chosen, fee=fee_with_change, change=change)

        # Without a change output: any remainder above the minimal fee is fee.
        if total >= send_amount + fee_fn(chosen, 1):
            return Selection(utxos=chosen, fee=total - send_amount, change=0)

    raise InsufficientFunds(
        f"have {total} base units, need {send_amount} + fee for the spend"
    )


def select_coins(
    utxos: list[Utxo],
    send_amount: int,
    fee_rate: float,
    memo_len: int,
    *,
    script: ScriptParams = P2WPKH,
) -> Selection:
    """Greedily select UTXOs (largest first) to fund a swap output.

    The transaction shape is: one vault/recipient output, one OP_RETURN (memo),
    and an optional change output, all of ``script``'s type. If the change
    would fall below the script's dust threshold it is dropped and folded into
    the fee.
    """
    return _select(
        utxos,
        send_amount,
        script.dust,
        lambda chosen, n_out: (
            math.ceil(
                estimate_vsize(len(chosen), n_out, memo_len, script=script) * fee_rate
            )
            + cpfp_surcharge(chosen)
        ),
    )


# --- ZIP-317 (Zcash): the fee scales with "logical actions", not vbytes ------
#
# conventional_fee = 5000 * max(2, logical_actions); for a transparent-only tx
# the logical actions are max(ceil(in_bytes/150), ceil(out_bytes/34)), which
# for P2PKH inputs (~148 B) and standard outputs (34 B) is max(n_in, n_out).
# A tx paying less than the conventional fee is deprioritized/rejected by
# ZIP-317-following nodes, so this is both the floor and what we pay.

ZIP317_MARGINAL_FEE = 5000
ZIP317_GRACE_ACTIONS = 2
# Conservative dust floor for a transparent output (mirrors the legacy P2PKH
# threshold; Zcash's own relay dust is lower, so this only errs safe).
DUST_ZEC = 546


def zip317_fee(n_inputs: int, n_outputs: int, memo_len: int = 0) -> int:
    """The ZIP-317 conventional fee for a transparent-only transaction.

    ``n_outputs`` counts the standard (34-byte) P2PKH outputs; a non-zero
    ``memo_len`` adds an OP_RETURN output, whose serialized size (up to ~92
    bytes for the 80-byte max) contributes ~3 logical actions of its own —
    ignoring it would put the fee below the conventional floor and get the tx
    deprioritized.
    """
    in_actions = math.ceil(n_inputs * 150 / 150)  # P2PKH input ≈ the 150-B unit
    out_bytes = n_outputs * 34
    if memo_len:
        out_bytes += _op_return_vb(memo_len) + 1  # +1: OP_PUSHDATA1 worst case
    out_actions = math.ceil(out_bytes / 34)
    return ZIP317_MARGINAL_FEE * max(ZIP317_GRACE_ACTIONS, in_actions, out_actions)


def select_coins_zip317(
    utxos: list[Utxo], send_amount: int, memo_len: int = 0, *, dust: int = DUST_ZEC
) -> Selection:
    """Greedy selection under the ZIP-317 fee model (Zcash transparent spends)."""
    return _select(
        utxos,
        send_amount,
        dust,
        lambda chosen, n_out: (
            zip317_fee(len(chosen), n_out, memo_len) + cpfp_surcharge(chosen)
        ),
    )


def sweep_amount_zip317(
    total: int, n_inputs: int, memo_len: int = 0, *, dust: int = DUST_ZEC
) -> tuple[int, int]:
    """Return ``(send_amount, fee)`` sweeping ``total`` into one output (ZIP-317)."""
    fee = zip317_fee(n_inputs, 1, memo_len)
    send = total - fee
    if send < dust:
        raise InsufficientFunds(f"balance {total} too small to sweep after fee {fee}")
    return send, fee
