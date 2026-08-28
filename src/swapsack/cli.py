"""Command-line interface for swapsack.

PYTHON_ARGCOMPLETE_OK — argcomplete's global completion hook only completes a
console script whose entry-point module carries this marker in its first 1024
bytes, so keep it near the top. (Completion also works without that hook, via
the files hatch_build.py ships; see README.)

Commands: init / add-hd / add-raw / list / address / balance / quote / swap /
send / status. Swaps and sends default to a dry run that builds + verifies +
prints without broadcasting; ``--confirm`` is required to actually send funds.

bitcoinlib-backed adapters are imported lazily inside handlers so simple
invocations (and argument-parsing tests) stay light.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import getpass
import json
import os
import sys
import time
from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

from swapsack.addresses import (
    is_evm_chain,
    parse_payment_uri,
    validate_destination_address,
)
from swapsack.cow import DEFAULT_COW_TOLERANCE_BPS
from swapsack.keystore import HdKey, Keystore
from swapsack.net import HTTP_ERRORS
from swapsack.swap import (
    DEFAULT_TOLERANCE_BPS,
    BroadcastError,
    SwapAborted,
    SwapRequest,
    execute_swap,
    prepare_swap,
)
from swapsack.thorchain import THORCHAIN_UNIT, asset_unit
from swapsack.verify import OP_RETURN_MAX_BYTES

# The finest base unit across all supported assets (CACAO's 1e10) — the
# parse-time floor for --amount; the per-asset floor lives in _base_units.
FINEST_UNIT = 10**10

try:
    from swapsack._version import __version__
except ImportError:  # not built yet (e.g. running from a fresh checkout)
    __version__ = "0+unknown"

DEFAULT_KEYSTORE = "~/.config/swapsack/keystore.json"
DEFAULT_CONFIG = "~/.config/swapsack/config.toml"
# UTXO fee target when nothing overrides it: the cheaper end of near-next-block
# inclusion (block 1 is the priciest tier; 2 usually matches it but never costs
# more). Deliberately fast — a 6-block target surprised an impatient user with a
# ~30-min-stuck swap. Override per-run with --fee-blocks, or set a personal
# default in config.toml ([fees] target_blocks). Higher = cheaper & slower.
DEFAULT_FEE_BLOCKS = 2
# Per-chain account/change derivation paths live on the adapter classes (see
# the UTXO registry below); the CLI reads them off the built adapter so it can
# never scan or send change on a path that drifts from the adapter's own.
BTC_RECEIVE_PATH = "m/84'/0'/0'/0/0"
ETH_MAX_FEE_WEI = 10**16  # 0.01 ETH sanity ceiling on inbound gas
ASSET = {
    "BTC": "BTC.BTC",
    "ETH": "ETH.ETH",
    "TRX": "TRON.TRX",
    "USDT-TRON": "TRON.USDT-TR7NHQJEKQXGTCI8Q8ZY4PL8OTSZGJLJ6T",
    "USDT-ETH": "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7",
    "USDC-ETH": "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48",
    "DASH": "DASH.DASH",  # Maya-only pool; hold/bal/send/sweep; see docs/dash.md
    "ZEC": "ZEC.ZEC",  # Maya-only; t-addr hold/bal/send/sweep; see docs/zcash.md
    # Destination-only (no source/hold yet): pay an external --dest address.
    "LTC": "LTC.LTC",
    "DOGE": "DOGE.DOGE",
    "BCH": "BCH.BCH",
    "ATOM": "GAIA.ATOM",  # Cosmos Hub; THORChain names the chain GAIA
    "XRP": "XRP.XRP",  # classic r… addresses only — no destination tag, see below
    "ADA": "ADA.ADA",  # Maya-only; unreachable from a UTXO source (memo length)
    "ETH-ARB": "ARB.ETH",  # Maya-only: native ETH on Arbitrum, not the ARB token
    # The same dollar, somewhere cheaper to use than ETH mainnet. Each chain
    # has its own USDC contract, so these are not interchangeable strings.
    "USDC-AVAX": "AVAX.USDC-0XB97EF9EF8734C71904D8002F8B6BC66DD9C48A6E",
    "USDC-ARB": "ARB.USDC-0XAF88D065E77C8CC2239327C5EDB3A432268E5831",  # Maya-only
    "CACAO": "MAYA.CACAO",  # Maya native asset; 1e10 decimals; see docs/cacao.md
    "RUNE": "THOR.RUNE",  # THORChain native asset (Cosmos MsgSend/MsgDeposit)
}

# Pool asset -> the wallet key that prices it ("ETH.USDC-0X…" -> "USDC-ETH"),
# so `balance` can value an LP position. Derived from ASSET rather than typed
# out again: a second table is a second thing to forget to update.
_POOL_ASSET_KEYS: dict[str, str] = {pool: key for key, pool in ASSET.items()}

# `balance --unit` choices. Named here (not imported at module scope) because
# argparse needs them while building the parser; pricefeed.unit_for does the
# actual resolution.
_UNIT_NAMES = ("EUR", "USD", "USDT", "USDC", "BTC", "ETH", "SATS")


# --- config helpers ---------------------------------------------------------


def _keystore_path(args: argparse.Namespace) -> Path:
    return Path(
        args.keystore or os.environ.get("SWAPSACK_KEYSTORE") or DEFAULT_KEYSTORE
    ).expanduser()


@functools.cache
def _config() -> dict:
    """Load the TOML config file once, or ``{}`` if it is absent or unreadable.

    Best-effort like the rest of the CLI's config: a missing or malformed file
    yields defaults rather than an error, so the wallet still works out of the
    box. Path is ``$SWAPSACK_CONFIG`` or ``~/.config/swapsack/config.toml``.

    A file that exists but cannot be parsed is *warned* about, though. Failing
    soft is right — a config typo should not stand between you and a spend —
    but failing silently would quietly revert settings the user believes are in
    force, and for ``[fees] target_blocks`` that means paying the faster,
    pricier default without being told. An absent file is the normal state and
    says nothing.
    """
    import tomllib

    path = Path(os.environ.get("SWAPSACK_CONFIG") or DEFAULT_CONFIG).expanduser()
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(
            f"warning: ignoring config {path}: {exc}; using defaults",
            file=sys.stderr,
        )
        return {}


def _fee_blocks(args: argparse.Namespace) -> int:
    """UTXO fee target in blocks: ``--fee-blocks`` > ``$SWAPSACK_FEE_BLOCKS`` >
    config ``[fees] target_blocks`` > :data:`DEFAULT_FEE_BLOCKS`.

    Lower targets a nearer block (faster, pricier). A non-positive or
    unparseable value anywhere falls through to the next source rather than
    building a zero-fee (unrelayable) tx — including a ``[fees]`` that is not a
    table at all, which is a config typo rather than a reason to crash.
    """
    fees = _config().get("fees")
    candidates = (
        getattr(args, "fee_blocks", None),
        os.environ.get("SWAPSACK_FEE_BLOCKS"),
        fees.get("target_blocks") if isinstance(fees, dict) else None,
    )
    for value in candidates:
        if value is None:
            continue
        try:
            blocks = int(value)
        except (TypeError, ValueError):
            continue
        if blocks > 0:
            return blocks
    return DEFAULT_FEE_BLOCKS


def _passphrase(*, confirm: bool = False) -> str:
    pw = os.environ.get("SWAPSACK_PASSPHRASE")
    # An *unset* variable means "ask me"; a set-but-empty one means the keystore
    # deliberately has no passphrase (a dedicated test/automation wallet), which
    # must not silently fall through to a prompt there is no terminal for.
    if pw is not None:
        return pw
    pw = getpass.getpass("Keystore passphrase: ")
    if confirm and getpass.getpass("Repeat passphrase: ") != pw:
        raise SystemExit("passphrases do not match")
    return pw


def _btc_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from swapsack.chains.btc import DEFAULT_ESPLORA, BtcAdapter

    url = args.esplora or os.environ.get("SWAPSACK_ESPLORA") or DEFAULT_ESPLORA
    return BtcAdapter(url, bip39_passphrase=passphrase)


def _eth_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from swapsack.chains.eth import DEFAULT_RPC, EthAdapter

    url = (
        getattr(args, "eth_rpc", None)
        or os.environ.get("SWAPSACK_ETH_RPC")
        or DEFAULT_RPC
    )
    return EthAdapter(url, bip39_passphrase=passphrase)


def _arb_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from swapsack.chains.arb import DEFAULT_ARB_RPC, ArbAdapter

    url = (
        getattr(args, "arb_rpc", None)
        or os.environ.get("SWAPSACK_ARB_RPC")
        or DEFAULT_ARB_RPC
    )
    return ArbAdapter(url, bip39_passphrase=passphrase)


# The spendable EVM chains. They dispatch identically (derive one address ->
# nonce+fees -> build -> gate -> broadcast) and differ only in their adapter, so
# this registry replaces what were `chain == "ETH"` branches in cmd_send,
# cmd_swap and _liquidity. BSC is deliberately absent: its adapter is
# address+balance only (nothing trades it), so it has no send/swap/LP path to
# dispatch to.
_EVM_ADAPTERS = {
    "ETH": _eth_adapter,
    "ARB": _arb_adapter,
}


def _tron_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from swapsack.chains.tron import DEFAULT_TRON_API, TronAdapter

    url = (
        getattr(args, "tron_api", None)
        or os.environ.get("SWAPSACK_TRON_API")
        or DEFAULT_TRON_API
    )
    return TronAdapter(url, bip39_passphrase=passphrase)


def _bsc_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from swapsack.chains.bsc import DEFAULT_BSC_RPC, BscAdapter

    url = (
        getattr(args, "bsc_rpc", None)
        or os.environ.get("SWAPSACK_BSC_RPC")
        or DEFAULT_BSC_RPC
    )
    return BscAdapter(url, bip39_passphrase=passphrase)


def _dash_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from swapsack.chains.dash import DEFAULT_DASH_API, DashAdapter

    url = (
        getattr(args, "dash_api", None)
        or os.environ.get("SWAPSACK_DASH_API")
        or DEFAULT_DASH_API
    )
    return DashAdapter(url, bip39_passphrase=passphrase)


def _zec_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from swapsack.chains.zcash import DEFAULT_ZEC_LWD, ZecAdapter

    url = (
        getattr(args, "zec_lwd", None)
        or os.environ.get("SWAPSACK_ZEC_LWD")
        or DEFAULT_ZEC_LWD
    )
    return ZecAdapter(url, bip39_passphrase=passphrase)


# The UTXO chains dispatch identically (scan -> build -> gate -> broadcast),
# differing only in their adapter; this one registry replaces what used to be
# parallel BTC/DASH/ZEC if/elif chains in cmd_swap, cmd_send and _liquidity.
# Account/change derivation paths ride on the adapter class, so nothing here
# restates them.
_UTXO_ADAPTERS = {
    "BTC": _btc_adapter,
    "DASH": _dash_adapter,
    "ZEC": _zec_adapter,
}


def _maya_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from swapsack.chains.maya import DEFAULT_MAYANODE, MayaAdapter

    url = (
        getattr(args, "maya_api", None)
        or os.environ.get("SWAPSACK_MAYA_API")
        or DEFAULT_MAYANODE
    )
    return MayaAdapter(url, bip39_passphrase=passphrase)


def _thor_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from swapsack.chains.thor import DEFAULT_THORNODE, ThorAdapter

    url = (
        getattr(args, "thornode", None)
        or os.environ.get("SWAPSACK_THORNODE")
        or DEFAULT_THORNODE
    )
    return ThorAdapter(url, bip39_passphrase=passphrase)


def _wallet_adapters(args: argparse.Namespace, passphrase: str = "") -> list:  # noqa: ANN201
    """Adapters whose balances `balance` reports — add a chain here and it scales."""
    return [
        _btc_adapter(args, passphrase),
        _eth_adapter(args, passphrase),
        _arb_adapter(args, passphrase),
        _tron_adapter(args, passphrase),
        _bsc_adapter(args, passphrase),
        _dash_adapter(args, passphrase),
        _zec_adapter(args, passphrase),
        _maya_adapter(args, passphrase),
        _thor_adapter(args, passphrase),
    ]


def _load_keystore(path: Path | str, passphrase: str) -> Keystore:
    """Load a keystore and surface the v1->v2 passphrase-strip warning.

    ``Keystore.load`` is deliberately silent (a library layer) and only records
    which HD keys lost a stored BIP-39 passphrase in the migration; the
    user-facing warning belongs here, at the CLI boundary. The strip itself is
    intentional (v1 never applied the passphrase, so funds sit at empty-
    passphrase addresses), but the next save erases the secret permanently.
    """
    keystore = Keystore.load(path, passphrase)
    if keystore.stripped_passphrase_labels:
        labels = ", ".join(keystore.stripped_passphrase_labels)
        _warn(
            f"dropping the stored BIP-39 passphrase from HD key(s) {labels}:",
            "this v1 keystore never applied it to derivation, so your funds sit "
            "at empty-passphrase addresses",
            "the next save upgrades to v2 and discards the passphrase "
            "permanently — note it down now if you need it elsewhere",
            "(re-add with `add-hd --bip39-passphrase` to actually use it)",
        )
    return keystore


def _load_mnemonic(args: argparse.Namespace) -> tuple[str, str]:
    """Return ``(mnemonic, bip39_passphrase)`` for the selected HD key.

    The BIP-39 passphrase is ``""`` when the key has none (and always ``""`` for
    a v1 keystore, where it was stripped on load — see keystore.ENVELOPE_VERSION).
    """
    keystore = _load_keystore(_keystore_path(args), _passphrase())
    for entry in keystore.entries:
        if isinstance(entry, HdKey) and (args.key is None or entry.label == args.key):
            passphrase = entry.passphrase.reveal() if entry.passphrase else ""
            return entry.mnemonic.reveal(), passphrase
    raise SystemExit("no matching HD key in keystore")


def _liquidity_client(args: argparse.Namespace):  # noqa: ANN202 (ThorchainClient)
    """The backend client for an LP op (thorchain or its fork maya)."""
    from swapsack.backends import get_backend

    return get_backend(getattr(args, "backend", "thorchain")).client


def _warn(header: str, *bullets: str) -> None:
    """Print a warning header followed by indented bullet lines (to stderr)."""
    print(header, file=sys.stderr)
    for bullet in bullets:
        print(f"  - {bullet}", file=sys.stderr)


# --- handlers ---------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    path = _keystore_path(args)
    if path.exists() and not args.force:
        print(f"{path} already exists; use --force to overwrite", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    Keystore().save(path, _passphrase(confirm=True))
    print(f"created empty keystore at {path}")
    return 0


def cmd_add_hd(args: argparse.Namespace) -> int:
    path = _keystore_path(args)
    pw = _passphrase()
    keystore = _load_keystore(path, pw)
    if args.generate:
        from swapsack.chains.btc import generate_mnemonic

        mnemonic = generate_mnemonic()
    else:
        mnemonic = args.mnemonic or getpass.getpass("BIP39 mnemonic: ")
    keystore.add_hd(args.label, mnemonic, passphrase=args.bip39_passphrase or None)
    keystore.save(path, pw)
    print(f"added HD key {args.label!r}")
    if args.generate:
        from swapsack.chains.btc import BtcAdapter

        print(
            "BTC receive address:",
            BtcAdapter(bip39_passphrase=args.bip39_passphrase or "").derive_address(
                mnemonic, BTC_RECEIVE_PATH
            ),
        )
        print(
            "the new seed is stored ENCRYPTED in the keystore; back up the keystore "
            "file + passphrase.\nto reveal the words (do it privately): "
            f"swapsack show-seed --key {args.label}"
        )
    return 0


def cmd_show_seed(args: argparse.Namespace) -> int:
    keystore = _load_keystore(_keystore_path(args), _passphrase())
    for entry in keystore.entries:
        if isinstance(entry, HdKey) and (args.key is None or entry.label == args.key):
            print(entry.mnemonic.reveal())
            if entry.passphrase is not None:
                # Back up the BIP-39 passphrase too — without it the words derive
                # a different (empty-passphrase) wallet.
                print(f"BIP39 passphrase: {entry.passphrase.reveal()}")
            return 0
    raise SystemExit("no matching HD key in keystore")


def cmd_add_raw(args: argparse.Namespace) -> int:
    path = _keystore_path(args)
    pw = _passphrase()
    keystore = _load_keystore(path, pw)
    secret = args.secret or getpass.getpass("private key: ")
    keystore.add_raw(args.label, args.chain, secret)
    keystore.save(path, pw)
    print(f"added raw {args.chain} key {args.label!r}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    keystore = _load_keystore(_keystore_path(args), _passphrase())
    for entry in keystore.entries:
        chain = getattr(entry, "chain", "")
        print(f"{entry.label}\t{entry.kind}\t{chain}")
    return 0


def cmd_address(args: argparse.Namespace) -> int:
    from swapsack.chains.arb import ArbAdapter
    from swapsack.chains.bsc import BscAdapter
    from swapsack.chains.btc import BtcAdapter
    from swapsack.chains.dash import DashAdapter
    from swapsack.chains.eth import EthAdapter
    from swapsack.chains.maya import MayaAdapter
    from swapsack.chains.thor import ThorAdapter
    from swapsack.chains.tron import TronAdapter
    from swapsack.chains.zcash import ZecAdapter

    mnemonic, passphrase = _load_mnemonic(args)
    print(
        "BTC: ",
        BtcAdapter(bip39_passphrase=passphrase).derive_address(
            mnemonic, BTC_RECEIVE_PATH
        ),
    )
    print("ETH: ", EthAdapter(bip39_passphrase=passphrase).derive_address(mnemonic))
    # Every EVM chain shares one derived address; printed per chain anyway so
    # `address` answers "where do I receive ARB/BSC funds" without the reader
    # having to know that.
    print(
        "ARB: ",
        ArbAdapter(bip39_passphrase=passphrase).derive_address(mnemonic),
        "(same EVM address as ETH)",
    )
    print(
        "BSC: ",
        BscAdapter(bip39_passphrase=passphrase).derive_address(mnemonic),
        "(same EVM address as ETH)",
    )
    print("TRON:", TronAdapter(bip39_passphrase=passphrase).derive_address(mnemonic))
    print("DASH:", DashAdapter(bip39_passphrase=passphrase).derive_address(mnemonic))
    print("ZEC: ", ZecAdapter(bip39_passphrase=passphrase).derive_address(mnemonic))
    print("MAYA:", MayaAdapter(bip39_passphrase=passphrase).derive_address(mnemonic))
    print("THOR:", ThorAdapter(bip39_passphrase=passphrase).derive_address(mnemonic))
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    """Collect every row first, then print one aligned (and priced) sheet.

    Rows used to print as they arrived, which is why they could not be aligned:
    the column widths — and the total — are only known once every chain has
    answered. The wait is the same; progress moves to stderr so the sheet on
    stdout stays a sheet.
    """
    from swapsack.backends import default_backends
    from swapsack.report import Row, balance_row, render

    print(
        "checking balances (the BTC address scan can take ~10s):",
        file=sys.stderr,
        flush=True,
    )
    mnemonic, passphrase = _load_mnemonic(args)
    backends = default_backends()
    # Derived once, then handed to every LP probe: a symmetric position is filed
    # under the protocol-chain address, not the asset address (see below).
    protocol_addresses = _protocol_lp_addresses(mnemonic, passphrase)
    rows: list = []
    try:
        for adapter in _wallet_adapters(args, passphrase):
            print(f" {adapter.chain}", end="", file=sys.stderr, flush=True)
            with adapter:
                try:
                    report = adapter.wallet_balance(mnemonic)
                except (
                    *HTTP_ERRORS,
                    RuntimeError,
                    KeyError,
                    ValueError,
                    IndexError,
                ) as exc:
                    print(
                        f"\n{adapter.chain}: balance unavailable ({exc})",
                        file=sys.stderr,
                    )
                    # Keep the chain on the sheet with an unknown amount rather
                    # than dropping it: stdout would otherwise carry a total
                    # computed as if it held nothing, and stderr is easy to
                    # lose to a redirect or scrollback.
                    rows.append(
                        Row(
                            label=adapter.chain,
                            amount=0.0,
                            note=f"balance unavailable ({exc})",
                            unavailable=True,
                        )
                    )
                    continue
                rows.append(balance_row(report))
                # Adapters flag where their pools live: () = pool-less (BSC: no
                # pools anywhere; CACAO: the settlement asset has no pool of
                # itself), a backend-name tuple = only there (DASH: Maya-only —
                # THORChain answers the probe with a 500, not a clean 404), and
                # None/absent = every backend. Probing a backend that cannot
                # host the pool is wasted round-trips at best and noise at worst.
                lp_backends = getattr(adapter, "lp_backends", None)
                if lp_backends != ():
                    probed = (
                        backends
                        if lp_backends is None
                        else [b for b in backends if b.name in lp_backends]
                    )
                    _report_liquidity(
                        probed,
                        adapter.asset,
                        report.addresses,
                        protocol_addresses,
                        rows,
                    )
                # A token's balance and that token's LP position are emitted
                # together, in that order. The sheet indents an LP line under
                # the row above it and keeps that row alive at zero so the
                # position never dangles — so emitting every balance and then
                # every position would file each position under the wrong
                # token, and protect the wrong row from collapsing.
                for token_row, pool_asset in _token_rows_with_pools(adapter, mnemonic):
                    if token_row is not None:
                        rows.append(token_row)
                    if lp_backends != ():
                        _report_liquidity(
                            probed,
                            pool_asset,
                            report.addresses,
                            protocol_addresses,
                            rows,
                        )
    finally:
        print(file=sys.stderr, flush=True)  # close the progress line
        for backend in backends:
            backend.client.close()
    unit, prices = _sheet_prices(args, rows)
    for line in render(
        rows, unit=unit, prices=prices, show_zeros=getattr(args, "zeros", False)
    ):
        print(line)
    return 0


def _sheet_prices(  # noqa: ANN202 (tuple[pricefeed.Unit | None, dict[str, float]])
    args: argparse.Namespace, rows: list
):
    """``(unit, {asset: price})`` for the sheet, or ``(None, {})`` when unpriced.

    One request for every asset on the sheet — which is also why
    ``--no-price-check`` has to suppress it rather than merely hide the column:
    a single lookup tells a third party the whole list of assets this IP holds.
    A feed that fails costs the user the value column and nothing else.
    """
    from swapsack.pricefeed import COINGECKO_IDS, PriceFeed, unit_for

    if not getattr(args, "price_check", True):
        return None, {}
    unit = unit_for(getattr(args, "unit", "EUR"))
    ids = {row.asset: COINGECKO_IDS.get(row.asset) for row in rows if row.asset}
    wanted = sorted({coin_id for coin_id in ids.values() if coin_id})
    if not wanted:
        return unit, {}
    try:
        with PriceFeed() as feed:
            spot = feed.spot(wanted, vs=(unit.vs,))
    # Deliberately broad, as everywhere the price feed is consulted: nothing it
    # can do may sink a command that has already fetched the balances.
    except (*HTTP_ERRORS, OSError, KeyError, ValueError, TypeError) as exc:
        print(f"price lookup failed ({exc}); showing amounts only", file=sys.stderr)
        return None, {}
    return unit, {
        asset: spot[coin_id][unit.vs]
        for asset, coin_id in ids.items()
        if coin_id in spot and unit.vs in spot[coin_id]
    }


def _token_pool_assets(adapter) -> list[str]:  # noqa: ANN001 (ChainAdapter)
    """THORChain/Maya pool-asset strings for the adapter's tracked ERC-20/TRC-20
    tokens (e.g. ``ETH.USDT-0X…``), so `balance` also probes *token* LP positions,
    not just the native pool. Empty for adapters that track no tokens.
    """
    return [
        f"{adapter.chain}.{symbol}-{contract.upper()}"
        for symbol, contract, _decimals in getattr(adapter, "tracked_tokens", ())
    ]


def _token_rows_with_pools(adapter, mnemonic: str) -> list[tuple[object, str]]:  # noqa: ANN001 (ChainAdapter)
    """``(balance row or None, pool asset)`` per tracked token, in table order.

    The two travel together so the caller can keep a token's LP row directly
    under that token's own balance row; see the comment at the call site for
    why the order matters to the rendered sheet.

    Token balances are separate network calls from the native balance, so a
    failure here is reported but does not sink the rest of the `balance` output
    — and crucially does not skip the *pool* probes, since a position can exist
    whether or not the balance RPC answered. ``token_balances`` builds its
    reports from the same ``tracked_tokens`` tuple in the same order, which is
    what makes the pairing positional; a short answer drops the pairing rather
    than risk filing a position under the wrong token.
    """
    from swapsack.report import balance_row

    pools = _token_pool_assets(adapter)
    if not pools:
        return []
    token_balances = getattr(adapter, "token_balances", None)
    reports: list = []
    if token_balances is not None:
        try:
            reports = token_balances(mnemonic)
        except (*HTTP_ERRORS, RuntimeError, KeyError, ValueError, IndexError) as exc:
            print(
                f"\n{adapter.chain}: token balances unavailable ({exc})",
                file=sys.stderr,
            )
    if len(reports) != len(pools):
        return [(None, pool) for pool in pools]
    return [
        (balance_row(report), pool) for report, pool in zip(reports, pools, strict=True)
    ]


def _protocol_lp_addresses(mnemonic: str, passphrase: str) -> dict[str, str]:
    """Backend name -> the wallet's address on that backend's *own* chain.

    ``maya1…``/``thor1…`` — the key a **symmetric** LP position is filed under.
    Derived from the two existing sources of truth (which backend settles which
    chain, and how that chain derives an address) so a new settlement chain
    cannot be added here without being added there.
    """
    from swapsack.backends import NATIVE_HOME_BACKEND

    return {
        backend: _DESTINATION_DERIVERS[chain](mnemonic, passphrase)
        for chain, backend in NATIVE_HOME_BACKEND.items()
        if chain in _DESTINATION_DERIVERS
    }


def _report_liquidity(
    backends: list,  # noqa: ANN401 (list[Backend]; lazy import avoids a cycle)
    asset: str,
    addresses: tuple[str, ...],
    protocol_addresses: dict[str, str] | None = None,
    rows: list | None = None,
) -> None:
    """Append a row for any LP position the wallet's addresses hold in ``asset``.

    Liquidity can sit on either backend, so every address is probed against all
    of them. A position is keyed by the L1 sender; for BTC that's not knowable
    ahead of time, so we probe every used address (most return nothing). The
    redeemable amount is shown as its own line, never folded into the spendable
    balance — an LP position isn't liquid and the figure is gross of exit fees.

    A **symmetric** position is not filed under the asset address at all: the
    protocol keys it by the RUNE/CACAO address that paired the two legs, and the
    asset address answers HTTP 200 with a zeros stub that collapses to "no
    position" — so the whole position was invisible until this probed
    ``protocol_addresses[backend.name]`` too (one extra lookup per pool per
    backend). Positions are de-duplicated by (pool, asset address, units), since
    "only one key answers" is the protocol's current behaviour, not a promise.
    """
    from swapsack.report import lp_row

    rows = [] if rows is None else rows
    asset_key = _POOL_ASSET_KEYS.get(asset, "")
    for backend in backends:
        protocol = "CACAO" if backend.name == "maya" else "RUNE"
        price: float | None = None  # asset per RUNE/CACAO; fetched once, lazily
        priced = False
        seen: set[tuple[str, str, int]] = set()
        probe = list(addresses)
        protocol_address = (protocol_addresses or {}).get(backend.name)
        if protocol_address is not None and protocol_address not in probe:
            probe.append(protocol_address)
        for address in probe:
            try:
                position = backend.client.liquidity_provider(asset, address)
            except HTTP_ERRORS as exc:
                print(
                    f"\n{backend.name} {asset}: LP lookup failed ({exc})",
                    file=sys.stderr,
                )
                break  # backend unreachable: don't hammer it for every address
            if position is None:
                continue
            key = (position.pool, position.asset_address, position.units)
            if key in seen:
                continue  # same position, answering on both of its keys
            seen.add(key)
            if not priced:  # only worth a pool fetch once we've found a position
                priced = True
                try:
                    price = backend.client.pool(asset).asset_per_protocol
                except HTTP_ERRORS:
                    price = None  # fall back to flagging the side as uncounted
            rows.append(
                lp_row(
                    position,
                    source=backend.name,
                    asset_key=asset_key,
                    protocol=protocol,
                    protocol_price_in_asset=price,
                )
            )


def _derivable_chain(to_: str) -> str:
    """The destination chain prefix (see DERIVABLE_CHAINS; others need --dest)."""
    return ASSET[to_].split(".", 1)[0]


def _derive_btc(mnemonic: str, passphrase: str) -> str:
    from swapsack.chains.btc import BtcAdapter

    return BtcAdapter(bip39_passphrase=passphrase).derive_address(
        mnemonic, BTC_RECEIVE_PATH
    )


def _derive_eth(mnemonic: str, passphrase: str) -> str:
    from swapsack.chains.eth import EthAdapter

    return EthAdapter(bip39_passphrase=passphrase).derive_address(mnemonic)


def _derive_tron(mnemonic: str, passphrase: str) -> str:
    from swapsack.chains.tron import TronAdapter

    return TronAdapter(bip39_passphrase=passphrase).derive_address(mnemonic)


def _derive_dash(mnemonic: str, passphrase: str) -> str:
    from swapsack.chains.dash import DashAdapter

    return DashAdapter(bip39_passphrase=passphrase).derive_address(mnemonic)


def _derive_zec(mnemonic: str, passphrase: str) -> str:
    from swapsack.chains.zcash import ZecAdapter

    return ZecAdapter(bip39_passphrase=passphrase).derive_address(mnemonic)


def _derive_arb(mnemonic: str, passphrase: str) -> str:
    from swapsack.chains.arb import ArbAdapter

    return ArbAdapter(bip39_passphrase=passphrase).derive_address(mnemonic)


def _derive_maya(mnemonic: str, passphrase: str) -> str:
    from swapsack.chains.maya import MayaAdapter

    return MayaAdapter(bip39_passphrase=passphrase).derive_address(mnemonic)


def _derive_thor(mnemonic: str, passphrase: str) -> str:
    from swapsack.chains.thor import ThorAdapter

    return ThorAdapter(bip39_passphrase=passphrase).derive_address(mnemonic)


# The one source of truth for "which destination chains can we derive from the
# seed": chain -> deriver. DERIVABLE_CHAINS (used by cmd_quote to decide whether
# to decrypt the keystore) is the key set, so the tuple and the derivation
# capability cannot drift apart.
_DESTINATION_DERIVERS: dict[str, Callable[[str, str], str]] = {
    "BTC": _derive_btc,
    "ETH": _derive_eth,
    # Arbitrum IS the ETH address (same m/44'/60' derivation) — derivable only
    # now that we can also spend there; before that it was --dest-only, since
    # auto-deriving a destination we cannot spend from just parks funds.
    "ARB": _derive_arb,
    "TRON": _derive_tron,
    "DASH": _derive_dash,
    "ZEC": _derive_zec,
    "MAYA": _derive_maya,
    "THOR": _derive_thor,
}
DERIVABLE_CHAINS = tuple(_DESTINATION_DERIVERS)
# Chains we can receive on but not spend from (Phase 1 in their design notes,
# mapped here) — auto-deriving a swap destination there parks the funds, so
# warn loudly. (DASH graduated: send/sweep landed with Phase 2.)
# Currently empty (DASH and ZEC both graduated with their send paths); the
# mechanism stays for the next Phase-1 chain.
RECEIVE_ONLY_CHAINS: dict[str, str] = {}


def _derive_destination_address(
    chain: str, mnemonic: str, passphrase: str = ""
) -> str | None:
    """Our receive address on ``chain``, or None when it needs an explicit --dest."""
    deriver = _DESTINATION_DERIVERS.get(chain)
    return deriver(mnemonic, passphrase) if deriver else None


def _dest_chain_caveats(args: argparse.Namespace, dest: str) -> None:
    """Warn or refuse on destination chains whose payout has a hard limitation.

    Both cases below are total-loss or never-works, and both are invisible in
    the quote: the backend simply returns no route (or pays an address that
    cannot be credited), which reads like a temporary problem rather than a
    permanent one.
    """
    chain = _derivable_chain(args.to_)
    if chain == "XRP":
        # THORChain rejects both an X-address and an 'address:tag' spelling, so
        # a swap payout can carry no destination tag at all. Exchange deposit
        # addresses almost always need one, and an untagged deposit is at best
        # a support ticket.
        _warn(
            "an XRP payout carries no destination tag:",
            "THORChain accepts only a classic r… address — X-addresses and "
            "'address:tag' are both rejected, so a tag cannot be sent",
            "do NOT use an exchange deposit address that requires a tag; "
            "such a deposit is usually unrecoverable",
            "pay a self-custodial XRP address instead",
        )
    if is_evm_chain(chain) and chain != "ETH":
        # Every EVM chain shares one 20-byte address space, so a `0x…` address
        # is silent about which chain it is for — and the payout is final. A
        # self-custodial address usually works on all of them (same key), but
        # an exchange deposit address is per-chain, and crediting is manual at
        # best when funds arrive over the wrong bridge.
        _warn(
            f"this pays out on {chain}, not Ethereum mainnet:",
            "every EVM chain uses the same address format, so the address "
            "itself cannot tell you which chain it belongs to",
            f"make sure the recipient accepts {args.to_} on {chain} — an "
            "exchange deposit address for Ethereum mainnet will not credit it",
        )
    from_chain = ASSET[args.from_].split(".", 1)[0]
    if from_chain not in _UTXO_ADAPTERS:
        return
    # A UTXO source carries its memo in an OP_RETURN, which the backend caps at
    # verify.OP_RETURN_MAX_BYTES. The backend builds the memo, so its exact
    # length is not knowable here — but the shortest one it could possibly
    # build is '=:<1-char asset>:<dest>', and if even that is over the cap the
    # swap can never be quoted, whatever the amount, backend or timing. Caught
    # here because the backend's own refusal arrives as "no quotes", which
    # reads as a missing pool rather than a permanent impossibility.
    shortest_memo = len("=:x:") + len(dest)
    if shortest_memo > OP_RETURN_MAX_BYTES:
        raise SystemExit(
            f"--dest: the {chain} address is {len(dest)} characters, so the "
            f"swap memo needs at least {shortest_memo} bytes — over the "
            f"{OP_RETURN_MAX_BYTES}-byte OP_RETURN limit a {from_chain} source "
            f"has to fit it in. Swap from an account-model chain (ETH sends the "
            f"memo as calldata, with no such limit) to reach {args.to_}."
        )


def _resolve_destination(
    args: argparse.Namespace, mnemonic: str | None, passphrase: str = ""
) -> str | None:
    if args.dest:
        problem = validate_destination_address(_derivable_chain(args.to_), args.dest)
        if problem:
            raise SystemExit(f"--dest: {problem}")
        # Pay the address itself, not the URI wrapper it arrived in.
        dest = parse_payment_uri(args.dest)[0]
        _dest_chain_caveats(args, dest)
        return dest
    if mnemonic is None:
        return None
    # The destination address depends on the target *chain*, so a token like
    # TRON.USDT lands at the same Tron address as native TRX, ETH.USDT at the
    # ETH address, etc. The BIP-39 passphrase must be applied here too, or an
    # auto-derived --dest would pay an address the user cannot spend.
    chain = _derivable_chain(args.to_)
    if chain in RECEIVE_ONLY_CHAINS:
        _warn(
            f"the derived {chain} destination is receive-only:",
            "this wallet cannot spend from it yet (no spend path implemented)",
            "funds stay recoverable by importing the seed into another wallet",
            f"see {RECEIVE_ONLY_CHAINS[chain]} — or pay an external --dest instead",
        )
    return _derive_destination_address(chain, mnemonic, passphrase)


def _backends_for(args: argparse.Namespace):  # noqa: ANN202 (list[SwapBackend], lazy import)
    from swapsack.backends import get_backend, swap_backends

    if args.backend == "auto":
        return swap_backends()
    return [get_backend(args.backend)]


def _streaming_kwargs(args: argparse.Namespace) -> dict[str, int | None]:
    """Streaming-swap quote kwargs from the parsed args (None when not requested)."""
    return {
        "streaming_interval": getattr(args, "stream_interval", None),
        "streaming_quantity": getattr(args, "stream_quantity", None),
    }


def _tolerance(
    args: argparse.Namespace, *, default: int = DEFAULT_TOLERANCE_BPS
) -> int:
    """The price tolerance: the flag, or ``default``.

    The flag defaults to None ("use the backend's default") because the right
    default differs per backend: 300 bps quote tolerance on THORChain/Maya, but
    only DEFAULT_COW_TOLERANCE_BPS on CoW, where the tolerance becomes the
    signed order's on-chain buyAmount floor (see _swap_via_cow).
    """
    tolerance = getattr(args, "tolerance_bps", None)
    return default if tolerance is None else tolerance


def _is_cow_order_uid(value: str) -> bool:
    """Whether ``value`` looks like a CoW order uid, not a chain txid.

    A uid is 56 bytes (order digest + owner address + validTo) hex-encoded
    with a ``0x`` prefix -> 112 hex chars. Every chain txid this wallet deals
    with is shorter (a bare 32-byte hash, or an EVM ``0x``-prefixed 32-byte
    hash), so length alone disambiguates cleanly.
    """
    if not value.lower().startswith("0x"):
        return False
    hex_part = value[2:]
    if len(hex_part) != 112:
        return False
    try:
        int(hex_part, 16)
    except ValueError:
        return False
    return True


# Which executors each source path can actually drive. A backend outside its
# caller's set can still *price* a swap (so it competes in `quote` and in
# `auto`'s comparison), but routing execution there would hand its quote to a
# deposit builder that cannot settle it — so selection refuses instead.
# Declared per path rather than globally because it genuinely differs: a signed
# CoW order needs an EVM source, and a Chainflip vault swap is a Bitcoin
# transaction, so neither is drivable from every `swap --from`.
MEMO_DEPOSIT_ONLY = frozenset({"memo-deposit"})
UTXO_EXECUTORS = frozenset({"memo-deposit", "vault-swap"})
EVM_EXECUTORS = frozenset({"memo-deposit", "signed-order"})
# What the parser's --backend list can reach at all, for the error message.
EXECUTABLE_EXECUTORS = UTXO_EXECUTORS | EVM_EXECUTORS


def _can_execute(
    backend,  # noqa: ANN001 (SwapBackend, lazy import)
    executors: frozenset[str] = EXECUTABLE_EXECUTORS,
    *,
    from_asset: str | None = None,
    to_asset: str | None = None,
) -> bool:
    """Whether this backend can *settle* the swap, not merely price it.

    The executor answers it for most backends. A backend may also narrow it per
    pair by defining ``can_execute``: Chainflip lists Tron and quotes it, but a
    vault swap encodes its destination into the payload the gate re-derives, and
    the gate has no base58check decoder — so that pair prices here and settles
    nowhere. Catching it now routes to a backend that can; catching it in the
    payload encoder is exit 1 for a swap THORChain would have done.
    """
    if getattr(backend, "executor", "") not in executors:
        return False
    narrower = getattr(backend, "can_execute", None)
    if narrower is None or from_asset is None or to_asset is None:
        return True
    return narrower(from_asset, to_asset)


def _select_backend(  # noqa: ANN202 (Backend, lazy import)
    args: argparse.Namespace,
    *,
    from_asset: str,
    to_asset: str,
    amount: int,
    destination: str | None,
    tolerance_bps: int | None = None,
    executors: frozenset[str] = EXECUTABLE_EXECUTORS,
):
    """Pick the backend (lowest price when --backend auto).

    ``tolerance_bps`` is threaded into the selection quotes so a swap the user
    enables by raising it isn't refused here at the default tolerance. The
    backends we don't return are closed before returning (the chosen one is
    closed by the caller's ``with backend.client``); a single explicit backend
    is returned unquoted and closed by the caller.

    Price-only backends (see :data:`EXECUTABLE_EXECUTORS`) are dropped here
    rather than in ``gather_quotes``: they are still worth quoting, and when one
    of them wins on price that is worth *saying* — a cheaper route exists, just
    not through this command yet.
    """
    from swapsack.backends import best_quote, gather_quotes

    backends = _backends_for(args)
    if len(backends) == 1:
        backend = backends[0]
        if not _can_execute(backend, executors):
            backend.client.close()
            raise SwapAborted(
                f"{backend.name} cannot execute a swap from {from_asset} "
                f"— use `quote --backend {backend.name}` for the price, and "
                f"another backend (or --backend auto) to swap"
            )
        if not _can_execute(
            backend, executors, from_asset=from_asset, to_asset=to_asset
        ):
            backend.client.close()
            raise SwapAborted(
                f"{backend.name} can quote {from_asset} -> {to_asset} but cannot "
                f"execute it — use `quote --backend {backend.name}` for the "
                f"price, and another backend (or --backend auto) to swap"
            )
        if not backend.serves(from_asset, to_asset):
            backend.client.close()
            raise SwapAborted(f"{backend.name} cannot serve {from_asset} -> {to_asset}")
        return backend
    results = gather_quotes(
        backends,
        from_asset,
        to_asset,
        amount,
        destination,
        tolerance_bps=tolerance_bps,
        **_streaming_kwargs(args),
    )
    executable = [
        pair
        for pair in results
        if _can_execute(pair[0], executors, from_asset=from_asset, to_asset=to_asset)
    ]
    if not executable:
        for unused in backends:
            unused.client.close()
        raise SwapAborted("no swap backend can serve this pair/amount")
    backend, quote = best_quote(executable)
    _note_unexecutable_best(
        results,
        chosen=quote,
        from_asset=from_asset,
        to_asset=to_asset,
        executors=executors,
    )
    if len(executable) > 1:
        print(
            f"routing via {backend.name} (best of {len(executable)})", file=sys.stderr
        )
    for unused in backends:
        if unused is not backend:
            unused.client.close()
    return backend


def _note_unexecutable_best(
    results,  # noqa: ANN001 (list[tuple[SwapBackend, quote]])
    *,
    chosen,  # noqa: ANN001 (any backend's quote)
    from_asset: str,
    to_asset: str,
    executors: frozenset[str],
) -> None:
    """Say so when a price-only backend beat the one we can actually execute.

    Silently routing around it would hide a real, reachable price — the user can
    take that route by hand today (docs/halt-alternatives.md), so name it and
    say by how much rather than quietly paying more.
    """
    unexecutable = [
        pair
        for pair in results
        if not _can_execute(
            pair[0], executors, from_asset=from_asset, to_asset=to_asset
        )
    ]
    if not unexecutable:
        return
    backend, quote = max(unexecutable, key=lambda pair: pair[1].expected_amount_out)
    if quote.expected_amount_out <= chosen.expected_amount_out:
        return
    unit = asset_unit(to_asset)
    better = (quote.expected_amount_out - chosen.expected_amount_out) / unit
    theirs = quote.expected_amount_out / unit
    ours = chosen.expected_amount_out / unit
    print(
        f"note: {backend.name} quoted {better:.8f} more ({theirs:.8f} vs "
        f"{ours:.8f}) but cannot execute yet; swapping via the best "
        f"backend that can",
        file=sys.stderr,
    )


def _market_comparison(
    from_key: str, to_key: str, amount_units: int, quoted_out_units: int
) -> list[str] | None:
    """Best-effort 'vs public spot' block, or None if unavailable/not mappable.

    Compares the quoted output against what an external mid-price swap would
    yield, surfacing the *total* realised cost (fees + slip + the pool-vs-market
    spread arbitrageurs earn). Returns up to three lines: a source header, the
    per-asset comparison, and (when the feed has a EUR price for the destination)
    the estimated absolute loss in EUR. Never raises: a feed failure drops it.
    """
    from swapsack.pricefeed import (
        COINGECKO_IDS,
        SOURCE,
        PriceFeed,
        loss_amount,
        loss_vs_market_bps,
        market_out,
    )

    id_from = COINGECKO_IDS.get(from_key)
    id_to = COINGECKO_IDS.get(to_key)
    if not id_from or not id_to:
        return None
    try:
        with PriceFeed() as feed:
            prices = feed.spot([id_from, id_to], vs=("usd", "eur"))
        market = market_out(
            amount_units / asset_unit(ASSET[from_key]),
            prices[id_from]["usd"],
            prices[id_to]["usd"],
        )
    except (*HTTP_ERRORS, KeyError, ValueError, ZeroDivisionError):
        return None
    quoted = quoted_out_units / asset_unit(ASSET[to_key])
    bps = loss_vs_market_bps(quoted, market)
    lines = [
        f"Market: ({SOURCE})",
        f"  ~{market:.8f} {to_key} at spot"
        f"  ->  ~{bps:.0f} bps total vs market (fees+slip+spread)",
    ]
    eur_out = prices.get(id_to, {}).get("eur")
    if eur_out:
        loss_eur = loss_amount(quoted, market) * eur_out
        if loss_eur >= 0:
            lines.append(f"  est. total loss ~€{loss_eur:.2f} (fees+slip+spread)")
        else:
            lines.append(
                f"  est. gain ~€{-loss_eur:.2f} vs market (pool priced in your favour)"
            )
    return lines


@functools.cache
def _eur_price(asset_key: str) -> float | None:
    """Best-effort EUR spot for one asset, or None. Cached for the process.

    Advisory only, exactly like :mod:`swapsack.pricefeed`'s market line: a fee
    is quoted and paid in the chain's own units, so an unreachable or wrong
    price must never change what is built, signed or broadcast — nor make a
    spend fail. Cached because a single command may price several fees, and a
    hot wallet should not stall on a courtesy lookup.
    """
    from swapsack.pricefeed import COINGECKO_IDS, PriceFeed

    coin_id = COINGECKO_IDS.get(asset_key)
    if not coin_id:
        return None
    try:
        with PriceFeed(timeout=5.0) as feed:
            prices = feed.spot([coin_id], vs=("eur",))
        price = prices[coin_id]["eur"]
    # Deliberately broad: this is a courtesy line on a money path, so *nothing*
    # from the feed (transport, DNS, a malformed body) may reach the caller.
    except (*HTTP_ERRORS, OSError, KeyError, ValueError, TypeError):
        return None
    return price if price > 0 else None


def _eur_suffix(amount_whole: float, asset_key: str, *, price_check: bool) -> str:
    """``" (~€1.23)"`` for a fee of ``amount_whole`` units, or ``""`` if unpriceable.

    ``asset_key`` is a wallet asset/chain key (``BTC``/``ETH``/``DASH``/…).
    Amounts that would round to nothing are shown as ``<€0.01`` rather than
    ``~€0.00``, which reads like "free".

    ``price_check`` is ``--price-check/--no-price-check``, and is required
    rather than defaulted: the lookup tells a third party that this IP is about
    to spend this asset, so opting out has to suppress the *request*, and a
    call site that forgets to pass the user's choice should fail loudly here
    rather than quietly leak.
    """
    if not price_check:
        return ""
    price = _eur_price(asset_key)
    if price is None:
        return ""
    eur = amount_whole * price
    if 0 < eur < 0.01:
        return " (<€0.01)"
    return f" (~€{eur:.2f})"


def _print_swap_costs(
    quote,  # noqa: ANN001
    from_key: str,
    to_key: str,
    amount_units: int,
    *,
    price_check: bool,
) -> None:
    """Print the itemised quoted-cost breakdown, plus an optional market line."""
    # A streaming swap: the network split the trade to cut slip. blocks == 0
    # means it decided no streaming was needed (small/low-slip trade), so the
    # line only appears when streaming is actually in effect.
    if getattr(quote, "streaming_swap_blocks", 0):
        mins = quote.total_swap_seconds / 60
        print(
            f"stream:  ~{quote.max_streaming_quantity} sub-swaps over "
            f"{quote.streaming_swap_blocks} blocks (~{mins:.0f} min) to cut slippage"
        )
    print("cost: (100 bps = 1%)")
    for line in quote.fees.breakdown(to_key):
        print(line)
    if price_check:
        market_lines = _market_comparison(
            from_key, to_key, amount_units, quote.expected_amount_out
        )
        for line in market_lines or ():
            print(line)


def cmd_quote(args: argparse.Namespace) -> int:
    if args.amount == "max":
        print("quote needs a numeric amount ('max' is only for swap)", file=sys.stderr)
        return 2
    from swapsack.backends import best_quote, gather_quotes

    # The quote API speaks the *source asset's* native unit (CACAO is 1e10).
    amount = _base_units(args.amount, asset_unit(ASSET[args.from_]))
    # Only decrypt the keystore if we actually need to derive the destination.
    if args.dest is None and _derivable_chain(args.to_) in DERIVABLE_CHAINS:
        mnemonic, passphrase = _load_mnemonic(args)
    else:
        mnemonic, passphrase = None, ""
    dest = _resolve_destination(args, mnemonic, passphrase)
    # A native RUNE/CACAO source deposits on its own network via MsgDeposit, so
    # only the home backend can serve it. Pin the quote to that backend (and
    # refuse an explicit foreign one) so the price shown matches the route the
    # swap command will actually execute — mirrors _swap_from_cosmos.
    from swapsack.backends import NATIVE_HOME_BACKEND, get_backend

    from_chain = ASSET[args.from_].split(".", 1)[0]
    if from_chain in NATIVE_HOME_BACKEND:
        home = NATIVE_HOME_BACKEND[from_chain]
        if args.backend not in ("auto", home):
            print(
                f"native {args.from_} deposits on {from_chain} itself; it can only "
                f"be quoted on the {home} backend (got --backend {args.backend})",
                file=sys.stderr,
            )
            return 2
        backends = [get_backend(home)]
    else:
        backends = _backends_for(args)
    try:
        results = gather_quotes(
            backends,
            ASSET[args.from_],
            ASSET[args.to_],
            amount,
            dest,
            **_streaming_kwargs(args),
        )
        if not results:
            print("no backend can serve this swap", file=sys.stderr)
            return 1
        chosen, chosen_quote = best_quote(results)
        print(f"in:     {args.amount} {args.from_}  ->  {args.to_}")
        to_unit = asset_unit(ASSET[args.to_])
        for backend, quote in sorted(results, key=lambda p: -p[1].expected_amount_out):
            out = quote.expected_amount_out / to_unit
            mark = "  <- best" if backend is chosen else ""
            print(f"  {backend.name:9} {out:.8f}  ({quote.fees.total_bps} bps){mark}")
        _print_swap_costs(
            chosen_quote, args.from_, args.to_, amount, price_check=args.price_check
        )
        return 0
    finally:
        for backend in backends:
            backend.client.close()


def cmd_swap(args: argparse.Namespace) -> int:
    chain = ASSET[args.from_].split(".", 1)[0]
    utxo = _UTXO_ADAPTERS.get(chain)  # BTC / DASH / ZEC (Maya-only pools route
    if utxo is not None:  # via _select_backend; ZEC uses the bespoke v4 signer)
        return _swap_from_utxo(args, utxo)
    evm = _EVM_ADAPTERS.get(chain)  # native coin and ERC-20 tokens
    if evm is not None:
        return _swap_from_evm(args, evm)
    if chain == "TRON":  # native TRX (TRC-20 tokens not yet a source)
        return _swap_from_tron(args)
    if chain == "MAYA":  # native CACAO (Cosmos MsgDeposit; Maya-only)
        return _swap_from_cosmos(args, _maya_adapter)
    if chain == "THOR":  # native RUNE (Cosmos MsgDeposit)
        return _swap_from_cosmos(args, _thor_adapter)
    print(f"swap source {args.from_} is not implemented yet", file=sys.stderr)
    return 2


def cmd_send(args: argparse.Namespace) -> int:
    chain = ASSET[args.asset].split(".", 1)[0]
    # Recipient sanity check once, before any keystore/network work — the
    # per-chain handlers each carried (or forgot) their own copy.
    problem = validate_destination_address(chain, args.address)
    if problem:
        print(f"recipient: {problem}", file=sys.stderr)
        return 2
    # Accept a BIP21-style payment URI (what wallets and QR codes hand out) and
    # spend to the address inside it. A URI amount that contradicts --amount is
    # refused rather than silently resolved either way: one of the two is what
    # the payee expects, and guessing wrong is irreversible.
    args.address, uri_params = parse_payment_uri(args.address)
    uri_amount = uri_params.get("amount")
    if uri_amount is not None and _amount_differs(uri_amount, args.amount):
        print(
            f"recipient: the payment URI asks for {uri_amount} {args.asset}, "
            f"but --amount is {args.amount}",
            file=sys.stderr,
        )
        return 2
    utxo = _UTXO_ADAPTERS.get(chain)  # BTC / DASH / ZEC (ZEC: v4/ZIP-243 signer)
    if utxo is not None:
        return _send_utxo(args, utxo)
    evm = _EVM_ADAPTERS.get(chain)  # native coin and ERC-20 tokens
    if evm is not None:
        return _send_evm(args, evm)
    if chain == "TRON":  # native TRX and TRC-20 tokens (USDT-TRON)
        return _send_tron(args)
    if chain == "MAYA":  # native CACAO (Cosmos MsgSend)
        return _send_cosmos(args, _maya_adapter)
    if chain == "THOR":  # native RUNE (Cosmos MsgSend)
        return _send_cosmos(args, _thor_adapter)
    print(f"send for {args.asset} is not implemented yet", file=sys.stderr)
    return 2


def _send_cosmos(args: argparse.Namespace, adapter_factory) -> int:  # noqa: ANN001
    """Plain native send for a THORChain-family asset (CACAO/RUNE)."""
    recipient = args.address
    mnemonic, passphrase = _load_mnemonic(args)
    with adapter_factory(args, passphrase) as adapter:
        if args.amount == "max":
            # The chain charges a fixed native tx fee separately from the sent
            # amount, so an exact drain-to-zero sweep isn't known at build time
            # (same reason native TRX has no sweep). Send a fixed amount instead.
            print(
                f"--amount max is not supported for native {adapter.symbol} send",
                file=sys.stderr,
            )
            return 2
        unit = 10**adapter.decimals
        amount = _base_units(args.amount, unit)
        prepared = adapter.build_and_verify_send(
            recipient=recipient, amount=amount, mnemonic=mnemonic
        )
        print(f"send:    {amount / unit:.8f} {adapter.symbol} to {recipient}")
        return _confirm_and_execute(prepared, adapter, args)


def _print_evm_fee_and_execute(
    prepared,  # noqa: ANN001 (swap.Prepared)
    adapter,  # noqa: ANN001 (EthAdapter or a subclass)
    args: argparse.Namespace,
) -> int:
    """Print an EVM tx's max fee in the chain's native coin, then confirm+execute.

    Every EVM chain here prices gas in an 18-decimal native coin; Arbitrum's is
    ether, the same asset as Ethereum's, so ``native_symbol`` is also the right
    key for the EUR lookup on both.
    """
    fee = prepared.built.fee / 10**18
    print(
        f"max fee: {fee:.6f} {adapter.native_symbol}"
        f"{_eur_suffix(fee, adapter.native_symbol, price_check=args.price_check)}"
    )
    return _confirm_and_execute(prepared, adapter, args)


def _send_evm(args: argparse.Namespace, adapter_factory) -> int:  # noqa: ANN001
    from swapsack.chains.coins import InsufficientFunds, token_sweep_amount
    from swapsack.chains.eth import eth_sweep_amount

    asset = ASSET[args.asset]
    recipient = args.address
    is_token = "-" in asset
    sweep = args.amount == "max"
    mnemonic, passphrase = _load_mnemonic(args)
    with adapter_factory(args, passphrase) as adapter:
        native = adapter.native_symbol
        from_address = adapter.derive_address(mnemonic)
        nonce = adapter.get_nonce(from_address)
        max_fee_per_gas, max_priority_fee_per_gas = adapter.fetch_fees()
        try:
            if sweep and is_token:
                token = asset.split("-", 1)[1]
                amount = token_sweep_amount(
                    adapter.fetch_token_balance(token, from_address),
                    adapter.token_decimals(token),
                )
            elif sweep:
                _warn(
                    f"sweeping native {native} on {adapter.chain} keeps only the "
                    f"reserve for THIS tx:",
                    f"you'll have ~no {native} left to pay gas for future token "
                    "transfers, swaps or LP moves",
                    f"consider sending a fixed amount and keeping some {native} "
                    "for gas",
                )
                amount = eth_sweep_amount(
                    adapter.fetch_balance(from_address),
                    # the adapter's budget, not Ethereum's: an L2 reserves more
                    gas=adapter.native_send_gas,
                    max_fee_per_gas=max_fee_per_gas,
                )
            else:
                amount = _base_units(args.amount)
        except InsufficientFunds as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        prepared = adapter.build_and_verify_send(
            recipient=recipient,
            amount=amount,
            asset=asset,
            mnemonic=mnemonic,
            nonce=nonce,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
            max_fee_wei=ETH_MAX_FEE_WEI,
        )
        print(f"send:    {amount / THORCHAIN_UNIT:.8f} {args.asset} to {recipient}")
        return _print_evm_fee_and_execute(prepared, adapter, args)


def _send_tron(args: argparse.Namespace) -> int:
    from swapsack.chains.coins import InsufficientFunds, token_sweep_amount

    asset = ASSET[args.asset]
    recipient = args.address
    is_token = "-" in asset
    sweep = args.amount == "max"
    if sweep and not is_token:
        # A native TRX sweep can't be exact — bandwidth/energy is charged
        # separately, not deducted from the sent amount (same as the TRX source).
        print("--amount max is not supported for native TRX send", file=sys.stderr)
        return 2
    mnemonic, passphrase = _load_mnemonic(args)
    with _tron_adapter(args, passphrase) as adapter:
        if sweep:
            contract, decimals = adapter.token_contract_and_decimals(asset)
            from_address = adapter.derive_address(mnemonic)
            try:
                amount = token_sweep_amount(
                    adapter.fetch_token_balance(contract, from_address), decimals
                )
            except InsufficientFunds as exc:
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1
        else:
            amount = _base_units(args.amount)
        if is_token:
            _warn(
                "TRC-20 send — the transfer burns TRX for energy (~15 TRX cap), "
                "separate from the tokens sent:",
                "keep spare TRX in the account",
            )
        try:
            prepared = adapter.build_and_verify_send(
                recipient=recipient, amount=amount, asset=asset, mnemonic=mnemonic
            )
        except ValueError as exc:
            # to_sun/to_token_native reject amounts finer than the chain's
            # precision (TRX is 1e6) — a clean abort, not a traceback (the
            # swap path catches the same pair).
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        print(f"send:    {amount / THORCHAIN_UNIT:.8f} {args.asset} to {recipient}")
        return _confirm_and_execute(prepared, adapter, args)


def _send_utxo(
    args: argparse.Namespace,
    adapter_factory,  # noqa: ANN001 (Callable[..., BtcAdapter | DashAdapter | ZecAdapter])
) -> int:
    """Plain send for a UTXO chain (BTC/DASH/ZEC): scan, select, gate, broadcast."""
    from swapsack.chains.coins import InsufficientFunds
    from swapsack.chains.scan import scan_account

    mnemonic, passphrase = _load_mnemonic(args)
    recipient = args.address
    sweep = args.amount == "max"
    with adapter_factory(args, passphrase) as adapter:
        records = scan_account(
            derive_address=lambda p: adapter.derive_address(mnemonic, p),
            probe=adapter.address_info,
            account=adapter.account,
        )
        utxos = [
            dataclasses.replace(u, path=path)
            for path, address, info in records
            if info.confirmed > 0
            for u in adapter.fetch_utxos(address)
        ]
        if not utxos:
            print("no confirmed UTXOs found for this wallet", file=sys.stderr)
            return 1

        change_address = adapter.derive_address(mnemonic, adapter.change_path)
        fee_rate = adapter.fetch_fee_rate(_fee_blocks(args))
        try:
            if sweep:
                total = sum(u.value for u in utxos)
                amount, _ = adapter.sweep_send_amount(total, len(utxos), fee_rate)
            else:
                amount = _base_units(args.amount)
            prepared = adapter.build_and_verify_send(
                recipient=recipient,
                amount=amount,
                now=int(time.time()),
                mnemonic=mnemonic,
                scanned_utxos=utxos,
                fee_rate=fee_rate,
                change_address=change_address,
                max_fee=args.max_fee,
                sweep=sweep,
            )
        except InsufficientFunds as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1

        label = adapter.chain.lower()
        print(f"send:    {amount} base units (1e-8 {adapter.chain}) to {recipient}")
        rate = f"@ {fee_rate}/vB" if fee_rate else "(ZIP-317 conventional fee)"
        eur = _eur_suffix(
            prepared.built.fee / THORCHAIN_UNIT,
            adapter.chain,
            price_check=args.price_check,
        )
        print(f"{label} fee: {prepared.built.fee} {rate}{eur}")
        return _confirm_and_execute(prepared, adapter, args)


def _confirm_and_execute(prepared, adapter, args: argparse.Namespace) -> int:  # noqa: ANN001
    if prepared.problems:
        print("VERIFY GATE FAILED — not safe to broadcast:", file=sys.stderr)
        for problem in prepared.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    if not args.confirm:
        print("\nDRY RUN — verified OK, not broadcast. Re-run with --confirm to send.")
        return 0
    # The summary the caller printed is freshly quoted THIS run, so confirm
    # against exactly what will be broadcast.
    if not args.yes:
        if input("\nBroadcast the swap shown above? type 'yes': ").strip() != "yes":
            print("aborted, not broadcast.")
            return 0
    expiry = getattr(prepared.plan, "expiry", None)
    if expiry is not None and time.time() >= expiry:
        print("ABORTED: quote expired while confirming; re-run.", file=sys.stderr)
        return 1
    try:
        result = execute_swap(prepared, adapter, confirm=True)
    except (BroadcastError, *HTTP_ERRORS) as exc:
        print(f"BROADCAST FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"\nBROADCAST txid: {result.txid}")
    print(f"track: swapsack status {result.txid}")
    return 0


def _swap_from_utxo(
    args: argparse.Namespace,
    adapter_factory,  # noqa: ANN001 (Callable[..., BtcAdapter | DashAdapter | ZecAdapter])
) -> int:
    """Swap from a UTXO chain (BTC/DASH/ZEC): scan, quote, build, gate, broadcast."""
    from swapsack.chains.coins import InsufficientFunds
    from swapsack.chains.scan import scan_account

    mnemonic, passphrase = _load_mnemonic(args)
    dest = _resolve_destination(args, mnemonic, passphrase)
    if dest is None:
        print("a --dest address is required for this destination", file=sys.stderr)
        return 2

    sweep = args.amount == "max"
    with adapter_factory(args, passphrase) as adapter:
        records = scan_account(
            derive_address=lambda p: adapter.derive_address(mnemonic, p),
            probe=adapter.address_info,
            account=adapter.account,
        )
        utxos = [
            dataclasses.replace(u, path=path)
            for path, address, info in records
            if info.confirmed > 0
            for u in adapter.fetch_utxos(address)
        ]
        if not utxos:
            print("no confirmed UTXOs found for this wallet", file=sys.stderr)
            return 1

        change_address = adapter.derive_address(mnemonic, adapter.change_path)
        fee_rate = adapter.fetch_fee_rate(_fee_blocks(args))
        if sweep:
            from swapsack.chains.coins import OP_RETURN_MAX_BYTES

            total = sum(u.value for u in utxos)
            try:
                # The memo isn't known until the quote; size for the maximum
                # so the fee is never underestimated.
                amount, _ = adapter.sweep_send_amount(
                    total, len(utxos), fee_rate, memo_len=OP_RETURN_MAX_BYTES
                )
            except InsufficientFunds as exc:
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1
        else:
            amount = _base_units(args.amount)

        request = SwapRequest(
            from_asset=adapter.asset,
            to_asset=ASSET[args.to_],
            amount=amount,
            destination=dest,
        )
        try:
            backend = _select_backend(
                args,
                from_asset=request.from_asset,
                to_asset=request.to_asset,
                amount=amount,
                destination=dest,
                tolerance_bps=_tolerance(args),
                executors=UTXO_EXECUTORS,
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        if backend.executor == "vault-swap":
            return _swap_via_chainflip(
                args,
                adapter,
                backend,
                request=request,
                dest=dest,
                mnemonic=mnemonic,
                utxos=utxos,
                fee_rate=fee_rate,
                change_address=change_address,
                sweep=sweep,
            )
        with backend.client as thor:
            try:
                prepared = prepare_swap(
                    thorchain=thor,
                    adapter=adapter,
                    request=request,
                    now=int(time.time()),
                    mnemonic=mnemonic,
                    tolerance_bps=_tolerance(args),
                    **_streaming_kwargs(args),
                    scanned_utxos=utxos,
                    fee_rate=fee_rate,
                    change_address=change_address,
                    max_fee=args.max_fee,
                    sweep=sweep,
                )
            except (SwapAborted, InsufficientFunds) as exc:
                # InsufficientFunds escapes select_coins inside build_and_verify
                # on a non-sweep swap; catch it here (not just in the sweep path)
                # so the user sees a clean ABORTED, not a traceback.
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1

            out = prepared.quote.expected_amount_out / asset_unit(ASSET[args.to_])
            print(f"via:     {backend.name}")
            print(
                f"send:    {amount} base units (1e-8 {adapter.chain}) to "
                f"{prepared.quote.inbound_address}"
            )
            print(f"expect:  {out:.8f} {args.to_} -> {dest}")
            print(f"memo:    {prepared.quote.memo}")
            _print_swap_costs(
                prepared.quote,
                args.from_,
                args.to_,
                amount,
                price_check=args.price_check,
            )
            rate = f"@ {fee_rate}/vB" if fee_rate else "(ZIP-317 conventional fee)"
            eur = _eur_suffix(
                prepared.built.fee / THORCHAIN_UNIT,
                adapter.chain,
                price_check=args.price_check,
            )
            print(f"inbound: {prepared.built.fee} on {adapter.chain} {rate}{eur}")
            return _confirm_and_execute(prepared, adapter, args)


def _swap_via_chainflip(
    args: argparse.Namespace,
    adapter,  # noqa: ANN001 (BtcAdapter)
    backend,  # noqa: ANN001 (ChainflipBackend)
    *,
    request: SwapRequest,
    dest: str,
    mnemonic: str,
    utxos: list,
    fee_rate: float,
    change_address: str,
    sweep: bool,
) -> int:
    """Chainflip's execute path: a vault swap, no broker and no memo protocol.

    The transaction is an ordinary Bitcoin payment to a protocol vault, with
    the swap's parameters — our destination, and a floor the protocol enforces
    — in its OP_RETURN. Nothing is registered anywhere on our behalf, so the
    gate can prove the whole intention from the bytes about to be published:
    see ``verify.verify_chainflip_vault_swap``.
    """
    from swapsack.chainflip import (
        CHAINFLIP_ASSETS,
        VAULT_SWAP_PLAN_TTL,
        ChainflipError,
        ChainflipRpc,
        deposit_units,
        parse_chainflip_quote,
        prepare_vault_swap,
    )
    from swapsack.chains.coins import InsufficientFunds
    from swapsack.verify import ChainflipVaultPlan

    if sweep:
        # Chainflip reads the change output as the swap's refund address and
        # requires it above dust, so there is nothing to sweep into. Refusing
        # beats building a transaction the protocol will not accept.
        print(
            "ABORTED: --amount max cannot be a Chainflip vault swap — the "
            "protocol needs a change output above dust to refund to. Name an "
            "amount, or swap via another backend.",
            file=sys.stderr,
        )
        return 1

    to_key = args.to_
    bps = getattr(args, "tolerance_bps", None)
    src = CHAINFLIP_ASSETS[request.from_asset]
    dst = CHAINFLIP_ASSETS[request.to_asset]
    try:
        with backend.client as client:
            # Re-quote here rather than reuse selection's: the floor encoded
            # into the payload has to come from the price we are about to
            # commit to, not from one taken a few round trips ago.
            # In the source asset's *native* units, as the API speaks — the
            # request carries the wallet-wide 1e8 ones, and the two coincide
            # only because BTC has eight decimals.
            parsed = parse_chainflip_quote(
                client.quote(src[:2], dst[:2], deposit_units(request.amount, src[2])),
                from_asset=request.from_asset,
                to_asset=request.to_asset,
            )
    except (ChainflipError, *HTTP_ERRORS) as exc:
        print(f"ABORTED: chainflip quote failed: {exc}", file=sys.stderr)
        return 1

    now = int(time.time())
    try:
        with ChainflipRpc() as rpc:
            vault_swap = prepare_vault_swap(
                rpc,
                from_asset=request.from_asset,
                to_asset=request.to_asset,
                destination=dest,
                quote=parsed,
                bps=bps,
            )
    except (ChainflipError, *HTTP_ERRORS) as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1

    plan = ChainflipVaultPlan(
        deposit_address=vault_swap.deposit_address,
        amount=request.amount,
        payload=vault_swap.payload,
        expiry=now + VAULT_SWAP_PLAN_TTL,
        destination_asset_id=vault_swap.destination_asset_id,
        destination_bytes=vault_swap.destination_bytes,
        min_output_amount=vault_swap.min_output_amount,
        known_vaults=vault_swap.known_vaults,
    )
    try:
        prepared = adapter.build_and_verify_vault_swap(
            plan=plan,
            now=now,
            mnemonic=mnemonic,
            scanned_utxos=utxos,
            fee_rate=fee_rate,
            change_address=change_address,
            max_fee=args.max_fee,
        )
    except InsufficientFunds as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1

    dest_unit = 10 ** dst[2]
    out = parsed.egress_amount / dest_unit
    floor = vault_swap.min_output_amount / dest_unit
    effective_bps = bps if bps is not None else parsed.recommended_slippage_bps
    print(f"via:     {backend.name} (vault swap — no broker, no memo protocol)")
    print(
        f"send:    {request.amount} base units (1e-8 {adapter.chain}) to "
        f"{plan.deposit_address}"
    )
    print(f"expect:  {out:.8f} {to_key} -> {dest}")
    print(
        f"floor:   {floor:.8f} {to_key} enforced on-chain "
        f"({effective_bps} bps tolerance); below it the swap refunds to "
        f"{change_address}"
    )
    print(f"eta:     ~{parsed.estimated_duration_seconds // 60} min")
    _print_swap_costs(
        parsed, args.from_, to_key, request.amount, price_check=args.price_check
    )
    eur = _eur_suffix(
        prepared.built.fee / THORCHAIN_UNIT, adapter.chain, price_check=args.price_check
    )
    print(f"inbound: {prepared.built.fee} on {adapter.chain} @ {fee_rate}/vB{eur}")
    print(
        "track:   this is not a THORChain swap; follow the deposit on "
        "scan.chainflip.io once it confirms"
    )
    return _confirm_and_execute(prepared, adapter, args)


def _swap_from_evm(args: argparse.Namespace, adapter_factory) -> int:  # noqa: ANN001
    from swapsack.chains.coins import (
        InsufficientFunds,
        token_sweep_amount,
    )
    from swapsack.chains.eth import eth_sweep_amount

    mnemonic, passphrase = _load_mnemonic(args)
    dest = _resolve_destination(args, mnemonic, passphrase)
    if dest is None:
        print("a --dest address is required for this destination", file=sys.stderr)
        return 2

    from_asset = ASSET[args.from_]
    is_token = "-" in from_asset
    sweep = args.amount == "max"
    if is_token:
        _warn(
            "token source — 2 transactions (approve + deposit/order):",
            "if the deposit/order fails after the approve, an exact-amount "
            "allowance to the router/relayer remains",
        )
    with adapter_factory(args, passphrase) as adapter:
        from_address = adapter.derive_address(mnemonic)
        nonce = adapter.get_nonce(from_address)
        max_fee_per_gas, max_priority_fee_per_gas = adapter.fetch_fees()
        if sweep and is_token:
            # A token sweep sends the whole balanceOf — gas is paid in the
            # native coin, not the token, so the amount is exact.
            token = from_asset.split("-", 1)[1]
            try:
                amount = token_sweep_amount(
                    adapter.fetch_token_balance(token, from_address),
                    adapter.token_decimals(token),
                )
            except InsufficientFunds as exc:
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1
        elif sweep:
            try:
                amount = eth_sweep_amount(
                    adapter.fetch_balance(from_address),
                    gas=args.eth_gas,
                    max_fee_per_gas=max_fee_per_gas,
                )
            except InsufficientFunds as exc:
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1
        else:
            amount = _base_units(args.amount)
        request = SwapRequest(
            from_asset=from_asset,
            to_asset=ASSET[args.to_],
            amount=amount,
            destination=dest,
        )
        try:
            backend = _select_backend(
                args,
                from_asset=from_asset,
                to_asset=request.to_asset,
                amount=amount,
                destination=dest,
                tolerance_bps=_tolerance(args),
                executors=EVM_EXECUTORS,
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        if backend.executor == "signed-order":
            return _swap_via_cow(
                args,
                adapter,
                backend,
                from_asset=from_asset,
                to_asset=request.to_asset,
                amount=amount,
                dest=dest,
                mnemonic=mnemonic,
                from_address=from_address,
                nonce=nonce,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
            )
        with backend.client as thor:
            try:
                prepared = prepare_swap(
                    thorchain=thor,
                    adapter=adapter,
                    request=request,
                    now=int(time.time()),
                    mnemonic=mnemonic,
                    tolerance_bps=_tolerance(args),
                    **_streaming_kwargs(args),
                    nonce=nonce,
                    gas=args.eth_gas,
                    max_fee_per_gas=max_fee_per_gas,
                    max_priority_fee_per_gas=max_priority_fee_per_gas,
                    max_fee_wei=ETH_MAX_FEE_WEI,
                )
            except SwapAborted as exc:
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1

            amount_in = amount / THORCHAIN_UNIT
            out = prepared.quote.expected_amount_out / asset_unit(ASSET[args.to_])
            max_fee_eth = prepared.built.fee / 10**18
            vault = prepared.quote.inbound_address
            print(f"via:     {backend.name}")
            print(f"send:    {amount_in:.8f} {args.from_} to {vault}")
            print(f"expect:  {out:.8f} {args.to_} -> {dest}")
            print(f"memo:    {prepared.quote.memo}")
            _print_swap_costs(
                prepared.quote,
                args.from_,
                args.to_,
                amount,
                price_check=args.price_check,
            )
            native = adapter.native_symbol
            print(
                f"inbound: {max_fee_eth:.6f} {native} max "
                f"({len(prepared.built.txs)} tx)"
                f"{_eur_suffix(max_fee_eth, native, price_check=args.price_check)}"
            )
            return _confirm_and_execute(prepared, adapter, args)


def _swap_via_cow(
    args: argparse.Namespace,
    adapter,  # noqa: ANN001 (EthAdapter)
    backend,  # noqa: ANN001 (CowBackend)
    *,
    from_asset: str,
    to_asset: str,
    amount: int,
    dest: str,
    mnemonic: str,
    from_address: str,
    nonce: int,
    max_fee_per_gas: int,
    max_priority_fee_per_gas: int,
) -> int:
    """CoW's execute path: sign an EIP-712 order instead of paying a vault.

    Re-quotes with the *real* signer as ``from`` (backend selection quoted
    with ``dest`` doubling as both, matching the shared ``SwapBackend`` quote
    signature — fine for price comparison, but the order's counterparty must
    be the address that will actually sign it). Funds the CoW vault relayer's
    allowance first if short, then gates the order exactly like a memo-deposit
    swap gates its tx, before ever asking for a signature.
    """
    from swapsack.cow import (
        COW_ASSETS,
        VAULT_RELAYER,
        CowError,
        build_order,
        quote_pair,
        sell_units,
    )
    from swapsack.verify import CowOrderPlan, verify_cow_order

    if not backend.serves(from_asset, to_asset):
        print(
            f"ABORTED: cow cannot serve {args.from_} -> {args.to_} "
            "(same-chain ERC-20 sell, ERC-20/ETH buy only)",
            file=sys.stderr,
        )
        return 1

    sell_contract, sell_decimals = COW_ASSETS[from_asset]
    buy_contract, buy_decimals = COW_ASSETS[to_asset]
    # Scale the *requested* amount ourselves: the verify gate and the approval
    # below bind to this (not the quote's own total), so it must use the same
    # conversion quote_pair sends to the API — hence the shared sell_units.
    sell_amount = sell_units(amount, sell_decimals)
    with backend.client as client:
        try:
            quote = quote_pair(
                client,
                from_asset,
                to_asset,
                amount,
                from_address=from_address,
                receiver=dest,
            )
        except (CowError, *HTTP_ERRORS) as exc:
            print(f"ABORTED: cow quote failed: {exc}", file=sys.stderr)
            return 1

        order = build_order(
            quote, tolerance_bps=_tolerance(args, default=DEFAULT_COW_TOLERANCE_BPS)
        )
        plan = CowOrderPlan(
            sell_token=sell_contract,
            buy_token=buy_contract,
            receiver=dest,
            sell_amount=sell_amount,
            min_buy_amount=int(order["buyAmount"]),
            expiry=quote.valid_to,
        )
        now = int(time.time())
        problems = verify_cow_order(order=order, plan=plan, now=now)

        current_allowance = adapter.fetch_token_allowance(
            sell_contract, from_address, VAULT_RELAYER
        )
        approvals = adapter.build_and_verify_approvals(
            mnemonic=mnemonic,
            token=sell_contract,
            spender=VAULT_RELAYER,
            amount=sell_amount,
            current_allowance=current_allowance,
            nonce=nonce,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
            max_fee_wei=ETH_MAX_FEE_WEI,
        )
        problems = problems + approvals.problems

        sell_in = quote.sell_amount_total / 10**sell_decimals
        out = quote.expected_amount_out / asset_unit(to_asset)
        buy_floor = int(order["buyAmount"]) / 10**buy_decimals
        print(f"via:     {backend.name}")
        print(f"sell:    {sell_in:.6f} {args.from_} (signed order, no vault/memo)")
        print(f"expect:  {out:.8f} {args.to_} -> {dest}  (floor {buy_floor:.6f})")
        print(f"valid:   order usable until unix {order['validTo']}")
        _print_swap_costs(
            quote, args.from_, args.to_, amount, price_check=args.price_check
        )
        if approvals.built.txs:
            print(
                f"approve: {len(approvals.built.txs)} tx to fund the CoW vault "
                "relayer allowance"
            )

        if problems:
            print("VERIFY GATE FAILED — not safe to sign:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        if not args.confirm:
            print(
                "\nDRY RUN — verified OK, not signed/submitted. Re-run with "
                "--confirm to send."
            )
            return 0
        if not args.yes:
            prompt = "\nSign and submit the CoW order shown above? type 'yes': "
            if input(prompt).strip() != "yes":
                print("aborted, not submitted.")
                return 0
        if int(time.time()) >= order["validTo"]:
            print("ABORTED: order expired while confirming; re-run.", file=sys.stderr)
            return 1

        if approvals.built.txs:
            raws = adapter.sign(approvals.built)
            try:
                approval_txid = adapter.broadcast(raws)
            except BroadcastError as exc:
                print(f"BROADCAST FAILED (approval): {exc}", file=sys.stderr)
                return 1
            # The orderbook validates the allowance at placement, so it must be
            # mined before we submit — otherwise the order is rejected, gas is
            # spent and an exact-amount allowance dangles. Wait for the receipt.
            print("approve: broadcast; waiting for it to mine before submitting…")
            receipt = adapter.wait_for_receipt(approval_txid)
            if receipt is None:
                print(
                    "TIMED OUT waiting for the approval to mine. It is still "
                    "pending — once it confirms, re-run the same command: the "
                    "allowance will already be in place, so no new approval is "
                    "sent and the order goes straight out.",
                    file=sys.stderr,
                )
                return 1
            if receipt.get("status") == "0x0":
                print(
                    "ABORTED: the approval transaction reverted; allowance not "
                    "set, order not submitted.",
                    file=sys.stderr,
                )
                return 1

        signature = adapter.sign_cow_order(order, mnemonic)
        try:
            uid = client.submit_order(
                order,
                signature=signature,
                from_address=from_address,
                quote_id=quote.quote_id,
            )
        except (CowError, *HTTP_ERRORS) as exc:
            print(f"ORDER SUBMIT FAILED: {exc}", file=sys.stderr)
            return 1
        print(f"\nORDER submitted, uid: {uid}")
        print(f"track: swapsack status {uid}")
        return 0


def _swap_from_tron(args: argparse.Namespace) -> int:
    from swapsack.chains.coins import InsufficientFunds, token_sweep_amount

    is_token = "-" in ASSET[args.from_]
    sweep = args.amount == "max"
    if sweep and not is_token:
        # A native TRX sweep would need a TRX reserve for bandwidth/energy.
        print("--amount max is not supported for native TRX yet", file=sys.stderr)
        return 2
    if is_token:
        _warn(
            "TRC-20 source — the transfer burns TRX for energy (~15 TRX cap), "
            "separate from the USDT sent:",
            "keep spare TRX in the account, and note TRON deposits are routerless "
            "and unrefundable if the memo/vault is wrong (the verify gate checks both)",
        )

    mnemonic, passphrase = _load_mnemonic(args)
    dest = _resolve_destination(args, mnemonic, passphrase)
    if dest is None:
        print("a --dest address is required for this destination", file=sys.stderr)
        return 2

    with _tron_adapter(args, passphrase) as adapter:
        if sweep:
            # A token sweep sends the whole balance — energy is paid in TRX, not
            # the token, so the amount is exact.
            contract, decimals = adapter.token_contract_and_decimals(ASSET[args.from_])
            try:
                amount = token_sweep_amount(
                    adapter.fetch_token_balance(
                        contract, adapter.derive_address(mnemonic)
                    ),
                    decimals,
                )
            except InsufficientFunds as exc:
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1
        else:
            amount = _base_units(args.amount)
        request = SwapRequest(
            from_asset=ASSET[args.from_],
            to_asset=ASSET[args.to_],
            amount=amount,
            destination=dest,
        )
        try:
            backend = _select_backend(
                args,
                from_asset=request.from_asset,
                to_asset=request.to_asset,
                amount=amount,
                destination=dest,
                tolerance_bps=_tolerance(args),
                executors=MEMO_DEPOSIT_ONLY,
            )
            with backend.client as thor:
                prepared = prepare_swap(
                    thorchain=thor,
                    adapter=adapter,
                    request=request,
                    now=int(time.time()),
                    mnemonic=mnemonic,
                    tolerance_bps=_tolerance(args),
                    **_streaming_kwargs(args),
                )
        except (SwapAborted, ValueError) as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1

        out = prepared.quote.expected_amount_out / asset_unit(ASSET[args.to_])
        vault = prepared.quote.inbound_address
        print(f"via:     {backend.name}")
        if is_token:
            print(f"send:    {amount / THORCHAIN_UNIT:.6f} {args.from_} to {vault}")
        else:
            print(f"send:    {prepared.plan.amount_sun} sun to {vault}")
        print(f"expect:  {out:.8f} {args.to_} -> {dest}")
        print(f"memo:    {prepared.quote.memo}")
        _print_swap_costs(
            prepared.quote, args.from_, args.to_, amount, price_check=args.price_check
        )
        print("inbound: paid from spare TRX (bandwidth/energy), NOT the sent amount")
        print("         -> keep some TRX headroom below your balance")
        return _confirm_and_execute(prepared, adapter, args)


def _swap_from_cosmos(args: argparse.Namespace, adapter_factory) -> int:  # noqa: ANN001
    """Swap FROM a THORChain-family native asset (CACAO/RUNE) via MsgDeposit."""
    if args.amount == "max":
        # A native sweep can't be exact — the chain charges a fixed native fee
        # separately from the deposited amount (same as native TRX).
        print(
            f"--amount max is not supported for native {args.from_} yet",
            file=sys.stderr,
        )
        return 2
    mnemonic, passphrase = _load_mnemonic(args)
    dest = _resolve_destination(args, mnemonic, passphrase)
    if dest is None:
        print("a --dest address is required for this destination", file=sys.stderr)
        return 2

    with adapter_factory(args, passphrase) as adapter:
        from swapsack.backends import NATIVE_HOME_BACKEND, get_backend

        # A native source deposits on its own network via MsgDeposit, so only
        # the home network's backend can serve it — no price routing here, and
        # an explicit foreign --backend would send a foreign-priced memo.
        home = NATIVE_HOME_BACKEND[adapter.chain]
        if args.backend not in ("auto", home):
            print(
                f"ABORTED: native {adapter.symbol} swaps deposit on "
                f"{adapter.chain} itself; only the {home} backend can serve "
                f"them (got --backend {args.backend})",
                file=sys.stderr,
            )
            return 1
        # CACAO is 1e10, RUNE is 1e8; the quote API speaks the asset's native
        # unit, so the scaled amount goes through as-is.
        unit = 10**adapter.decimals
        amount = _base_units(args.amount, unit)
        request = SwapRequest(
            from_asset=ASSET[args.from_],
            to_asset=ASSET[args.to_],
            amount=amount,
            destination=dest,
        )
        try:
            backend = get_backend(home)
            with backend.client as thor:
                prepared = prepare_swap(
                    thorchain=thor,
                    adapter=adapter,
                    request=request,
                    now=int(time.time()),
                    mnemonic=mnemonic,
                    tolerance_bps=_tolerance(args),
                    **_streaming_kwargs(args),
                )
        except (SwapAborted, ValueError) as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1

        out = prepared.quote.expected_amount_out / asset_unit(ASSET[args.to_])
        print(f"via:     {backend.name}")
        print(f"deposit: {amount / unit:.8f} {adapter.symbol} (MsgDeposit, no vault)")
        print(f"expect:  {out:.8f} {args.to_} -> {dest}")
        print(f"memo:    {prepared.quote.memo}")
        _print_swap_costs(
            prepared.quote, args.from_, args.to_, amount, price_check=args.price_check
        )
        print(
            f"inbound: {adapter.chain} charges a fixed native {adapter.symbol} tx fee, "
            "separate from"
        )
        print(
            f"         the deposit -> keep a little {adapter.symbol} headroom below "
            "your balance"
        )
        return _confirm_and_execute(prepared, adapter, args)


def cmd_add_liquidity(args: argparse.Namespace) -> int:
    from swapsack.liquidity import add_liquidity_memo

    pool = ASSET[args.asset]
    if getattr(args, "symmetric", False):
        return _liquidity_symmetric(args, pool)
    sweep = args.amount == "max"
    amount = None if sweep else _base_units(args.amount)
    return _liquidity(args, memo=add_liquidity_memo(pool), amount=amount, sweep=sweep)


def cmd_withdraw_liquidity(args: argparse.Namespace) -> int:
    from swapsack.liquidity import withdraw_liquidity_memo

    pool = ASSET[args.asset]
    memo = withdraw_liquidity_memo(pool, args.bps)
    # Before any keystore or network work, and before the symmetric lookup in
    # particular: asking a backend that does not run this pool would fail as
    # "cannot tell what kind of position this is" rather than as the wrong
    # backend, which is what it actually is.
    factory = _lp_asset_factory(pool.split(".", 1)[0])
    if factory is not None and _lp_backend_refused(args, factory(args)):
        return 2
    # Loaded once and threaded through: routing has to derive the maya1/thor1
    # address to find out which kind of position this is, and the withdraw
    # itself needs the seed again — but the user gets one passphrase prompt.
    credentials = _load_mnemonic(args)
    try:
        position = _protocol_side_position(args, pool, credentials)
    except SwapAborted as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1
    if position is not None:
        return _withdraw_from_protocol_side(args, memo, credentials, position)
    return _liquidity(args, memo=memo, amount=None, credentials=credentials)


def _protocol_side_position(  # noqa: ANN202 (thorchain.LiquidityPosition | None)
    args: argparse.Namespace, pool: str, credentials: tuple[str, str]
):
    """The LP record filed under our RUNE/CACAO address, or ``None``.

    That is where a **symmetric** position lives, and *only* there — the asset
    address answers a zeros stub for it (the same lookup that once hid it from
    `balance`). A single-sided position is the other way round, so ``None``
    means "withdraw from the asset chain as before".

    A failed lookup raises rather than answering ``None``: falling back to the
    asset-chain trigger on a symmetric position spends a transaction that
    cannot match anything, and the user would read the silence as a completed
    exit.
    """
    mnemonic, passphrase = credentials
    with _protocol_adapter(args, passphrase) as protocol, _liquidity_client(args) as be:
        try:
            return be.liquidity_provider(pool, protocol.derive_address(mnemonic))
        except HTTP_ERRORS as exc:
            raise SwapAborted(
                f"cannot tell whether this is a symmetric position "
                f"({args.backend} LP lookup failed: {exc}); refusing to send a "
                f"withdraw trigger that may not match it"
            ) from exc


def _withdraw_from_protocol_side(
    args: argparse.Namespace,
    memo: str,
    credentials: tuple[str, str],
    position,  # noqa: ANN001 (thorchain.LiquidityPosition)
) -> int:
    """Withdraw a symmetric position by triggering from the RUNE/CACAO side.

    A native ``MsgDeposit`` carrying dust and the ordinary ``-:POOL:<bps>``
    memo. Both sides come back proportionally — the asset to the asset address
    the protocol has on file, the RUNE/CACAO to the address that signs this.
    """
    from swapsack.liquidity import WITHDRAW_TRIGGER_AMOUNT

    _warn_liquidity_risks()
    mnemonic, passphrase = credentials
    with _protocol_adapter(args, passphrase) as protocol:
        address = protocol.derive_address(mnemonic)
        prepared = protocol.build_and_verify_native_deposit(
            memo=memo,
            amount=WITHDRAW_TRIGGER_AMOUNT,
            mnemonic=mnemonic,
            now=int(time.time()),
        )
        print(
            f"withdraw: {args.bps / 100:.2f}% of a SYMMETRIC {position.pool} "
            f"position ({protocol.symbol}-side, units {position.units})"
        )
        print(
            f"trigger:  dust {protocol.symbol} MsgDeposit on {protocol.chain} "
            f"from {address}"
        )
        print(f"memo:     {memo}")
        print(
            f"returns:  the asset side to {position.asset_address}, "
            f"the {protocol.symbol} side to {address}"
        )
        print(f"max fee:  {protocol.chain}'s fixed native tx fee")
        return _confirm_and_execute(prepared, protocol, args)


def _warn_liquidity_risks() -> None:
    """The risks every LP op carries, single-sided or symmetric."""
    _warn(
        "only add liquidity that you can afford to lose, risks include:",
        "experimental feature - bugs may cause lost funds",
        "you're exposed to RUNE/CACAO volatility",
        "volatility may cause arbitrageurs to eat your funds",
        "for small amounts, the networking fees will probably outsize any win",
    )


def _liquidity(
    args: argparse.Namespace,
    *,
    memo: str,
    amount: int | None,
    sweep: bool = False,
    credentials: tuple[str, str] | None = None,
) -> int:
    """Dispatch an LP add/withdraw to the asset chain's own path.

    ``credentials`` is an already-loaded ``(mnemonic, bip39_passphrase)``, so a
    caller that had to open the keystore to *route* the command (see
    `cmd_withdraw_liquidity`) does not make the user type the passphrase twice.
    """
    _warn_liquidity_risks()
    asset = ASSET[args.asset]
    chain = asset.split(".", 1)[0]
    if "-" in asset and chain not in _EVM_ADAPTERS:
        # Only ERC-20 LP on a spendable EVM chain is wired (via that chain's
        # Maya router). USDT-TRON has no Maya pool; there's nowhere to provide
        # it. The guard reads the registry rather than naming ETH so that
        # adding an EVM chain does not silently leave its token pools refused.
        print(f"token liquidity is only supported for EVM tokens, not {args.asset}")
        return 2
    # Refuse a backend that can't host the pool up front, before any keystore or
    # network work — uniformly for every chain that has an adapter. This was
    # once wired only into the DASH/ZEC branches, so an lp_backends restriction
    # on any other adapter was silently ignored. The throwaway adapter does no
    # I/O at construction; it only carries the class attrs.
    factory = _lp_asset_factory(chain)
    if factory is not None and _lp_backend_refused(args, factory(args)):
        return 2
    utxo = _UTXO_ADAPTERS.get(chain)
    if utxo is not None:
        return _liquidity_utxo(
            args,
            utxo,
            memo=memo,
            amount=amount,
            sweep=sweep,
            credentials=credentials,
        )
    evm = _EVM_ADAPTERS.get(chain)
    if evm is not None:
        return _liquidity_evm(
            args,
            evm,
            memo=memo,
            amount=amount,
            sweep=sweep,
            credentials=credentials,
        )
    if chain == "TRON":
        return _liquidity_tron(
            args, memo=memo, amount=amount, sweep=sweep, credentials=credentials
        )
    print(f"liquidity on {chain} is not implemented", file=sys.stderr)
    return 2


def _lp_asset_factory(chain: str):  # noqa: ANN202 (Callable[..., ChainAdapter] | None)
    """The adapter factory an LP op on ``chain`` builds through, or ``None``.

    One lookup for both callers, so the "which chains can hold liquidity" answer
    cannot differ between routing a withdraw and executing one. TRON is absent
    deliberately: it has its own path and no `lp_backends` restriction to check.
    """
    return _UTXO_ADAPTERS.get(chain) or _EVM_ADAPTERS.get(chain)


def _lp_backend_refused(args: argparse.Namespace, adapter: object) -> bool:
    """Refuse an LP request against a backend that cannot host the pool.

    Checked up front, before any keystore/network work: LP has no 'auto'
    routing (it's a choice of network/pairing), so the user must name a
    backend the chain's pools exist on (the adapter's ``lp_backends``).
    ``adapter`` may be a class or an instance — only class attributes are read.
    """
    allowed = getattr(adapter, "lp_backends", None)
    if allowed is None or args.backend in allowed:
        return False
    if not allowed:
        # () means pool-less everywhere (BSC; CACAO, the settlement asset).
        # There is no backend to suggest, and allowed[0] would IndexError.
        print(
            f"{adapter.chain} has no liquidity pools on any backend",
            file=sys.stderr,
        )
        return True
    print(
        f"{adapter.chain} liquidity exists only on {'/'.join(allowed)} — "
        f"re-run with --backend {allowed[0]}",
        file=sys.stderr,
    )
    return True


def _liquidity_utxo(
    args: argparse.Namespace,
    adapter_factory,  # noqa: ANN001 (Callable[..., BtcAdapter | DashAdapter | ZecAdapter])
    *,
    memo: str,
    amount: int | None,
    sweep: bool = False,
    credentials: tuple[str, str] | None = None,
) -> int:
    from swapsack.chains.coins import InsufficientFunds
    from swapsack.chains.scan import scan_account
    from swapsack.swap import prepare_liquidity

    mnemonic, passphrase = credentials or _load_mnemonic(args)
    with adapter_factory(args, passphrase) as adapter, _liquidity_client(args) as thor:
        records = scan_account(
            derive_address=lambda p: adapter.derive_address(mnemonic, p),
            probe=adapter.address_info,
            account=adapter.account,
        )
        utxos = [
            dataclasses.replace(u, path=path)
            for path, address, info in records
            if info.confirmed > 0
            for u in adapter.fetch_utxos(address)
        ]
        if not utxos:
            print(
                f"no confirmed {adapter.chain} (add needs funds; withdraw needs "
                f"a little {adapter.chain} in-wallet for the trigger tx)",
                file=sys.stderr,
            )
            return 1
        change_address = adapter.derive_address(mnemonic, adapter.change_path)
        fee_rate = adapter.fetch_fee_rate(_fee_blocks(args))
        if sweep:
            total = sum(u.value for u in utxos)
            try:
                amount, _ = adapter.sweep_send_amount(
                    total, len(utxos), fee_rate, memo_len=len(memo.encode())
                )
            except InsufficientFunds as exc:
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1
        try:
            prepared = prepare_liquidity(
                thorchain=thor,
                adapter=adapter,
                memo=memo,
                amount=amount,
                now=int(time.time()),
                mnemonic=mnemonic,
                scanned_utxos=utxos,
                fee_rate=fee_rate,
                change_address=change_address,
                max_fee=args.max_fee,
                sweep=sweep,
            )
        except (SwapAborted, InsufficientFunds) as exc:
            # Non-sweep LP: InsufficientFunds escapes select_coins inside
            # build_and_verify_deposit; catch it here so the user sees ABORTED,
            # not a raw traceback.
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        vault = prepared.plan.inbound_address
        print(
            f"send:    {prepared.plan.amount} base units (1e-8 {adapter.chain}) "
            f"to {vault}"
        )
        print(f"memo:    {memo}")
        label = adapter.chain.lower()
        rate = f"@ {fee_rate}/vB" if fee_rate else "(ZIP-317 conventional fee)"
        eur = _eur_suffix(
            prepared.built.fee / THORCHAIN_UNIT,
            adapter.chain,
            price_check=args.price_check,
        )
        print(f"{label} fee: {prepared.built.fee} {rate}{eur}")
        return _confirm_and_execute(prepared, adapter, args)


def _eth_lp_build_kwargs(
    args: argparse.Namespace,
    adapter,  # noqa: ANN001 (EthAdapter)
    thor,  # noqa: ANN001 (ThorchainClient)
    *,
    from_address: str,
    asset: str,
    token_add: bool,
) -> tuple[dict[str, object], int]:
    """The ``build_and_verify_deposit`` kwargs for an ETH-chain LP leg.

    Shared by the single-sided and symmetric paths so the router lookup, the
    token-decimals lookup and the gas/fee plumbing exist once. Returns the
    kwargs and the deposited asset's decimals (18 for native ETH).
    """
    max_fee_per_gas, max_priority_fee_per_gas = adapter.fetch_fees()
    kwargs: dict[str, object] = {
        "nonce": adapter.get_nonce(from_address),
        "gas": args.eth_gas,
        "max_fee_per_gas": max_fee_per_gas,
        "max_priority_fee_per_gas": max_priority_fee_per_gas,
        "max_fee_wei": ETH_MAX_FEE_WEI,
    }
    if not token_add:
        return kwargs, 18
    token = asset.split("-", 1)[1]
    status = thor.inbound_addresses().get(adapter.chain)
    if not status or not status.router:
        raise SwapAborted(
            f"no {adapter.chain} router on this backend — token LP needs it"
        )
    kwargs["router"] = status.router
    # The adapter takes the contract explicitly; it must not parse it out of
    # the memo (a symmetric add memo has a suffix after the pool).
    kwargs["token"] = token
    _warn(
        "token liquidity add — 2 transactions (approve + deposit):",
        "gas is paid in ETH, separate from the tokens deposited",
        "if the deposit fails after approve, a router allowance remains",
    )
    return kwargs, adapter.token_decimals(token)


def _liquidity_evm(
    args: argparse.Namespace,
    adapter_factory,  # noqa: ANN001 (Callable[..., EthAdapter | ArbAdapter])
    *,
    memo: str,
    amount: int | None,
    sweep: bool = False,
    credentials: tuple[str, str] | None = None,
) -> int:
    from swapsack.chains.coins import InsufficientFunds, token_sweep_amount
    from swapsack.chains.eth import eth_sweep_amount
    from swapsack.swap import prepare_liquidity

    asset = ASSET[args.asset]
    # A token *add* (approve + router deposit) is the only token op that needs
    # the router; a token *withdraw* is a native-coin dust trigger, handled
    # natively.
    token_add = memo.startswith("+") and "-" in asset
    mnemonic, passphrase = credentials or _load_mnemonic(args)
    with adapter_factory(args, passphrase) as adapter, _liquidity_client(args) as thor:
        from_address = adapter.derive_address(mnemonic)
        try:
            build_extra, decimals = _eth_lp_build_kwargs(
                args,
                adapter,
                thor,
                from_address=from_address,
                asset=asset,
                token_add=token_add,
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        try:
            if sweep and token_add:
                token = asset.split("-", 1)[1]
                amount = token_sweep_amount(
                    adapter.fetch_token_balance(token, from_address), decimals
                )
            elif sweep:
                amount = eth_sweep_amount(
                    adapter.fetch_balance(from_address),
                    gas=args.eth_gas,
                    max_fee_per_gas=build_extra["max_fee_per_gas"],
                )
        except InsufficientFunds as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        try:
            prepared = prepare_liquidity(
                thorchain=thor,
                adapter=adapter,
                memo=memo,
                amount=amount,
                now=int(time.time()),
                mnemonic=mnemonic,
                **build_extra,
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        if token_add:
            built = prepared.built
            print(
                f"send:    {built.native_amount / 10**decimals:.6f} {args.asset} "
                f"via router {built.router}"
            )
            print(f"vault:   {built.vault}")
        else:
            native_amt = prepared.plan.amount_wei / 10**18
            print(
                f"send:    {native_amt:.8f} {adapter.native_symbol} to "
                f"{prepared.plan.inbound_address}"
            )
        print(f"memo:    {memo}")
        return _print_evm_fee_and_execute(prepared, adapter, args)


# A symmetric add is offered only for account-model asset chains. The protocol
# pairs the two legs by matching each memo's referenced address against the other
# leg's *observed sender*, and only an account-model chain has one unambiguously:
# a multi-input UTXO tx does not, and the vin[0] convention is an assumption no
# testnet can verify for us. See docs/liquidity-symmetric.md.
_SYMMETRIC_ASSET_CHAINS = ("ETH", "ARB")


def _protocol_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    """The RUNE/CACAO adapter for the LP backend — the symmetric add's other leg.

    Resolved through the module globals at call time (not a lookup table built
    at import) so the factories stay monkeypatchable in tests.
    """
    factory = _maya_adapter if args.backend == "maya" else _thor_adapter
    return factory(args, passphrase)


def _liquidity_symmetric(args: argparse.Namespace, pool: str) -> int:
    """Add liquidity to both sides of ``pool`` at once — two linked deposits."""
    from swapsack.swap import prepare_symmetric_liquidity

    chain = pool.split(".", 1)[0]
    # The same up-front refusal _liquidity makes, before any keystore or network
    # work. --symmetric has its own dispatch out of cmd_add_liquidity and so
    # skipped that guard entirely, reaching the backend's missing router only
    # after the risk warnings had printed and the passphrase had been asked for.
    factory = _lp_asset_factory(chain)
    if factory is not None and _lp_backend_refused(args, factory(args)):
        return 2
    if chain not in _SYMMETRIC_ASSET_CHAINS:
        print(
            f"--symmetric needs an account-model asset chain "
            f"({'/'.join(_SYMMETRIC_ASSET_CHAINS)}), not {chain}: the protocol "
            f"pairs the two legs by the asset leg's observed sender, and a UTXO "
            f"transaction has no single sender (the convention is vin[0], which "
            f"no testnet exists to verify). Use a single-sided add instead.",
            file=sys.stderr,
        )
        return 2
    if args.amount == "max":
        print(
            "--symmetric needs a definite --amount, not 'max': the RUNE/CACAO leg "
            "is computed from it at the current pool ratio, and a sweep would "
            "leave no gas for the asset leg either.",
            file=sys.stderr,
        )
        return 2
    _warn_liquidity_risks()
    _warn(
        "symmetric add — TWO irreversible transactions on two chains:",
        "they are prepared and verified together, and neither is broadcast "
        "unless both pass",
        "but if the second fails after the first is out, the position sits "
        "pending until the protocol refunds it",
        "symmetric avoids entry slip; it does NOT reduce your RUNE/CACAO "
        "exposure, which is ~50% of the position either way",
    )

    mnemonic, passphrase = _load_mnemonic(args)
    amount = _base_units(args.amount)
    token_add = "-" in pool
    asset_factory = _EVM_ADAPTERS[chain]
    with (
        asset_factory(args, passphrase) as adapter,
        _protocol_adapter(args, passphrase) as protocol,
        _liquidity_client(args) as thor,
    ):
        asset_address = adapter.derive_address(mnemonic)
        protocol_address = protocol.derive_address(mnemonic)
        try:
            build_extra, decimals = _eth_lp_build_kwargs(
                args,
                adapter,
                thor,
                from_address=asset_address,
                asset=pool,
                token_add=token_add,
            )
            prepared = prepare_symmetric_liquidity(
                thorchain=thor,
                asset_adapter=adapter,
                protocol_adapter=protocol,
                pool=pool,
                asset_amount=amount,
                asset_address=asset_address,
                protocol_address=protocol_address,
                mnemonic=mnemonic,
                now=int(time.time()),
                **build_extra,
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        _print_symmetric_legs(
            args,
            prepared,
            protocol,
            decimals=decimals,
            token_add=token_add,
            adapter=adapter,
        )
        return _confirm_and_execute_symmetric(prepared, adapter, protocol, args)


def _print_symmetric_legs(
    args: argparse.Namespace,
    prepared,  # noqa: ANN001 (swap.SymmetricPrepared)
    protocol,  # noqa: ANN001 (CosmosAdapter)
    *,
    decimals: int,
    token_add: bool,
    adapter,  # noqa: ANN001 (ChainAdapter — for its native fee symbol)
) -> None:
    built = prepared.asset.built
    # The two ETH deposit shapes differ and neither carries the other's fields:
    # a token add is a router call (its own plan, with native_amount/router/
    # vault), a native add is a plain vault payment whose amount lives on the
    # plan as wei. Same split as the single-sided path below.
    if token_add:
        print(
            f"asset leg:    {built.native_amount / 10**decimals:.6f} {args.asset} "
            f"via router {built.router} -> vault {built.vault}"
        )
    else:
        eth_amount = prepared.asset.plan.amount_wei / 10**18
        print(
            f"asset leg:    {eth_amount:.8f} {args.asset} -> vault "
            f"{prepared.asset.plan.inbound_address}"
        )
    print(f"  memo:       {prepared.asset_memo}")
    protocol_unit = 10**protocol.decimals
    print(
        f"protocol leg: {prepared.protocol_amount / protocol_unit:.8f} "
        f"{protocol.symbol} (MsgDeposit on {protocol.chain})"
    )
    print(f"  memo:       {prepared.protocol_memo}")
    # The asset chain's own coin, not a hardcoded "ETH": ETH and ARB both pay
    # ether, so this reads the same today and would misreport on a third EVM.
    native = adapter.native_symbol
    native_fee = prepared.asset.built.fee / 10**18
    print(
        f"max fee:      {native_fee:.6f} {native}"
        f"{_eur_suffix(native_fee, native, price_check=args.price_check)}"
        f" + {protocol.chain}'s fixed native tx fee"
    )


def _confirm_and_execute_symmetric(
    prepared,  # noqa: ANN001 (swap.SymmetricPrepared)
    asset_adapter,  # noqa: ANN001
    protocol_adapter,  # noqa: ANN001
    args: argparse.Namespace,
) -> int:
    from swapsack.swap import PartialSymmetricAdd, execute_symmetric_liquidity

    if prepared.problems:
        print("VERIFY GATE FAILED — not safe to broadcast:", file=sys.stderr)
        for problem in prepared.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    if not args.confirm:
        print(
            "\nDRY RUN — both legs verified OK, neither broadcast. "
            "Re-run with --confirm to send."
        )
        return 0
    if not args.yes:
        if input("\nBroadcast BOTH legs shown above? type 'yes': ").strip() != "yes":
            print("aborted, nothing broadcast.")
            return 0
    try:
        result = execute_symmetric_liquidity(
            prepared,
            asset_adapter=asset_adapter,
            protocol_adapter=protocol_adapter,
            confirm=True,
        )
    except PartialSymmetricAdd as exc:
        # Must precede the BroadcastError arm below — it is a subclass, and this
        # is the one outcome the user has to act on.
        _warn(
            "PARTIAL ADD — the position is pending, NOT complete:",
            f"the {protocol_adapter.symbol} leg IS ON-CHAIN, txid {exc.protocol_txid}",
            f"the {asset_adapter.chain} leg did NOT go out: {exc.cause}",
            "the protocol refunds an unpaired leg once its window expires; wait "
            "for that before re-running, or the refund and a retry will collide",
        )
        return 1
    except (BroadcastError, *HTTP_ERRORS) as exc:
        print(f"BROADCAST FAILED (nothing was sent): {exc}", file=sys.stderr)
        return 1
    print(f"\nBROADCAST {protocol_adapter.symbol} leg: {result.protocol_txid}")
    print(f"BROADCAST {asset_adapter.chain} leg: {result.asset_txid}")
    print("both legs are out; the protocol pairs them once it has observed both.")
    return 0


def _liquidity_tron(
    args: argparse.Namespace,
    *,
    memo: str,
    amount: int | None,
    sweep: bool = False,
    credentials: tuple[str, str] | None = None,
) -> int:
    from swapsack.swap import prepare_liquidity

    if sweep:
        print("--amount max is not supported for TRON liquidity yet", file=sys.stderr)
        return 2
    mnemonic, passphrase = credentials or _load_mnemonic(args)
    with _tron_adapter(args, passphrase) as adapter, _liquidity_client(args) as thor:
        try:
            prepared = prepare_liquidity(
                thorchain=thor,
                adapter=adapter,
                memo=memo,
                amount=amount,
                now=int(time.time()),
                mnemonic=mnemonic,
            )
        except (SwapAborted, ValueError) as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        vault = prepared.plan.inbound_address
        print(f"send:    {prepared.plan.amount_sun} sun to {vault}")
        print(f"memo:    {memo}")
        print("trx fee: paid from spare TRX/bandwidth, NOT the sent amount")
        print("         -> keep some TRX headroom below your balance (~1 TRX)")
        return _confirm_and_execute(prepared, adapter, args)


def cmd_status(args: argparse.Namespace) -> int:
    # A CoW order uid (56 bytes: digest + owner + validTo) looks nothing like a
    # chain txid, so it's auto-detected and routed to the orderbook directly —
    # there is no vault/inbound-observed concept for a signed order.
    if _is_cow_order_uid(args.txid):
        from swapsack.cow import CowError, default_cow_backend

        with default_cow_backend().client as client:
            try:
                status = client.order_status(args.txid)
            except (CowError, *HTTP_ERRORS) as exc:
                print(f"ORDER STATUS FAILED: {exc}", file=sys.stderr)
                return 1
        print(json.dumps(status, indent=2))
        return 0

    # A bare tx hash doesn't say which network observed it, and an inbound only
    # exists on the chain it was deposited to (a Maya LP is invisible to
    # thornode). With --backend auto we query every (thornode-style) backend
    # and report the one that actually observed it; an unknown hash just
    # yields a "not observed" body on each, so falling through to the last is
    # harmless. Deliberately thornode-only backends here (not _backends_for's
    # swap_backends()): CoW has no tx_status concept, only order_status above.
    from swapsack.backends import default_backends

    # A bare 64-hex hash could be a BTC (or DASH/ZEC) txid; show what the tx
    # itself did (inputs, outputs, change, fee) before, and independently of,
    # the swap-observation view — that is the useful part for a plain send.
    _print_onchain_tx(args)

    if args.backend == "auto":
        backends = default_backends()
    else:
        from swapsack.backends import get_backend

        backends = [get_backend(args.backend)]
    status: dict[str, object] = {}
    for backend in backends:
        try:
            with backend.client as thor:
                status = thor.tx_status(args.txid)
        except HTTP_ERRORS:
            continue
        observed = status.get("stages", {}).get("inbound_observed", {}).get("started")
        if observed:
            if len(backends) > 1:
                print(f"// observed on {backend.name}", file=sys.stderr)
            print(json.dumps(status, indent=2))
            return 0
    # Not observed on any backend. The bare "started": false body is the correct
    # answer but reads like a broken command, so say what it actually means —
    # the common case is a hash that was never a swap at all.
    names = "/".join(b.name for b in backends)
    print(json.dumps(status, indent=2))
    print(
        f"// not observed by {names}. Either it is not a swap inbound at all "
        "(a plain 'send' never is — only a deposit to a vault with a swap memo "
        "gets observed), or the vaults have not seen it yet (usually within a "
        "block or two of confirmation).",
        file=sys.stderr,
    )
    return 0


def _print_onchain_tx(args: argparse.Namespace) -> None:
    """Print what a BTC transaction actually did, if the hash is one. Best-effort.

    Queries Esplora directly (no keystore — a txid is public), so it works for a
    hash the wallet did not create. A miss (not a BTC tx / not yet propagated /
    Esplora down) prints nothing and never raises: the swap-stage view below is
    the fallback.
    """
    try:
        with _btc_adapter(args) as adapter:
            tx = adapter.fetch_tx(args.txid)
    except (*HTTP_ERRORS, ValueError):
        return
    if tx is None:
        return

    where = (
        f"confirmed in block {tx.block_height}"
        if tx.confirmed
        else "unconfirmed (in mempool)"
    )
    print(f"on-chain (BTC): {where}")
    print(f"  in:  {tx.total_in} sats over {len(tx.inputs)} input(s)")
    for o in tx.outputs:
        if o.op_return:
            print("  out: OP_RETURN (swap/LP memo)")
            continue
        print(f"  out: {o.value} sats -> {o.address}")
    fee_eur = _eur_suffix(tx.fee / THORCHAIN_UNIT, "BTC", price_check=args.price_check)
    print(f"  fee: {tx.fee} sats @ {tx.fee_rate:.2f} sats/vB ({tx.vsize} vB){fee_eur}")
    if not tx.has_op_return:
        print("  note: no OP_RETURN — a plain send, not a swap (no vault to track)")


# --- parser -----------------------------------------------------------------


def _amount_differs(uri_amount: str, requested: Decimal | str) -> bool:
    """True if a payment URI's ``amount=`` disagrees with the parsed ``--amount``.

    An unparseable URI amount is not treated as a disagreement — the URI is a
    hint from a third party, and ``--amount`` is what the user actually typed.
    ``max`` (sweep) never matches a fixed URI amount.
    """
    try:
        wanted = Decimal(uri_amount)
    except (ArithmeticError, ValueError):
        return False
    return wanted != requested if isinstance(requested, Decimal) else True


def _amount(value: str) -> Decimal | str:
    """Parse a swap amount: a positive number, or the literal 'max' to sweep.

    Returns a :class:`~decimal.Decimal` (never a binary ``float``) so the amount
    can be scaled to base units exactly — float64 holds only ~15-16 significant
    decimals, enough to mis-size a large swap by a base unit.

    Rejecting ``<= 0`` / nan / inf — and amounts smaller than one base unit
    (1e-8) — here means no handler has to re-check, a typo'd or zero amount fails
    fast at the CLI, and a positive amount that would round to **zero** base
    units can never reach a tx (which would burn a fee on a no-op send).
    """
    if value.lower() == "max":
        return "max"
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(
            f"amount must be a positive number or 'max', got {value!r}"
        ) from None
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError(
            f"amount must be a positive number or 'max', got {value!r}"
        )
    # The finest base unit any supported asset has is CACAO's 1e-10; the
    # per-asset floor is enforced in _base_units, where the unit is known.
    if amount * FINEST_UNIT < 1:
        raise argparse.ArgumentTypeError(
            f"amount {value!r} is below one base unit (1e-10); too small to send"
        )
    return amount


def _nonneg_int(value: str) -> int:
    """argparse type for a non-negative integer (streaming quantity: 0 = auto)."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected an integer, got {value!r}"
        ) from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n


