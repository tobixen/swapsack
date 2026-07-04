"""The pre-broadcast safety gate for Bitcoin -> * swaps.

Given the outputs of an *unsigned* transaction and the swap we intend it to
perform, return a list of human-readable problems. An empty list means the
transaction matches the intended swap exactly and is safe to sign and
broadcast; a non-empty list MUST block broadcasting. On THORChain a wrong
vault, amount or memo means irreversible loss of funds, so this gate is
deliberately strict and dependency-free (easy to read and test).
"""

from __future__ import annotations

import dataclasses

OP_RETURN_MAX_BYTES = 80
# wei (1e18) per THORChain base unit (1e8)
WEI_PER_THORCHAIN_UNIT = 10**10


def memo_pays_destination(destination: str, memo: str) -> bool:
    """Whether the swap memo actually pays ``destination``.

    Exact (case-sensitive) match — correct for bech32 (BTC) and base58 (TRON),
    where case is significant. Only EVM hex addresses (``0x…``) get a
    case-insensitive fallback, since THORChain may re-case them.
    """
    if not destination:
        return True
    if destination in memo:
        return True
    if destination.lower().startswith("0x"):
        return destination.lower() in memo.lower()
    return False


@dataclasses.dataclass(frozen=True)
class TxOutput:
    """One output of a Bitcoin transaction.

    ``address`` is ``None`` for an OP_RETURN (data) output, in which case
    ``op_return_data`` holds the raw bytes.
    """

    address: str | None
    value: int
    op_return_data: bytes | None = None


@dataclasses.dataclass(frozen=True)
class SwapPlan:
    """What we intend the transaction to do, derived from a THORChain quote."""

    inbound_address: str
    amount: int
    memo: str
    expiry: int
    destination: str = ""  # our payout address; must appear in the memo when set


def verify_btc_swap(
    outputs: list[TxOutput],
    fee: int,
    plan: SwapPlan,
    owned_addresses: set[str],
    now: int,
    *,
    max_fee: int,
) -> list[str]:
    """Return reasons the tx does not match ``plan``; empty means safe.

    ``now`` and ``plan.expiry`` are unix timestamps. ``fee`` and ``max_fee`` are
    in satoshis.
    """
    problems: list[str] = []

    if now >= plan.expiry:
        problems.append(f"quote expired (now {now} >= expiry {plan.expiry})")

    # Exactly one output to the vault, for the exact amount.
    vault_outs = [o for o in outputs if o.address == plan.inbound_address]
    if len(vault_outs) != 1:
        problems.append(
            f"expected exactly one output to vault {plan.inbound_address}, "
            f"found {len(vault_outs)}"
        )
    elif vault_outs[0].value != plan.amount:
        problems.append(
            f"vault output amount {vault_outs[0].value} != intended {plan.amount}"
        )

    # Exactly one OP_RETURN, decoding to exactly the quoted memo.
    op_returns = [o for o in outputs if o.op_return_data is not None]
    if len(op_returns) != 1:
        problems.append(
            f"expected exactly one OP_RETURN output, found {len(op_returns)}"
        )
    else:
        data = op_returns[0].op_return_data
        assert data is not None  # narrowed by the op_return_data filter above
        if len(data) > OP_RETURN_MAX_BYTES:
            problems.append(
                f"memo is {len(data)} bytes, exceeds OP_RETURN limit of "
                f"{OP_RETURN_MAX_BYTES}"
            )
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append("OP_RETURN memo is not valid UTF-8")
        else:
            if decoded != plan.memo:
                problems.append(
                    f"OP_RETURN memo {decoded!r} != quoted memo {plan.memo!r}"
                )

    # Every non-vault, non-OP_RETURN output (i.e. change) must return to us.
    for o in outputs:
        if o.op_return_data is not None or o.address == plan.inbound_address:
            continue
        if o.address not in owned_addresses:
            problems.append(f"change output to non-owned address {o.address}")

    # The quoted memo must actually pay our own destination.
    if not memo_pays_destination(plan.destination, plan.memo):
        problems.append(
            f"quoted memo {plan.memo!r} does not pay destination {plan.destination}"
        )

    if fee < 0:
        problems.append(f"negative fee {fee}")
    elif fee > max_fee:
        problems.append(f"fee {fee} exceeds max_fee {max_fee}")

    return problems


