"""Ethereum chain adapter (native ETH) for THORChain swaps.

Derivation and signing use eth-account; chain state and broadcast use JSON-RPC.
A native ETH deposit goes directly to the inbound vault with the THORChain memo
hex-encoded in the transaction's calldata (the router is only needed for tokens).

build_unsigned_swap is pure given nonce/gas/fees (so it is unit-testable); the
caller fetches those over RPC. Amounts are THORChain 1e8 base units, converted
to wei via WEI_PER_THORCHAIN_UNIT.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from Crypto.Hash import keccak
from eth_account import Account
from eth_account.signers.local import LocalAccount

from cryptoswap.chains.base import BalanceReport
from cryptoswap.chains.coins import InsufficientFunds
from cryptoswap.net import HttpClient
from cryptoswap.swap import Prepared, SwapRequest
from cryptoswap.thorchain import Quote
from cryptoswap.verify import WEI_PER_THORCHAIN_UNIT, EthSwapPlan, verify_eth_swap

DEFAULT_ETH_DERIVATION = "m/44'/60'/0'/0/0"
DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"
CHAIN_ID = 1
DEFAULT_GAS = 60000

Account.enable_unaudited_hdwallet_features()


def _keccak256(data: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def eth_sweep_amount(balance_wei: int, gas: int, max_fee_per_gas: int) -> int:
    """THORChain 1e8 amount sweeping the balance minus the worst-case gas reserve.

    Reserves ``gas * max_fee_per_gas`` so the deposit always leaves enough wei to
    pay the L1 fee; any sub-1e10-wei remainder is left behind.
    """
    sendable = balance_wei - gas * max_fee_per_gas
    if sendable <= 0:
        raise InsufficientFunds(
            f"balance {balance_wei} wei too small to cover gas reserve "
            f"{gas * max_fee_per_gas}"
        )
    return sendable // WEI_PER_THORCHAIN_UNIT


def to_checksum_address(addr: bytes | str) -> str:
    """EIP-55 checksum encoding of a 20-byte address (bytes or hex string)."""
    if isinstance(addr, str):
        addr = bytes.fromhex(addr.removeprefix("0x"))
    lower = addr.hex()
    digest = _keccak256(lower.encode()).hex()
    encoded = "".join(
        c.upper() if c.isalpha() and int(d, 16) >= 8 else c
        for c, d in zip(lower, digest, strict=False)
    )
    return "0x" + encoded


@dataclasses.dataclass
class EthBuiltSwap:
    tx: dict[str, Any]
    private_key: Any
    to: str
    value: int
    data: str
    chain_id: int
    gas: int
    max_fee_per_gas: int

    @property
    def fee(self) -> int:
        return self.gas * self.max_fee_per_gas


class EthAdapter(HttpClient):
    """ChainAdapter for native Ethereum."""

    chain = "ETH"
    asset = "ETH.ETH"

    def __init__(self, rpc_url: str = DEFAULT_RPC, timeout: float = 20.0) -> None:
        super().__init__(timeout)
        self.rpc_url = rpc_url

    def _key(self, mnemonic: str, path: str) -> LocalAccount:
        return Account.from_mnemonic(mnemonic, account_path=path)

    def derive_address(self, mnemonic: str, path: str = DEFAULT_ETH_DERIVATION) -> str:
        return self._key(mnemonic, path).address

    # --- JSON-RPC ---

    def _rpc(self, method: str, params: list[object]) -> object:
        resp = self._post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            raise RuntimeError(f"RPC {method}: {payload['error']}")
        return payload["result"]

    def get_nonce(self, address: str) -> int:
        return int(self._rpc("eth_getTransactionCount", [address, "pending"]), 16)

    def fetch_balance(self, address: str) -> int:
        return int(self._rpc("eth_getBalance", [address, "latest"]), 16)

    def wallet_balance(self, mnemonic: str) -> BalanceReport:
        address = self.derive_address(mnemonic)
        return BalanceReport(
            symbol="ETH",
            confirmed=self.fetch_balance(address),
            decimals=18,
            note=f"({address})",
        )

    def fetch_fees(self) -> tuple[int, int]:
        """Return ``(max_fee_per_gas, max_priority_fee_per_gas)`` in wei."""
        tip = int(self._rpc("eth_maxPriorityFeePerGas", []), 16)
        block = self._rpc("eth_getBlockByNumber", ["latest", False])
        base = int(block["baseFeePerGas"], 16)
        return base * 2 + tip, tip

    def broadcast(self, raw_hex: str) -> str:
        return self._rpc("eth_sendRawTransaction", [raw_hex])

    def build_unsigned_swap(
        self,
        *,
        mnemonic: str,
        vault_address: str,
        amount: int,
        memo: str,
        nonce: int,
        gas: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        path: str = DEFAULT_ETH_DERIVATION,
    ) -> EthBuiltSwap:
        account = self._key(mnemonic, path)
        to = to_checksum_address(vault_address)
        value = amount * WEI_PER_THORCHAIN_UNIT
        data = "0x" + memo.encode().hex()
        tx = {
            "type": 2,
            "chainId": CHAIN_ID,
            "nonce": nonce,
            "to": to,
            "value": value,
            "gas": gas,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
            "data": data,
        }
        return EthBuiltSwap(
            tx=tx,
            private_key=account.key,
            to=to,
            value=value,
            data=data,
            chain_id=CHAIN_ID,
            gas=gas,
            max_fee_per_gas=max_fee_per_gas,
        )

    def sign(self, built: EthBuiltSwap) -> str:
        signed = Account.sign_transaction(built.tx, built.private_key)
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:
            raw = signed.rawTransaction
        return "0x" + raw.hex()

    def build_and_verify(
        self,
        *,
        quote: Quote,
        request: SwapRequest,
        now: int,
        mnemonic: str,
        nonce: int,
        gas: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        max_fee_wei: int,
    ) -> Prepared:
        built = self.build_unsigned_swap(
            mnemonic=mnemonic,
            vault_address=quote.inbound_address,
            amount=request.amount,
            memo=quote.memo or "",
            nonce=nonce,
            gas=gas,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
        )
        plan = EthSwapPlan(
            inbound_address=quote.inbound_address,
            amount_wei=request.amount * WEI_PER_THORCHAIN_UNIT,
            memo=quote.memo or "",
            expiry=quote.expiry,
            destination=request.destination,
        )
        problems = verify_eth_swap(
            to=built.to,
            value=built.value,
            data=built.data,
            chain_id=built.chain_id,
            gas=built.gas,
            max_fee_per_gas=built.max_fee_per_gas,
            plan=plan,
            now=now,
            max_fee_wei=max_fee_wei,
        )
        return Prepared(quote=quote, built=built, plan=plan, problems=problems)
