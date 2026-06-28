"""Tests for the pre-broadcast swap verify gate.

These guard against the irreversible-loss failure modes of a THORChain BTC
swap: wrong vault, wrong amount, wrong/garbled memo, change leaking to a
non-owned address, an expired quote, or an absurd fee.
"""

from cryptoswap.verify import (
    EthSwapPlan,
    SwapPlan,
    TxOutput,
    verify_btc_swap,
    verify_eth_swap,
)

VAULT = "bc1qct4mxayrdy96d4py20l4u02mu06r667f42p9fp"
CHANGE = "bc1qchange00000000000000000000000000000000"
MEMO = "=:ETH.ETH:0x1111111111111111111111111111111111111111:6700000"

PLAN = SwapPlan(inbound_address=VAULT, amount=178100, memo=MEMO, expiry=2000)
OWNED = {CHANGE}


def good_outputs():
    return [
        TxOutput(address=VAULT, value=178100),
        TxOutput(address=CHANGE, value=50000),
        TxOutput(address=None, value=0, op_return_data=MEMO.encode()),
    ]


def verify(outputs, fee=600, now=1000, max_fee=10000):
    return verify_btc_swap(
        outputs, fee=fee, plan=PLAN, owned_addresses=OWNED, now=now, max_fee=max_fee
    )


def test_valid_swap_has_no_problems():
    assert verify(good_outputs()) == []


def test_wrong_vault_amount():
    outs = good_outputs()
    outs[0] = TxOutput(address=VAULT, value=178099)
    assert any("amount" in p for p in verify(outs))


def test_wrong_vault_address():
    outs = good_outputs()
    outs[0] = TxOutput(address="bc1qwrongvault0000000000000000000", value=178100)
    assert any("vault" in p.lower() for p in verify(outs))


def test_memo_mismatch():
    outs = good_outputs()
    outs[2] = TxOutput(address=None, value=0, op_return_data=b"=:ETH.ETH:0xDEADBEEF")
    assert any("memo" in p.lower() for p in verify(outs))


def test_missing_op_return():
    assert any("op_return" in p.lower() for p in verify(good_outputs()[:2]))


def test_change_to_unowned_address():
    outs = good_outputs()
    outs[1] = TxOutput(address="bc1qattacker00000000000000000000000000000", value=50000)
    assert any("owned" in p.lower() for p in verify(outs))


def test_expired_quote():
    assert any("expir" in p.lower() for p in verify(good_outputs(), now=99999))


def test_excessive_fee():
    assert any("fee" in p.lower() for p in verify(good_outputs(), fee=999999))


def test_memo_too_long_for_op_return():
    long_memo = "=:ETH.ETH:" + "x" * 90
    plan = SwapPlan(inbound_address=VAULT, amount=178100, memo=long_memo, expiry=2000)
    outs = [
        TxOutput(address=VAULT, value=178100),
        TxOutput(address=CHANGE, value=50000),
        TxOutput(address=None, value=0, op_return_data=long_memo.encode()),
    ]
    problems = verify_btc_swap(
        outs, fee=600, plan=plan, owned_addresses=OWNED, now=1000, max_fee=10000
    )
    assert any("80" in p for p in problems)


# --- ETH swap verify gate ---

ETH_VAULT = "0x85034887f6656d610c38ef1710208495791fb146"
ETH_MEMO = "=:BTC.BTC:bc1qexampledest:123"
ETH_PLAN = EthSwapPlan(
    inbound_address=ETH_VAULT, amount_wei=10**16, memo=ETH_MEMO, expiry=2000
)


def eth_verify(**override):
    args = dict(
        to=ETH_VAULT,
        value=10**16,
        data="0x" + ETH_MEMO.encode().hex(),
        chain_id=1,
        gas=60000,
        max_fee_per_gas=20_000_000_000,
        plan=ETH_PLAN,
        now=1000,
        max_fee_wei=10**16,
    )
    args.update(override)
    return verify_eth_swap(**args)


def test_eth_valid_has_no_problems():
    assert eth_verify() == []


def test_eth_wrong_vault():
    assert any("vault" in p.lower() for p in eth_verify(to="0xdeadbeef"))


def test_eth_wrong_value():
    assert any("value" in p.lower() for p in eth_verify(value=999))


def test_eth_wrong_memo():
    assert any(
        "memo" in p.lower() or "calldata" in p.lower()
        for p in eth_verify(data="0xdeadbeef")
    )


def test_eth_wrong_chain_id():
    assert any("chain" in p.lower() for p in eth_verify(chain_id=137))


def test_eth_expired():
    assert any("expir" in p.lower() for p in eth_verify(now=99999))


def test_eth_fee_too_high():
    assert any("fee" in p.lower() for p in eth_verify(max_fee_per_gas=10**15))


# --- M1: the memo must pay our own destination ---


def test_btc_rejects_memo_not_paying_destination():
    memo = "=:e:0x1111111111111111111111111111111111111111:6700000"
    plan = SwapPlan(
        inbound_address=VAULT,
        amount=178100,
        memo=memo,
        expiry=2000,
        destination="0x2222222222222222222222222222222222222222",
    )
    outs = [
        TxOutput(address=VAULT, value=178100),
        TxOutput(address=CHANGE, value=50000),
        TxOutput(address=None, value=0, op_return_data=memo.encode()),
    ]
    problems = verify_btc_swap(
        outs, fee=600, plan=plan, owned_addresses=OWNED, now=1000, max_fee=10000
    )
    assert any("destination" in p.lower() for p in problems)


def test_btc_accepts_memo_paying_destination():
    dest = "0x1111111111111111111111111111111111111111"
    memo = f"=:e:{dest}:6700000"
    plan = SwapPlan(
        inbound_address=VAULT, amount=178100, memo=memo, expiry=2000, destination=dest
    )
    outs = [
        TxOutput(address=VAULT, value=178100),
        TxOutput(address=CHANGE, value=50000),
        TxOutput(address=None, value=0, op_return_data=memo.encode()),
    ]
    problems = verify_btc_swap(
        outs, fee=600, plan=plan, owned_addresses=OWNED, now=1000, max_fee=10000
    )
    assert problems == []


def test_eth_rejects_memo_not_paying_destination():
    plan = EthSwapPlan(
        inbound_address=ETH_VAULT,
        amount_wei=10**16,
        memo=ETH_MEMO,
        expiry=2000,
        destination="bc1qsomeoneelse",
    )
    assert any("destination" in p.lower() for p in eth_verify(plan=plan))


def test_eth_accepts_memo_paying_destination():
    plan = EthSwapPlan(
        inbound_address=ETH_VAULT,
        amount_wei=10**16,
        memo=ETH_MEMO,
        expiry=2000,
        destination="bc1qexampledest",
    )
    assert eth_verify(plan=plan) == []
