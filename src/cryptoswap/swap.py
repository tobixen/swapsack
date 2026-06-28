"""Swap orchestration: quote -> build -> verify -> (confirm) -> sign -> broadcast.

The orchestrator depends only on small protocols, so it can be tested with fakes
and is decoupled from bitcoinlib. It is currently BTC-source specific: amounts
are THORChain 1e8 base units, which for BTC.BTC equal satoshis directly. An
account-based source chain (ETH) will need decimal handling at this seam.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

from cryptoswap.chains.coins import Utxo
from cryptoswap.thorchain import ChainStatus, Quote
from cryptoswap.verify import SwapPlan, TxOutput, verify_btc_swap

DEFAULT_TOLERANCE_BPS = 300


class SwapAborted(RuntimeError):
    """Raised when a swap must not proceed (halted chain, too small, unsafe tx)."""


class BuiltSwapLike(Protocol):
    """The slice of a built swap the orchestrator needs (see chains.btc.BuiltSwap)."""

    outputs: list[TxOutput]
    fee: int
    change_address: str


class ThorchainLike(Protocol):
    def inbound_addresses(self) -> dict[str, ChainStatus]: ...

    def quote_swap(
        self,
        from_asset: str,
        to_asset: str,
        amount: int,
        destination: str | None = None,
        *,
        tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
    ) -> Quote: ...


class BtcSwapAdapter(Protocol):
    chain: str

    def build_unsigned_swap(
        self,
        *,
        mnemonic: str,
        utxos: list[Utxo],
        vault_address: str,
        amount: int,
        memo: str,
        fee_rate: float,
        change_address: str,
        sweep: bool = False,
    ) -> BuiltSwapLike: ...

    def sign(self, built: BuiltSwapLike) -> str: ...

    def broadcast(self, raw_hex: str) -> str: ...


@dataclasses.dataclass(frozen=True)
class SwapRequest:
    from_asset: str
    to_asset: str
    amount: int  # THORChain 1e8 base units of from_asset
    destination: str  # address on the destination chain


@dataclasses.dataclass(frozen=True)
class Prepared:
    quote: Quote
    built: BuiltSwapLike
    plan: SwapPlan
    problems: list[str]

    @property
    def safe(self) -> bool:
        return not self.problems


@dataclasses.dataclass(frozen=True)
class SwapResult:
    prepared: Prepared
    txid: str | None
    broadcast: bool


def prepare_btc_swap(
    *,
    thorchain: ThorchainLike,
    adapter: BtcSwapAdapter,
    mnemonic: str,
    request: SwapRequest,
    scanned_utxos: list[Utxo],
    fee_rate: float,
    change_address: str,
    now: int,
    max_fee: int,
    tolerance_bps: int = DEFAULT_TOLERANCE_BPS,
    sweep: bool = False,
) -> Prepared:
    """Quote, build the unsigned tx, and run it through the verify gate."""
    status = thorchain.inbound_addresses().get(adapter.chain)
    if status is None or not status.tradable:
        raise SwapAborted(f"{adapter.chain} is not currently tradable on THORChain")

    quote = thorchain.quote_swap(
        request.from_asset,
        request.to_asset,
        request.amount,
        request.destination,
        tolerance_bps=tolerance_bps,
    )
    if request.amount < quote.recommended_min_amount_in:
        raise SwapAborted(
            f"amount {request.amount} is below the recommended minimum "
            f"{quote.recommended_min_amount_in}; swap would be uneconomical"
        )
    if not quote.memo:
        raise SwapAborted("THORChain quote returned no memo (missing destination?)")

    built = adapter.build_unsigned_swap(
        mnemonic=mnemonic,
        utxos=scanned_utxos,
        vault_address=quote.inbound_address,
        amount=request.amount,
        memo=quote.memo,
        fee_rate=fee_rate,
        change_address=change_address,
        sweep=sweep,
    )
    owned = {change_address} | {u.address for u in scanned_utxos}
    plan = SwapPlan(
        inbound_address=quote.inbound_address,
        amount=request.amount,
        memo=quote.memo,
        expiry=quote.expiry,
    )
    problems = verify_btc_swap(
        built.outputs,
        fee=built.fee,
        plan=plan,
        owned_addresses=owned,
        now=now,
        max_fee=max_fee,
    )
    return Prepared(quote=quote, built=built, plan=plan, problems=problems)


def execute_swap(
    prepared: Prepared, adapter: BtcSwapAdapter, *, confirm: bool
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
    raw = adapter.sign(prepared.built)
    txid = adapter.broadcast(raw)
    return SwapResult(prepared=prepared, txid=txid, broadcast=True)
