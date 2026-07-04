"""Tests for CLI argument parsing (handlers do I/O and are tested manually)."""

from decimal import Decimal

import pytest

from cryptoswap_wallet.cli import ASSET, build_parser


def test_swap_defaults():
    args = build_parser().parse_args(["swap", "--amount", "0.001781"])
    assert args.command == "swap"
    assert args.from_ == "BTC"
    assert args.to_ == "ETH"
    # Amounts are parsed as Decimal (never binary float) so they scale to base
    # units exactly.
    assert args.amount == Decimal("0.001781")
    assert args.confirm is False


def test_price_check_defaults_on_and_can_be_disabled():
    on = build_parser().parse_args(["swap", "--amount", "0.001"])
    assert on.price_check is True
    off = build_parser().parse_args(["swap", "--amount", "0.001", "--no-price-check"])
    assert off.price_check is False
    # quote gets the same flag (shared _add_swap_args).
    q = build_parser().parse_args(["quote", "--amount", "0.001", "--no-price-check"])
    assert q.price_check is False


def test_streaming_flags_parse_and_default_to_none():
    plain = build_parser().parse_args(["swap", "--amount", "0.1"])
    assert plain.stream_interval is None
    assert plain.stream_quantity is None
    streamed = build_parser().parse_args(
        ["swap", "--amount", "0.1", "--stream-interval", "1", "--stream-quantity", "0"]
    )
    assert streamed.stream_interval == 1
    assert streamed.stream_quantity == 0


def test_streaming_interval_rejects_negative():
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["swap", "--amount", "0.1", "--stream-interval", "-1"]
        )


def test_streaming_kwargs_helper_reads_args():
    from cryptoswap_wallet.cli import _streaming_kwargs

    args = build_parser().parse_args(
        ["quote", "--amount", "0.1", "--stream-interval", "3"]
    )
    assert _streaming_kwargs(args) == {
        "streaming_interval": 3,
        "streaming_quantity": None,
    }


def test_market_comparison_skips_unmapped_asset_without_network():
    from cryptoswap_wallet.cli import _market_comparison

    # TCY has no CoinGecko id in the map -> returns None before any HTTP call.
    assert _market_comparison("TCY", "BTC", 100_000_000, 1) is None


def _patch_feed(monkeypatch, prices):
    import cryptoswap_wallet.pricefeed as pf

    def fake_spot(self, coin_ids, *, vs=("usd",)):
        return prices

    monkeypatch.setattr(pf.PriceFeed, "spot", fake_spot)


def test_market_comparison_is_three_lines_with_eur_loss(monkeypatch):
    from cryptoswap_wallet.cli import _market_comparison

    _patch_feed(
        monkeypatch,
        {
            "bitcoin": {"usd": 60000.0, "eur": 55000.0},
            "dash": {"usd": 30.0, "eur": 27.5},
        },
    )
    # 1 BTC in; quoted 1900 DASH out. market = 1*60000/30 = 2000 DASH;
    # loss = 100 DASH -> 100 * 27.5 EUR = €2750.00; bps = 100/2000 = 500.
    lines = _market_comparison("BTC", "DASH", 100_000_000, 190_000_000_000)
    assert lines[0] == "Market: (CoinGecko)"
    assert "2000.00000000 DASH at spot" in lines[1]
    assert "500 bps total vs market" in lines[1]
    assert "€2750.00" in lines[2] and "loss" in lines[2]


def test_market_comparison_drops_eur_line_when_no_eur_price(monkeypatch):
    from cryptoswap_wallet.cli import _market_comparison

    _patch_feed(monkeypatch, {"bitcoin": {"usd": 60000.0}, "dash": {"usd": 30.0}})
    lines = _market_comparison("BTC", "DASH", 100_000_000, 190_000_000_000)
    assert len(lines) == 2  # header + comparison, no EUR loss line


