"""Client for the THORChain REST API: swap quotes, inbound addresses, status.

All monetary amounts are expressed in THORChain's fixed 1e8 base units,
regardless of the underlying asset's native precision (1 BTC, 1 ETH and 1 RUNE
are all ``100_000_000`` here).

The pure parsing helpers (:func:`parse_quote`, :func:`parse_inbound_addresses`)
are kept free of any I/O so they can be tested against recorded responses; the
HTTP plumbing comes from :class:`cryptoswap_wallet.net.HttpClient`.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from cryptoswap_wallet.net import HttpClient

if TYPE_CHECKING:
    from collections.abc import Mapping

THORCHAIN_UNIT = 100_000_000
DEFAULT_BASE_URL = "https://thornode.thorchain.liquify.com"


class ThorchainError(RuntimeError):
    """Raised when the THORChain API returns an error response."""


@dataclasses.dataclass(frozen=True)
class ChainStatus:
    """Per-chain state from ``/thorchain/inbound_addresses``."""

    chain: str
    gas_rate: int
    gas_rate_units: str
    outbound_fee: int
    dust_threshold: int
    halted: bool
    global_trading_paused: bool
    chain_trading_paused: bool
    address: str = ""  # inbound vault address (for non-quoted deposits, e.g. LP)
    router: str | None = None  # EVM router contract, when present

    @property
    def tradable(self) -> bool:
        """True only if no halt or pause flag is set for this chain."""
        return not (
            self.halted or self.global_trading_paused or self.chain_trading_paused
        )


@dataclasses.dataclass(frozen=True)
class SwapFees:
    """Fee breakdown from a quote, denominated in the destination asset."""

    asset: str
    outbound: int
    affiliate: int
    liquidity: int
    total: int
    slippage_bps: int
    total_bps: int


@dataclasses.dataclass(frozen=True)
class Quote:
    """A swap quote from ``/thorchain/quote/swap``.

    ``memo`` is only returned when a ``destination`` was supplied. ``router`` is
    set for EVM source chains, where deposits go through a router contract rather
    than a plain transfer.
    """

    inbound_address: str
    expected_amount_out: int
    memo: str | None
    fees: SwapFees
    recommended_min_amount_in: int
    expiry: int
    dust_threshold: int
    recommended_gas_rate: int
    gas_rate_units: str
    router: str | None
    max_streaming_quantity: int
    streaming_swap_blocks: int
    total_swap_seconds: int
    raw: Mapping[str, Any]


def _int(value: str | int) -> int:
    """Coerce THORChain's stringly-typed integers (e.g. ``"7761"``) to int."""
    return int(value)


def parse_inbound_addresses(payload: list[dict[str, Any]]) -> dict[str, ChainStatus]:
    """Parse the ``/thorchain/inbound_addresses`` array, keyed by chain."""
    chains: dict[str, ChainStatus] = {}
    for entry in payload:
        chains[entry["chain"]] = ChainStatus(
            chain=entry["chain"],
            gas_rate=_int(entry["gas_rate"]),
            gas_rate_units=entry["gas_rate_units"],
            outbound_fee=_int(entry["outbound_fee"]),
            dust_threshold=_int(entry["dust_threshold"]),
            halted=bool(entry.get("halted", False)),
            global_trading_paused=bool(entry.get("global_trading_paused", False)),
            chain_trading_paused=bool(entry.get("chain_trading_paused", False)),
            address=entry.get("address", ""),
            router=entry.get("router"),
        )
    return chains


def parse_quote(payload: dict[str, Any]) -> Quote:
    """Parse a ``/thorchain/quote/swap`` response into a :class:`Quote`.

    Raises :class:`ThorchainError` if the payload carries an ``error`` field.
    """
    if "error" in payload:
        raise ThorchainError(payload["error"])
    fees = payload["fees"]
    return Quote(
        inbound_address=payload["inbound_address"],
        expected_amount_out=_int(payload["expected_amount_out"]),
        memo=payload.get("memo"),
        fees=SwapFees(
            asset=fees["asset"],
            outbound=_int(fees["outbound"]),
            affiliate=_int(fees["affiliate"]),
            liquidity=_int(fees["liquidity"]),
            total=_int(fees["total"]),
            slippage_bps=_int(fees["slippage_bps"]),
            total_bps=_int(fees["total_bps"]),
        ),
        recommended_min_amount_in=_int(payload["recommended_min_amount_in"]),
        expiry=_int(payload["expiry"]),
        dust_threshold=_int(payload["dust_threshold"]),
        recommended_gas_rate=_int(payload["recommended_gas_rate"]),
        gas_rate_units=payload["gas_rate_units"],
        router=payload.get("router"),
        max_streaming_quantity=_int(payload.get("max_streaming_quantity", 0)),
        streaming_swap_blocks=_int(payload.get("streaming_swap_blocks", 0)),
        total_swap_seconds=_int(payload.get("total_swap_seconds", 0)),
        raw=payload,
    )


class ThorchainClient(HttpClient):
    """Thin wrapper around a thornode-style REST API.

    Used for both THORChain (``path_prefix="thorchain"``) and its fork Maya
    (``path_prefix="mayachain"``) — the API shape and ``=:`` memos are identical.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        client_id: str | None = None,
        timeout: float = 20.0,
        path_prefix: str = "thorchain",
    ) -> None:
        super().__init__(timeout)
        self.base_url = base_url.rstrip("/")
        self.path_prefix = path_prefix
        self._headers = {"x-client-id": client_id} if client_id else {}

    def inbound_addresses(self) -> dict[str, ChainStatus]:
        resp = self._get(
            f"{self.base_url}/{self.path_prefix}/inbound_addresses",
            headers=self._headers,
        )
        resp.raise_for_status()
        return parse_inbound_addresses(resp.json())

    def tx_status(self, txid: str) -> dict[str, Any]:
        resp = self._get(
            f"{self.base_url}/{self.path_prefix}/tx/status/{txid}",
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    def quote_swap(
        self,
        from_asset: str,
        to_asset: str,
        amount: int,
        destination: str | None = None,
        *,
        streaming_interval: int | None = None,
        streaming_quantity: int | None = None,
        tolerance_bps: int | None = None,
    ) -> Quote:
        """Fetch a swap quote. ``amount`` is in 1e8 base units of ``from_asset``."""
        params: dict[str, Any] = {
            "from_asset": from_asset,
            "to_asset": to_asset,
            "amount": amount,
        }
        if destination is not None:
            params["destination"] = destination
        if streaming_interval is not None:
            params["streaming_interval"] = streaming_interval
        if streaming_quantity is not None:
            params["streaming_quantity"] = streaming_quantity
        if tolerance_bps is not None:
            params["tolerance_bps"] = tolerance_bps
        resp = self._get(
            f"{self.base_url}/{self.path_prefix}/quote/swap",
            params=params,
            headers=self._headers,
        )
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                resp.raise_for_status()
            else:
                raise ThorchainError(payload.get("error", resp.text))
        return parse_quote(resp.json())
