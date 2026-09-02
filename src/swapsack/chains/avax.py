"""Avalanche C-Chain adapter — native AVAX plus the pooled USDC and USDT.

Avalanche's C-Chain is an EVM chain, so derivation, JSON-RPC, tx building,
signing and the verify gates are all Ethereum's; this adapter is a thin
:class:`EthAdapter` subclass overriding only the chain-specific surface (RPC,
chain id, asset names, tracked tokens). ``chains/bsc.py`` established that seam
and ``chains/arb.py`` proved it for a *tradable* chain — the difference here is
which network has the pools: THORChain runs `AVAX.AVAX`, `AVAX.USDC` and
`AVAX.USDT` (all `Available`, none halted, checked 2026-09-02) and **Maya has no
AVAX pools at all**, so ``lp_backends = ("thorchain",)`` — the exact inverse of
Arbitrum. That is worth stating rather than inheriting: copying ARB's
``("maya",)`` would refuse every Avalanche liquidity op with a message naming
the wrong network.

Only the **C-Chain** is EVM and pooled. Avalanche's X- and P-Chains have their
own `X-avax1…`/`P-avax1…` bech32 addresses, and ``addresses.py`` refuses them
deliberately: a valid Avalanche address that a payout could never credit.

Three things that are easy to get wrong when copying an EVM adapter, all pinned
by tests in ``tests/test_avax.py``:

* **Chain id 43114.** Signing with Ethereum's 1 does not merely get rejected —
  the emitted raw tx is a *valid Ethereum mainnet transaction* paying the same
  recipient in real ETH. Passed to ``super().__init__`` so every inherited
  builder signs for Avalanche. Signing for the right chain is only half of it:
  the verify gate compares the built tx against a *plan*, and ``EthSwapPlan``
  defaults to chain 1, so a builder that omits ``chain_id=self.chain_id`` from
  the plan refuses every transaction it makes with ``chainId 43114 != 1``. That
  fails closed rather than losing money, but it silently disables the path.
* **Both tokens are 6 decimals here**, like Ethereum's and Arbitrum's and
  unlike BSC's 18. The BSC docstring's warning is about BSC specifically; do not
  generalize it. Read off the live contracts on 2026-09-02, and
  ``test_avax_token_decimals_match_the_live_contracts`` keeps that reading
  runnable.
* **These are the bridged Avalanche-native issues THORChain pools**, i.e. the
  contracts named in the pool asset strings. Avalanche also carries older
  `USDC.e`/`USDT.e` bridge tokens at different addresses; a deposit of one of
  those would not be credited.

Two ARB overrides that are deliberately **not** copied:

* ``native_label`` — Arbitrum needs one because its native coin is *ether*, so
  an unqualified balance row is indistinguishable from Ethereum's. AVAX names
  its own chain, so the inherited ``native_symbol`` is already the string
  ``--asset`` accepts.
* ``native_send_gas`` — Arbitrum raises it because an L2 bills the L1 calldata
  cost as extra gas consumed. Avalanche is an L1 with no such surcharge, so
  Ethereum's 21000 is the whole cost of a value transfer.
"""

from __future__ import annotations

from swapsack.chains.eth import EthAdapter

# Keyless public Avalanche C-Chain JSON-RPC (same provider family as the
# ETH/ARB/BSC defaults). Override with --avax-rpc / $SWAPSACK_AVAX_RPC.
DEFAULT_AVAX_RPC = "https://avalanche-c-chain-rpc.publicnode.com"
AVAX_CHAIN_ID = 43114

# ERC-20 tokens the wallet tracks for `balance` (symbol, contract, decimals).
# Both 6 decimals (verified on-chain via decimals()); these are the contracts
# THORChain's AVAX.USDC / AVAX.USDT pools name — see the docstring on the older
# `.e` bridge tokens, which are different tokens at different addresses.
AVAX_TRACKED_TOKENS = (
    ("USDC", "0xb97ef9ef8734c71904d8002f8b6bc66dd9c48a6e", 6),
    ("USDT", "0x9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7", 6),
)


class AvaxAdapter(EthAdapter):
    """ChainAdapter for the Avalanche C-Chain (native AVAX + USDC/USDT)."""

    chain = "AVAX"
    asset = "AVAX.AVAX"
    native_symbol = "AVAX"
    # THORChain is the only network with AVAX pools — Maya has none, so `balance`
    # must not probe it for positions that cannot exist, and an LP op aimed at
    # Maya is refused up front with the right network named.
    lp_backends = ("thorchain",)
    token_suffix = "AVAX"  # balance label suffix, e.g. "USDC-AVAX"
    tracked_tokens = AVAX_TRACKED_TOKENS
    known_token_decimals = {  # noqa: RUF012 (mirrors EthAdapter's class attribute)
        "0x" + contract.lower().removeprefix("0x"): decimals
        for _, contract, decimals in AVAX_TRACKED_TOKENS
    }

    def __init__(
        self,
        rpc_url: str = DEFAULT_AVAX_RPC,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(
            rpc_url, timeout, bip39_passphrase=bip39_passphrase, chain_id=AVAX_CHAIN_ID
        )