@dataclasses.dataclass(frozen=True)
class SendPlan:
    """What we intend a plain BTC send transaction to do.

    Unlike a swap there is no THORChain vault, no memo and no quote/expiry: just
    pay ``amount`` sats to ``recipient`` and return any change to ourselves.
    """

    recipient: str
    amount: int  # sats to the recipient


def verify_btc_send(
    outputs: list[TxOutput],
    fee: int,
    plan: SendPlan,
    owned_addresses: set[str],
    *,
    max_fee: int,
) -> list[str]:
    """Return reasons a plain send tx does not match ``plan``; empty means safe.

    A send pays exactly ``plan.amount`` sats to ``plan.recipient`` and returns
    any change to an owned address. It carries no swap memo, so an OP_RETURN
    output here is unexpected and blocks (it would burn value and signals a
    misconstructed tx). ``fee`` and ``max_fee`` are in satoshis.
    """
    problems: list[str] = []

    # Exactly one output to the recipient, for the exact amount.
    recipient_outs = [o for o in outputs if o.address == plan.recipient]
    if len(recipient_outs) != 1:
        problems.append(
            f"expected exactly one output to recipient {plan.recipient}, "
            f"found {len(recipient_outs)}"
        )
    elif recipient_outs[0].value != plan.amount:
        problems.append(
            f"recipient output amount {recipient_outs[0].value} != "
            f"intended {plan.amount}"
        )

    # A plain send must not carry data outputs.
    if any(o.op_return_data is not None for o in outputs):
        problems.append("plain send must not carry an OP_RETURN output")

    # Every non-recipient, non-data output (i.e. change) must return to us.
    for o in outputs:
        if o.op_return_data is not None or o.address == plan.recipient:
            continue
        if o.address not in owned_addresses:
            problems.append(f"change output to non-owned address {o.address}")

    if fee < 0:
        problems.append(f"negative fee {fee}")
    elif fee > max_fee:
        problems.append(f"fee {fee} exceeds max_fee {max_fee}")

    return problems


@dataclasses.dataclass(frozen=True)
class TronSwapPlan:
    """What we intend a TRON deposit transaction to do (swap or LP).

    ``amount_sun`` is the native TRX amount (1 TRX = 1e6 sun) sent to the vault;
    ``memo`` is carried in the transaction's ``data`` field. A swap sets
    ``destination`` (which must appear in the memo); an LP deposit leaves it
    blank.
    """

    inbound_address: str  # base58 vault address
    amount_sun: int
    memo: str
    expiry: int
    destination: str = ""


def verify_tron_swap(
    *,
    contract_type: str,
    to_address: str,
    amount_sun: int,
    memo: str,
    plan: TronSwapPlan,
    now: int,
) -> list[str]:
    """Return reasons a TRON deposit tx does not match ``plan``; empty means safe.

    A TRON swap/LP deposit is a single ``TransferContract`` paying ``amount_sun``
    to the vault with ``memo`` in the tx data. A wrong vault, amount or memo
    means irreversible loss. ``memo`` is the already-decoded UTF-8 string. There
    is no fee output to check (TRON charges bandwidth/energy separately).
    """
    problems: list[str] = []

    if now >= plan.expiry:
        problems.append(f"quote expired (now {now} >= expiry {plan.expiry})")
    if contract_type != "TransferContract":
        problems.append(f"contract type {contract_type!r} != 'TransferContract'")
    if to_address != plan.inbound_address:
        problems.append(f"tx pays {to_address} != vault {plan.inbound_address}")
    if amount_sun != plan.amount_sun:
        problems.append(f"tx amount {amount_sun} sun != intended {plan.amount_sun}")
    if memo != plan.memo:
        problems.append(f"tx memo {memo!r} != intended {plan.memo!r}")
    if not memo_pays_destination(plan.destination, plan.memo):
        problems.append(
            f"quoted memo {plan.memo!r} does not pay destination {plan.destination}"
        )

    return problems