def test_market_comparison_shows_gain_when_pool_favours_you(monkeypatch):
    from cryptoswap_wallet.cli import _market_comparison

    _patch_feed(
        monkeypatch,
        {
            "bitcoin": {"usd": 60000.0, "eur": 55000.0},
            "dash": {"usd": 30.0, "eur": 27.5},
        },
    )
    # Quoted 2100 DASH > market 2000 -> a gain, not a loss.
    lines = _market_comparison("BTC", "DASH", 100_000_000, 210_000_000_000)
    assert "gain" in lines[2]


def test_market_comparison_scales_cacao_output_by_1e10(monkeypatch):
    from cryptoswap_wallet.cli import _market_comparison

    _patch_feed(
        monkeypatch,
        {
            "bitcoin": {"usd": 60000.0, "eur": 55000.0},
            "cacao": {"usd": 0.1, "eur": 0.09},
        },
    )
    # 1 BTC in; quoted 590_000 CACAO out in 1e10 base units (5.9e15). market =
    # 1*60000/0.1 = 600_000 CACAO; loss = 10_000 CACAO. If the output were mis-
    # divided by 1e8 it would read 59_000_000 CACAO -> a bogus huge "gain".
    lines = _market_comparison("BTC", "CACAO", 100_000_000, 5_900_000_000_000_000)
    assert "600000.00000000 CACAO at spot" in lines[1]
    assert "loss" in lines[2]


def test_swap_confirm_and_target():
    args = build_parser().parse_args(
        ["swap", "--amount", "0.01", "--to", "TRX", "--confirm"]
    )
    assert args.confirm is True
    assert args.to_ == "TRX"


def test_swap_amount_max_parses():
    args = build_parser().parse_args(["swap", "--amount", "max"])
    assert args.amount == "max"


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5", "nan", "inf"])
def test_swap_rejects_nonpositive_or_nonfinite_amount(bad):
    # L2: reject amount <= 0 (and nan/inf) at parse time, not deep in a handler.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["swap", "--amount", bad])


def test_add_liquidity_rejects_zero_amount():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["add-liquidity", "--asset", "BTC", "--amount", "0"])


def test_add_liquidity_usdt_eth_routes_to_eth_handler(monkeypatch):
    import cryptoswap_wallet.cli as cli

    called = {}

    def fake_eth(args, *, memo, amount, sweep=False):
        called.update(memo=memo, amount=amount, sweep=sweep)
        return 0

    monkeypatch.setattr(cli, "_liquidity_eth", fake_eth)
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "USDT-ETH", "--amount", "25"]
    )
    assert cli.cmd_add_liquidity(args) == 0
    assert called["memo"] == "+:ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7"
    assert called["amount"] == 2_500_000_000  # 25 USDT in THORChain 1e8 units


def test_token_pool_assets_uppercases_contract():
    from cryptoswap_wallet.cli import _token_pool_assets

    class FakeEth:
        chain = "ETH"
        tracked_tokens = (
            ("USDT", "0xdac17f958d2ee523a2206206994597c13d831ec7", 6),
            ("USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6),
        )

    assert _token_pool_assets(FakeEth()) == [
        "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7",
        "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48",
    ]


def test_token_pool_assets_empty_without_tracked_tokens():
    from cryptoswap_wallet.cli import _token_pool_assets

    class FakeBtc:
        chain = "BTC"

    assert _token_pool_assets(FakeBtc()) == []


def test_add_liquidity_usdt_tron_rejected(capsys):
    import cryptoswap_wallet.cli as cli

    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "USDT-TRON", "--amount", "10"]
    )
    assert cli.cmd_add_liquidity(args) == 2
    assert "only supported for ETH tokens" in capsys.readouterr().out


def test_swap_amount_numeric_parses():
    args = build_parser().parse_args(["swap", "--amount", "0.001"])
    assert args.amount == Decimal("0.001")


def test_swap_yes_flag_parses():
    args = build_parser().parse_args(["swap", "--amount", "max", "--confirm", "--yes"])
    assert args.confirm is True
    assert args.yes is True


def test_swap_requires_amount():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["swap"])


def test_swap_rejects_unknown_asset():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["swap", "--amount", "1", "--to", "NOPE"])


