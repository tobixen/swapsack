"""THORChain/Maya liquidity-provision memos + maths (experimental).

Add (single-sided / asymmetric): deposit the asset to the inbound vault with
memo ``+:POOL``. Withdraw: send a dust tx to the vault with
``-:POOL:<basis_points>`` (1..10000) from the address that provided. No quote
is involved — this is not a swap. Risk (impermanent loss, RUNE price, protocol)
and fee yield both scale ~linearly with the deposit; the thing that penalises a
small position is the roughly *fixed* round-trip transaction cost (add +
withdraw trigger + outbound), which is a larger fraction of a smaller stake.

Withdrawing a **symmetric** position is not that: the protocol looks the LP
record up by the trigger's observed sender, and a symmetric position is filed
under the RUNE/CACAO address, so an asset-chain trigger finds nothing. It is
triggered from the protocol side instead — a native ``MsgDeposit`` carrying
``WITHDRAW_TRIGGER_AMOUNT`` with the same ``-:POOL:<bps>`` memo — and both sides
come back proportionally, asset to the asset address and RUNE/CACAO to the
protocol one. See ``docs/liquidity-symmetric.md``.

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

Add (protocol side only): a native ``MsgDeposit`` of RUNE/CACAO alone with the
plain ``+:POOL`` memo. The record is filed under the RUNE/CACAO address with no
asset address, so a withdraw (the protocol-side trigger above) pays out
*entirely* in RUNE/CACAO. It takes entry slip like a single-sided asset add.

CACAO Pool (Maya) / RUNEPool (THORChain): not a pool of its own but a share of
the protocol-owned liquidity spread across every pool. Both networks use the
same memos: deposit is a ``MsgDeposit`` of CACAO/RUNE with memo ``pool+``;
withdraw is a ``MsgDeposit`` of **zero** CACAO/RUNE with
``pool-:<basis_points>``, refused until ``<TOKEN>POOLDEPOSITMATURITYBLOCKS``
have passed since the last deposit. See ``docs/cacao.md``.
"""

from __future__ import annotations

import dataclasses

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


# A withdraw is *triggered*, not paid for: the deposit carries the memo and the
# amount is irrelevant to what comes back, so it is dust. 1 base unit is what
# Maya's own withdraw traffic uses (1e-10 CACAO; sampled 2026-08-17), and it is
# the smallest thing a MsgDeposit can carry.
WITHDRAW_TRIGGER_AMOUNT = 1


def _check_basis_points(basis_points: int) -> None:
    if not 1 <= basis_points <= MAX_BASIS_POINTS:
        raise ValueError(
            f"basis_points must be 1..{MAX_BASIS_POINTS}, got {basis_points}"
        )


def withdraw_liquidity_memo(pool: str, basis_points: int) -> str:
    _check_basis_points(basis_points)
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


@dataclasses.dataclass(frozen=True)
class ProtocolPool:
    """A network's protocol pool: its token (which also prefixes its mimir
    keys, e.g. ``RUNEPOOLENABLED``), the token's decimals, and its name."""

    token: str
    decimals: int
    name: str


# By client path prefix — the one table every pool lookup reads.
PROTOCOL_POOLS = {
    "mayachain": ProtocolPool("CACAO", 10, "CACAO Pool"),
    "thorchain": ProtocolPool("RUNE", 8, "RUNEPool"),
}

# mayanode memo_cacao_pool.go and thornode memo_rune_pool.go. Both withdraw
# handlers refuse any coin (Msg{Cacao,Rune}PoolWithdraw.ValidateBasic: "coins
# must be empty"), while the deposit handler insists on exactly one — so the
# trigger is one coin of amount 0.
POOL_DEPOSIT_MEMO = "pool+"
POOL_WITHDRAW_AMOUNT = 0


def pool_withdraw_memo(basis_points: int) -> str:
    _check_basis_points(basis_points)
    return f"pool-:{basis_points}"
