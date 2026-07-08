"""TRON chain adapter for THORChain swaps.

Derives the Tron address from the seed and reads the TRX balance via the
standard java-tron HTTP API (keyless; defaults to a public node, overridable
with ``--tron-api``). A Tron address is the keccak-derived 20-byte account (same
as Ethereum) prefixed with 0x41 and base58check-encoded.

Spending FROM Tron (a source / liquidity-deposit adapter) builds a native
``TransferContract`` to the vault with the swap/LP memo in the tx ``data`` field,
signs it locally and broadcasts it. tronpy handles the protobuf tx; we only
override its block-reference lookup (``get_latest_solid_block_id``) because the
keyless public node returns an empty ``getnodeinfo`` — the tx ref block comes
from ``getnowblock`` instead. The pre-broadcast verify gate (``verify_tron_swap``)
checks vault, amount and memo before anything is signed.
"""

from __future__ import annotations

import dataclasses
import hashlib

from eth_account import Account
from eth_account.signers.local import LocalAccount

from swapsack.chains.base import BalanceReport
from swapsack.net import HttpClient
from swapsack.swap import BroadcastError, Prepared, SwapRequest
from swapsack.thorchain import Quote
from swapsack.verify import (
    TronSendPlan,
    TronSwapPlan,
    TronTokenSendPlan,
    TronTokenSwapPlan,
    verify_tron_send,
    verify_tron_swap,
    verify_tron_token_send,
    verify_tron_token_swap,
)

DEFAULT_TRON_DERIVATION = "m/44'/195'/0'/0/0"
# Keyless public node serving the standard java-tron HTTP API. TronGrid
# (api.trongrid.io) works too but rate-limits without an API key.
DEFAULT_TRON_API = "https://tron-rpc.publicnode.com"
TRON_MAINNET_PREFIX = 0x41
TRX_DECIMALS = 6
# THORChain represents every asset in 1e8 units; TRX is natively 1e6 (sun), so
# 1 sun = 100 of THORChain's 1e8 units.
THORCHAIN_UNITS_PER_SUN = 100
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# TRC-20 tokens the wallet tracks for `balance` (symbol, contract base58, decimals).
TRACKED_TOKENS = (("USDT", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6),)

# THORChain asset string -> (base58 contract, decimals). THORChain upper-cases the
# base58 contract in the asset string, which is lossy (base58 is case-sensitive),
# so we recover the real mixed-case contract from TRACKED_TOKENS. Used to spend a
# TRC-20 as a swap source.
_TRON_TOKENS = {
    f"TRON.{symbol}-{contract.upper()}": (contract, decimals)
    for symbol, contract, decimals in TRACKED_TOKENS
}

# ERC-20/TRC-20 transfer(address,uint256) selector + a minimal ABI so building a
# token transfer needs no on-chain ABI fetch.
TRC20_TRANSFER_SELECTOR = "a9059cbb"
_TRC20_TRANSFER_ABI = [
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "Nonpayable",
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "outputs": [{"type": "bool"}],
    }
]
# A TRC-20 deposit needs spare TRX for energy; cap what a malformed call can burn.
DEFAULT_TRC20_FEE_LIMIT_SUN = 15_000_000


def decode_trc20_transfer(call_data: str) -> tuple[str, int]:
    """Decode ``transfer(address,uint256)`` calldata to ``(to_base58, amount)``.

    Used to bind a built TRC-20 transfer back to the intended recipient/amount
    (the Phase 2 verify gate will reuse this, mirroring ``verify_eth_token_swap``).
    Raises :class:`ValueError` on a selector mismatch.
    """
    from tronpy.abi import trx_abi

    raw = bytes.fromhex(call_data.removeprefix("0x"))
    if raw[:4].hex() != TRC20_TRANSFER_SELECTOR:
        raise ValueError(
            f"selector {raw[:4].hex()} != transfer {TRC20_TRANSFER_SELECTOR}"
        )
    to_base58, amount = trx_abi.decode(["address", "uint256"], raw[4:])
    return to_base58, amount


Account.enable_unaudited_hdwallet_features()