def test_swap_from_eth_parses():
    args = build_parser().parse_args(
        ["swap", "--from", "ETH", "--to", "BTC", "--amount", "0.01"]
    )
    assert args.from_ == "ETH"
    assert args.to_ == "BTC"


def test_swap_from_eth_token_sweep_uses_full_token_balance(monkeypatch):
    """`--amount max` for an ERC-20 source sweeps the whole balanceOf (gas is
    paid in ETH, so the token amount is exact) — it must no longer be rejected."""
    import cryptoswap_wallet.cli as cli
    from cryptoswap_wallet.swap import SwapAborted

    class FakeAdapter:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def derive_address(self, mnemonic):
            return "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"

        def get_nonce(self, address):
            return 0

        def fetch_fees(self):
            return (20_000_000_000, 1_000_000_000)

        def fetch_token_balance(self, token, address):
            return 2_500_000  # 2.5 USDT (6 decimals)

        def token_decimals(self, token):
            return 6

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(cli, "_resolve_destination", lambda args, m, p="": "bc1qdest")
    monkeypatch.setattr(cli, "_eth_adapter", lambda args, passphrase="": FakeAdapter())

    captured = {}

    def fake_select_backend(
        args, *, from_asset, to_asset, amount, destination, tolerance_bps=None
    ):
        captured["amount"] = amount
        raise SwapAborted("captured")  # short-circuit before any network/quote

    monkeypatch.setattr(cli, "_select_backend", fake_select_backend)

    args = build_parser().parse_args(
        ["swap", "--from", "USDT-ETH", "--to", "BTC", "--amount", "max"]
    )
    rc = cli._swap_from_eth(args)
    assert rc == 1  # aborted via our stub, not the old "not supported" rejection
    assert captured["amount"] == 250_000_000  # 2.5 USDT in THORChain 1e8 units


def test_swap_tolerance_bps_defaults_to_300():
    args = build_parser().parse_args(["swap", "--amount", "1"])
    assert args.tolerance_bps == 300


def test_swap_tolerance_bps_flag_parses():
    args = build_parser().parse_args(
        ["swap", "--amount", "1", "--tolerance-bps", "1500"]
    )
    assert args.tolerance_bps == 1500


def test_swap_from_tron_token_sweep_uses_full_balance(monkeypatch):
    """`--amount max` for USDT-TRON sweeps the whole token balance (energy is
    paid in TRX, so the amount is exact) — it must build the swap, not reject."""
    import cryptoswap_wallet.cli as cli
    from cryptoswap_wallet.swap import SwapAborted

    class FakeAdapter:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def derive_address(self, mnemonic):
            return "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"

        def token_contract_and_decimals(self, from_asset):
            return ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6)

        def fetch_token_balance(self, contract, address):
            return 23_000_000  # 23 USDT (6 decimals)

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(cli, "_resolve_destination", lambda args, m, p="": "bc1qdest")
    monkeypatch.setattr(cli, "_tron_adapter", lambda args, passphrase="": FakeAdapter())

    captured = {}

    def fake_select_backend(
        args, *, from_asset, to_asset, amount, destination, tolerance_bps=None
    ):
        captured["amount"] = amount
        raise SwapAborted("captured")  # short-circuit before any network/quote

    monkeypatch.setattr(cli, "_select_backend", fake_select_backend)

    args = build_parser().parse_args(
        ["swap", "--from", "USDT-TRON", "--to", "BTC", "--amount", "max"]
    )
    rc = cli._swap_from_tron(args)
    assert rc == 1  # aborted via our stub, not a "not supported" rejection
    assert captured["amount"] == 2_300_000_000  # 23 USDT in THORChain 1e8 units


def test_swap_from_tron_native_max_still_rejected():
    """Native TRX sweep stays unsupported (it needs a TRX fee reserve)."""
    import cryptoswap_wallet.cli as cli

    args = build_parser().parse_args(
        ["swap", "--from", "TRX", "--to", "BTC", "--amount", "max"]
    )
    assert cli._swap_from_tron(args) == 2


def test_swap_eth_rpc_flag_parses():
    args = build_parser().parse_args(
        ["swap", "--from", "ETH", "--amount", "0.01", "--eth-rpc", "https://x.example"]
    )
    assert args.eth_rpc == "https://x.example"


