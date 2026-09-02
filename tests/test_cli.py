"""Tests for CLI argument parsing (handlers do I/O and are tested manually)."""

import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

# The native-swap tests construct cosmos adapters, whose first bitcoinlib import
# has noisy side effects (a leaked file handle + a SQLAlchemy deprecation
# warning) that `filterwarnings = ["error"]` would otherwise turn into a
# spurious in-test failure. Mirrors the other bitcoinlib-backed tests.
pytest.importorskip("bitcoinlib")

from swapsack import cli  # noqa: E402
from swapsack.cli import ASSET, build_parser  # noqa: E402


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


@pytest.mark.parametrize(
    "argv",
    [
        ["send", "--amount", "0.001", "bc1qrecipient"],
        ["add-liquidity", "--asset", "BTC", "--amount", "0.001"],
        ["withdraw-liquidity", "--asset", "BTC"],
        # status prices the on-chain fee line too, so it needs the opt-out.
        ["status", "ab" * 32],
    ],
)
def test_price_check_can_be_disabled_on_every_command_that_prices(argv):
    """Every command that consults the price feed must let you opt out.

    The EUR fee estimate put a CoinGecko lookup on `send`, the liquidity
    commands and `status` — paths that previously made no third-party price call
    at all — so each needs the same opt-out `swap`/`quote` already had.
    """
    on = build_parser().parse_args(argv)
    assert on.price_check is True
    off = build_parser().parse_args([*argv, "--no-price-check"])
    assert off.price_check is False


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


def test_streaming_interval_rejects_zero():
    # 0 is NOT "off": downstream checks are `is not None`, so interval 0 would
    # drop the price tolerance (LIM=0) while the node returns a plain
    # non-streaming quote — a swap with no slippage protection at all.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["swap", "--amount", "0.1", "--stream-interval", "0"])


def test_streaming_quantity_zero_still_allowed():
    # 0 = "let the network pick" for the quantity (unlike the interval).
    args = build_parser().parse_args(
        ["swap", "--amount", "0.1", "--stream-interval", "1", "--stream-quantity", "0"]
    )
    assert args.stream_quantity == 0


def test_streaming_kwargs_helper_reads_args():
    from swapsack.cli import _streaming_kwargs

    args = build_parser().parse_args(
        ["quote", "--amount", "0.1", "--stream-interval", "3"]
    )
    assert _streaming_kwargs(args) == {
        "streaming_interval": 3,
        "streaming_quantity": None,
    }


def test_market_comparison_skips_unmapped_asset_without_network():
    from swapsack.cli import _market_comparison

    # TCY has no CoinGecko id in the map -> returns None before any HTTP call.
    assert _market_comparison("TCY", "BTC", 100_000_000, 1) is None


def _patch_feed(monkeypatch, prices):
    import swapsack.pricefeed as pf

    def fake_spot(self, coin_ids, *, vs=("usd",)):
        return prices

    monkeypatch.setattr(pf.PriceFeed, "spot", fake_spot)


def test_market_comparison_is_three_lines_with_eur_loss(monkeypatch):
    from swapsack.cli import _market_comparison

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
    from swapsack.cli import _market_comparison

    _patch_feed(monkeypatch, {"bitcoin": {"usd": 60000.0}, "dash": {"usd": 30.0}})
    lines = _market_comparison("BTC", "DASH", 100_000_000, 190_000_000_000)
    assert len(lines) == 2  # header + comparison, no EUR loss line


def test_market_comparison_shows_gain_when_pool_favours_you(monkeypatch):
    from swapsack.cli import _market_comparison

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
    from swapsack.cli import _market_comparison

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
    import swapsack.cli as cli

    called = {}

    def fake_evm(args, factory, *, memo, amount, sweep=False, **_):
        called.update(memo=memo, amount=amount, sweep=sweep, factory=factory)
        return 0

    monkeypatch.setattr(cli, "_liquidity_evm", fake_evm)
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "USDT-ETH", "--amount", "25"]
    )
    assert cli.cmd_add_liquidity(args) == 0
    assert called["memo"] == "+:ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7"
    assert called["amount"] == 2_500_000_000  # 25 USDT in THORChain 1e8 units
    assert called["factory"] is cli._eth_adapter


def test_token_pool_assets_uppercases_contract():
    from swapsack.cli import _token_pool_assets

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
    from swapsack.cli import _token_pool_assets

    class FakeBtc:
        chain = "BTC"

    assert _token_pool_assets(FakeBtc()) == []


def test_add_liquidity_usdt_tron_rejected(capsys):
    import swapsack.cli as cli

    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "USDT-TRON", "--amount", "10"]
    )
    assert cli.cmd_add_liquidity(args) == 2
    assert "only supported for EVM tokens" in capsys.readouterr().out


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
    import swapsack.cli as cli
    from swapsack.swap import SwapAborted

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
    monkeypatch.setitem(
        cli._EVM_ADAPTERS, "ETH", lambda args, passphrase="": FakeAdapter()
    )
    monkeypatch.setattr(cli, "_eth_adapter", lambda args, passphrase="": FakeAdapter())

    captured = {}

    def fake_select_backend(
        args, *, from_asset, to_asset, amount, destination, tolerance_bps=None, **kw
    ):
        captured["amount"] = amount
        raise SwapAborted("captured")  # short-circuit before any network/quote

    monkeypatch.setattr(cli, "_select_backend", fake_select_backend)

    args = build_parser().parse_args(
        ["swap", "--from", "USDT-ETH", "--to", "BTC", "--amount", "max"]
    )
    rc = cli._swap_from_evm(args, cli._eth_adapter)
    assert rc == 1  # aborted via our stub, not the old "not supported" rejection
    assert captured["amount"] == 250_000_000  # 2.5 USDT in THORChain 1e8 units


def test_swap_tolerance_bps_defaults_to_backend_default():
    # None = "use the backend's default": DEFAULT_TOLERANCE_BPS (300) on the
    # thornode paths, the much tighter DEFAULT_COW_TOLERANCE_BPS (50) on CoW —
    # a signed CoW order's buyAmount floor is what a solver must beat, so a 3%
    # floor on a stable pair would authorize a 3% haircut.
    args = build_parser().parse_args(["swap", "--amount", "1"])
    assert args.tolerance_bps is None
    from swapsack.cli import DEFAULT_COW_TOLERANCE_BPS, _tolerance

    assert _tolerance(args) == 300
    assert (
        _tolerance(args, default=DEFAULT_COW_TOLERANCE_BPS) == DEFAULT_COW_TOLERANCE_BPS
    )


def test_swap_tolerance_bps_flag_overrides_any_default():
    # An explicit flag wins regardless of which backend's default is passed.
    from swapsack.cli import DEFAULT_COW_TOLERANCE_BPS, _tolerance

    args = build_parser().parse_args(
        ["swap", "--amount", "1", "--tolerance-bps", "1500"]
    )
    assert _tolerance(args, default=DEFAULT_COW_TOLERANCE_BPS) == 1500


def test_swap_tolerance_bps_flag_parses():
    args = build_parser().parse_args(
        ["swap", "--amount", "1", "--tolerance-bps", "1500"]
    )
    assert args.tolerance_bps == 1500


def test_swap_from_tron_token_sweep_uses_full_balance(monkeypatch):
    """`--amount max` for USDT-TRON sweeps the whole token balance (energy is
    paid in TRX, so the amount is exact) — it must build the swap, not reject."""
    import swapsack.cli as cli
    from swapsack.swap import SwapAborted

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
        args, *, from_asset, to_asset, amount, destination, tolerance_bps=None, **kw
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


@pytest.mark.parametrize(
    ("from_asset", "factory", "wrong_backend", "home"),
    [
        ("RUNE", "_thor_adapter", "maya", "thorchain"),
        ("CACAO", "_maya_adapter", "thorchain", "maya"),
    ],
)
def test_swap_from_native_refuses_foreign_backend(
    monkeypatch, capsys, from_asset, factory, wrong_backend, home
):
    """A native source deposits on its own network via MsgDeposit, so an explicit
    --backend naming the *other* network must abort before any network call —
    the deposit would land on the home chain carrying a foreign-priced memo
    (refunded minus the native fee at best)."""
    import swapsack.cli as cli

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(cli, "_resolve_destination", lambda args, m, p="": "bc1qdest")

    args = build_parser().parse_args(
        [
            "swap",
            "--from",
            from_asset,
            "--to",
            "BTC",
            "--amount",
            "1",
            "--backend",
            wrong_backend,
        ]
    )
    rc = cli._swap_from_cosmos(args, getattr(cli, factory))
    assert rc == 1
    err = capsys.readouterr().err
    assert "ABORTED" in err
    assert home in err


@pytest.mark.parametrize(
    ("from_asset", "factory", "home"),
    [
        ("RUNE", "_thor_adapter", "thorchain"),
        ("CACAO", "_maya_adapter", "maya"),
    ],
)
def test_swap_from_native_auto_pins_home_backend(
    monkeypatch, from_asset, factory, home
):
    """--backend auto must not price-route a native source: only the home
    network's backend can serve a MsgDeposit swap."""
    import swapsack.backends as backends_mod
    import swapsack.cli as cli
    from swapsack.swap import SwapAborted

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(cli, "_resolve_destination", lambda args, m, p="": "bc1qdest")

    captured = {}

    def fake_get_backend(name):
        captured["backend"] = name
        raise SwapAborted("captured")  # short-circuit before any network/quote

    monkeypatch.setattr(backends_mod, "get_backend", fake_get_backend)

    args = build_parser().parse_args(
        ["swap", "--from", from_asset, "--to", "BTC", "--amount", "1"]
    )
    rc = cli._swap_from_cosmos(args, getattr(cli, factory))
    assert rc == 1
    assert captured["backend"] == home


def test_send_validates_recipient_before_dispatch(monkeypatch, capsys):
    # The recipient sanity check lives once in cmd_send, before any handler,
    # keystore or network work — the per-chain handlers each carried (or, for
    # BTC, forgot) their own copy.
    import swapsack.cli as cli

    called = []
    monkeypatch.setattr(cli, "_send_utxo", lambda *a, **kw: called.append(1) or 0)
    args = build_parser().parse_args(
        # a TRON-looking address for a BTC send
        [
            "send",
            "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH",
            "--asset",
            "BTC",
            "--amount",
            "0.1",
        ]
    )
    rc = cli.cmd_send(args)
    assert rc == 2
    assert not called  # refused before the handler ran
    assert "does not look like" in capsys.readouterr().err


def test_send_tron_sub_precision_amount_aborts_cleanly(monkeypatch, capsys):
    """TronAdapter.to_sun/to_token_native raise ValueError for amounts finer
    than the chain's precision; _send_tron must print the standard ABORTED
    message (like _swap_from_tron does), not leak a traceback."""
    import swapsack.cli as cli

    class FakeAdapter:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def build_and_verify_send(self, **kwargs):
            raise ValueError(
                "amount 10 (1e8 units) is not a whole number of sun; "
                "TRX precision is 1e6"
            )

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(cli, "_tron_adapter", lambda args, passphrase="": FakeAdapter())

    args = build_parser().parse_args(
        [
            "send",
            "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH",
            "--asset",
            "TRX",
            "--amount",
            "0.0000001",
        ]
    )
    rc = cli._send_tron(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "ABORTED" in err
    assert "sun" in err


def test_swap_from_tron_native_max_still_rejected():
    """Native TRX sweep stays unsupported (it needs a TRX fee reserve)."""
    import swapsack.cli as cli

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

    from swapsack.cli import _wallet_adapters

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


def test_swap_backend_accepts_chainflip():
    args = build_parser().parse_args(
        ["swap", "--amount", "0.001", "--backend", "chainflip"]
    )
    assert args.backend == "chainflip"


# --- price-only backends are quotable but not swappable ---------------------


class _PriceOnlyBackend:
    """A backend that can quote but whose executor no swap path can drive."""

    name = "somevenue"
    executor = "deposit-channel"

    def __init__(self, out=10**9):
        self.out = out
        self.client = SimpleNamespace(close=lambda: None)

    def serves(self, *a):
        return True

    def try_quote(self, *a, **kw):
        return SimpleNamespace(expected_amount_out=self.out)


class _ExecutableBackend:
    name = "thorchain"
    executor = "memo-deposit"

    def __init__(self, out=1):
        self.out = out
        self.client = SimpleNamespace(close=lambda: None)

    def serves(self, *a):
        return True

    def try_quote(self, *a, **kw):
        return SimpleNamespace(expected_amount_out=self.out)


def _select(
    monkeypatch, backends, backend_arg="auto", executors=None, to_asset="ETH.ETH"
):
    monkeypatch.setattr(cli, "_backends_for", lambda args: backends)
    args = build_parser().parse_args(
        ["swap", "--amount", "0.1", "--backend", backend_arg]
    )
    return cli._select_backend(
        args,
        from_asset="BTC.BTC",
        to_asset=to_asset,
        amount=10_000_000,
        destination="0xdead",
        executors=executors if executors is not None else cli.EXECUTABLE_EXECUTORS,
    )


def test_explicit_price_only_backend_is_refused_with_a_usable_message(monkeypatch):
    with pytest.raises(cli.SwapAborted) as exc:
        _select(monkeypatch, [_PriceOnlyBackend()], backend_arg="chainflip")
    assert "cannot execute a swap from" in str(exc.value)
    assert "quote --backend somevenue" in str(exc.value)


class _VaultSwapBackend(_PriceOnlyBackend):
    name = "chainflip"
    executor = "vault-swap"


class _SignedOrderBackend(_PriceOnlyBackend):
    name = "cow"
    executor = "signed-order"


def test_a_vault_swap_backend_is_drivable_from_a_utxo_source(monkeypatch):
    chosen = _select(monkeypatch, [_VaultSwapBackend()], executors=cli.UTXO_EXECUTORS)
    assert chosen.name == "chainflip"


def test_a_vault_swap_backend_is_refused_from_an_evm_source(monkeypatch):
    # A Chainflip vault swap is a Bitcoin transaction; the EVM path cannot
    # build one, so selection must refuse rather than hand it the quote.
    with pytest.raises(cli.SwapAborted):
        _select(monkeypatch, [_VaultSwapBackend()], executors=cli.EVM_EXECUTORS)


def test_a_signed_order_backend_is_refused_from_a_utxo_source(monkeypatch):
    with pytest.raises(cli.SwapAborted):
        _select(monkeypatch, [_SignedOrderBackend()], executors=cli.UTXO_EXECUTORS)


def test_auto_routes_around_a_price_only_backend(monkeypatch):
    chosen = _select(monkeypatch, [_ExecutableBackend(), _PriceOnlyBackend()])
    assert chosen.name == "thorchain"


def test_auto_says_out_loud_when_the_price_only_backend_was_cheaper(
    monkeypatch, capsys
):
    _select(monkeypatch, [_ExecutableBackend(out=1), _PriceOnlyBackend(out=10**9)])
    err = capsys.readouterr().err
    assert "somevenue quoted" in err
    assert "cannot execute yet" in err


def test_auto_names_a_backend_this_source_cannot_drive(monkeypatch, capsys):
    # Chainflip can execute — just not from an EVM source. The note is still
    # the honest thing to print: the price is real and reachable another way.
    _select(
        monkeypatch,
        [_ExecutableBackend(out=1), _VaultSwapBackend(out=10**9)],
        executors=cli.EVM_EXECUTORS,
    )
    assert "chainflip quoted" in capsys.readouterr().err


def test_auto_stays_quiet_when_the_price_only_backend_was_not_cheaper(
    monkeypatch, capsys
):
    _select(monkeypatch, [_ExecutableBackend(out=10**9), _PriceOnlyBackend(out=1)])
    assert "somevenue quoted" not in capsys.readouterr().err


class _PartialVaultSwapBackend(_VaultSwapBackend):
    """Chainflip's real shape: it *lists* Tron and quotes it happily, but the
    vault-swap gate cannot encode a Tron destination, so it cannot settle one."""

    def can_execute(self, from_asset, to_asset):
        return to_asset != "TRON.TRX"


def test_auto_routes_around_a_destination_the_backend_cannot_settle(
    monkeypatch, capsys
):
    # Winning on price and then raising in the payload encoder is exit 1 for a
    # pair that has a working route — so the narrowing belongs at selection.
    chosen = _select(
        monkeypatch,
        [_ExecutableBackend(out=1), _PartialVaultSwapBackend(out=10**9)],
        executors=cli.UTXO_EXECUTORS,
        to_asset="TRON.TRX",
    )
    assert chosen.name == "thorchain"
    assert "chainflip quoted" in capsys.readouterr().err


def test_auto_still_routes_to_that_backend_for_a_destination_it_can_settle(
    monkeypatch,
):
    chosen = _select(
        monkeypatch,
        [_ExecutableBackend(out=1), _PartialVaultSwapBackend(out=10**9)],
        executors=cli.UTXO_EXECUTORS,
        to_asset="ETH.ETH",
    )
    assert chosen.name == "chainflip"


def test_an_explicit_backend_that_cannot_settle_the_destination_is_refused(
    monkeypatch,
):
    with pytest.raises(cli.SwapAborted) as exc:
        _select(
            monkeypatch,
            [_PartialVaultSwapBackend()],
            backend_arg="chainflip",
            executors=cli.UTXO_EXECUTORS,
            to_asset="TRON.TRX",
        )
    assert "TRON.TRX" in str(exc.value)


def test_auto_aborts_when_only_a_price_only_backend_can_serve(monkeypatch):
    with pytest.raises(cli.SwapAborted):
        _select(monkeypatch, [_PriceOnlyBackend()])


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
    from swapsack.cli import cmd_send

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
    # The send path now reads these off the adapter instead of hardcoding "ETH",
    # so a second EVM chain labels its own gas coin.
    chain = "ETH"
    native_symbol = "ETH"

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

        from swapsack.swap import Prepared

        self._captured.update(kw)
        return Prepared(
            quote=None, built=SimpleNamespace(fee=10**14), plan=None, problems=[]
        )


def test_send_eth_native_dry_run(monkeypatch):
    import swapsack.cli as cli

    captured = {}
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setitem(
        cli._EVM_ADAPTERS, "ETH", lambda args, passphrase="": _FakeEthSend(captured)
    )
    args = build_parser().parse_args(
        ["send", ETH_RECIP, "--asset", "ETH", "--amount", "0.001"]
    )
    assert cli.cmd_send(args) == 0  # dry run, verify gate clean
    assert captured["recipient"] == ETH_RECIP
    assert captured["asset"] == "ETH.ETH"
    assert captured["amount"] == 100_000  # 0.001 ETH in 1e8 units


def test_send_eth_token_sweep_uses_full_balance(monkeypatch):
    import swapsack.cli as cli

    captured = {}
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setitem(
        cli._EVM_ADAPTERS, "ETH", lambda args, passphrase="": _FakeEthSend(captured)
    )
    args = build_parser().parse_args(
        ["send", ETH_RECIP, "--asset", "USDT-ETH", "--amount", "max"]
    )
    assert cli.cmd_send(args) == 0
    assert captured["amount"] == 250_000_000  # 2.5 USDT in 1e8 units
    assert captured["asset"].startswith("ETH.USDT-")


def test_send_eth_rejects_bad_recipient():
    import swapsack.cli as cli

    args = build_parser().parse_args(
        ["send", "0xnothex", "--asset", "ETH", "--amount", "1"]
    )
    assert cli.cmd_send(args) == 2  # gross-format recipient rejected before build


def test_send_tron_native_max_refused():
    import swapsack.cli as cli

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
    from swapsack.cli import main

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
    assert ASSET["AVAX"] == "AVAX.AVAX"
    assert ASSET["USDT-AVAX"].startswith("AVAX.USDT-")
    # Destination-only assets (item 3).
    assert ASSET["LTC"] == "LTC.LTC"
    assert ASSET["DOGE"] == "DOGE.DOGE"
    assert ASSET["BCH"] == "BCH.BCH"


def test_swap_to_ltc_parses():
    args = build_parser().parse_args(
        ["swap", "--to", "LTC", "--amount", "0.01", "--dest", "ltc1qexample"]
    )
    assert ASSET[args.to_] == "LTC.LTC"


def test_balance_skips_lp_probe_for_poolless_adapters(monkeypatch):
    # BSC has no pools on either network and the settlement assets (CACAO/RUNE)
    # have no pool of themselves — probing them is guaranteed-404 HTTP round
    # trips (up to the full timeout each) for zero information.
    import swapsack.backends as backends_mod
    import swapsack.cli as cli

    def FakeReport():  # noqa: N802 (a factory named like the class it replaces)
        from swapsack.chains.base import BalanceReport

        return BalanceReport(
            symbol="X", confirmed=100_000_000, decimals=8, addresses=("addr1",)
        )

    class FakeAdapter:
        def __init__(self, chain, lp_backends=None):
            self.chain = chain
            self.asset = f"{chain}.{chain}"
            self.lp_backends = lp_backends

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def wallet_balance(self, mnemonic):
            return FakeReport()

    probed = []
    monkeypatch.setattr(
        cli,
        "_report_liquidity",
        lambda backends, asset, addrs, protocol=None, rows=None: probed.append(asset),
    )
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (MNEMONIC, ""))
    monkeypatch.setattr(
        cli,
        "_wallet_adapters",
        lambda args, p="": [FakeAdapter("BTC"), FakeAdapter("BSC", lp_backends=())],
    )
    monkeypatch.setattr(backends_mod, "default_backends", lambda: [])
    args = build_parser().parse_args(["balance"])
    assert cli.cmd_balance(args) == 0
    assert probed == ["BTC.BTC"]


