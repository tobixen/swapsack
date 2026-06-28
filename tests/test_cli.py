"""Tests for CLI argument parsing (handlers do I/O and are tested manually)."""

import pytest

from cryptoswap_wallet.cli import ASSET, build_parser


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


def test_swap_yes_flag_parses():
    args = build_parser().parse_args(["swap", "--amount", "max", "--confirm", "--yes"])
    assert args.confirm is True
    assert args.yes is True


def test_swap_requires_amount():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["swap"])


def test_swap_rejects_unknown_asset():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["swap", "--amount", "1", "--to", "DOGE"])


def test_swap_from_eth_parses():
    args = build_parser().parse_args(
        ["swap", "--from", "ETH", "--to", "BTC", "--amount", "0.01"]
    )
    assert args.from_ == "ETH"
    assert args.to_ == "BTC"


def test_swap_eth_rpc_flag_parses():
    args = build_parser().parse_args(
        ["swap", "--from", "ETH", "--amount", "0.01", "--eth-rpc", "https://x.example"]
    )
    assert args.eth_rpc == "https://x.example"


def test_balance_eth_rpc_flag_parses():
    args = build_parser().parse_args(["balance", "--eth-rpc", "https://x.example"])
    assert args.command == "balance"
    assert args.eth_rpc == "https://x.example"


def test_add_liquidity_parses():
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "BTC", "--amount", "0.001"]
    )
    assert args.command == "add-liquidity"
    assert args.asset == "BTC"
    assert args.amount == 0.001


def test_withdraw_liquidity_parses():
    args = build_parser().parse_args(
        ["withdraw-liquidity", "--asset", "ETH", "--bps", "5000"]
    )
    assert args.command == "withdraw-liquidity"
    assert args.bps == 5000


def test_withdraw_liquidity_defaults_to_full():
    args = build_parser().parse_args(["withdraw-liquidity", "--asset", "BTC"])
    assert args.bps == 10000


def test_swap_backend_defaults_to_auto():
    args = build_parser().parse_args(["swap", "--amount", "0.001"])
    assert args.backend == "auto"


def test_quote_backend_choice():
    args = build_parser().parse_args(
        ["quote", "--amount", "0.001", "--backend", "maya"]
    )
    assert args.backend == "maya"


def test_status_takes_txid():
    args = build_parser().parse_args(["status", "ABC123"])
    assert args.txid == "ABC123"


def test_send_parses_recipient_and_amount():
    from cryptoswap_wallet.cli import cmd_send

    args = build_parser().parse_args(
        ["send", "bc1qrecipient", "--amount", "0.001", "--confirm"]
    )
    assert args.address == "bc1qrecipient"
    assert args.amount == 0.001
    assert args.asset == "BTC"
    assert args.confirm is True
    assert args.func is cmd_send


def test_send_amount_max_parses():
    args = build_parser().parse_args(["send", "bc1qx", "--amount", "max"])
    assert args.amount == "max"


def test_send_requires_address_and_amount():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["send", "--amount", "0.001"])  # no recipient
    with pytest.raises(SystemExit):
        build_parser().parse_args(["send", "bc1qx"])  # no amount


def test_asset_map():
    assert ASSET["BTC"] == "BTC.BTC"
    assert ASSET["ETH"] == "ETH.ETH"
    assert ASSET["TRX"] == "TRON.TRX"
    assert ASSET["USDT-TRON"].startswith("TRON.USDT-")
    assert ASSET["USDT-ETH"].startswith("ETH.USDT-")


def test_resolve_destination_for_usdt_targets():
    pytest.importorskip("eth_account")
    from types import SimpleNamespace

    from cryptoswap_wallet.cli import _resolve_destination

    mnemonic = (
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )
    tron = _resolve_destination(SimpleNamespace(dest=None, to_="USDT-TRON"), mnemonic)
    eth = _resolve_destination(SimpleNamespace(dest=None, to_="USDT-ETH"), mnemonic)
    assert tron == "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"
    assert eth == "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"


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
