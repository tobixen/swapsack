"""Tests for the bitcoinlib-free Bitcoin helpers: OP_RETURN encoding and the
deterministic coin selection used to build swap transactions.
"""

import pytest

from swapsack.chains.coins import (
    InsufficientFunds,
    Utxo,
    decode_op_return,
    encode_op_return,
    estimate_vsize,
    select_coins,
    token_sweep_amount,
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


def test_decode_op_return_rejects_bare_opcode():
    # A length-1 nulldata script (bare OP_RETURN, no push) must reject cleanly,
    # not raise IndexError when indexing the (absent) push-length byte.
    with pytest.raises(ValueError):
        decode_op_return(b"\x6a")


def test_decode_op_return_rejects_truncated_pushdata1():
    # OP_RETURN OP_PUSHDATA1 with the length byte missing.
    with pytest.raises(ValueError):
        decode_op_return(b"\x6a\x4c")


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
    from swapsack.chains.coins import sweep_amount

    send, fee = sweep_amount(total=200000, n_inputs=1, fee_rate=2, memo_len=50)
    assert send + fee == 200000
    assert send > 0 and fee > 0


def test_sweep_amount_raises_when_balance_below_fee():
    from swapsack.chains.coins import sweep_amount

    with pytest.raises(InsufficientFunds):
        sweep_amount(total=100, n_inputs=1, fee_rate=2, memo_len=50)


def test_select_folds_sub_dust_change_into_fee():
    # 100262 - 100000 leaves only 262, below the dust floor: no change output,
    # the remainder becomes extra fee, and value is still conserved.
    sel = select_coins([u(100262)], send_amount=100000, fee_rate=1, memo_len=10)
    assert sel.change == 0
    assert sel.fee == 262
    assert sum(x.value for x in sel.utxos) == 100000 + sel.fee


# --- token sweep (1e8 amount for the whole token balance) ---


def test_token_sweep_amount_converts_native_to_1e8():
    # 2.5 USDT (6 decimals) -> 2.5 in THORChain 1e8 units. The token sweep is
    # exact: gas is paid in the chain's native coin, not the token.
    assert token_sweep_amount(2_500_000, 6) == 250_000_000
    # 8-decimal token round-trips 1:1 with the 1e8 unit.
    assert token_sweep_amount(123, 8) == 123


def test_token_sweep_amount_rejects_empty_balance():
    with pytest.raises(InsufficientFunds):
        token_sweep_amount(0, 6)


# --- legacy (P2PKH) script params — the DASH/ZEC spend path ------------------


def test_p2pkh_vsize_uses_legacy_input_output_sizes():
    from swapsack.chains.coins import P2PKH, P2WPKH

    # 1-in 2-out, no OP_RETURN: 11 + 148 + 2*34 = 227 legacy vs 11+68+62=141 segwit
    assert estimate_vsize(1, 2, 0, script=P2PKH) == 227
    assert estimate_vsize(1, 2, 0, script=P2WPKH) == estimate_vsize(1, 2, 0)
    assert estimate_vsize(1, 2, 0, script=P2PKH) > estimate_vsize(1, 2, 0)


def test_select_p2pkh_uses_legacy_dust_threshold():
    from swapsack.chains.coins import P2PKH, P2WPKH

    # Change of ~400: above segwit dust (294) but below legacy dust (546) —
    # P2WPKH keeps it as change, P2PKH must fold it into the fee.
    # legacy fee with change @1: 11+148+2*34 = 227; total-send-227 = 400 change
    sel_legacy = select_coins([u(100_627)], 100_000, 1.0, 0, script=P2PKH)
    assert sel_legacy.change == 0
    assert sel_legacy.fee == 627  # everything above the send amount
    sel_segwit = select_coins([u(100_541)], 100_000, 1.0, 0, script=P2WPKH)
    assert sel_segwit.change == 400  # 100_541 - 100_000 - 141
    assert sel_segwit.fee == 141


def test_sweep_amount_p2pkh_conserves_value():
    from swapsack.chains.coins import P2PKH, sweep_amount

    total = 1_000_000
    send, fee = sweep_amount(total, 2, 1.0, memo_len=0, script=P2PKH)
    assert send + fee == total
    # 2-in 1-out legacy: 11 + 2*148 + 34 = 341
    assert fee == 341


def test_sweep_amount_p2pkh_respects_legacy_dust():
    from swapsack.chains.coins import P2PKH, sweep_amount

    # send = total - fee(341) = 545 < dust 546 -> refuse
    with pytest.raises(InsufficientFunds):
        sweep_amount(886, 2, 1.0, memo_len=0, script=P2PKH)
