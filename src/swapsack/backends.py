"""Swap backends and lowest-price routing.

Two kinds of backend exist behind one small protocol (``name``, ``client``,
``executor``, ``serves()``, ``try_quote()``):

- *thornode-style* (THORChain and its fork Maya — same API and ``=:`` memo
  format, so one client drives both): executes by paying an inbound vault with
  a memo (``executor = "memo-deposit"``).
- *CoW Protocol*: same-chain ETH-token swaps executed by signing an EIP-712
  order (``executor = "signed-order"``); see ``swapsack.cow``.

``gather_quotes`` asks every backend that can serve the pair and normalizes to
"expected output in 1e8 units", so ``best_quote`` picks the backend giving the
most output regardless of kind; the CLI then dispatches execution on
``executor``.
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING, Protocol

from swapsack.net import HTTP_ERRORS
from swapsack.thorchain import (
    DEFAULT_BASE_URLS,
    Quote,
    ThorchainClient,
    ThorchainError,
    effective_tolerance_bps,
)

if TYPE_CHECKING:
    from swapsack.cow import CowQuote

# See thorchain.DEFAULT_BASE_URLS for why THORChain has a fallback list rather
# than a single default. Maya has no known second public node yet, so it stays
# a single URL; override either with SWAPSACK_THORNODE/SWAPSACK_MAYANODE.
DEFAULT_MAYANODE = "https://mayanode.mayachain.info"


# A native asset (RUNE/CACAO) is swapped by depositing on its own network via
# MsgDeposit, so only that network's backend can serve it — the other network
# treats the asset as an external chain (inbound vault + MsgSend, which
# CosmosAdapter does not implement). Keyed by the adapter's chain.
NATIVE_HOME_BACKEND = {"THOR": "thorchain", "MAYA": "maya"}


class SwapBackend(Protocol):
    """What ``gather_quotes``/``best_quote`` and the CLI need from a backend."""

    name: str
    client: object  # has close(); thornode-style backends expose ThorchainClient
    executor: str  # "memo-deposit" | "signed-order" — the CLI dispatches on it

    def serves(self, from_asset: str, to_asset: str) -> bool: ...

    def try_quote(
        self,
        from_asset: str,
        to_asset: str,
        amount: int,
        destination: str | None,
        *,
        tolerance_bps: int | None = None,
        streaming_interval: int | None = None,
        streaming_quantity: int | None = None,
    ) -> Quote | CowQuote | None: ...


@dataclasses.dataclass(frozen=True)
class Backend:
    """A thornode-style backend (THORChain / Maya)."""

    name: str
    client: ThorchainClient

    executor = "memo-deposit"

    def serves(self, from_asset: str, to_asset: str) -> bool:
        # Whether a pool exists/trades is only knowable from the quote itself,
        # so a thornode backend "serves" everything and lets try_quote decide.
        return True

    def try_quote(
        self,
        from_asset: str,
        to_asset: str,
        amount: int,
        destination: str | None,
        *,
        tolerance_bps: int | None = None,
        streaming_interval: int | None = None,
        streaming_quantity: int | None = None,
    ) -> Quote | None:
        """One quote, or None when this backend can't serve the swap (no pool,
        halted, below minimum, no memo, or a network error).

        ``tolerance_bps`` is always passed explicitly (None -> the client omits
        the param -> no limit — the informational ``quote`` path, where the
        price must come back even when fees exceed any default tolerance);
        merely leaving the kwarg off would let the client fall back to its
        DEFAULT_TOLERANCE_BPS and refuse the quote. Streaming forces LIM=0 via
        the shared effective_tolerance_bps rule.
        """
        extra: dict[str, int | None] = {
            "tolerance_bps": effective_tolerance_bps(tolerance_bps, streaming_interval)
        }
        if streaming_interval is not None:
            extra["streaming_interval"] = streaming_interval
            if streaming_quantity is not None:
                extra["streaming_quantity"] = streaming_quantity
        try:
            quote = self.client.quote_swap(
                from_asset, to_asset, amount, destination, **extra
            )
        except (ThorchainError, *HTTP_ERRORS):
            return None
        if quote.memo and amount >= quote.recommended_min_amount_in:
            return quote
        return None


def default_backends() -> list[Backend]:
    """The thornode-style backends (used directly by LP/status/balance, which
    speak the thornode API; swap routing adds CoW via :func:`swap_backends`)."""
    thornode = os.environ.get("SWAPSACK_THORNODE") or DEFAULT_BASE_URLS
    mayanode = os.environ.get("SWAPSACK_MAYANODE") or DEFAULT_MAYANODE
    return [
        Backend("thorchain", ThorchainClient(thornode)),
        Backend("maya", ThorchainClient(mayanode, path_prefix="mayachain")),
    ]


def swap_backends() -> list[SwapBackend]:
    """Every backend a swap/quote can price-route across."""
    from swapsack.cow import default_cow_backend

    return [*default_backends(), default_cow_backend()]


def get_backend(name: str) -> SwapBackend:
    for backend in swap_backends():
        if backend.name == name:
            return backend
    raise ValueError(f"unknown backend {name!r}")


def gather_quotes(
    backends: list[SwapBackend],
    from_asset: str,
    to_asset: str,
    amount: int,
    destination: str | None,
    *,
    tolerance_bps: int | None = None,
    streaming_interval: int | None = None,
    streaming_quantity: int | None = None,
) -> list[tuple[SwapBackend, Quote | CowQuote]]:
    """Quote every backend that can serve this pair; drop the ones that can't.

    ``tolerance_bps`` is threaded into each quote, so backend selection happens
    at the same tolerance the swap will lock in. ``streaming_interval``/
    ``streaming_quantity`` request a streaming (slip-reducing) quote on the
    thornode backends — and rule out backends with no streaming concept — so
    selection reflects the price the swap will actually use.
    """
    results: list[tuple[SwapBackend, Quote | CowQuote]] = []
    for backend in backends:
        if not backend.serves(from_asset, to_asset):
            continue
        quote = backend.try_quote(
            from_asset,
            to_asset,
            amount,
            destination,
            tolerance_bps=tolerance_bps,
            streaming_interval=streaming_interval,
            streaming_quantity=streaming_quantity,
        )
        if quote is not None:
            results.append((backend, quote))
    return results


def best_quote(
    results: list[tuple[SwapBackend, Quote | CowQuote]],
) -> tuple[SwapBackend, Quote | CowQuote]:
    """The backend giving the most output (expected_amount_out, 1e8 base units)."""
    return max(results, key=lambda pair: pair[1].expected_amount_out)
