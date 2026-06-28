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
    if plan.destination and plan.destination.lower() not in plan.memo.lower():
        problems.append(
            f"quoted memo {plan.memo!r} does not pay destination {plan.destination}"
        )
    total_fee = gas * max_fee_per_gas
    if total_fee > max_fee_wei:
        problems.append(f"max fee {total_fee} wei exceeds limit {max_fee_wei}")

    return problems
