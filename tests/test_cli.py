"""Tests for CLI argument parsing (handlers do I/O and are tested manually)."""

import pytest

from cryptoswap.cli import ASSET, build_parser


def test_swap_defaults():
    args = build_parser().parse_args(["swap", "--amount", "0.001781"])
    assert args.command == "swap"
    assert args.from_ == "BTC"
    assert args.to_ == "ETH"
    assert args.amount == 0.001781
    assert args.confirm is False


def test_swap_confirm_and_target():
    args = build_parser().parse_args(
        ["swap", "--amount", "0.01", "--to", "TRX", "--confirm"]
    )
    assert args.confirm is True
    assert args.to_ == "TRX"


def test_swap_amount_max_parses():
    args = build_parser().parse_args(["swap", "--amount", "max"])
    assert args.amount == "max"


def test_swap_amount_numeric_parses():
    args = build_parser().parse_args(["swap", "--amount", "0.001"])
    assert args.amount == 0.001


def test_swap_requires_amount():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["swap"])


def test_swap_rejects_unknown_asset():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["swap", "--amount", "1", "--to", "DOGE"])


def test_status_takes_txid():
    args = build_parser().parse_args(["status", "ABC123"])
    assert args.txid == "ABC123"


def test_asset_map():
    assert ASSET["BTC"] == "BTC.BTC"
    assert ASSET["ETH"] == "ETH.ETH"
    assert ASSET["TRX"] == "TRON.TRX"


def test_add_hd_generate_flag():
    args = build_parser().parse_args(["add-hd", "--label", "x", "--generate"])
    assert args.generate is True


def test_add_hd_generate_and_mnemonic_mutually_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["add-hd", "--label", "x", "--generate", "--mnemonic", "a b c"]
        )


def test_show_seed_command():
    args = build_parser().parse_args(["show-seed", "--key", "x"])
    assert args.command == "show-seed"
    assert args.key == "x"