@dataclasses.dataclass(frozen=True)
class TronTokenSwapPlan:
    """What we intend a TRON TRC-20 (e.g. USDT-TRON) deposit to do.

    TRON has no THORChain router, so the deposit is a plain ``transfer(vault,
    amount)`` on the token contract with the swap ``memo`` in the tx data field.
    ``token`` is the TRC-20 contract the ``TriggerSmartContract`` must target;
    ``inbound_address`` is the vault the transfer must pay; ``amount`` is in the
    token's native units.
    """

    inbound_address: str  # base58 vault (the transfer recipient)
    token: str  # base58 TRC-20 contract (the TriggerSmartContract target)
    amount: int  # token native units transferred to the vault
    memo: str
    expiry: int
    destination: str = ""


def verify_tron_token_swap(
    *,
    contract_type: str,
    trigger_to: str,
    recipient: str,
    transfer_amount: int,
    trx_value: int,
    memo: str,
    plan: TronTokenSwapPlan,
    now: int,
) -> list[str]:
    """Return reasons a TRON TRC-20 deposit does not match ``plan``; empty is safe.

    Binds every field that can cause irreversible loss: the trigger must target
    the intended token contract, the decoded ``transfer`` must pay the vault the
    intended amount, no native TRX may ride along, and the memo (carried in the
    tx data) must match the quote and pay the destination. ``recipient``,
    ``transfer_amount`` and ``memo`` are the already-decoded values.
    """
    problems: list[str] = []

    if now >= plan.expiry:
        problems.append(f"quote expired (now {now} >= expiry {plan.expiry})")
    if contract_type != "TriggerSmartContract":
        problems.append(f"contract type {contract_type!r} != 'TriggerSmartContract'")
    if trigger_to != plan.token:
        problems.append(f"tx triggers {trigger_to} != token contract {plan.token}")
    if recipient != plan.inbound_address:
        problems.append(f"transfer pays {recipient} != vault {plan.inbound_address}")
    if transfer_amount != plan.amount:
        problems.append(f"transfer amount {transfer_amount} != intended {plan.amount}")
    if trx_value != 0:
        problems.append(f"token deposit must not send TRX value (got {trx_value} sun)")
    if memo != plan.memo:
        problems.append(f"tx memo {memo!r} != intended {plan.memo!r}")
    if not memo_pays_destination(plan.destination, plan.memo):
        problems.append(
            f"quoted memo {plan.memo!r} does not pay destination {plan.destination}"
        )

    return problems


@dataclasses.dataclass(frozen=True)
class EthSwapPlan:
    """What we intend an ETH deposit transaction to do (from a THORChain quote)."""

    inbound_address: str
    amount_wei: int
    memo: str
    expiry: int
    chain_id: int = 1
    destination: str = ""  # our payout address; must appear in the memo when set


def verify_eth_swap(
    *,
    to: str,
    value: int,
    data: str,
    chain_id: int,
    gas: int,
    max_fee_per_gas: int,
    plan: EthSwapPlan,
    now: int,
    max_fee_wei: int,
) -> list[str]:
    """Return reasons the ETH deposit tx does not match ``plan``; empty means safe.

    Native ETH deposits send ``value`` wei to the vault with the memo as hex
    calldata. A wrong vault, amount, or memo means irreversible loss.
    """
    problems: list[str] = []

    if now >= plan.expiry:
        problems.append(f"quote expired (now {now} >= expiry {plan.expiry})")
    if (to or "").lower() != plan.inbound_address.lower():
        problems.append(f"tx 'to' {to} != vault {plan.inbound_address}")
    if value != plan.amount_wei:
        problems.append(f"tx value {value} wei != intended {plan.amount_wei}")
    expected_data = "0x" + plan.memo.encode().hex()
    if (data or "").lower() != expected_data.lower():
        problems.append(f"calldata {data!r} != memo-encoded {expected_data!r}")
    if chain_id != plan.chain_id:
        problems.append(f"chainId {chain_id} != {plan.chain_id}")
    if not memo_pays_destination(plan.destination, plan.memo):
        problems.append(
            f"quoted memo {plan.memo!r} does not pay destination {plan.destination}"
        )
    total_fee = gas * max_fee_per_gas
    if total_fee > max_fee_wei:
        problems.append(f"max fee {total_fee} wei exceeds limit {max_fee_wei}")

    return problems


# --- plain external sends (no swap: no vault, no memo, no router) -------------
#
# A send pays an arbitrary recipient the exact amount and carries NO memo/router/
# extra calldata. An unexpected memo or data field signals a misconstructed tx
# (e.g. a swap builder used by mistake) and MUST block — hence these gates assert
# emptiness as strictly as the swap gates assert the memo.


