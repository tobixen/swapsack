"""Bitcoin chain adapter, backed by bitcoinlib for HD keys, signing and OP_RETURN.

Building a swap is deliberately split from signing: ``build_unsigned_swap``
returns the tx together with neutral outputs for the :mod:`cryptoswap.verify`
gate, and only after that gate passes should the caller ``sign`` and
``broadcast``. UTXO sync and broadcast use a public Esplora API (no node).

Current limitation: signing assumes all selected UTXOs belong to a single
derived key (``path``); per-input paths are a later addition.
"""

from __future__ import annotations

import dataclasses

from bitcoinlib.keys import HDKey
from bitcoinlib.mnemonic import Mnemonic
from bitcoinlib.transactions import Transaction

from cryptoswap.chains.base import BalanceReport
from cryptoswap.chains.coins import (
    InsufficientFunds,
    Utxo,
    decode_op_return,
    encode_op_return,
    select_coins,
)
from cryptoswap.net import HttpClient
from cryptoswap.swap import Prepared, SwapRequest
from cryptoswap.thorchain import Quote
from cryptoswap.verify import SwapPlan, TxOutput, verify_btc_swap

DEFAULT_ESPLORA = "https://blockstream.info/api"
DEFAULT_DERIVATION = "m/84'/0'/0'/0/0"
ACCOUNT = "m/84'/0'/0'"


def generate_mnemonic(strength: int = 128) -> str:
    """Generate a fresh BIP39 mnemonic (128 bits of entropy = 12 words)."""
    return Mnemonic().generate(strength)


@dataclasses.dataclass(frozen=True)
class AddressInfo:
    """Summary of an address from a single Esplora ``/address`` call."""

    has_history: bool
    confirmed: int  # sats, confirmed balance
    pending: int  # sats, net mempool delta (negative when spending)


def parse_address_info(stats: dict) -> AddressInfo:
    chain = stats.get("chain_stats", {})
    mem = stats.get("mempool_stats", {})
    confirmed = chain.get("funded_txo_sum", 0) - chain.get("spent_txo_sum", 0)
    pending = mem.get("funded_txo_sum", 0) - mem.get("spent_txo_sum", 0)
    has_history = chain.get("tx_count", 0) > 0 or mem.get("tx_count", 0) > 0
    return AddressInfo(has_history=has_history, confirmed=confirmed, pending=pending)


@dataclasses.dataclass
class BuiltSwap:
    tx: Transaction
    outputs: list[TxOutput]
    fee: int
    change_address: str
    keys: list[HDKey] = dataclasses.field(default_factory=list)


def _extract_outputs(tx: Transaction) -> list[TxOutput]:
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


class BtcAdapter(HttpClient):
    """ChainAdapter for Bitcoin (native segwit / P2WPKH)."""

    chain = "BTC"
    asset = "BTC.BTC"

    def __init__(
        self, esplora_url: str = DEFAULT_ESPLORA, timeout: float = 20.0
    ) -> None:
        super().__init__(timeout)
        self.esplora_url = esplora_url.rstrip("/")

    def _hdkey(self, mnemonic: str, path: str) -> HDKey:
        seed = Mnemonic().to_seed(mnemonic)
        return HDKey.from_seed(seed, network="bitcoin").key_for_path(path)

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        return self._hdkey(mnemonic, path).address(
            script_type="p2wpkh", encoding="bech32"
        )

    def build_unsigned_swap(
        self,
        *,
        mnemonic: str,
        utxos: list[Utxo],
        vault_address: str,
        amount: int,
        memo: str,
        fee_rate: float,
        change_address: str | None = None,
        default_path: str = DEFAULT_DERIVATION,
        sweep: bool = False,
    ) -> BuiltSwap:
        change_address = change_address or self.derive_address(mnemonic, default_path)
        memo_bytes = memo.encode()
        if sweep:
            # Spend everything: fee is whatever is left over the vault output.
            chosen = list(utxos)
            fee = sum(u.value for u in chosen) - amount
            change = 0
            if fee < 0:
                raise InsufficientFunds(f"amount {amount} exceeds balance")
        else:
            sel = select_coins(utxos, amount, fee_rate, len(memo_bytes))
            chosen, fee, change = sel.utxos, sel.fee, sel.change

        tx = Transaction(network="bitcoin", witness_type="segwit")
        keys: list[HDKey] = []
        for utxo in chosen:
            key = self._hdkey(mnemonic, utxo.path or default_path)
            tx.add_input(
                prev_txid=utxo.txid,
                output_n=utxo.vout,
                value=utxo.value,
                keys=key,
                witness_type="segwit",
            )
            keys.append(key)
        tx.add_output(amount, address=vault_address)
        tx.add_output(0, lock_script=encode_op_return(memo_bytes))
        if change > 0:
            tx.add_output(change, address=change_address)

        return BuiltSwap(
            tx=tx,
            outputs=_extract_outputs(tx),
            fee=fee,
            change_address=change_address,
            keys=keys,
        )

    def sign(self, built: BuiltSwap) -> str:
        # Each input carries its own key; a given key signs only the input(s) it
        # matches, so don't error on the non-matching ones.
        built.tx.sign(built.keys, fail_on_unknown_key=False)
        return built.tx.raw_hex()

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

    # --- network via Esplora; covered by manual/integration testing, not units ---

    def address_info(self, address: str) -> AddressInfo:
        """History + confirmed/pending balance from a single /address call."""
        resp = self._get(f"{self.esplora_url}/address/{address}")
        resp.raise_for_status()
        return parse_address_info(resp.json())

    def fetch_utxos(self, address: str) -> list[Utxo]:
        resp = self._get(f"{self.esplora_url}/address/{address}/utxo")
        resp.raise_for_status()
        # Fail closed: only spend UTXOs explicitly marked confirmed (L1).
        return [
            Utxo(txid=x["txid"], vout=x["vout"], value=x["value"], address=address)
            for x in resp.json()
            if x.get("status", {}).get("confirmed", False)
        ]

    def fetch_balance(self, address: str) -> int:
        return self.address_info(address).confirmed

    def wallet_balance(self, mnemonic: str, account: str = ACCOUNT) -> BalanceReport:
        from cryptoswap.chains.scan import scan_account

        records = scan_account(
            derive_address=lambda p: self.derive_address(mnemonic, p),
            probe=self.address_info,
            account=account,
        )
        confirmed = sum(info.confirmed for _, _, info in records)
        pending = sum(info.pending for _, _, info in records)
        return BalanceReport(
            symbol="BTC",
            confirmed=confirmed,
            decimals=8,
            pending=pending,
            note=f"({len(records)} used addresses)",
        )

    def fetch_fee_rate(self, target_blocks: int = 6) -> float:
        resp = self._get(f"{self.esplora_url}/fee-estimates")
        resp.raise_for_status()
        estimates = resp.json()
        # Fall back to the *highest* known rate, never the cheapest/slowest (M2).
        return float(estimates.get(str(target_blocks)) or max(estimates.values()))

    def broadcast(self, raw_hex: str) -> str:
        resp = self._post(f"{self.esplora_url}/tx", data=raw_hex)
        resp.raise_for_status()
        return resp.text.strip()