def _pos_int(value: str) -> int:
    """argparse type for a positive integer (streaming interval: 0 is NOT "off" —
    it would request streaming handling, dropping the price tolerance, while the
    node returns a plain non-streaming quote with LIM=0)."""
    n = _nonneg_int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def _base_units(amount: Decimal, unit: int = THORCHAIN_UNIT) -> int:
    """Scale a human ``--amount`` (whole asset units) to integer base units.

    ``unit`` is the asset's base unit (THORChain's shared 1e8 by default; CACAO
    is 1e10). Decimal end-to-end: a large amount like ``93393106.59778857`` must
    not pick up a float rounding error and be signed/broadcast one base unit off.

    Raises :class:`SwapAborted` when the amount is below one whole base unit —
    checked on the *unrounded* product, so a sub-unit amount like 0.6 base units
    is rejected rather than ROUND_HALF_EVEN'd up to 1 and silently over-sent. A
    0-value (or over-sent) tx is money the user didn't ask to move; main() turns
    an escaped SwapAborted into the standard ABORTED message.
    """
    product = amount * unit
    if product < 1:
        raise SwapAborted(
            f"amount {amount} is below one base unit (1/{unit}); too small to send"
        )
    return int(product.to_integral_value(rounding=ROUND_HALF_EVEN))


