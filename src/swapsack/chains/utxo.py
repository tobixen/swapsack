"""Shared bitcoinlib-backed transaction construction for UTXO chains.

BTC (native segwit) and DASH (legacy P2PKH) build, gate and sign their spends
through this one code path — the differences are collapsed into three knobs:
the bitcoinlib ``network`` name, the ``witness_type``, and the
:class:`~swapsack.chains.coins.ScriptParams` driving the fee/dust maths.
Building is deliberately split from signing: ``build_unsigned_swap`` returns
the tx together with neutral outputs for the :mod:`swapsack.verify` gate, and
only after that gate passes should the caller ``sign`` and broadcast.

Current limitation (as before the extraction): signing assumes each selected
UTXO carries its own derivation ``path``; inputs with no path fall back to
``default_path``.
"""

from __future__ import annotations

import dataclasses

from bitcoinlib.keys import HDKey
from bitcoinlib.mnemonic import Mnemonic
from bitcoinlib.transactions import Transaction

from swapsack.chains.coins import (
    P2WPKH,
    InsufficientFunds,
    ScriptParams,
    Utxo,
    decode_op_return,
    encode_op_return,
    select_coins,
)
from swapsack.swap import Prepared, SwapRequest
from swapsack.thorchain import Quote
from swapsack.verify import (
    SendPlan,
    SwapPlan,
    TxOutput,
    verify_btc_send,
    verify_btc_swap,
)


@dataclasses.dataclass
class BuiltSwap:
    tx: Transaction
    outputs: list[TxOutput]
    fee: int
    change_address: str
    keys: list[HDKey] = dataclasses.field(default_factory=list)


def extract_outputs(tx: Transaction) -> list[TxOutput]:
    outputs: list[TxOutput] = []
    for o in tx.outputs:
        if o.script_type == "nulldata":
            outputs.append(
                TxOutput(
                    address=None,
                    value=o.value,
                    op_return_data=decode_op_return(bytes(o.lock_script)),
                )
            )
        else:
            outputs.append(TxOutput(address=o.address, value=o.value))
    return outputs


class UtxoTxBuilder:
    """Mixin: build + gate + sign UTXO transactions via bitcoinlib.

    Expects the adapter to provide ``self.network`` (bitcoinlib network name),
    ``self.bip39_passphrase`` and ``derive_address``; per-chain class attrs
    below pick the script flavour.
    """

    witness_type = "segwit"
    script: ScriptParams = P2WPKH
    default_derivation: str

    def _hdkey(self, mnemonic: str, path: str) -> HDKey:
        seed = Mnemonic().to_seed(mnemonic, self.bip39_passphrase)
        return HDKey.from_seed(seed, network=self.network).key_for_path(path)

    def build_unsigned_swap(
        self,
        *,
        mnemonic: str,
        utxos: list[Utxo],
        vault_address: str,
        amount: int,
        memo: str | None,
        fee_rate: float,
        change_address: str | None = None,
        default_path: str | None = None,
        sweep: bool = False,
    ) -> BuiltSwap:
        """Build the unsigned tx paying ``amount`` to ``vault_address``.

        ``memo`` of ``None`` omits the OP_RETURN output entirely — used for a
        plain send (no swap). Any other value is encoded as the single OP_RETURN.
        """
        default_path = default_path or self.default_derivation
        change_address = change_address or self.derive_address(mnemonic, default_path)
        memo_bytes = memo.encode() if memo is not None else b""
        if sweep:
            # Spend everything: fee is whatever is left over the vault output.
            chosen = list(utxos)
            fee = sum(u.value for u in chosen) - amount
            change = 0
            if fee < 0:
                raise InsufficientFunds(f"amount {amount} exceeds balance")
        else:
            sel = select_coins(
                utxos, amount, fee_rate, len(memo_bytes), script=self.script
            )
            chosen, fee, change = sel.utxos, sel.fee, sel.change

        tx = Transaction(network=self.network, witness_type=self.witness_type)
        keys: list[HDKey] = []
        for utxo in chosen:
            key = self._hdkey(mnemonic, utxo.path or default_path)
            tx.add_input(
                prev_txid=utxo.txid,
                output_n=utxo.vout,
                value=utxo.value,
                keys=key,
                witness_type=self.witness_type,
            )
            keys.append(key)
        tx.add_output(amount, address=vault_address)
        if memo is not None:
            tx.add_output(0, lock_script=encode_op_return(memo_bytes))
        if change > 0:
            tx.add_output(change, address=change_address)

        return BuiltSwap(
            tx=tx,
            outputs=extract_outputs(tx),
            fee=fee,
            change_address=change_address,
            keys=keys,
        )

    def sign(self, built: BuiltSwap) -> list[str]:
        # Each input carries its own key; a given key signs only the input(s) it
        # matches, so don't error on the non-matching ones.
        built.tx.sign(built.keys, fail_on_unknown_key=False)
        # M3: with fail_on_unknown_key=False a missing/mismatched key leaves an
        # input silently unsigned; catch that here rather than at broadcast.
        unsigned = [i for i, inp in enumerate(built.tx.inputs) if not inp.signatures]
        if unsigned:
            raise RuntimeError(
                f"refusing to broadcast: {self.chain} inputs {unsigned} left "
                "unsigned (no matching key)"
            )
        if not built.tx.verify():
            raise RuntimeError(
                f"refusing to broadcast: {self.chain} tx failed signature verification"
            )
        return [built.tx.raw_hex()]

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
        memo: str,
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
