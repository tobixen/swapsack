"""Tests for the pre-broadcast swap verify gate.

These guard against the irreversible-loss failure modes of a THORChain BTC
swap: wrong vault, wrong amount, wrong/garbled memo, change leaking to a
non-owned address, an expired quote, or an absurd fee.
"""

from swapsack.verify import (
    EthSendPlan,
    EthSwapPlan,
    EthTokenSendPlan,
    SendPlan,
    SwapPlan,
    TronSendPlan,
    TronSwapPlan,
    TronTokenSendPlan,
    TronTokenSwapPlan,
    TxOutput,
    verify_btc_send,
    verify_btc_swap,
    verify_eth_send,
    verify_eth_swap,
    verify_eth_token_send,
    verify_tron_send,
    verify_tron_swap,
    verify_tron_token_send,
    verify_tron_token_swap,
)

VAULT = "bc1qct4mxayrdy96d4py20l4u02mu06r667f42p9fp"
CHANGE = "bc1qchange00000000000000000000000000000000"
RECIPIENT = "bc1qrecipient000000000000000000000000000000"
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


# --- streaming swaps: the memo gains a `LIM/INTERVAL/QUANTITY` suffix ---
#
# A streaming swap only rewrites the memo's limit field (e.g. `…:6700000` ->
# `…:6700000/1/0`). The gate binds the tx memo to the quote's memo verbatim and
# additionally checks our destination is present, so the suffix must neither
# break a legitimate streaming swap nor weaken the destination-binding check.

STREAM_DEST = "0x1111111111111111111111111111111111111111"
STREAM_MEMO = f"=:ETH.ETH:{STREAM_DEST}:6700000/1/0"


def _stream_outputs(memo):
    return [
        TxOutput(address=VAULT, value=178100),
        TxOutput(address=CHANGE, value=50000),
        TxOutput(address=None, value=0, op_return_data=memo.encode()),
    ]


def test_streaming_memo_is_accepted_and_still_binds_destination():
    plan = SwapPlan(
        inbound_address=VAULT,
        amount=178100,
        memo=STREAM_MEMO,
        expiry=2000,
        destination=STREAM_DEST,
    )
    problems = verify_btc_swap(
        _stream_outputs(STREAM_MEMO),
        fee=600,
        plan=plan,
        owned_addresses=OWNED,
        now=1000,
        max_fee=10000,
    )
    assert problems == []


def test_streaming_memo_that_pays_someone_else_is_rejected():
    # Same streaming shape, but the memo pays a *different* address than our
    # destination -> the destination-binding check must still fire.
    other = "0x2222222222222222222222222222222222222222"
    memo = f"=:ETH.ETH:{other}:6700000/1/0"
    plan = SwapPlan(
        inbound_address=VAULT,
        amount=178100,
        memo=memo,
        expiry=2000,
        destination=STREAM_DEST,
    )
    problems = verify_btc_swap(
        _stream_outputs(memo),
        fee=600,
        plan=plan,
        owned_addresses=OWNED,
        now=1000,
        max_fee=10000,
    )
    assert any("does not pay destination" in p for p in problems)


def test_streaming_memo_tampered_output_is_rejected():
    # The tx must carry exactly the quoted (streaming) memo: a swapped-in memo
    # missing the streaming suffix is a mismatch and blocks.
    plan = SwapPlan(
        inbound_address=VAULT,
        amount=178100,
        memo=STREAM_MEMO,
        expiry=2000,
        destination=STREAM_DEST,
    )
    tampered = f"=:ETH.ETH:{STREAM_DEST}:6700000"  # streaming suffix stripped
    problems = verify_btc_swap(
        _stream_outputs(tampered),
        fee=600,
        plan=plan,
        owned_addresses=OWNED,
        now=1000,
        max_fee=10000,
    )
    assert any("memo" in p.lower() for p in problems)


# --- BTC plain-send verify gate (no vault, no memo) ---

SEND_PLAN = SendPlan(recipient=RECIPIENT, amount=100_000)


def send_outputs():
    return [
        TxOutput(address=RECIPIENT, value=100_000),
        TxOutput(address=CHANGE, value=50_000),
    ]


def verify_send(outputs, fee=600, max_fee=10000):
    return verify_btc_send(
        outputs, fee=fee, plan=SEND_PLAN, owned_addresses=OWNED, max_fee=max_fee
    )


