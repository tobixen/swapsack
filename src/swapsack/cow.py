"""CoW Protocol backend: same-chain ETH-token swaps via a keyless intent API.

Where THORChain/Maya route a same-chain token pair (USDT-ETH <-> USDC-ETH)
through two cross-chain pool legs plus a flat outbound fee, CoW settles it in
one solver auction for a fraction of the cost (see docs/backends.md). The
execution model is an *intent*: instead of paying a vault with a memo, the
wallet signs a structured EIP-712 order (sellToken, buyToken, amounts,
receiver, validTo) and posts it to the orderbook — solvers settle it atomically
on-chain or it expires harmlessly. Every order field is bound by
:func:`swapsack.verify.verify_cow_order` before signing, exactly like a
``SendPlan``; that gateability is why CoW was chosen over calldata-style
aggregators.

Quoting and order submission are keyless REST (probed live 2026-07-11; a
signed order from an unfunded key passes every orderbook validation up to
``InsufficientBalance``). The only per-swap on-chain transaction is an exact-
amount ERC-20 approval to CoW's vault relayer when the current allowance is
short — built and gated by the ETH adapter.

Amounts cross this module in two units: the wallet-wide THORChain 1e8 base
units at the backend surface (so ``--backend auto`` can price-compare), and
each token's own native decimals inside quotes/orders (what the API and the
signed order speak).
"""

from __future__ import annotations

import dataclasses
import datetime
import os
from typing import TYPE_CHECKING, Any

from swapsack.net import HTTP_ERRORS, HttpClient
from swapsack.thorchain import THORCHAIN_UNIT, SwapFees

# The gate-side constants live in verify.py (which stays import-free); this is
# the builder side of the same contract, so share rather than restate them.
from swapsack.verify import COW_MAX_ORDER_VALIDITY, COW_ZERO_APP_DATA

MAX_ORDER_VALIDITY = COW_MAX_ORDER_VALIDITY
ZERO_APP_DATA = COW_ZERO_APP_DATA

if TYPE_CHECKING:
    from collections.abc import Mapping

DEFAULT_COW_API = "https://api.cow.fi/mainnet/api/v1"
# GPv2Settlement — the EIP-712 verifying contract every CoW order is signed for.
SETTLEMENT_CONTRACT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
# GPv2VaultRelayer — the only contract that ever needs an ERC-20 allowance; it
# pulls the sell token during settlement. NOT the settlement contract itself.
VAULT_RELAYER = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"
# The buy-side sentinel for native ETH: the settlement contract unwraps WETH and
# delivers ETH. (Native ETH as the *sell* side needs the on-chain eth-flow
# contract — not supported here; sell a token instead.)
NATIVE_ETH = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
# The signed order's buyAmount is a hard on-chain floor a solver must beat, so
# unlike THORChain's 300 bps quote tolerance this defaults tight: 50 bps below
# the quoted buy amount (solver competition normally fills well above it).
DEFAULT_COW_TOLERANCE_BPS = 50

# THORChain-style asset string -> (contract, decimals). Keys must match
# cli.ASSET values. ETH.ETH is buyable only (see NATIVE_ETH above); the
# ERC-20 entries are both sellable and buyable.
COW_ASSETS: dict[str, tuple[str, int]] = {
    "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7": (
        "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        6,
    ),
    "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48": (
        "0xA0b86991c6218B36c1d19D4a2e9Eb0cE3606eB48",
        6,
    ),
    "ETH.ETH": (NATIVE_ETH, 18),
}

ORDER_TYPE = [
    {"name": "sellToken", "type": "address"},
    {"name": "buyToken", "type": "address"},
    {"name": "receiver", "type": "address"},
    {"name": "sellAmount", "type": "uint256"},
    {"name": "buyAmount", "type": "uint256"},
    {"name": "validTo", "type": "uint32"},
    {"name": "appData", "type": "bytes32"},
    {"name": "feeAmount", "type": "uint256"},
    {"name": "kind", "type": "string"},
    {"name": "partiallyFillable", "type": "bool"},
    {"name": "sellTokenBalance", "type": "string"},
    {"name": "buyTokenBalance", "type": "string"},
]


class CowError(RuntimeError):
    """Raised when the CoW orderbook API returns an error response."""


