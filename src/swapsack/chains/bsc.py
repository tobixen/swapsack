"""BNB Smart Chain (BSC) adapter — native BNB plus the BEP-20 USDT and USDC.

BSC is an EVM chain, so derivation, JSON-RPC, tx building, signing and the
verify gates are all Ethereum's; this adapter is a thin :class:`EthAdapter`
subclass that only overrides the chain-specific surface (native symbol, RPC,
chain id 56, tracked tokens).

THORChain-only: its BSC trading halt lifted 2026-09-02 (``BSC.BNB``,
``BSC.USDT`` and ``BSC.USDC`` all ``Available``), and Maya has no BSC pools.
Until then this adapter stubbed ``build_and_verify`` out by hand; the inherited
one is used now.

Gotcha: BSC's USDC/USDT are 18-decimal BEP-20 tokens, NOT 6-decimal like their
Ethereum namesakes — hence a BSC-specific tracked-token table.
"""

from __future__ import annotations

from swapsack.chains.eth import EthAdapter

# Keyless public BSC JSON-RPC node (same provider family as the ETH/TRON defaults).
DEFAULT_BSC_RPC = "https://bsc-rpc.publicnode.com"
BSC_CHAIN_ID = 56

# BEP-20 tokens the wallet tracks for `balance` (symbol, contract, decimals).
# Both are 18 decimals on BSC (verified on-chain via decimals()), unlike the
# 6-decimal ETH/TRON USDT/USDC — getting this wrong misreports balances by 1e12.
BSC_TRACKED_TOKENS = (
    ("USDT", "0x55d398326f99059ff775485246999027b3197955", 18),
    ("USDC", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", 18),
)


class BscAdapter(EthAdapter):
    """ChainAdapter for BNB Smart Chain (native BNB + BEP-20), address/balance only."""

    chain = "BSC"
    asset = "BSC.BNB"
    native_symbol = "BNB"
    # THORChain is the only network with BSC pools — Maya has none.
    lp_backends = ("thorchain",)
    token_suffix = "BSC"
    tracked_tokens = BSC_TRACKED_TOKENS
    known_token_decimals = {  # noqa: RUF012 (mirrors EthAdapter's class attribute)
        "0x" + contract.lower().removeprefix("0x"): decimals
        for _, contract, decimals in BSC_TRACKED_TOKENS
    }

    def __init__(
        self,
        rpc_url: str = DEFAULT_BSC_RPC,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(
            rpc_url, timeout, bip39_passphrase=bip39_passphrase, chain_id=BSC_CHAIN_ID
        )
