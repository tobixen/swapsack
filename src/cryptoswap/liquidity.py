"""THORChain liquidity-provision memos (experimental).

Add (single-sided / asymmetric): deposit the asset to the inbound vault with
memo ``+:POOL``. Withdraw: send a dust tx to the vault with
``-:POOL:<basis_points>`` (1..10000) from the address that provided. No quote
is involved — this is not a swap. Risk (impermanent loss, RUNE price, protocol)
and fee yield both scale ~linearly with the deposit; the thing that penalises a
small position is the roughly *fixed* round-trip transaction cost (add +
withdraw trigger + outbound), which is a larger fraction of a smaller stake.
"""

from __future__ import annotations

MAX_BASIS_POINTS = 10000


def add_liquidity_memo(pool: str) -> str:
    return f"+:{pool}"


def withdraw_liquidity_memo(pool: str, basis_points: int) -> str:
    if not 1 <= basis_points <= MAX_BASIS_POINTS:
        raise ValueError(
            f"basis_points must be 1..{MAX_BASIS_POINTS}, got {basis_points}"
        )
    return f"-:{pool}:{basis_points}"
