"""Tests for the bitcoinlib-free Bitcoin helpers: OP_RETURN encoding and the
deterministic coin selection used to build swap transactions.
"""

import pytest

from cryptoswap.chains.coins import (
    InsufficientFunds,
    Utxo,
    decode_op_return,
    encode_op_return,
    estimate_vsize,
    select_coins,
)


def u(value: int, vout: int = 0) -> Utxo:
    return Utxo(txid="aa" * 32, vout=vout, value=value, address="bc1qowned")


def test_op_return_roundtrip_short():
    memo = b"=:ETH.ETH:0xabc"
    script = encode_op_return(memo)
    assert script[0] == 0x6A
    assert decode_op_return(script) == memo


def test_op_return_roundtrip_pushdata1():
    memo = b"x" * 80
    script = encode_op_return(memo)
    assert script[1] == 0x4C  # OP_PUSHDATA1 for 76..80 bytes
    assert decode_op_return(script) == memo


def test_op_return_rejects_oversize():
    with pytest.raises(ValueError):
        encode_op_return(b"x" * 81)


def test_decode_rejects_non_op_return():
    with pytest.raises(ValueError):
        decode_op_return(b"\x00\x01\x02")


def test_estimate_vsize_monotonic():
    assert estimate_vsize(2, 2, 50) > estimate_vsize(1, 1, 0)


def test_select_with_change_conserves_value():
    sel = select_coins([u(200000)], send_amount=178100, fee_rate=2, memo_len=50)
    assert len(sel.utxos) == 1
    assert sel.change > 0
    assert sel.fee > 0
    assert sum(x.value for x in sel.utxos) == 178100 + sel.fee + sel.change


def test_select_uses_multiple_utxos():
    sel = select_coins(
        [u(100000, vout=0), u(90000, vout=1)],
        send_amount=178100,
        fee_rate=1,
        memo_len=50,
    )
    assert len(sel.utxos) == 2


def test_select_insufficient_funds():
    with pytest.raises(InsufficientFunds):
        select_coins([u(100000)], send_amount=178100, fee_rate=2, memo_len=50)


def test_sweep_amount_conserves_value():
    from cryptoswap.chains.coins import sweep_amount

    send, fee = sweep_amount(total=200000, n_inputs=1, fee_rate=2, memo_len=50)
    assert send + fee == 200000
    assert send > 0 and fee > 0


def test_sweep_amount_raises_when_balance_below_fee():
    from cryptoswap.chains.coins import sweep_amount

    with pytest.raises(InsufficientFunds):
        sweep_amount(total=100, n_inputs=1, fee_rate=2, memo_len=50)


def test_select_folds_sub_dust_change_into_fee():
    # 100262 - 100000 leaves only 262, below the dust floor: no change output,
    # the remainder becomes extra fee, and value is still conserved.
    sel = select_coins([u(100262)], send_amount=100000, fee_rate=1, memo_len=10)
    assert sel.change == 0
    assert sel.fee == 262
    assert sum(x.value for x in sel.utxos) == 100000 + sel.fee
