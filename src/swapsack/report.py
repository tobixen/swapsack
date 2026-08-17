"""The `balance` sheet: rows in, aligned (optionally valued) lines out.

Pure — no network, no adapters, no printing — so the layout and, more
importantly, the *arithmetic of the total* can be tested offline.

Four rules the total obeys, all of them the same rule really: a balance sheet
may not quietly overstate what you have or understate what it does not know.

* A row it cannot price is **named**, never counted as zero. Silently dropping
  it under-reports the total with nothing on screen to say so — the same defect
  as the LP position `balance` used to hide entirely.
* Liquidity is totalled **separately** from spendable funds. An LP position is
  not liquid and its redeem value is gross of exit fees, so folding it into one
  number would present an estimate of a non-withdrawable amount as cash.
* A row worth nothing is not worth a line, but its absence is still accounted
  for: zero rows collapse into one trailing "zero:" line naming them, so a chain
  never simply disappears from the sheet.
* A chain that **did not answer** is not a chain holding nothing. Its row stays
  on the sheet with "?" for an amount, it is named in the footer, and the total
  is stamped INCOMPLETE — an unknown holding may not be quietly totalled as
  zero, and a warning on stderr does not count because stdout is what gets
  read, redirected and pasted.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from swapsack.pricefeed import Unit

if TYPE_CHECKING:  # imported for types only — report.py stays a leaf module
    from swapsack.chains.base import BalanceReport
    from swapsack.thorchain import LiquidityPosition

# Column gap. Two spaces reads as a column; one reads as a typo.
GAP = "  "


@dataclasses.dataclass(frozen=True)
class Row:
    """One line of the sheet, before layout.

    ``asset`` is the wallet asset key used to price it (``BTC``, ``USDC-ETH``,
    …) — the same string `--asset` accepts, which is also `COINGECKO_IDS`' key.
    Empty means "no idea what this is worth", which the footer then says.
    """

    label: str
    amount: float
    asset: str = ""
    note: str = ""
    lp: bool = False  # a liquidity position: not spendable, never hidden
    approx: bool = False  # amount is an estimate (an LP's repriced total)
    # The chain did not answer. NOT the same as a zero balance: the amount is
    # unknown, so the row must never be hidden, never counted, and must make
    # the total say it is short of something.
    unavailable: bool = False

    def amount_text(self) -> str:
        if self.unavailable:
            return "?"
        return f"{'~' if self.approx else ''}{self.amount:.8f}"


def balance_row(report: BalanceReport) -> Row:
    """A wallet balance as a sheet row.

    The report's ``symbol`` doubles as the pricing key because it is the same
    string `--asset` accepts (`ETH-ARB`, `USDC-ETH`, …) — the property the
    Arbitrum labelling fix established, and the reason no lookup table is
    needed here.
    """
    note = report.note.strip("()")
    if report.pending:
        pending = report.pending / 10**report.decimals
        note = f"+{pending:.8f} pending" + (f"; {note}" if note else "")
    return Row(
        label=report.symbol,
        amount=report.confirmed / 10**report.decimals,
        asset=report.symbol,
        note=note,
    )


def lp_row(
    position: LiquidityPosition,
    *,
    source: str,
    asset_key: str = "",
    protocol: str = "RUNE",
    protocol_price_in_asset: float | None = None,
    unit_scale: int = 100_000_000,
) -> Row:
    """A liquidity position as a sheet row, indented under its asset.

    The amount is what the position is worth *in the pool's asset*: the asset
    side plus the RUNE/CACAO side repriced at the current pool rate. That second
    half is an estimate and gross of exit fees, so the row is marked ``approx``
    whenever it is included — and when no pool price is available the row shows
    the asset side alone and says the other side is uncounted, rather than
    quietly showing half a position as if it were whole.
    """
    asset_side = position.asset_redeem_value / unit_scale
    extras = []
    if protocol_price_in_asset is not None and position.protocol_redeem_value:
        other = position.protocol_redeem_value * protocol_price_in_asset / unit_scale
        amount, approx = asset_side + other, True
        extras.append(f"{other:.8f} via {protocol}")
    else:
        amount, approx = asset_side, False
        if position.protocol_redeem_value:
            extras.append(f"plus a {protocol} side not counted")
    if protocol_price_in_asset is not None and (
        position.asset_deposit_value or position.protocol_deposit_value
    ):
        deposited = (
            position.asset_deposit_value
            + position.protocol_deposit_value * protocol_price_in_asset
        ) / unit_scale
        extras.insert(0, f"deposited ~{deposited:.8f}")
    if position.pending_asset:
        extras.append(f"+{position.pending_asset / unit_scale:.8f} pending")
    return Row(
        label=f"  +LP {source} {short_pool(position.pool)}",
        amount=amount,
        asset=asset_key,
        note="; ".join(extras),
        lp=True,
        approx=approx,
    )


def short_pool(pool: str) -> str:
    """``ETH.USDC-0XA0B8…`` -> ``ETH.USDC``: 42 characters of contract carry no
    information the symbol and chain do not already give a reader."""
    return pool.split("-", 1)[0]


def value_of(row: Row, prices: dict[str, float]) -> float | None:
    """``row``'s worth in the price map's currency, or ``None`` if unpriceable."""
    price = prices.get(row.asset) if row.asset else None
    return None if price is None else row.amount * price


def render(
    rows: list[Row],
    *,
    unit: Unit | None = None,
    prices: dict[str, float] | None = None,
    show_zeros: bool = False,
) -> list[str]:
    """The sheet as lines. Without ``unit``/``prices`` the value column and the
    totals are simply absent — an unpriced sheet is a complete sheet, just a
    less informative one, so a dead price feed never costs the user their
    balances."""
    prices = prices or {}
    valued = unit is not None

    keep = [show_zeros or bool(r.amount) or r.lp or r.unavailable for r in rows]
    # An LP row is indented under the asset above it, so that asset keeps its
    # line even at zero — otherwise the position dangles under whatever row
    # happened to come before it.
    for i, row in enumerate(rows):
        if row.lp and i and not keep[i - 1] and not rows[i - 1].lp:
            keep[i - 1] = True
    shown = [row for row, wanted in zip(rows, keep, strict=True) if wanted]
    hidden = [
        row.label.strip() for row, wanted in zip(rows, keep, strict=True) if not wanted
    ]

    cells: list[tuple[str, str, str, str]] = []
    for row in shown:
        value = value_of(row, prices) if valued else None
        cells.append(
            (
                row.label,
                row.amount_text(),
                unit.format(value) if valued and value is not None else "",
                row.note,
            )
        )
    footer: list[str] = []
    silent = [row.label.strip() for row in rows if row.unavailable]
    if valued:
        totals, unpriced = _totals(shown, unit, prices, incomplete=bool(silent))
        if totals:
            cells.append(("", "", "", ""))  # blank line, then the totals block
            cells.extend(totals)
        if unpriced:
            names = ", ".join(unpriced)
            footer.append(f"not priced (excluded from the total): {names}")
    if silent:
        footer.append(
            f"did not answer (holdings unknown, not in the total): {', '.join(silent)}"
        )
    if hidden:
        footer.append(f"zero: {', '.join(hidden)}")
    # One layout pass over body *and* totals, so a total sits under the column
    # it totals rather than in a block of its own alignment.
    return _layout(cells, valued=valued) + footer


def _layout(cells: list[tuple[str, str, str, str]], *, valued: bool) -> list[str]:
    """Pad the columns to a common width: label left, numbers right, note left."""
    if not cells:
        return []
    label_w = max(len(c[0]) for c in cells)
    amount_w = max(len(c[1]) for c in cells)
    value_w = max(len(c[2]) for c in cells) if valued else 0
    out = []
    for label, amount, value, note in cells:
        line = f"{label:<{label_w}}{GAP}{amount:>{amount_w}}"
        if valued:
            line += f"{GAP}{value:>{value_w}}"
        if note:
            line += f"{GAP}{note}"
        out.append(line.rstrip())
    return out


def _totals(
    rows: list[Row], unit: Unit, prices: dict[str, float], *, incomplete: bool = False
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """The spendable/liquidity/total cells, and the names left out of them.

    ``incomplete`` marks the total as short of at least one chain that never
    answered — the number is still worth printing, but it must not read as the
    whole picture.
    """
    spendable = liquidity = 0.0
    unpriced: list[str] = []
    for row in rows:
        if row.unavailable:  # an unknown amount cannot be added or excused
            continue
        value = value_of(row, prices)
        if value is None:
            if row.amount:  # a row worth nothing is no loss to the total
                unpriced.append(row.label.strip())
            continue
        if row.lp:
            liquidity += value
        else:
            spendable += value

    cells = []
    if liquidity:
        cells.append(("spendable", "", unit.format(spendable), ""))
        cells.append(
            (
                "liquidity",
                "",
                unit.format(liquidity),
                "not spendable; gross of exit fees",
            )
        )
    cells.append(
        (
            "total",
            "",
            unit.format(spendable + liquidity),
            "INCOMPLETE — a chain did not answer" if incomplete else "",
        )
    )
    return cells, unpriced
