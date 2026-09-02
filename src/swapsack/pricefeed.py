"""Best-effort external spot-price lookup, to compare a swap quote against a
public market mid.

This is **advisory only**: it is used to print a "vs market" line so the user
can see the *total* realised cost of a swap (protocol fees + slip + the
pool-vs-market spread that arbitrageurs capture), which the quote's own fee
fields do not include. It is never consulted when building, verifying, or
broadcasting a transaction — a wrong or unreachable price must never change what
gets signed, so every caller treats a failure here as "just skip the line".

Keyless (CoinGecko ``simple/price``). The pure helpers are kept free of I/O so
they can be unit-tested against a recorded response.
"""

from __future__ import annotations

import dataclasses

from swapsack.net import HttpClient

DEFAULT_COINGECKO = "https://api.coingecko.com/api/v3"
# Human-readable name of the price source, shown in the swap/quote output header.
SOURCE = "CoinGecko"

# Wallet ASSET key (the --from / --to values) -> CoinGecko coin id. Tokens map
# to the underlying asset regardless of chain (USDT on ETH or TRON is "tether").
# Assets absent here simply get no market line (e.g. RUNE/CACAO/synths).
COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "TRX": "tron",
    "LTC": "litecoin",
    "DOGE": "dogecoin",
    "BCH": "bitcoin-cash",
    "DASH": "dash",
    "ZEC": "zcash",
    "CACAO": "cacao",
    "RUNE": "thorchain",
    "BNB": "binancecoin",
    "ATOM": "cosmos",
    "XRP": "ripple",
    "ADA": "cardano",
    # Native ETH on Arbitrum is ETH — same asset, same price, different chain.
    "ETH-ARB": "ethereum",
    # "avalanche-2", not "avalanche" (a different, long-defunct coin). A wrong
    # id silently drops the market line rather than erroring.
    "AVAX": "avalanche-2",
    "USDT-ETH": "tether",
    "USDT-TRON": "tether",
    "USDC-ETH": "usd-coin",
    # Same dollar, cheaper chain — one price for all three.
    "USDC-AVAX": "usd-coin",
    "USDT-AVAX": "tether",
    "USDC-ARB": "usd-coin",
    # BSC is hold+balance only (nothing trades it), so these have no ASSET entry
    # — but `balance` prints the rows, and an unpriceable row it cannot value is
    # named in the sheet's footer every single run if they are missing here.
    "USDT-BSC": "tether",
    "USDC-BSC": "usd-coin",
}


@dataclasses.dataclass(frozen=True)
class Unit:
    """A unit the `balance` sheet can be denominated in.

    ``vs`` is CoinGecko's ``vs_currencies`` name, which is *not* always the name
    the user types: it has no ``usdt``/``usdc``, so the dollar stablecoins have
    to mean ``usd`` — near enough for a display total, and far better than a
    flag that silently prices nothing.
    """

    name: str
    vs: str
    prefix: str = ""
    suffix: str = ""
    decimals: int = 2

    def format(self, value: float) -> str:
        return f"{self.prefix}{value:,.{self.decimals}f}{self.suffix}".replace(",", "")


UNITS: dict[str, Unit] = {
    "EUR": Unit("EUR", "eur", prefix="€"),
    "USD": Unit("USD", "usd", prefix="$"),
    "USDT": Unit("USDT", "usd", prefix="$"),
    "USDC": Unit("USDC", "usd", prefix="$"),
    "BTC": Unit("BTC", "btc", prefix="₿", decimals=8),
    "ETH": Unit("ETH", "eth", prefix="Ξ", decimals=6),
    "SATS": Unit("SATS", "sats", suffix=" sats", decimals=0),
}


def unit_for(name: str) -> Unit:
    """Look up a ``--unit`` value, case-insensitively."""
    try:
        return UNITS[name.upper()]
    except KeyError:
        raise ValueError(
            f"unknown unit {name!r}; choose from {', '.join(UNITS)}"
        ) from None


def parse_prices(payload: dict) -> dict[str, dict[str, float]]:
    """Extract ``{coin_id: {currency: price}}`` from a ``simple/price`` body
    (e.g. USD *and* EUR in one call). Malformed entries are skipped."""
    return {
        coin: {cur: float(price) for cur, price in v.items()}
        for coin, v in payload.items()
        if isinstance(v, dict)
    }


def market_out(amount_in: float, price_in: float, price_out: float) -> float:
    """Destination units a perfect (fee-less, slip-less) mid-price swap would yield.

    ``amount_in`` is in whole source units; prices are USD per whole unit.
    """
    if price_out <= 0:
        raise ValueError("non-positive destination price")
    return amount_in * price_in / price_out


def loss_vs_market_bps(quoted_out: float, market: float) -> float:
    """How far the quoted output falls below the market mid, in basis points.

    Positive = you receive less than market (the normal case: fees + slip +
    spread). Negative would mean the pool priced in your favour.
    """
    if market <= 0:
        return 0.0
    return (market - quoted_out) / market * 10_000


def loss_amount(quoted_out: float, market: float) -> float:
    """Destination units lost vs the market mid (negative = pool favoured you)."""
    return market - quoted_out


class PriceFeed(HttpClient):
    """Thin keyless client for CoinGecko spot prices."""

    source = SOURCE

    def __init__(
        self, base_url: str = DEFAULT_COINGECKO, timeout: float = 10.0
    ) -> None:
        super().__init__(timeout)
        self.base_url = base_url.rstrip("/")

    def spot(
        self, coin_ids: list[str], *, vs: tuple[str, ...] = ("usd",)
    ) -> dict[str, dict[str, float]]:
        """``{coin_id: {currency: price}}`` for ``coin_ids`` in each ``vs`` currency."""
        resp = self._get(
            f"{self.base_url}/simple/price",
            params={
                "ids": ",".join(sorted(set(coin_ids))),
                "vs_currencies": ",".join(vs),
            },
        )
        resp.raise_for_status()
        return parse_prices(resp.json())
