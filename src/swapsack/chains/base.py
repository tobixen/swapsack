"""The common interface every chain adapter implements.

The uniform surface across chains is intentionally small: address derivation,
a wallet balance (so `balance` scales without per-chain code), and broadcast.
Building the swap transaction is chain-specific (UTXO vs account models differ),
but every adapter funnels its result through the shared :mod:`swapsack.verify`
gate before signing.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol, runtime_checkable

from swapsack.chains.coins import RBF_SEQUENCE_MAX


@dataclasses.dataclass(frozen=True)
class AddressInfo:
    """One address's history + balance, as probed by an adapter's data source.

    ``has_history`` (not a nonzero balance) is what keeps the gap-limit scan
    going past used-but-emptied addresses — see :mod:`swapsack.chains.scan`.
    """

    has_history: bool
    confirmed: int  # base units (sats/duffs/zats), confirmed balance
    pending: int  # base units, net mempool delta (negative when spending)


@dataclasses.dataclass(frozen=True)
class BalanceReport:
    """A chain-agnostic balance, in the chain's base units."""

    symbol: str
    confirmed: int
    decimals: int
    pending: int = 0
    note: str = ""
    # The wallet addresses this balance covers, so `balance` can probe them for
    # liquidity positions without re-deriving/re-scanning (BTC is multi-address).
    addresses: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class TxEntry:
    """One input or output of a broadcast transaction, in the chain's base units.

    The display fields (``value``/``address``) are all a ``status`` view needs.
    The rest are what it takes to *rebuild* the transaction for a fee bump: an
    input must name the outpoint it spends and the nSequence it spent it with,
    and an OP_RETURN output must carry its payload byte-for-byte — a swap memo
    re-encoded from a lossy summary is a mis-sent deposit.
    """

    value: int
    address: str | None = None  # None for an OP_RETURN (or an unparsed script)
    op_return: bool = False
    # Inputs only: the outpoint being spent, and its nSequence (the RBF signal).
    txid: str | None = None
    vout: int | None = None
    sequence: int | None = None
    # Outputs only: the decoded OP_RETURN payload (None if not one, or unreadable).
    op_return_data: bytes | None = None
    # Outputs only, and only where the data source reports it (Dash's Insight
    # does; Esplora does not): the txid that has since spent this output.
    # ``None`` means "not reported", NOT "unspent" — see
    # :func:`swapsack.chains.history.wallet_history`, which infers the rest.
    spent_by: str | None = None


@dataclasses.dataclass(frozen=True)
class TxSummary:
    """What a broadcast transaction actually did, as the chain reports it.

    Chain-neutral on purpose: BTC builds it from an Esplora ``/tx`` body and
    DASH from an Insight one, and everything downstream — ``status``, the fee
    bump planner, the history listing — reads this shape rather than either
    explorer's JSON. ``inputs``/``outputs`` are in order, so a partial send
    reads as "one recipient, one change" — the shape a user needs to confirm
    their remainder came back — and an output's index in ``outputs`` is its
    ``vout``.
    """

    txid: str
    confirmed: bool
    block_height: int | None
    fee: int  # base units
    vsize: int  # virtual bytes; fee/vsize is the fee rate
    inputs: tuple[TxEntry, ...]
    outputs: tuple[TxEntry, ...]
    # Seconds since the epoch, as the explorer dates the block. None while
    # unconfirmed (a mempool transaction has no block to be dated by).
    block_time: int | None = None

    @property
    def total_in(self) -> int:
        return sum(i.value for i in self.inputs)

    @property
    def total_out(self) -> int:
        return sum(o.value for o in self.outputs)

    @property
    def fee_rate(self) -> float:
        return self.fee / self.vsize if self.vsize else 0.0

    @property
    def has_op_return(self) -> bool:
        """True if the tx carries a memo — i.e. it is a swap deposit, not a send."""
        return any(o.op_return for o in self.outputs)

    @property
    def signals_rbf(self) -> bool:
        """True if BIP125 opt-in Replace-By-Fee applies: any input below 0xfffffffe.

        Standard-policy nodes replace a mempool transaction only when it signals,
        so this is the difference between a bump that relays and one that is
        dropped by every peer. An input whose sequence the explorer did not
        report counts as not signalling — fail closed.
        """
        return any(
            i.sequence is not None and i.sequence < RBF_SEQUENCE_MAX
            for i in self.inputs
        )


@runtime_checkable
class ChainAdapter(Protocol):
    chain: str  # e.g. "BTC"
    asset: str  # THORChain asset notation, e.g. "BTC.BTC"

    def derive_address(self, mnemonic: str, path: str) -> str: ...

    def wallet_balance(self, mnemonic: str) -> BalanceReport:
        """The wallet's balance for this chain, derived from the mnemonic."""
        ...

    def broadcast(self, raws: list[str]) -> str:
        """Broadcast a signed transaction; return its txid."""
        ...