def test_balance_probes_maya_only_chains_on_maya_only(monkeypatch):
    # DASH.DASH pools exist only on Maya, and THORChain answers a probe for a
    # pool it doesn't run with a 500 (not a clean "no position" 404) — so
    # `balance` must not probe THORChain for a Maya-only chain at all.
    import swapsack.backends as backends_mod
    import swapsack.cli as cli
    from swapsack.chains.dash import DashAdapter

    def FakeReport():  # noqa: N802 (a factory named like the class it replaces)
        from swapsack.chains.base import BalanceReport

        return BalanceReport(
            symbol="DASH",
            confirmed=100_000_000,
            decimals=8,
            addresses=("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5",),
        )

    class FakeClient:
        def close(self):
            pass

    class FakeBackend:
        executor = "memo-deposit"

        def __init__(self, name):
            self.name = name
            self.client = FakeClient()

    class FakeDashAdapter:
        chain = "DASH"
        asset = "DASH.DASH"
        # Mirror the real adapter's flag so the class attribute drives the test.
        lp_backends = DashAdapter.lp_backends

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def wallet_balance(self, mnemonic):
            return FakeReport()

    probed = []
    monkeypatch.setattr(
        cli,
        "_report_liquidity",
        lambda backends, asset, addrs, protocol=None, rows=None: probed.append(
            (asset, tuple(b.name for b in backends))
        ),
    )
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (MNEMONIC, ""))
    monkeypatch.setattr(cli, "_wallet_adapters", lambda args, p="": [FakeDashAdapter()])
    monkeypatch.setattr(
        backends_mod,
        "default_backends",
        lambda: [FakeBackend("thorchain"), FakeBackend("maya")],
    )
    args = build_parser().parse_args(["balance"])
    assert cli.cmd_balance(args) == 0
    assert probed == [("DASH.DASH", ("maya",))]


def test_poolless_adapters_are_flagged():
    from swapsack.chains.bsc import BscAdapter
    from swapsack.chains.btc import BtcAdapter
    from swapsack.chains.dash import DashAdapter
    from swapsack.chains.maya import MayaAdapter
    from swapsack.chains.thor import ThorAdapter

    assert BscAdapter.lp_backends == ()  # no BSC pools anywhere (documented)
    # CACAO is Maya's settlement asset — no MAYA.CACAO pool on Maya, and
    # THORChain doesn't trade Maya assets — so it is genuinely pool-less.
    assert MayaAdapter.lp_backends == ()
    # RUNE is THORChain's settlement asset (no pool on THORChain) but Maya runs
    # a live THOR.RUNE pool, so RUNE LP positions DO exist and must be probed.
    assert getattr(ThorAdapter, "lp_backends", None) is None
    assert getattr(BtcAdapter, "lp_backends", None) is None
    assert DashAdapter.lp_backends == ("maya",)  # Maya-only pool


def test_balance_probes_rune_pool_on_maya(monkeypatch):
    # Regression: RUNE has a live THOR.RUNE pool on Maya, so `balance` must
    # still probe THOR.RUNE — a blanket cosmos "no LP pools" flag silently hid
    # RUNE LP positions (funds appeared to vanish from the accounting). The
    # fake mirrors the real ThorAdapter's lp_backends flag so the class
    # attribute drives the probe decision.
    import swapsack.backends as backends_mod
    import swapsack.cli as cli
    from swapsack.chains.thor import ThorAdapter

    def FakeReport():  # noqa: N802 (a factory named like the class it replaces)
        from swapsack.chains.base import BalanceReport

        return BalanceReport(
            symbol="RUNE", confirmed=100_000_000, decimals=8, addresses=("thor1abc",)
        )

    class FakeThorAdapter:
        chain = "THOR"
        asset = "THOR.RUNE"
        lp_backends = getattr(ThorAdapter, "lp_backends", None)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def wallet_balance(self, mnemonic):
            return FakeReport()

    probed = []
    monkeypatch.setattr(
        cli,
        "_report_liquidity",
        lambda backends, asset, addrs, protocol=None, rows=None: probed.append(asset),
    )
    monkeypatch.setattr(cli, "_token_rows_with_pools", lambda a, m: [])
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (MNEMONIC, ""))
    monkeypatch.setattr(cli, "_wallet_adapters", lambda args, p="": [FakeThorAdapter()])
    monkeypatch.setattr(backends_mod, "default_backends", lambda: [])
    args = build_parser().parse_args(["balance"])
    assert cli.cmd_balance(args) == 0
    assert probed == ["THOR.RUNE"]


def test_derivable_chains_all_actually_derive():
    # DERIVABLE_CHAINS and the derivation logic must be one source: every chain
    # the tuple advertises must actually produce an address (no drift where the
    # tuple lists a chain that _derive_destination_address returns None for, or
    # vice versa).
    from swapsack.cli import DERIVABLE_CHAINS, _derive_destination_address

    for chain in DERIVABLE_CHAINS:
        addr = _derive_destination_address(chain, MNEMONIC)
        assert addr, f"{chain} in DERIVABLE_CHAINS but derived nothing"
    # A chain not in the set derives nothing (needs an explicit --dest).
    assert _derive_destination_address("LTC", MNEMONIC) is None


def test_resolve_destination_derives_maya_and_thor():
    # The MAYA/THOR adapters expose derive_address, so `swap --to CACAO/RUNE`
    # must not demand a --dest the wallet itself prints in `address`.
    import swapsack.cli as cli

    args = build_parser().parse_args(["swap", "--to", "CACAO", "--amount", "1"])
    assert (
        cli._resolve_destination(args, MNEMONIC)
        == "maya1gm00vwsfcp48enm4uv9e5dhm37jtd0ye2fs0sl"
    )
    args = build_parser().parse_args(["swap", "--to", "RUNE", "--amount", "1"])
    assert (
        cli._resolve_destination(args, MNEMONIC)
        == "thor1gm00vwsfcp48enm4uv9e5dhm37jtd0ye27wrx0"
    )


def test_quote_derives_cacao_destination(monkeypatch):
    # cmd_quote's derivable-chain set must be the same one _resolve_destination
    # uses (they were two hardcoded copies that drifted independently).
    import swapsack.backends as backends_mod
    import swapsack.cli as cli

    captured = {}

    def fake_gather(backends, from_a, to_a, amount, dest, **kw):
        captured["dest"] = dest
        return []

    monkeypatch.setattr(backends_mod, "gather_quotes", fake_gather)
    monkeypatch.setattr(cli, "_backends_for", lambda args: [])
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (MNEMONIC, ""))
    args = build_parser().parse_args(
        ["quote", "--from", "BTC", "--to", "CACAO", "--amount", "1"]
    )
    assert cli.cmd_quote(args) == 1  # our stub returns no quotes
    assert captured["dest"] == "maya1gm00vwsfcp48enm4uv9e5dhm37jtd0ye2fs0sl"


def test_quote_pins_native_source_to_home_backend(monkeypatch):
    # A native RUNE/CACAO source deposits on its own network, so quote must
    # price it only on the home backend — matching what swap will execute.
    # Otherwise quote could advertise a maya route for THOR.RUNE that the swap
    # command refuses (or silently ignores).
    from types import SimpleNamespace

    import swapsack.backends as backends_mod
    import swapsack.cli as cli

    captured = {}

    def fake_gather(backends, from_a, to_a, amount, dest, **kw):
        captured["backends"] = [b.name for b in backends]
        return []

    monkeypatch.setattr(backends_mod, "gather_quotes", fake_gather)
    monkeypatch.setattr(
        backends_mod,
        "get_backend",
        lambda name: SimpleNamespace(name=name, client=_ClosableClient()),
    )
    args = build_parser().parse_args(
        [
            "quote",
            "--from",
            "RUNE",
            "--to",
            "BTC",
            "--amount",
            "1",
            "--dest",
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        ]
    )
    assert cli.cmd_quote(args) == 1  # our stub returns no quotes
    assert captured["backends"] == ["thorchain"]


def test_quote_refuses_foreign_backend_for_native_source():
    # Consistent with swap: an explicit foreign --backend for a native source
    # is refused, not silently re-pointed.
    import swapsack.cli as cli

    args = build_parser().parse_args(
        [
            "quote",
            "--from",
            "RUNE",
            "--to",
            "BTC",
            "--amount",
            "1",
            "--dest",
            "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            "--backend",
            "maya",
        ]
    )
    assert cli.cmd_quote(args) == 2


class _ClosableClient:
    def close(self):
        return None


def test_resolve_destination_rejects_bad_dest():
    from swapsack.cli import _resolve_destination

    args = build_parser().parse_args(
        ["swap", "--to", "LTC", "--amount", "0.01", "--dest", "not-a-real-address!!"]
    )
    with pytest.raises(SystemExit):
        _resolve_destination(args, mnemonic=None)


def test_resolve_destination_accepts_good_ltc_dest():
    from swapsack.cli import _resolve_destination

    dest = "ltc1qjmxnz78nmc8nq77wuxh25n2es7rzm5c2rkk4wh"
    args = build_parser().parse_args(
        ["swap", "--to", "LTC", "--amount", "0.01", "--dest", dest]
    )
    assert _resolve_destination(args, mnemonic=None) == dest


# --- destination-chain caveats -----------------------------------------------

ADA_DEST = (
    "addr1qxf4ppedwy4pylzff47uxhjxkz7fuchz3q3ewz89s80u8g05et60l9tnrg37f8"
    "9emhs5r6xxnq80tt6l0k5y0dlcqfcqtrftvm"
)
XRP_DEST = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
ATOM_DEST = "cosmos1tjjcfptfjmzm5zl9sr3r6n4dqmvqckl9a8nz3h"


def _swap_args(from_, to_, dest):
    return build_parser().parse_args(
        ["swap", "--from", from_, "--to", to_, "--amount", "0.01", "--dest", dest]
    )


def test_ada_from_a_utxo_source_is_refused_up_front():
    """A Cardano address is 103 chars, so the shortest possible memo is already
    over the 80-byte OP_RETURN cap — the backend does refuse, but as "no
    quotes", which reads like a missing pool rather than a permanent limit."""
    from swapsack.cli import _resolve_destination

    for source in ["BTC", "DASH", "ZEC"]:
        with pytest.raises(SystemExit) as excinfo:
            _resolve_destination(_swap_args(source, "ADA", ADA_DEST), mnemonic=None)
        assert "OP_RETURN" in str(excinfo.value)


def test_ada_from_an_account_model_source_is_allowed():
    # ETH puts the memo in calldata, where the limit does not apply — the whole
    # point of the message the UTXO sources get.
    from swapsack.cli import _resolve_destination

    assert _resolve_destination(_swap_args("ETH", "ADA", ADA_DEST), None) == ADA_DEST


def test_a_short_dest_from_a_utxo_source_is_untouched():
    # The guard must not fire on ordinary destinations — a taproot address is
    # the longest common one at 62 chars, comfortably inside the cap.
    from swapsack.cli import _resolve_destination

    taproot = "bc1p5d7rjq7g6rdk2yhzks9smlaqtedr4dekq08ge8ztwac72sfr9rusxg3297"
    assert _resolve_destination(_swap_args("BTC", "BTC", taproot), None) == taproot


def test_xrp_dest_warns_that_no_destination_tag_can_be_sent(capsys):
    from swapsack.cli import _resolve_destination

    assert _resolve_destination(_swap_args("BTC", "XRP", XRP_DEST), None) == XRP_DEST
    warning = capsys.readouterr().err
    assert "destination tag" in warning
    assert "exchange deposit address" in warning


def test_atom_dest_is_accepted_without_a_warning(capsys):
    from swapsack.cli import _resolve_destination

    assert _resolve_destination(_swap_args("BTC", "ATOM", ATOM_DEST), None) == ATOM_DEST
    assert capsys.readouterr().err == ""


EVM_DEST = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"


@pytest.mark.parametrize(
    ("key", "asset"),
    [
        ("USDC-AVAX", "AVAX.USDC-0XB97EF9EF8734C71904D8002F8B6BC66DD9C48A6E"),
        ("USDC-ARB", "ARB.USDC-0XAF88D065E77C8CC2239327C5EDB3A432268E5831"),
        ("USDT-AVAX", "AVAX.USDT-0X9702230A8EA53601F5CD2DC00FDBC13D4DF4A8C7"),
    ],
)
def test_usdc_on_a_cheaper_chain_names_that_chains_own_contract(key, asset):
    """The contract is what decides which token lands — it is not interchangeable.

    USDC has a different contract address on every chain, and a swap memo that
    named the wrong one would either fail to route or pay out a different token
    entirely. Pinned against the live pool listing rather than trusted to a
    careful paste.
    """
    from swapsack.cli import ASSET, _derivable_chain

    assert ASSET[key] == asset
    assert _derivable_chain(key) == asset.split(".", 1)[0]


@pytest.mark.parametrize("key", ["USDC-AVAX", "USDC-ARB", "ETH-ARB", "USDT-AVAX"])
def test_a_non_mainnet_evm_dest_warns_which_chain_it_pays_on(key, capsys):
    """Every EVM chain shares one address space, so the address cannot tell you
    which chain a payout lands on — and an exchange deposit address that only
    credits Ethereum mainnet will swallow an Arbitrum or Avalanche payout."""
    from swapsack.cli import _resolve_destination

    assert _resolve_destination(_swap_args("BTC", key, EVM_DEST), None) == EVM_DEST
    warning = capsys.readouterr().err
    assert "exchange deposit address" in warning
    # Name the chain the funds actually land on, not just "not mainnet".
    assert key.rsplit("-", 1)[1].lower() in warning.lower()


def test_an_ethereum_mainnet_dest_gets_no_chain_warning(capsys):
    # The warning has to stay rare to stay read: mainnet USDC is the ordinary
    # case and the address means exactly what it looks like.
    from swapsack.cli import _resolve_destination

    assert _resolve_destination(_swap_args("BTC", "USDC-ETH", EVM_DEST), None) == (
        EVM_DEST
    )
    assert capsys.readouterr().err == ""


def test_resolve_destination_takes_only_the_address_from_a_uri():
    """A `--dest` URI contributes its address and nothing else, deliberately.

    `send` aborts when a URI `amount=` contradicts `--amount`, but on a swap the
    two mean different things — `--amount` is what you sell, a URI amount would
    be what the payee wants to receive — so there is nothing to reconcile and
    the parameters are dropped. Pinned because the guard's absence here should
    be a decision, not an oversight.
    """
    from swapsack.cli import _resolve_destination

    dest = "ltc1qjmxnz78nmc8nq77wuxh25n2es7rzm5c2rkk4wh"
    args = build_parser().parse_args(
        [
            "swap",
            "--to",
            "LTC",
            "--amount",
            "0.01",
            "--dest",
            f"litecoin:{dest}?amount=99&label=Alice",
        ]
    )
    assert _resolve_destination(args, mnemonic=None) == dest
    # The sell amount is untouched by the URI's amount.
    assert args.amount == Decimal("0.01")


def test_resolve_destination_for_usdt_targets():
    pytest.importorskip("eth_account")
    from types import SimpleNamespace

    from swapsack.cli import _resolve_destination

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
    from swapsack.cli import _amount, _base_units

    assert _base_units(_amount("93393106.59778857")) == 9339310659778857


def test_base_units_round_trip_simple():
    from swapsack.cli import _amount, _base_units

    assert _base_units(_amount("0.5")) == 50_000_000


