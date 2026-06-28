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

DEFAULT_THORNODE = "https://thornode.thorchain.liquify.com"
DEFAULT_MAYANODE = "https://mayanode.mayachain.info"


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
) -> list[tuple[Backend, Quote]]:
    """Quote every backend; drop ones that can't serve this swap (no pool, halted,
    below minimum, no memo, or a network error)."""
    results: list[tuple[Backend, Quote]] = []
    for backend in backends:
        try:
            quote = backend.client.quote_swap(from_asset, to_asset, amount, destination)
        except (ThorchainError, *HTTP_ERRORS):
            continue
        if quote.memo and amount >= quote.recommended_min_amount_in:
            results.append((backend, quote))
    return results


def best_quote(results: list[tuple[Backend, Quote]]) -> tuple[Backend, Quote]:
    """The backend giving the most output (expected_amount_out, 1e8 base units)."""
    return max(results, key=lambda pair: pair[1].expected_amount_out)