@dataclasses.dataclass(frozen=True)
class CowQuote:
    """A parsed ``/quote`` response, normalized for cross-backend comparison.

    Native-decimal amounts (``sell_amount``/``fee_amount``/``buy_amount``)
    drive order building; ``expected_amount_out`` (1e8 units of the
    destination) and ``fees`` (destination-denominated, like a thornode quote)
    let ``best_quote`` and the shared cost display treat this like any other
    backend's quote. ``expiry`` is the *quote's* expiration (order validity is
    the separate ``valid_to``).
    """

    sell_token: str
    buy_token: str
    receiver: str
    sell_amount: int  # native sell-token units, after the fee was carved out
    fee_amount: int  # native sell-token units
    buy_amount: int  # native buy-token units (the estimate, not the floor)
    valid_to: int
    quote_id: int | None
    expiry: int
    verified: bool
    expected_amount_out: int  # 1e8 units of the destination asset
    fees: SwapFees
    raw: Mapping[str, Any]

    @property
    def sell_amount_total(self) -> int:
        """What actually leaves the wallet: quote sellAmount + fee (this equals
        the requested sellAmountBeforeFee — asserted by the verify gate against
        the user's own amount)."""
        return self.sell_amount + self.fee_amount


def _parse_expiration(stamp: str) -> int:
    """ISO ``2026-07-11T05:29:28.152309192Z`` -> epoch seconds (fraction
    dropped — the API uses nanoseconds, which fromisoformat can't parse)."""
    return int(
        datetime.datetime.fromisoformat(stamp.split(".")[0])
        .replace(tzinfo=datetime.UTC)
        .timestamp()
    )


def parse_cow_quote(
    payload: Mapping[str, Any], *, to_asset: str, buy_decimals: int
) -> CowQuote:
    """Parse a ``/quote`` response; raises :class:`CowError` on an error body."""
    if "errorType" in payload:
        raise CowError(
            f"{payload['errorType']}: {payload.get('description', '')}".strip(": ")
        )
    quote = payload["quote"]
    sell_amount = int(quote["sellAmount"])
    fee_amount = int(quote["feeAmount"])
    buy_amount = int(quote["buyAmount"])
    buy_unit = 10**buy_decimals
    expected_out = buy_amount * THORCHAIN_UNIT // buy_unit
    # The fee is charged in the *sell* token; SwapFees is destination-
    # denominated, so convert at the quote's own price (buy per sell) before
    # scaling to 1e8. total_bps is input-relative, like thornode's.
    fee_in_buy = fee_amount * buy_amount // sell_amount if sell_amount else 0
    fee_out = fee_in_buy * THORCHAIN_UNIT // buy_unit
    total_in = sell_amount + fee_amount
    fees = SwapFees(
        asset=to_asset,
        outbound=fee_out,
        affiliate=0,
        liquidity=0,
        total=fee_out,
        slippage_bps=0,
        total_bps=10000 * fee_amount // total_in if total_in else 0,
    )
    return CowQuote(
        sell_token=quote["sellToken"],
        buy_token=quote["buyToken"],
        receiver=quote.get("receiver", ""),
        sell_amount=sell_amount,
        fee_amount=fee_amount,
        buy_amount=buy_amount,
        valid_to=int(quote["validTo"]),
        quote_id=payload.get("id"),
        expiry=_parse_expiration(payload["expiration"]),
        verified=bool(payload.get("verified", False)),
        expected_amount_out=expected_out,
        fees=fees,
        raw=payload,
    )


def build_order(
    quote: CowQuote, *, tolerance_bps: int = DEFAULT_COW_TOLERANCE_BPS
) -> dict[str, Any]:
    """The order struct to sign and submit, from a parsed quote.

    Modern CoW rule: submitted orders must carry ``feeAmount: 0`` with the fee
    folded into ``sellAmount`` ("fee in price" — the orderbook rejects non-zero
    fees). ``buyAmount`` becomes the on-chain enforced floor: the quoted output
    minus ``tolerance_bps``. Numeric token amounts are strings (the API's JSON
    convention for uint256); ``validTo`` stays an int.
    """
    return {
        "sellToken": quote.sell_token,
        "buyToken": quote.buy_token,
        "receiver": quote.receiver,
        "sellAmount": str(quote.sell_amount_total),
        "buyAmount": str(quote.buy_amount * (10000 - tolerance_bps) // 10000),
        "validTo": quote.valid_to,
        "appData": ZERO_APP_DATA,
        "feeAmount": "0",
        "kind": "sell",
        "partiallyFillable": False,
        "sellTokenBalance": "erc20",
        "buyTokenBalance": "erc20",
    }


def order_typed_data(order: Mapping[str, Any]) -> dict[str, Any]:
    """The full EIP-712 message for ``order`` (domain ``Gnosis Protocol`` v2,
    verifying contract = the settlement contract). Kept separate from
    :func:`sign_order` so tests can cross-check the digest independently."""
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": ORDER_TYPE,
        },
        "primaryType": "Order",
        "domain": {
            "name": "Gnosis Protocol",
            "version": "v2",
            "chainId": 1,
            "verifyingContract": SETTLEMENT_CONTRACT,
        },
        "message": {
            **order,
            "sellAmount": int(order["sellAmount"]),
            "buyAmount": int(order["buyAmount"]),
            "feeAmount": int(order["feeAmount"]),
            "appData": bytes.fromhex(str(order["appData"])[2:]),
        },
    }