@pytest.mark.parametrize("cmd", ["swap", "send"])
def test_amount_rejects_below_finest_unit(cmd):
    # The parse-time floor is the finest supported base unit (CACAO's 1e-10);
    # anything below can't be a whole number of base units for ANY asset.
    argv = (
        ["send", "bc1qx", "--amount", "0.00000000001"]
        if cmd == "send"
        else ["swap", "--amount", "0.00000000001"]
    )
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_amount_accepts_cacao_scale_amounts():
    # 5e-9 is 50 CACAO base units (1e-10) — a perfectly sendable amount that
    # the old 1e-8 parse floor wrongly refused. The asset is unknown at parse
    # time, so per-asset enforcement lives in _base_units.
    from swapsack.cli import _amount

    assert _amount("0.000000005") == Decimal("0.000000005")


def test_base_units_rejects_amount_below_one_base_unit():
    # 1e-9 scales to 0.1 of a 1e8 base unit -> would round to 0 and burn a fee
    # on a no-op send. The guard moved here from _amount, where the per-asset
    # unit wasn't known; with CACAO's 1e10 unit the same amount is fine.
    from swapsack.cli import _base_units
    from swapsack.swap import SwapAborted

    with pytest.raises(SwapAborted, match="base unit"):
        _base_units(Decimal("0.000000001"))
    assert _base_units(Decimal("0.000000001"), 10**10) == 10


def test_base_units_rejects_sub_unit_amount_that_would_round_up():
    # Regression: an amount in (0.5, 1) base units (e.g. 0.6 sat) must be
    # rejected, NOT silently rounded UP to 1 and sent — that ships ~1.67x what
    # the user typed. The floor is one *whole* base unit, checked on the
    # unrounded product, not on the ROUND_HALF_EVEN result.
    from swapsack.cli import _base_units
    from swapsack.swap import SwapAborted

    with pytest.raises(SwapAborted, match="base unit"):
        _base_units(Decimal("0.000000006"))  # 0.6 sat at 1e8
    # A whole base unit and above still scales (1.5 sat rounds to 2, unchanged).
    assert _base_units(Decimal("0.00000001")) == 1
    assert _base_units(Decimal("0.000000015")) == 2


def test_main_prints_aborted_for_escaped_swap_aborted(monkeypatch, capsys):
    # _base_units can raise SwapAborted from handlers with no local handler
    # (e.g. cmd_quote); main() must turn it into the standard ABORTED message,
    # not a traceback.
    import swapsack.cli as cli
    from swapsack.swap import SwapAborted

    def boom(args):
        raise SwapAborted("test escape")

    monkeypatch.setattr(cli, "cmd_quote", boom)
    rc = cli.main(["quote", "--amount", "1"])
    assert rc == 1
    assert "ABORTED: test escape" in capsys.readouterr().err


def test_main_prints_one_line_for_an_escaped_network_failure(monkeypatch, capsys):
    # A read timeout against a public API is not a bug in swapsack: the read
    # paths that have no local handler (the HD scan, for one) must still exit
    # with a one-liner naming the host, not a 60-line traceback.
    import niquests

    import swapsack.cli as cli

    def boom(args):
        raise niquests.exceptions.ReadTimeout(
            "HTTPSConnectionPool(host='blockstream.info', port=443): Read timed out."
        )

    monkeypatch.setattr(cli, "cmd_quote", boom)
    rc = cli.main(["quote", "--amount", "1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "blockstream.info" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("from_asset", "expected"),
    [("CACAO", 100 * 10**10), ("BTC", 100 * 10**8)],
)
def test_quote_scales_amount_per_source_asset(monkeypatch, from_asset, expected):
    # The quote API speaks the source asset's native unit (CACAO is 1e10, not
    # the shared 1e8): a fixed-1e8 scaling quoted 1/100th of the typed CACAO
    # amount — a wildly misleading price preview.
    import swapsack.backends as backends_mod
    import swapsack.cli as cli

    captured = {}

    def fake_gather(backends, from_a, to_a, amount, dest, **kw):
        captured["amount"] = amount
        return []

    monkeypatch.setattr(backends_mod, "gather_quotes", fake_gather)
    monkeypatch.setattr(cli, "_backends_for", lambda args: [])
    args = build_parser().parse_args(
        [
            "quote",
            "--from",
            from_asset,
            "--to",
            "ETH",
            "--amount",
            "100",
            "--dest",
            "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
        ]
    )
    rc = cli.cmd_quote(args)
    assert rc == 1  # our stub returns no quotes
    assert captured["amount"] == expected


# --- BIP-39 passphrase threaded out of the keystore (finding #1) ---


def test_load_mnemonic_returns_bip39_passphrase(tmp_path, monkeypatch):
    import swapsack.cli as cli
    from swapsack.keystore import Keystore

    path = tmp_path / "ks.json"
    ks = Keystore()
    ks.add_hd("w", MNEMONIC, passphrase="extra-word")
    ks.save(path, "pw", n=1024)
    monkeypatch.setenv("SWAPSACK_KEYSTORE", str(path))
    monkeypatch.setenv("SWAPSACK_PASSPHRASE", "pw")

    args = build_parser().parse_args(["address"])
    mnemonic, passphrase = cli._load_mnemonic(args)
    assert mnemonic == MNEMONIC
    assert passphrase == "extra-word"


def test_cli_renders_v1_passphrase_strip_warning(tmp_path, monkeypatch, capsys):
    # The v1 passphrase-strip warning lives at the CLI boundary (keystore.load
    # is silent and only records the labels). Loading a v1-with-passphrase
    # keystore through the CLI must print it, naming the key.
    import json as _json

    import swapsack.cli as cli
    from swapsack.keystore import Keystore

    path = tmp_path / "ks.json"
    ks = Keystore()
    ks.add_hd("w", MNEMONIC, passphrase="extra-word")
    ks.save(path, "pw", n=1024)
    env = _json.loads(path.read_text())
    env["version"] = 1
    path.write_text(_json.dumps(env))
    monkeypatch.setenv("SWAPSACK_KEYSTORE", str(path))
    monkeypatch.setenv("SWAPSACK_PASSPHRASE", "pw")

    args = build_parser().parse_args(["address"])
    cli._load_mnemonic(args)
    err = capsys.readouterr().err
    assert "w" in err
    assert "passphrase" in err


# --- uncaught InsufficientFunds on a non-sweep BTC swap (finding #4) ---


def test_swap_from_btc_insufficient_funds_aborts_cleanly(monkeypatch):
    import swapsack.cli as cli
    from swapsack.chains.coins import InsufficientFunds, Utxo

    class FakeBtc:
        chain = "BTC"
        asset = "BTC.BTC"
        account = "m/84'/0'/0'"
        change_path = "m/84'/0'/0'/1/0"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def derive_address(self, mnemonic, path=None):
            return "bc1qchange"

        def address_info(self, address):
            return None  # unused: scan_account is stubbed

        def fetch_utxos(self, address, *, include_unconfirmed=False):
            return [Utxo(txid="aa" * 32, vout=0, value=100_000, address=address)]

        def fetch_fee_rate(self, target_blocks=2):
            return 5.0

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeBackend:
        name = "thorchain"
        executor = "memo-deposit"
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
    monkeypatch.setattr("swapsack.chains.scan.scan_account", fake_scan)
    monkeypatch.setattr(cli, "prepare_swap", boom)

    args = build_parser().parse_args(
        ["swap", "--from", "BTC", "--to", "ETH", "--amount", "0.5"]
    )
    rc = cli._swap_from_utxo(args, cli._btc_adapter)
    assert rc == 1  # clean ABORTED, not a traceback


def test_swap_routes_dash_to_the_utxo_handler(monkeypatch):
    import swapsack.cli as cli

    routed = {}

    def fake_swap_from_utxo(args, adapter_factory):
        routed.update(factory=adapter_factory)
        return 0

    monkeypatch.setattr(cli, "_swap_from_utxo", fake_swap_from_utxo)
    args = build_parser().parse_args(
        ["swap", "--from", "DASH", "--to", "BTC", "--amount", "0.5"]
    )
    assert cli.cmd_swap(args) == 0
    assert routed["factory"] is cli._dash_adapter


def test_utxo_registry_carries_the_exact_derivation_paths():
    # Finding 10: account/change paths live on the adapter classes (not restated
    # in cli constants that could drift). Pin the exact BIP44/84 paths so a typo
    # in a money-sensitive derivation path fails loudly here rather than sending
    # change to — or scanning — the wrong address.
    import swapsack.cli as cli
    from swapsack.chains.btc import BtcAdapter
    from swapsack.chains.dash import DashAdapter
    from swapsack.chains.zcash import ZecAdapter

    assert set(cli._UTXO_ADAPTERS) == {"BTC", "DASH", "ZEC"}
    expected = {
        BtcAdapter: ("m/84'/0'/0'", "m/84'/0'/0'/1/0"),
        DashAdapter: ("m/44'/5'/0'", "m/44'/5'/0'/1/0"),
        ZecAdapter: ("m/44'/133'/0'", "m/44'/133'/0'/1/0"),
    }
    for cls, (account, change_path) in expected.items():
        assert cls.account == account
        assert cls.change_path == change_path
        # The change path is the account's internal chain; the receive default
        # is its external chain — both under the same account.
        assert cls.change_path.startswith(cls.account + "/1/")
        assert cls.default_derivation.startswith(cls.account + "/0/")


def test_swap_and_send_pass_the_change_path_address_as_change(monkeypatch):
    """The CLI must fund change from the adapter's *change* path.

    Adapter-level tests prove the builder honours whatever ``change_address``
    it is handed, and the gate puts that address into the owned set by
    construction — so if the CLI handed over the wrong address, nothing
    downstream would object. On a partial (non-sweep) spend the change output
    carries the whole remainder of the wallet, so pin the wiring here.
    """
    import swapsack.cli as cli
    from swapsack.chains.coins import Utxo

    class FakeBtc:
        chain = "BTC"
        asset = "BTC.BTC"
        account = "m/84'/0'/0'"
        change_path = "m/84'/0'/0'/1/0"
        default_derivation = "m/84'/0'/0'/0/0"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def derive_address(self, mnemonic, path=None):
            return f"bc1q-addr-for-{path}"  # path-dependent, so a mix-up shows

        def address_info(self, address):
            return None  # unused: scan_account is stubbed

        def fetch_utxos(self, address, *, include_unconfirmed=False):
            return [Utxo(txid="aa" * 32, vout=0, value=1_000_000, address=address)]

        def fetch_fee_rate(self, target_blocks=2):
            return 5.0

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeBackend:
        name = "thorchain"
        executor = "memo-deposit"
        client = FakeClient()

    def fake_scan(*, derive_address, probe, account):
        from types import SimpleNamespace

        assert account == FakeBtc.account
        return [
            (
                "m/84'/0'/0'/0/0",
                "bc1q-addr-for-m/84'/0'/0'/0/0",
                SimpleNamespace(confirmed=1_000_000),
            )
        ]

    expected_change = f"bc1q-addr-for-{FakeBtc.change_path}"

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(cli, "_resolve_destination", lambda args, m, p="": "bc1qdest")
    monkeypatch.setattr(cli, "_btc_adapter", lambda args, passphrase="": FakeBtc())
    monkeypatch.setattr(cli, "_select_backend", lambda *a, **k: FakeBackend())
    monkeypatch.setattr("swapsack.chains.scan.scan_account", fake_scan)

    # --- swap --from BTC (non-sweep): capture what prepare_swap is handed ---
    captured: dict = {}

    def fake_prepare_swap(**kwargs):
        captured.update(kwargs)
        raise SystemExit(0)  # stop before quoting/printing

    monkeypatch.setattr(cli, "prepare_swap", fake_prepare_swap)
    args = build_parser().parse_args(
        ["swap", "--from", "BTC", "--to", "ETH", "--amount", "0.001"]
    )
    with pytest.raises(SystemExit):
        cli._swap_from_utxo(args, cli._btc_adapter)
    assert captured["change_address"] == expected_change
    assert captured["sweep"] is False

    # --- send BTC (non-sweep): same guarantee on the plain-send path ---
    sent: dict = {}

    class SendingBtc(FakeBtc):
        def build_and_verify_send(self, **kwargs):
            sent.update(kwargs)
            raise SystemExit(0)

    monkeypatch.setattr(cli, "_btc_adapter", lambda args, passphrase="": SendingBtc())
    args = build_parser().parse_args(
        ["send", "--asset", "BTC", "--amount", "0.001", "bc1qrecipient"]
    )
    with pytest.raises(SystemExit):
        cli._send_utxo(args, cli._btc_adapter)
    assert sent["change_address"] == expected_change
    assert sent["sweep"] is False


def test_add_liquidity_btc_not_refused_by_the_hoisted_lp_check(monkeypatch):
    # The lp_backends check now runs for every UTXO chain (was DASH/ZEC only).
    # BTC has no restriction, so the uniform check must still let it through.
    import swapsack.cli as cli

    called = []
    monkeypatch.setattr(cli, "_liquidity_utxo", lambda *a, **kw: called.append(1) or 0)
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "BTC", "--amount", "1"]
    )
    assert cli.cmd_add_liquidity(args) == 0
    assert called


def test_add_liquidity_dash_requires_maya_backend(monkeypatch, capsys):
    # DASH.DASH pools exist only on Maya (adapter.lp_backends); an LP request
    # against THORChain must refuse up front, before any keystore/network work.
    import swapsack.cli as cli

    called = []
    monkeypatch.setattr(cli, "_liquidity_utxo", lambda *a, **kw: called.append(1) or 0)
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "DASH", "--amount", "1"]
    )  # --backend defaults to thorchain
    assert cli.cmd_add_liquidity(args) == 2
    assert not called
    assert "maya" in capsys.readouterr().err.lower()

    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "DASH", "--amount", "1", "--backend", "maya"]
    )
    assert cli.cmd_add_liquidity(args) == 0
    assert called


def test_select_backend_refuses_explicit_backend_that_cannot_serve_the_pair(
    monkeypatch,
):
    # A single explicit --backend is returned unchecked today; cow's serves()
    # rules out non-ETH pairs, so this must raise SwapAborted rather than
    # handing back a backend the swap dispatch can't drive (AttributeError).
    from types import SimpleNamespace

    import swapsack.cli as cli
    from swapsack.swap import SwapAborted

    class RecordingClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class UnservingBackend:
        name = "cow"
        client = RecordingClient()
        executor = "signed-order"

        def serves(self, from_asset, to_asset):
            return False

    backend = UnservingBackend()
    monkeypatch.setattr(cli, "_backends_for", lambda args: [backend])

    with pytest.raises(SwapAborted):
        cli._select_backend(
            SimpleNamespace(backend="cow"),
            from_asset="BTC.BTC",
            to_asset="ETH.ETH",
            amount=1,
            destination="bc1qdest",
            tolerance_bps=300,
        )
    assert backend.client.closed is True


# --- backend sessions are closed after selection (finding #12) ---


def test_select_backend_closes_unused_clients(monkeypatch):
    from types import SimpleNamespace

    import swapsack.backends as backends_mod
    import swapsack.cli as cli
    from swapsack.backends import Backend

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


def test_swap_routes_zec_to_the_utxo_handler(monkeypatch):
    import swapsack.cli as cli

    routed = {}

    def fake_swap_from_utxo(args, adapter_factory):
        routed.update(factory=adapter_factory)
        return 0

    monkeypatch.setattr(cli, "_swap_from_utxo", fake_swap_from_utxo)
    args = build_parser().parse_args(
        ["swap", "--from", "ZEC", "--to", "BTC", "--amount", "0.5"]
    )
    assert cli.cmd_swap(args) == 0
    assert routed["factory"] is cli._zec_adapter


def test_add_liquidity_zec_requires_maya_backend(monkeypatch, capsys):
    # Same Maya-only guard as DASH, via the shared _lp_backend_refused helper.
    import swapsack.cli as cli

    called = []
    monkeypatch.setattr(cli, "_liquidity_utxo", lambda *a, **kw: called.append(1) or 0)
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "ZEC", "--amount", "1"]
    )  # --backend defaults to thorchain
    assert cli.cmd_add_liquidity(args) == 2
    assert not called
    assert "maya" in capsys.readouterr().err.lower()

    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "ZEC", "--amount", "1", "--backend", "maya"]
    )
    assert cli.cmd_add_liquidity(args) == 0
    assert called


def test_backend_cow_parses_for_swap_and_quote():
    args = build_parser().parse_args(
        [
            "swap",
            "--from",
            "USDT-ETH",
            "--to",
            "USDC-ETH",
            "--amount",
            "1",
            "--backend",
            "cow",
        ]
    )
    assert args.backend == "cow"
    args = build_parser().parse_args(
        [
            "quote",
            "--from",
            "USDT-ETH",
            "--to",
            "ETH",
            "--amount",
            "1",
            "--backend",
            "cow",
        ]
    )
    assert args.backend == "cow"


def test_swap_via_cow_gate_catches_inflated_quote_amount():
    """The gate must bind sell_amount/approval to the amount WE requested, not
    to the API's own sellAmount+feeAmount total — otherwise a quote that
    inflates its own total sails through the gate meant to catch exactly
    that."""
    import swapsack.cli as cli
    from swapsack.cow import CowBackend

    USDT_ASSET = "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7"
    USDC_ASSET = "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
    RECEIVER = "0x40A50cf069e992AA4536211B23F286eF88752187"

    # Requested: 100 USDT (1e8 units) -> 100_000_000 native (6 decimals). The
    # quote inflates sellAmount+feeAmount to 150_000_000 -- 50% more than asked.
    valid_to = int(time.time()) + 3600  # within COW_MAX_ORDER_VALIDITY (7200s)
    malicious_payload = {
        "quote": {
            "sellToken": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            "buyToken": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            "receiver": RECEIVER.lower(),
            "sellAmount": "100000000",
            "buyAmount": "99000000",
            "validTo": valid_to,
            "appData": "0x" + "00" * 32,
            "feeAmount": "50000000",
            "kind": "sell",
            "partiallyFillable": False,
            "sellTokenBalance": "erc20",
            "buyTokenBalance": "erc20",
        },
        "from": RECEIVER.lower(),
        "expiration": "2033-05-18T00:00:00.000000000Z",
        "id": 1,
        "verified": True,
    }

    class FakeCowClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def quote(self, *a, **kw):
            return malicious_payload

    class FakeAdapter:
        def __init__(self):
            self.approval_calls = []

        def fetch_token_allowance(self, token, owner, spender):
            return 0

        def build_and_verify_approvals(self, **kw):
            from types import SimpleNamespace

            self.approval_calls.append(kw)
            return SimpleNamespace(problems=[], built=SimpleNamespace(txs=[]))

    backend = CowBackend(FakeCowClient())
    adapter = FakeAdapter()
    args = build_parser().parse_args(
        [
            "swap",
            "--from",
            "USDT-ETH",
            "--to",
            "USDC-ETH",
            "--amount",
            "100",
            "--backend",
            "cow",
        ]
    )

    rc = cli._swap_via_cow(
        args,
        adapter,
        backend,
        from_asset=USDT_ASSET,
        to_asset=USDC_ASSET,
        amount=10_000_000_000,  # 100 USDT in 1e8 units
        dest=RECEIVER,
        mnemonic="mnemonic",
        from_address=RECEIVER,
        nonce=0,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
    )

    assert rc == 1
    assert adapter.approval_calls[0]["amount"] == 100_000_000  # requested, not inflated