def _add_price_check_args(sub: argparse.ArgumentParser) -> None:
    """The CoinGecko opt-out, shared by every command that prices anything.

    Both the swap 'vs market' comparison and the EUR fee estimate go out to the
    same public feed, so one flag governs both — and every command that can
    trigger either has to offer it, or the opt-out is a lie somewhere.
    """
    sub.add_argument(
        "--price-check",
        dest="price_check",
        action="store_true",
        default=True,
        help="consult a public spot price (CoinGecko) to show the quote vs "
        "market and fees in EUR; default on",
    )
    sub.add_argument(
        "--no-price-check",
        dest="price_check",
        action="store_false",
        help="make no external price request at all (no market comparison, no "
        "EUR fee estimate)",
    )


def _add_swap_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--from", dest="from_", default="BTC", choices=list(ASSET))
    sub.add_argument("--to", dest="to_", default="ETH", choices=list(ASSET))
    sub.add_argument(
        "--amount", type=_amount, required=True, help="amount of --from asset, or 'max'"
    )
    sub.add_argument("--dest", help="destination address (default: derived from seed)")
    sub.add_argument("--key", help="keystore HD key label (default: first)")
    sub.add_argument(
        "--backend",
        choices=["thorchain", "maya", "cow", "chainflip", "auto"],
        default="auto",
        help="swap backend (auto = lowest price across all; cow = same-chain "
        "ETH-token swaps via a signed intent order, no memo/vault; chainflip = "
        "an independent cross-chain venue, executed from BTC as a vault swap — "
        "no broker, no deposit channel; EVM destinations only, "
        "see docs/chainflip-effort.md)",
    )
    _add_price_check_args(sub)
    sub.add_argument(
        "--stream-interval",
        type=_pos_int,
        metavar="BLOCKS",
        help="streaming swap: blocks between sub-swaps (>=1). Splits the trade "
        "over time so each hits the pool smaller, sharply cutting slippage on "
        "large/thinly-pooled swaps — at the cost of a longer settlement (funds "
        "in-flight, exposed to price movement). Manages slippage itself, so it "
        "OVERRIDES --tolerance-bps (the memo limit is set to 0). See docs/streaming.md",
    )
    sub.add_argument(
        "--stream-quantity",
        type=_nonneg_int,
        metavar="N",
        help="streaming swap: number of sub-swaps (0/omit = let the network pick "
        "the count that minimises slippage). Only meaningful with --stream-interval",
    )


