"""MayaChain adapter — the native CACAO asset.

A thin configuration of the shared ``chains.cosmos.CosmosAdapter`` (MayaChain and
THORChain are the same Cosmos-SDK software). CACAO is the one asset that deviates
from THORChain's 1e8 convention: it is **1e10** (10 decimals). See docs/cacao.md
for the full design notes.
"""

from __future__ import annotations

from swapsack.chains.cosmos import CosmosAdapter

DEFAULT_MAYANODE = "https://mayanode.mayachain.info"
# Must agree with thorchain._ASSET_UNITS["MAYA.CACAO"] (display scaling); the
# modules stay import-independent (thorchain must not drag in bitcoinlib), so
# a test cross-checks them instead: test_maya.py::test_cacao_unit_agrees…
CACAO_DECIMALS = 10


class MayaAdapter(CosmosAdapter):
    """ChainAdapter for MayaChain (native CACAO, 1e10)."""

    chain = "MAYA"
    asset = "MAYA.CACAO"
    symbol = "CACAO"
    # CACAO is Maya's settlement asset: there is no MAYA.CACAO pool on Maya, and
    # THORChain doesn't trade Maya assets — so it is genuinely pool-less and
    # `balance` skips the guaranteed-404 LP probe. (RUNE differs: Maya runs a
    # live THOR.RUNE pool, so ThorAdapter keeps the default lp_pools=True.)
    lp_pools = False
    hrp = "maya"
    denom = "cacao"
    decimals = CACAO_DECIMALS
    default_chain_id = "mayachain-mainnet-v1"
    default_node = DEFAULT_MAYANODE
    home_path_prefix = "mayachain"  # matches the maya backend's ThorchainClient