def test_valid_send_has_no_problems():
    assert verify_send(send_outputs()) == []


def test_send_without_change_is_fine():
    assert verify_send([TxOutput(address=RECIPIENT, value=100_000)]) == []


def test_send_wrong_recipient_amount():
    outs = send_outputs()
    outs[0] = TxOutput(address=RECIPIENT, value=99_999)
    assert any("amount" in p for p in verify_send(outs))


def test_send_missing_recipient_output():
    assert any(
        "recipient" in p.lower()
        for p in verify_send([TxOutput(address=CHANGE, value=150_000)])
    )


def test_send_change_to_unowned_address_blocks():
    outs = send_outputs()
    outs[1] = TxOutput(address="bc1qattacker00000000000000000000000000", value=50_000)
    assert any("non-owned" in p for p in verify_send(outs))


def test_send_rejects_op_return():
    outs = send_outputs()
    outs.append(TxOutput(address=None, value=0, op_return_data=b"=:ETH.ETH:sneaky"))
    assert any("op_return" in p.lower() for p in verify_send(outs))


def test_send_fee_over_max_blocks():
    assert any(
        "max_fee" in p for p in verify_send(send_outputs(), fee=20_000, max_fee=10_000)
    )


def test_send_negative_fee_blocks():
    assert any("negative" in p for p in verify_send(send_outputs(), fee=-1))


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


def test_eth_rejects_case_corrupted_non_evm_destination():
    # ETH -> TRON: a base58 destination is case-SENSITIVE. A memo carrying the
    # destination with mangled case pays a DIFFERENT address and must be
    # rejected. The old `lower()`-both-sides check wrongly accepted it; the gate
    # now defers to memo_pays_destination (which only case-folds 0x addresses).
    dest = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
    memo = f"=:TRON.TRX:{dest.lower()}:0"
    plan = EthSwapPlan(
        inbound_address=ETH_VAULT,
        amount_wei=10**16,
        memo=memo,
        expiry=2000,
        destination=dest,
    )
    problems = eth_verify(plan=plan, data="0x" + memo.encode().hex())
    assert any("destination" in p.lower() for p in problems)


def test_eth_accepts_recased_evm_destination():
    # EVM destinations stay case-insensitive (THORChain may re-case them).
    dest = "0x1111111111111111111111111111111111111111"
    memo = f"=:e:{dest.upper()}:0"
    plan = EthSwapPlan(
        inbound_address=ETH_VAULT,
        amount_wei=10**16,
        memo=memo,
        expiry=2000,
        destination=dest,
    )
    assert eth_verify(plan=plan, data="0x" + memo.encode().hex()) == []


# --- TRON deposit verify gate (swap source + liquidity) ---

TRON_VAULT = "TNVaVKErJ3pdC2nVjC4d4n6Te8H1Lz9Yth"
TRON_DEST = "0x1111111111111111111111111111111111111111"
TRON_MEMO = f"=:ETH.ETH:{TRON_DEST}:6700000"
TRON_PLAN = TronSwapPlan(
    inbound_address=TRON_VAULT,
    amount_sun=1_500_000,
    memo=TRON_MEMO,
    expiry=2000,
    destination=TRON_DEST,
)


def tron_verify(
    contract_type="TransferContract",
    to=TRON_VAULT,
    amount_sun=1_500_000,
    memo=TRON_MEMO,
    now=1000,
    plan=TRON_PLAN,
):
    return verify_tron_swap(
        contract_type=contract_type,
        to_address=to,
        amount_sun=amount_sun,
        memo=memo,
        plan=plan,
        now=now,
    )


def test_tron_valid_has_no_problems():
    assert tron_verify() == []


def test_tron_wrong_contract_type():
    assert any(
        "contract" in p.lower()
        for p in tron_verify(contract_type="TriggerSmartContract")
    )


def test_tron_wrong_vault():
    assert any("vault" in p.lower() for p in tron_verify(to="TWrongVaultAddr"))


def test_tron_wrong_amount():
    assert any("amount" in p for p in tron_verify(amount_sun=1_500_001))


def test_tron_wrong_memo():
    assert any("memo" in p.lower() for p in tron_verify(memo="=:ETH.ETH:0xdead"))


