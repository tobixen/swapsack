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
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.signers.local import LocalAccount

from swapsack.chains.base import BalanceReport
from swapsack.chains.coins import InsufficientFunds
from swapsack.net import HttpClient
from swapsack.swap import BroadcastError, Prepared, SwapAborted, SwapRequest
from swapsack.thorchain import Quote
from swapsack.verify import (
    WEI_PER_THORCHAIN_UNIT,
    EthSendPlan,
    EthSwapPlan,
    EthTokenSendPlan,
    memo_pays_destination,
    verify_eth_send,
    verify_eth_swap,
    verify_eth_token_send,
)

DEFAULT_ETH_DERIVATION = "m/44'/60'/0'/0/0"
DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"
CHAIN_ID = 1
DEFAULT_GAS = 60000

# ERC-20 token source: approve(router, amount) then router.depositWithExpiry(...).
APPROVE_SELECTOR = "095ea7b3"  # approve(address,uint256)
DEPOSIT_SELECTOR = (
    "44bc937b"  # depositWithExpiry(address,address,uint256,string,uint256)
)
DECIMALS_SELECTOR = "313ce567"  # decimals()
BALANCEOF_SELECTOR = "70a08231"  # balanceOf(address)
TRANSFER_SELECTOR = "a9059cbb"  # transfer(address,uint256) — plain ERC-20 send
APPROVE_GAS = 70000
TOKEN_DEPOSIT_GAS = 200000
# Plain external sends: a bare value transfer is 21000; an ERC-20 transfer() is
# ~50-65k. These are used by `send` (no router/approve), not the swap path.
NATIVE_SEND_GAS = 21000
TOKEN_TRANSFER_GAS = 65000

