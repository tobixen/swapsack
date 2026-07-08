"""THORChain/Maya liquidity-provision memos + maths (experimental).

Add (single-sided / asymmetric): deposit the asset to the inbound vault with
memo ``+:POOL``. Withdraw: send a dust tx to the vault with
``-:POOL:<basis_points>`` (1..10000) from the address that provided. No quote
is involved — this is not a swap. Risk (impermanent loss, RUNE price, protocol)
and fee yield both scale ~linearly with the deposit; the thing that penalises a
small position is the roughly *fixed* round-trip transaction cost (add +
withdraw trigger + outbound), which is a larger fraction of a smaller stake.

Add (symmetric / two-sided): **two linked deposits**, one per side, paired by
the protocol via cross-referenced addresses:

* the asset leg goes to the asset chain's inbound vault with memo
  ``+:POOL:<protocol_address>`` (your RUNE/CACAO address), and
* the protocol leg is a native ``MsgDeposit`` carrying RUNE/CACAO with memo
  ``+:POOL:<asset_address>`` (your address on the asset chain).

Both must arrive; the protocol pairs them by matching each memo's referenced
address against the *other* leg's observed sender. If only one lands the
position sits pending (or is refunded after a timeout) — a real partial-failure
hazard, so the caller must prepare + gate **both** legs before broadcasting
**either**.
"""

from __future__ import annotations

MAX_BASIS_POINTS = 10000


def add_liquidity_memo(pool: str) -> str:
    return f"+:{pool}"


def symmetric_add_memo(pool: str, paired_address: str) -> str:
    """Add-liquidity memo that pairs with ``paired_address`` on the other side.

    Used for both legs of a symmetric add: the asset leg passes the protocol
    (RUNE/CACAO) address; the protocol leg passes the asset-chain address.
    """
    if not paired_address:
        raise ValueError("symmetric add needs the paired address")
    return f"+:{pool}:{paired_address}"


def withdraw_liquidity_memo(pool: str, basis_points: int) -> str:
    if not 1 <= basis_points <= MAX_BASIS_POINTS:
        raise ValueError(
            f"basis_points must be 1..{MAX_BASIS_POINTS}, got {basis_points}"
        )
    return f"-:{pool}:{basis_points}"


def pair_amount(asset_amount: int, balance_asset: int, balance_protocol: int) -> int:
    """Protocol-asset amount to match ``asset_amount`` at the current pool ratio.

    ``asset_amount`` is in THORChain's 1e8 asset units (as sent on the asset leg).
    ``balance_asset`` / ``balance_protocol`` are the raw pool depths: the asset
    side is 1e8, the protocol side is its *native* unit (RUNE 1e8, CACAO 1e10),
    so the returned amount is already in the protocol asset's native base units.
    A symmetric add at the pool ratio incurs no entry slip.
    """
    if balance_asset <= 0 or balance_protocol <= 0:
        raise ValueError(
            f"empty pool depth: asset={balance_asset} prot={balance_protocol}"
        )
    if asset_amount <= 0:
        raise ValueError(f"asset_amount must be positive, got {asset_amount}")
    return asset_amount * balance_protocol // balance_asset
