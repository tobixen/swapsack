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
from swapsack.chains.gated import GatedTxBuilder
from swapsack.verify import TxOutput

# Signal BIP125 opt-in Replace-By-Fee on every input. 0xfffffffd is the largest
# nSequence that still signals RBF (< 0xfffffffe) while leaving locktime enabled.
# The wallet fee-targets only a few blocks, so a low-fee spend can get stuck;
# signalling lets a future `bump` command fee-replace it (standard-policy nodes
# reject replacing a non-signalling tx). Harmless today — miners treat a
# signalling tx identically. See docs/TODO.md for the planned bump/RBF command.
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
        self, total: int, n_inputs: int, fee_rate: float, memo_len: int = 0
    ) -> tuple[int, int]:
        """``(send_amount, fee)`` draining every UTXO into one output.

        ``memo_len`` sizes the OP_RETURN a swap/LP deposit carries (0 for a
        plain send). Adapter-level so each chain brings its own fee model (ZEC
        overrides with ZIP-317, where the rate argument is meaningless).
        """
        from swapsack.chains.coins import sweep_amount

        return sweep_amount(
            total, n_inputs, fee_rate, memo_len=memo_len, script=self.script
        )

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
                sequence=RBF_SEQUENCE,
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
