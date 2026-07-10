"""Dash chain adapter — Phase 1 (hold/balance) + Phase 2 (send/sweep).

Dash is a legacy (pre-segwit) P2PKH chain with no Blockstream Esplora; the
balance / UTXO / broadcast layer speaks to an Insight-API instance
(configurable — a single community explorer is a SPOF that can silently
under-report funds, see docs/dash.md). Swaps route through Maya only (no DASH
pool on THORChain).

Spending shares the bitcoinlib build/gate/sign path with BTC
(:mod:`swapsack.chains.utxo`): Dash transactions are plain pre-segwit Bitcoin
transactions with different address prefixes, so a ``dash`` network registered
in bitcoinlib (below) is all the signer needs. The legacy P2PKH fee/dust maths
comes from :data:`swapsack.chains.coins.P2PKH`. Insight exposes no usable
``estimatefee``, and Dash fees are ~fixed and low, so ``fetch_fee_rate``
returns a conservative constant instead of a network estimate.

The swap-*from* side (vault deposit + OP_RETURN memo) is Phase 3 — the
building blocks are here, but it is not wired into the CLI.
"""

from __future__ import annotations

from bitcoinlib.networks import NETWORK_DEFINITIONS

from swapsack.chains.base import AddressInfo, BalanceReport
from swapsack.chains.coins import P2PKH, Utxo
from swapsack.chains.p2pkh import derive_p2pkh_address
from swapsack.chains.utxo import UtxoTxBuilder
from swapsack.net import HttpClient

DEFAULT_DASH_API = "https://insight.dash.org/insight-api"
DEFAULT_DERIVATION = "m/44'/5'/0'/0/0"
ACCOUNT = "m/44'/5'/0'"
PREFIX_P2PKH = b"\x4c"  # addresses start with "X"

# Conservative flat fee rate (duffs/vB). Dash Core's min relay is 1 duff/B and
# blocks are far from full; 2 leaves margin without overpaying (a typical
# 1-in-2-out send is ~227 vB ≈ 454 duffs ≈ €0.0002). Insight has no usable
# estimatefee endpoint to ask instead (see docs/dash.md).
DEFAULT_FEE_RATE = 2.0

# bitcoinlib ships no Dash network; register one (idempotent). Only the fields
# the signer touches matter here: the address/WIF prefixes and standard BIP32
# xpub/xprv bytes (the legacy drkp/drkv bytes are deprecated — Trust Wallet et
# al. use the standard ones). Dash has no segwit, hence no bech32 prefix.
_DASH_NETWORK = {
    "description": "Dash Network",
    "currency_name": "dash",
    "currency_name_plural": "dash",
    "currency_symbol": "DASH",
    "currency_code": "DASH",
    "prefix_address": "4C",
    "prefix_address_p2sh": "10",
    "prefix_bech32": "",
    "prefix_wif": "CC",
    "prefixes_wif": [
        ["0488B21E", "xpub", "public", False, "legacy", "p2pkh"],
        ["0488ADE4", "xprv", "private", False, "legacy", "p2pkh"],
    ],
    "bip44_cointype": 5,
    "denominator": 1e-08,
    "dust_amount": P2PKH.dust,
    "fee_default": 1000,
    "fee_min": 226,
    "fee_max": 100000,
    "priority": 5,
}
NETWORK_DEFINITIONS.setdefault("dash", _DASH_NETWORK)


def parse_insight_addr(stats: dict) -> AddressInfo:
    # Insight spells it "txApperances" (sic); newer forks add the corrected
    # spelling as an alias. Accept either, preferring the original.
    appearances = stats.get("txApperances", stats.get("txAppearances", 0))
    unconfirmed = stats.get("unconfirmedTxApperances", 0)
    received = stats.get("totalReceivedSat", 0)
    return AddressInfo(
        has_history=appearances > 0 or unconfirmed > 0 or received > 0,
        confirmed=stats.get("balanceSat", 0),
        pending=stats.get("unconfirmedBalanceSat", 0),
    )


class DashAdapter(HttpClient, UtxoTxBuilder):
    """ChainAdapter for Dash (legacy P2PKH): hold, balance, send, sweep."""

    chain = "DASH"
    asset = "DASH.DASH"
    # The DASH.DASH pool exists only on Maya — and THORChain answers an LP probe
    # for a pool it doesn't run with a 500, not a clean "no position" 404.
    lp_backends = ("maya",)
    # UtxoTxBuilder knobs: legacy transactions with legacy fee/dust sizing.
    witness_type = "legacy"
    script = P2PKH
    default_derivation = DEFAULT_DERIVATION
    network = "dash"

    def __init__(
        self,
        api_url: str = DEFAULT_DASH_API,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(timeout)
        self.api_url = api_url.rstrip("/")
        self.bip39_passphrase = bip39_passphrase

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        # Deliberately NOT bitcoinlib's encoder: this stays the independent,
        # golden-vector-pinned path (test_dash.py cross-checks the registered
        # bitcoinlib network agrees with it before any signing).
        return derive_p2pkh_address(mnemonic, path, PREFIX_P2PKH, self.bip39_passphrase)

    # --- network via Insight; guarded by an opt-in live test (test_dash.py) ---

    def address_info(self, address: str) -> AddressInfo:
        resp = self._get(f"{self.api_url}/addr/{address}")
        resp.raise_for_status()
        return parse_insight_addr(resp.json())

    def fetch_utxos(self, address: str) -> list[Utxo]:
        resp = self._get(f"{self.api_url}/addr/{address}/utxo")
        resp.raise_for_status()
        # Fail closed: only spend UTXOs with at least one confirmation.
        return [
            Utxo(
                txid=x["txid"],
                vout=x["vout"],
                value=x["satoshis"],
                address=address,
            )
            for x in resp.json()
            if x.get("confirmations", 0) > 0
        ]

    def fetch_balance(self, address: str) -> int:
        return self.address_info(address).confirmed

    def fetch_fee_rate(self, target_blocks: int = 6) -> float:  # noqa: ARG002
        """A conservative flat duffs/vB rate (see DEFAULT_FEE_RATE)."""
        return DEFAULT_FEE_RATE

    def wallet_balance(self, mnemonic: str, account: str = ACCOUNT) -> BalanceReport:
        from swapsack.chains.scan import scan_account

        records = scan_account(
            derive_address=lambda p: self.derive_address(mnemonic, p),
            probe=self.address_info,
            account=account,
        )
        return BalanceReport(
            symbol="DASH",
            confirmed=sum(info.confirmed for _, _, info in records),
            decimals=8,
            pending=sum(info.pending for _, _, info in records),
            note=f"({len(records)} used addresses)",
            addresses=tuple(address for _, address, _ in records),
        )

    def broadcast(self, raws: list[str]) -> str:
        txid = ""
        for raw in raws:
            resp = self._post(f"{self.api_url}/tx/send", json={"rawtx": raw})
            resp.raise_for_status()
            txid = resp.json()["txid"]
        return txid
