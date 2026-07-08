"""Bitcoin coin selection and OP_RETURN encoding — pure, dependency-free logic.

Kept separate from the bitcoinlib-backed adapter so it can be tested without the
``btc`` extra, and so the money-sensitive selection/fee maths is easy to read.
All amounts are in satoshis.
"""

from __future__ import annotations

import dataclasses
import math

OP_RETURN_MAX_BYTES = 80
OP_PUSHDATA1 = 0x4C
OP_RETURN_OPCODE = 0x6A

# Approximate virtual sizes (vbytes) for a native-segwit (P2WPKH) spend.
TX_OVERHEAD_VB = 11
P2WPKH_INPUT_VB = 68
P2WPKH_OUTPUT_VB = 31
# A P2WPKH output's value is not worth keeping below this (sats).
DUST_P2WPKH = 294


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


def estimate_vsize(n_inputs: int, n_p2wpkh_outputs: int, op_return_len: int) -> int:
    """Estimate transaction vsize for P2WPKH inputs/outputs plus one OP_RETURN."""
    vsize = (
        TX_OVERHEAD_VB
        + n_inputs * P2WPKH_INPUT_VB
        + n_p2wpkh_outputs * P2WPKH_OUTPUT_VB
    )
    if op_return_len:
        vsize += _op_return_vb(op_return_len)
    return vsize


def sweep_amount(
    total: int,
    n_inputs: int,
    fee_rate: float,
    memo_len: int = OP_RETURN_MAX_BYTES,
    *,
    dust: int = DUST_P2WPKH,
) -> tuple[int, int]:
    """Return ``(send_amount, fee)`` for sweeping ``total`` into one output.

    Spends every input into a single P2WPKH (vault) output plus the OP_RETURN
    memo, with no change. ``memo_len`` defaults to the maximum so the fee is
    never underestimated.
    """
    fee = math.ceil(estimate_vsize(n_inputs, 1, memo_len) * fee_rate)
    send = total - fee
    if send < dust:
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


def select_coins(
    utxos: list[Utxo],
    send_amount: int,
    fee_rate: float,
    memo_len: int,
    *,
    dust: int = DUST_P2WPKH,
) -> Selection:
    """Greedily select UTXOs (largest first) to fund a swap output.

    The transaction shape is: one P2WPKH vault output, one OP_RETURN (memo), and
    an optional P2WPKH change output. If the change would fall below ``dust`` it
    is dropped and folded into the fee.
    """
    chosen: list[Utxo] = []
    total = 0
    for utxo in sorted(utxos, key=lambda x: x.value, reverse=True):
        chosen.append(utxo)
        total += utxo.value

        # With a change output.
        fee_with_change = math.ceil(estimate_vsize(len(chosen), 2, memo_len) * fee_rate)
        change = total - send_amount - fee_with_change
        if change >= dust:
            return Selection(utxos=chosen, fee=fee_with_change, change=change)

        # Without a change output: any remainder above the minimal fee is fee.
        fee_no_change = math.ceil(estimate_vsize(len(chosen), 1, memo_len) * fee_rate)
        if total >= send_amount + fee_no_change:
            return Selection(utxos=chosen, fee=total - send_amount, change=0)

    raise InsufficientFunds(f"have {total} sats, need {send_amount} + fee for the swap")
