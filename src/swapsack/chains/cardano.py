"""Cardano chain adapter — hold and balance (send and swap-from to follow).

Keys and addresses come from :mod:`swapsack.chains.cardano_keys` (CIP-1852,
Icarus master key, Shelley *base* addresses — the ones Yoroi/Eternl show for the
same mnemonic). Everything network goes to **Koios** (``api.koios.rest``), a
keyless public Cardano API; any Koios instance can be named with ``--ada-api``.

Units: the adapter's whole surface — balances, UTXO values, amounts, fees —
is in **1e-8 ADA**, the unit Maya quotes and the shared UTXO flow and verify
gate count in. ADA's own unit is the lovelace (1e-6), so a value read from the
chain is scaled up by :data:`LOVELACE_TO_UNITS` here and back down only on the
wire. Swaps route through Maya only: THORChain has no ADA pool.
"""

from __future__ import annotations

from swapsack.chains.base import AddressInfo, BalanceReport
from swapsack.chains.cardano_keys import base_address, derive, parse_path, root_key
from swapsack.net import HttpClient

DEFAULT_ADA_API = "https://api.koios.rest/api/v1"
ACCOUNT = "m/1852'/1815'/0'"
DEFAULT_DERIVATION = f"{ACCOUNT}/0/0"  # external chain, first index
CHANGE_PATH = f"{ACCOUNT}/1/0"  # internal (change) chain, first index
# CIP-1852 role 2: the account's single staking key, shared by every base
# address of the account (only the payment half differs per address).
STAKE_ROLE_INDEX = "2/0"
LOVELACE_TO_UNITS = 100  # 1 lovelace = 1e-6 ADA = 100 × 1e-8 ADA


def parse_address_info(rows: list[dict]) -> AddressInfo:
    """One address's Koios ``address_info`` answer, in 1e-8 ADA.

    Koios lists every address that ever appeared on chain and omits the rest,
    so presence alone is the history signal: a used-but-emptied address comes
    back with balance 0 and must keep the gap-limit scan going. Koios indexes
    the ledger only (no mempool), so ``pending`` is always 0.
    """
    if not rows:
        return AddressInfo(has_history=False, confirmed=0, pending=0)
    return AddressInfo(
        has_history=True,
        confirmed=int(rows[0]["balance"]) * LOVELACE_TO_UNITS,
        pending=0,
    )


class AdaAdapter(HttpClient):
    """ChainAdapter for Cardano (Shelley base addresses): hold and balance."""

    chain = "ADA"
    asset = "ADA.ADA"
    unconfirmed_spendable = False  # Koios indexes the ledger, not the mempool
    default_derivation = DEFAULT_DERIVATION
    account = ACCOUNT
    change_path = CHANGE_PATH
    # The ADA.ADA pool exists only on Maya.
    lp_backends = ("maya",)

    def __init__(
        self,
        api_url: str = DEFAULT_ADA_API,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(timeout, hint="--ada-api / $SWAPSACK_ADA_API")
        self.api_url = api_url.rstrip("/")
        self.bip39_passphrase = bip39_passphrase

    # --- derivation ------------------------------------------------------------

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        """The base address whose payment key sits at ``path``.

        The stake half is the account's role-2 key, so ``path`` must be a full
        CIP-1852 ``m/purpose'/coin'/account'/role/index``.
        """
        if len(parse_path(path)) != 5:
            raise ValueError(f"not a CIP-1852 address path: {path!r}")
        account = path.rsplit("/", 2)[0]
        root = root_key(mnemonic, self.bip39_passphrase)
        payment = derive(root, path).public_key
        stake = derive(root, f"{account}/{STAKE_ROLE_INDEX}").public_key
        return base_address(payment, stake)

    # --- network via Koios; guarded by an opt-in live test ---------------------

    def _query(self, endpoint: str, body: dict) -> list[dict]:
        resp = self._post_query(f"{self.api_url}/{endpoint}", json=body)
        resp.raise_for_status()
        return resp.json()

    def address_info(self, address: str) -> AddressInfo:
        return parse_address_info(
            self._query("address_info", {"_addresses": [address]})
        )

    def wallet_balance(self, mnemonic: str, account: str = ACCOUNT) -> BalanceReport:
        from swapsack.chains.scan import wallet_balance_from_scan

        return wallet_balance_from_scan(
            derive_address=lambda p: self.derive_address(mnemonic, p),
            probe=self.address_info,
            account=account,
            symbol="ADA",
        )