class _FakeFees:
    def breakdown(self, _to_key):
        return []


class _FakeCowQuote:
    sell_amount_total = 100_000_000
    valid_to = 9_999_999_999
    expected_amount_out = 100_000_000
    quote_id = 7
    fees = _FakeFees()
    streaming_swap_blocks = 0


class _FakeApprovalsBuilt:
    txs = [{"approve": 1}]


class _FakeApprovals:
    built = _FakeApprovalsBuilt()
    problems = []


class _FakeCowClient:
    def __init__(self):
        self.submitted = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def quote(self, *a, **k):
        return {"raw": True}

    def submit_order(self, order, *, signature, from_address, quote_id):
        self.submitted = order
        return "0xUID"


class _FakeCowBackend:
    name = "cow"

    def __init__(self, client):
        self.client = client

    def serves(self, _from, _to):
        return True


class _FakeEthAdapter:
    def __init__(self, receipt):
        self._receipt = receipt
        self.calls = []

    def fetch_token_allowance(self, *a, **k):
        return 0  # short -> an approval tx is built

    def build_and_verify_approvals(self, **k):
        return _FakeApprovals()

    def sign(self, _built):
        self.calls.append("sign")
        return ["0xraw"]

    def broadcast(self, _raws):
        self.calls.append("broadcast")
        return "0xapprovalhash"

    def wait_for_receipt(self, txid, **k):
        self.calls.append(("wait", txid))
        return self._receipt

    def sign_cow_order(self, order, mnemonic):
        self.calls.append("sign_order")
        return "0xsig"


def _patch_cow_helpers(monkeypatch):
    import swapsack.cow as cow
    import swapsack.verify as verify

    monkeypatch.setattr(cow, "parse_cow_quote", lambda *a, **k: _FakeCowQuote())
    monkeypatch.setattr(
        cow,
        "build_order",
        lambda *a, **k: {"buyAmount": "100", "validTo": 9_999_999_999},
    )
    monkeypatch.setattr(verify, "verify_cow_order", lambda **k: [])


def _run_cow_confirm(monkeypatch, receipt):
    import argparse

    from swapsack.cli import _swap_via_cow

    _patch_cow_helpers(monkeypatch)
    client = _FakeCowClient()
    adapter = _FakeEthAdapter(receipt)
    args = argparse.Namespace(
        from_="USDT-ETH", to_="USDC-ETH", confirm=True, yes=True, price_check=False
    )
    usdt = "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7"
    usdc = "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
    rc = _swap_via_cow(
        args,
        adapter,
        _FakeCowBackend(client),
        from_asset=usdt,
        to_asset=usdc,
        amount=10_000_000_000,  # 100 USDT in THORChain 1e8 units
        dest="0x" + "ab" * 20,
        mnemonic="m",
        from_address="0x" + "cd" * 20,
        nonce=0,
        max_fee_per_gas=1,
        max_priority_fee_per_gas=1,
    )
    return rc, adapter, client


def test_cow_waits_for_approval_receipt_before_submitting(monkeypatch):
    # Finding 4: the order must not be submitted until the ERC-20 approval is
    # mined (the orderbook validates the allowance at placement).
    rc, adapter, client = _run_cow_confirm(monkeypatch, receipt={"status": "0x1"})
    assert rc == 0
    assert client.submitted is not None
    # broadcast the approval, THEN wait, THEN sign+submit the order.
    assert [c for c in adapter.calls if c in ("broadcast", "sign_order")] == [
        "broadcast",
        "sign_order",
    ]
    assert any(isinstance(c, tuple) and c[0] == "wait" for c in adapter.calls)
    wait_i = next(i for i, c in enumerate(adapter.calls) if c[0] == "wait")
    assert adapter.calls.index("broadcast") < wait_i < adapter.calls.index("sign_order")


def test_cow_aborts_without_submitting_when_approval_never_mines(monkeypatch):
    # wait_for_receipt returns None on timeout -> abort, do NOT submit the order
    # against a still-zero allowance.
    rc, adapter, client = _run_cow_confirm(monkeypatch, receipt=None)
    assert rc == 1
    assert client.submitted is None
    assert "sign_order" not in adapter.calls


def test_cow_aborts_when_approval_reverts(monkeypatch):
    # A reverted approval (status 0x0) leaves the allowance unset -> abort.
    rc, adapter, client = _run_cow_confirm(monkeypatch, receipt={"status": "0x0"})
    assert rc == 1
    assert client.submitted is None
    assert "sign_order" not in adapter.calls


def test_status_accepts_cow_order_uid():
    # A CoW order uid is 56 bytes (digest + owner + validTo) = 114 chars with
    # the 0x prefix; a chain txid is 32 bytes. status auto-detects.
    from swapsack.cli import _is_cow_order_uid

    uid = "0x" + "ab" * 56
    assert _is_cow_order_uid(uid)
    assert not _is_cow_order_uid("ab" * 32)  # plain txid
    assert not _is_cow_order_uid("0x" + "ab" * 32)  # EVM txid
    assert not _is_cow_order_uid("0x" + "zz" * 56)  # not hex


# --- BIP21 payment URIs reach the handlers as bare addresses -----------------


def test_send_accepts_a_payment_uri_and_strips_it(monkeypatch):
    """`send bitcoin:<addr>` must work AND pay the bare address.

    Validating the URI but handing the raw `bitcoin:…` string to the builder
    would be worse than rejecting it: bitcoinlib would refuse (or, on another
    chain, encode a nonsense output).
    """
    import swapsack.cli as cli

    seen = {}

    def fake_send_utxo(args, factory):
        seen["address"] = args.address
        return 0

    monkeypatch.setattr(cli, "_send_utxo", fake_send_utxo)
    args = build_parser().parse_args(
        [
            "send",
            "bitcoin:1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa?label=Alice",
            "--asset",
            "BTC",
            "--amount",
            "0.01527",
        ]
    )
    assert cli.cmd_send(args) == 0
    assert seen["address"] == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def test_send_refuses_a_uri_amount_that_contradicts_the_flag(monkeypatch, capsys):
    """A URI asking for 0.5 BTC while --amount says 0.01 is not a spend to guess at."""
    import swapsack.cli as cli

    called = []
    monkeypatch.setattr(cli, "_send_utxo", lambda *a, **kw: called.append(1) or 0)
    args = build_parser().parse_args(
        [
            "send",
            "bitcoin:1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa?amount=0.5",
            "--asset",
            "BTC",
            "--amount",
            "0.01527",
        ]
    )
    assert cli.cmd_send(args) == 2
    assert not called
    err = capsys.readouterr().err
    assert "0.5" in err and "0.01527" in err


def test_send_accepts_a_uri_amount_that_agrees(monkeypatch):
    import swapsack.cli as cli

    monkeypatch.setattr(cli, "_send_utxo", lambda *a, **kw: 0)
    args = build_parser().parse_args(
        [
            "send",
            "bitcoin:1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa?amount=0.01527",
            "--asset",
            "BTC",
            "--amount",
            "0.01527",
        ]
    )
    assert cli.cmd_send(args) == 0