def test_tron_expired():
    assert any("expired" in p for p in tron_verify(now=3000))


def test_tron_rejects_memo_not_paying_destination():
    plan = TronSwapPlan(
        inbound_address=TRON_VAULT,
        amount_sun=1_500_000,
        memo=TRON_MEMO,
        expiry=2000,
        destination="0x2222222222222222222222222222222222222222",
    )
    assert any("destination" in p.lower() for p in tron_verify(plan=plan))


def test_tron_lp_deposit_no_destination_check():
    # An LP deposit has no destination; the memo is +:POOL.
    plan = TronSwapPlan(
        inbound_address=TRON_VAULT, amount_sun=1_500_000, memo="+:TRON.TRX", expiry=2000
    )
    assert tron_verify(memo="+:TRON.TRX", plan=plan) == []


# --- TRON TRC-20 token deposit verify gate (USDT-TRON source) ---

TRON_TOKEN = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # USDT-TRON contract (base58)
TRON_TOKEN_MEMO = f"=:b:{TRON_DEST}:6700000"
TRON_TOKEN_PLAN = TronTokenSwapPlan(
    inbound_address=TRON_VAULT,
    token=TRON_TOKEN,
    amount=20_000_000,  # 20 USDT (6 decimals)
    memo=TRON_TOKEN_MEMO,
    expiry=2000,
    destination=TRON_DEST,
)


def tron_token_verify(
    contract_type="TriggerSmartContract",
    trigger_to=TRON_TOKEN,
    recipient=TRON_VAULT,
    transfer_amount=20_000_000,
    trx_value=0,
    memo=TRON_TOKEN_MEMO,
    now=1000,
    plan=TRON_TOKEN_PLAN,
):
    return verify_tron_token_swap(
        contract_type=contract_type,
        trigger_to=trigger_to,
        recipient=recipient,
        transfer_amount=transfer_amount,
        trx_value=trx_value,
        memo=memo,
        plan=plan,
        now=now,
    )


def test_tron_token_valid_has_no_problems():
    assert tron_token_verify() == []


def test_tron_token_wrong_contract_type():
    assert any(
        "contract" in p.lower()
        for p in tron_token_verify(contract_type="TransferContract")
    )


def test_tron_token_trigger_not_targeting_token():
    # The TriggerSmartContract must call the token contract, not something else.
    assert any("token" in p.lower() for p in tron_token_verify(trigger_to=TRON_VAULT))


def test_tron_token_wrong_recipient():
    # The transfer must pay the vault; a swapped recipient is irreversible loss.
    assert any(
        "vault" in p.lower() for p in tron_token_verify(recipient="TWrongVaultAddr")
    )


def test_tron_token_wrong_amount():
    assert any("amount" in p for p in tron_token_verify(transfer_amount=20_000_001))


def test_tron_token_rejects_trx_value():
    # A token transfer must not also move native TRX.
    assert any("TRX" in p for p in tron_token_verify(trx_value=1))


def test_tron_token_wrong_memo():
    assert any("memo" in p.lower() for p in tron_token_verify(memo="=:ETH.ETH:0xdead"))


def test_tron_token_expired():
    assert any("expired" in p for p in tron_token_verify(now=3000))


def test_tron_token_rejects_memo_not_paying_destination():
    plan = TronTokenSwapPlan(
        inbound_address=TRON_VAULT,
        token=TRON_TOKEN,
        amount=20_000_000,
        memo=TRON_TOKEN_MEMO,
        expiry=2000,
        destination="0x2222222222222222222222222222222222222222",
    )
    assert any("destination" in p.lower() for p in tron_token_verify(plan=plan))


# --- send gates (plain external transfer: no swap, no memo, no router) --------

ETH_RECIPIENT = "0x1111111111111111111111111111111111111111"
ETH_SEND_PLAN = EthSendPlan(recipient=ETH_RECIPIENT, amount_wei=10**16, chain_id=1)


def eth_send_verify(**over):
    kw = dict(
        to=ETH_RECIPIENT,
        value=10**16,
        data="0x",
        chain_id=1,
        gas=21000,
        max_fee_per_gas=20_000_000_000,
        plan=ETH_SEND_PLAN,
        max_fee_wei=10**16,
    )
    kw.update(over)
    return verify_eth_send(**kw)


def test_eth_send_clean():
    assert eth_send_verify() == []


