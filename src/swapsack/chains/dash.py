"""Dash chain adapter — Phase 1: address derivation + balance (receive-only).

Dash is a legacy (pre-segwit) P2PKH chain with no Blockstream Esplora; balances
come from an Insight-API instance (configurable — a single community explorer
is a SPOF that can silently under-report funds, see docs/dash.md). Swaps route
through Maya only (no DASH pool on THORChain).

The spend side (send/sweep/swap-from) is deliberately NOT implemented — that is
Phase 2 in docs/dash.md (legacy fee maths + verify gate) — so ``broadcast``
refuses loudly rather than ever pretending to work. Funds received here are
spendable by importing the seed into another Dash wallet (standard BIP44,
``m/44'/5'/0'/0/x``).
"""

from __future__ import annotations

import dataclasses

from swapsack.chains.base import BalanceReport
from swapsack.chains.p2pkh import derive_p2pkh_address
from swapsack.net import HttpClient

DEFAULT_DASH_API = "https://insight.dash.org/insight-api"
DEFAULT_DERIVATION = "m/44'/5'/0'/0/0"
ACCOUNT = "m/44'/5'/0'"
PREFIX_P2PKH = b"\x4c"  # addresses start with "X"


@dataclasses.dataclass(frozen=True)
class AddressInfo:
    """Summary of an address from a single Insight ``/addr`` call."""

    has_history: bool
    confirmed: int  # duffs (1e-8 DASH), confirmed balance
    pending: int  # duffs, net mempool delta (negative when spending)


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


class DashAdapter(HttpClient):
    """ChainAdapter for Dash (legacy P2PKH), Phase 1: address + balance only."""

    chain = "DASH"
    asset = "DASH.DASH"
    # The DASH.DASH pool exists only on Maya — and THORChain answers an LP probe
    # for a pool it doesn't run with a 500, not a clean "no position" 404.
    lp_backends = ("maya",)

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
        return derive_p2pkh_address(mnemonic, path, PREFIX_P2PKH, self.bip39_passphrase)

    # --- network via Insight; guarded by an opt-in live test (test_dash.py) ---

    def address_info(self, address: str) -> AddressInfo:
        resp = self._get(f"{self.api_url}/addr/{address}")
        resp.raise_for_status()
        return parse_insight_addr(resp.json())

    def fetch_balance(self, address: str) -> int:
        return self.address_info(address).confirmed

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
        raise NotImplementedError(
            "the DASH spend path is not implemented (receive/balance only) — "
            "see docs/dash.md Phase 2"
        )
