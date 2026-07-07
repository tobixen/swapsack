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

# DEFAULT_TOLERANCE_BPS lives in thorchain (re-exported here for callers like
# cli) so the client default and this protocol default can't drift apart.
from cryptoswap_wallet.thorchain import (
    DEFAULT_TOLERANCE_BPS,
    ChainStatus,
    Quote,
    ThorchainError,
    effective_tolerance_bps,
)


class SwapAborted(RuntimeError):
    """Raised when a swap must not proceed (halted chain, too small, unsafe tx)."""


def _explain_quote_error(exc: ThorchainError, tolerance_bps: int) -> str:
    """Turn a raw THORChain quote rejection into an actionable abort message.

    The common, confusing case is ``emit asset ... less than price limit ...``:
    THORChain derives the price limit from ``tolerance_bps`` off the spot price,
    so when the swap's fees/slippage exceed the tolerance the emitted amount
    falls below the limit and the quote is refused. Small swaps trip this easily
    because fixed outbound fees dominate them.
    """
    msg = str(exc)
    if "price limit" in msg:
        return (
            f"THORChain rejected the quote: the swap's fees and slippage exceed "
            f"your {tolerance_bps / 100:.2f}% tolerance. Send a larger amount "
            f"(fixed outbound fees dominate small swaps), spread it over blocks "
            f"with --stream-interval to cut slippage, or raise --tolerance-bps. "
            f"[{msg}]"
        )
    return f"THORChain rejected the quote: {msg}"


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
        streaming_interval: int | None = None,
        streaming_quantity: int | None = None,
        tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
    ) -> Quote: ...

    def mimir(self) -> dict: ...


def lp_deposit_pause_reason(mimir: dict, pool: str) -> str | None:
    """The mimir key pausing LP *deposits* for ``pool``, or None if open.

    THORChain refunds add-liquidity deposits while any of these are set, so the
    caller should abort before broadcasting. Withdrawals stay open so LPs can
    exit. ``pool`` is e.g. ``TRON.TRX``; the per-pool key uses ``-`` for ``.``.
    """
    chain = pool.split(".", 1)[0]
    for key in (
        "PAUSELP",
        f"PAUSELP{chain}",
        f"PAUSELPDEPOSIT-{pool.replace('.', '-')}",
    ):
        if int(mimir.get(key, 0) or 0) >= 1:
            return key
    return None


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
    streaming_interval: int | None = None,
    streaming_quantity: int | None = None,
    **build_kwargs: object,
) -> Prepared:
    """Run the chain-agnostic checks, then delegate build+verify to the adapter.

    Chain-specific inputs (UTXOs/fee_rate/change for BTC; nonce/gas/fees for ETH)
    are passed through ``build_kwargs`` to ``adapter.build_and_verify``.

    ``streaming_interval`` (blocks between sub-swaps) turns this into a streaming
    swap: the trade is split over blocks to cut slippage. When set, THORChain
    returns a memo carrying the ``…/interval/quantity`` suffix, which the adapter
    embeds and the verify gate binds like any other memo. ``streaming_quantity``
    of ``None``/``0`` lets the network pick the sub-swap count that minimises slip.
    """
    # A native source (RUNE/CACAO) is deposited to the chain itself via
    # MsgDeposit — there is no external inbound vault, and the quote below fails
    # anyway if trading is paused. But the deposit executes on the adapter's own
    # network no matter which network produced the quote, so make sure we are
    # talking to the home network: a network that lists the adapter's chain
    # among its *inbound* (external) chains is the other one (e.g. Maya quoting
    # THOR.RUNE), and the MsgDeposit would carry a foreign-priced memo — a
    # refund minus the native fee at best, a swap at unconfirmed terms at worst.
    if getattr(adapter, "native_source", False):
        if adapter.chain in thorchain.inbound_addresses():
            raise SwapAborted(
                f"this backend treats {adapter.chain} as an external chain, but a "
                f"native {request.from_asset} swap deposits on {adapter.chain} "
                f"itself — use the {adapter.chain}-native backend"
            )
    else:
        status = thorchain.inbound_addresses().get(adapter.chain)
        if status is None or not status.tradable:
            raise SwapAborted(f"{adapter.chain} is not currently tradable on THORChain")

    try:
        # Streaming drops tolerance_bps (LIM=0) — the same shared rule backend
        # selection applies, so the executed swap quotes at the same limit.
        quote = thorchain.quote_swap(
            request.from_asset,
            request.to_asset,
            request.amount,
            request.destination,
            streaming_interval=streaming_interval,
            streaming_quantity=streaming_quantity,
            tolerance_bps=effective_tolerance_bps(tolerance_bps, streaming_interval),
        )
    except ThorchainError as exc:
        raise SwapAborted(_explain_quote_error(exc, tolerance_bps)) from exc
    if request.amount < quote.recommended_min_amount_in:
        raise SwapAborted(
            f"amount {request.amount} is below the recommended minimum "
            f"{quote.recommended_min_amount_in}; swap would be uneconomical"
        )
    if not quote.memo:
        raise SwapAborted("THORChain quote returned no memo (missing destination?)")
    # parse_quote tolerates a missing inbound_address because native (RUNE/
    # CACAO) quotes legitimately have none — but an external-chain source pays
    # *to* that vault, so an empty one (degraded/malformed node response) must
    # abort here, not crash inside signing or build a tx around "".
    if not getattr(adapter, "native_source", False) and not quote.inbound_address:
        raise SwapAborted(
            "quote returned no inbound vault address (malformed or degraded "
            "node response); not building a transaction"
        )

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
    # An add-liquidity deposit (memo "+:POOL") is refunded minus gas while LP is
    # paused, so check the mimir toggles first. Withdrawals ("-:…") stay open.
    if memo.startswith("+"):
        pool = memo.split(":")[1] if ":" in memo else ""
        reason = lp_deposit_pause_reason(thorchain.mimir(), pool)
        if reason:
            raise SwapAborted(
                f"THORChain has LP deposits paused (mimir {reason}); an add would "
                f"be observed and then refunded minus gas. Not broadcasting."
            )
    deposit_amount = status.dust_threshold if amount is None else amount
    # parse_inbound_addresses tolerates a missing dust_threshold (parses as 0),
    # but a 0-value deposit is money lost: gas burned on ETH on a tx the vault
    # ignores, a below-dust output the network rejects on BTC.
    if deposit_amount <= 0:
        raise SwapAborted(
            f"no usable dust threshold for {adapter.chain} (degraded "
            f"inbound_addresses response?); refusing a 0-value deposit"
        )
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