def test_balance_eth_rpc_flag_parses():
    args = build_parser().parse_args(["balance", "--eth-rpc", "https://x.example"])
    assert args.command == "balance"
    assert args.eth_rpc == "https://x.example"


def test_balance_bsc_rpc_flag_parses():
    args = build_parser().parse_args(["balance", "--bsc-rpc", "https://bsc.example"])
    assert args.command == "balance"
    assert args.bsc_rpc == "https://bsc.example"


def test_wallet_adapters_include_bsc_maya_and_thor():
    from types import SimpleNamespace

    from cryptoswap_wallet.cli import _wallet_adapters

    args = SimpleNamespace(
        esplora=None,
        eth_rpc=None,
        tron_api=None,
        bsc_rpc=None,
        maya_api=None,
        thornode=None,
    )
    chains = {a.chain for a in _wallet_adapters(args)}
    assert {"BTC", "ETH", "TRON", "BSC", "MAYA", "THOR"} <= chains


def test_add_liquidity_parses():
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "BTC", "--amount", "0.001"]
    )
    assert args.command == "add-liquidity"
    assert args.asset == "BTC"
    assert args.amount == Decimal("0.001")


def test_add_liquidity_amount_max_parses():
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "BTC", "--amount", "max"]
    )
    assert args.amount == "max"


def test_add_liquidity_backend_defaults_to_thorchain():
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "BTC", "--amount", "1"]
    )
    assert args.backend == "thorchain"


def test_add_liquidity_backend_maya_parses():
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "BTC", "--amount", "1", "--backend", "maya"]
    )
    assert args.backend == "maya"


def test_liquidity_backend_has_no_auto():
    # LP is not price-routed, so 'auto' must not be offered.
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["add-liquidity", "--asset", "BTC", "--amount", "1", "--backend", "auto"]
        )


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


def test_status_backend_defaults_to_auto():
    args = build_parser().parse_args(["status", "ABC123"])
    assert args.backend == "auto"


def test_status_backend_maya_parses():
    args = build_parser().parse_args(["status", "ABC123", "--backend", "maya"])
    assert args.backend == "maya"


def test_send_parses_recipient_and_amount():
    from cryptoswap_wallet.cli import cmd_send

    args = build_parser().parse_args(
        ["send", "bc1qrecipient", "--amount", "0.001", "--confirm"]
    )
    assert args.address == "bc1qrecipient"
    assert args.amount == Decimal("0.001")
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


def test_send_rpc_flags_parse():
    args = build_parser().parse_args(
        [
            "send",
            "0x" + "1" * 40,
            "--asset",
            "ETH",
            "--amount",
            "1",
            "--eth-rpc",
            "https://e.example",
            "--tron-api",
            "https://t.example",
        ]
    )
    assert args.eth_rpc == "https://e.example"
    assert args.tron_api == "https://t.example"


ETH_RECIP = "0x1111111111111111111111111111111111111111"


class _FakeEthSend:
    def __init__(self, captured):
        self._captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def derive_address(self, mnemonic):
        return "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"

    def get_nonce(self, address):
        return 0

    def fetch_fees(self):
        return (20_000_000_000, 1_000_000_000)

    def fetch_token_balance(self, token, address):
        return 2_500_000  # 2.5 USDT (6 dec)

    def token_decimals(self, token):
        return 6

    def build_and_verify_send(self, **kw):
        from types import SimpleNamespace

        from cryptoswap_wallet.swap import Prepared

        self._captured.update(kw)
        return Prepared(
            quote=None, built=SimpleNamespace(fee=10**14), plan=None, problems=[]
        )


def test_send_eth_native_dry_run(monkeypatch):
    import cryptoswap_wallet.cli as cli

    captured = {}
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(
        cli, "_eth_adapter", lambda args, passphrase="": _FakeEthSend(captured)
    )
    args = build_parser().parse_args(
        ["send", ETH_RECIP, "--asset", "ETH", "--amount", "0.001"]
    )
    assert cli.cmd_send(args) == 0  # dry run, verify gate clean
    assert captured["recipient"] == ETH_RECIP
    assert captured["asset"] == "ETH.ETH"
    assert captured["amount"] == 100_000  # 0.001 ETH in 1e8 units


