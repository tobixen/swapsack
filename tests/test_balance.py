"""Tests for the chain-agnostic BalanceReport formatting."""

from swapsack.chains.base import BalanceReport


def test_format_basic_account_balance():
    report = BalanceReport(
        symbol="ETH", confirmed=2_580_000_000_000_000_000, decimals=18
    )
    assert report.format().startswith("ETH: 2.58")


def test_format_with_pending_and_note():
    report = BalanceReport(
        symbol="BTC",
        confirmed=50_000,
        decimals=8,
        pending=10_000,
        note="(1 used addresses)",
    )
    line = report.format()
    assert "BTC: 0.00050000" in line
    assert "pending" in line
    assert "used addresses" in line