@dataclasses.dataclass
class BuiltTronTx:
    """An unsigned TRON transfer plus the neutral fields the verify gate reads."""

    tx: object  # tronpy Transaction (online-built, carries its client for broadcast)
    priv: object  # tronpy PrivateKey for signing
    contract_type: str
    to_address: str  # base58: vault (native) or token contract (TRC-20 trigger)
    amount_sun: int  # native TRX value in sun; 0 for a TRC-20 transfer
    memo: str
    call_data: str = ""  # TriggerSmartContract calldata hex (TRC-20); "" for native


def _keyless_tron(api_url: str):  # noqa: ANN202 (tronpy Tron subclass, lazy import)
    from tronpy import Tron
    from tronpy.providers import HTTPProvider

    class _KeylessTron(Tron):
        # The keyless node's getnodeinfo is empty, which breaks tronpy's
        # get_latest_solid_block_id; use the latest block as the tx ref instead.
        def get_latest_solid_block_id(self) -> str:
            return self.provider.make_request("wallet/getnowblock")["blockID"]

    return _KeylessTron(provider=HTTPProvider(endpoint_uri=api_url))


def base58check_encode(payload: bytes) -> str:
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    data = payload + checksum
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, remainder = divmod(n, 58)
        out = _B58_ALPHABET[remainder] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