def _add_broadcast_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--key", help="keystore HD key label (default: first)")
    sub.add_argument("--confirm", action="store_true", help="actually broadcast")
    sub.add_argument(
        "--yes", action="store_true", help="skip the interactive confirm (automation)"
    )
    sub.add_argument("--max-fee", type=int, default=50_000, help="max BTC fee in sats")
    sub.add_argument("--eth-rpc", help="Ethereum JSON-RPC URL ($SWAPSACK_ETH_RPC)")
    sub.add_argument("--arb-rpc", help="Arbitrum JSON-RPC URL ($SWAPSACK_ARB_RPC)")
    sub.add_argument("--eth-gas", type=int, default=60000, help="ETH gas limit")
    sub.add_argument(
        "--fee-blocks",
        type=int,
        help="UTXO fee target in blocks (lower = faster & pricier); "
        "overrides config.toml [fees] target_blocks and $SWAPSACK_FEE_BLOCKS",
    )
    _add_price_check_args(sub)


def _add_liquidity_backend_arg(sub: argparse.ArgumentParser) -> None:
    # No 'auto': LP is not price-routed — it's a choice of which network (and
    # which pairing, RUNE vs Maya's CACAO) to hold the position on.
    sub.add_argument(
        "--backend",
        choices=["thorchain", "maya"],
        default="thorchain",
        help="network to LP on (maya pairs with CACAO and is the only one "
        "with a DASH pool; maya has no TRON pool)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swapsack",
        description="CLI multi-currency wallet with THORChain swaps",
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("--keystore", help="keystore path ($SWAPSACK_KEYSTORE)")
    parser.add_argument("--esplora", help="Esplora API base URL ($SWAPSACK_ESPLORA)")
    sub = parser.add_subparsers(dest="command")

    s = sub.add_parser("init", help="create an empty encrypted keystore")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("add-hd", help="add or generate a BIP39 mnemonic")
    s.add_argument("--label", required=True)
    src = s.add_mutually_exclusive_group()
    src.add_argument("--mnemonic", help="mnemonic (omit to be prompted)")
    src.add_argument("--generate", action="store_true", help="generate a fresh seed")
    s.add_argument("--bip39-passphrase")
    s.set_defaults(func=cmd_add_hd)

    s = sub.add_parser("add-raw", help="add a standalone private key")
    s.add_argument("--label", required=True)
    s.add_argument("--chain", required=True)
    s.add_argument("--secret", help="key (omit to be prompted)")
    s.set_defaults(func=cmd_add_raw)

    s = sub.add_parser("list", help="list keystore entries")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show-seed", help="reveal an HD mnemonic (run privately)")
    s.add_argument("--key")
    s.set_defaults(func=cmd_show_seed)

    s = sub.add_parser(
        "address", help="show derived BTC, ETH, BSC, TRON, MAYA and THOR addresses"
    )
    s.add_argument("--key")
    s.set_defaults(func=cmd_address)

    s = sub.add_parser("balance", help="show balances across supported chains")
    s.add_argument("--key")
    s.add_argument("--eth-rpc", help="Ethereum JSON-RPC URL ($SWAPSACK_ETH_RPC)")
    s.add_argument("--arb-rpc", help="Arbitrum JSON-RPC URL ($SWAPSACK_ARB_RPC)")
    s.add_argument("--tron-api", help="TRON API base URL ($SWAPSACK_TRON_API)")
    s.add_argument("--bsc-rpc", help="BSC JSON-RPC URL ($SWAPSACK_BSC_RPC)")
    s.add_argument("--dash-api", help="Dash Insight API URL ($SWAPSACK_DASH_API)")
    s.add_argument("--zec-lwd", help="Zcash lightwalletd host:port ($SWAPSACK_ZEC_LWD)")
    s.add_argument("--maya-api", help="MayaChain REST URL ($SWAPSACK_MAYA_API)")
    s.add_argument("--thornode", help="THORChain REST URL ($SWAPSACK_THORNODE)")
    s.add_argument(
        "--unit",
        type=str.upper,
        default="EUR",
        choices=list(_UNIT_NAMES),
        help="denominate the value column and the total in this unit "
        "(USDT/USDC price in USD — CoinGecko has no stablecoin rate); "
        "default EUR",
    )
    s.add_argument(
        "--zeros",
        action="store_true",
        help="show rows worth nothing instead of naming them on one line",
    )
    _add_price_check_args(s)
    s.set_defaults(func=cmd_balance)

    s = sub.add_parser("quote", help="show a THORChain swap quote")
    _add_swap_args(s)
    s.set_defaults(func=cmd_quote)

    s = sub.add_parser(
        "swap", help="build/verify (and with --confirm, broadcast) a swap"
    )
    _add_swap_args(s)
    s.add_argument("--confirm", action="store_true", help="actually broadcast")
    s.add_argument(
        "--yes", action="store_true", help="skip the interactive confirm (automation)"
    )
    s.add_argument("--max-fee", type=int, default=50_000, help="max BTC fee in sats")
    s.add_argument("--eth-rpc", help="Ethereum JSON-RPC URL ($SWAPSACK_ETH_RPC)")
    s.add_argument("--arb-rpc", help="Arbitrum JSON-RPC URL ($SWAPSACK_ARB_RPC)")
    s.add_argument(
        "--fee-blocks",
        type=int,
        help="UTXO fee target in blocks (lower = faster & pricier); "
        "overrides config.toml [fees] target_blocks and $SWAPSACK_FEE_BLOCKS",
    )
    s.add_argument(
        "--eth-gas", type=int, default=60000, help="gas limit for ETH deposit"
    )
    s.add_argument(
        "--tolerance-bps",
        type=int,
        default=None,
        help="max basis points of price tolerance; omit for the backend's own "
        f"default ({DEFAULT_TOLERANCE_BPS} thornode/maya, {DEFAULT_COW_TOLERANCE_BPS} "
        "cow — there it becomes the signed order's on-chain buyAmount floor). "
        "Raise it for small/high-fee swaps refused at the default. Ignored "
        "when --stream-interval is set (streaming manages slippage itself)",
    )
    s.set_defaults(func=cmd_swap)

    s = sub.add_parser("add-liquidity", help="EXPERIMENTAL: add liquidity to a pool")
    s.add_argument("--asset", required=True, choices=list(ASSET))
    s.add_argument(
        "--amount",
        type=_amount,
        required=True,
        help="amount of --asset, or 'max' to add the whole balance (BTC/ETH)",
    )
    s.add_argument(
        "--symmetric",
        action="store_true",
        help="EXPERIMENTAL: two-sided add — pairs --amount with the matching "
        "RUNE/CACAO from your own balance, in two linked txs. Takes no entry "
        "slip, but both legs must land or the position sits pending",
    )
    _add_liquidity_backend_arg(s)
    _add_broadcast_args(s)
    s.set_defaults(func=cmd_add_liquidity)

    s = sub.add_parser(
        "withdraw-liquidity", help="EXPERIMENTAL: withdraw liquidity from a pool"
    )
    s.add_argument("--asset", required=True, choices=list(ASSET))
    s.add_argument(
        "--bps", type=int, default=10000, help="basis points to withdraw (1..10000)"
    )
    _add_liquidity_backend_arg(s)
    _add_broadcast_args(s)
    s.set_defaults(func=cmd_withdraw_liquidity)

    s = sub.add_parser(
        "send",
        help="send to an external address (no swap); BTC/ETH/TRON/CACAO/RUNE",
    )
    s.add_argument("address", help="recipient address")
    s.add_argument(
        "--asset",
        default="BTC",
        choices=list(ASSET),
        help="asset to send (default BTC)",
    )
    s.add_argument(
        "--amount",
        type=_amount,
        required=True,
        help="amount to send, or 'max' to sweep",
    )
    s.add_argument("--key", help="keystore HD key label (default: first)")
    s.add_argument("--confirm", action="store_true", help="actually broadcast")
    s.add_argument(
        "--yes", action="store_true", help="skip the interactive confirm (automation)"
    )
    s.add_argument(
        "--max-fee",
        type=int,
        default=50_000,
        help="max UTXO-chain fee in base units (BTC sats / DASH duffs)",
    )
    s.add_argument(
        "--fee-blocks",
        type=int,
        help="UTXO fee target in blocks (lower = faster & pricier); "
        "overrides config.toml [fees] target_blocks and $SWAPSACK_FEE_BLOCKS",
    )
    s.add_argument("--eth-rpc", help="Ethereum JSON-RPC URL ($SWAPSACK_ETH_RPC)")
    s.add_argument("--arb-rpc", help="Arbitrum JSON-RPC URL ($SWAPSACK_ARB_RPC)")
    s.add_argument("--tron-api", help="TRON API base URL ($SWAPSACK_TRON_API)")
    s.add_argument("--dash-api", help="Dash Insight API URL ($SWAPSACK_DASH_API)")
    s.add_argument("--maya-api", help="MayaChain REST URL ($SWAPSACK_MAYA_API)")
    s.add_argument("--thornode", help="THORChain REST URL ($SWAPSACK_THORNODE)")
    _add_price_check_args(s)
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("status", help="track a swap by inbound txid")
    s.add_argument("txid")
    s.add_argument(
        "--backend",
        choices=["thorchain", "maya", "auto"],
        default="auto",
        help="network to query (auto = try all, report where observed)",
    )
    _add_price_check_args(s)
    s.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # Shell tab-completion. argcomplete sets _ARGCOMPLETE only when the completion
    # machinery invokes us, so gate the import on it: normal runs pay nothing, and
    # there's no optional-vs-required ambiguity (it's a declared dependency).
    # Enable with: eval "$(register-python-argcomplete swapsack)"
    if "_ARGCOMPLETE" in os.environ:
        import argcomplete

        argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except SwapAborted as exc:
        # Backstop for handlers with no local handler (e.g. _base_units raising
        # from cmd_quote): the standard ABORTED message, never a traceback.
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