def test_send_eth_token_sweep_uses_full_balance(monkeypatch):
    import cryptoswap_wallet.cli as cli

    captured = {}
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(
        cli, "_eth_adapter", lambda args, passphrase="": _FakeEthSend(captured)
    )
    args = build_parser().parse_args(
        ["send", ETH_RECIP, "--asset", "USDT-ETH", "--amount", "max"]
    )
    assert cli.cmd_send(args) == 0
    assert captured["amount"] == 250_000_000  # 2.5 USDT in 1e8 units
    assert captured["asset"].startswith("ETH.USDT-")


def test_send_eth_rejects_bad_recipient():
    import cryptoswap_wallet.cli as cli

    args = build_parser().parse_args(
        ["send", "0xnothex", "--asset", "ETH", "--amount", "1"]
    )
    assert cli.cmd_send(args) == 2  # gross-format recipient rejected before build


def test_send_tron_native_max_refused():
    import cryptoswap_wallet.cli as cli

    args = build_parser().parse_args(
        [
            "send",
            "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH",
            "--asset",
            "TRX",
            "--amount",
            "max",
        ]
    )
    assert cli.cmd_send(args) == 2  # native TRX sweep can't be exact


def test_main_version_exits_cleanly(monkeypatch):
    # Exercises main()'s completion gate: with _ARGCOMPLETE unset, argcomplete is
    # never imported and argparse's --version action exits 0.
    monkeypatch.delenv("_ARGCOMPLETE", raising=False)
    from cryptoswap_wallet.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


def test_asset_map():
    assert ASSET["BTC"] == "BTC.BTC"
    assert ASSET["ETH"] == "ETH.ETH"
    assert ASSET["TRX"] == "TRON.TRX"
    assert ASSET["USDT-TRON"].startswith("TRON.USDT-")
    assert ASSET["USDT-ETH"].startswith("ETH.USDT-")
    assert ASSET["USDC-ETH"].startswith("ETH.USDC-")
    # Destination-only assets (item 3).
    assert ASSET["LTC"] == "LTC.LTC"
    assert ASSET["DOGE"] == "DOGE.DOGE"
    assert ASSET["BCH"] == "BCH.BCH"


def test_swap_to_ltc_parses():
    args = build_parser().parse_args(
        ["swap", "--to", "LTC", "--amount", "0.01", "--dest", "ltc1qexample"]
    )
    assert ASSET[args.to_] == "LTC.LTC"


def test_resolve_destination_rejects_bad_dest():
    from cryptoswap_wallet.cli import _resolve_destination

    args = build_parser().parse_args(
        ["swap", "--to", "LTC", "--amount", "0.01", "--dest", "not-a-real-address!!"]
    )
    with pytest.raises(SystemExit):
        _resolve_destination(args, mnemonic=None)


def test_resolve_destination_accepts_good_ltc_dest():
    from cryptoswap_wallet.cli import _resolve_destination

    dest = "ltc1qg9stkxrszkdqsuj92lm4c7akvk36zvhqw7p6ck"
    args = build_parser().parse_args(
        ["swap", "--to", "LTC", "--amount", "0.01", "--dest", dest]
    )
    assert _resolve_destination(args, mnemonic=None) == dest


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


# --- money: Decimal scaling and sub-base-unit guard (findings #2, #9) ---

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def test_base_units_scales_without_float_error():
    # 93393106.59778857 BTC through binary float rounds to ...858 base units;
    # the Decimal path must yield the exact ...857.
    from cryptoswap_wallet.cli import _amount, _base_units

    assert _base_units(_amount("93393106.59778857")) == 9339310659778857


def test_base_units_round_trip_simple():
    from cryptoswap_wallet.cli import _amount, _base_units

    assert _base_units(_amount("0.5")) == 50_000_000


