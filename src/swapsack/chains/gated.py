"""The build-then-gate wrappers shared by every UTXO-family adapter.

BTC and DASH build via bitcoinlib (:class:`~swapsack.chains.utxo.UtxoTxBuilder`);
ZEC builds via its bespoke v4/ZIP-243 signer
(:class:`~swapsack.chains.zcash.ZecAdapter`). Both plug their chain-specific
construction into the :meth:`build_unsigned_swap` hook; these wrappers add the
money-safety gate (:mod:`swapsack.verify`) and the ``SwapPlan``/``SendPlan``
uniformly — so a gate-wiring fix lives in exactly one place instead of being
copied per chain (where missing a copy silently weakens that chain's gate).

The hook returns anything with neutral ``outputs`` and ``fee`` (bitcoinlib's
``BuiltSwap`` or zcash's ``ZecBuilt``); that object is carried on the returned
:class:`~swapsack.swap.Prepared` for the caller to ``sign``.
"""

from __future__ import annotations

from typing import Protocol

from swapsack.chains.coins import Utxo
from swapsack.swap import Prepared, SwapRequest
from swapsack.thorchain import Quote
from swapsack.verify import (
    SendPlan,
    SwapPlan,
    TxOutput,
    verify_btc_send,
    verify_btc_swap,
)


class _Built(Protocol):
    """The subset of a built (unsigned) tx the gate wrappers need."""

    outputs: list[TxOutput]
    fee: int


class GatedTxBuilder:
    """Mixin: build (via the ``build_unsigned_swap`` hook) then gate the result."""

    def build_unsigned_swap(
        self,
        *,
        mnemonic: str,
        utxos: list[Utxo],
        vault_address: str,
        amount: int,
        memo: str | bytes | None,
        fee_rate: float,
        change_address: str,
        sweep: bool = False,
    ) -> _Built:
        """Construct the unsigned tx paying ``amount`` to ``vault_address``.

        ``memo`` of ``None`` omits the OP_RETURN (plain send); a ``str`` is a
        THORChain/Maya memo and ``bytes`` a binary payload (a Chainflip vault
        swap), either carried as the single OP_RETURN. Chain-specific —
        overridden by each adapter.
        """
        raise NotImplementedError

    def build_and_verify(
        self,
        *,
        quote: Quote,
        request: SwapRequest,
        now: int,
        mnemonic: str,
        scanned_utxos: list[Utxo],
        fee_rate: float,
        change_address: str,
        max_fee: int,
        sweep: bool = False,
    ) -> Prepared:
        built = self.build_unsigned_swap(
            mnemonic=mnemonic,
            utxos=scanned_utxos,
            vault_address=quote.inbound_address,
            amount=request.amount,
            memo=quote.memo or "",
            fee_rate=fee_rate,
            change_address=change_address,
            sweep=sweep,
        )
        owned = {change_address} | {u.address for u in scanned_utxos}
        plan = SwapPlan(
            inbound_address=quote.inbound_address,
            amount=request.amount,
            memo=quote.memo or "",
            expiry=quote.expiry,
            destination=request.destination,
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

    def build_and_verify_deposit(
        self,
        *,
        vault: str,
        memo: str | bytes,
        amount: int,
        now: int,
        mnemonic: str,
        scanned_utxos: list[Utxo],
        fee_rate: float,
        change_address: str,
        max_fee: int,
        sweep: bool = False,
    ) -> Prepared:
        built = self.build_unsigned_swap(
            mnemonic=mnemonic,
            utxos=scanned_utxos,
            vault_address=vault,
            amount=amount,
            memo=memo,
            fee_rate=fee_rate,
            change_address=change_address,
            sweep=sweep,
        )
        owned = {change_address} | {u.address for u in scanned_utxos}
        plan = SwapPlan(
            inbound_address=vault, amount=amount, memo=memo, expiry=now + 3600
        )
        problems = verify_btc_swap(
            built.outputs,
            fee=built.fee,
            plan=plan,
            owned_addresses=owned,
            now=now,
            max_fee=max_fee,
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

    def build_and_verify_send(
        self,
        *,
        recipient: str,
        amount: int,
        now: int,  # noqa: ARG002 (kept for a uniform build_and_verify_* signature)
        mnemonic: str,
        scanned_utxos: list[Utxo],
        fee_rate: float,
        change_address: str,
        max_fee: int,
        sweep: bool = False,
    ) -> Prepared:
        """Build + verify a plain send (no swap, no memo) to ``recipient``."""
        built = self.build_unsigned_swap(
            mnemonic=mnemonic,
            utxos=scanned_utxos,
            vault_address=recipient,
            amount=amount,
            memo=None,
            fee_rate=fee_rate,
            change_address=change_address,
            sweep=sweep,
        )
        owned = {change_address} | {u.address for u in scanned_utxos}
        plan = SendPlan(recipient=recipient, amount=amount)
        # The gate is chain-agnostic (pure output/value/dust checks) despite
        # the historical btc name.
        problems = verify_btc_send(
            built.outputs,
            fee=built.fee,
            plan=plan,
            owned_addresses=owned,
            max_fee=max_fee,
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)
