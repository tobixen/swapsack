"""Tests for the `balance` sheet renderer (pure: no network, no adapters).

The rules here are the ones that make a balance sheet trustworthy rather than
merely tidy: what a total is allowed to include, and what it must say out loud
when it cannot price something. A silently-wrong total is the same class of
defect as the LP position `balance` used to hide.
"""

import re

import pytest

from swapsack.pricefeed import UNITS, unit_for
from swapsack.report import Row, render

EUR = UNITS["EUR"]


def _value_columns(lines: list[str]) -> list[int]:
    """Where each rendered € amount *ends* — the column a right-aligned number
    lines up on (its start moves with the number's width)."""
    return [m.end() for line in lines for m in [re.search(r"€[\d.]+", line)] if m]


def test_rows_align_on_the_value_column():
    rows = [
        Row(label="BTC", amount=0.0005, asset="BTC"),
        Row(label="USDC-ETH", amount=40.94348, asset="USDC-ETH"),
        Row(label="  +LP maya ETH.USDT", amount=24.436, asset="USDT-ETH", lp=True),
    ]
    lines = render(
        rows, unit=EUR, prices={"BTC": 100000.0, "USDC-ETH": 0.9, "USDT-ETH": 0.9}
    )
    columns = _value_columns(lines)
    assert len(columns) >= 4  # three rows + at least the total
    assert len(set(columns)) == 1  # …all ending in the same column


def test_amounts_align_too():
    rows = [
        Row(label="BTC", amount=0.0005, asset="BTC"),
        Row(label="a-very-long-label-here", amount=1234.5, asset="ETH"),
    ]
    lines = render(rows, unit=EUR, prices={"BTC": 1.0, "ETH": 1.0})
    short = line_index(lines, "0.00050000") + len("0.00050000")
    long_ = line_index(lines, "1234.50000000") + len("1234.50000000")
    assert short == long_  # both amounts END in the same column


def line_index(lines: list[str], needle: str) -> int:
    for line in lines:
        if needle in line:
            return line.index(needle)
    raise AssertionError(f"{needle!r} not rendered in {lines}")


def test_total_separates_spendable_from_liquidity():
    rows = [
        Row(label="BTC", amount=1.0, asset="BTC"),
        Row(label="  +LP maya BTC.BTC", amount=0.5, asset="BTC", lp=True),
    ]
    lines = render(rows, unit=EUR, prices={"BTC": 100.0})
    text = "\n".join(lines)
    assert "spendable" in text and "€100.00" in text
    # An LP position is not spendable and is gross of exit fees — it may be in
    # the total, but never folded into one indistinguishable number.
    assert "liquidity" in text and "€50.00" in text
    assert "total" in text and "€150.00" in text


def test_total_omits_the_liquidity_line_when_there_is_none():
    lines = render(
        [Row(label="BTC", amount=1.0, asset="BTC")], unit=EUR, prices={"BTC": 2.0}
    )
    text = "\n".join(lines)
    assert "€2.00" in text
    assert "liquidity" not in text


def test_an_unpriceable_row_is_named_and_not_counted_as_zero():
    """The failure this exists to prevent: a row we cannot price silently
    contributing 0 to the total, so the sheet under-reports and says nothing."""
    rows = [
        Row(label="BTC", amount=1.0, asset="BTC"),
        Row(label="MYSTERY", amount=7.0, asset="NOPE"),
    ]
    lines = render(rows, unit=EUR, prices={"BTC": 100.0})
    text = "\n".join(lines)
    assert "€100.00" in text  # the total is BTC alone…
    assert "not priced" in text and "MYSTERY" in text  # …and says so


def test_an_unpriceable_row_with_nothing_in_it_is_not_worth_mentioning():
    rows = [
        Row(label="BTC", amount=1.0, asset="BTC"),
        Row(label="MYSTERY", amount=0.0, asset="NOPE"),
    ]
    text = "\n".join(render(rows, unit=EUR, prices={"BTC": 100.0}))
    assert "not priced" not in text


