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
    memo_bytes,
    select_coins,
)
from swapsack.chains.gated import GatedTxBuilder
from swapsack.chains.rbf import Replacement
from swapsack.swap import Prepared
from swapsack.verify import (
    SendPlan,
    SwapPlan,
    TxOutput,
    verify_btc_send,
    verify_btc_swap,
)

# Signal BIP125 opt-in Replace-By-Fee on every input. 0xfffffffd is the largest
# nSequence that still signals RBF (< 0xfffffffe) while leaving locktime enabled.
# The wallet fee-targets only a few blocks, so a low-fee spend can get stuck;
# signalling is what lets `swapsack bump` fee-replace it afterwards, since
# standard-policy nodes refuse to replace a non-signalling tx. Costs nothing —
# miners treat a signalling tx identically. The replacement itself is planned in
# chains/rbf.py and rebuilt by build_replacement below.
#
# This builder is shared with DASH, where the signal is inert: Dash Core
# implements no mempool replacement (deliberately, for InstantSend). Setting it
# anyway keeps one builder rather than a per-chain sequence, and a non-final
# nSequence with nLockTime 0 is standard and relays normally on both chains.
RBF_SEQUENCE = 0xFFFFFFFD


@dataclasses.dataclass
class BuiltSwap:
    tx: Transaction
    outputs: list[TxOutput]
    fee: int
    change_address: str
    keys: list[HDKey] = dataclasses.field(default_factory=list)
    # The UTXOs coin selection actually took — a subset of what was scanned, and
    # the only honest basis for saying what this transaction spends and what its
    # unconfirmed parents cost it (see ``cli._report_cpfp_surcharge``).
    inputs: list[Utxo] = dataclasses.field(default_factory=list)


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