def test_swap_dest_accepts_a_payment_uri():
    import swapsack.cli as cli

    args = build_parser().parse_args(
        [
            "swap",
            "--from",
            "BTC",
            "--to",
            "ETH",
            "--amount",
            "0.1",
            "--dest",
            "ethereum:0x9858EfFD232B4033E47d90003D41EC34EcaEda94@1",
        ]
    )
    assert (
        cli._resolve_destination(args, None)
        == "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    )


def test_empty_passphrase_env_is_honoured_not_prompted(monkeypatch):
    """An intentionally passphrase-less keystore must work non-interactively.

    A dedicated test/automation wallet is created with an empty passphrase; if
    the empty string is treated as 'unset' the CLI drops to getpass and dies
    with no TTY. Distinguish unset (prompt) from deliberately empty (use it).
    """
    import swapsack.cli as cli

    def no_prompt(prompt=""):
        raise AssertionError("should not prompt when the env var is set")

    monkeypatch.setattr(cli.getpass, "getpass", no_prompt)
    monkeypatch.setenv("SWAPSACK_PASSPHRASE", "")
    assert cli._passphrase() == ""

    monkeypatch.setenv("SWAPSACK_PASSPHRASE", "hunter2")
    assert cli._passphrase() == "hunter2"


def test_unset_passphrase_env_still_prompts(monkeypatch):
    import swapsack.cli as cli

    monkeypatch.delenv("SWAPSACK_PASSPHRASE", raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "typed")
    assert cli._passphrase() == "typed"


# --- fees shown in approximate EUR -------------------------------------------


class _FakeFeed:
    """Stand-in for PriceFeed returning a fixed EUR spot."""

    prices = {"bitcoin": {"eur": 50_000.0}, "ethereum": {"eur": 2_000.0}}

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def spot(self, coin_ids, *, vs=("usd",)):
        return {c: self.prices[c] for c in coin_ids if c in self.prices}


@pytest.fixture(autouse=True)
def fake_feed(monkeypatch):
    """Give every CLI test a fixed EUR spot instead of a live CoinGecko lookup.

    Autouse because ``--price-check`` defaults *on*: any command that prints a
    fee prices it, so a test that merely exercises `send`/`status` reaches the
    feed without ever mentioning it. Five did exactly that (found when the
    offline guard in ``conftest.py`` started refusing connections), and the
    ``@functools.cache`` on ``_eur_price`` made it worse than a plain leak: the
    first test to warm the cache silently spared the rest, so *which* test went
    out depended on ordering. Clearing that cache around every test is the
    other half of the fix. Tests that want a different feed (a tripwire, a
    raiser) monkeypatch over this in their own body.
    """
    import swapsack.cli as cli
    import swapsack.pricefeed as pricefeed

    cli._eur_price.cache_clear()
    monkeypatch.setattr(pricefeed, "PriceFeed", _FakeFeed)
    yield
    cli._eur_price.cache_clear()


def test_eur_suffix_converts_a_fee(fake_feed):
    import swapsack.cli as cli

    # 20 000 sats of BTC at €50 000 -> €10.00
    assert cli._eur_suffix(20_000 / 1e8, "BTC", price_check=True) == " (~€10.00)"
    assert cli._eur_suffix(0.5, "ETH", price_check=True) == " (~€1000.00)"
    # Sub-cent fees still say something useful rather than '~€0.00'.
    assert cli._eur_suffix(1 / 1e8, "BTC", price_check=True) == " (<€0.01)"


def test_eur_suffix_makes_no_lookup_when_price_check_is_off(monkeypatch):
    """--no-price-check must mean *no* third-party request, not a discarded one.

    The fee estimate is a courtesy line, but the request that produces it tells
    CoinGecko that this IP is about to spend this asset. Opting out has to stop
    the call itself.
    """
    import swapsack.cli as cli
    import swapsack.pricefeed as pricefeed

    cli._eur_price.cache_clear()

    class Tripwire(_FakeFeed):
        def spot(self, coin_ids, *, vs=("usd",)):
            raise AssertionError("price feed consulted despite --no-price-check")

    monkeypatch.setattr(pricefeed, "PriceFeed", Tripwire)
    assert cli._eur_suffix(20_000 / 1e8, "BTC", price_check=False) == ""
    cli._eur_price.cache_clear()


def test_eur_suffix_is_best_effort(monkeypatch):
    """A price-feed failure must never break or noisily disturb a spend."""
    import swapsack.cli as cli
    import swapsack.pricefeed as pricefeed

    cli._eur_price.cache_clear()

    class Boom(_FakeFeed):
        def spot(self, coin_ids, *, vs=("usd",)):
            raise OSError("feed down")

    monkeypatch.setattr(pricefeed, "PriceFeed", Boom)
    assert cli._eur_suffix(0.001, "BTC", price_check=True) == ""
    cli._eur_price.cache_clear()
    # An asset with no CoinGecko mapping (e.g. a synth) simply gets no estimate.
    assert cli._eur_suffix(1.0, "NOSUCH", price_check=True) == ""


def test_btc_send_fee_line_shows_eur(monkeypatch, capsys, fake_feed):
    import swapsack.cli as cli
    from swapsack.chains.coins import Utxo

    class FakeBtc:
        chain = "BTC"
        asset = "BTC.BTC"
        account = "m/84'/0'/0'"
        change_path = "m/84'/0'/0'/1/0"
        default_derivation = "m/84'/0'/0'/0/0"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def derive_address(self, mnemonic, path=None):
            return "bc1qchange"

        def address_info(self, address):
            return None

        def fetch_utxos(self, address, *, include_unconfirmed=False):
            return [Utxo(txid="aa" * 32, vout=0, value=1_000_000, address=address)]

        def fetch_fee_rate(self, target_blocks=2):
            return 2.0

        def build_and_verify_send(self, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                problems=[],
                built=SimpleNamespace(fee=20_000),
                plan=None,
            )

    def fake_scan(*, derive_address, probe, account):
        from types import SimpleNamespace

        return [("m/84'/0'/0'/0/0", "bc1qowned", SimpleNamespace(confirmed=1_000_000))]

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(cli, "_btc_adapter", lambda args, passphrase="": FakeBtc())
    monkeypatch.setattr("swapsack.chains.scan.scan_account", fake_scan)
    monkeypatch.setattr(cli, "_confirm_and_execute", lambda *a, **kw: 0)

    args = build_parser().parse_args(
        ["send", "--asset", "BTC", "--amount", "0.001", "bc1qrecipient"]
    )
    assert cli._send_utxo(args, cli._btc_adapter) == 0
    out = capsys.readouterr().out
    assert "btc fee: 20000" in out
    assert "~€10.00" in out  # 20 000 sats at €50 000/BTC

    # ...and --no-price-check makes the same send without touching the feed.
    import swapsack.pricefeed as pricefeed

    cli._eur_price.cache_clear()

    class Tripwire(_FakeFeed):
        def spot(self, coin_ids, *, vs=("usd",)):
            raise AssertionError("price feed consulted despite --no-price-check")

    monkeypatch.setattr(pricefeed, "PriceFeed", Tripwire)
    quiet = build_parser().parse_args(
        ["send", "--asset", "BTC", "--amount", "0.001", "--no-price-check", "bc1qrec"]
    )
    assert cli._send_utxo(quiet, cli._btc_adapter) == 0
    out = capsys.readouterr().out
    assert "btc fee: 20000" in out
    assert "€" not in out
    cli._eur_price.cache_clear()


# --- status explains an unobserved txid instead of dumping an empty body ------


class _StubBackend:
    def __init__(self, name, body):
        self.name = name
        self._body = body
        self.client = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def tx_status(self, txid):
        return self._body


NOT_OBSERVED = {"stages": {"inbound_observed": {"started": False, "completed": False}}}


@pytest.fixture
def no_onchain_btc(monkeypatch):
    """Make `status`'s on-chain lookup a miss instead of a live Esplora query.

    `cmd_status` prints two things: the swap stages and (best-effort) what the
    transaction did on-chain. A test that only cares about the stages still
    triggers the second, which went out to Esplora until the offline guard in
    `conftest.py` caught it. `None` is the honest stub — it is what a real
    lookup returns for the synthetic hashes these tests use.
    """
    import swapsack.cli as cli

    class NoTx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fetch_tx(self, txid):
            return None

    monkeypatch.setattr(cli, "_btc_adapter", lambda args, passphrase="": NoTx())


@pytest.fixture
def no_chainflip(monkeypatch):
    """Make `status`'s Chainflip lookup a miss instead of a live API query.

    Same reasoning as `no_onchain_btc`: `cmd_status` now also asks Chainflip
    whether the txid was one of its vault-swap deposits, so a test that only
    cares about the thornode stages would otherwise go out to the network (and
    be caught by the offline guard in `conftest.py`). `None` is the honest stub
    — it is what the real lookup returns for the synthetic hashes these tests
    use.
    """
    import swapsack.cli as cli

    class NoSwap:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def swap_status(self, txid):
            return None

    monkeypatch.setattr(cli, "_chainflip_client", lambda args: NoSwap())


def test_status_explains_an_unobserved_txid(
    monkeypatch, capsys, no_onchain_btc, no_chainflip
):
    """A hash no vault has seen must say what that means, not print a stub body.

    A plain `send` is never observed by THORChain/Maya — only swap inbounds
    are — so the bare `"started": false` JSON reads as a broken command rather
    than the correct answer it is.
    """
    import swapsack.cli as cli

    monkeypatch.setattr(
        "swapsack.backends.default_backends",
        lambda: [_StubBackend("thorchain", NOT_OBSERVED)],
    )
    args = build_parser().parse_args(["status", "ab" * 32])
    assert cli.cmd_status(args) == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "not observed" in combined.lower()
    # Name the most likely cause: it simply isn't a swap.
    assert "send" in combined.lower()
    # The machine-readable body is still there for scripts.
    assert '"stages"' in captured.out


def test_status_reports_the_backend_that_observed_it(
    monkeypatch, capsys, no_onchain_btc, no_chainflip
):
    import swapsack.cli as cli

    observed = {
        "stages": {
            "inbound_observed": {"started": True, "completed": True},
            "swap_finalised": {"completed": True},
            "outbound_signed": {"completed": False},
        }
    }
    monkeypatch.setattr(
        "swapsack.backends.default_backends",
        lambda: [
            _StubBackend("thorchain", NOT_OBSERVED),
            _StubBackend("maya", observed),
        ],
    )
    args = build_parser().parse_args(["status", "ab" * 32])
    assert cli.cmd_status(args) == 0
    captured = capsys.readouterr()
    assert "maya" in (captured.out + captured.err)
    assert "not observed" not in (captured.out + captured.err).lower()


def test_status_prints_the_on_chain_summary(
    monkeypatch, capsys, fake_feed, esplora_tx_partial_send, no_chainflip
):
    """`status <txid>` should say what the transaction actually did.

    The swap stages alone are useless for a plain send: the interesting facts
    are where the money went, how much came back as change, and what it cost.
    """
    import swapsack.cli as cli
    from swapsack.chains.btc import parse_tx_summary

    class FakeBtc:
        chain = "BTC"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def fetch_tx(self, txid):
            return parse_tx_summary(esplora_tx_partial_send)

    monkeypatch.setattr(
        "swapsack.backends.default_backends",
        lambda: [_StubBackend("thorchain", NOT_OBSERVED)],
    )
    monkeypatch.setattr(cli, "_btc_adapter", lambda args, passphrase="": FakeBtc())
    args = build_parser().parse_args(["status", "cc" * 32])
    assert cli.cmd_status(args) == 0
    out = capsys.readouterr().out

    assert "confirmed" in out and "959260" in out
    assert "1Recipient" in out and "1527000" in out  # where the money went
    assert "bc1qchange" in out and "482840" in out  # what came back
    assert "160" in out  # the fee, in sats
    assert "~€" in out or "<€0.01" in out  # ...and in EUR
    # A plain send has no memo, so say so rather than leaving the user guessing
    # why the swap stages are empty.
    assert "no OP_RETURN" in out or "not a swap" in out.lower()


# --- configurable UTXO fee target (--fee-blocks / env / config.toml) ---------


@pytest.fixture
def _clear_config_cache():
    import swapsack.cli as cli

    cli._config.cache_clear()
    yield
    cli._config.cache_clear()


def _ns(**kw):
    from types import SimpleNamespace

    kw.setdefault("fee_blocks", None)
    return SimpleNamespace(**kw)


def test_fee_blocks_precedence_flag_over_env_over_config(
    monkeypatch, tmp_path, _clear_config_cache
):
    import swapsack.cli as cli

    cfg = tmp_path / "config.toml"
    cfg.write_text("[fees]\ntarget_blocks = 7\n")
    monkeypatch.setenv("SWAPSACK_CONFIG", str(cfg))

    # config only
    monkeypatch.delenv("SWAPSACK_FEE_BLOCKS", raising=False)
    assert cli._fee_blocks(_ns()) == 7
    # env beats config
    cli._config.cache_clear()
    monkeypatch.setenv("SWAPSACK_FEE_BLOCKS", "5")
    assert cli._fee_blocks(_ns()) == 5
    # flag beats env + config
    assert cli._fee_blocks(_ns(fee_blocks=3)) == 3


def test_fee_blocks_defaults_when_nothing_set(monkeypatch, _clear_config_cache):
    import swapsack.cli as cli

    monkeypatch.setenv("SWAPSACK_CONFIG", "/nonexistent/swapsack.toml")
    monkeypatch.delenv("SWAPSACK_FEE_BLOCKS", raising=False)
    assert cli._fee_blocks(_ns()) == cli.DEFAULT_FEE_BLOCKS


def test_fee_blocks_ignores_junk_and_nonpositive(
    monkeypatch, tmp_path, _clear_config_cache
):
    import swapsack.cli as cli

    cfg = tmp_path / "config.toml"
    cfg.write_text('[fees]\ntarget_blocks = "nope"\n')  # unparseable -> fall through
    monkeypatch.setenv("SWAPSACK_CONFIG", str(cfg))
    monkeypatch.setenv("SWAPSACK_FEE_BLOCKS", "0")  # non-positive -> fall through
    assert cli._fee_blocks(_ns()) == cli.DEFAULT_FEE_BLOCKS


def test_config_returns_empty_on_missing_or_malformed(
    monkeypatch, tmp_path, _clear_config_cache
):
    import swapsack.cli as cli

    monkeypatch.setenv("SWAPSACK_CONFIG", str(tmp_path / "absent.toml"))
    assert cli._config() == {}
    cli._config.cache_clear()
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not valid = toml")
    monkeypatch.setenv("SWAPSACK_CONFIG", str(bad))
    assert cli._config() == {}


def test_malformed_config_warns_instead_of_silently_reverting(
    monkeypatch, tmp_path, capsys, _clear_config_cache
):
    """Failing soft is right; failing *silently* costs money.

    A typo anywhere in the file discards the whole thing, so a user who set
    `target_blocks = 4` is quietly moved back to the faster, pricier default.
    Say so once on stderr — the spend still proceeds.
    """
    import swapsack.cli as cli

    bad = tmp_path / "bad.toml"
    bad.write_text("this is = not valid = toml")
    monkeypatch.setenv("SWAPSACK_CONFIG", str(bad))
    monkeypatch.delenv("SWAPSACK_FEE_BLOCKS", raising=False)

    assert cli._config() == {}
    err = capsys.readouterr().err
    assert str(bad) in err
    assert "ignor" in err.lower()
    # ...and the spend is not blocked by it.
    assert cli._fee_blocks(_ns()) == cli.DEFAULT_FEE_BLOCKS


def test_config_tolerates_a_scalar_where_a_table_belongs(
    monkeypatch, tmp_path, _clear_config_cache
):
    """`fees = "fast"` must fall through to the default, not AttributeError."""
    import swapsack.cli as cli

    cfg = tmp_path / "config.toml"
    cfg.write_text('fees = "fast"\n')
    monkeypatch.setenv("SWAPSACK_CONFIG", str(cfg))
    monkeypatch.delenv("SWAPSACK_FEE_BLOCKS", raising=False)
    assert cli._fee_blocks(_ns()) == cli.DEFAULT_FEE_BLOCKS


def test_valid_config_is_silent(monkeypatch, tmp_path, capsys, _clear_config_cache):
    """No warning on the happy path — a wallet must not cry wolf before a spend."""
    import swapsack.cli as cli

    cfg = tmp_path / "config.toml"
    cfg.write_text("[fees]\ntarget_blocks = 4\n")
    monkeypatch.setenv("SWAPSACK_CONFIG", str(cfg))
    monkeypatch.delenv("SWAPSACK_FEE_BLOCKS", raising=False)
    assert cli._fee_blocks(_ns()) == 4
    captured = capsys.readouterr()
    assert captured.err == ""
    # An absent file is the default state, not a misconfiguration: also silent.
    cli._config.cache_clear()
    monkeypatch.setenv("SWAPSACK_CONFIG", str(tmp_path / "absent.toml"))
    assert cli._config() == {}
    assert capsys.readouterr().err == ""


def test_default_fee_blocks_is_a_fast_target():
    # The default must be a nearer target than the old surprising 6-block one.
    import swapsack.cli as cli

    assert 1 <= cli.DEFAULT_FEE_BLOCKS <= 3


# --- symmetric (two-sided) add-liquidity ------------------------------------
#
# The orchestration itself is covered in test_swap.py; these pin the CLI's own
# decisions — which assets it will attempt at all, and that a half-completed add
# is reported with the live txid rather than as a plain failure.


class _FakeSymEth:
    chain = "ETH"
    native_symbol = "ETH"  # mirrors the real adapter: the fee line reads this

    def __init__(self):
        self.broadcast_error = None
        self.broadcasted = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def derive_address(self, mnemonic, path=None):
        return "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"

    def get_nonce(self, address):
        return 0

    def fetch_fees(self):
        return (20_000_000_000, 1_000_000_000)

    def token_decimals(self, token):
        return 6

    def build_and_verify_deposit(self, *, vault, memo, amount, now, **kwargs):
        """Mirrors EthAdapter's two shapes — they differ, and the difference bit."""
        from swapsack.swap import Prepared

        if kwargs.get("token"):
            # EthTokenDeposit: the router call is its own plan, and carries the
            # token amount / router / vault.
            built = SimpleNamespace(
                fee=10**15,
                native_amount=100_000_000,
                router=kwargs["router"],
                vault=vault,
            )
            return Prepared(quote=None, built=built, plan=built, problems=[])
        # EthBuiltSwap + EthSwapPlan: no native_amount, no router, no vault —
        # the amount lives on the plan as wei.
        plan = SimpleNamespace(
            inbound_address=vault, memo=memo, amount_wei=amount * 10**10
        )
        return Prepared(
            quote=None, built=SimpleNamespace(fee=10**15), plan=plan, problems=[]
        )

    def sign(self, built):
        return ["beef"]

    def broadcast(self, raws):
        if self.broadcast_error is not None:
            raise self.broadcast_error
        self.broadcasted = True
        return "eth_txid"


class _FakeSymMaya:
    chain = "MAYA"
    asset = "MAYA.CACAO"
    symbol = "CACAO"
    decimals = 10

    def __init__(self, balance=10**20):
        self._balance = balance
        self.broadcasted = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def derive_address(self, mnemonic, path=None):
        return "maya1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"

    def fetch_balance(self, address):
        return self._balance

    def build_and_verify_native_deposit(self, *, memo, amount, mnemonic, now, **kw):
        from swapsack.swap import Prepared

        plan = SimpleNamespace(memo=memo, amount=amount)
        return Prepared(
            quote=None, built=SimpleNamespace(fee=0), plan=plan, problems=[]
        )

    def sign(self, built):
        return ["cafe"]

    def broadcast(self, raws):
        self.broadcasted = True
        return "maya_txid"


class _FakeSymThor:
    """The LP backend client (Maya), as a context manager."""

    ROUTER = "0xe3985E6b61b814F7Cdb188766562ba71b446B46d"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def inbound_addresses(self):
        from swapsack.thorchain import ChainStatus

        return {
            "ETH": ChainStatus(
                chain="ETH",
                gas_rate=15,
                gas_rate_units="gwei",
                outbound_fee=0,
                dust_threshold=0,
                halted=False,
                global_trading_paused=False,
                chain_trading_paused=False,
                address="0xvault",
                router=self.ROUTER,
            )
        }

    def mimir(self):
        return {}

    def pool(self, asset):
        from swapsack.thorchain import PoolDepth

        # Maya's live ETH.USDC ratio: ~231.7k USDC (1e8) : ~2.05M CACAO (1e10).
        return PoolDepth(
            asset=asset,
            balance_asset=23_167_994_792_257,
            balance_protocol=20_511_113_357_838_187,
        )


def _wire_symmetric(monkeypatch, eth=None, maya=None):
    import swapsack.cli as cli

    eth = eth or _FakeSymEth()
    maya = maya or _FakeSymMaya()
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    # The EVM asset adapter is reached through the _EVM_ADAPTERS registry, so
    # patch the entry rather than the module-level factory the dict captured.
    monkeypatch.setitem(cli._EVM_ADAPTERS, "ETH", lambda args, passphrase="": eth)
    monkeypatch.setattr(cli, "_maya_adapter", lambda args, passphrase="": maya)
    monkeypatch.setattr(cli, "_liquidity_client", lambda args: _FakeSymThor())
    return eth, maya


def _symmetric_args(*extra):
    return build_parser().parse_args(
        [
            "add-liquidity",
            "--asset",
            "USDC-ETH",
            "--amount",
            "100",
            "--symmetric",
            "--backend",
            "maya",
            *extra,
        ]
    )


def test_add_liquidity_symmetric_flag_parses():
    args = _symmetric_args()
    assert args.symmetric is True
    # The default must stay single-sided: symmetric is opt-in.
    plain = build_parser().parse_args(
        ["add-liquidity", "--asset", "BTC", "--amount", "1"]
    )
    assert plain.symmetric is False


def test_add_liquidity_symmetric_routes_to_the_two_leg_path(monkeypatch):
    import swapsack.cli as cli

    called = {}

    def fake_symmetric(args, pool):
        called["pool"] = pool
        return 0

    monkeypatch.setattr(cli, "_liquidity_symmetric", fake_symmetric)
    assert cli.cmd_add_liquidity(_symmetric_args()) == 0
    assert called["pool"] == ASSET["USDC-ETH"]


def test_add_liquidity_symmetric_refuses_a_utxo_source(capsys, monkeypatch):
    """A UTXO tx has no single sender, so the pairing address would be a guess
    (the protocol observes vin[0] by convention — an assumption no testnet can
    verify for us). Refuse rather than risk legs that never pair."""
    import swapsack.cli as cli

    _wire_symmetric(monkeypatch)
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "BTC", "--amount", "1", "--symmetric"]
    )
    assert cli.cmd_add_liquidity(args) == 2
    err = capsys.readouterr().err
    assert "account-model" in err or "single sender" in err


def test_add_liquidity_symmetric_refuses_amount_max(capsys, monkeypatch):
    import swapsack.cli as cli

    _wire_symmetric(monkeypatch)
    args = build_parser().parse_args(
        [
            "add-liquidity",
            "--asset",
            "USDC-ETH",
            "--amount",
            "max",
            "--symmetric",
            "--backend",
            "maya",
        ]
    )
    assert cli.cmd_add_liquidity(args) == 2
    assert "max" in capsys.readouterr().err


def test_add_liquidity_symmetric_dry_run_prints_both_legs(monkeypatch, capsys):
    import swapsack.cli as cli

    eth, maya = _wire_symmetric(monkeypatch)
    assert cli.cmd_add_liquidity(_symmetric_args()) == 0
    out = capsys.readouterr().out
    # Each leg's memo names the other side's address — the pairing, made visible
    # before the user confirms two irreversible txs.
    assert "+:ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48:maya1" in out
    assert "+:ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48:0x9858" in out
    assert "DRY RUN" in out
    assert not eth.broadcasted and not maya.broadcasted


def test_add_liquidity_symmetric_reports_a_half_add_with_the_live_txid(
    monkeypatch, capsys
):
    """The failure the two-leg design exists for: CACAO is irreversibly out and
    the USDC leg bounced. The user must be told what is live, loudly."""
    import swapsack.cli as cli
    from swapsack.swap import BroadcastError

    eth = _FakeSymEth()
    eth.broadcast_error = BroadcastError("rpc rejected")
    eth, maya = _wire_symmetric(monkeypatch, eth=eth)
    rc = cli.cmd_add_liquidity(_symmetric_args("--confirm", "--yes"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "maya_txid" in err
    assert "PARTIAL" in err.upper()
    assert maya.broadcasted


def test_add_liquidity_symmetric_native_eth_prints_the_eth_leg(monkeypatch, capsys):
    """A native-ETH symmetric add builds a plain vault deposit, not a router
    call, so its built tx has no native_amount/router/vault — printing it the
    token way raised AttributeError instead of showing the leg."""
    import swapsack.cli as cli

    eth, maya = _wire_symmetric(monkeypatch)
    args = build_parser().parse_args(
        [
            "add-liquidity",
            "--asset",
            "ETH",
            "--amount",
            "0.5",
            "--symmetric",
            "--backend",
            "maya",
        ]
    )
    assert cli.cmd_add_liquidity(args) == 0
    out = capsys.readouterr().out
    assert "0.50000000 ETH" in out
    assert "+:ETH.ETH:maya1" in out
    assert "+:ETH.ETH:0x9858" in out
    assert not eth.broadcasted and not maya.broadcasted


# --- Arbitrum wiring --------------------------------------------------------
#
# ARB is the second *spendable* EVM chain, which is what turns the `chain ==
# "ETH"` branches in cmd_send/cmd_swap/_liquidity into a table. These pin that
# each of those paths actually reaches an ARB adapter rather than silently
# building an Ethereum transaction.


def test_symmetric_fee_line_names_the_chains_own_native_coin(monkeypatch, capsys):
    """The one fee line the native_symbol sweep missed.

    Correct today because ETH and ARB both pay ether, so this pins the rule
    rather than a present-day bug: a third EVM chain would be misreported.
    """
    import swapsack.cli as cli

    class FakeBuilt:
        fee = 10**16  # 0.01 native

    class FakeAssetLeg:
        built = FakeBuilt()

        class plan:
            amount_wei = 10**18
            inbound_address = "0xvault"

    class FakePrepared:
        asset = FakeAssetLeg()
        asset_memo = "+:BNB.USDC:maya1x"
        protocol_memo = "+:BNB.USDC:0xabc"
        protocol_amount = 10**10

    class FakeProtocol:
        decimals = 10
        symbol = "CACAO"
        chain = "MAYA"

    class FakeAdapter:
        native_symbol = "BNB"

    args = build_parser().parse_args(
        ["add-liquidity", "--symmetric", "--asset", "USDC-ETH", "--amount", "1"]
    )
    cli._print_symmetric_legs(
        args,
        FakePrepared(),
        FakeProtocol(),
        decimals=6,
        token_add=False,
        adapter=FakeAdapter(),
    )
    out = capsys.readouterr().out
    assert "max fee:" in out
    assert "BNB" in out.split("max fee:")[1]
    assert "ETH" not in out.split("max fee:")[1]


def test_evm_adapters_table_covers_every_spendable_evm_chain():
    import swapsack.cli as cli

    # BSC is deliberately absent: its adapter is address+balance only, so it has
    # no send/swap/LP path to dispatch to.
    assert set(cli._EVM_ADAPTERS) == {"ETH", "ARB", "AVAX"}


def test_arb_adapter_factory_honours_env_and_flag(monkeypatch):
    import swapsack.cli as cli

    monkeypatch.setenv("SWAPSACK_ARB_RPC", "https://from-env.example")
    args = build_parser().parse_args(["balance"])
    with cli._arb_adapter(args) as adapter:
        assert adapter.rpc_url == "https://from-env.example"
        assert adapter.chain_id == 42161


def test_avax_adapter_factory_honours_env_and_flag(monkeypatch):
    import swapsack.cli as cli

    monkeypatch.setenv("SWAPSACK_AVAX_RPC", "https://from-env.example")
    args = build_parser().parse_args(["balance"])
    with cli._avax_adapter(args) as adapter:
        assert adapter.rpc_url == "https://from-env.example"
        assert adapter.chain_id == 43114


def _wire_evm_dispatch(monkeypatch, seen):
    """Replace every EVM per-chain handler with a recorder of the chain it got."""
    import swapsack.cli as cli

    def recorder(name):
        def handler(args, factory, *a, **kw):
            seen.append((name, factory))
            return 0

        return handler

    monkeypatch.setattr(cli, "_send_evm", recorder("send"))
    monkeypatch.setattr(cli, "_swap_from_evm", recorder("swap"))


def test_send_dispatches_arb_to_the_arb_adapter(monkeypatch):
    import swapsack.cli as cli

    seen = []
    _wire_evm_dispatch(monkeypatch, seen)
    args = build_parser().parse_args(
        [
            "send",
            "0x1111111111111111111111111111111111111111",
            "--asset",
            "USDC-ARB",
            "--amount",
            "1",
        ]
    )
    assert cli.cmd_send(args) == 0
    assert seen == [("send", cli._arb_adapter)]


def test_swap_from_arb_dispatches_to_the_arb_adapter(monkeypatch):
    import swapsack.cli as cli

    seen = []
    _wire_evm_dispatch(monkeypatch, seen)
    args = build_parser().parse_args(
        ["swap", "--from", "USDC-ARB", "--to", "BTC", "--amount", "10"]
    )
    assert cli.cmd_swap(args) == 0
    assert seen == [("swap", cli._arb_adapter)]


def test_send_still_dispatches_eth_to_the_eth_adapter(monkeypatch):
    import swapsack.cli as cli

    seen = []
    _wire_evm_dispatch(monkeypatch, seen)
    args = build_parser().parse_args(
        [
            "send",
            "0x1111111111111111111111111111111111111111",
            "--asset",
            "USDC-ETH",
            "--amount",
            "1",
        ]
    )
    assert cli.cmd_send(args) == 0
    assert seen == [("send", cli._eth_adapter)]


def test_liquidity_dispatches_arb_tokens(monkeypatch):
    """The token guard used to read `chain != "ETH"`, which refused every ARB
    token pool outright — including the ARB.USDC one this exists to reach."""
    import swapsack.cli as cli

    seen = {}

    def fake(args, factory, *, memo, amount, sweep=False, **_):
        seen.update(factory=factory, memo=memo)
        return 0

    monkeypatch.setattr(cli, "_liquidity_evm", fake)
    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "USDC-ARB", "--amount", "10", "--backend", "maya"]
    )
    assert cli.cmd_add_liquidity(args) == 0
    assert seen["factory"] is cli._arb_adapter
    assert seen["memo"] == "+:" + ASSET["USDC-ARB"]


