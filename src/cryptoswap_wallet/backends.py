"""Swap backends and lowest-price routing.

A backend is a thornode-style network we can quote + deposit against. THORChain
and its fork Maya share the same API and ``=:`` memo format, so the same client
and chain adapters drive both — only the base URL, path prefix and asset set
differ. ``gather_quotes`` + ``best_quote`` pick the backend giving the most output.
"""

from __future__ import annotations

import dataclasses
import os

from cryptoswap_wallet.net import HTTP_ERRORS
from cryptoswap_wallet.thorchain import Quote, ThorchainClient, ThorchainError

# thornode.thorchain.liquify.com's TLS cert expired 2024-02-07 (never renewed)
# and the ninerealms gateways were retired (no A record), so default to a node
# that currently resolves + serves a valid cert. Override with
# CRYPTOSWAP_WALLET_THORNODE if this one degrades.
DEFAULT_THORNODE = "https://thornode.thorchain.network"
DEFAULT_MAYANODE = "https://mayanode.mayachain.info"


# A native asset (RUNE/CACAO) is swapped by depositing on its own network via
# MsgDeposit, so only that network's backend can serve it — the other network
# treats the asset as an external chain (inbound vault + MsgSend, which
# CosmosAdapter does not implement). Keyed by the adapter's chain.
NATIVE_HOME_BACKEND = {"THOR": "thorchain", "MAYA": "maya"}


@dataclasses.dataclass(frozen=True)
class Backend:
    name: str
    client: ThorchainClient


def default_backends() -> list[Backend]:
    thornode = os.environ.get("CRYPTOSWAP_WALLET_THORNODE") or DEFAULT_THORNODE
    mayanode = os.environ.get("CRYPTOSWAP_WALLET_MAYANODE") or DEFAULT_MAYANODE
    return [
        Backend("thorchain", ThorchainClient(thornode)),
        Backend("maya", ThorchainClient(mayanode, path_prefix="mayachain")),
    ]


def get_backend(name: str) -> Backend:
    for backend in default_backends():
        if backend.name == name:
            return backend
    raise ValueError(f"unknown backend {name!r}")


def gather_quotes(
    backends: list[Backend],
    from_asset: str,
    to_asset: str,
    amount: int,
    destination: str | None,
    *,
    tolerance_bps: int | None = None,
    streaming_interval: int | None = None,
    streaming_quantity: int | None = None,
) -> list[tuple[Backend, Quote]]:
    """Quote every backend; drop ones that can't serve this swap (no pool, halted,
    below minimum, no memo, or a network error).

    ``tolerance_bps`` is threaded into each quote, so backend selection happens
    at the same tolerance the swap will lock in. ``None`` sends *no* limit — the
    informational ``quote`` path, where the price must come back even when fees
    exceed any default tolerance. ``streaming_interval``/``streaming_quantity``
    request a streaming (slip-reducing) quote so backend selection reflects the
    same streamed price the swap will use.
    """
    # tolerance_bps is always passed explicitly (None -> the client omits the
    # param -> no limit); merely leaving the kwarg off would let the client
    # fall back to its DEFAULT_TOLERANCE_BPS and refuse the quote.
    extra: dict[str, int | None] = {"tolerance_bps": tolerance_bps}
    if streaming_interval is not None:
        # A tolerance limit and streaming don't mix on THORChain/Maya: a tight
        # price limit defeats streaming's own slip management, and the node then
        # reports the base (non-streamed) emit and refuses — streaming forces
        # LIM=0.
        extra["streaming_interval"] = streaming_interval
        if streaming_quantity is not None:
            extra["streaming_quantity"] = streaming_quantity
        extra["tolerance_bps"] = None
    results: list[tuple[Backend, Quote]] = []
    for backend in backends:
        try:
            quote = backend.client.quote_swap(
                from_asset, to_asset, amount, destination, **extra
            )
        except (ThorchainError, *HTTP_ERRORS):
            continue
        if quote.memo and amount >= quote.recommended_min_amount_in:
            results.append((backend, quote))
    return results


def best_quote(results: list[tuple[Backend, Quote]]) -> tuple[Backend, Quote]:
    """The backend giving the most output (expected_amount_out, 1e8 base units)."""
    return max(results, key=lambda pair: pair[1].expected_amount_out)
