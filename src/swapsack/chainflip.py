"""Chainflip backend: a second independent cross-chain venue, quoting keylessly.

THORChain and Maya share a codebase and a failure mode — on 2026-08-18 they were
both halted at once, leaving BTC->ETH with no route at all (see
``docs/halt-alternatives.md``). Chainflip is a separate protocol with its own
validators and pools, so pricing against it is both a price check and the
resilience hedge ``--backend auto`` was meant to provide.

The REST quote is keyless, so pricing needs no account and no key. Execution is
a *vault swap* — a plain Bitcoin transaction paying a protocol vault with the
swap parameters in an OP_RETURN, no broker and no deposit channel — which is
what the ``vault-swap`` executor means, and why only a UTXO source path can
drive this backend. The destination is *encoded* rather than registered, so the
gate re-derives it from the payload's own bytes; only the assets
:data:`VAULT_SWAP_ASSET_IDS` can encode are executable, which
:meth:`ChainflipBackend.can_execute` says at selection time. See
``docs/chainflip-effort.md``.

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
from swapsack.verify import (
    VAULT_SWAP_PAYLOAD_BYTES,
    decode_vault_swap_payload,
)

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


class NoBrokerAvailable(ChainflipError):
    """Every broker refused to encode on the terms this wallet asks for.

    Separate from a plain :class:`ChainflipError` so that "we recognised a
    refusal and ran out of accounts" can be told from "the chain said something
    we do not understand" — a distinction the live tests lean on, since a
    refusal the wallet stops recognising is a fallback that has silently died.
    """


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


@dataclasses.dataclass(frozen=True)
class SwapStatus:
    """What Chainflip made of one deposit, in the protocol's own terms.

    Deliberately thin: ``state`` is carried through verbatim rather than mapped
    onto a local vocabulary, because Chainflip owns that vocabulary and may
    extend it — printing a word this wallet does not recognise is honest, while
    folding it into a guessed category is not.
    """

    state: str
    swap_id: str
    src_chain: str
    src_asset: str
    dest_chain: str
    dest_asset: str
    dest_address: str
    deposit_amount: int  # base units of the source asset
    deposit_txid: str
    # None until the payout leg exists — "not yet", which is not the same as 0.
    output_amount: int | None = None
    egress_txid: str = ""  # the payout transaction, once it has been sent
    witnessed_at: int | None = None  # ms since the epoch, as Chainflip dates it
    # Set only when the swap did not fill: the price never cleared the
    # encoded floor before the retry window ran out, so fill-or-kill sent the
    # deposit back rather than executing at a worse one. Chainflip's own word
    # for why, e.g. "MinPriceViolation" — same reasoning as `state` above.
    aborted_reason: str = ""
    # The refund leg, in *source*-asset base units on the *source* chain — it
    # never became the destination asset, so it is not `output_amount`. None
    # until the leg is witnessed, which can be after `aborted_reason` already
    # names the refund as decided.
    refund_amount: int | None = None
    refund_txid: str = ""

    @property
    def settled(self) -> bool:
        return self.state.upper() == "COMPLETED"

    @property
    def aborted(self) -> bool:
        """True once Chainflip has given up trying to fill this swap.

        Not the same as :attr:`refunded`: an abort can end with the deposit
        refunded, forfeited outright (below the chain's minimum, say, where a
        refund would cost more in fees than the deposit is worth), or — in
        the window between the decision and the refund transaction being
        witnessed — neither yet.
        """
        return bool(self.aborted_reason)

    @property
    def refunded(self) -> bool:
        """True once the refund leg itself exists — money has actually moved.

        Deliberately independent of :attr:`aborted`: what proves a refund
        happened is the refund leg's own fields, not the reason string that
        (usually) triggers it. A response where the two drift — a renamed or
        relocated reason field, ``refundEgress`` untouched — must not
        silently regress this to "not refunded", which is the shape of the
        bug this property exists to close.
        """
        return self.refund_amount is not None or bool(self.refund_txid)


def _int_or_none(value: object) -> int | None:
    """Chainflip sends base-unit amounts as decimal *strings*."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def parse_swap_status(payload: dict) -> SwapStatus:
    """Parse a ``/v2/swaps/<txid>`` body. Pure, like the quote parsers above.

    Every leg after the deposit is absent while a swap is in flight, so each is
    read defensively: an in-flight swap must read as "not paid out yet", never
    as a payout of zero.
    """
    deposit = payload.get("deposit") or {}
    egress = payload.get("swapEgress") or {}
    refund = payload.get("refundEgress") or {}
    return SwapStatus(
        state=str(payload.get("state", "") or ""),
        swap_id=str(payload.get("swapId", "") or ""),
        src_chain=str(payload.get("srcChain", "") or ""),
        src_asset=str(payload.get("srcAsset", "") or ""),
        dest_chain=str(payload.get("destChain", "") or ""),
        dest_asset=str(payload.get("destAsset", "") or ""),
        dest_address=str(payload.get("destAddress", "") or ""),
        deposit_amount=_int_or_none(deposit.get("amount")) or 0,
        deposit_txid=str(deposit.get("txRef", "") or ""),
        # The *egress* amount, not the swap's output amount: the difference is
        # the outbound fee, and what left the protocol is what the user got.
        output_amount=_int_or_none(egress.get("amount")),
        egress_txid=str(egress.get("txRef", "") or ""),
        witnessed_at=_int_or_none(deposit.get("witnessedAt")),
        aborted_reason=str(payload.get("abortedReason", "") or ""),
        refund_amount=_int_or_none(refund.get("amount")),
        refund_txid=str(refund.get("txRef", "") or ""),
    )