@pytest.mark.parametrize("cmd", ["swap", "send"])
def test_amount_rejects_sub_base_unit(cmd):
    # 1e-9 scales to 0.1 base units -> would round to 0 and burn a fee on a
    # no-op send; reject at parse time.
    argv = (
        ["send", "bc1qx", "--amount", "0.000000001"]
        if cmd == "send"
        else ["swap", "--amount", "0.000000001"]
    )
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


# --- BIP-39 passphrase threaded out of the keystore (finding #1) ---


def test_load_mnemonic_returns_bip39_passphrase(tmp_path, monkeypatch):
    import cryptoswap_wallet.cli as cli
    from cryptoswap_wallet.keystore import Keystore

    path = tmp_path / "ks.json"
    ks = Keystore()
    ks.add_hd("w", MNEMONIC, passphrase="extra-word")
    ks.save(path, "pw", n=1024)
    monkeypatch.setenv("CRYPTOSWAP_WALLET_KEYSTORE", str(path))
    monkeypatch.setenv("CRYPTOSWAP_WALLET_PASSPHRASE", "pw")

    args = build_parser().parse_args(["address"])
    mnemonic, passphrase = cli._load_mnemonic(args)
    assert mnemonic == MNEMONIC
    assert passphrase == "extra-word"


# --- uncaught InsufficientFunds on a non-sweep BTC swap (finding #4) ---


def test_swap_from_btc_insufficient_funds_aborts_cleanly(monkeypatch):
    import cryptoswap_wallet.cli as cli
    from cryptoswap_wallet.chains.coins import InsufficientFunds, Utxo

    class FakeBtc:
        chain = "BTC"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def derive_address(self, mnemonic, path=None):
            return "bc1qchange"

        def address_info(self, address):
            return None  # unused: scan_account is stubbed

        def fetch_utxos(self, address):
            return [Utxo(txid="aa" * 32, vout=0, value=100_000, address=address)]

        def fetch_fee_rate(self):
            return 5.0

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeBackend:
        name = "thorchain"
        client = FakeClient()

    def fake_scan(*, derive_address, probe, account):
        from types import SimpleNamespace

        return [("m/84'/0'/0'/0/0", "bc1qowned", SimpleNamespace(confirmed=100_000))]

    def boom(**kwargs):
        raise InsufficientFunds("have 100000 sats, need 50000000 + fee for the swap")

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(cli, "_resolve_destination", lambda args, m, p="": "bc1qdest")
    monkeypatch.setattr(cli, "_btc_adapter", lambda args, passphrase="": FakeBtc())
    monkeypatch.setattr(cli, "_select_backend", lambda *a, **k: FakeBackend())
    monkeypatch.setattr("cryptoswap_wallet.chains.scan.scan_account", fake_scan)
    monkeypatch.setattr(cli, "prepare_swap", boom)

    args = build_parser().parse_args(
        ["swap", "--from", "BTC", "--to", "ETH", "--amount", "0.5"]
    )
    assert cli._swap_from_btc(args) == 1  # clean ABORTED, not a traceback


# --- backend sessions are closed after selection (finding #12) ---


def test_select_backend_closes_unused_clients(monkeypatch):
    from types import SimpleNamespace

    import cryptoswap_wallet.backends as backends_mod
    import cryptoswap_wallet.cli as cli
    from cryptoswap_wallet.backends import Backend

    class RecordingClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    b1 = Backend("thorchain", RecordingClient())
    b2 = Backend("maya", RecordingClient())
    monkeypatch.setattr(cli, "_backends_for", lambda args: [b1, b2])
    monkeypatch.setattr(backends_mod, "gather_quotes", lambda *a, **k: [(b1, object())])
    monkeypatch.setattr(backends_mod, "best_quote", lambda results: results[0])

    chosen = cli._select_backend(
        SimpleNamespace(backend="auto"),
        from_asset="BTC.BTC",
        to_asset="ETH.ETH",
        amount=1,
        destination="bc1qdest",
        tolerance_bps=300,
    )
    assert chosen is b1
    assert b2.client.closed is True  # the backend we won't use is closed
    assert b1.client.closed is False  # the chosen one stays open for the caller