# ERC-20 tokens the wallet tracks for `balance` (symbol, contract, decimals).
TRACKED_TOKENS = (
    ("USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
    ("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
)

# Known token decimals — don't trust an RPC value that determines how much we
# send. Derived from TRACKED_TOKENS so the contract address is listed once.
KNOWN_TOKEN_DECIMALS = {contract: decimals for _, contract, decimals in TRACKED_TOKENS}

Account.enable_unaudited_hdwallet_features()


def encode_approve(router: str, amount: int) -> str:
    return (
        "0x"
        + APPROVE_SELECTOR
        + abi_encode(["address", "uint256"], [router, amount]).hex()
    )


def encode_transfer(to: str, amount: int) -> str:
    """ERC-20 ``transfer(to, amount)`` calldata — a plain send (no router)."""
    return (
        "0x"
        + TRANSFER_SELECTOR
        + abi_encode(["address", "uint256"], [to, amount]).hex()
    )


def encode_deposit(vault: str, token: str, amount: int, memo: str, expiry: int) -> str:
    args = abi_encode(
        ["address", "address", "uint256", "string", "uint256"],
        [vault, token, amount, memo, expiry],
    )
    return "0x" + DEPOSIT_SELECTOR + args.hex()


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
        if addr[:2].lower() == "0x":  # accept 0x or 0X (THORChain uppercases)
            addr = addr[2:]
        addr = bytes.fromhex(addr)
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

    @property
    def txs(self) -> list[dict[str, Any]]:
        return [self.tx]


@dataclasses.dataclass
class EthTokenBuiltSwap:
    """An ERC-20 token swap: approve(router) then router.depositWithExpiry(...)."""

    approve_tx: dict[str, Any]
    deposit_tx: dict[str, Any]
    private_key: Any
    token: str
    router: str
    vault: str
    native_amount: int
    memo: str
    expiry: int
    chain_id: int = CHAIN_ID

    @property
    def txs(self) -> list[dict[str, Any]]:
        return [self.approve_tx, self.deposit_tx]

    @property
    def fee(self) -> int:
        return sum(t["gas"] * t["maxFeePerGas"] for t in self.txs)


def _decode_call(data: str, selector: str, types: list[str]) -> tuple[Any, ...]:
    """Split a 0x calldata into (selector, decoded args); raise on selector mismatch."""
    raw = bytes.fromhex(data.removeprefix("0x"))
    if raw[:4].hex() != selector:
        raise ValueError(f"selector {raw[:4].hex()} != expected {selector}")
    return tuple(abi_decode(types, raw[4:]))


def verify_eth_token_swap(
    *, built: EthTokenBuiltSwap, destination: str, now: int, max_fee_wei: int
) -> list[str]:
    """Gate for an ERC-20 token deposit (approve + router.depositWithExpiry).

    Decodes the calldata positionally (not substring containment) and binds every
    field — including the **amount** on both txs — to the intended values.
    """
    problems: list[str] = []
    approve, deposit = built.approve_tx, built.deposit_tx

    if now >= built.expiry:
        problems.append(f"quote expired (now {now} >= expiry {built.expiry})")
    if approve["to"].lower() != built.token.lower():
        problems.append(f"approve 'to' {approve['to']} != token {built.token}")
    if deposit["to"].lower() != built.router.lower():
        problems.append(f"deposit 'to' {deposit['to']} != router {built.router}")
    if approve["value"] != 0 or deposit["value"] != 0:
        problems.append("token txs must not send ETH value")
    if approve["chainId"] != built.chain_id or deposit["chainId"] != built.chain_id:
        problems.append("wrong chainId")

    try:
        spender, allowance = _decode_call(
            approve["data"], APPROVE_SELECTOR, ["address", "uint256"]
        )
    except Exception:  # noqa: BLE001 - any decode failure is a reject
        problems.append("approve calldata could not be decoded")
    else:
        if spender.lower() != built.router.lower():
            problems.append(f"approve spender {spender} != router {built.router}")
        if allowance != built.native_amount:
            problems.append(f"approve amount {allowance} != {built.native_amount}")

    try:
        d_vault, d_token, d_amount, d_memo, d_expiry = _decode_call(
            deposit["data"],
            DEPOSIT_SELECTOR,
            ["address", "address", "uint256", "string", "uint256"],
        )
    except Exception:  # noqa: BLE001 - any decode failure is a reject
        problems.append("deposit calldata could not be decoded")
    else:
        if d_vault.lower() != built.vault.lower():
            problems.append(f"deposit vault {d_vault} != {built.vault}")
        if d_token.lower() != built.token.lower():
            problems.append(f"deposit token {d_token} != {built.token}")
        if d_amount != built.native_amount:
            problems.append(f"deposit amount {d_amount} != {built.native_amount}")
        if d_memo != built.memo:
            problems.append(f"deposit memo {d_memo!r} != {built.memo!r}")
        if d_expiry != built.expiry:
            problems.append(f"deposit expiry {d_expiry} != {built.expiry}")
        if not memo_pays_destination(destination, d_memo):
            problems.append(f"memo {d_memo!r} does not pay destination {destination}")

    if built.fee > max_fee_wei:
        problems.append(f"max fee {built.fee} wei exceeds limit {max_fee_wei}")
    return problems


class EthAdapter(HttpClient):
    """ChainAdapter for native Ethereum.

    The chain-specific surface (chain/asset/native symbol, the token balance
    label suffix, and the tracked-token table) is exposed as class attributes so
    other EVM chains — which share derivation, JSON-RPC and balance mechanics —
    can subclass and override only what differs (see ``chains.bsc``).
    """

    chain = "ETH"
    asset = "ETH.ETH"
    native_symbol = "ETH"
    token_suffix = "ETH"  # balance label suffix, e.g. "USDC-ETH"
    tracked_tokens = TRACKED_TOKENS
    known_token_decimals = KNOWN_TOKEN_DECIMALS

    def __init__(
        self,
        rpc_url: str = DEFAULT_RPC,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
        chain_id: int = CHAIN_ID,
    ) -> None:
        super().__init__(timeout)
        self.rpc_url = rpc_url
        self.bip39_passphrase = bip39_passphrase
        # EVM chain id used when building/signing txs (1 = mainnet). Set to a
        # testnet id (e.g. Sepolia 11155111) alongside a matching RPC to send on
        # a testnet.
        self.chain_id = chain_id

    def _key(self, mnemonic: str, path: str) -> LocalAccount:
        return Account.from_mnemonic(
            mnemonic, passphrase=self.bip39_passphrase, account_path=path
        )

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
        if "result" not in payload:
            raise RuntimeError(
                f"RPC {method}: malformed response (no result): {payload!r}"
            )
        return payload["result"]

    def get_nonce(self, address: str) -> int:
        return int(self._rpc("eth_getTransactionCount", [address, "pending"]), 16)

    def fetch_balance(self, address: str) -> int:
        return int(self._rpc("eth_getBalance", [address, "latest"]), 16)

    def fetch_token_decimals(self, token: str) -> int:
        result = self._rpc(
            "eth_call", [{"to": token, "data": "0x" + DECIMALS_SELECTOR}, "latest"]
        )
        return int(result, 16)

    def token_decimals(self, token: str) -> int:
        """Decimals for a token: a trusted constant for known tokens, else RPC.

        The value scales how much we send, so we don't trust RPC for tokens we
        already know (e.g. USDT = 6).
        """
        key = "0x" + token.lower().removeprefix("0x")
        known = self.known_token_decimals.get(key)
        return known if known is not None else self.fetch_token_decimals(token)

    def wallet_balance(self, mnemonic: str) -> BalanceReport:
        address = self.derive_address(mnemonic)
        return BalanceReport(
            symbol=self.native_symbol,
            confirmed=self.fetch_balance(address),
            decimals=18,
            note=f"({address})",
            addresses=(address,),
        )

    def fetch_token_balance(self, token: str, address: str) -> int:
        """ERC-20 ``balanceOf(address)`` in the token's native units."""
        owner = to_checksum_address(address)[2:].lower()
        data = "0x" + BALANCEOF_SELECTOR + owner.rjust(64, "0")
        return int(self._rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)

    def token_balances(self, mnemonic: str) -> list[BalanceReport]:
        """ERC-20 balances (e.g. USDT-ETH) at the wallet's EVM address."""
        address = self.derive_address(mnemonic)
        return [
            BalanceReport(
                symbol=f"{symbol}-{self.token_suffix}",
                confirmed=self.fetch_token_balance(contract, address),
                decimals=decimals,
                addresses=(address,),
            )
            for symbol, contract, decimals in self.tracked_tokens
        ]

    def fetch_fees(self) -> tuple[int, int]:
        """Return ``(max_fee_per_gas, max_priority_fee_per_gas)`` in wei."""
        tip = int(self._rpc("eth_maxPriorityFeePerGas", []), 16)
        block = self._rpc("eth_getBlockByNumber", ["latest", False])
        base = int(block["baseFeePerGas"], 16)
        return base * 2 + tip, tip

    def broadcast(self, raws: list[str]) -> str:
        txid = ""
        for raw in raws:
            # A JSON-RPC rejection (nonce too low, intrinsic gas, …) comes back
            # HTTP 200 with an `error` body, which _rpc raises as a bare
            # RuntimeError. Wrap it as BroadcastError so the CLI reports it
            # cleanly instead of crashing — especially important for a token
            # swap, where the approve tx may already be on-chain.
            try:
                txid = self._rpc("eth_sendRawTransaction", [raw])
            except RuntimeError as exc:
                raise BroadcastError(str(exc)) from exc
        return str(txid)

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
            "chainId": self.chain_id,
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
            chain_id=self.chain_id,
            gas=gas,
            max_fee_per_gas=max_fee_per_gas,
        )

    def _sign_tx(self, tx: dict[str, Any], private_key: object) -> str:
        signed = Account.sign_transaction(tx, private_key)
        raw = getattr(signed, "raw_transaction", None)
        if raw is None:
            raw = signed.rawTransaction
        return "0x" + raw.hex()

    def sign(self, built: EthBuiltSwap | EthTokenBuiltSwap) -> list[str]:
        return [self._sign_tx(tx, built.private_key) for tx in built.txs]

    def _build_token_deposit(
        self,
        *,
        account: LocalAccount,
        token: str,
        router: str,
        vault: str,
        native: int,
        memo: str,
        expiry: int,
        nonce: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
    ) -> EthTokenBuiltSwap:
        """Build the approve + ``router.depositWithExpiry`` pair for any ERC-20
        deposit to a THORChain/Maya vault — a token swap *or* a token LP add.

        ``memo`` is the deposit memo (``=:…`` for a swap, ``+:POOL`` for LP);
        amounts are already in the token's native units.
        """
        token = to_checksum_address(token)
        router = to_checksum_address(router)
        vault = to_checksum_address(vault)
        common = {
            "type": 2,
            "chainId": self.chain_id,
            "value": 0,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
        }
        approve_tx = {
            **common,
            "nonce": nonce,
            "to": token,
            "gas": APPROVE_GAS,
            "data": encode_approve(router, native),
        }
        deposit_tx = {
            **common,
            "nonce": nonce + 1,
            "to": router,
            "gas": TOKEN_DEPOSIT_GAS,
            "data": encode_deposit(vault, token, native, memo, expiry),
        }
        return EthTokenBuiltSwap(
            approve_tx=approve_tx,
            deposit_tx=deposit_tx,
            private_key=account.key,
            token=token,
            router=router,
            vault=vault,
            native_amount=native,
            memo=memo,
            expiry=expiry,
            chain_id=self.chain_id,
        )

    def build_token_swap(
        self,
        *,
        mnemonic: str,
        request: SwapRequest,
        quote: Quote,
        nonce: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        decimals: int,
    ) -> EthTokenBuiltSwap:
        return self._build_token_deposit(
            account=self._key(mnemonic, DEFAULT_ETH_DERIVATION),
            token=request.from_asset.split("-", 1)[1],
            router=quote.router or "",
            vault=quote.inbound_address,
            # THORChain 1e8 units -> the token's native decimals.
            native=request.amount * 10**decimals // 10**8,
            memo=quote.memo or "",
            expiry=quote.expiry,
            nonce=nonce,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
        )

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
        if "-" in request.from_asset:  # ERC-20 token source
            built_token = self.build_token_swap(
                mnemonic=mnemonic,
                request=request,
                quote=quote,
                nonce=nonce,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
                decimals=self.token_decimals(request.from_asset.split("-", 1)[1]),
            )
            problems = verify_eth_token_swap(
                built=built_token,
                destination=request.destination,
                now=now,
                max_fee_wei=max_fee_wei,
            )
            return Prepared(
                quote=quote, built=built_token, plan=built_token, problems=problems
            )

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

    def build_and_verify_send(
        self,
        *,
        recipient: str,
        amount: int,
        asset: str,
        mnemonic: str,
        nonce: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        max_fee_wei: int,
        path: str = DEFAULT_ETH_DERIVATION,
    ) -> Prepared:
        """Build + verify a plain external send (no swap, no memo, no router).

        ``amount`` is in THORChain 1e8 base units. For an ERC-20 (``asset`` like
        ``ETH.USDT-0x...``) this is a single ``transfer(recipient, amount)`` on
        the token — no approve, no router. A wrong recipient is irreversible, so
        the recipient/amount are bound by the verify gate before signing.
        """
        if "-" in asset:  # ERC-20 token send
            return self._build_and_verify_token_send(
                recipient=recipient,
                amount=amount,
                asset=asset,
                mnemonic=mnemonic,
                nonce=nonce,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
                max_fee_wei=max_fee_wei,
                path=path,
            )
        built = self.build_unsigned_swap(
            mnemonic=mnemonic,
            vault_address=recipient,
            amount=amount,
            memo="",  # a plain send carries no memo -> empty calldata
            nonce=nonce,
            gas=NATIVE_SEND_GAS,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
            path=path,
        )
        plan = EthSendPlan(
            recipient=built.to,
            amount_wei=amount * WEI_PER_THORCHAIN_UNIT,
            chain_id=self.chain_id,
        )
        problems = verify_eth_send(
            to=built.to,
            value=built.value,
            data=built.data,
            chain_id=built.chain_id,
            gas=built.gas,
            max_fee_per_gas=built.max_fee_per_gas,
            plan=plan,
            max_fee_wei=max_fee_wei,
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

    def _build_and_verify_token_send(
        self,
        *,
        recipient: str,
        amount: int,
        asset: str,
        mnemonic: str,
        nonce: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        max_fee_wei: int,
        path: str,
    ) -> Prepared:
        account = self._key(mnemonic, path)
        token = to_checksum_address(asset.split("-", 1)[1])
        to = to_checksum_address(recipient)
        decimals = self.token_decimals(token)
        native = amount * 10**decimals // 10**8
        data = encode_transfer(to, native)
        tx = {
            "type": 2,
            "chainId": self.chain_id,
            "nonce": nonce,
            "to": token,
            "value": 0,
            "gas": TOKEN_TRANSFER_GAS,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
            "data": data,
        }
        built = EthBuiltSwap(
            tx=tx,
            private_key=account.key,
            to=token,
            value=0,
            data=data,
            chain_id=self.chain_id,
            gas=TOKEN_TRANSFER_GAS,
            max_fee_per_gas=max_fee_per_gas,
        )
        plan = EthTokenSendPlan(
            token=token, recipient=to, amount=native, chain_id=self.chain_id
        )
        try:
            d_recipient, d_amount = _decode_call(
                data, TRANSFER_SELECTOR, ["address", "uint256"]
            )
        except Exception:  # noqa: BLE001 - any decode failure is a reject
            return Prepared(
                quote=None,
                built=built,
                plan=plan,
                problems=["transfer calldata could not be decoded"],
            )
        problems = verify_eth_token_send(
            to=built.to,
            value=built.value,
            chain_id=built.chain_id,
            recipient=d_recipient,
            transfer_amount=d_amount,
            gas=built.gas,
            max_fee_per_gas=built.max_fee_per_gas,
            plan=plan,
            max_fee_wei=max_fee_wei,
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

    def build_and_verify_deposit(
        self,
        *,
        vault: str,
        memo: str,
        amount: int,
        now: int,
        mnemonic: str,
        nonce: int,
        gas: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        max_fee_wei: int,
        router: str | None = None,
        token: str | None = None,
    ) -> Prepared:
        # An ERC-20 LP *add* (memo "+:ETH.USDT-0x…") is a token deposit: approve +
        # router.depositWithExpiry, exactly like a token swap but with the LP memo
        # and no destination to bind. Needs the backend's ETH router. The caller
        # passes the ``token`` contract explicitly (it knows the asset); parsing
        # it out of the memo would break on a symmetric add memo, whose
        # ":<paired_address>" suffix follows the pool. A *withdraw* ("-:POOL:bps")
        # — even of a token pool — is instead a dust native-ETH trigger from the
        # provider address, so it takes the native path below (token=None).
        if token is not None:
            if not router:
                raise SwapAborted("token liquidity needs the backend's ETH router")
            decimals = self.token_decimals(token)
            expiry = now + 3600
            built_token = self._build_token_deposit(
                account=self._key(mnemonic, DEFAULT_ETH_DERIVATION),
                token=token,
                router=router,
                vault=vault,
                native=amount * 10**decimals // 10**8,
                memo=memo,
                expiry=expiry,
                nonce=nonce,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
            )
            # No destination in an LP memo, so pass "" (memo_pays_destination
            # is a no-op for empty); the gate still binds token/router/vault/
            # amount/memo/expiry.
            problems = verify_eth_token_swap(
                built=built_token, destination="", now=now, max_fee_wei=max_fee_wei
            )
            return Prepared(
                quote=None, built=built_token, plan=built_token, problems=problems
            )
        # Defensive: an add against a token pool (a "-" in the pool segment)
        # without an explicit token would deposit native ETH against that pool —
        # mispaired at the vault. Refuse rather than guess a contract.
        if memo.startswith("+") and "-" in memo.split(":")[1]:
            raise SwapAborted(
                "token-pool liquidity add needs an explicit token contract"
            )

        built = self.build_unsigned_swap(
            mnemonic=mnemonic,
            vault_address=vault,
            amount=amount,
            memo=memo,
            nonce=nonce,
            gas=gas,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
        )
        plan = EthSwapPlan(
            inbound_address=vault,
            amount_wei=amount * WEI_PER_THORCHAIN_UNIT,
            memo=memo,
            expiry=now + 3600,
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
        return Prepared(quote=None, built=built, plan=plan, problems=problems)
