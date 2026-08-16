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
from swapsack.thorchain import (
    DEFAULT_TOLERANCE_BPS,
    ChainStatus,
    PoolDepth,
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

    def pool(self, asset: str) -> PoolDepth: ...


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
    # MsgDeposit — there is no external inbound vault to look up. But the deposit
    # executes on the adapter's own network no matter which network produced the
    # quote, so the quoting client must be the home one: quoting THOR.RUNE on the
    # maya backend would return a Maya-priced memo (a refund minus the native fee
    # at best, a swap at unconfirmed terms at worst). This is a LOCAL identity
    # check — comparing the adapter's home path_prefix against the client's — so
    # a native swap that needs no vault data makes no inbound_addresses() call
    # (which would add a round trip and an uncaught-HTTP crash mode). The CLI
    # already pins native sources to their home backend; this guards a direct
    # library caller that pairs a native adapter with the wrong client.
    if getattr(adapter, "native_source", False):
        home = getattr(adapter, "home_path_prefix", None)
        actual = getattr(thorchain, "path_prefix", None)
        if home is not None and actual is not None and home != actual:
            raise SwapAborted(
                f"this backend ({actual}) is not {adapter.chain}'s home network, "
                f"but a native {request.from_asset} swap deposits on "
                f"{adapter.chain} itself — use the {adapter.chain}-native backend"
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
    # A withdraw (amount=None) triggers with a nominal deposit of the chain's
    # dust_threshold. That is legitimately 0 on EVM chains (Maya reports "0" for
    # ETH/ARB/KUJI/THOR): a 0-value native tx carrying the memo is exactly how
    # those chains trigger a withdraw, so 0 must NOT be treated as an error here
    # — doing so locked every EVM LP position. UTXO chains report a real
    # nonzero dust; a genuinely below-dust BTC output (only from a malformed
    # node response) is rejected by the network at broadcast, not silently lost.
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


# --- symmetric (two-sided) liquidity ----------------------------------------


class PartialSymmetricAdd(BroadcastError):
    """The protocol leg is broadcast but the asset leg failed — position pending.

    The one outcome a symmetric add cannot undo. It subclasses
    :class:`BroadcastError` so an existing ``except BroadcastError`` still
    catches it, but carries ``protocol_txid`` so the caller can tell the user
    exactly what is live on-chain rather than reporting a bare failure.
    """

    def __init__(self, protocol_txid: str, cause: Exception) -> None:
        super().__init__(
            f"the protocol leg IS BROADCAST (txid {protocol_txid}) but the asset "
            f"leg failed: {cause}"
        )
        self.protocol_txid = protocol_txid
        self.cause = cause


@dataclasses.dataclass(frozen=True)
class SymmetricPrepared:
    """Both legs of a symmetric add, built and gated but not broadcast."""

    asset: Prepared
    protocol: Prepared
    pool: str
    asset_amount: int  # THORChain 1e8 units of the pool asset
    protocol_amount: int  # RUNE/CACAO native base units (1e8 / 1e10)
    asset_address: str
    protocol_address: str
    asset_memo: str
    protocol_memo: str

    @property
    def problems(self) -> list[str]:
        """Both gates' problems, labelled by leg (either one blocks the add)."""
        return [f"asset leg: {p}" for p in self.asset.problems] + [
            f"protocol leg: {p}" for p in self.protocol.problems
        ]

    @property
    def safe(self) -> bool:
        return self.asset.safe and self.protocol.safe


@dataclasses.dataclass(frozen=True)
class SymmetricResult:
    prepared: SymmetricPrepared
    protocol_txid: str | None
    asset_txid: str | None
    broadcast: bool


def prepare_symmetric_liquidity(
    *,
    thorchain: ThorchainLike,
    asset_adapter: SwapSource,
    protocol_adapter,  # noqa: ANN001 (chains.cosmos.CosmosAdapter)
    pool: str,
    asset_amount: int,
    asset_address: str,
    protocol_address: str,
    mnemonic: str,
    now: int,
    **asset_build_kwargs: object,
) -> SymmetricPrepared:
    """Build + gate **both** legs of a symmetric add, broadcasting neither.

    The asset leg deposits ``asset_amount`` to the asset chain's inbound vault
    with memo ``+:POOL:<protocol_address>``; the protocol leg is a native
    ``MsgDeposit`` of the pool-ratio-matched RUNE/CACAO amount with memo
    ``+:POOL:<asset_address>``. The protocol pairs them by matching each memo's
    referenced address against the *other* leg's observed sender, which is why
    ``asset_address`` must be the address the asset leg will actually send from.
    For an account-model chain that is the single derived address; a UTXO source
    has no unambiguous sender (the protocol observes ``vin[0]`` by convention),
    so callers should not offer this for one — see docs/liquidity-symmetric.md.

    Every check that can refuse the add happens before either leg is built, so
    an abort here means nothing was signed and nothing is live.
    """
    from swapsack.liquidity import pair_amount, symmetric_add_memo

    # The LP-pause check must happen here rather than per-leg: the protocol leg
    # goes to the chain itself, not to an inbound vault, so it never passes
    # through prepare_liquidity's gate and would otherwise be unchecked. A
    # paused pool refunds an add minus gas — on two chains, here.
    reason = lp_deposit_pause_reason(thorchain.mimir(), pool)
    if reason:
        raise SwapAborted(
            f"LP deposits are paused (mimir {reason}); a symmetric add would be "
            f"observed and then refunded minus gas on both legs. Not broadcasting."
        )
    status = thorchain.inbound_addresses().get(asset_adapter.chain)
    if status is None or not status.tradable:
        raise SwapAborted(f"{asset_adapter.chain} is not currently tradable")
    if not status.address:
        raise SwapAborted(f"no inbound vault address for {asset_adapter.chain}")

    depth = thorchain.pool(pool)
    protocol_amount = pair_amount(
        asset_amount, depth.balance_asset, depth.balance_protocol
    )
    held = protocol_adapter.fetch_balance(protocol_address)
    if held < protocol_amount:
        unit = 10**protocol_adapter.decimals
        raise SwapAborted(
            f"the {protocol_adapter.symbol} leg needs "
            f"{protocol_amount / unit:.8f} {protocol_adapter.symbol} at the "
            f"current pool ratio but {protocol_address} holds {held / unit:.8f} "
            f"(and the native tx fee comes out of that too)"
        )

    asset_memo = symmetric_add_memo(pool, protocol_address)
    protocol_memo = symmetric_add_memo(pool, asset_address)
    asset_leg = asset_adapter.build_and_verify_deposit(
        vault=status.address,
        memo=asset_memo,
        amount=asset_amount,
        now=now,
        mnemonic=mnemonic,
        **asset_build_kwargs,
    )
    protocol_leg = protocol_adapter.build_and_verify_native_deposit(
        memo=protocol_memo,
        amount=protocol_amount,
        mnemonic=mnemonic,
        now=now,
    )
    return SymmetricPrepared(
        asset=asset_leg,
        protocol=protocol_leg,
        pool=pool,
        asset_amount=asset_amount,
        protocol_amount=protocol_amount,
        asset_address=asset_address,
        protocol_address=protocol_address,
        asset_memo=asset_memo,
        protocol_memo=protocol_memo,
    )


def execute_symmetric_liquidity(
    prepared: SymmetricPrepared,
    *,
    asset_adapter: SwapSource,
    protocol_adapter,  # noqa: ANN001 (chains.cosmos.CosmosAdapter)
    confirm: bool,
) -> SymmetricResult:
    """Broadcast both legs, protocol first. Refuses unless *both* gates passed.

    Order is deliberate. The protocol leg is native, cheap and fast, so if it
    fails nothing is live and the expensive asset leg is simply never sent —
    the benign failure, and the one we can still choose. Reversing the order
    would risk stranding the costly leg instead.

    If the asset leg fails *after* the protocol leg is out, the position is
    genuinely half-added and :class:`PartialSymmetricAdd` carries the live txid.
    """
    if not prepared.safe:
        raise SwapAborted(
            "verify gate refused the transaction: " + "; ".join(prepared.problems)
        )
    if not confirm:
        return SymmetricResult(
            prepared=prepared, protocol_txid=None, asset_txid=None, broadcast=False
        )
    # Nothing is live yet, so a failure here propagates as an ordinary error.
    protocol_txid = protocol_adapter.broadcast(
        protocol_adapter.sign(prepared.protocol.built)
    )
    try:
        asset_txid = asset_adapter.broadcast(asset_adapter.sign(prepared.asset.built))
    except Exception as exc:
        raise PartialSymmetricAdd(protocol_txid, exc) from exc
    return SymmetricResult(
        prepared=prepared,
        protocol_txid=protocol_txid,
        asset_txid=asset_txid,
        broadcast=True,
    )
