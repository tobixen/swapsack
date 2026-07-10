"""Bitcoin chain adapter, backed by bitcoinlib for HD keys, signing and OP_RETURN.

The build/gate/sign machinery lives in :mod:`swapsack.chains.utxo` (shared with
the legacy-P2PKH chains); this module adds the BTC specifics: bech32 (P2WPKH)
derivation and the Esplora-shaped UTXO / balance / fee / broadcast layer.
"""

from __future__ import annotations

from bitcoinlib.mnemonic import Mnemonic

from swapsack.chains.base import AddressInfo, BalanceReport
from swapsack.chains.coins import Utxo
from swapsack.chains.utxo import UtxoTxBuilder
from swapsack.net import HttpClient

DEFAULT_ESPLORA = "https://blockstream.info/api"
DEFAULT_DERIVATION = "m/84'/0'/0'/0/0"
ACCOUNT = "m/84'/0'/0'"


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


class BtcAdapter(HttpClient, UtxoTxBuilder):
    """ChainAdapter for Bitcoin (native segwit / P2WPKH)."""

    chain = "BTC"
    asset = "BTC.BTC"
    # UtxoTxBuilder knobs (P2WPKH sizing is its default script)
    witness_type = "segwit"
    default_derivation = DEFAULT_DERIVATION

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

    def wallet_balance(self, mnemonic: str, account: str = ACCOUNT) -> BalanceReport:
        from swapsack.chains.scan import scan_account

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
            addresses=tuple(address for _, address, _ in records),
        )

    def fetch_fee_rate(self, target_blocks: int = 6) -> float:
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
