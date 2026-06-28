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

from cryptoswap_wallet.chains.base import BalanceReport
from cryptoswap_wallet.net import HttpClient
from cryptoswap_wallet.swap import BroadcastError, Prepared, SwapRequest
from cryptoswap_wallet.thorchain import Quote
from cryptoswap_wallet.verify import TronSwapPlan, verify_tron_swap

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

Account.enable_unaudited_hdwallet_features()


@dataclasses.dataclass
class BuiltTronTx:
    """An unsigned TRON transfer plus the neutral fields the verify gate reads."""

    tx: object  # tronpy Transaction (online-built, carries its client for broadcast)
    priv: object  # tronpy PrivateKey for signing
    contract_type: str
    to_address: str  # base58
    amount_sun: int
    memo: str


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

    def __init__(self, api_url: str = DEFAULT_TRON_API, timeout: float = 20.0) -> None:
        super().__init__(timeout)
        self.api_url = api_url.rstrip("/")

    def _key(self, mnemonic: str, path: str) -> LocalAccount:
        return Account.from_mnemonic(mnemonic, account_path=path)

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
        )

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
        builder = _keyless_tron(self.api_url).trx.transfer(owner, to, amount_sun)
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

    def build_and_verify(
        self, *, quote: Quote, request: SwapRequest, now: int, mnemonic: str
    ) -> Prepared:
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
