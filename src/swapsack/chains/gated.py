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
    DEFAULT_MAX_FEE,
    OP_RETURN_MAX_BYTES,
    ChainflipVaultPlan,
    SendPlan,
    SwapPlan,
    TxOutput,
    verify_btc_send,
    verify_btc_swap,
    verify_chainflip_vault_swap,
)


class _Built(Protocol):
    """The subset of a built (unsigned) tx the gate wrappers need."""

    outputs: list[TxOutput]
    fee: int


class GatedTxBuilder:
    """Mixin: build (via the ``build_unsigned_swap`` hook) then gate the result."""

    # Whether ``--allow-unconfirmed`` can do anything on this chain. False for
    # ZEC, whose lightwalletd address index holds mined outputs only — the CLI
    # says so rather than letting the flag look like it took effect.
    unconfirmed_spendable = True
    # The longest memo a deposit can carry: an OP_RETURN's, unless the chain
    # carries it elsewhere (ADA: transaction metadata).
    memo_max_bytes = OP_RETURN_MAX_BYTES
    # --max-fee when the user gives none; ADA, whose fees run ~0.2, overrides it.
    default_max_fee = DEFAULT_MAX_FEE
    # How the fee was priced, for a chain with no fee rate to print.
    fee_basis = ""

    def canonical_address(self, address: str) -> str:
        """The form of ``address`` the neutral output extraction decodes back.

        The gate compares the address re-extracted from the serialized bytes
        with the one it was asked to pay, so two spellings of one script (ZEC's
        ZIP-320 ``tex1…`` vs its ``t1…``) must be reduced to the extracted one
        first. Identity unless a chain has such aliases.
        """
        return address

    def display_address(self, address: str) -> str:
        """``address`` as the confirm prompt shows it: the canonical form plus
        any alias the operator may compare it against. Identity by default."""
        return address

    def cpfp_deficits(
        self,
        utxos: list[Utxo],
        fee_rate: float,  # noqa: ARG002
    ) -> list[Utxo]:
        """Fill in the CPFP surcharge for unconfirmed inputs; by default, none.

        Overridden by BTC, the one chain here with a real mempool fee market.
        DASH prices spends at a flat generous rate and has no replacement at
        all; ZEC never returns an unconfirmed UTXO to begin with. Neither has a
        parent to lift, so both keep this no-op — and the caller can price any
        chain's inputs without asking which it is holding.
        """
        return utxos

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
            inbound_address=self.canonical_address(quote.inbound_address),
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
            memo_max_bytes=self.memo_max_bytes,
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
            inbound_address=self.canonical_address(vault),
            amount=amount,
            memo=memo,
            expiry=now + 3600,
        )
        problems = verify_btc_swap(
            built.outputs,
            fee=built.fee,
            plan=plan,
            owned_addresses=owned,
            now=now,
            max_fee=max_fee,
            memo_max_bytes=self.memo_max_bytes,
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

    def build_and_verify_vault_swap(
        self,
        *,
        plan: ChainflipVaultPlan,
        now: int,
        mnemonic: str,
        scanned_utxos: list[Utxo],
        fee_rate: float,
        change_address: str,
        max_fee: int,
    ) -> Prepared:
        """Build + verify a Chainflip vault swap: pay a vault, say why in bytes.

        The transaction is the shape Chainflip requires and this builder already
        emits — vault output, nulldata OP_RETURN, change — where the change
        doubles as the swap's refund address, which is why it must be ours and
        why a sweep (no change) can never be a vault swap. The gate enforces
        that on the bytes rather than trusting this note: a selection that folds
        a sub-dust change into the fee reaches the same shape without a sweep.

        There is no ``sweep`` parameter for that reason, and the plan carries
        the amount so the gate and the builder cannot be given different ones.
        """
        built = self.build_unsigned_swap(
            mnemonic=mnemonic,
            utxos=scanned_utxos,
            vault_address=plan.deposit_address,
            amount=plan.amount,
            memo=plan.payload,
            fee_rate=fee_rate,
            change_address=change_address,
        )
        owned = {change_address} | {u.address for u in scanned_utxos}
        problems = verify_chainflip_vault_swap(
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
        plan = SendPlan(recipient=self.canonical_address(recipient), amount=amount)
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