class TronAdapter(HttpClient):
    chain = "TRON"
    asset = "TRON.TRX"

    def __init__(
        self,
        api_url: str = DEFAULT_TRON_API,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(timeout)
        self.api_url = api_url.rstrip("/")
        self.bip39_passphrase = bip39_passphrase
        # tronpy clients spawned for tx-building each hold their own HTTP session;
        # track them so close() can release the sockets (they must outlive build
        # for broadcast, which always runs inside this adapter's context).
        self._tron_clients: list = []

    def _tron_client(self):  # noqa: ANN202 (tronpy Tron subclass, lazy import)
        client = _keyless_tron(self.api_url)
        self._tron_clients.append(client)
        return client

    def close(self) -> None:
        for client in self._tron_clients:
            client.provider.sess.close()
        self._tron_clients.clear()
        super().close()

    def _key(self, mnemonic: str, path: str) -> LocalAccount:
        return Account.from_mnemonic(
            mnemonic, passphrase=self.bip39_passphrase, account_path=path
        )

    def derive_address(self, mnemonic: str, path: str = DEFAULT_TRON_DERIVATION) -> str:
        addr20 = bytes.fromhex(self._key(mnemonic, path).address[2:])
        return base58check_encode(bytes([TRON_MAINNET_PREFIX]) + addr20)

    def fetch_balance(self, address: str) -> int:
        """Confirmed TRX balance in sun (1 TRX = 1e6 sun); 0 for unused accounts.

        Uses the standard java-tron ``/wallet/getaccount`` HTTP API, which is
        keyless and served by any full node (TronGrid, ``tron-rpc.publicnode.com``,
        a self-hosted node, …) — unlike TronGrid's proprietary ``/v1/accounts``
        indexed route, which other public nodes 404 on. ``visible: true`` makes
        the node accept and return base58 addresses. A fresh account returns
        ``{}`` and an activated-but-empty one omits ``balance``; both mean zero.
        """
        resp = self._post(
            f"{self.api_url}/wallet/getaccount",
            json={"address": address, "visible": True},
        )
        resp.raise_for_status()
        return int(resp.json().get("balance", 0))

    def wallet_balance(self, mnemonic: str) -> BalanceReport:
        address = self.derive_address(mnemonic)
        return BalanceReport(
            symbol="TRX",
            confirmed=self.fetch_balance(address),
            decimals=TRX_DECIMALS,
            note=f"({address})",
            addresses=(address,),
        )

    def fetch_token_balance(self, contract: str, address: str) -> int:
        """TRC-20 ``balanceOf(address)`` in the token's native units (keyless call).

        Uses the standard java-tron read-only ``/wallet/triggerconstantcontract``
        route. The owner is ABI-encoded as its 20-byte EVM form left-padded to 32
        bytes; an empty/failed call decodes as zero.
        """
        from tronpy.keys import to_hex_address

        owner_param = to_hex_address(address)[2:].rjust(64, "0")
        resp = self._post(
            f"{self.api_url}/wallet/triggerconstantcontract",
            json={
                "owner_address": address,
                "contract_address": contract,
                "function_selector": "balanceOf(address)",
                "parameter": owner_param,
                "visible": True,
            },
        )
        resp.raise_for_status()
        results = resp.json().get("constant_result") or []
        return int(results[0], 16) if results else 0

    def get_transaction_info(self, txid: str) -> dict:
        """Receipt for ``txid`` via ``/wallet/gettransactioninfobyid``.

        Returns ``{}`` until the tx is in a block; once mined it carries
        ``blockNumber`` and a ``receipt`` (``receipt.result == "SUCCESS"`` for a
        contract call that did not revert). Used to confirm a broadcast.
        """
        resp = self._post(
            f"{self.api_url}/wallet/gettransactioninfobyid", json={"value": txid}
        )
        resp.raise_for_status()
        return resp.json()

    def token_balances(self, mnemonic: str) -> list[BalanceReport]:
        """TRC-20 balances (e.g. USDT-TRON) at the wallet's Tron address."""
        address = self.derive_address(mnemonic)
        return [
            BalanceReport(
                symbol=f"{symbol}-TRON",
                confirmed=self.fetch_token_balance(contract, address),
                decimals=decimals,
                addresses=(address,),
            )
            for symbol, contract, decimals in TRACKED_TOKENS
        ]

    # --- spending FROM Tron (swap source + liquidity deposit) ---------------

    @staticmethod
    def to_sun(amount_thorchain: int) -> int:
        """Convert a THORChain 1e8 amount to native sun; reject sub-sun dust."""
        sun, remainder = divmod(amount_thorchain, THORCHAIN_UNITS_PER_SUN)
        if remainder:
            raise ValueError(
                f"amount {amount_thorchain} (1e8 units) is not a whole number of "
                f"sun; TRX precision is 1e6"
            )
        return sun

    @staticmethod
    def token_contract_and_decimals(from_asset: str) -> tuple[str, int]:
        """Look up a TRON token's mixed-case base58 contract and decimals.

        Keyed by the THORChain asset string (which upper-cases the contract).
        Raises :class:`ValueError` for a non-token or unknown asset.
        """
        try:
            return _TRON_TOKENS[from_asset]
        except KeyError:
            raise ValueError(
                f"{from_asset} is not a supported TRON token source"
            ) from None

    @staticmethod
    def to_token_native(amount_thorchain: int, decimals: int) -> int:
        """Convert a THORChain 1e8 amount to a token's native units; reject dust.

        THORChain quotes every asset in 1e8 units; a token with ``decimals`` base
        units has ``10**(8-decimals)`` of them per native unit. Rejects an amount
        that isn't a whole number of the token's base unit.
        """
        if decimals > 8:
            raise ValueError(f"token with {decimals} decimals (> 8) not supported")
        factor = 10 ** (8 - decimals)
        native, remainder = divmod(amount_thorchain, factor)
        if remainder:
            raise ValueError(
                f"amount {amount_thorchain} (1e8 units) is not a whole number of "
                f"the token's {decimals}-decimal base unit"
            )
        return native

    def build_unsigned_transfer(
        self,
        *,
        mnemonic: str,
        to: str,
        amount_sun: int,
        memo: str,
        path: str = DEFAULT_TRON_DERIVATION,
    ) -> BuiltTronTx:
        """Build (but do not sign) a TransferContract to ``to`` carrying ``memo``.

        Hits the node to fetch the ref block; signing/broadcast happen later,
        only after the verify gate passes.
        """
        from tronpy.keys import PrivateKey, to_base58check_address

        priv = PrivateKey(bytes(self._key(mnemonic, path).key))
        owner = priv.public_key.to_base58check_address()
        builder = self._tron_client().trx.transfer(owner, to, amount_sun)
        if memo:
            builder = builder.memo(memo)
        tx = builder.build()
        contract = tx._raw_data["contract"][0]
        value = contract["parameter"]["value"]
        data = tx._raw_data.get("data")
        return BuiltTronTx(
            tx=tx,
            priv=priv,
            contract_type=contract["type"],
            to_address=to_base58check_address(value["to_address"]),
            amount_sun=value["amount"],
            memo=bytes.fromhex(data).decode() if data else "",
        )

    def build_unsigned_trc20_transfer(
        self,
        *,
        mnemonic: str,
        token: str,
        to: str,
        amount: int,
        memo: str,
        fee_limit_sun: int = DEFAULT_TRC20_FEE_LIMIT_SUN,
        path: str = DEFAULT_TRON_DERIVATION,
    ) -> BuiltTronTx:
        """Build (but do not sign) a TRC-20 ``transfer(to, amount)`` carrying ``memo``.

        This is the Phase 2 USDT-TRON deposit primitive: a ``TriggerSmartContract``
        on the token, with the swap memo in the tx data field (where THORChain
        reads it — TRON has no router contract). ``amount`` is in the token's
        native units. Like the native builder it only hits the node for the ref
        block; signing/broadcast happen later, after the verify gate passes.
        """
        from tronpy.contract import Contract
        from tronpy.keys import PrivateKey, to_base58check_address

        priv = PrivateKey(bytes(self._key(mnemonic, path).key))
        owner = priv.public_key.to_base58check_address()
        cntr = Contract(addr=token, abi=_TRC20_TRANSFER_ABI, client=self._tron_client())
        builder = (
            cntr.functions.transfer(to, amount)
            .with_owner(owner)
            .fee_limit(fee_limit_sun)
        )
        if memo:
            builder = builder.memo(memo)
        tx = builder.build()
        contract = tx._raw_data["contract"][0]
        value = contract["parameter"]["value"]
        data = tx._raw_data.get("data")
        return BuiltTronTx(
            tx=tx,
            priv=priv,
            contract_type=contract["type"],
            to_address=to_base58check_address(value["contract_address"]),
            amount_sun=0,
            memo=bytes.fromhex(data).decode() if data else "",
            call_data=value["data"],
        )

    def _build_and_verify(
        self,
        *,
        to: str,
        amount_sun: int,
        memo: str,
        expiry: int,
        destination: str,
        now: int,
        mnemonic: str,
        quote: Quote | None,
    ) -> Prepared:
        built = self.build_unsigned_transfer(
            mnemonic=mnemonic, to=to, amount_sun=amount_sun, memo=memo
        )
        plan = TronSwapPlan(
            inbound_address=to,
            amount_sun=amount_sun,
            memo=memo,
            expiry=expiry,
            destination=destination,
        )
        problems = verify_tron_swap(
            contract_type=built.contract_type,
            to_address=built.to_address,
            amount_sun=built.amount_sun,
            memo=built.memo,
            plan=plan,
            now=now,
        )
        return Prepared(quote=quote, built=built, plan=plan, problems=problems)

    def _build_and_verify_token(
        self, *, quote: Quote, request: SwapRequest, now: int, mnemonic: str
    ) -> Prepared:
        """Build + gate a TRC-20 deposit (e.g. USDT-TRON) to the vault.

        Routerless: a plain ``transfer(vault, amount)`` on the token with the swap
        memo in the tx data. The gate decodes the built calldata and binds the
        token contract, recipient, amount and memo — a mistake here is an
        unrefundable loss.
        """
        token, decimals = self.token_contract_and_decimals(request.from_asset)
        native = self.to_token_native(request.amount, decimals)
        vault = quote.inbound_address
        memo = quote.memo or ""
        built = self.build_unsigned_trc20_transfer(
            mnemonic=mnemonic, token=token, to=vault, amount=native, memo=memo
        )
        recipient, transfer_amount = decode_trc20_transfer(built.call_data)
        plan = TronTokenSwapPlan(
            inbound_address=vault,
            token=token,
            amount=native,
            memo=memo,
            expiry=quote.expiry,
            destination=request.destination,
        )
        problems = verify_tron_token_swap(
            contract_type=built.contract_type,
            trigger_to=built.to_address,
            recipient=recipient,
            transfer_amount=transfer_amount,
            trx_value=built.amount_sun,
            memo=built.memo,
            plan=plan,
            now=now,
        )
        return Prepared(quote=quote, built=built, plan=plan, problems=problems)

    def build_and_verify(
        self, *, quote: Quote, request: SwapRequest, now: int, mnemonic: str
    ) -> Prepared:
        if "-" in request.from_asset:  # TRC-20 token source (e.g. USDT-TRON)
            return self._build_and_verify_token(
                quote=quote, request=request, now=now, mnemonic=mnemonic
            )
        return self._build_and_verify(
            to=quote.inbound_address,
            amount_sun=self.to_sun(request.amount),
            memo=quote.memo or "",
            expiry=quote.expiry,
            destination=request.destination,
            now=now,
            mnemonic=mnemonic,
            quote=quote,
        )

    def build_and_verify_send(
        self, *, recipient: str, amount: int, asset: str, mnemonic: str
    ) -> Prepared:
        """Build + verify a plain external send (no swap, no memo).

        ``amount`` is in THORChain 1e8 base units. For a TRC-20 (``asset`` like
        ``TRON.USDT-...``) this is a routerless ``transfer(recipient, amount)``
        with no memo in the tx data; for native TRX it is a ``TransferContract``.
        A wrong recipient is irreversible, so recipient/amount are bound by the
        verify gate before signing.
        """
        if "-" in asset:  # TRC-20 token send
            token, decimals = self.token_contract_and_decimals(asset)
            native = self.to_token_native(amount, decimals)
            built = self.build_unsigned_trc20_transfer(
                mnemonic=mnemonic, token=token, to=recipient, amount=native, memo=""
            )
            d_recipient, transfer_amount = decode_trc20_transfer(built.call_data)
            plan = TronTokenSendPlan(token=token, recipient=recipient, amount=native)
            problems = verify_tron_token_send(
                contract_type=built.contract_type,
                trigger_to=built.to_address,
                recipient=d_recipient,
                transfer_amount=transfer_amount,
                trx_value=built.amount_sun,
                memo=built.memo,
                plan=plan,
            )
            return Prepared(quote=None, built=built, plan=plan, problems=problems)

        amount_sun = self.to_sun(amount)
        built = self.build_unsigned_transfer(
            mnemonic=mnemonic, to=recipient, amount_sun=amount_sun, memo=""
        )
        plan = TronSendPlan(recipient=recipient, amount_sun=amount_sun)
        problems = verify_tron_send(
            contract_type=built.contract_type,
            to_address=built.to_address,
            amount_sun=built.amount_sun,
            memo=built.memo,
            plan=plan,
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

    def build_and_verify_deposit(
        self, *, vault: str, memo: str, amount: int, now: int, mnemonic: str
    ) -> Prepared:
        return self._build_and_verify(
            to=vault,
            amount_sun=self.to_sun(amount),
            memo=memo,
            expiry=now + 3600,
            destination="",
            now=now,
            mnemonic=mnemonic,
            quote=None,
        )

    def sign(self, built: BuiltTronTx) -> list:  # noqa: ANN201 (list of tronpy tx)
        return [built.tx.sign(built.priv)]

    def broadcast(self, raws: list) -> str:  # noqa: ANN001 (list of tronpy tx)
        from tronpy.exceptions import (
            ApiError,
            TaposError,
            TransactionError,
            TvmError,
            UnknownError,
            ValidationError,
        )

        tron_errors = (
            ApiError,
            TaposError,
            TransactionError,
            TvmError,
            UnknownError,
            ValidationError,
        )
        txid = ""
        for tx in raws:
            try:
                tx.broadcast()
            except tron_errors as exc:
                msg = str(exc)
                if "not sufficient" in msg.lower():
                    msg += (
                        " — a TRON transfer also needs spare TRX for the network fee "
                        "(bandwidth/energy), which is NOT part of the sent amount; "
                        "leave some TRX headroom below your balance (~1 TRX)."
                    )
                raise BroadcastError(msg) from exc
            txid = tx.txid
        return txid