class UtxoTxBuilder(GatedTxBuilder):
    """Mixin: build + gate + sign UTXO transactions via bitcoinlib.

    The gate wrappers (``build_and_verify``/``_deposit``/``_send``) are shared
    with ZEC via :class:`~swapsack.chains.gated.GatedTxBuilder`; this class
    supplies the bitcoinlib ``build_unsigned_swap`` hook and ``sign``.

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

    def sweep_send_amount(
        self,
        total: int,
        n_inputs: int,
        fee_rate: float,
        memo_len: int = 0,
        extra_fee: int = 0,
    ) -> tuple[int, int]:
        """``(send_amount, fee)`` draining every UTXO into one output.

        ``memo_len`` sizes the OP_RETURN a swap/LP deposit carries (0 for a
        plain send); ``extra_fee`` is the CPFP surcharge for the unconfirmed
        inputs a sweep necessarily spends. Adapter-level so each chain brings
        its own fee model (ZEC overrides with ZIP-317, where the rate argument
        is meaningless).
        """
        from swapsack.chains.coins import sweep_amount

        return sweep_amount(
            total,
            n_inputs,
            fee_rate,
            memo_len=memo_len,
            script=self.script,
            extra_fee=extra_fee,
        )

    def build_unsigned_swap(
        self,
        *,
        mnemonic: str,
        utxos: list[Utxo],
        vault_address: str,
        amount: int,
        memo: str | bytes | None,
        fee_rate: float,
        change_address: str | None = None,
        default_path: str | None = None,
        sweep: bool = False,
    ) -> BuiltSwap:
        """Build the unsigned tx paying ``amount`` to ``vault_address``.

        ``memo`` of ``None`` omits the OP_RETURN output entirely — used for a
        plain send (no swap). A ``str`` is a THORChain/Maya memo; ``bytes`` is a
        binary payload (a Chainflip vault swap) carried verbatim.
        """
        default_path = default_path or self.default_derivation
        change_address = change_address or self.derive_address(mnemonic, default_path)
        payload = memo_bytes(memo)
        if sweep:
            # Spend everything: fee is whatever is left over the vault output.
            chosen = list(utxos)
            fee = sum(u.value for u in chosen) - amount
            change = 0
            if fee < 0:
                raise InsufficientFunds(f"amount {amount} exceeds balance")
        else:
            sel = select_coins(
                utxos, amount, fee_rate, len(payload), script=self.script
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
                sequence=RBF_SEQUENCE,
            )
            keys.append(key)
        tx.add_output(amount, address=vault_address)
        if memo is not None:
            tx.add_output(0, lock_script=encode_op_return(payload))
        if change > 0:
            tx.add_output(change, address=change_address)

        return BuiltSwap(
            tx=tx,
            outputs=extract_outputs(tx),
            fee=fee,
            change_address=change_address,
            keys=keys,
            inputs=chosen,
        )

    def build_replacement(
        self, *, mnemonic: str, replacement: Replacement
    ) -> BuiltSwap:
        """Rebuild a mempool transaction with a higher fee (BIP125 replace-by-fee).

        Unlike :meth:`build_unsigned_swap` there is no coin selection and no fee
        maths: :func:`~swapsack.chains.rbf.plan_replacement` has already fixed
        the inputs (the original's, which is what makes this a replacement) and
        the outputs (the original's, with only the change reduced). This turns
        that plan into a signable transaction and nothing more.
        """
        tx = Transaction(network=self.network, witness_type=self.witness_type)
        keys: list[HDKey] = []
        for utxo in replacement.inputs:
            key = self._hdkey(mnemonic, utxo.path or self.default_derivation)
            tx.add_input(
                prev_txid=utxo.txid,
                output_n=utxo.vout,
                value=utxo.value,
                keys=key,
                witness_type=self.witness_type,
                sequence=RBF_SEQUENCE,
            )
            keys.append(key)
        for out in replacement.outputs:
            if out.op_return_data is not None:
                tx.add_output(
                    out.value, lock_script=encode_op_return(out.op_return_data)
                )
            else:
                tx.add_output(out.value, address=out.address)

        return BuiltSwap(
            tx=tx,
            outputs=extract_outputs(tx),
            fee=replacement.fee,
            change_address=replacement.change_address,
            keys=keys,
            inputs=replacement.inputs,
        )

    def build_and_verify_replacement(
        self,
        *,
        mnemonic: str,
        replacement: Replacement,
        now: int,
        max_fee: int,
    ) -> Prepared:
        """Build + gate a fee bump, through the same gate a first spend passes.

        The plan is reconstructed from the original transaction's own outputs,
        so the gate cannot re-derive the *intent* — the quote that authorised
        the deposit is long gone, and its destination is inside a memo this does
        not parse. What it does prove is that the rebuild did not drift: same
        vault/recipient, same amount, byte-identical memo, change still ours,
        and the raised fee still inside ``--max-fee``. That is the whole risk a
        replacement adds over the original, which passed the full gate when it
        was built.
        """
        built = self.build_replacement(mnemonic=mnemonic, replacement=replacement)
        owned = {replacement.change_address} | {u.address for u in replacement.inputs}
        if replacement.memo is None:
            plan: SendPlan | SwapPlan = SendPlan(
                recipient=replacement.recipient, amount=replacement.amount
            )
            problems = verify_btc_send(
                built.outputs,
                fee=built.fee,
                plan=plan,
                owned_addresses=owned,
                max_fee=max_fee,
            )
        else:
            plan = SwapPlan(
                inbound_address=replacement.recipient,
                amount=replacement.amount,
                memo=replacement.memo,
                # Not a quote expiry: nothing is being re-quoted, and the
                # original's expiry is unknowable after the fact. The memo's own
                # min-out limit is what protects the swap, and the CLI warns
                # that it may have gone stale in the mempool. The hour is the
                # same placeholder build_and_verify_deposit uses for the other
                # unquoted deposit, and must outlast the confirmation prompt:
                # the CLI re-checks it before broadcasting.
                expiry=now + 3600,
                # The payout destination lives inside the memo, which is
                # carried verbatim off the chain from a transaction the full
                # gate already passed when it was built. Empty makes that skip
                # deliberate and greppable rather than accidental.
                destination="",
            )
            problems = verify_btc_swap(
                built.outputs,
                fee=built.fee,
                plan=plan,
                owned_addresses=owned,
                now=now,
                max_fee=max_fee,
            )
        # The gate reads the outputs but takes the fee on trust, and here the
        # fee is planned rather than derived from a selection — so reconcile it
        # against the transaction itself. A change value the builder got wrong
        # would otherwise be paid straight to a miner, silently.
        real_fee = sum(u.value for u in replacement.inputs) - sum(
            o.value for o in built.outputs
        )
        if real_fee != built.fee:
            problems.append(
                f"replacement pays {real_fee} sats in fee, not the planned {built.fee}"
            )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

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