def test_zero_rows_are_summarised_rather_than_listed():
    rows = [
        Row(label="BTC", amount=1.0, asset="BTC"),
        Row(label="USDT-ETH", amount=0.0, asset="USDT-ETH"),
        Row(label="BNB", amount=0.0, asset="BNB"),
    ]
    lines = render(rows, unit=EUR, prices={"BTC": 1.0})
    text = "\n".join(lines)
    assert "USDT-ETH" in text and "BNB" in text  # named, so nothing vanishes
    assert not any(line.startswith("USDT-ETH") for line in lines)  # but not a row
    assert "zero:" in text


def test_zero_rows_can_be_shown_in_full():
    rows = [Row(label="BTC", amount=1.0, asset="BTC"), Row(label="BNB", amount=0.0)]
    lines = render(rows, unit=EUR, prices={"BTC": 1.0}, show_zeros=True)
    assert any(line.startswith("BNB") for line in lines)


def test_an_lp_row_is_never_hidden_even_at_zero():
    rows = [Row(label="  +LP maya BTC.BTC", amount=0.0, asset="BTC", lp=True)]
    lines = render(rows, unit=EUR, prices={"BTC": 1.0})
    assert any("+LP" in line for line in lines)


def test_without_a_unit_there_is_no_value_column_and_no_total():
    rows = [Row(label="BTC", amount=1.0, asset="BTC", note="2 used addresses")]
    lines = render(rows)
    text = "\n".join(lines)
    assert "1.00000000" in text and "2 used addresses" in text
    assert "€" not in text and "total" not in text


def test_an_estimated_amount_is_marked():
    """An LP redeem value is repriced at the pool rate — it carries a ~, and so
    must the number in the table, or it reads as exact."""
    rows = [
        Row(label="  +LP maya BTC.BTC", amount=1.0, asset="BTC", lp=True, approx=True)
    ]
    assert "~1.00000000" in "\n".join(render(rows))


def test_unit_for_accepts_the_documented_choices():
    assert unit_for("eur").vs == "eur"
    assert unit_for("BTC").vs == "btc"
    # CoinGecko has no usdt/usdc vs_currency; the dollar stablecoins price in
    # usd, which is what the flag has to mean rather than silently pricing
    # nothing.
    assert unit_for("USDT").vs == "usd"
    assert unit_for("USDC").vs == "usd"


def test_unit_for_rejects_an_unknown_unit():
    with pytest.raises(ValueError, match="unknown unit"):
        unit_for("DOGECOIN")


def test_units_format_with_their_own_precision():
    assert UNITS["EUR"].format(1234.5) == "€1234.50"
    assert UNITS["BTC"].format(0.5) == "₿0.50000000"
    assert UNITS["SATS"].format(1234.6) == "1235 sats"


# --- turning wallet/protocol records into rows ------------------------------


def _position(**over):
    from swapsack.thorchain import LiquidityPosition

    fields = dict(
        pool="ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48",
        asset_address="0xours",
        units=1568919094620,
        asset_redeem_value=5_255_137_357,
        pending_asset=0,
        protocol_redeem_value=1_768_000_000_000,
        asset_deposit_value=5_254_921_611,
        protocol_deposit_value=1_770_000_000_000,
    )
    fields.update(over)
    return LiquidityPosition(**fields)


def test_balance_row_prices_by_its_own_label():
    from swapsack.chains.base import BalanceReport
    from swapsack.report import balance_row

    row = balance_row(
        BalanceReport(
            symbol="ETH-ARB", confirmed=2_580_000_000_000_000_000, decimals=18
        )
    )
    assert row.label == "ETH-ARB"
    assert row.amount == pytest.approx(2.58)
    assert row.asset == "ETH-ARB"  # what COINGECKO_IDS and --asset both take


def test_balance_row_keeps_pending_and_drops_the_note_parens():
    from swapsack.chains.base import BalanceReport
    from swapsack.report import balance_row

    row = balance_row(
        BalanceReport(
            symbol="BTC",
            confirmed=50_000,
            decimals=8,
            pending=10_000,
            note="(2 used addresses)",
        )
    )
    assert "pending" in row.note and "2 used addresses" in row.note
    assert "(" not in row.note  # the table's own columns do the framing


