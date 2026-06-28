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

import httpx
from bitcoinlib.keys import HDKey
from bitcoinlib.mnemonic import Mnemonic
from bitcoinlib.transactions import Transaction

from cryptoswap.chains.coins import (
    InsufficientFunds,
    Utxo,
    decode_op_return,
    encode_op_return,
    select_coins,
)
from cryptoswap.verify import TxOutput

DEFAULT_ESPLORA = "https://blockstream.info/api"
DEFAULT_DERIVATION = "m/84'/0'/0'/0/0"


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


class BtcAdapter:
    """ChainAdapter for Bitcoin (native segwit / P2WPKH)."""

    chain = "BTC"
    asset = "BTC.BTC"

    def __init__(
        self, esplora_url: str = DEFAULT_ESPLORA, timeout: float = 20.0
    ) -> None:
        self.esplora_url = esplora_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.Client | None = None

    @property
    def _http(self) -> httpx.Client:
        # One pooled, thread-safe client reused across the (concurrent) scan;
        # a fresh connection per address made balance scans take ~40s.
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> BtcAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

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

    # --- network via Esplora; covered by manual/integration testing, not units ---

    def address_info(self, address: str) -> AddressInfo:
        """History + confirmed/pending balance from a single /address call."""
        resp = self._http.get(f"{self.esplora_url}/address/{address}")
        resp.raise_for_status()
        return parse_address_info(resp.json())

    def fetch_utxos(self, address: str) -> list[Utxo]:
        resp = self._http.get(f"{self.esplora_url}/address/{address}/utxo")
        resp.raise_for_status()
        return [
            Utxo(txid=x["txid"], vout=x["vout"], value=x["value"], address=address)
            for x in resp.json()
            if x.get("status", {}).get("confirmed", True)
        ]

    def fetch_balance(self, address: str) -> int:
        return self.address_info(address).confirmed

    def fetch_fee_rate(self, target_blocks: int = 6) -> float:
        resp = self._http.get(f"{self.esplora_url}/fee-estimates")
        resp.raise_for_status()
        estimates = resp.json()
        return float(estimates.get(str(target_blocks)) or min(estimates.values()))

    def broadcast(self, raw_hex: str) -> str:
        resp = self._http.post(f"{self.esplora_url}/tx", content=raw_hex)
        resp.raise_for_status()
        return resp.text.strip()
