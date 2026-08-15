"""Shared adapter for THORChain-family native assets (Cosmos-SDK / Tendermint).

MayaChain (CACAO) and THORChain (RUNE) are the same chain software (Maya is a
fork), so a single :class:`CosmosAdapter` implements hold/balance/send/swap-from
for both; the concrete adapters (``maya.MayaAdapter``, ``thor.ThorAdapter``) are
just a few class-attribute overrides (HRP, asset, denom, decimals, chain-id,
node URL). They share SLIP-44 coin type 931, secp256k1, and a
``bech32(ripemd160(sha256(compressed_pubkey)))`` account address; only the HRP
differs (``maya`` vs ``thor``).

Deriving the address is the money-sensitive part — a wrong address silently
sends funds to one the wallet does not control, and there is no testnet — so the
derivation is cross-checked in the tests against independent BIP32
implementations and golden vectors. See docs/cacao.md for the design notes.
"""

from __future__ import annotations

import base64
import dataclasses

from bitcoinlib.encoding import hash160
from bitcoinlib.keys import HDKey
from bitcoinlib.mnemonic import Mnemonic

# Re-exported: the Cosmos address codec lives in swapsack.bech32 so the
# destination-address validator can verify a maya1/thor1 checksum without
# importing a chain adapter. Callers here keep importing it from this module.
from swapsack.bech32 import bech32_decode, bech32_encode
from swapsack.chains import cosmos_tx
from swapsack.chains.base import BalanceReport
from swapsack.net import HTTP_ERRORS, HttpClient
from swapsack.swap import BroadcastError, Prepared
from swapsack.verify import (
    CosmosDepositPlan,
    CosmosSendPlan,
    verify_cosmos_deposit,
    verify_cosmos_send,
)

# THORChain/Maya share this derivation (SLIP-44 coin type 931, secp256k1).
DEFAULT_DERIVATION = "m/44'/931'/0'/0/0"
# Gas limit for a native tx. THORChain-family chains charge a fixed native-tx fee
# themselves (deducted from the account), so the cosmos Fee carries no coins.
DEFAULT_GAS_LIMIT = 2_000_000

# --- balance parsing --------------------------------------------------------


def parse_balances(payload: dict, denom: str) -> int:
    """Sum the base-unit amount for ``denom`` in a cosmos bank balances response.

    A fresh (never-funded) account returns an empty ``balances`` list -> 0.
    """
    total = 0
    for entry in payload.get("balances", []):
        if entry.get("denom") == denom:
            total += int(entry.get("amount", 0))
    return total


@dataclasses.dataclass
class BuiltCosmosTx:
    """An unsigned Cosmos tx: the two SignDoc byte-blobs + what signing needs."""

    body_bytes: bytes
    auth_info_bytes: bytes
    chain_id: str
    account_number: int
    private_key: bytes


