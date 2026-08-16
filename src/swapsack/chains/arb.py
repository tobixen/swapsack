"""Arbitrum One adapter — native ETH on Arbitrum plus native (Circle) USDC.

Arbitrum is an EVM chain, so derivation, JSON-RPC, tx building, signing and the
verify gates are all Ethereum's; this adapter is a thin :class:`EthAdapter`
subclass overriding only the chain-specific surface (RPC, chain id, asset names,
tracked tokens). ``chains/bsc.py`` established that seam — the difference here is
that ARB is **tradable**, so unlike BSC this adapter keeps the inherited swap and
liquidity paths rather than stubbing them out. Maya runs `ARB.ETH` and `ARB.USDC`
pools and publishes an ARB router; THORChain has no ARB pools at all, hence
``lp_backends = ("maya",)``.

Three things that are easy to get wrong when copying an EVM adapter, all pinned
by tests in ``tests/test_arb.py``:

* **Chain id 42161.** Signing with Ethereum's 1 does not merely get rejected —
  the emitted raw tx is a *valid Ethereum mainnet transaction* paying the same
  recipient in real ETH. Passed to ``super().__init__`` so every inherited
  builder signs for Arbitrum. Signing for the right chain is only half of it:
  the verify gate compares the built tx against a *plan*, and ``EthSwapPlan``
  defaults to chain 1, so a builder that omits ``chain_id=self.chain_id`` from
  the plan refuses every transaction it makes with ``chainId 42161 != 1``. That
  fails closed rather than losing money, but it silently disables the path —
  ``tests/test_arb.py`` pins the swap and deposit gates for exactly this.
* **USDC is 6 decimals here**, like Ethereum's and unlike BSC's 18. The BSC
  docstring's warning is about BSC specifically; do not generalize it.
* **The USDC contract is the native (Circle-issued) one.** Arbitrum also carries
  a *bridged* `USDC.e` at `0xff970a61…`, a different token with its own
  liquidity. Maya's `ARB.USDC` pool means the native one below; a deposit of the
  bridged token would not be credited.

Gas: Arbitrum charges the L1 calldata cost by inflating the gas a tx consumes,
which is why the inherited fixed gas constants deserved a look. Measured against
the live chain on 2026-08-16 via the ``NodeInterface`` precompile
(``gasEstimateL1Component``), that surcharge is **101–172 gas** for 0–108 bytes
of calldata — negligible beside the inherited swap/deposit budgets (60k native,
200k token deposit), and it scales with the L1 base fee, which was ~0.8 gwei at
the time. Those are inherited unchanged. The **plain-send** budget is not:
Ethereum's ``NATIVE_SEND_GAS`` of 21000 is that chain's exact floor with no
slack, and the same measurement put an Arbitrum native transfer at 21,345 — over
it — so ``native_send_gas`` is raised below. See ``docs/TODO.md`` for the
measurement and the conditions that would change it.
"""

from __future__ import annotations

from swapsack.chains.eth import EthAdapter

# Keyless public Arbitrum One JSON-RPC (same provider family as the ETH/BSC
# defaults). Override with --arb-rpc / $SWAPSACK_ARB_RPC.
DEFAULT_ARB_RPC = "https://arbitrum-one-rpc.publicnode.com"
ARB_CHAIN_ID = 42161

# ERC-20 tokens the wallet tracks for `balance` (symbol, contract, decimals).
# Native Circle USDC — see the docstring on why the bridged USDC.e is excluded.
ARB_TRACKED_TOKENS = (("USDC", "0xaf88d065e77c8cc2239327c5edb3a432268e5831", 6),)


class ArbAdapter(EthAdapter):
    """ChainAdapter for Arbitrum One (native ETH + native USDC)."""

    chain = "ARB"
    # Maya's native-ETH-on-Arbitrum pool. Deliberately not "ARB.ARB": the ARB
    # *token* pool is Staged, not tradeable, so "ARB" as an asset means this.
    asset = "ARB.ETH"
    native_symbol = "ETH"
    # Maya is the only network with ARB pools, so `balance` must not probe
    # THORChain for LP positions that cannot exist there.
    lp_backends = ("maya",)
    # Ethereum's 21000 is the exact floor for a bare value transfer and carries
    # no slack, but Arbitrum bills the L1 calldata cost as extra gas consumed —
    # measured at 21,345 for a native transfer (docs/TODO.md). Inheriting 21000
    # means the tx runs out of gas, reverts, and burns the whole limit having
    # delivered nothing. A limit is refunded when unused, so round up freely.
    native_send_gas = 30000
    token_suffix = "ARB"  # balance label suffix, e.g. "USDC-ARB"
    tracked_tokens = ARB_TRACKED_TOKENS
    known_token_decimals = {  # noqa: RUF012 (mirrors EthAdapter's class attribute)
        "0x" + contract.lower().removeprefix("0x"): decimals
        for _, contract, decimals in ARB_TRACKED_TOKENS
    }

    def __init__(
        self,
        rpc_url: str = DEFAULT_ARB_RPC,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(
            rpc_url, timeout, bip39_passphrase=bip39_passphrase, chain_id=ARB_CHAIN_ID
        )