def asset_decimals(chain: str, asset: str) -> int | None:
    """Decimals for a Chainflip ``(chain, asset)`` pair, or ``None`` if unknown.

    Reverses :data:`CHAINFLIP_ASSETS`. Chainflip trades assets this wallet has
    no key for (SOL, DOT, …), and a swap *to* one of those is perfectly normal —
    so an unknown pair yields ``None`` and the caller prints base units rather
    than scaling by a made-up power of ten.
    """
    for cf_chain, cf_asset, decimals in CHAINFLIP_ASSETS.values():
        if (cf_chain, cf_asset) == (chain, asset):
            return decimals
    return None


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

    def swap_status(self, txid: str) -> SwapStatus | None:
        """The protocol's view of the swap a transaction paid for, or ``None``.

        Keyed by the **deposit transaction id**, which is the only handle a
        vault swap leaves behind: there is no deposit channel and no order id,
        just the transaction the wallet broadcast. A txid Chainflip never
        witnessed answers 404, which is information ("not one of ours"), not a
        failure — so it comes back as ``None`` rather than raising.
        """
        resp = self._get(f"{self.base_url}/swaps/{txid}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ChainflipError(f"malformed swap status for {txid}")
        return parse_swap_status(payload)


@dataclasses.dataclass(frozen=True)
class ChainflipBackend:
    """Chainflip as a swap backend next to thorchain/maya/cow.

    ``executor`` says this backend settles by paying a protocol vault with the
    swap parameters encoded in the transaction — *not* by the thornode ``=:``
    memo the CLI's deposit path builds. That makes it drivable only from a UTXO
    source (``UTXO_EXECUTORS``) and only to a destination the payload can encode
    (:meth:`can_execute`); everything else it serves still price-competes in
    ``gather_quotes``.
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

    def can_execute(self, from_asset: str, to_asset: str) -> bool:
        """Whether a *vault swap* can settle this pair, not merely price it.

        Narrower than :meth:`serves` on purpose. The destination is encoded into
        the OP_RETURN payload and re-derived by the gate, so only the assets
        :data:`VAULT_SWAP_ASSET_IDS` can encode are settleable here — Tron is
        listed and quotable but needs a base58check decoder the gate does not
        have. Saying so at selection time routes such a pair to a backend that
        can run it, instead of aborting in ``destination_bytes`` after the
        quotes are in.
        """
        return self.serves(from_asset, to_asset) and to_asset in VAULT_SWAP_ASSET_IDS

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


# --- vault swaps (execution) ------------------------------------------------
#
# Chainflip can be paid two ways. A *deposit channel* has a broker register your
# destination, which you then have to trust or read back. A **vault swap** puts
# the swap parameters in your own transaction's OP_RETURN and pays a protocol
# vault directly: no broker, no channel, no expiry race — and the destination is
# something we encode rather than something we are told. That is why this is the
# path built here; ``docs/chainflip-effort.md`` has the reasoning and the live
# probes behind it.
#
# The Bitcoin transaction shape Chainflip requires — pay the vault, the nulldata
# OP_RETURN, then change (which doubles as the refund address) — is exactly what
# ``UtxoTxBuilder.build_unsigned_swap`` already emits for THORChain.

CHAINFLIP_RPC = "https://mainnet-rpc.chainflip.io"

# Chainflip's output-asset ids as they appear in a vault-swap payload, verified
# by differential encoding against mainnet on 2026-08-28. Only assets whose
# address the gate can independently reproduce are listed: these all encode as
# 20 raw bytes. Tron does too, but needs a base58check decoder we do not have;
# Solana's is 32 bytes and changes the payload length. Both are refused rather
# than trusted — see destination_bytes.
VAULT_SWAP_ASSET_IDS: dict[str, int] = {
    "ETH.ETH": 1,
    "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48": 3,
    "ARB.ETH": 6,
    "ARB.USDC-0XAF88D065E77C8CC2239327C5EDB3A432268E5831": 7,
    "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7": 8,
}

# The reverse lookup, built once rather than re-built by every caller that
# needs to go from a decoded payload's numeric asset id back to our asset
# string (``status``, reading a deposit it did not build).
VAULT_SWAP_ASSETS_BY_ID: dict[int, str] = {
    asset_id: asset for asset, asset_id in VAULT_SWAP_ASSET_IDS.items()
}

# Blocks Chainflip keeps retrying a swap whose price never clears the floor
# before refunding to the change output. The chain caps this
# (max_swap_retry_duration_blocks, 600 at the time of writing) and rejects more.
DEFAULT_RETRY_DURATION_BLOCKS = 100

# The encode RPC wants a broker account. It is **inert for what we broadcast**:
# with a zero commission the payload is byte-identical whichever account is
# named (checked against all five below on 2026-08-31), and the account only
# selects which of the protocol's published vault addresses to pay — every one
# of which the gate confirms against cf_get_vault_addresses.
#
# What it is *not* is free of the chain: the id is a constant here, but whether
# the account named will encode for us is on-chain state that can change under
# us, and did. That is the difference between this and a service we call — no
# host has to be up for a vault swap to be built, but a usable broker has to
# exist, which is why there is a list and a live test watching it.
#
# It is a *list* because a broker can set a minimum commission it will encode
# for, and a broker demanding one is a broker this wallet cannot use: a
# commission is a skim verify.verify_chainflip_vault_swap refuses. That is not
# hypothetical — the single account hardcoded here until 2026-08-31
# ("Broker as a Service") started enforcing 5 bps and broke every vault swap.
# The chain exposes no way to read a broker's minimum (cf_account_info does not
# carry one), so the only way to find a usable broker is to ask and read the
# rejection, which is what prepare_vault_swap does.
#
# Only a broker with a private Bitcoin channel can encode a vault swap at all:
# of the 134 accounts cf_all_account_infos listed as brokers on 2026-08-31, 128
# answered NoPrivateChannelExistsForBroker, one demanded a commission, and these
# five encoded at zero. Named accounts come first as the likelier to be
# long-lived.
DEFAULT_BROKER_ACCOUNTS = (
    "cFLRQDfEdmnv6d2XfHJNRBQHi4fruPMReLSfvB8WWD2ENbqj7",  # Chainflip SDK
    "cFNx21kQWmr9wsqq29zWM7RpDBKv4bctudEUE6J22Hd4NUUHR",  # Rango
    "cFL4To8Uow6B1hk4dNrhWhvKpkBtnUTrVdWCEKCaXiXMMztjM",  # sk-dev
    "cFKpid38PmmZ8V81AHaZAhHzzpRbsf7Xw5PYt5ajTXAUvHoTQ",
    "cFNwtr2mPhpUEB5AyJq38DqMKMkSdzaL9548hajN2DRTwh7Mq",
)

# Rejections that mean "this broker will not encode for us", as opposed to
# "this swap is wrong". Both arrive as a DispatchError with nothing but its text
# to tell them apart, and the distinction matters: a bad swap retried at every
# broker turns one clear error into five requests and a misleading summary.
BROKER_REFUSALS = (
    "Broker commission is too low",
    "NoPrivateChannelExistsForBroker",
)

# A u8 in the payload; 255 is what the protocol encodes when it is not asked
# for. Its units are documented as basis points, which a u8 cannot express past
# 2.55%, so rather than set a number whose meaning we are unsure of we leave the
# protocol default and rely on min_output_amount — a floor we compute, encode
# and gate ourselves. Noted so the next reader knows this was a decision.
UNSET_ORACLE_SLIPPAGE = 255

# How long a prepared vault swap stays valid, in seconds. Chainflip itself gives
# a vault swap two epoch rotations (~3-6 days), so this is not the protocol's
# deadline — it is ours: the encoded floor comes from a price quoted now, and a
# plan left sitting at a confirmation prompt should be re-quoted rather than
# broadcast against a stale one.
VAULT_SWAP_PLAN_TTL = 600


@dataclasses.dataclass(frozen=True)
class VaultSwap:
    """An unsigned Chainflip vault swap: where to pay and what to say.

    Everything the gate needs to re-derive its checks travels with it, so the
    CLI never has to reconstruct an intention from two places.
    """

    deposit_address: str
    payload: bytes
    known_vaults: frozenset[str]
    destination_asset_id: int
    destination_bytes: bytes
    min_output_amount: int


def destination_bytes(asset: str, address: str) -> bytes:
    """The 20 raw address bytes a vault-swap payload carries for ``asset``.

    Raises for an asset whose address this cannot reproduce: the gate proves the
    payload pays us by comparing these bytes, so an asset it cannot encode is an
    asset it cannot verify — and an unverifiable swap must not be built.
    """
    if asset not in VAULT_SWAP_ASSET_IDS:
        raise ChainflipError(
            f"cannot verify a Chainflip vault swap paying {asset}: the gate "
            f"decodes the payload's destination itself, and only "
            f"{', '.join(sorted(VAULT_SWAP_ASSET_IDS))} are supported"
        )
    if not address.lower().startswith("0x"):
        raise ChainflipError(f"expected an 0x… address for {asset}, got {address!r}")
    try:
        raw = bytes.fromhex(address[2:])
    except ValueError as exc:
        raise ChainflipError(f"destination {address!r} is not hex: {exc}") from exc
    if len(raw) != 20:
        raise ChainflipError(
            f"destination {address!r} is {len(raw)} bytes, expected 20"
        )
    return raw


def min_output_amount(quote: ChainflipQuote, bps: int | None) -> int:
    """The on-chain floor to encode: the quote less ``bps`` of tolerance.

    This is the vault swap's equivalent of CoW's ``buyAmount`` — a number the
    protocol enforces, not a hint. ``None`` takes the quote's own
    ``recommendedSlippageTolerancePercent``, which is the right default because
    a Bitcoin deposit waits ~15 minutes for confirmations and the price moves
    in that window; a CoW-style 50 bps floor would simply refund most swaps.
    """
    if bps is None:
        bps = quote.recommended_slippage_bps
    if not 0 <= bps < 10000:
        raise ChainflipError(f"tolerance {bps} bps must be >= 0 and < 10000")
    return quote.egress_amount * (10000 - bps) // 10000


class ChainflipRpc(HttpClient):
    """The Chainflip State Chain's public JSON-RPC (keyless).

    Separate from :class:`ChainflipClient`: that one talks to the hosted
    swapping service for prices, this one talks to the protocol's own nodes for
    the things a money path must not take on a service's word — the vault
    addresses and the parameter encoding.
    """

    def __init__(self, base_url: str = CHAINFLIP_RPC, timeout: float = 25.0) -> None:
        super().__init__(timeout)
        self.base_url = base_url.rstrip("/")

    def call(self, method: str, params: list[Any]) -> Any:  # noqa: ANN401 (JSON)
        resp = self._post(
            self.base_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        if resp.status_code >= 400:
            resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ChainflipError(f"malformed RPC response for {method}")
        if "error" in payload:
            error = payload["error"]
            raise ChainflipError(
                f"{method}: {error.get('message', error)}"
                if isinstance(error, dict)
                else f"{method}: {error}"
            )
        if "result" not in payload:
            raise ChainflipError(f"RPC response for {method} carries no result")
        return payload["result"]


def bitcoin_vault_addresses(rpc: ChainflipRpc) -> frozenset[str]:
    """The Bitcoin vault addresses the protocol publishes on-chain.

    The chain answers with ``(account id, {"Btc": [byte, …]})`` pairs, where the
    byte array is the address string's own ASCII. This is the independent side
    of the gate's vault check: the deposit address an encoding hands back has to
    appear here, or we are not paying the protocol.
    """
    result = rpc.call("cf_get_vault_addresses", [])
    try:
        entries = result["bitcoin"]
        return frozenset(
            bytes(address["Btc"]).decode() for _account, address in entries
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ChainflipError(f"malformed vault address list: {exc!r}") from exc


def _encode_params(
    *,
    account: str,
    from_asset: str,
    to_asset: str,
    destination: str,
    floor: int,
) -> list[Any]:
    """The parameter list cf_request_swap_parameter_encoding takes.

    One place, because the live test asks each broker in turn through this same
    builder: a test that hand-rolled the list could keep passing while the
    wallet sent something else.
    """
    return [
        account,
        dict(zip(("chain", "asset"), CHAINFLIP_ASSETS[from_asset][:2], strict=True)),
        dict(zip(("chain", "asset"), CHAINFLIP_ASSETS[to_asset][:2], strict=True)),
        destination,
        0,  # broker commission: nobody skims a swap this wallet builds
        {
            "chain": "Bitcoin",
            "min_output_amount": hex(floor),
            "retry_duration": DEFAULT_RETRY_DURATION_BLOCKS,
        },
    ]


def _request_encoding(
    rpc: ChainflipRpc,
    *,
    from_asset: str,
    to_asset: str,
    destination: str,
    floor: int,
    accounts: Sequence[str] = DEFAULT_BROKER_ACCOUNTS,
) -> Any:  # noqa: ANN401 (JSON)
    """Ask the brokers in turn to encode the swap; return the first answer.

    The commission is zero at every attempt and never escalates: a commission
    is a skim the gate refuses, so asking for one would only build a
    transaction this wallet's own gate throws away. A broker that will not
    encode on those terms (:data:`BROKER_REFUSALS`) is skipped for the next;
    any other error is about the *swap* and is raised as it comes, unretried.

    ``accounts`` exists for the live tests, which ask one named broker at a
    time; nothing in the wallet passes it.
    """
    refusals = []
    for account in accounts:
        try:
            return rpc.call(
                "cf_request_swap_parameter_encoding",
                _encode_params(
                    account=account,
                    from_asset=from_asset,
                    to_asset=to_asset,
                    destination=destination,
                    floor=floor,
                ),
            )
        except ChainflipError as exc:
            if not any(refusal in str(exc) for refusal in BROKER_REFUSALS):
                raise
            refusals.append(f"{account} ({exc})")
    listed = "; ".join(refusals)
    # No diagnosis of our own: a broker refuses either because it wants a
    # commission or because it has no private channel, and telling a user the
    # wrong one of those sends them looking for the wrong thing. Each account's
    # own words, and what to do about it.
    raise NoBrokerAvailable(
        f"no Chainflip broker would encode this vault swap — the wallet asks "
        f"every broker for a zero commission, and each of these refused: "
        f"{listed}. Swap via another backend, or report this: the account list "
        f"may need widening, or paying a commission may need to become an "
        f"explicit, disclosed option"
    )


def prepare_vault_swap(
    rpc: ChainflipRpc,
    *,
    from_asset: str,
    to_asset: str,
    destination: str,
    quote: ChainflipQuote,
    bps: int | None,
) -> VaultSwap:
    """Ask the chain to encode a vault swap, then check it before returning it.

    Every check here is repeated by :func:`swapsack.verify.
    verify_chainflip_vault_swap` against the built transaction — this is not the
    gate. It fails early so the user gets one clear sentence instead of a gate
    problem list, and so a bad encoding never reaches a transaction builder.
    """
    if from_asset != "BTC.BTC":
        raise ChainflipError(
            f"vault swaps are implemented for a Bitcoin source only, not "
            f"{from_asset} (an EVM source needs a contract call, not this path)"
        )
    expected_dest = destination_bytes(to_asset, destination)
    asset_id = VAULT_SWAP_ASSET_IDS[to_asset]
    floor = min_output_amount(quote, bps)
    vaults = bitcoin_vault_addresses(rpc)
    result = _request_encoding(
        rpc,
        from_asset=from_asset,
        to_asset=to_asset,
        destination=destination,
        floor=floor,
    )
    try:
        deposit_address = str(result["deposit_address"])
        raw = str(result["nulldata_payload"])
        payload = bytes.fromhex(raw[2:] if raw.lower().startswith("0x") else raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ChainflipError(f"malformed vault swap encoding: {exc!r}") from exc

    if deposit_address not in vaults:
        raise ChainflipError(
            f"the encoding names deposit address {deposit_address}, which is "
            f"not one of the protocol vaults published on-chain"
        )
    decoded = decode_vault_swap_payload(payload)
    if decoded is None:
        raise ChainflipError(
            f"vault swap payload is {len(payload)} bytes, expected "
            f"{VAULT_SWAP_PAYLOAD_BYTES}"
        )
    if decoded.destination != expected_dest:
        raise ChainflipError(
            f"the encoding pays destination 0x{decoded.destination.hex()}, "
            f"not {destination}"
        )
    if decoded.asset_id != asset_id:
        raise ChainflipError(
            f"the encoding pays output asset {decoded.asset_id}, not "
            f"{asset_id} ({to_asset})"
        )
    if decoded.min_output_amount < floor:
        raise ChainflipError(
            f"the encoding's min output {decoded.min_output_amount} is below "
            f"the floor {floor} we asked for"
        )
    # The encoding was asked for a zero commission, so a payload carrying a fee
    # anyway is not the swap we asked for. The gate refuses it too — this is the
    # layer that says so in one sentence, and it is also what stops a broker who
    # answers with a fee instead of an error from ending the fallback silently.
    for fee, label in (
        (decoded.broker_fee, "broker fee"),
        (decoded.boost_fee, "boost fee"),
        (decoded.affiliates, "affiliate fee entries"),
    ):
        if fee:
            raise ChainflipError(
                f"the encoding carries {fee} {label}; a zero commission was "
                f"asked for, and a swap this wallet builds pays no skim"
            )
    return VaultSwap(
        deposit_address=deposit_address,
        payload=payload,
        known_vaults=vaults,
        destination_asset_id=asset_id,
        destination_bytes=expected_dest,
        min_output_amount=floor,
    )