class CosmosAdapter(HttpClient):
    """ChainAdapter base for a THORChain-family native asset.

    Concrete adapters set the class attributes below. The asset is deposited to
    the chain itself (``MsgDeposit``), not to an external inbound vault, so
    ``native_source`` makes ``prepare_swap`` skip the vault lookup and instead
    verify (locally, no I/O) that the quoting client is this asset's home
    network — a MsgDeposit executes on the adapter's own chain regardless of
    which backend priced it. ``home_path_prefix`` names that network and must
    match the home ``ThorchainClient.path_prefix``.
    """

    chain: str  # e.g. "MAYA" / "THOR"
    asset: str  # e.g. "MAYA.CACAO" / "THOR.RUNE"
    symbol: str  # balance display symbol, e.g. "CACAO" / "RUNE"
    hrp: str  # bech32 prefix, e.g. "maya" / "thor"
    denom: str  # bank denom, e.g. "cacao" / "rune"
    decimals: int  # 10 for CACAO, 8 for RUNE
    default_chain_id: str
    default_node: str
    home_path_prefix: str  # home backend's ThorchainClient.path_prefix
    native_source = True

    def __init__(
        self,
        node_url: str | None = None,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(timeout)
        self.node_url = (node_url or self.default_node).rstrip("/")
        self.bip39_passphrase = bip39_passphrase

    # --- keys / address / balance (read-only) -------------------------------

    def _keys(self, mnemonic: str, path: str) -> tuple[bytes, bytes]:
        """Return ``(private_byte, compressed_public_byte)`` for ``path``."""
        seed = Mnemonic().to_seed(mnemonic, self.bip39_passphrase)
        key = HDKey.from_seed(seed).key_for_path(path)
        return key.private_byte, key.public_byte

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        """Derive the bech32 account address for ``path`` (default receive key)."""
        _, pubkey = self._keys(mnemonic, path)
        return bech32_encode(self.hrp, hash160(pubkey))

    def fetch_balance(self, address: str) -> int:
        """Confirmed native balance in base units; 0 for a fresh account."""
        resp = self._get(f"{self.node_url}/cosmos/bank/v1beta1/balances/{address}")
        resp.raise_for_status()
        return parse_balances(resp.json(), self.denom)

    def wallet_balance(self, mnemonic: str) -> BalanceReport:
        address = self.derive_address(mnemonic)
        return BalanceReport(
            symbol=self.symbol,
            confirmed=self.fetch_balance(address),
            decimals=self.decimals,
            note=f"({address})",
            addresses=(address,),
        )

    def fetch_account(self, address: str) -> tuple[int, int]:
        """Return ``(account_number, sequence)`` for signing; both 0 for a fresh
        account the node has not seen yet."""
        resp = self._get(f"{self.node_url}/cosmos/auth/v1beta1/accounts/{address}")
        if resp.status_code == 404:
            return 0, 0
        resp.raise_for_status()
        account = resp.json().get("account", {})
        return int(account.get("account_number", 0)), int(account.get("sequence", 0))

    def fetch_chain_id(self) -> str:
        """The network's chain id (falls back to the known mainnet id on failure)."""
        try:
            resp = self._get(
                f"{self.node_url}/cosmos/base/tendermint/v1beta1/node_info"
            )
            resp.raise_for_status()
            return resp.json()["default_node_info"]["network"] or self.default_chain_id
        except (KeyError, ValueError, *HTTP_ERRORS):
            return self.default_chain_id

    # --- spending: plain send (MsgSend) -------------------------------------

    def _built(
        self,
        *,
        body_bytes: bytes,
        private_key: bytes,
        public_key: bytes,
        sender: str,
        gas_limit: int,
    ) -> BuiltCosmosTx:
        account_number, sequence = self.fetch_account(sender)
        return BuiltCosmosTx(
            body_bytes=body_bytes,
            auth_info_bytes=cosmos_tx.auth_info(public_key, sequence, [], gas_limit),
            chain_id=self.fetch_chain_id(),
            account_number=account_number,
            private_key=private_key,
        )

    def build_and_verify_send(
        self,
        *,
        recipient: str,
        amount: int,
        mnemonic: str,
        path: str = DEFAULT_DERIVATION,
        gas_limit: int = DEFAULT_GAS_LIMIT,
    ) -> Prepared:
        """Build + gate a plain ``MsgSend`` of ``amount`` (native base units).

        Signing/broadcast happen only after the returned gate passes. The gate
        decodes the *serialized* body and binds sender/recipient/denom/amount and
        the absence of a memo — a wrong recipient here is an unrefundable loss.
        """
        private_key, public_key = self._keys(mnemonic, path)
        sender = self.derive_address(mnemonic, path)
        amount_str = str(amount)
        body_bytes = cosmos_tx.tx_body(
            [
                (
                    cosmos_tx.MSGSEND_TYPE_URL,
                    cosmos_tx.msg_send(sender, recipient, self.denom, amount_str),
                )
            ],
            "",
        )
        built = self._built(
            body_bytes=body_bytes,
            private_key=private_key,
            public_key=public_key,
            sender=sender,
            gas_limit=gas_limit,
        )
        plan = CosmosSendPlan(
            from_addr=sender, recipient=recipient, denom=self.denom, amount=amount_str
        )
        problems = verify_cosmos_send(
            decoded=cosmos_tx.decode_msg_send_body(body_bytes), plan=plan
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

    # --- native MsgDeposit (swap-from and LP legs share one build+gate) ------

    def _prepare_deposit(
        self,
        *,
        memo: str,
        amount: int,
        mnemonic: str,
        now: int,
        destination: str,
        expiry: int,
        quote,  # noqa: ANN001 (thorchain.Quote | None)
        path: str,
        gas_limit: int,
    ) -> Prepared:
        """Build + gate a native ``MsgDeposit`` carrying ``memo``.

        The single money path both deposit flavours ride: the gate binds the
        deposited coin/amount, the exact ``memo``, our signer, the ``expiry``
        and (when given) that the memo pays ``destination``.
        """
        private_key, public_key = self._keys(mnemonic, path)
        sender = self.derive_address(mnemonic, path)
        _, signer_bytes = bech32_decode(sender)
        amount_str = str(amount)
        deposit = cosmos_tx.msg_deposit([(self.asset, amount_str)], memo, signer_bytes)
        body_bytes = cosmos_tx.tx_body([(cosmos_tx.MSGDEPOSIT_TYPE_URL, deposit)], "")
        built = self._built(
            body_bytes=body_bytes,
            private_key=private_key,
            public_key=public_key,
            sender=sender,
            gas_limit=gas_limit,
        )
        plan = CosmosDepositPlan(
            asset=self.asset,
            amount=amount_str,
            memo=memo,
            destination=destination,
            signer=signer_bytes,
            expiry=expiry,
        )
        problems = verify_cosmos_deposit(
            decoded=cosmos_tx.decode_msg_deposit_body(body_bytes), plan=plan, now=now
        )
        return Prepared(quote=quote, built=built, plan=plan, problems=problems)

    def build_and_verify(
        self,
        *,
        quote,  # noqa: ANN001 (thorchain.Quote)
        request,  # noqa: ANN001 (swap.SwapRequest)
        now: int,
        mnemonic: str,
        path: str = DEFAULT_DERIVATION,
        gas_limit: int = DEFAULT_GAS_LIMIT,
    ) -> Prepared:
        """Build + gate a native swap as a ``MsgDeposit`` (no inbound vault).

        ``request.amount`` is in the asset's native base units (the quote API's
        unit for the native asset). The quote memo drives the swap; the gate binds
        the deposited coin/amount, memo, our signer, and that the memo pays dest.
        """
        return self._prepare_deposit(
            memo=quote.memo or "",
            amount=request.amount,
            mnemonic=mnemonic,
            now=now,
            destination=request.destination,
            expiry=quote.expiry,
            quote=quote,
            path=path,
            gas_limit=gas_limit,
        )

    def build_and_verify_native_deposit(
        self,
        *,
        memo: str,
        amount: int,
        mnemonic: str,
        now: int,
        expiry: int | None = None,
        path: str = DEFAULT_DERIVATION,
        gas_limit: int = DEFAULT_GAS_LIMIT,
    ) -> Prepared:
        """Build + gate a native ``MsgDeposit`` carrying ``memo`` (e.g. an LP add).

        Unlike :meth:`build_and_verify` there is no swap quote/destination — the
        gate binds the deposited coin/amount, the exact ``memo`` and our signer.
        Used for the protocol (RUNE/CACAO) leg of a symmetric liquidity add.
        """
        return self._prepare_deposit(
            memo=memo,
            amount=amount,
            mnemonic=mnemonic,
            now=now,
            destination="",  # LP add: no swap destination to bind in the memo
            expiry=expiry if expiry is not None else now + 3600,
            quote=None,
            path=path,
            gas_limit=gas_limit,
        )

    # --- sign + broadcast ---------------------------------------------------

    def sign(self, built: BuiltCosmosTx) -> list[str]:
        doc = cosmos_tx.sign_doc(
            built.body_bytes,
            built.auth_info_bytes,
            built.chain_id,
            built.account_number,
        )
        signature = cosmos_tx.sign_direct(built.private_key, doc)
        tx_raw = cosmos_tx.tx_raw(built.body_bytes, built.auth_info_bytes, signature)
        return [base64.b64encode(tx_raw).decode()]

    def broadcast(self, raws: list[str]) -> str:
        """Broadcast base64 TxRaw bytes via the cosmos REST endpoint; return txhash."""
        txhash = ""
        for tx_b64 in raws:
            resp = self._post(
                f"{self.node_url}/cosmos/tx/v1beta1/txs",
                json={"tx_bytes": tx_b64, "mode": "BROADCAST_MODE_SYNC"},
            )
            resp.raise_for_status()
            result = resp.json().get("tx_response", {})
            if result.get("code", 0) != 0:
                raise BroadcastError(
                    f"{self.chain} rejected tx (code {result.get('code')}): "
                    f"{result.get('raw_log')}"
                )
            txhash = result.get("txhash", "")
        return txhash
