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

    def format(self) -> str:
        amount = self.confirmed / 10**self.decimals
        line = f"{self.symbol}: {amount:.8f}"
        if self.pending:
            line += f" (+{self.pending / 10**self.decimals:.8f} pending)"
        if self.note:
            line += f"  {self.note}"
        return line


@runtime_checkable
class ChainAdapter(Protocol):
    chain: str  # e.g. "BTC"
    asset: str  # THORChain asset notation, e.g. "BTC.BTC"

    def derive_address(self, mnemonic: str, path: str) -> str: ...

    def wallet_balance(self, mnemonic: str) -> BalanceReport:
        """The wallet's balance for this chain, derived from the mnemonic."""
        ...

    def broadcast(self, raw_hex: str) -> str:
        """Broadcast a signed transaction; return its txid."""
        ...
