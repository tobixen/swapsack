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
import time
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.signers.local import LocalAccount

from swapsack.chains.base import BalanceReport
from swapsack.chains.coins import InsufficientFunds

# Re-exported: EIP-55 checksumming lives in the dependency-free swapsack.evm so
# the address validator can use it without importing this module's chain stack.
# Callers here keep importing it from this module.
from swapsack.evm import to_checksum_address
from swapsack.net import HTTP_ERRORS, HttpClient
from swapsack.swap import BroadcastError, Prepared, SwapAborted, SwapRequest
from swapsack.thorchain import Quote
from swapsack.verify import (
    EVM_VAULT_PARAMS_VERSION,
    WEI_PER_THORCHAIN_UNIT,
    ChainflipEvmVaultPlan,
    EthSendPlan,
    EthSwapPlan,
    EthTokenSendPlan,
    decode_evm_vault_call,
    decode_evm_vault_parameters,
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
ALLOWANCE_SELECTOR = "dd62ed3e"  # allowance(address,address)
TRANSFER_SELECTOR = "a9059cbb"  # transfer(address,uint256) — plain ERC-20 send
APPROVE_GAS = 70000
TOKEN_DEPOSIT_GAS = 200000

# A Chainflip vault swap is a call into the protocol's Vault contract, not a
# memo deposit, so it gets its own budget rather than DEFAULT_GAS. Measured
# against mainnet on 2026-09-02 via eth_estimateGas: 32,212 on Ethereum and
# 32,754 on Arbitrum (which bills the L1 calldata cost as extra gas consumed)
# for xSwapNative. These round that up generously — an unused limit is refunded,
# and running out of gas burns the whole limit having delivered nothing. The
# token figure also covers the ERC-20 transferFrom the Vault does on the way in.
VAULT_SWAP_GAS = 120000
VAULT_SWAP_TOKEN_GAS = 250000
# Plain external sends: a bare value transfer is 21000; an ERC-20 transfer() is
# ~50-65k. These are used by `send` (no router/approve), not the swap path.
# NATIVE_SEND_GAS is Ethereum's exact floor with no slack, so an L2 that bills
# L1 calldata as extra gas consumed needs its own budget — see
# EthAdapter.native_send_gas and chains/arb.py.
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


@dataclasses.dataclass
class EthVaultSwapBuilt:
    """A Chainflip vault swap on an EVM chain: the Vault call, and the ERC-20
    ``approve`` it needs when the source is a token.

    Shaped like the other built-swap types (``txs``/``private_key``/``fee``) so
    ``EthAdapter.sign``/``broadcast`` and ``_confirm_and_execute`` drive it
    unchanged. ``approve_tx`` is ``None`` for a native source, and the gate
    treats a mismatch between that and ``plan.source_token`` as a problem rather
    than a shrug — an approve nobody asked for is an allowance left behind.
    """

    swap_tx: dict[str, Any]
    private_key: Any
    approve_tx: dict[str, Any] | None = None

    @property
    def txs(self) -> list[dict[str, Any]]:
        return [tx for tx in (self.approve_tx, self.swap_tx) if tx is not None]

    @property
    def fee(self) -> int:
        return sum(t["gas"] * t["maxFeePerGas"] for t in self.txs)


def _verify_vault_parameters(
    parameters: bytes, plan: ChainflipEvmVaultPlan
) -> list[str]:
    """The ``cfParameters`` half of the vault-swap gate: refund, floor, no skim."""
    problems: list[str] = []
    decoded = decode_evm_vault_parameters(parameters)
    if decoded is None:
        return [
            f"vault swap parameters are {len(parameters)} bytes, expected the "
            f"96-byte layout this wallet decodes"
        ]
    if decoded.version != EVM_VAULT_PARAMS_VERSION:
        problems.append(
            f"vault swap parameters are version {decoded.version} != "
            f"{EVM_VAULT_PARAMS_VERSION}; refusing to guess their layout"
        )
    expected_refund = plan.refund_address.lower().removeprefix("0x")
    if decoded.refund_address.hex() != expected_refund:
        problems.append(
            f"parameters refund to 0x{decoded.refund_address.hex()}, intended "
            f"{plan.refund_address}"
        )
    if decoded.min_price < plan.min_price:
        problems.append(
            f"parameters min price {decoded.min_price} is below our floor "
            f"{plan.min_price}"
        )
    # The last field in the payload that would otherwise be the broker's to
    # choose. Zero refunds on the first block that does not clear the floor;
    # past the chain's cap the protocol rejects a payload we have already paid
    # into. Neither is a swap this wallet asked for.
    if decoded.retry_duration != plan.retry_duration:
        problems.append(
            f"parameters retry duration {decoded.retry_duration} != intended "
            f"{plan.retry_duration}"
        )
    if decoded.broker_fee:
        problems.append(f"parameters carry a broker fee of {decoded.broker_fee} bps")
    if decoded.boost_fee:
        problems.append(f"parameters carry a boost fee of {decoded.boost_fee} bps")
    if decoded.affiliates:
        problems.append(
            f"parameters carry {decoded.affiliates} affiliate fee entries; "
            f"expected none"
        )
    # Three Options this wallet never asks for. A Some would lengthen the blob
    # and fail the decode above, so reaching here with one set means the layout
    # moved under us — which is exactly when to stop rather than guess.
    for flag, label in (
        (decoded.ccm, "a cross-chain message"),
        (decoded.oracle_slippage, "an oracle slippage limit"),
        (decoded.dca, "DCA parameters"),
    ):
        if flag:
            problems.append(f"parameters carry {label}, which was not asked for")
    return problems


def verify_chainflip_evm_vault_swap(
    *,
    built: EthVaultSwapBuilt,
    plan: ChainflipEvmVaultPlan,
    now: int,
    max_fee_wei: int,
) -> list[str]:
    """Return reasons the txs are not the vault swap we intend; empty means safe.

    Two layers, as on the Bitcoin side. The EVM layer is the ordinary one: the
    right contract, the right value, the right chain id, a sane fee, and — for a
    token source — an ``approve`` for exactly the amount and no more. The
    Chainflip layer then reads the calldata back and checks it promises what we
    asked for: our destination, our refund address, our floor, nobody skimming.

    The calldata is compared against ``plan.calldata`` *and* decoded. That looks
    redundant and is not: the first binds the transaction to the bytes the
    preparation step checked, the second is what makes those bytes mean
    something. Either alone would pass a plan built from a bad encoding.
    """
    problems: list[str] = []
    swap, approve = built.swap_tx, built.approve_tx

    if now >= plan.expiry:
        problems.append(f"quote expired (now {now} >= expiry {plan.expiry})")
    if plan.vault_contract.lower() not in plan.known_vaults:
        problems.append(
            f"Vault contract {plan.vault_contract} is not the one the protocol "
            f"publishes on-chain"
        )
    if swap["to"].lower() != plan.vault_contract.lower():
        problems.append(f"tx 'to' {swap['to']} != Vault {plan.vault_contract}")
    if swap["value"] != plan.value:
        problems.append(f"tx value {swap['value']} wei != intended {plan.value}")
    # xSwapToken is non-payable, so a real Vault would revert — but this layer
    # is meant to hold on its own, without borrowing the contract's opinion.
    if plan.source_token and plan.value:
        problems.append(
            f"a token vault swap must not send ether, but the plan carries "
            f"{plan.value} wei"
        )
    if swap["chainId"] != plan.chain_id:
        problems.append(f"chainId {swap['chainId']} != {plan.chain_id}")
    expected_data = "0x" + plan.calldata.hex()
    if (swap["data"] or "").lower() != expected_data.lower():
        problems.append(f"calldata {swap['data']!r} != planned {expected_data!r}")

    problems += _verify_vault_approve(approve, plan)

    call = decode_evm_vault_call(bytes.fromhex(swap["data"].removeprefix("0x")))
    if call is None:
        problems.append("calldata is not a Chainflip Vault call we can read back")
        return problems
    if call.destination != plan.destination_bytes:
        problems.append(
            f"call pays destination {call.destination.hex()}, intended "
            f"{plan.destination_bytes.hex()}"
        )
    if call.destination_asset_id != plan.destination_asset_id:
        problems.append(
            f"call pays output asset {call.destination_asset_id}, intended "
            f"{plan.destination_asset_id}"
        )
    if call.destination_chain_id != plan.destination_chain_id:
        problems.append(
            f"call pays out on chain {call.destination_chain_id}, intended "
            f"{plan.destination_chain_id}"
        )
    if call.source_token != plan.source_token:
        problems.append(
            f"call spends token {call.source_token or '(native)'}, intended "
            f"{plan.source_token or '(native)'}"
        )
    # For a token the amount is an argument; for the native coin it is the
    # transaction's own value, already checked above — and the argument must
    # then be silent, or the call is not the shape we asked for.
    moved = call.source_amount if plan.source_token else swap["value"]
    if moved != plan.source_amount:
        problems.append(
            f"call moves {moved} source units, intended {plan.source_amount}"
        )
    if not plan.source_token and call.source_amount:
        problems.append(
            f"a native vault swap must carry no token amount, but the call "
            f"names {call.source_amount}"
        )
    problems += _verify_vault_parameters(call.parameters, plan)

    if built.fee > max_fee_wei:
        problems.append(f"max fee {built.fee} wei exceeds limit {max_fee_wei}")
    return problems


def _verify_vault_approve(
    approve: dict[str, Any] | None, plan: ChainflipEvmVaultPlan
) -> list[str]:
    """The ``approve`` half of the gate — including its absence being correct."""
    if not plan.source_token:
        return (
            []
            if approve is None
            else ["a native vault swap must not be preceded by an approve"]
        )
    if approve is None:
        return [f"a {plan.source_token} vault swap needs an approve, and has none"]
    problems: list[str] = []
    if approve["to"].lower() != plan.source_token.lower():
        problems.append(f"approve 'to' {approve['to']} != token {plan.source_token}")
    if approve["value"] != 0:
        problems.append("approve tx must not send ETH value")
    if approve["chainId"] != plan.chain_id:
        problems.append(f"approve chainId {approve['chainId']} != {plan.chain_id}")
    try:
        spender, allowance = _decode_call(
            approve["data"], APPROVE_SELECTOR, ["address", "uint256"]
        )
    except Exception:  # noqa: BLE001 - any decode failure is a reject
        return [*problems, "approve calldata could not be decoded"]
    if spender.lower() != plan.vault_contract.lower():
        problems.append(f"approve spender {spender} != Vault {plan.vault_contract}")
    if allowance != plan.source_amount:
        problems.append(f"approve amount {allowance} != {plan.source_amount}")
    return problems


@dataclasses.dataclass
class EthApprovals:
    """0–2 ERC-20 ``approve`` txs granting ``spender`` exactly ``amount``.

    Used by the CoW path to fund the vault relayer's pull. Empty ``txs`` means
    the standing allowance already suffices. Two txs is the USDT quirk: it
    reverts on a nonzero -> nonzero allowance change, so a leftover partial
    allowance is reset to 0 first (a nonzero -> zero change is always allowed).
    Shaped like the other built-swap types (``txs``/``private_key``/``fee``) so
    ``EthAdapter.sign``/``broadcast`` drive it unchanged.
    """

    txs: list[dict[str, Any]]
    private_key: Any
    token: str
    spender: str
    amount: int
    chain_id: int = CHAIN_ID

    @property
    def fee(self) -> int:
        return sum(t["gas"] * t["maxFeePerGas"] for t in self.txs)


def verify_eth_approvals(built: EthApprovals, *, max_fee_wei: int) -> list[str]:
    """Gate for the approval txs: every tx must be an ``approve`` on the
    intended token, to the intended spender, and only the final one may grant
    a non-zero amount — exactly ``built.amount``."""
    problems: list[str] = []
    for i, tx in enumerate(built.txs):
        expected = built.amount if i == len(built.txs) - 1 else 0
        if tx["to"].lower() != built.token.lower():
            problems.append(f"approve 'to' {tx['to']} != token {built.token}")
        if tx["value"] != 0:
            problems.append("approve tx must not send ETH value")
        if tx["chainId"] != built.chain_id:
            problems.append("wrong chainId")
        try:
            spender, allowance = _decode_call(
                tx["data"], APPROVE_SELECTOR, ["address", "uint256"]
            )
        except Exception:  # noqa: BLE001 - any decode failure is a reject
            problems.append("approve calldata could not be decoded")
            continue
        if spender.lower() != built.spender.lower():
            problems.append(f"approve spender {spender} != {built.spender}")
        if allowance != expected:
            problems.append(f"approve amount {allowance} != {expected}")
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
    # Gas *limit* for a plain native send. A limit is not a fee — unused gas is
    # refunded — so a subclass on a chain that consumes more than the 21000
    # floor (an L2 billing L1 calldata) must raise this rather than inherit it.
    native_send_gas = NATIVE_SEND_GAS

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

    @property
    def native_label(self) -> str:
        """What `balance` calls the native row — the string ``--asset`` accepts.

        Defaults to ``native_symbol``, which is right wherever the coin's name
        names its chain too (ETH, BNB). A chain whose native coin is *ether*
        must override this with a class attribute (see ``ArbAdapter``): symbol
        and address are both identical to Ethereum's, so an unqualified row is
        indistinguishable from it.
        """
        return self.native_symbol

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
            symbol=self.native_label,
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

    def fetch_token_allowance(self, token: str, owner: str, spender: str) -> int:
        """ERC-20 ``allowance(owner, spender)`` in the token's native units."""
        data = (
            "0x"
            + ALLOWANCE_SELECTOR
            + to_checksum_address(owner)[2:].lower().rjust(64, "0")
            + to_checksum_address(spender)[2:].lower().rjust(64, "0")
        )
        return int(self._rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)

    def build_and_verify_approvals(
        self,
        *,
        mnemonic: str,
        token: str,
        spender: str,
        amount: int,
        current_allowance: int,
        nonce: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        max_fee_wei: int,
        path: str = DEFAULT_ETH_DERIVATION,
    ) -> Prepared:
        """Build + gate the ``approve`` txs granting ``spender`` exactly
        ``amount`` (native token units) — or none, when ``current_allowance``
        already covers it. See :class:`EthApprovals` for the 2-tx USDT case.
        """
        account = self._key(mnemonic, path)
        token = to_checksum_address(token)
        spender = to_checksum_address(spender)
        amounts: list[int] = []
        if current_allowance < amount:
            if current_allowance > 0:
                amounts.append(0)
            amounts.append(amount)
        txs = [
            {
                "type": 2,
                "chainId": self.chain_id,
                "nonce": nonce + i,
                "to": token,
                "value": 0,
                "gas": APPROVE_GAS,
                "maxFeePerGas": max_fee_per_gas,
                "maxPriorityFeePerGas": max_priority_fee_per_gas,
                "data": encode_approve(spender, value),
            }
            for i, value in enumerate(amounts)
        ]
        built = EthApprovals(
            txs=txs,
            private_key=account.key,
            token=token,
            spender=spender,
            amount=amount,
            chain_id=self.chain_id,
        )
        problems = verify_eth_approvals(built, max_fee_wei=max_fee_wei)
        return Prepared(quote=None, built=built, plan=built, problems=problems)

    def sign_cow_order(
        self, order: dict[str, Any], mnemonic: str, path: str = DEFAULT_ETH_DERIVATION
    ) -> str:
        """EIP-712-sign a CoW order with this wallet's ETH key.

        Keeps the private key inside the adapter (like every other signing
        path here) rather than handing it to the CLI.
        """
        from swapsack.cow import sign_order

        return sign_order(order, self._key(mnemonic, path).key)

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

    def wait_for_receipt(
        self, txid: str, *, timeout: float = 120.0, poll_interval: float = 3.0
    ) -> dict[str, Any] | None:
        """Poll ``eth_getTransactionReceipt`` until ``txid`` mines or ``timeout``.

        Returns the receipt (carrying ``status`` — ``0x1`` success, ``0x0``
        revert) once the tx is mined, or None if it is still pending when the
        deadline passes. Used before a CoW order submit so the ERC-20 allowance
        is actually on-chain (the orderbook validates it at placement).

        A failing poll is treated exactly like a pending one, because the
        caller is past the point of no return: the approval is broadcast, gas
        is spent. A transport error or a non-conformant reply escaping here
        would replace the "re-run — the allowance is already in place" guidance
        with a traceback, leaving the user unable to tell whether the order was
        submitted. A node that never recovers simply runs out the deadline.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                receipt = self._rpc("eth_getTransactionReceipt", [txid])
            except (*HTTP_ERRORS, RuntimeError):
                receipt = None
            if receipt is not None:
                return receipt  # type: ignore[return-value]
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval)

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

    def sign(
        self,
        built: EthBuiltSwap | EthTokenBuiltSwap | EthApprovals | EthVaultSwapBuilt,
    ) -> list[str]:
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
            chain_id=self.chain_id,
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

    def build_and_verify_vault_swap(
        self,
        *,
        plan: ChainflipEvmVaultPlan,
        now: int,
        mnemonic: str,
        nonce: int,
        max_fee_per_gas: int,
        max_priority_fee_per_gas: int,
        max_fee_wei: int,
        path: str = DEFAULT_ETH_DERIVATION,
    ) -> Prepared:
        """Build + gate a Chainflip vault swap: the Vault call, plus an approve
        for a token source.

        The calldata is the chain's, not ours — ``chainflip.prepare_evm_vault_
        swap`` obtained and checked it, and it arrives here already bound to the
        plan. What this adds is the transaction around it, and the gate that
        proves the transaction is the one the plan describes.

        Gas is :data:`VAULT_SWAP_GAS`/:data:`VAULT_SWAP_TOKEN_GAS` rather than
        the caller's ``--eth-gas``: that budget sizes a memo deposit, and a
        Vault call is a different piece of work.
        """
        account = self._key(mnemonic, path)
        common = {
            "type": 2,
            "chainId": self.chain_id,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
        }
        approve_tx = None
        if plan.source_token:
            approve_tx = {
                **common,
                "nonce": nonce,
                "to": to_checksum_address(plan.source_token),
                "value": 0,
                "gas": APPROVE_GAS,
                "data": encode_approve(
                    to_checksum_address(plan.vault_contract), plan.source_amount
                ),
            }
        swap_tx = {
            **common,
            "nonce": nonce + (1 if approve_tx else 0),
            "to": to_checksum_address(plan.vault_contract),
            "value": plan.value,
            "gas": VAULT_SWAP_TOKEN_GAS if plan.source_token else VAULT_SWAP_GAS,
            "data": "0x" + plan.calldata.hex(),
        }
        built = EthVaultSwapBuilt(
            swap_tx=swap_tx, private_key=account.key, approve_tx=approve_tx
        )
        problems = verify_chainflip_evm_vault_swap(
            built=built, plan=plan, now=now, max_fee_wei=max_fee_wei
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

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
            gas=self.native_send_gas,
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
            chain_id=self.chain_id,
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
