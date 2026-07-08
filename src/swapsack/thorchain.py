"""Client for the THORChain REST API: swap quotes, inbound addresses, status.

All monetary amounts are expressed in THORChain's fixed 1e8 base units,
regardless of the underlying asset's native precision (1 BTC, 1 ETH and 1 RUNE
are all ``100_000_000`` here).

The pure parsing helpers (:func:`parse_quote`, :func:`parse_inbound_addresses`)
are kept free of any I/O so they can be tested against recorded responses; the
HTTP plumbing comes from :class:`swapsack.net.HttpClient`.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from swapsack.net import HttpClient

if TYPE_CHECKING:
    from collections.abc import Mapping

THORCHAIN_UNIT = 100_000_000
# See backends.DEFAULT_THORNODE: the old liquify default's cert expired 2024-02-07.
DEFAULT_BASE_URL = "https://thornode.thorchain.network"
# Default price tolerance for a quote, in basis points. Defined here (not in
# swap.py) so the client default matches the ThorchainLike protocol default
# without a circular import.
DEFAULT_TOLERANCE_BPS = 300


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


# THORChain accounting is a fixed 1e8 for (almost) every asset. Maya's native
# CACAO is the exception: it is 1e10 (10 decimals). Amounts denominated in the
# destination asset (quote output, fee breakdown) must divide by the asset's own
# unit, not the 1e8 default, or CACAO renders 100x too large. Keyed by the full
# THORChain/Maya asset string (e.g. "MAYA.CACAO"). Must agree with the adapter
# decimals (maya.CACAO_DECIMALS etc.) — this module stays free of the heavy
# adapter imports, so tests cross-check the two instead of deriving one from
# the other.
_ASSET_UNITS: dict[str, int] = {"MAYA.CACAO": 10**10}


def asset_unit(asset: str) -> int:
    """Base units per whole coin for a THORChain/Maya ``asset`` string.

    Defaults to :data:`THORCHAIN_UNIT` (1e8); only assets that deviate (Maya's
    1e10 CACAO) need an entry.
    """
    return _ASSET_UNITS.get(asset, THORCHAIN_UNIT)


def effective_tolerance_bps(
    tolerance_bps: int | None, streaming_interval: int | None
) -> int | None:
    """The tolerance to send with a quote request: streaming forces ``None``.

    A tolerance limit and streaming don't mix on THORChain/Maya — a tight price
    limit defeats streaming's own slip management, and the node then reports the
    base (non-streamed) emit and refuses. The single rule both backend selection
    (``gather_quotes``) and the swap (``prepare_swap``) apply, so they cannot
    drift and quote at different limits.
    """
    return None if streaming_interval is not None else tolerance_bps


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

    def breakdown(self, symbol: str) -> list[str]:
        """Itemised, human-readable cost lines for the destination ``symbol``.

        Amounts are in the destination asset's own base units (1e8 for almost
        everything; 1e10 for Maya's CACAO — see :func:`asset_unit`). On THORChain
        the *liquidity* fee **is** the slip (a bigger trade vs. the pool depth
        costs more), so it is labelled slip/swap; ``outbound`` is the flat fee to
        deliver the output on the destination chain. This is the quoted cost
        only — the inbound (source-chain) tx fee is separate and printed by the
        per-chain swap path.
        """
        unit = asset_unit(self.asset)
        lines = [
            f"  slip/swap fee  {self.liquidity / unit:.8f} {symbol}"
            f"  ({self.slippage_bps} bps)",
            f"  outbound fee   {self.outbound / unit:.8f} {symbol}  (flat)",
        ]
        if self.affiliate:
            lines.append(f"  affiliate      {self.affiliate / unit:.8f} {symbol}")
        lines.append(
            f"  quoted total   {self.total / unit:.8f} {symbol}"
            f"  ({self.total_bps} bps of input)"
        )
        return lines


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


@dataclasses.dataclass(frozen=True)
class PoolDepth:
    """A pool's current depths, from ``/pool/{asset}``; used to value the RUNE/
    CACAO side of an LP position in asset terms. 1e8 base units (Maya names the
    protocol balance ``balance_cacao`` where THORChain uses ``balance_rune``)."""

    asset: str
    balance_asset: int
    balance_protocol: int

    @property
    def asset_per_protocol(self) -> float:
        """Asset units per 1 unit of RUNE/CACAO (0 for an empty/absent pool)."""
        if not self.balance_protocol:
            return 0.0
        return self.balance_asset / self.balance_protocol


def parse_pool_depth(payload: dict[str, Any]) -> PoolDepth:
    protocol = payload.get("balance_rune", payload.get("balance_cacao", "0"))
    return PoolDepth(
        asset=payload.get("asset", ""),
        balance_asset=_int(payload.get("balance_asset", "0")),
        balance_protocol=_int(protocol),
    )


@dataclasses.dataclass(frozen=True)
class LiquidityPosition:
    """A wallet's stake in a pool, from ``pool/{asset}/liquidity_provider/{addr}``.

    All amounts are THORChain 1e8 base units (1 whole asset == ``100_000_000``).
    ``asset_redeem_value`` is what is currently redeemable on this chain's side;
    ``protocol_redeem_value`` is the RUNE (THORChain) / CACAO (Maya) side, which
    is non-zero for symmetric or aged single-sided positions. A single-sided
    withdraw converts that side back to the asset, so given a pool price we fold
    it into a total; without one we flag it rather than silently drop it.

    ``*_deposit_value`` is the protocol's per-side valuation of the position at
    deposit time, **not** what the wallet sent: even a single-sided asset add
    gets a non-zero RUNE/CACAO ``deposit_value`` (the protocol books your units
    across both sides). So the two legs together are the contribution; we never
    show the raw protocol leg (it isn't asset units and can't be added to them),
    but fold it into one asset-equivalent figure at the pool price.
    """

    pool: str
    asset_address: str
    units: int
    asset_redeem_value: int
    pending_asset: int
    protocol_redeem_value: int
    asset_deposit_value: int = 0
    protocol_deposit_value: int = 0

    def format(
        self,
        source: str,
        *,
        protocol: str = "RUNE",
        protocol_price_in_asset: float | None = None,
    ) -> str:
        """A one-line LP summary for ``balance``; ``source`` is the backend name.

        With ``protocol_price_in_asset`` (asset units per 1 RUNE/CACAO) the RUNE/
        CACAO side is valued and folded into an estimated total; the figure is
        gross of the exit slip/fees a real withdraw would pay, hence ``~``. The
        deposit is shown the same way (one asset-equivalent number, both legs
        repriced at the *current* pool price) — an estimate of cost basis, not an
        exact deposit-time figure.
        """

        def in_asset(value: int) -> float:
            return value * (protocol_price_in_asset or 0.0) / THORCHAIN_UNIT

        asset_side = self.asset_redeem_value / THORCHAIN_UNIT
        if protocol_price_in_asset is not None and self.protocol_redeem_value:
            side = in_asset(self.protocol_redeem_value)
            total = asset_side + side
            head = (
                f"~{total:.8f} redeemable "
                f"({asset_side:.8f} asset + {side:.8f} via {protocol})"
            )
        elif self.protocol_redeem_value:
            head = (
                f"{asset_side:.8f} redeemable "
                f"(plus a {protocol}-side value not counted)"
            )
        else:
            head = f"{asset_side:.8f} redeemable"
        line = f"  +LP {source} {self.pool}: {head}"
        extras = []
        if protocol_price_in_asset is not None and (
            self.asset_deposit_value or self.protocol_deposit_value
        ):
            deposited = self.asset_deposit_value / THORCHAIN_UNIT + in_asset(
                self.protocol_deposit_value
            )
            extras.append(f"deposited ~{deposited:.8f}")
        if self.pending_asset:
            extras.append(f"+{self.pending_asset / THORCHAIN_UNIT:.8f} pending")
        if extras:
            line += "; " + "; ".join(extras)
        return line


def parse_liquidity_provider(payload: dict[str, Any]) -> LiquidityPosition | None:
    """Parse a ``liquidity_provider`` response, or ``None`` when nothing's worth
    reporting.

    An address with no position answers HTTP 200 with ``units: "0"`` (not a 404),
    and a fully-withdrawn position can linger with units but nothing redeemable;
    both collapse to ``None``. Maya names the protocol side ``cacao_*`` where
    THORChain uses ``rune_*``.
    """
    if "error" in payload:
        return None
    redeem = payload.get("rune_redeem_value", payload.get("cacao_redeem_value", "0"))
    deposit = payload.get("rune_deposit_value", payload.get("cacao_deposit_value", "0"))
    pos = LiquidityPosition(
        pool=payload.get("asset", ""),
        asset_address=payload.get("asset_address", ""),
        units=_int(payload.get("units", "0")),
        asset_redeem_value=_int(payload.get("asset_redeem_value", "0")),
        pending_asset=_int(payload.get("pending_asset", "0")),
        protocol_redeem_value=_int(redeem),
        asset_deposit_value=_int(payload.get("asset_deposit_value", "0")),
        protocol_deposit_value=_int(deposit),
    )
    if not (pos.asset_redeem_value or pos.pending_asset or pos.protocol_redeem_value):
        return None
    return pos


def normalize_txid(txid: str) -> str:
    """Normalise a user-supplied txid to the form thornode/mayanode index by.

    EVM tx hashes are quoted with a ``0x`` prefix by explorers, wallets and our
    own broadcast output, but THORChain/Maya store and look up observed inbound
    hashes *without* it. ``tx/status`` answers an unknown hash with an empty
    "never observed" body (not a 404), so passing the prefix verbatim makes an
    already-confirmed inbound look perpetually stuck. UTXO/Cosmos txids carry no
    ``0x`` prefix, so stripping it is a no-op for them (and the endpoint
    uppercases the hex itself, so case needs no handling here).
    """
    return txid.removeprefix("0x").removeprefix("0X")


def parse_inbound_addresses(payload: list[dict[str, Any]]) -> dict[str, ChainStatus]:
    """Parse the ``/thorchain/inbound_addresses`` array, keyed by chain."""
    chains: dict[str, ChainStatus] = {}
    for entry in payload:
        # Read every field with .get: a partial/degraded thornode response must
        # degrade to defaults (and let the halt/pause flags gate tradability),
        # not raise KeyError mid-swap-prep.
        chain = entry.get("chain", "")
        chains[chain] = ChainStatus(
            chain=chain,
            gas_rate=_int(entry.get("gas_rate", 0)),
            gas_rate_units=entry.get("gas_rate_units", ""),
            outbound_fee=_int(entry.get("outbound_fee", 0)),
            dust_threshold=_int(entry.get("dust_threshold", 0)),
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
        # A native-source (RUNE/CACAO) swap is a MsgDeposit to the chain itself,
        # so the quote carries no inbound vault address.
        inbound_address=payload.get("inbound_address", ""),
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
        # dust/gas fields describe the inbound tx on an external source chain; a
        # native-source (RUNE/CACAO) MsgDeposit quote omits them.
        dust_threshold=_int(payload.get("dust_threshold", 0)),
        recommended_gas_rate=_int(payload.get("recommended_gas_rate", 0)),
        gas_rate_units=payload.get("gas_rate_units", ""),
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
            f"{self.base_url}/{self.path_prefix}/tx/status/{normalize_txid(txid)}",
            headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    def liquidity_provider(self, pool: str, address: str) -> LiquidityPosition | None:
        """``address``'s LP position in ``pool``, or ``None`` if it has none.

        A pool this backend doesn't run (e.g. ``TRON.TRX`` on Maya) 404s; treat
        that as "no position" rather than an error, so a single shared address
        set can be probed against every backend.
        """
        resp = self._get(
            f"{self.base_url}/{self.path_prefix}/pool/{pool}/liquidity_provider/{address}",
            headers=self._headers,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return parse_liquidity_provider(resp.json())

    def pool(self, asset: str) -> PoolDepth:
        """Current depths for ``asset``'s pool (to value an LP's RUNE/CACAO side)."""
        resp = self._get(
            f"{self.base_url}/{self.path_prefix}/pool/{asset}",
            headers=self._headers,
        )
        resp.raise_for_status()
        return parse_pool_depth(resp.json())

    def mimir(self) -> dict[str, Any]:
        """Network config toggles (e.g. ``PAUSELP``); values are ints."""
        resp = self._get(
            f"{self.base_url}/{self.path_prefix}/mimir",
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
        tolerance_bps: int | None = DEFAULT_TOLERANCE_BPS,
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