def test_lp_row_folds_the_protocol_side_and_marks_it_approximate():
    from swapsack.report import lp_row

    row = lp_row(
        _position(),
        source="maya",
        asset_key="USDC-ETH",
        protocol="CACAO",
        protocol_price_in_asset=2.97e-2,
    )
    assert row.lp and row.approx  # half of it is repriced, not measured
    assert row.amount == pytest.approx(52.55137357 + 17680.0 * 2.97e-2)
    assert "via CACAO" in row.note
    assert row.asset == "USDC-ETH"


def test_lp_row_without_a_pool_price_flags_the_uncounted_side():
    """Showing the asset half as if it were the position would understate the
    holding — the row says the other side is missing instead."""
    from swapsack.report import lp_row

    row = lp_row(_position(), source="maya", protocol="CACAO")
    assert row.amount == pytest.approx(52.55137357)
    assert not row.approx
    assert "not counted" in row.note


def test_lp_row_label_drops_the_contract_and_keeps_the_source():
    from swapsack.report import lp_row

    row = lp_row(_position(), source="maya", protocol="CACAO")
    assert row.label.strip() == "+LP maya ETH.USDC"


def test_lp_row_keeps_the_deposited_figure_lp_yield_parses():
    """~/bin/lp-yield reads `deposited ~N` out of these lines to compute yield;
    dropping it from the note would silently end that series."""
    from swapsack.report import lp_row

    row = lp_row(
        _position(),
        source="maya",
        protocol="CACAO",
        protocol_price_in_asset=2.97e-2,
    )
    assert row.note.startswith("deposited ~")


def test_a_zero_row_that_owns_a_position_keeps_its_line():
    """An LP row is indented *under* its asset. Hiding the asset because the
    spendable balance is zero would leave the position dangling under whatever
    row happened to precede it — which is how a BTC position ends up looking
    like it belongs to ETH."""
    rows = [
        Row(label="BTC", amount=0.0, asset="BTC", note="2 used addresses"),
        Row(label="  +LP maya BTC.BTC", amount=0.5, asset="BTC", lp=True),
        Row(label="BNB", amount=0.0, asset="BNB"),
    ]
    lines = render(rows, unit=EUR, prices={"BTC": 100.0})
    assert any(line.startswith("BTC") for line in lines)
    assert "zero: BNB" in "\n".join(lines)  # the childless one still collapses


def test_cli_unit_choices_match_the_units_that_exist():
    """`balance --unit` lists its choices without importing the price feed (a
    heavy import on every CLI start), so an invariant keeps the two in step."""
    from swapsack.cli import _UNIT_NAMES

    assert set(_UNIT_NAMES) == set(UNITS)


# --- the history / UTXO listings --------------------------------------------


def _tx(**kw):
    from swapsack.chains.history import WalletTx

    base = dict(
        txid="ab" * 32,
        confirmed=True,
        block_height=900_000,
        block_time=1_756_000_000,
        fee=1_000,
        received=0,
        sent=0,
        has_op_return=False,
        memo=None,
        counterparties=(),
    )
    return WalletTx(**{**base, **kw})


def _out(**kw):
    from swapsack.chains.history import Output

    base = dict(
        txid="ab" * 32,
        vout=0,
        value=500_000,
        address="bc1qowned",
        path="m/84'/0'/0'/0/0",
        confirmed=True,
        block_height=900_000,
        block_time=1_756_000_000,
        spent_by=None,
    )
    return Output(**{**base, **kw})


def test_history_dates_a_confirmed_transaction_in_utc():
    from swapsack.report import render_history

    lines = render_history([_tx(received=500_000)], symbol="BTC")
    assert "2025-08-24 01:46" in lines[1]


def test_history_says_mempool_rather_than_inventing_a_date():
    from swapsack.report import render_history

    lines = render_history(
        [_tx(confirmed=False, block_height=None, block_time=None, received=1)],
        symbol="BTC",
    )
    assert "mempool" in lines[1]


def test_history_falls_back_to_the_block_height_when_undated():
    """Some sources confirm a transaction without dating it. A blank column
    would read as "no information"; the height is information."""
    from swapsack.report import render_history

    lines = render_history([_tx(block_time=None, received=1)], symbol="BTC")
    assert "block 900000" in lines[1]