def test_liquidity_still_refuses_tron_tokens(capsys):
    """Widening the guard to "any EVM chain" must not let USDT-TRON through —
    Maya has no TRON token pool, so there is nowhere to provide it."""
    import swapsack.cli as cli

    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "USDT-TRON", "--amount", "10"]
    )
    assert cli.cmd_add_liquidity(args) == 2
    assert "only supported for EVM tokens" in capsys.readouterr().out


def test_symmetric_accepts_arb_but_not_avax():
    """A symmetric add pairs the asset leg with your own CACAO on Maya, and Maya
    has no AVAX pools at all — so Avalanche is an account-model chain that still
    cannot host one. Being EVM is necessary, not sufficient."""
    import swapsack.cli as cli

    assert set(cli._SYMMETRIC_ASSET_CHAINS) == {"ETH", "ARB"}
    assert "AVAX" in cli._EVM_ADAPTERS


def test_arb_is_a_derivable_destination():
    """An Arbitrum address IS our ETH address, so --dest for ARB no longer needs
    to be supplied by hand now that we can spend there."""
    import swapsack.cli as cli

    assert "ARB" in cli.DERIVABLE_CHAINS
    derived = cli._derive_destination_address("ARB", MNEMONIC)
    assert derived == cli._derive_destination_address("ETH", MNEMONIC)


def test_avax_is_a_derivable_destination():
    """Same reasoning as ARB: the C-Chain address IS our ETH address, and
    auto-deriving it is only honest once we can spend there too."""
    import swapsack.cli as cli

    assert "AVAX" in cli.DERIVABLE_CHAINS
    derived = cli._derive_destination_address("AVAX", MNEMONIC)
    assert derived == cli._derive_destination_address("ETH", MNEMONIC)
    # Derivable and spendable, so no receive-only warning is owed.
    assert "AVAX" not in cli.RECEIVE_ONLY_CHAINS


def test_address_command_lists_arb(monkeypatch, capsys):
    import swapsack.cli as cli

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (MNEMONIC, ""))
    args = build_parser().parse_args(["address"])
    assert cli.cmd_address(args) == 0
    out = capsys.readouterr().out
    assert "ARB:" in out


# --- `balance` must show a SYMMETRIC LP position ---------------------------
# Maya files a symmetric position under the *protocol* (CACAO) address, not the
# asset address: the same pool queried by our maya1… returns non-zero units,
# queried by our 0x… returns a zeros stub. `_report_liquidity` only probed the
# chain's own addresses, so a real 2026-08-16 ETH.USDC position printed no line
# at all — the wallet silently under-reported funds (docs/TODO.md).

USDC_POOL = "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48"
EVM_ADDRESS = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
PROTOCOL_ADDRESS = "maya1gm00vwsfcp48enm4uv9e5dhm37jtd0ye2fs0sl"

# Trimmed real shapes: the record as Maya answers it for the CACAO address …
SYMMETRIC_BY_PROTOCOL_ADDRESS = {
    "asset": USDC_POOL,
    "asset_address": EVM_ADDRESS,
    "cacao_address": PROTOCOL_ADDRESS,
    "units": "1568919094620",
    "pending_asset": "0",
    "asset_deposit_value": "2000000000",
    "cacao_deposit_value": "1770000000000",
    "asset_redeem_value": "1998000000",
    "cacao_redeem_value": "1768000000000",
}
# … and the zeros stub the *asset* address gets for that same position: HTTP
# 200, the queried address echoed back, and nothing else (verified live against
# a third party's ETH.USDC position, 2026-08-17).
SYMMETRIC_STUB_BY_ASSET_ADDRESS = {
    "asset": USDC_POOL,
    "asset_address": EVM_ADDRESS,
    "cacao_address": None,
    "units": "0",
    "pending_asset": "0",
    "asset_redeem_value": "0",
    "cacao_redeem_value": "0",
    "asset_deposit_value": "0",
    "cacao_deposit_value": "0",
}


class _FakePool:
    asset_per_protocol = 0.113


class _FakeLpClient:
    """A thornode-style client whose LP answers are keyed by address."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.queried: list[str] = []

    def liquidity_provider(self, pool, address):
        from swapsack.thorchain import parse_liquidity_provider

        self.queried.append(address)
        payload = self.answers.get(address)
        return parse_liquidity_provider(payload) if payload else None

    def pool(self, asset):
        return _FakePool()

    def close(self):
        pass


class _FakeLpBackend:
    def __init__(self, name, client):
        self.name = name
        self.client = client


def test_report_liquidity_finds_symmetric_position_via_protocol_address(capsys):
    import swapsack.cli as cli

    client = _FakeLpClient(
        {
            EVM_ADDRESS: SYMMETRIC_STUB_BY_ASSET_ADDRESS,
            PROTOCOL_ADDRESS: SYMMETRIC_BY_PROTOCOL_ADDRESS,
        }
    )
    rows = []
    cli._report_liquidity(
        [_FakeLpBackend("maya", client)],
        USDC_POOL,
        (EVM_ADDRESS,),
        {"maya": PROTOCOL_ADDRESS},
        rows,
    )
    assert [r.label.strip() for r in rows] == ["+LP maya ETH.USDC"]
    assert rows[0].lp and rows[0].asset == "USDC-ETH"  # …and it can be valued
    # The asset address is still probed (single-sided positions live there); the
    # protocol address is the addition.
    assert client.queried == [EVM_ADDRESS, PROTOCOL_ADDRESS]


def test_report_liquidity_probes_each_backends_own_protocol_address(capsys):
    """thor1… is meaningless to Maya and maya1… to THORChain, so each backend
    gets only its own protocol address."""
    import swapsack.cli as cli

    maya = _FakeLpClient({})
    thorchain = _FakeLpClient({})
    cli._report_liquidity(
        [_FakeLpBackend("maya", maya), _FakeLpBackend("thorchain", thorchain)],
        "ETH.ETH",
        (EVM_ADDRESS,),
        {"maya": PROTOCOL_ADDRESS, "thorchain": "thor1abc"},
    )
    assert maya.queried == [EVM_ADDRESS, PROTOCOL_ADDRESS]
    assert thorchain.queried == [EVM_ADDRESS, "thor1abc"]


def test_report_liquidity_does_not_double_count_a_position(capsys):
    """Today only the protocol address answers, but that is Maya's behaviour,
    not a guarantee — a backend answering on both keys must still print once,
    or the wallet over-reports."""
    import swapsack.cli as cli

    client = _FakeLpClient(
        {
            EVM_ADDRESS: SYMMETRIC_BY_PROTOCOL_ADDRESS,
            PROTOCOL_ADDRESS: SYMMETRIC_BY_PROTOCOL_ADDRESS,
        }
    )
    rows = []
    cli._report_liquidity(
        [_FakeLpBackend("maya", client)],
        USDC_POOL,
        (EVM_ADDRESS,),
        {"maya": PROTOCOL_ADDRESS},
        rows,
    )
    assert len(rows) == 1


def test_report_liquidity_skips_a_protocol_address_already_scanned(capsys):
    """The MAYA adapter's own report already carries maya1…; probing it twice
    would be a wasted round-trip per pool."""
    import swapsack.cli as cli

    client = _FakeLpClient({})
    cli._report_liquidity(
        [_FakeLpBackend("maya", client)],
        "THOR.RUNE",
        (PROTOCOL_ADDRESS,),
        {"maya": PROTOCOL_ADDRESS},
    )
    assert client.queried == [PROTOCOL_ADDRESS]


def test_balance_passes_the_derived_protocol_addresses(monkeypatch):
    """Wiring: the maya1/thor1 addresses `_report_liquidity` needs are derived
    from the seed once, not re-scanned per pool."""
    import swapsack.backends as backends_mod
    import swapsack.cli as cli
    from swapsack.chains.maya import MayaAdapter
    from swapsack.chains.thor import ThorAdapter

    def FakeReport():  # noqa: N802 (a factory named like the class it replaces)
        from swapsack.chains.base import BalanceReport

        return BalanceReport(
            symbol="ETH", confirmed=10**18, decimals=18, addresses=(EVM_ADDRESS,)
        )

    class FakeEthAdapter:
        chain = "ETH"
        asset = "ETH.ETH"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def wallet_balance(self, mnemonic):
            return FakeReport()

    seen = []
    monkeypatch.setattr(
        cli,
        "_report_liquidity",
        lambda backends, asset, addrs, protocol_addresses=None, rows=None: seen.append(
            protocol_addresses
        ),
    )
    monkeypatch.setattr(cli, "_token_rows_with_pools", lambda a, m: [])
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (MNEMONIC, ""))
    monkeypatch.setattr(cli, "_wallet_adapters", lambda args, p="": [FakeEthAdapter()])
    monkeypatch.setattr(backends_mod, "default_backends", lambda: [])
    args = build_parser().parse_args(["balance"])
    assert cli.cmd_balance(args) == 0
    assert seen == [
        {
            "maya": MayaAdapter().derive_address(MNEMONIC),
            "thorchain": ThorAdapter().derive_address(MNEMONIC),
        }
    ]


# --- withdrawing a SYMMETRIC position (the CACAO-side trigger) --------------
# A symmetric position is filed under the maya1… address, so the asset-chain
# trigger this CLI used to build looked it up where it does not exist and would
# have spent a transaction doing nothing. Maya's own traffic answers it from the
# protocol side: of 300 recent withdraws, every two-sided payout was triggered
# by a MsgDeposit from maya1… carrying 1 base unit of CACAO.

WITHDRAW_POOL = ASSET["USDC-ETH"]


def _symmetric_position(pool=WITHDRAW_POOL):
    from swapsack.thorchain import parse_liquidity_provider

    return parse_liquidity_provider(
        {
            "asset": pool,
            "asset_address": EVM_ADDRESS,
            "cacao_address": PROTOCOL_ADDRESS,
            "units": "1568919094620",
            "asset_redeem_value": "1998000000",
            "cacao_redeem_value": "1768000000000",
        }
    )


class _FakeProtocolAdapter:
    """Stand-in for MayaAdapter/ThorAdapter (the CACAO/RUNE side)."""

    chain = "MAYA"
    symbol = "CACAO"
    decimals = 10

    def __init__(self):
        self.deposits = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def derive_address(self, mnemonic, path=None):
        return PROTOCOL_ADDRESS

    def build_and_verify_native_deposit(self, **kwargs):
        self.deposits.append(kwargs)
        return SimpleNamespace(problems=[], plan=SimpleNamespace(memo=kwargs["memo"]))


def _wire_withdraw(monkeypatch, *, position, protocol=None):
    """Route cmd_withdraw_liquidity's lookups at fakes; return what it reached."""
    import swapsack.cli as cli

    protocol = protocol or _FakeProtocolAdapter()
    seen = {"loads": 0, "asset_chain": [], "executed": []}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def liquidity_provider(self, pool, address):
            if isinstance(position, Exception):
                raise position
            seen.setdefault("queried", []).append((pool, address))
            return position

    def fake_load(args):
        seen["loads"] += 1
        return (MNEMONIC, "")

    monkeypatch.setattr(cli, "_load_mnemonic", fake_load)
    monkeypatch.setattr(cli, "_protocol_adapter", lambda args, p="": protocol)
    monkeypatch.setattr(cli, "_liquidity_client", lambda args: FakeClient())
    monkeypatch.setattr(
        cli,
        "_liquidity",
        lambda args, *, memo, amount, **kw: seen["asset_chain"].append(memo) or 0,
    )
    monkeypatch.setattr(
        cli,
        "_confirm_and_execute",
        lambda prepared, adapter, args: (
            seen["executed"].append((prepared, adapter)) or 0
        ),
    )
    return seen, protocol


def test_withdraw_symmetric_triggers_from_the_protocol_side(monkeypatch, capsys):
    import swapsack.cli as cli

    seen, protocol = _wire_withdraw(monkeypatch, position=_symmetric_position())
    args = build_parser().parse_args(
        ["withdraw-liquidity", "--asset", "USDC-ETH", "--backend", "maya"]
    )
    assert cli.cmd_withdraw_liquidity(args) == 0
    # Nothing was sent on the asset chain — that trigger cannot match.
    assert seen["asset_chain"] == []
    assert len(protocol.deposits) == 1
    deposit = protocol.deposits[0]
    assert deposit["memo"] == f"-:{WITHDRAW_POOL}:10000"
    assert deposit["amount"] == 1  # dust, as Maya's own withdraw traffic uses
    assert seen["executed"] and seen["executed"][0][1] is protocol
    out = capsys.readouterr().out
    assert PROTOCOL_ADDRESS in out  # where the trigger comes from
    assert EVM_ADDRESS in out  # …and where the asset side lands


def test_withdraw_symmetric_honours_partial_bps(monkeypatch):
    import swapsack.cli as cli

    seen, protocol = _wire_withdraw(monkeypatch, position=_symmetric_position())
    args = build_parser().parse_args(
        [
            "withdraw-liquidity",
            "--asset",
            "USDC-ETH",
            "--bps",
            "2500",
            "--backend",
            "maya",
        ]
    )
    assert cli.cmd_withdraw_liquidity(args) == 0
    assert protocol.deposits[0]["memo"] == f"-:{WITHDRAW_POOL}:2500"


def test_withdraw_single_sided_still_triggers_from_the_asset_chain(monkeypatch):
    """A single-sided position IS filed under the asset address; that path must
    keep working exactly as before."""
    import swapsack.cli as cli

    seen, protocol = _wire_withdraw(monkeypatch, position=None)
    args = build_parser().parse_args(
        ["withdraw-liquidity", "--asset", "BTC", "--backend", "maya"]
    )
    assert cli.cmd_withdraw_liquidity(args) == 0
    assert seen["asset_chain"] == [f"-:{ASSET['BTC']}:10000"]
    assert protocol.deposits == []


def test_withdraw_refuses_when_the_position_lookup_fails(monkeypatch, capsys):
    """An unreachable backend must not silently fall back to the asset-chain
    trigger: for a symmetric position that spends a tx and does nothing."""
    import niquests

    import swapsack.cli as cli

    seen, protocol = _wire_withdraw(
        monkeypatch,
        position=niquests.exceptions.ConnectionError("mayanode down"),
    )
    args = build_parser().parse_args(
        ["withdraw-liquidity", "--asset", "USDC-ETH", "--backend", "maya"]
    )
    assert cli.cmd_withdraw_liquidity(args) == 1
    assert seen["asset_chain"] == []
    assert protocol.deposits == []
    assert "ABORTED" in capsys.readouterr().err


def test_withdraw_asks_for_the_keystore_once(monkeypatch):
    """Routing needs the seed to derive maya1…, and so does the withdraw — but
    the user must be prompted for the keystore passphrase only once."""
    import swapsack.cli as cli

    for position in (_symmetric_position(), None):
        seen, _ = _wire_withdraw(monkeypatch, position=position)
        args = build_parser().parse_args(
            ["withdraw-liquidity", "--asset", "USDC-ETH", "--backend", "maya"]
        )
        assert cli.cmd_withdraw_liquidity(args) == 0
        assert seen["loads"] == 1


def test_withdraw_refuses_a_backend_without_the_pool_before_looking_anything_up(
    monkeypatch, capsys
):
    """ARB pools exist only on Maya. The wrong-backend refusal must still come
    first — otherwise the symmetric lookup goes to a node that cannot know the
    pool, and its 404 is reported as "cannot tell what kind of position this
    is", which is both wrong and unactionable."""
    import swapsack.cli as cli

    seen, protocol = _wire_withdraw(monkeypatch, position=None)
    args = build_parser().parse_args(["withdraw-liquidity", "--asset", "USDC-ARB"])
    assert cli.cmd_withdraw_liquidity(args) == 2
    assert seen["loads"] == 0  # not even the keystore was opened
    assert seen["asset_chain"] == []
    assert protocol.deposits == []
    assert "maya" in capsys.readouterr().err.lower()


def test_symmetric_add_refuses_wrong_backend_before_touching_the_keystore(
    monkeypatch, capsys
):
    """`--symmetric` must run the same up-front backend check as a single-sided add.

    cmd_add_liquidity dispatched to _liquidity_symmetric *before* the
    _lp_backend_refused guard, so `--symmetric --asset USDC-ARB` (the default
    backend being thorchain, which has no ARB pools at all) prompted for the
    keystore passphrase and then died on a missing router instead of saying
    which backend to use. README and CHANGELOG both promise the up-front
    refusal.
    """
    import swapsack.cli as cli

    def boom(*a, **kw):
        raise AssertionError("keystore touched before the backend was validated")

    monkeypatch.setattr(cli, "_load_mnemonic", boom)
    args = build_parser().parse_args(
        ["add-liquidity", "--symmetric", "--asset", "USDC-ARB", "--amount", "100"]
    )
    assert args.backend == "thorchain"  # the default that makes this reachable
    assert cli.cmd_add_liquidity(args) == 2
    assert "ARB liquidity exists only on maya" in capsys.readouterr().err


# --- `balance` renders one aligned, valued sheet ----------------------------


def _fake_sheet_adapters(monkeypatch, *, reports):
    """Wire cmd_balance at fake adapters returning the given BalanceReports."""
    import swapsack.backends as backends_mod
    import swapsack.cli as cli

    class FakeAdapter:
        def __init__(self, report):
            self.report = report
            self.chain = report.symbol
            self.asset = f"{report.symbol}.{report.symbol}"
            self.lp_backends = ()  # no LP probing in this fixture

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def wallet_balance(self, mnemonic):
            return self.report

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (MNEMONIC, ""))
    monkeypatch.setattr(cli, "_token_rows_with_pools", lambda a, m: [])
    monkeypatch.setattr(
        cli, "_wallet_adapters", lambda args, p="": [FakeAdapter(r) for r in reports]
    )
    monkeypatch.setattr(backends_mod, "default_backends", lambda: [])


