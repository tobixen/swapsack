"""Bitcoin chain adapter, backed by bitcoinlib for HD keys, signing and OP_RETURN.

The build/gate/sign machinery lives in :mod:`swapsack.chains.utxo` (shared with
the legacy-P2PKH chains); this module adds the BTC specifics: bech32 (P2WPKH)
derivation and the Esplora-shaped UTXO / balance / fee / broadcast layer.
"""

from __future__ import annotations

import dataclasses

from bitcoinlib.mnemonic import Mnemonic

from swapsack.chains.base import AddressInfo, BalanceReport
from swapsack.chains.coins import Utxo
from swapsack.chains.utxo import UtxoTxBuilder
from swapsack.net import HttpClient

DEFAULT_ESPLORA = "https://blockstream.info/api"
DEFAULT_DERIVATION = "m/84'/0'/0'/0/0"  # receive chain, first index
ACCOUNT = "m/84'/0'/0'"
CHANGE_PATH = "m/84'/0'/0'/1/0"  # internal (change) chain, first index


def generate_mnemonic(strength: int = 128) -> str:
    """Generate a fresh BIP39 mnemonic (128 bits of entropy = 12 words)."""
    return Mnemonic().generate(strength)


def parse_address_info(stats: dict) -> AddressInfo:
    """Parse a single Esplora ``/address`` response."""
    chain = stats.get("chain_stats", {})
    mem = stats.get("mempool_stats", {})
    confirmed = chain.get("funded_txo_sum", 0) - chain.get("spent_txo_sum", 0)
    pending = mem.get("funded_txo_sum", 0) - mem.get("spent_txo_sum", 0)
    has_history = chain.get("tx_count", 0) > 0 or mem.get("tx_count", 0) > 0
    return AddressInfo(has_history=has_history, confirmed=confirmed, pending=pending)


@dataclasses.dataclass(frozen=True)
class TxEntry:
    """One input or output of a broadcast transaction, in sats."""

    value: int
    address: str | None = None  # None for an OP_RETURN (or an unparsed script)
    op_return: bool = False


@dataclasses.dataclass(frozen=True)
class TxSummary:
    """What a broadcast transaction actually did, as the chain reports it.

    Built from an Esplora ``/tx`` body. ``inputs``/``outputs`` are in order, so
    a partial send reads as "one recipient, one change" — the shape a user needs
    to confirm their remainder came back.
    """

    txid: str
    confirmed: bool
    block_height: int | None
    fee: int  # sats
    vsize: int  # virtual bytes; fee/vsize is the fee rate
    inputs: tuple[TxEntry, ...]
    outputs: tuple[TxEntry, ...]

    @property
    def total_in(self) -> int:
        return sum(i.value for i in self.inputs)

    @property
    def total_out(self) -> int:
        return sum(o.value for o in self.outputs)

    @property
    def fee_rate(self) -> float:
        return self.fee / self.vsize if self.vsize else 0.0

    @property
    def has_op_return(self) -> bool:
        """True if the tx carries a memo — i.e. it is a swap deposit, not a send."""
        return any(o.op_return for o in self.outputs)


def parse_tx_summary(payload: dict) -> TxSummary:
    """Parse an Esplora ``/tx/<txid>`` response into a :class:`TxSummary`.

    Pure (no I/O) so it can be unit-tested against a recorded response, like
    ``parse_address_info``.
    """
    status = payload.get("status", {})
    # Esplora reports weight; vsize is weight/4 rounded up.
    weight = payload.get("weight")
    vsize = -(-weight // 4) if weight else payload.get("size", 0)

    def entry(item: dict) -> TxEntry:
        is_data = item.get("scriptpubkey_type") == "op_return"
        return TxEntry(
            value=item.get("value", 0),
            address=item.get("scriptpubkey_address"),
            op_return=is_data,
        )

    return TxSummary(
        txid=payload.get("txid", ""),
        confirmed=bool(status.get("confirmed")),
        block_height=status.get("block_height"),
        fee=payload.get("fee", 0),
        vsize=vsize,
        inputs=tuple(entry(i.get("prevout") or {}) for i in payload.get("vin", [])),
        outputs=tuple(entry(o) for o in payload.get("vout", [])),
    )


class BtcAdapter(HttpClient, UtxoTxBuilder):
    """ChainAdapter for Bitcoin (native segwit / P2WPKH)."""

    chain = "BTC"
    asset = "BTC.BTC"
    # UtxoTxBuilder knobs (P2WPKH sizing is its default script)
    witness_type = "segwit"
    default_derivation = DEFAULT_DERIVATION
    account = ACCOUNT
    change_path = CHANGE_PATH

    def __init__(
        self,
        esplora_url: str = DEFAULT_ESPLORA,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
        network: str = "bitcoin",
    ) -> None:
        super().__init__(timeout)
        self.esplora_url = esplora_url.rstrip("/")
        self.bip39_passphrase = bip39_passphrase
        # bitcoinlib network name: "bitcoin" (mainnet) or "testnet"/"signet".
        # Set alongside a matching testnet Esplora URL to spend on a testnet.
        self.network = network

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        return self._hdkey(mnemonic, path).address(
            script_type="p2wpkh", encoding="bech32"
        )

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

    def fetch_tx(self, txid: str) -> TxSummary | None:
        """What a broadcast tx did, or None if this chain has never seen it."""
        resp = self._get(f"{self.esplora_url}/tx/{txid}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return parse_tx_summary(resp.json())

    def wallet_balance(self, mnemonic: str, account: str = ACCOUNT) -> BalanceReport:
        from swapsack.chains.scan import wallet_balance_from_scan

        return wallet_balance_from_scan(
            derive_address=lambda p: self.derive_address(mnemonic, p),
            probe=self.address_info,
            account=account,
            symbol="BTC",
        )

    def fetch_fee_rate(self, target_blocks: int = 2) -> float:
        resp = self._get(f"{self.esplora_url}/fee-estimates")
        resp.raise_for_status()
        estimates = resp.json()
        # Fall back to the *highest* known rate, never the cheapest/slowest (M2).
        return float(estimates.get(str(target_blocks)) or max(estimates.values()))

    def broadcast(self, raws: list[str]) -> str:
        txid = ""
        for raw in raws:
            resp = self._post(f"{self.esplora_url}/tx", data=raw)
            resp.raise_for_status()
            txid = resp.text.strip()
        return txid
