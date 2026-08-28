"""Chainflip backend: a second independent cross-chain venue, quoting keylessly.

THORChain and Maya share a codebase and a failure mode — on 2026-08-18 they were
both halted at once, leaving BTC->ETH with no route at all (see
``docs/halt-alternatives.md``). Chainflip is a separate protocol with its own
validators and pools, so pricing against it is both a price check and the
resilience hedge ``--backend auto`` was meant to provide.

This module is **phase B1: quoting only**. The REST quote is keyless, so this is
a read-only price source with no money path. Execution is a *vault swap* — a
plain Bitcoin transaction paying a protocol vault with the swap parameters in an
OP_RETURN, no broker and no deposit channel — which is a separate phase; see
``docs/chainflip-effort.md``. Until then the backend advertises the
``vault-swap`` executor, which the CLI refuses to run rather than silently
handing a Chainflip quote to the thornode deposit path.

Amounts cross this module in two units, as in :mod:`swapsack.cow`: the
wallet-wide 1e8 base units at the backend surface (so ``best_quote`` can compare
across backends), and each asset's own native decimals inside the quote (what
the API speaks).
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING, Any

from swapsack.net import HTTP_ERRORS, HttpClient
from swapsack.thorchain import THORCHAIN_UNIT, SwapFees, asset_unit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DEFAULT_CHAINFLIP_API = "https://chainflip-swap.chainflip.io/v2"

# THORChain-style asset string -> (Chainflip chain, Chainflip asset, decimals).
# Keys must match cli.ASSET values. Chainflip also lists SOL, DOT, FLIP, WBTC
# and the Assethub/Solana stablecoins, which have no wallet key yet — adding one
# is a separate destination-only change, not a line here.
CHAINFLIP_ASSETS: dict[str, tuple[str, str, int]] = {
    "BTC.BTC": ("Bitcoin", "BTC", 8),
    "ETH.ETH": ("Ethereum", "ETH", 18),
    "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48": ("Ethereum", "USDC", 6),
    "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7": ("Ethereum", "USDT", 6),
    "ARB.ETH": ("Arbitrum", "ETH", 18),
    "ARB.USDC-0XAF88D065E77C8CC2239327C5EDB3A432268E5831": ("Arbitrum", "USDC", 6),
    "TRON.TRX": ("Tron", "TRX", 6),
    "TRON.USDT-TR7NHQJEKQXGTCI8Q8ZY4PL8OTSZGJLJ6T": ("Tron", "USDT", 6),
}


class ChainflipError(RuntimeError):
    """Raised when the Chainflip quote API returns an error or a junk body."""


@dataclasses.dataclass(frozen=True)
class ChainflipFees(SwapFees):
    """Chainflip's three fee legs, converted to destination 1e8 units.

    Chainflip charges in three *different* assets — INGRESS in the source,
    NETWORK in the intermediate (USDC), EGRESS in the destination — where
    ``SwapFees`` is destination-denominated throughout. The inherited fields
    keep the generic surface working (``best_quote``, ``fees.total_bps``):
    ``outbound`` is EGRESS, the flat cost of delivering on the destination
    chain, and ``liquidity`` is what it costs to get through the pools.
    ``breakdown`` is overridden because THORChain's "slip/swap fee" wording
    would be a lie here — Chainflip's slip is in the price, not in a fee field.
    """

    ingress: int = 0
    network: int = 0
    egress: int = 0

    def breakdown(self, symbol: str) -> list[str]:
        unit = asset_unit(self.asset)
        return [
            f"  ingress fee    {self.ingress / unit:.8f} {symbol}  (source chain)",
            f"  network fee    {self.network / unit:.8f} {symbol}  (protocol)",
            f"  egress fee     {self.egress / unit:.8f} {symbol}  (flat)",
            f"  quoted total   {self.total / unit:.8f} {symbol}"
            f"  ({self.total_bps} bps of input)",
        ]


@dataclasses.dataclass(frozen=True)
class ChainflipQuote:
    """A parsed ``/v2/quote`` response, normalized for cross-backend comparison.

    ``expected_amount_out`` and ``fees`` are the generic surface every backend
    exposes; the native-decimal amounts below are kept verbatim for the
    execution phase (and for the tests that pin the conversions).
    """

    expected_amount_out: int  # 1e8 units of the destination asset
    fees: ChainflipFees
    egress_amount: int  # native destination units, after the egress fee
    deposit_amount: int  # native source units
    intermediate_amount: int | None  # native USDC units, None on a single leg
    estimated_duration_seconds: int
    low_liquidity_warning: bool
    recommended_slippage_bps: int
    raw: Mapping[str, Any]


def select_quote(payload: Any) -> Mapping[str, Any]:  # noqa: ANN401 (JSON)
    """The quote to use out of the API's response.

    ``/v2/quote`` answers with an *array* of routes; take the ``REGULAR`` one.
    The others (and the nested ``boostQuote``) buy faster confirmation for an
    extra fee, which is not the route we would take by default.
    """
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, list) or not payload:
        raise ChainflipError(f"no quote in response: {payload!r:.120}")
    for entry in payload:
        if isinstance(entry, dict) and entry.get("type") == "REGULAR":
            return entry
    first = payload[0]
    if not isinstance(first, dict):
        raise ChainflipError(f"no quote in response: {payload!r:.120}")
    return first


def parse_chainflip_quote(
    payload: Any,  # noqa: ANN401 (JSON: object or array)
    *,
    from_asset: str,
    to_asset: str,
) -> ChainflipQuote:
    """Parse a quote response; raises :class:`ChainflipError` on an error body.

    Also on a *structurally* surprising one — a proxy error page served as a
    200, say. Only :class:`ChainflipError` and HTTP errors are caught by
    callers, so a bare ``KeyError`` would come out as a traceback instead of a
    clean abort (the same reasoning as :func:`swapsack.cow.parse_cow_quote`).
    """
    quote = select_quote(payload)
    if "message" in quote and "egressAmount" not in quote:
        raise ChainflipError(str(quote["message"]))
    try:
        return _parse_quote_fields(quote, from_asset=from_asset, to_asset=to_asset)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ChainflipError(f"malformed quote response: {exc!r}") from exc


def _parse_quote_fields(
    quote: Mapping[str, Any], *, from_asset: str, to_asset: str
) -> ChainflipQuote:
    """The field reads of :func:`parse_chainflip_quote`, which wraps what they
    raise."""
    dest_unit = 10 ** CHAINFLIP_ASSETS[to_asset][2]
    egress = int(quote["egressAmount"])
    deposit = int(quote["depositAmount"])
    intermediate = quote.get("intermediateAmount")
    intermediate = int(intermediate) if intermediate is not None else None
    fees = _convert_fees(
        quote["includedFees"],
        to_asset=to_asset,
        dest_unit=dest_unit,
        egress=egress,
        deposit=deposit,
        intermediate=intermediate,
    )
    slippage_pct = float(quote.get("recommendedSlippageTolerancePercent") or 0)
    return ChainflipQuote(
        expected_amount_out=egress * THORCHAIN_UNIT // dest_unit,
        fees=fees,
        egress_amount=egress,
        deposit_amount=deposit,
        intermediate_amount=intermediate,
        estimated_duration_seconds=int(quote.get("estimatedDurationSeconds") or 0),
        low_liquidity_warning=bool(quote.get("lowLiquidityWarning", False)),
        recommended_slippage_bps=round(slippage_pct * 100),
        raw=quote,
    )


def _convert_fees(
    included: Sequence[Mapping[str, Any]],
    *,
    to_asset: str,
    dest_unit: int,
    egress: int,
    deposit: int,
    intermediate: int | None,
) -> ChainflipFees:
    """Chainflip's three fee legs, each converted to destination 1e8 units.

    Each leg is charged in a different asset, so each needs its own rate, and
    both rates come from the quote itself rather than a price feed (a second
    source could disagree with the very quote we are pricing):

    * INGRESS is in the source asset -> value it at the quote's end-to-end rate,
      ``egress / deposit``.
    * NETWORK is in the intermediate asset (USDC) -> value it at
      ``egress / intermediate``. Using the end-to-end rate here would be wrong
      by the whole first pool leg.
    * EGRESS is already in the destination asset.

    A same-chain pair has no intermediate leg; a NETWORK fee there is charged in
    the destination asset already.
    """
    native: dict[str, int] = {"INGRESS": 0, "NETWORK": 0, "EGRESS": 0}
    for fee in included:
        kind = str(fee.get("type", ""))
        amount = int(fee["amount"])
        if kind == "INGRESS":
            native["INGRESS"] += amount * egress // deposit if deposit else 0
        elif kind == "NETWORK":
            native["NETWORK"] += (
                amount * egress // intermediate if intermediate else amount
            )
        elif kind == "EGRESS":
            native["EGRESS"] += amount
        else:
            # An unknown leg is still money: fold it in rather than under-report.
            native["NETWORK"] += amount * egress // deposit if deposit else 0
    scaled = {k: v * THORCHAIN_UNIT // dest_unit for k, v in native.items()}
    total = sum(scaled.values())
    # Input-relative, like thornode's: the fee against what the swap would have
    # paid out with no fees at all.
    gross = egress * THORCHAIN_UNIT // dest_unit + total
    return ChainflipFees(
        asset=to_asset,
        outbound=scaled["EGRESS"],
        affiliate=0,
        liquidity=scaled["INGRESS"] + scaled["NETWORK"],
        total=total,
        # recommendedSlippageTolerancePercent is a *tolerance*, not realised
        # slip; reporting it here would overstate the cost. The Market: line is
        # what surfaces the pool-vs-market spread.
        slippage_bps=0,
        total_bps=10000 * total // gross if gross else 0,
        ingress=scaled["INGRESS"],
        network=scaled["NETWORK"],
        egress=scaled["EGRESS"],
    )


def deposit_units(amount: int, decimals: int) -> int:
    """Scale a wallet-wide 1e8 ``amount`` to the source asset's native units."""
    return amount * 10**decimals // THORCHAIN_UNIT