@dataclasses.dataclass(frozen=True)
class EthSendPlan:
    """What we intend a plain native-ETH send to do."""

    recipient: str
    amount_wei: int
    chain_id: int = 1


def verify_eth_send(
    *,
    to: str,
    value: int,
    data: str,
    chain_id: int,
    gas: int,
    max_fee_per_gas: int,
    plan: EthSendPlan,
    max_fee_wei: int,
) -> list[str]:
    """Return reasons a native-ETH send does not match ``plan``; empty means safe."""
    problems: list[str] = []

    if (to or "").lower() != plan.recipient.lower():
        problems.append(f"tx 'to' {to} != recipient {plan.recipient}")
    if value != plan.amount_wei:
        problems.append(f"tx value {value} wei != intended {plan.amount_wei}")
    if (data or "0x") not in ("0x", ""):
        problems.append(f"plain ETH send must carry no calldata, got {data!r}")
    if chain_id != plan.chain_id:
        problems.append(f"chainId {chain_id} != {plan.chain_id}")
    total_fee = gas * max_fee_per_gas
    if total_fee > max_fee_wei:
        problems.append(f"max fee {total_fee} wei exceeds limit {max_fee_wei}")

    return problems


@dataclasses.dataclass(frozen=True)
class EthTokenSendPlan:
    """What we intend a plain ERC-20 send to do: transfer(recipient, amount)."""

    token: str  # the ERC-20 contract the tx must target
    recipient: str  # the decoded transfer recipient
    amount: int  # token native units
    chain_id: int = 1


def verify_eth_token_send(
    *,
    to: str,
    value: int,
    chain_id: int,
    recipient: str,
    transfer_amount: int,
    gas: int,
    max_fee_per_gas: int,
    plan: EthTokenSendPlan,
    max_fee_wei: int,
) -> list[str]:
    """Return reasons an ERC-20 send does not match ``plan``; empty means safe.

    ``recipient``/``transfer_amount`` are the already-decoded ``transfer`` args
    (a routerless, approveless plain token transfer). A wrong token target,
    recipient or amount means irreversible loss.
    """
    problems: list[str] = []

    if (to or "").lower() != plan.token.lower():
        problems.append(f"tx 'to' {to} != token contract {plan.token}")
    if value != 0:
        problems.append(f"token send must not send ETH value (got {value})")
    if (recipient or "").lower() != plan.recipient.lower():
        problems.append(f"transfer pays {recipient} != recipient {plan.recipient}")
    if transfer_amount != plan.amount:
        problems.append(f"transfer amount {transfer_amount} != intended {plan.amount}")
    if chain_id != plan.chain_id:
        problems.append(f"chainId {chain_id} != {plan.chain_id}")
    total_fee = gas * max_fee_per_gas
    if total_fee > max_fee_wei:
        problems.append(f"max fee {total_fee} wei exceeds limit {max_fee_wei}")

    return problems


@dataclasses.dataclass(frozen=True)
class TronSendPlan:
    """What we intend a plain native-TRX send to do."""

    recipient: str  # base58
    amount_sun: int


def verify_tron_send(
    *,
    contract_type: str,
    to_address: str,
    amount_sun: int,
    memo: str,
    plan: TronSendPlan,
) -> list[str]:
    """Return reasons a native-TRX send does not match ``plan``; empty means safe."""
    problems: list[str] = []

    if contract_type != "TransferContract":
        problems.append(f"contract type {contract_type!r} != 'TransferContract'")
    if to_address != plan.recipient:
        problems.append(f"tx pays {to_address} != recipient {plan.recipient}")
    if amount_sun != plan.amount_sun:
        problems.append(f"tx amount {amount_sun} sun != intended {plan.amount_sun}")
    if memo:
        problems.append(f"plain send must carry no memo, got {memo!r}")

    return problems


@dataclasses.dataclass(frozen=True)
class TronTokenSendPlan:
    """What we intend a plain TRC-20 send to do: transfer(recipient, amount)."""

    token: str  # base58 TRC-20 contract (the TriggerSmartContract target)
    recipient: str  # base58 transfer recipient
    amount: int  # token native units