def test_balance_prints_an_aligned_sheet_with_a_total(monkeypatch, capsys):
    import swapsack.cli as cli
    from swapsack.chains.base import BalanceReport

    _fake_sheet_adapters(
        monkeypatch,
        reports=[
            BalanceReport(symbol="BTC", confirmed=100_000_000, decimals=8),
            BalanceReport(symbol="ETH", confirmed=10**18, decimals=18),
        ],
    )
    monkeypatch.setattr(
        cli,
        "_sheet_prices",
        lambda args, rows: (cli_unit(), {"BTC": 100.0, "ETH": 10.0}),
    )
    args = build_parser().parse_args(["balance"])
    assert cli.cmd_balance(args) == 0
    out = capsys.readouterr().out
    assert "€100.00" in out and "€10.00" in out
    assert "total" in out and "€110.00" in out
    # The value column lines up (right-aligned numbers end in one column).
    import re

    ends = {
        m.end() for line in out.splitlines() for m in [re.search(r"€[\d.]+", line)] if m
    }
    assert len(ends) == 1


def cli_unit():
    from swapsack.pricefeed import UNITS

    return UNITS["EUR"]


def test_balance_no_price_check_makes_no_price_request(monkeypatch, capsys):
    """--no-price-check must suppress the *request*: one lookup of the whole
    sheet tells a third party every asset this IP holds."""
    import swapsack.cli as cli
    from swapsack.chains.base import BalanceReport

    _fake_sheet_adapters(
        monkeypatch, reports=[BalanceReport(symbol="BTC", confirmed=1, decimals=8)]
    )

    def boom(*a, **kw):
        raise AssertionError("the price feed must not be consulted")

    monkeypatch.setattr("swapsack.pricefeed.PriceFeed.spot", boom)
    args = build_parser().parse_args(["balance", "--no-price-check"])
    assert cli.cmd_balance(args) == 0
    out = capsys.readouterr().out
    assert "0.00000001" in out  # the balances still print…
    assert "€" not in out and "total" not in out  # …just unvalued


def test_balance_unit_flag_selects_the_currency(monkeypatch):
    import swapsack.cli as cli

    seen = {}

    class FakeFeed:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def spot(self, ids, *, vs):
            seen.update(ids=ids, vs=vs)
            return {"bitcoin": {"btc": 1.0}}

    monkeypatch.setattr("swapsack.pricefeed.PriceFeed", lambda *a, **kw: FakeFeed())
    args = build_parser().parse_args(["balance", "--unit", "btc"])
    from swapsack.report import Row

    unit, prices = cli._sheet_prices(args, [Row(label="BTC", amount=1.0, asset="BTC")])
    assert unit.name == "BTC"
    assert seen["vs"] == ("btc",) and seen["ids"] == ["bitcoin"]
    assert prices == {"BTC": 1.0}


def test_balance_survives_a_dead_price_feed(monkeypatch, capsys):
    """A courtesy lookup must never cost the user their balances."""
    import niquests

    import swapsack.cli as cli
    from swapsack.report import Row

    def boom(*a, **kw):
        raise niquests.exceptions.ConnectionError("coingecko down")

    monkeypatch.setattr("swapsack.pricefeed.PriceFeed.spot", boom)
    args = build_parser().parse_args(["balance"])
    unit, prices = cli._sheet_prices(args, [Row(label="BTC", amount=1.0, asset="BTC")])
    assert unit is None and prices == {}
    assert "price" in capsys.readouterr().err.lower()


def test_balance_names_a_chain_that_did_not_answer(monkeypatch, capsys):
    """A chain whose lookup raises must not silently vanish from the sheet.

    It used to be `continue`d with only a stderr line, so stdout carried an
    authoritative `total` computed as if that chain held nothing — the exact
    thing report.py's docstring forbids ("may not quietly overstate what you
    have or understate what it does not know"). Stderr is easy to lose.
    """
    import swapsack.backends as backends_mod
    import swapsack.cli as cli
    from swapsack.chains.base import BalanceReport

    class FakeAdapter:
        def __init__(self, symbol, *, fail=False):
            self.chain = symbol
            self.asset = f"{symbol}.{symbol}"
            self.lp_backends = ()
            self._fail = fail

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def wallet_balance(self, mnemonic):
            if self._fail:
                raise RuntimeError("upstream 502")
            return BalanceReport(symbol=self.chain, confirmed=10**8, decimals=8)

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (MNEMONIC, ""))
    monkeypatch.setattr(cli, "_token_rows_with_pools", lambda a, m: [])
    monkeypatch.setattr(
        cli,
        "_wallet_adapters",
        lambda args, p="": [FakeAdapter("BTC"), FakeAdapter("ZEC", fail=True)],
    )
    monkeypatch.setattr(backends_mod, "default_backends", lambda: [])
    monkeypatch.setattr(
        cli, "_sheet_prices", lambda args, rows: (cli_unit(), {"BTC": 100.0})
    )
    args = build_parser().parse_args(["balance"])
    assert cli.cmd_balance(args) == 0
    out = capsys.readouterr().out
    assert "ZEC" in out, "the chain that failed must still appear on the sheet"
    assert "did not answer" in out
    # The total is still printed (it is useful) but must not read as complete.
    assert "total" in out
    assert "INCOMPLETE" in out
    # …and the failed chain's unknown amount is not silently counted as zero.
    assert "€100.00" in out


def test_balance_keeps_each_lp_row_under_its_own_token(monkeypatch, capsys):
    """A token's LP row must follow that token's balance row, not another's.

    The sheet indents an LP line under the row above it and keeps that row
    alive at zero so the position does not dangle. Emitting every token balance
    and then every token pool position files each position under the wrong
    token, so the wrong row is the one protected from collapsing.
    """
    import swapsack.backends as backends_mod
    import swapsack.cli as cli
    from swapsack.chains.base import BalanceReport
    from swapsack.report import Row

    class FakeEth:
        chain = "ETH"
        asset = "ETH.ETH"
        token_suffix = "ETH"
        lp_backends = None
        tracked_tokens = (("USDT", "0xdac17f", 6), ("USDC", "0xa0b869", 6))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def wallet_balance(self, mnemonic):
            return BalanceReport(symbol="ETH", confirmed=0, decimals=18)

        def token_balances(self, mnemonic):
            return [
                BalanceReport(symbol="USDT-ETH", confirmed=0, decimals=6),
                BalanceReport(symbol="USDC-ETH", confirmed=0, decimals=6),
            ]

    def fake_lp(backends, asset, addrs, protocol_addresses=None, rows=None):
        # Only the USDC pool holds a position.
        if asset.startswith("ETH.USDC"):
            rows.append(Row(label="  +LP maya ETH.USDC", amount=5.0, lp=True))

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (MNEMONIC, ""))
    monkeypatch.setattr(cli, "_report_liquidity", fake_lp)
    monkeypatch.setattr(cli, "_wallet_adapters", lambda args, p="": [FakeEth()])
    monkeypatch.setattr(backends_mod, "default_backends", lambda: [])
    monkeypatch.setattr(cli, "_sheet_prices", lambda args, rows: (None, {}))
    args = build_parser().parse_args(["balance"])
    assert cli.cmd_balance(args) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    labels = [ln.split()[0] for ln in lines]
    assert "+LP" in lines[labels.index("USDC-ETH") + 1], (
        f"the LP row must sit directly under USDC-ETH, got: {lines}"
    )
    # …and USDT-ETH, which has no position, is free to collapse into `zero:`.
    assert "USDT-ETH" not in " ".join(ln for ln in lines if not ln.startswith("zero:"))


def test_lp_backend_refusal_survives_a_chain_with_no_pools_anywhere():
    """`lp_backends = ()` means no pools on any backend — say so, don't crash.

    The message formatted allowed[0], which IndexErrors on the empty tuple.
    Unreachable while BSC/MAYA stay out of the adapter registries, but
    _lp_asset_factory widened the set of callers.
    """
    import swapsack.cli as cli

    class Poolless:
        chain = "BSC"
        lp_backends = ()

    args = build_parser().parse_args(
        ["add-liquidity", "--asset", "USDC-ETH", "--amount", "1"]
    )
    assert cli._lp_backend_refused(args, Poolless()) is True


def test_chainflip_vault_swap_refuses_a_sweep(capsys):
    # Chainflip reads the change output as the refund address and needs it
    # above dust, so there is nothing to sweep into. This must fail before any
    # network call, not build a transaction the protocol would reject.
    from swapsack.swap import SwapRequest

    args = build_parser().parse_args(
        ["swap", "--from", "BTC", "--to", "ETH", "--amount", "max"]
    )
    rc = cli._swap_via_chainflip(
        args,
        adapter=None,
        backend=None,
        request=SwapRequest(
            from_asset="BTC.BTC", to_asset="ETH.ETH", amount=0, destination="0xdead"
        ),
        dest="0xdead",
        mnemonic="",
        utxos=[],
        fee_rate=2,
        change_address="bc1qchange",
        sweep=True,
    )
    assert rc == 1
    assert "--amount max cannot be a Chainflip vault swap" in capsys.readouterr().err


def test_the_chainflip_re_quote_is_sent_in_native_source_units(monkeypatch, capsys):
    """The execution re-quote speaks the source asset's own units, not 1e8.

    ``request.amount`` is in the wallet-wide 1e8 base units every backend is
    compared in; ``ChainflipClient.quote`` takes the source asset's native ones,
    which is why ``try_quote`` routes through ``deposit_units``. The two happen
    to coincide for BTC and *only* for BTC, so a second UTXO source chain would
    silently quote the wrong amount — and encode the on-chain floor from it.
    """
    import swapsack.chainflip as chainflip_mod
    from swapsack.swap import SwapRequest

    monkeypatch.setitem(
        chainflip_mod.CHAINFLIP_ASSETS, "BTC.BTC", ("Bitcoin", "BTC", 6)
    )
    sent = []

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def quote(self, src, dst, amount):
            sent.append(amount)
            raise chainflip_mod.ChainflipError("stop here")

    args = build_parser().parse_args(
        ["swap", "--from", "BTC", "--to", "ETH", "--amount", "0.1"]
    )
    rc = cli._swap_via_chainflip(
        args,
        adapter=None,
        backend=SimpleNamespace(name="chainflip", client=_Client()),
        request=SwapRequest(
            from_asset="BTC.BTC",
            to_asset="ETH.ETH",
            amount=10_000_000,
            destination="0xdead",
        ),
        dest="0xdead",
        mnemonic="",
        utxos=[],
        fee_rate=2,
        change_address="bc1qchange",
        sweep=False,
    )
    assert rc == 1
    assert sent == [chainflip_mod.deposit_units(10_000_000, 6)]


# --- --allow-unconfirmed: opt-in mempool inputs, priced by CPFP ---


def _fake_unconfirmed_wallet(monkeypatch):
    """A one-address BTC wallet holding one confirmed and one mempool UTXO."""
    from swapsack.chains.coins import Utxo

    seen: dict = {}

    class FakeBtc:
        chain = "BTC"
        asset = "BTC.BTC"
        account = "m/84'/0'/0'"
        change_path = "m/84'/0'/0'/1/0"
        default_derivation = "m/84'/0'/0'/0/0"
        unconfirmed_spendable = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def derive_address(self, mnemonic, path=None):
            return "bc1qowned" if path == self.default_derivation else "bc1qchange"

        def address_info(self, address):
            return None  # unused: scan_account is stubbed

        def fetch_utxos(self, address, *, include_unconfirmed=False):
            seen["include_unconfirmed"] = include_unconfirmed
            utxos = [Utxo(txid="aa" * 32, vout=0, value=1_000_000, address=address)]
            if include_unconfirmed:
                utxos.append(
                    Utxo(
                        txid="bb" * 32,
                        vout=0,
                        value=500_000,
                        address=address,
                        confirmed=False,
                    )
                )
            return utxos

        def cpfp_deficits(self, utxos, fee_rate):
            import dataclasses

            seen["cpfp_fee_rate"] = fee_rate
            return [
                u if u.confirmed else dataclasses.replace(u, ancestor_deficit=800)
                for u in utxos
            ]

        def fetch_fee_rate(self, target_blocks=2):
            return 5.0

        def sweep_send_amount(self, total, n_inputs, fee_rate, memo_len=0):
            return total - 1000, 1000

        def build_and_verify_send(self, **kwargs):
            seen.update(kwargs)
            raise SystemExit(0)  # stop before printing/broadcasting

    def fake_scan(*, derive_address, probe, account):
        return [
            (
                "m/84'/0'/0'/0/0",
                "bc1qowned",
                SimpleNamespace(confirmed=1_000_000, pending=500_000),
            )
        ]

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: ("mnemonic", ""))
    monkeypatch.setattr(cli, "_btc_adapter", lambda args, passphrase="": FakeBtc())
    monkeypatch.setattr("swapsack.chains.scan.scan_account", fake_scan)
    return seen


def test_send_is_confirmed_only_without_the_flag(monkeypatch):
    seen = _fake_unconfirmed_wallet(monkeypatch)
    args = build_parser().parse_args(["send", "bc1qdest", "--amount", "0.001"])
    assert args.allow_unconfirmed is False
    with pytest.raises(SystemExit):
        cli._send_utxo(args, cli._btc_adapter)
    assert seen["include_unconfirmed"] is False
    assert [u.value for u in seen["scanned_utxos"]] == [1_000_000]
    assert "cpfp_fee_rate" not in seen  # nothing unconfirmed to price


def test_allow_unconfirmed_spends_the_mempool_utxo_and_prices_its_parent(monkeypatch):
    seen = _fake_unconfirmed_wallet(monkeypatch)
    args = build_parser().parse_args(
        ["send", "bc1qdest", "--amount", "0.001", "--allow-unconfirmed"]
    )
    with pytest.raises(SystemExit):
        cli._send_utxo(args, cli._btc_adapter)
    assert seen["include_unconfirmed"] is True
    # Priced against the same rate the spend is built at, not a fresh estimate.
    assert seen["cpfp_fee_rate"] == 5.0
    spent = [(u.value, u.confirmed, u.ancestor_deficit) for u in seen["scanned_utxos"]]
    assert spent == [(1_000_000, True, 0), (500_000, False, 800)]


def test_allow_unconfirmed_warns_before_it_builds(monkeypatch, capsys):
    _fake_unconfirmed_wallet(monkeypatch)
    args = build_parser().parse_args(
        ["send", "bc1qdest", "--amount", "0.001", "--allow-unconfirmed"]
    )
    with pytest.raises(SystemExit):
        cli._send_utxo(args, cli._btc_adapter)
    out = capsys.readouterr()
    assert "unconfirmed" in (out.out + out.err).lower()


def test_the_cpfp_surcharge_is_reported_for_the_inputs_actually_spent(capsys):
    """Scanning prices every mempool coin; the transaction spends a subset.

    `select_coins` takes confirmed coins first, so a spend that the confirmed
    balance covers uses none of the unconfirmed ones it was offered — and a
    surcharge named for a parent whose output is never spent is a fee figure
    that does not match the transaction being approved.
    """
    from swapsack.chains.coins import Utxo

    confirmed = Utxo(txid="aa" * 32, vout=0, value=1_000_000, address="bc1qowned")
    prepared = SimpleNamespace(
        problems=[],
        built=SimpleNamespace(inputs=[confirmed]),
        plan=SimpleNamespace(expiry=None),
    )
    args = build_parser().parse_args(["send", "bc1qdest", "--amount", "0.001"])
    assert cli._confirm_and_execute(prepared, None, args) == 0
    assert "CPFP" not in capsys.readouterr().err


def test_the_cpfp_surcharge_is_reported_when_a_mempool_input_is_spent(capsys):
    from swapsack.chains.coins import Utxo

    prepared = SimpleNamespace(
        problems=[],
        built=SimpleNamespace(
            inputs=[
                Utxo(txid="aa" * 32, vout=0, value=1_000_000, address="bc1qowned"),
                Utxo(
                    txid="bb" * 32,
                    vout=0,
                    value=500_000,
                    address="bc1qowned",
                    confirmed=False,
                    ancestor_deficit=800,
                ),
            ]
        ),
        plan=SimpleNamespace(expiry=None),
    )
    args = build_parser().parse_args(["send", "bc1qdest", "--amount", "0.001"])
    assert cli._confirm_and_execute(prepared, None, args) == 0
    err = capsys.readouterr().err
    assert "800" in err
    assert "CPFP" in err


def test_allow_unconfirmed_is_offered_on_every_utxo_spend_command():
    parser = build_parser()
    for argv in (
        ["send", "bc1qdest", "--amount", "0.001"],
        ["swap", "--from", "BTC", "--to", "ETH", "--amount", "0.001"],
        ["add-liquidity", "--asset", "BTC", "--amount", "0.001"],
        ["withdraw-liquidity", "--asset", "BTC"],
    ):
        assert parser.parse_args([*argv, "--allow-unconfirmed"]).allow_unconfirmed
    # ...and nowhere else: `balance` spends nothing, so the flag would be noise.
    with pytest.raises(SystemExit):
        parser.parse_args(["balance", "--allow-unconfirmed"])


def test_btc_adapter_uses_both_default_endpoints(monkeypatch):
    # Nothing chosen: two interchangeable public explorers, so one going quiet
    # mid-scan is survivable.
    monkeypatch.delenv("SWAPSACK_ESPLORA", raising=False)
    args = build_parser().parse_args(["balance"])
    assert len(cli._btc_adapter(args)._candidates) == 2


@pytest.mark.parametrize("via", ["flag", "env"])
def test_a_chosen_esplora_is_the_only_one_used(monkeypatch, via):
    # Naming an endpoint is a choice of operator (or a self-hosted instance):
    # the fallback must not hand a second one the wallet's addresses.
    monkeypatch.delenv("SWAPSACK_ESPLORA", raising=False)
    argv = ["balance"]
    if via == "flag":
        argv = ["--esplora", "https://my-own.example/api", *argv]  # a global flag
    else:
        monkeypatch.setenv("SWAPSACK_ESPLORA", "https://my-own.example/api")
    args = build_parser().parse_args(argv)
    assert cli._btc_adapter(args)._candidates == ("https://my-own.example/api",)


# --- bump: fee-replacing a stuck BTC transaction (BIP125 RBF) ---------------