class ChainflipClient(HttpClient):
    """Thin client for the Chainflip swapping service's quote API (keyless)."""

    def __init__(
        self, base_url: str = DEFAULT_CHAINFLIP_API, timeout: float = 20.0
    ) -> None:
        super().__init__(timeout)
        self.base_url = base_url.rstrip("/")

    def quote(self, src: tuple[str, str], dst: tuple[str, str], amount: int) -> Any:  # noqa: ANN401 (JSON: the API answers with an array)
        """A quote for ``amount`` (native source units) from ``src`` to ``dst``,
        each a ``(chain, asset)`` pair."""
        resp = self._get(
            f"{self.base_url}/quote",
            params={
                "srcChain": src[0],
                "srcAsset": src[1],
                "destChain": dst[0],
                "destAsset": dst[1],
                "amount": str(amount),
            },
        )
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                resp.raise_for_status()
            else:
                raise ChainflipError(
                    str(payload.get("message", payload) if payload else resp.text)
                )
        return resp.json()


@dataclasses.dataclass(frozen=True)
class ChainflipBackend:
    """Chainflip as a swap backend next to thorchain/maya/cow.

    ``executor`` says this backend settles by paying a protocol vault with the
    swap parameters encoded in the transaction — *not* by the thornode ``=:``
    memo the CLI's deposit path builds. That path is not implemented yet, so the
    CLI refuses to execute here; quotes still price-compete in ``gather_quotes``.
    """

    client: ChainflipClient
    name: str = "chainflip"

    executor = "vault-swap"

    def serves(self, from_asset: str, to_asset: str) -> bool:
        """Any distinct pair of assets Chainflip lists *and* the wallet names.

        Whether a pool has the liquidity to fill it is only knowable from the
        quote, which is ``try_quote``'s job — the same division as the thornode
        backends.
        """
        return (
            from_asset in CHAINFLIP_ASSETS
            and to_asset in CHAINFLIP_ASSETS
            and from_asset != to_asset
        )

    def try_quote(
        self,
        from_asset: str,
        to_asset: str,
        amount: int,
        destination: str | None = None,  # noqa: ARG002 (price needs no address)
        *,
        tolerance_bps: int | None = None,  # noqa: ARG002 (no quote-side limit)
        streaming_interval: int | None = None,
        streaming_quantity: int | None = None,  # noqa: ARG002 (with the interval)
    ) -> ChainflipQuote | None:
        """One quote, or None when this backend can't serve the swap.

        No ``destination`` is needed — unlike CoW, the Chainflip quote is purely
        a price, so ``quote`` works before a ``--dest`` is known. Streaming is a
        thornode concept (Chainflip's analogue is DCA, not wired up here), so a
        streaming request rules this backend out rather than quoting a route the
        swap would not take. ``tolerance_bps`` shapes the vault swap's on-chain
        ``min_output_amount`` floor at execution time, not the quote.
        """
        if streaming_interval is not None or not self.serves(from_asset, to_asset):
            return None
        src = CHAINFLIP_ASSETS[from_asset]
        native = deposit_units(amount, src[2])
        if native <= 0:
            return None
        try:
            payload = self.client.quote(src[:2], CHAINFLIP_ASSETS[to_asset][:2], native)
            return parse_chainflip_quote(
                payload, from_asset=from_asset, to_asset=to_asset
            )
        except (ChainflipError, KeyError, ValueError, TypeError, *HTTP_ERRORS):
            return None


def default_chainflip_backend() -> ChainflipBackend:
    base_url = os.environ.get("SWAPSACK_CHAINFLIP_API") or DEFAULT_CHAINFLIP_API
    return ChainflipBackend(ChainflipClient(base_url))
