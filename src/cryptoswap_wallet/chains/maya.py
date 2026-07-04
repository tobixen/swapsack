"""MayaChain adapter — the native CACAO asset.

A thin configuration of the shared ``chains.cosmos.CosmosAdapter`` (MayaChain and
THORChain are the same Cosmos-SDK software). CACAO is the one asset that deviates
from THORChain's 1e8 convention: it is **1e10** (10 decimals). See docs/cacao.md
for the full design notes.
"""

from __future__ import annotations

from cryptoswap_wallet.chains.cosmos import CosmosAdapter

DEFAULT_MAYANODE = "https://mayanode.mayachain.info"
CACAO_DECIMALS = 10
CACAO_UNIT = 10**CACAO_DECIMALS


class MayaAdapter(CosmosAdapter):
    """ChainAdapter for MayaChain (native CACAO, 1e10)."""

    chain = "MAYA"
    asset = "MAYA.CACAO"
    symbol = "CACAO"
    hrp = "maya"
    denom = "cacao"
    decimals = CACAO_DECIMALS
    default_chain_id = "mayachain-mainnet-v1"
    default_node = DEFAULT_MAYANODE