def test_bump_defaults_to_the_configured_fee_target():
    args = build_parser().parse_args(["bump", "cc" * 32])
    assert args.txid == "cc" * 32
    assert args.fee_rate is None  # -> fetch_fee_rate(--fee-blocks)
    assert args.confirm is False  # a bump is a spend: dry run unless asked


def test_bump_refuses_a_rate_and_a_block_target_at_once():
    """Two different ways to say what to pay is a contradiction, not a default."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["bump", "cc" * 32, "--fee-rate", "10", "--fee-blocks", "2"]
        )


def _bump_adapter(monkeypatch, *, memo=True, confirmed=False, parent_confirmed=True):
    """A BtcAdapter whose Esplora reads are canned: one stuck deposit, ours."""
    import swapsack.cli as cli
    from swapsack.chains.base import AddressInfo
    from swapsack.chains.btc import BtcAdapter, parse_tx_summary

    mnemonic = (
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )
    real = BtcAdapter()
    spend_path, change_path = "m/84'/0'/0'/0/0", "m/84'/0'/0'/1/0"
    spender = real.derive_address(mnemonic, spend_path)
    change = real.derive_address(mnemonic, change_path)
    memo_bytes = b"=:ETH.ETH:0x1111111111111111111111111111111111111111"
    vout = [
        {
            "scriptpubkey_type": "v0_p2wpkh",
            "scriptpubkey_address": "bc1qct4mxayrdy96d4py20l4u02mu06r667f42p9fp",
            "value": 1_000_000,
        }
    ]
    if memo:
        vout.append(
            {
                "scriptpubkey": (b"\x6a" + bytes([len(memo_bytes)]) + memo_bytes).hex(),
                "scriptpubkey_type": "op_return",
                "value": 0,
            }
        )
    vout.append(
        {
            "scriptpubkey_type": "v0_p2wpkh",
            "scriptpubkey_address": change,
            "value": 100_000,
        }
    )
    payload = {
        "txid": "cc" * 32,
        "weight": 1000,
        "fee": 250,
        "status": (
            {"confirmed": True, "block_height": 959260}
            if confirmed
            else {"confirmed": False}
        ),
        "vin": [
            {
                "txid": "dd" * 32,
                "vout": 1,
                "sequence": 0xFFFFFFFD,
                "prevout": {
                    "scriptpubkey_type": "v0_p2wpkh",
                    "scriptpubkey_address": spender,
                    "value": 1_100_250,
                },
            }
        ],
        "vout": vout,
    }
    broadcast: list[str] = []

    class FakeBtc(BtcAdapter):
        def address_info(self, address):
            used = address in {spender, change}
            return AddressInfo(
                has_history=used, confirmed=1_100_250 if used else 0, pending=0
            )

        def fetch_tx(self, txid):
            if txid == payload["txid"]:
                return parse_tx_summary(payload)
            if txid == "dd" * 32:  # the parent, priced for CPFP
                return parse_tx_summary(
                    {
                        "txid": txid,
                        "weight": 800,  # -> 200 vB
                        "fee": 4000 if parent_confirmed else 200,
                        "status": (
                            {"confirmed": True, "block_height": 959000}
                            if parent_confirmed
                            else {"confirmed": False}
                        ),
                        "vin": [],
                        "vout": [],
                    }
                )
            return None

        def fetch_fee_rate(self, target_blocks=2):
            return 10.0

        def broadcast(self, raws):
            broadcast.extend(raws)
            return "ee" * 32

    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (mnemonic, ""))
    monkeypatch.setattr(cli, "_btc_adapter", lambda args, passphrase="": FakeBtc())
    return broadcast, change


def test_bump_dry_run_shows_the_new_fee_and_broadcasts_nothing(monkeypatch, capsys):
    """The default is a dry run: a bump re-spends real coins."""
    import swapsack.cli as cli

    broadcast, change = _bump_adapter(monkeypatch)
    args = build_parser().parse_args(["bump", "cc" * 32])
    assert cli.cmd_bump(args) == 0
    out = capsys.readouterr().out
    assert "250" in out and "2500" in out  # old fee -> new fee
    assert "97750" in out or "97,750" in out  # the change it comes out of
    assert change in out
    assert "DRY RUN" in out
    assert broadcast == []


def test_bump_broadcasts_the_replacement_when_confirmed(monkeypatch, capsys):
    import swapsack.cli as cli

    broadcast, _ = _bump_adapter(monkeypatch)
    args = build_parser().parse_args(["bump", "cc" * 32, "--confirm", "--yes"])
    assert cli.cmd_bump(args) == 0
    assert len(broadcast) == 1
    out = capsys.readouterr().out
    assert "ee" * 32 in out  # the replacement's own txid, which is a new one


def test_bump_warns_that_a_swap_quote_may_have_gone_stale(monkeypatch, capsys):
    """Unsticking a deposit faster does not re-quote it — say so before broadcast.

    The memo carries the min-out limit the swap was quoted at; if the market has
    moved past it while the transaction sat in the mempool, the protocol refunds
    rather than fills.
    """
    import swapsack.cli as cli

    _bump_adapter(monkeypatch)
    args = build_parser().parse_args(["bump", "cc" * 32])
    assert cli.cmd_bump(args) == 0
    combined = "".join(capsys.readouterr())
    assert "memo" in combined.lower()
    assert "refund" in combined.lower() or "stale" in combined.lower()


def test_bump_of_a_plain_send_says_nothing_about_swaps(monkeypatch, capsys):
    import swapsack.cli as cli

    _bump_adapter(monkeypatch, memo=False)
    args = build_parser().parse_args(["bump", "cc" * 32])
    assert cli.cmd_bump(args) == 0
    combined = "".join(capsys.readouterr())
    assert "refund" not in combined.lower()


def test_bump_refuses_a_confirmed_transaction(monkeypatch, capsys):
    import swapsack.cli as cli

    _bump_adapter(monkeypatch, confirmed=True)
    args = build_parser().parse_args(["bump", "cc" * 32])
    assert cli.cmd_bump(args) == 1
    assert "959260" in capsys.readouterr().err


def test_bump_says_so_when_the_chain_has_never_seen_the_txid(monkeypatch, capsys):
    import swapsack.cli as cli

    _bump_adapter(monkeypatch)
    args = build_parser().parse_args(["bump", "ab" * 32])
    assert cli.cmd_bump(args) == 1
    assert "never seen" in capsys.readouterr().err


def test_bump_also_drags_its_own_unconfirmed_parent(monkeypatch, capsys):
    """Raising this transaction's rate is no use while an ancestor holds it down.

    A miner takes the package or none of it, so the bump pays the parent's
    shortfall too — otherwise the "faster" replacement confirms no sooner than
    the transaction it replaced.
    """
    import swapsack.cli as cli

    _bump_adapter(monkeypatch, parent_confirmed=False)
    args = build_parser().parse_args(["bump", "cc" * 32])
    assert cli.cmd_bump(args) == 0
    out = capsys.readouterr().out
    # Parent is 200 vB paying 200 sats; lifting it to 10 sat/vB costs 1800 on
    # top of this transaction's own 2500.
    assert "4300" in out
    assert "1800" in out and "child-pays-for-parent" in out


# --- history / utxos --------------------------------------------------------


HISTORY_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def _history_btc(monkeypatch, *, per_address=None, no_history=False):
    """A BtcAdapter whose scan finds two addresses and whose history is scripted."""
    from swapsack.chains.base import AddressInfo, TxEntry, TxSummary
    from swapsack.chains.btc import BtcAdapter
    from swapsack.chains.history import AddressTxs

    adapter = BtcAdapter()
    recv = adapter.derive_address(HISTORY_MNEMONIC, "m/84'/0'/0'/0/0")
    change = adapter.derive_address(HISTORY_MNEMONIC, "m/84'/0'/0'/1/0")
    used = {recv, change}

    funding = TxSummary(
        txid="aa" * 32,
        confirmed=True,
        block_height=900_000,
        block_time=1_756_000_000,
        fee=200,
        vsize=110,
        inputs=(
            TxEntry(value=1_000_000, address="bc1qstranger", txid="00" * 32, vout=0),
        ),
        outputs=(TxEntry(value=500_000, address=recv),),
    )
    deposit = TxSummary(
        txid="bb" * 32,
        confirmed=False,
        block_height=None,
        block_time=None,
        fee=1_000,
        vsize=200,
        inputs=(TxEntry(value=500_000, address=recv, txid="aa" * 32, vout=0),),
        outputs=(
            TxEntry(value=300_000, address="bc1qvault"),
            TxEntry(value=0, op_return=True, op_return_data=b"=:ETH.USDT:0xdead"),
            TxEntry(value=199_000, address=change),
        ),
    )
    scripted = per_address or {recv: [funding, deposit], change: [deposit]}

    class FakeBtc(BtcAdapter):
        def address_info(self, address):
            return AddressInfo(has_history=address in used, confirmed=0, pending=0)

        def address_txs(self, address, *, limit=500):
            return AddressTxs(transactions=list(scripted.get(address, ())))

        def fetch_utxos(self, address, *, include_unconfirmed=False):
            from swapsack.chains.coins import Utxo

            if address != change:
                return []
            return [Utxo(txid="bb" * 32, vout=2, value=199_000, address=address)]

    if no_history:
        # How a chain with no address index looks to the dispatch: ZecAdapter
        # simply never defines the method (pinned by the test below).
        FakeBtc.address_txs = None
    fake = FakeBtc()
    # The registry holds a reference taken at import time, so patching
    # cli._btc_adapter by name would not reach it.
    for chain in ("BTC", "ZEC"):
        monkeypatch.setitem(cli._UTXO_ADAPTERS, chain, lambda args, passphrase="": fake)
    monkeypatch.setattr(cli, "_load_mnemonic", lambda args: (HISTORY_MNEMONIC, ""))
    return fake, recv, change


def test_history_lists_the_transactions_newest_first(monkeypatch, capsys):
    _history_btc(monkeypatch)
    args = build_parser().parse_args(["history"])
    assert cli.cmd_history(args) == 0
    out = capsys.readouterr().out
    # The unconfirmed swap deposit leads; the funding transaction follows.
    assert out.index("bb" * 32) < out.index("aa" * 32)
    assert "swap deposit" in out
    assert "bc1qvault" in out
    assert "=:ETH.USDT:0xdead" in out


def test_history_json_is_machine_readable_and_keeps_the_memo(monkeypatch, capsys):
    _history_btc(monkeypatch)
    args = build_parser().parse_args(["history", "--json"])
    assert cli.cmd_history(args) == 0
    payload = json.loads(capsys.readouterr().out)
    deposit = next(t for t in payload["transactions"] if t["txid"] == "bb" * 32)
    assert deposit["net"] == -301_000
    assert deposit["memo"] == b"=:ETH.USDT:0xdead".hex()
    assert deposit["counterparties"] == ["bc1qvault"]


def test_history_refuses_zec_with_a_reason_rather_than_an_empty_list(
    monkeypatch, capsys
):
    """A chain whose data source cannot answer must say so. An empty listing
    would read as "you have no transactions" — the opposite of the truth."""
    _history_btc(monkeypatch, no_history=True)
    args = build_parser().parse_args(["history", "--asset", "ZEC"])
    assert cli.cmd_history(args) == 1
    assert "not available" in capsys.readouterr().err.lower()


def test_utxos_lists_spent_and_unspent_by_default(monkeypatch, capsys):
    _history_btc(monkeypatch)
    args = build_parser().parse_args(["utxos"])
    assert cli.cmd_utxos(args) == 0
    out = capsys.readouterr().out
    assert f"spent by {'bb' * 32}" in out  # the funding output, now spent
    assert "unspent" in out
    assert "1 unspent of 2 outputs" in out


def test_utxos_can_show_only_the_unspent_ones(monkeypatch, capsys):
    _history_btc(monkeypatch)
    args = build_parser().parse_args(["utxos", "--unspent"])
    assert cli.cmd_utxos(args) == 0
    out = capsys.readouterr().out
    assert "spent by" not in out
    assert f"{'bb' * 32}:2" in out


def test_utxos_can_show_only_the_spent_ones(monkeypatch, capsys):
    _history_btc(monkeypatch)
    args = build_parser().parse_args(["utxos", "--spent"])
    assert cli.cmd_utxos(args) == 0
    out = capsys.readouterr().out
    assert f"{'aa' * 32}:0" in out
    assert f"{'bb' * 32}:2" not in out


def test_utxos_falls_back_to_the_unspent_set_when_history_is_unavailable(
    monkeypatch, capsys
):
    """ZEC's lightwalletd serves unspent outputs but not a transaction history.
    Half an answer beats none — as long as the missing half is named."""
    _history_btc(monkeypatch, no_history=True)
    args = build_parser().parse_args(["utxos", "--asset", "ZEC"])
    assert cli.cmd_utxos(args) == 0
    captured = capsys.readouterr()
    assert f"{'bb' * 32}:2" in captured.out
    assert "not available" in captured.err.lower()


def test_utxos_json_carries_the_outpoint_and_the_spender(monkeypatch, capsys):
    _history_btc(monkeypatch)
    args = build_parser().parse_args(["utxos", "--json"])
    assert cli.cmd_utxos(args) == 0
    payload = json.loads(capsys.readouterr().out)
    spent = next(o for o in payload["outputs"] if o["spent_by"])
    assert spent["outpoint"] == f"{'aa' * 32}:0"
    assert spent["spent_by"] == "bb" * 32
    assert payload["unspent_total"] == 199_000


def test_history_warns_when_the_walk_was_cut_short(monkeypatch, capsys):
    """A truncated walk cannot tell spent from unspent. Printing the listing
    without saying so would present half a picture as the whole one."""
    from swapsack.chains.history import AddressTxs

    fake, recv, _change = _history_btc(monkeypatch)
    monkeypatch.setattr(
        type(fake),
        "address_txs",
        lambda self, address, *, limit=500: AddressTxs(transactions=[], truncated=True),
    )
    args = build_parser().parse_args(["history"])
    assert cli.cmd_history(args) == 0
    assert "incomplete" in capsys.readouterr().err.lower()


def test_zec_adapter_really_has_no_history_source():
    """Pins the premise the two fallback tests above stand on."""
    from swapsack.chains.zcash import ZecAdapter

    assert getattr(ZecAdapter, "address_txs", None) is None


def test_history_and_utxos_only_offer_the_utxo_chains():
    """ETH/TRON have no address-indexed source wired up, so offering them would
    promise a listing that cannot be produced."""
    for command in ("history", "utxos"):
        args = build_parser().parse_args([command, "--asset", "DASH"])
        assert args.asset == "DASH"
        with pytest.raises(SystemExit):
            build_parser().parse_args([command, "--asset", "ETH"])


# --- status: what Chainflip made of a deposit -------------------------------


def _cf_status(**overrides):
    from swapsack.chainflip import SwapStatus

    base = dict(
        state="COMPLETED",
        swap_id="1776971",
        src_chain="Bitcoin",
        src_asset="BTC",
        dest_chain="Ethereum",
        dest_asset="USDT",
        dest_address="0xrecipient",
        deposit_amount=410_000,
        deposit_txid="3b" * 32,
        output_amount=421_500_000,
        egress_txid="0xpayout",
        witnessed_at=1_756_000_000_000,
    )
    return SwapStatus(**{**base, **overrides})


def _stub_chainflip(monkeypatch, status):
    """Point cmd_status's Chainflip probe at a scripted answer."""
    import swapsack.cli as cli

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def swap_status(self, txid):
            if isinstance(status, Exception):
                raise status
            return status

    monkeypatch.setattr(cli, "_chainflip_client", lambda args: FakeClient())
    monkeypatch.setattr(cli, "_print_onchain_tx", lambda args: None)
    monkeypatch.setattr(
        "swapsack.backends.default_backends",
        lambda: [_StubBackend("thorchain", NOT_OBSERVED)],
    )


def test_status_reports_a_chainflip_swap_and_does_not_call_it_unobserved(
    monkeypatch, capsys
):
    """A Chainflip vault swap is invisible to thornode, so the old `status`
    ended on "not observed by thorchain/maya" — which reads as "your deposit
    went nowhere" for a swap that in fact completed."""
    _stub_chainflip(monkeypatch, _cf_status())
    args = build_parser().parse_args(["status", "3b" * 32])
    assert cli.cmd_status(args) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert "chainflip" in out.lower()
    assert "COMPLETED" in out
    assert "0.00410000 BTC" in out  # 8 decimals
    assert "421.500000 USDT" in out  # 6 decimals
    assert "0xrecipient" in out
    assert "0xpayout" in out
    assert "not observed" not in (out + captured.err).lower()


def test_status_says_a_chainflip_swap_is_still_in_flight(monkeypatch, capsys):
    """No payout leg yet must read as "not yet", not as a payout of nothing."""
    _stub_chainflip(
        monkeypatch, _cf_status(state="SWAPPING", output_amount=None, egress_txid="")
    )
    args = build_parser().parse_args(["status", "3b" * 32])
    assert cli.cmd_status(args) == 0
    out = capsys.readouterr().out
    assert "SWAPPING" in out
    assert "0.00000000 USDT" not in out
    assert "not paid out yet" in out.lower() or "pending" in out.lower()


def test_status_prints_base_units_for_an_asset_it_cannot_scale(monkeypatch, capsys):
    """Chainflip trades assets this wallet has no key for. Scaling one by a
    guessed number of decimals would misreport the amount by orders of
    magnitude; saying "base units" is honest."""
    _stub_chainflip(
        monkeypatch,
        _cf_status(dest_chain="Solana", dest_asset="SOL", output_amount=3_075_950_670),
    )
    args = build_parser().parse_args(["status", "3b" * 32])
    assert cli.cmd_status(args) == 0
    out = capsys.readouterr().out
    assert "3075950670" in out
    assert "base units" in out


def test_status_falls_through_to_thorchain_when_chainflip_never_saw_it(
    monkeypatch, capsys
):
    _stub_chainflip(monkeypatch, None)
    args = build_parser().parse_args(["status", "3b" * 32])
    assert cli.cmd_status(args) == 0
    captured = capsys.readouterr()
    assert "chainflip:" not in captured.out.lower()
    assert "not observed" in (captured.out + captured.err).lower()


def test_a_broken_chainflip_api_does_not_break_status(monkeypatch, capsys):
    """The probe is best-effort, exactly like the on-chain view: a dead endpoint
    must not cost the user the answer the other backends can still give."""
    import niquests

    _stub_chainflip(monkeypatch, niquests.exceptions.ConnectionError("down"))
    args = build_parser().parse_args(["status", "3b" * 32])
    assert cli.cmd_status(args) == 0
    captured = capsys.readouterr()
    assert "not observed" in (captured.out + captured.err).lower()
