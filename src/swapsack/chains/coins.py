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
from collections.abc import Callable

OP_RETURN_MAX_BYTES = 80
OP_PUSHDATA1 = 0x4C
OP_RETURN_OPCODE = 0x6A

TX_OVERHEAD_VB = 11


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

# Backwards-compatible aliases (pre-ScriptParams API).
P2WPKH_INPUT_VB = P2WPKH.input_vb
P2WPKH_OUTPUT_VB = P2WPKH.output_vb
DUST_P2WPKH = P2WPKH.dust


class InsufficientFunds(RuntimeError):
    """Raised when the available UTXOs cannot cover the amount plus fee."""


@dataclasses.dataclass(frozen=True)
class Utxo:
    txid: str
    vout: int
    value: int
    address: str
    path: str | None = None  # HD derivation path, set by the address scanner


@dataclasses.dataclass(frozen=True)
class Selection:
    utxos: list[Utxo]
    fee: int
    change: int


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
) -> tuple[int, int]:
    """Return ``(send_amount, fee)`` for sweeping ``total`` into one output.

    Spends every input into a single (vault/recipient) output plus the
    OP_RETURN memo, with no change. ``memo_len`` defaults to the maximum so the
    fee is never underestimated.
    """
    fee = math.ceil(estimate_vsize(n_inputs, 1, memo_len, script=script) * fee_rate)
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
    fee_fn: Callable[[int, int], int],
) -> Selection:
    """The greedy largest-first selection core, fee-model agnostic.

    ``fee_fn(n_inputs, n_outputs)`` prices a candidate transaction shape: one
    recipient/vault output (plus any memo the model accounts for internally)
    and an optional change output. Change below ``dust`` is dropped and folded
    into the fee.
    """
    chosen: list[Utxo] = []
    total = 0
    for utxo in sorted(utxos, key=lambda x: x.value, reverse=True):
        chosen.append(utxo)
        total += utxo.value

        # With a change output.
        fee_with_change = fee_fn(len(chosen), 2)
        change = total - send_amount - fee_with_change
        if change >= dust:
            return Selection(utxos=chosen, fee=fee_with_change, change=change)

        # Without a change output: any remainder above the minimal fee is fee.
        if total >= send_amount + fee_fn(len(chosen), 1):
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
        lambda n_in, n_out: math.ceil(
            estimate_vsize(n_in, n_out, memo_len, script=script) * fee_rate
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


def zip317_fee(n_inputs: int, n_outputs: int) -> int:
    """The ZIP-317 conventional fee for a transparent-only transaction."""
    return ZIP317_MARGINAL_FEE * max(ZIP317_GRACE_ACTIONS, n_inputs, n_outputs)


def select_coins_zip317(
    utxos: list[Utxo], send_amount: int, *, dust: int = DUST_ZEC
) -> Selection:
    """Greedy selection under the ZIP-317 fee model (Zcash transparent sends)."""
    return _select(utxos, send_amount, dust, zip317_fee)


def sweep_amount_zip317(
    total: int, n_inputs: int, *, dust: int = DUST_ZEC
) -> tuple[int, int]:
    """Return ``(send_amount, fee)`` sweeping ``total`` into one output (ZIP-317)."""
    fee = zip317_fee(n_inputs, 1)
    send = total - fee
    if send < dust:
        raise InsufficientFunds(f"balance {total} too small to sweep after fee {fee}")
    return send, fee