def test_history_signs_the_amount_so_direction_is_unmissable():
    from swapsack.report import render_history

    incoming = render_history([_tx(received=500_000)], symbol="BTC")[1]
    outgoing = render_history([_tx(sent=500_000, received=199_000)], symbol="BTC")[1]
    assert "+0.00500000" in incoming
    assert "-0.00301000" in outgoing


def test_history_prints_the_full_txid():
    """A truncated txid cannot be pasted into `swapsack status` or an explorer,
    which is the entire point of having the listing."""
    from swapsack.report import render_history

    lines = render_history([_tx(received=1)], symbol="BTC")
    assert "ab" * 32 in lines[1]


def test_history_names_the_counterparty_and_the_memo_of_a_swap_deposit():
    from swapsack.report import render_history

    lines = render_history(
        [
            _tx(
                sent=500_000,
                received=199_000,
                has_op_return=True,
                memo=b"=:ETH.ETH:0xdead",
                counterparties=("bc1qvault",),
            )
        ],
        symbol="BTC",
    )
    assert "swap deposit" in lines[1]
    assert "bc1qvault" in lines[1]
    assert "=:ETH.ETH:0xdead" in lines[1]


def test_history_renders_a_binary_memo_as_hex_not_mojibake():
    """A Chainflip vault-swap payload is binary. Decoding it as text would
    either throw or print garbage; hex is what an explorer shows anyway."""
    from swapsack.report import render_history

    memo = bytes([0x00, 0x01, 0xFE])
    lines = render_history([_tx(sent=1, has_op_return=True, memo=memo)], symbol="BTC")
    assert "0001fe" in lines[1]


def test_history_charges_the_fee_only_to_transactions_we_paid_for():
    """An incoming transaction's fee was paid by the sender; showing it against
    our row would misattribute someone else's cost to us."""
    from swapsack.report import render_history

    outgoing = render_history([_tx(sent=500_000, fee=1_000)], symbol="BTC")[1]
    incoming = render_history([_tx(received=500_000, fee=1_000)], symbol="BTC")[1]
    assert "fee 1000" in outgoing
    assert "fee" not in incoming


def test_history_footer_counts_and_nets():
    from swapsack.report import render_history

    lines = render_history(
        [_tx(received=500_000), _tx(txid="cd" * 32, sent=500_000, received=199_000)],
        symbol="BTC",
    )
    assert any("2 transactions" in line for line in lines)
    assert any("0.00199000" in line for line in lines)


def test_history_of_nothing_says_so_instead_of_printing_a_bare_header():
    from swapsack.report import render_history

    lines = render_history([], symbol="BTC")
    assert len(lines) == 1
    assert "no transactions" in lines[0].lower()


def test_outputs_mark_spent_ones_with_the_spending_txid():
    from swapsack.report import render_outputs

    lines = render_outputs(
        [_out(spent_by="cd" * 32), _out(vout=1, value=7)], symbol="BTC"
    )
    assert f"spent by {'cd' * 32}" in lines[1]
    assert "unspent" in lines[2]


def test_outputs_show_the_outpoint_and_the_derivation_path():
    from swapsack.report import render_outputs

    lines = render_outputs([_out(vout=3)], symbol="BTC")
    assert f"{'ab' * 32}:3" in lines[1]
    assert "m/84'/0'/0'/0/0" in lines[1]


def test_outputs_mark_an_unconfirmed_output():
    from swapsack.report import render_outputs

    lines = render_outputs(
        [_out(confirmed=False, block_height=None, block_time=None)], symbol="BTC"
    )
    assert "mempool" in lines[1]


def test_outputs_total_only_the_unspent_ones():
    """The spent column is history; adding it to the total would double-count
    money that has already left."""
    from swapsack.report import render_outputs

    lines = render_outputs(
        [_out(value=500_000, spent_by="cd" * 32), _out(vout=1, value=199_000)],
        symbol="BTC",
    )
    footer = " ".join(lines[-2:])
    assert "0.00199000" in footer
    assert "0.00500000" not in footer.replace("0.00199000", "")


def test_outputs_of_nothing_says_so():
    from swapsack.report import render_outputs

    lines = render_outputs([], symbol="BTC")
    assert len(lines) == 1
    assert "no outputs" in lines[0].lower()