def verify_tron_token_send(
    *,
    contract_type: str,
    trigger_to: str,
    recipient: str,
    transfer_amount: int,
    trx_value: int,
    memo: str,
    plan: TronTokenSendPlan,
) -> list[str]:
    """Return reasons a TRC-20 send does not match ``plan``; empty means safe."""
    problems: list[str] = []

    if contract_type != "TriggerSmartContract":
        problems.append(f"contract type {contract_type!r} != 'TriggerSmartContract'")
    if trigger_to != plan.token:
        problems.append(f"tx triggers {trigger_to} != token contract {plan.token}")
    if recipient != plan.recipient:
        problems.append(f"transfer pays {recipient} != recipient {plan.recipient}")
    if transfer_amount != plan.amount:
        problems.append(f"transfer amount {transfer_amount} != intended {plan.amount}")
    if trx_value != 0:
        problems.append(f"token send must not send TRX value (got {trx_value} sun)")
    if memo:
        problems.append(f"plain send must carry no memo, got {memo!r}")

    return problems


@dataclasses.dataclass(frozen=True)
class CosmosSendPlan:
    """What we intend a plain native send (CACAO/RUNE) to do (MsgSend, no memo)."""

    from_addr: str
    recipient: str
    denom: str
    amount: str  # native base units (CACAO 1e10 / RUNE 1e8), as the on-chain string


def verify_cosmos_send(*, decoded: dict, plan: CosmosSendPlan) -> list[str]:
    """Return reasons a decoded MsgSend body does not match ``plan``; empty is safe.

    ``decoded`` is :func:`cryptoswap_wallet.chains.cosmos_tx.decode_msg_send_body`
    output — i.e. what was *actually serialized*, so a build bug that bound the
    wrong recipient/amount is caught before signing.
    """
    problems: list[str] = []

    if decoded.get("type_url") != "/cosmos.bank.v1beta1.MsgSend":
        problems.append(f"message type {decoded.get('type_url')!r} != MsgSend")
    if decoded.get("from_addr") != plan.from_addr:
        problems.append(f"tx sends from {decoded.get('from_addr')} != {plan.from_addr}")
    if decoded.get("to_addr") != plan.recipient:
        problems.append(
            f"tx pays {decoded.get('to_addr')} != recipient {plan.recipient}"
        )
    if decoded.get("denom") != plan.denom:
        problems.append(f"tx denom {decoded.get('denom')!r} != {plan.denom!r}")
    if decoded.get("amount") != plan.amount:
        problems.append(f"tx amount {decoded.get('amount')} != intended {plan.amount}")
    if decoded.get("memo"):
        problems.append(f"plain send must carry no memo, got {decoded.get('memo')!r}")

    return problems


@dataclasses.dataclass(frozen=True)
class CosmosDepositPlan:
    """What we intend a native ``MsgDeposit`` (swap from CACAO/RUNE) to do.

    A native swap has no inbound vault — the memo drives it — so the gate binds
    the deposited coin/amount, the memo, our own signer, and that the memo pays
    the intended destination.
    """

    asset: str  # "MAYA.CACAO" / "THOR.RUNE"
    amount: str  # native base units, as the on-chain string
    memo: str
    destination: str
    signer: bytes  # our 20-byte account (must match the address we derived)
    expiry: int


def verify_cosmos_deposit(
    *, decoded: dict, plan: CosmosDepositPlan, now: int
) -> list[str]:
    """Return reasons a decoded MsgDeposit body does not match ``plan``; empty is ok."""
    problems: list[str] = []

    if now >= plan.expiry:
        problems.append(f"quote expired (now {now} >= expiry {plan.expiry})")
    if decoded.get("type_url") != "/types.MsgDeposit":
        problems.append(f"message type {decoded.get('type_url')!r} != MsgDeposit")
    if decoded.get("coins") != [(plan.asset, plan.amount)]:
        problems.append(
            f"tx deposits {decoded.get('coins')} != intended "
            f"[({plan.asset!r}, {plan.amount!r})]"
        )
    if decoded.get("memo") != plan.memo:
        problems.append(f"tx memo {decoded.get('memo')!r} != quoted {plan.memo!r}")
    if decoded.get("signer") != plan.signer:
        problems.append("tx signer != our derived account")
    if not memo_pays_destination(plan.destination, plan.memo):
        problems.append(
            f"quoted memo {plan.memo!r} does not pay destination {plan.destination}"
        )

    return problems
