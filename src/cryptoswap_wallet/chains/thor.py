"""THORChain adapter — the native RUNE asset.

A thin configuration of the shared ``chains.cosmos.CosmosAdapter``. RUNE uses
THORChain's standard 1e8 base units (unlike Maya's 1e10 CACAO) and the ``thor``
bech32 HRP; everything else (derivation, MsgSend/MsgDeposit, signing) is shared.
See docs/cacao.md for the design notes (the same shape applies here).
"""

from __future__ import annotations

from cryptoswap_wallet.chains.cosmos import CosmosAdapter

DEFAULT_THORNODE = "https://thornode.thorchain.network"
RUNE_DECIMALS = 8


class ThorAdapter(CosmosAdapter):
    """ChainAdapter for THORChain (native RUNE, 1e8)."""

    chain = "THOR"
    asset = "THOR.RUNE"
    symbol = "RUNE"
    hrp = "thor"
    denom = "rune"
    decimals = RUNE_DECIMALS
    default_chain_id = "thorchain-1"
    default_node = DEFAULT_THORNODE