def test_eth_send_wrong_recipient():
    assert any("recipient" in p for p in eth_send_verify(to="0x" + "2" * 40))


def test_eth_send_wrong_amount():
    assert any("value" in p for p in eth_send_verify(value=10**15))


def test_eth_send_rejects_calldata():
    # A plain send must carry no calldata — a memo/extra data signals a misbuild.
    assert any("calldata" in p for p in eth_send_verify(data="0x" + "de" * 8))


def test_eth_send_wrong_chain_id():
    assert any("chainId" in p for p in eth_send_verify(chain_id=56))


def test_eth_send_fee_ceiling():
    assert any("fee" in p for p in eth_send_verify(max_fee_wei=1))


ETH_TOKEN = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
ETH_TOKEN_SEND_PLAN = EthTokenSendPlan(
    token=ETH_TOKEN, recipient=ETH_RECIPIENT, amount=2_500_000, chain_id=1
)


def eth_token_send_verify(**over):
    kw = dict(
        to=ETH_TOKEN,
        value=0,
        chain_id=1,
        recipient=ETH_RECIPIENT,
        transfer_amount=2_500_000,
        gas=65000,
        max_fee_per_gas=20_000_000_000,
        plan=ETH_TOKEN_SEND_PLAN,
        max_fee_wei=10**16,
    )
    kw.update(over)
    return verify_eth_token_send(**kw)


def test_eth_token_send_clean():
    assert eth_token_send_verify() == []


def test_eth_token_send_wrong_token_target():
    assert any("token" in p for p in eth_token_send_verify(to="0x" + "3" * 40))


def test_eth_token_send_rejects_eth_value():
    assert any("value" in p for p in eth_token_send_verify(value=1))


def test_eth_token_send_wrong_recipient():
    assert any(
        "recipient" in p for p in eth_token_send_verify(recipient="0x" + "2" * 40)
    )


def test_eth_token_send_wrong_amount():
    assert any("amount" in p for p in eth_token_send_verify(transfer_amount=1))


TRON_RECIPIENT = "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"
TRON_SEND_PLAN = TronSendPlan(recipient=TRON_RECIPIENT, amount_sun=1_000_000)


def tron_send_verify(**over):
    kw = dict(
        contract_type="TransferContract",
        to_address=TRON_RECIPIENT,
        amount_sun=1_000_000,
        memo="",
        plan=TRON_SEND_PLAN,
    )
    kw.update(over)
    return verify_tron_send(**kw)


def test_tron_send_clean():
    assert tron_send_verify() == []


def test_tron_send_wrong_recipient():
    assert any("recipient" in p for p in tron_send_verify(to_address="Twrong"))


def test_tron_send_wrong_amount():
    assert any("amount" in p for p in tron_send_verify(amount_sun=2_000_000))


def test_tron_send_rejects_memo():
    assert any("memo" in p for p in tron_send_verify(memo="=:hi"))


def test_tron_send_wrong_contract_type():
    assert any(
        "contract type" in p
        for p in tron_send_verify(contract_type="TriggerSmartContract")
    )


TRON_SEND_TOKEN = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
TRON_TOKEN_SEND_PLAN = TronTokenSendPlan(
    token=TRON_SEND_TOKEN, recipient=TRON_RECIPIENT, amount=3_000_000
)


def tron_token_send_verify(**over):
    kw = dict(
        contract_type="TriggerSmartContract",
        trigger_to=TRON_SEND_TOKEN,
        recipient=TRON_RECIPIENT,
        transfer_amount=3_000_000,
        trx_value=0,
        memo="",
        plan=TRON_TOKEN_SEND_PLAN,
    )
    kw.update(over)
    return verify_tron_token_send(**kw)


def test_tron_token_send_clean():
    assert tron_token_send_verify() == []


def test_tron_token_send_wrong_token():
    assert any("token" in p for p in tron_token_send_verify(trigger_to="Tother"))


def test_tron_token_send_wrong_recipient():
    assert any("recipient" in p for p in tron_token_send_verify(recipient="Tnope"))


def test_tron_token_send_rejects_trx_value():
    assert any("TRX value" in p for p in tron_token_send_verify(trx_value=1))


def test_tron_token_send_rejects_memo():
    assert any("memo" in p for p in tron_token_send_verify(memo="=:hi"))