def sign_order(order: Mapping[str, Any], private_key: Any) -> str:  # noqa: ANN401
    """EIP-712-sign ``order``; returns the 65-byte ``r||s||v`` signature hex.

    eth-account is imported lazily so quoting (``--backend auto`` price
    comparison) never pays for the signing stack.
    """
    from eth_account import Account

    signed = Account.sign_typed_data(private_key, full_message=order_typed_data(order))
    return "0x" + signed.signature.hex().removeprefix("0x")


class CowClient(HttpClient):
    """Thin client for the CoW orderbook REST API (keyless)."""

    def __init__(self, base_url: str = DEFAULT_COW_API, timeout: float = 20.0) -> None:
        super().__init__(timeout)
        self.base_url = base_url.rstrip("/")

    def _json_or_error(self, resp: Any) -> Any:  # noqa: ANN401 (niquests.Response)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                resp.raise_for_status()
            else:
                raise CowError(
                    f"{payload.get('errorType', resp.status_code)}: "
                    f"{payload.get('description', resp.text)}"
                )
        return resp.json()

    def quote(
        self,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        *,
        from_address: str,
        receiver: str,
    ) -> dict[str, Any]:
        """A sell-order quote; ``sell_amount`` is native sell-token units
        *before* the fee (the response carves the fee out of it)."""
        resp = self._post(
            f"{self.base_url}/quote",
            json={
                "sellToken": sell_token,
                "buyToken": buy_token,
                "from": from_address,
                "receiver": receiver,
                "kind": "sell",
                "sellAmountBeforeFee": str(sell_amount),
            },
        )
        return self._json_or_error(resp)

    def submit_order(
        self,
        order: Mapping[str, Any],
        *,
        signature: str,
        from_address: str,
        quote_id: int | None = None,
    ) -> str:
        """POST the signed order; returns the order uid the API answers with."""
        payload: dict[str, Any] = {
            **order,
            "signingScheme": "eip712",
            "signature": signature,
            "from": from_address,
        }
        if quote_id is not None:
            payload["quoteId"] = quote_id
        resp = self._post(f"{self.base_url}/orders", json=payload)
        uid = self._json_or_error(resp)
        return str(uid)

    def order_status(self, uid: str) -> dict[str, Any]:
        """The order record for ``uid`` — includes ``status`` (open / fulfilled
        / cancelled / expired) and the executed amounts."""
        resp = self._get(f"{self.base_url}/orders/{uid}")
        return self._json_or_error(resp)


@dataclasses.dataclass(frozen=True)
class CowBackend:
    """The CoW orderbook as a swap backend next to thorchain/maya.

    ``executor`` tells the CLI this backend settles by *signed order*, not by
    paying a vault with a memo — quotes price-compete in ``gather_quotes``, but
    execution dispatches to the CoW path.
    """

    client: CowClient
    name: str = "cow"

    executor = "signed-order"

    def serves(self, from_asset: str, to_asset: str) -> bool:
        """Same-chain ETH pairs only: sell an ERC-20, buy an ERC-20 or native
        ETH (selling native ETH would need the on-chain eth-flow contract)."""
        return (
            from_asset in COW_ASSETS
            and from_asset != "ETH.ETH"
            and to_asset in COW_ASSETS
            and to_asset != from_asset
        )

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
    ) -> CowQuote | None:
        """Quote for the shared ``gather_quotes`` surface; None = can't serve.

        ``amount`` arrives in the wallet-wide 1e8 units and is scaled to the
        sell token's native decimals. ``tolerance_bps`` only shapes the *order*
        floor later, and streaming is a thornode concept — a streaming request
        simply rules this backend out rather than silently ignoring the flag.
        ``destination`` doubles as the quote's ``from`` (the API wants one for
        fee estimation; balances are only checked at order submission).
        """
        if streaming_interval is not None or destination is None:
            return None
        if not self.serves(from_asset, to_asset):
            return None
        sell_contract, sell_decimals = COW_ASSETS[from_asset]
        buy_contract, buy_decimals = COW_ASSETS[to_asset]
        sell_amount = amount * 10**sell_decimals // THORCHAIN_UNIT
        if sell_amount <= 0:
            return None
        try:
            payload = self.client.quote(
                sell_contract,
                buy_contract,
                sell_amount,
                from_address=destination,
                receiver=destination,
            )
            return parse_cow_quote(
                payload, to_asset=to_asset, buy_decimals=buy_decimals
            )
        except (CowError, *HTTP_ERRORS):
            return None


def default_cow_backend() -> CowBackend:
    base_url = os.environ.get("SWAPSACK_COW_API") or DEFAULT_COW_API
    return CowBackend(CowClient(base_url))
