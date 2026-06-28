"""Swap orchestration: the chain-agnostic half of the pipeline.

``prepare_swap`` does the parts identical for every source chain — tradable
check, quote, recommended-minimum, memo-present — then delegates the
chain-specific tx shape, verify-plan and gate to the source adapter's
``build_and_verify``. ``execute_swap`` signs + broadcasts a gate-passed swap.

This keeps one orchestrator (and one adapter protocol) instead of a near-identical
copy per source chain; see A4 in docs/core-review.md.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

from cryptoswap_wallet.thorchain import ChainStatus, Quote

DEFAULT_TOLERANCE_BPS = 300


class SwapAborted(RuntimeError):
    """Raised when a swap must not proceed (halted chain, too small, unsafe tx)."""


class BroadcastError(RuntimeError):
    """Raised when broadcasting a signed tx is rejected by the network/node.

    Adapters wrap their library-specific broadcast errors in this so the CLI can
    report a clean message instead of leaking a traceback.
    """


@dataclasses.dataclass(frozen=True)
class SwapRequest:
    from_asset: str
    to_asset: str
    amount: int  # THORChain 1e8 base units of from_asset
    destination: str  # address on the destination chain


@dataclasses.dataclass(frozen=True)
class Prepared:
    quote: Quote | None  # None for liquidity deposits (no swap quote)
    built: object  # chain-specific built tx (BuiltSwap / EthBuiltSwap)
    plan: object  # chain-specific verify plan (SwapPlan / EthSwapPlan)
    problems: list[str]

    @property
    def safe(self) -> bool:
        return not self.problems


@dataclasses.dataclass(frozen=True)
class SwapResult:
    prepared: Prepared
    txid: str | None
    broadcast: bool


class ThorchainLike(Protocol):
    def inbound_addresses(self) -> dict[str, ChainStatus]: ...

    def quote_swap(
        self,
        from_asset: str,
        to_asset: str,
        amount: int,
        destination: str | None = None,
        *,
        tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
    ) -> Quote: ...


class SwapSource(Protocol):
    """A source-chain adapter: builds + verifies its own swap, signs, broadcasts."""

    chain: str

    def build_and_verify(
        self, *, quote: Quote, request: SwapRequest, now: int, **kwargs: object
    ) -> Prepared: ...

    def build_and_verify_deposit(
        self, *, vault: str, memo: str, amount: int, now: int, **kwargs: object
    ) -> Prepared:
        """Build + verify a non-quoted deposit to ``vault`` carrying ``memo``."""
        ...

    def sign(self, built: object) -> list[str]:
        """Sign the built swap; returns raw txs in broadcast order (1 or more)."""
        ...

    def broadcast(self, raws: list[str]) -> str:
        """Broadcast the signed txs in order; return the tracking txid (the last)."""
        ...


def prepare_swap(
    *,
    thorchain: ThorchainLike,
    adapter: SwapSource,
    request: SwapRequest,
    now: int,
    tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
    **build_kwargs: object,
) -> Prepared:
    """Run the chain-agnostic checks, then delegate build+verify to the adapter.

    Chain-specific inputs (UTXOs/fee_rate/change for BTC; nonce/gas/fees for ETH)
    are passed through ``build_kwargs`` to ``adapter.build_and_verify``.
    """
    status = thorchain.inbound_addresses().get(adapter.chain)
    if status is None or not status.tradable:
        raise SwapAborted(f"{adapter.chain} is not currently tradable on THORChain")

    quote = thorchain.quote_swap(
        request.from_asset,
        request.to_asset,
        request.amount,
        request.destination,
        tolerance_bps=tolerance_bps,
    )
    if request.amount < quote.recommended_min_amount_in:
        raise SwapAborted(
            f"amount {request.amount} is below the recommended minimum "
            f"{quote.recommended_min_amount_in}; swap would be uneconomical"
        )
    if not quote.memo:
        raise SwapAborted("THORChain quote returned no memo (missing destination?)")

    return adapter.build_and_verify(
        quote=quote, request=request, now=now, **build_kwargs
    )


def prepare_liquidity(
    *,
    thorchain: ThorchainLike,
    adapter: SwapSource,
    memo: str,
    amount: int | None,
    now: int,
    **build_kwargs: object,
) -> Prepared:
    """Prepare an (experimental) liquidity add/withdraw deposit to the vault.

    ``amount`` of ``None`` means "use the chain's dust threshold" (for withdraws,
    where the deposit is just a trigger).

    Caveat (less verifiable than a swap by construction): the vault here comes
    from ``inbound_addresses`` and the verify gate then checks the tx pays that
    same address — the same input on both sides. A swap cross-checks the vault
    against an independent quote; LP has no second source, so a compromised
    THORNode response is not caught here. The ``+:POOL`` / ``-:POOL:bps`` memos
    are simple and unit-tested, and LP is opt-in experimental. Treat the vault
    as trusted only as far as you trust the configured THORNode.
    """
    status = thorchain.inbound_addresses().get(adapter.chain)
    if status is None or not status.tradable:
        raise SwapAborted(f"{adapter.chain} is not currently tradable on THORChain")
    if not status.address:
        raise SwapAborted(f"no inbound vault address for {adapter.chain}")
    deposit_amount = status.dust_threshold if amount is None else amount
    return adapter.build_and_verify_deposit(
        vault=status.address, memo=memo, amount=deposit_amount, now=now, **build_kwargs
    )


def execute_swap(
    prepared: Prepared, adapter: SwapSource, *, confirm: bool
) -> SwapResult:
    """Sign and broadcast a prepared swap. Refuses unless the verify gate passed.

    With ``confirm=False`` this is a dry run: nothing is signed or broadcast.
    """
    if not prepared.safe:
        raise SwapAborted(
            "verify gate refused the transaction: " + "; ".join(prepared.problems)
        )
    if not confirm:
        return SwapResult(prepared=prepared, txid=None, broadcast=False)
    raws = adapter.sign(prepared.built)
    txid = adapter.broadcast(raws)
    return SwapResult(prepared=prepared, txid=txid, broadcast=True)
