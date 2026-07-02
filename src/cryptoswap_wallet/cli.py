"""Command-line interface for cryptoswap_wallet.

Commands: init / add-hd / add-raw / list / address / balance / quote / swap /
send / status. Swaps and sends default to a dry run that builds + verifies +
prints without broadcasting; ``--confirm`` is required to actually send funds.

bitcoinlib-backed adapters are imported lazily inside handlers so simple
invocations (and argument-parsing tests) stay light.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import sys
import time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

from cryptoswap_wallet.addresses import validate_destination_address
from cryptoswap_wallet.keystore import HdKey, Keystore
from cryptoswap_wallet.net import HTTP_ERRORS
from cryptoswap_wallet.swap import (
    DEFAULT_TOLERANCE_BPS,
    BroadcastError,
    SwapAborted,
    SwapRequest,
    execute_swap,
    prepare_swap,
)
from cryptoswap_wallet.thorchain import THORCHAIN_UNIT

try:
    from cryptoswap_wallet._version import __version__
except ImportError:  # not built yet (e.g. running from a fresh checkout)
    __version__ = "0+unknown"

DEFAULT_KEYSTORE = "~/.config/cryptoswap-wallet/keystore.json"
BTC_ACCOUNT = "m/84'/0'/0'"
BTC_RECEIVE_PATH = "m/84'/0'/0'/0/0"
BTC_CHANGE_PATH = "m/84'/0'/0'/1/0"
ETH_MAX_FEE_WEI = 10**16  # 0.01 ETH sanity ceiling on inbound gas
ASSET = {
    "BTC": "BTC.BTC",
    "ETH": "ETH.ETH",
    "TRX": "TRON.TRX",
    "USDT-TRON": "TRON.USDT-TR7NHQJEKQXGTCI8Q8ZY4PL8OTSZGJLJ6T",
    "USDT-ETH": "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7",
    "USDC-ETH": "ETH.USDC-0XA0B86991C6218B36C1D19D4A2E9EB0CE3606EB48",
    # Destination-only (no source/hold yet): pay an external --dest address.
    "LTC": "LTC.LTC",
    "DOGE": "DOGE.DOGE",
    "BCH": "BCH.BCH",
    "DASH": "DASH.DASH",  # Maya-only pool; see docs/dash.md
}


# --- config helpers ---------------------------------------------------------


def _keystore_path(args: argparse.Namespace) -> Path:
    return Path(
        args.keystore
        or os.environ.get("CRYPTOSWAP_WALLET_KEYSTORE")
        or DEFAULT_KEYSTORE
    ).expanduser()


def _passphrase(*, confirm: bool = False) -> str:
    pw = os.environ.get("CRYPTOSWAP_WALLET_PASSPHRASE")
    if pw:
        return pw
    pw = getpass.getpass("Keystore passphrase: ")
    if confirm and getpass.getpass("Repeat passphrase: ") != pw:
        raise SystemExit("passphrases do not match")
    return pw


def _btc_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from cryptoswap_wallet.chains.btc import DEFAULT_ESPLORA, BtcAdapter

    url = args.esplora or os.environ.get("CRYPTOSWAP_WALLET_ESPLORA") or DEFAULT_ESPLORA
    return BtcAdapter(url, bip39_passphrase=passphrase)


def _eth_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from cryptoswap_wallet.chains.eth import DEFAULT_RPC, EthAdapter

    url = (
        getattr(args, "eth_rpc", None)
        or os.environ.get("CRYPTOSWAP_WALLET_ETH_RPC")
        or DEFAULT_RPC
    )
    return EthAdapter(url, bip39_passphrase=passphrase)


def _tron_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from cryptoswap_wallet.chains.tron import DEFAULT_TRON_API, TronAdapter

    url = (
        getattr(args, "tron_api", None)
        or os.environ.get("CRYPTOSWAP_WALLET_TRON_API")
        or DEFAULT_TRON_API
    )
    return TronAdapter(url, bip39_passphrase=passphrase)


def _bsc_adapter(args: argparse.Namespace, passphrase: str = ""):  # noqa: ANN202
    from cryptoswap_wallet.chains.bsc import DEFAULT_BSC_RPC, BscAdapter

    url = (
        getattr(args, "bsc_rpc", None)
        or os.environ.get("CRYPTOSWAP_WALLET_BSC_RPC")
        or DEFAULT_BSC_RPC
    )
    return BscAdapter(url, bip39_passphrase=passphrase)


def _wallet_adapters(args: argparse.Namespace, passphrase: str = "") -> list:  # noqa: ANN201
    """Adapters whose balances `balance` reports — add a chain here and it scales."""
    return [
        _btc_adapter(args, passphrase),
        _eth_adapter(args, passphrase),
        _tron_adapter(args, passphrase),
        _bsc_adapter(args, passphrase),
    ]


def _load_mnemonic(args: argparse.Namespace) -> tuple[str, str]:
    """Return ``(mnemonic, bip39_passphrase)`` for the selected HD key.

    The BIP-39 passphrase is ``""`` when the key has none (and always ``""`` for
    a v1 keystore, where it was stripped on load — see keystore.ENVELOPE_VERSION).
    """
    keystore = Keystore.load(_keystore_path(args), _passphrase())
    for entry in keystore.entries:
        if isinstance(entry, HdKey) and (args.key is None or entry.label == args.key):
            passphrase = entry.passphrase.reveal() if entry.passphrase else ""
            return entry.mnemonic.reveal(), passphrase
    raise SystemExit("no matching HD key in keystore")


def _liquidity_client(args: argparse.Namespace):  # noqa: ANN202 (ThorchainClient)
    """The backend client for an LP op (thorchain or its fork maya)."""
    from cryptoswap_wallet.backends import get_backend

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
    keystore = Keystore.load(path, pw)
    if args.generate:
        from cryptoswap_wallet.chains.btc import generate_mnemonic

        mnemonic = generate_mnemonic()
    else:
        mnemonic = args.mnemonic or getpass.getpass("BIP39 mnemonic: ")
    keystore.add_hd(args.label, mnemonic, passphrase=args.bip39_passphrase or None)
    keystore.save(path, pw)
    print(f"added HD key {args.label!r}")
    if args.generate:
        from cryptoswap_wallet.chains.btc import BtcAdapter

        print(
            "BTC receive address:",
            BtcAdapter(bip39_passphrase=args.bip39_passphrase or "").derive_address(
                mnemonic, BTC_RECEIVE_PATH
            ),
        )
        print(
            "the new seed is stored ENCRYPTED in the keystore; back up the keystore "
            "file + passphrase.\nto reveal the words (do it privately): "
            f"cryptoswap-wallet show-seed --key {args.label}"
        )
    return 0


def cmd_show_seed(args: argparse.Namespace) -> int:
    keystore = Keystore.load(_keystore_path(args), _passphrase())
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
    keystore = Keystore.load(path, pw)
    secret = args.secret or getpass.getpass("private key: ")
    keystore.add_raw(args.label, args.chain, secret)
    keystore.save(path, pw)
    print(f"added raw {args.chain} key {args.label!r}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    keystore = Keystore.load(_keystore_path(args), _passphrase())
    for entry in keystore.entries:
        chain = getattr(entry, "chain", "")
        print(f"{entry.label}\t{entry.kind}\t{chain}")
    return 0


def cmd_address(args: argparse.Namespace) -> int:
    from cryptoswap_wallet.chains.bsc import BscAdapter
    from cryptoswap_wallet.chains.btc import BtcAdapter
    from cryptoswap_wallet.chains.eth import EthAdapter
    from cryptoswap_wallet.chains.tron import TronAdapter

    mnemonic, passphrase = _load_mnemonic(args)
    print(
        "BTC: ",
        BtcAdapter(bip39_passphrase=passphrase).derive_address(
            mnemonic, BTC_RECEIVE_PATH
        ),
    )
    print("ETH: ", EthAdapter(bip39_passphrase=passphrase).derive_address(mnemonic))
    # BSC is EVM: the same derived address as ETH (and every other EVM chain).
    print(
        "BSC: ",
        BscAdapter(bip39_passphrase=passphrase).derive_address(mnemonic),
        "(same EVM address as ETH)",
    )
    print("TRON:", TronAdapter(bip39_passphrase=passphrase).derive_address(mnemonic))
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    print(
        "checking balances (the BTC address scan can take ~10s)...",
        file=sys.stderr,
        flush=True,
    )
    mnemonic, passphrase = _load_mnemonic(args)
    from cryptoswap_wallet.backends import default_backends

    backends = default_backends()
    try:
        for adapter in _wallet_adapters(args, passphrase):
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
                        f"{adapter.chain}: balance unavailable ({exc})", file=sys.stderr
                    )
                    continue
                print(report.format())
                _report_liquidity(backends, adapter.asset, report.addresses)
                _report_token_balances(adapter, mnemonic)
    finally:
        for backend in backends:
            backend.client.close()
    return 0


def _report_token_balances(adapter, mnemonic: str) -> None:  # noqa: ANN001 (ChainAdapter)
    """Print any ERC-20/TRC-20 token balances the adapter tracks (e.g. USDT).

    Token balances are separate network calls from the native balance, so a
    failure here is reported but does not sink the rest of the `balance` output.
    """
    token_balances = getattr(adapter, "token_balances", None)
    if token_balances is None:
        return
    try:
        reports = token_balances(mnemonic)
    except (*HTTP_ERRORS, RuntimeError, KeyError, ValueError, IndexError) as exc:
        print(f"{adapter.chain}: token balances unavailable ({exc})", file=sys.stderr)
        return
    for report in reports:
        print(report.format())


def _report_liquidity(
    backends: list,  # noqa: ANN401 (list[Backend]; lazy import avoids a cycle)
    asset: str,
    addresses: tuple[str, ...],
) -> None:
    """Print any LP positions the wallet's addresses hold in ``asset``'s pool.

    Liquidity can sit on either backend, so every address is probed against all
    of them. A position is keyed by the L1 sender; for BTC that's not knowable
    ahead of time, so we probe every used address (most return nothing). The
    redeemable amount is shown as its own line, never folded into the spendable
    balance — an LP position isn't liquid and the figure is gross of exit fees.
    """
    for backend in backends:
        protocol = "CACAO" if backend.name == "maya" else "RUNE"
        price: float | None = None  # asset per RUNE/CACAO; fetched once, lazily
        priced = False
        for address in addresses:
            try:
                position = backend.client.liquidity_provider(asset, address)
            except HTTP_ERRORS as exc:
                print(
                    f"{backend.name} {asset}: LP lookup failed ({exc})", file=sys.stderr
                )
                break  # backend unreachable: don't hammer it for every address
            if position is None:
                continue
            if not priced:  # only worth a pool fetch once we've found a position
                priced = True
                try:
                    price = backend.client.pool(asset).asset_per_protocol
                except HTTP_ERRORS:
                    price = None  # fall back to flagging the side as uncounted
            print(
                position.format(
                    backend.name, protocol=protocol, protocol_price_in_asset=price
                )
            )


def _derivable_chain(to_: str) -> str:
    """The destination chain prefix (BTC/ETH/TRON are derivable; others need --dest)."""
    return ASSET[to_].split(".", 1)[0]


def _resolve_destination(
    args: argparse.Namespace, mnemonic: str | None, passphrase: str = ""
) -> str | None:
    if args.dest:
        problem = validate_destination_address(_derivable_chain(args.to_), args.dest)
        if problem:
            raise SystemExit(f"--dest: {problem}")
        return args.dest
    if mnemonic is None:
        return None
    # The destination address depends on the target *chain*, so a token like
    # TRON.USDT lands at the same Tron address as native TRX, ETH.USDT at the
    # ETH address, etc. The BIP-39 passphrase must be applied here too, or an
    # auto-derived --dest would pay an address the user cannot spend.
    chain = _derivable_chain(args.to_)
    if chain == "ETH":
        from cryptoswap_wallet.chains.eth import EthAdapter

        return EthAdapter(bip39_passphrase=passphrase).derive_address(mnemonic)
    if chain == "BTC":
        from cryptoswap_wallet.chains.btc import BtcAdapter

        return BtcAdapter(bip39_passphrase=passphrase).derive_address(
            mnemonic, BTC_RECEIVE_PATH
        )
    if chain == "TRON":
        from cryptoswap_wallet.chains.tron import TronAdapter

        return TronAdapter(bip39_passphrase=passphrase).derive_address(mnemonic)
    return None  # unknown target chain: caller must pass --dest


def _backends_for(args: argparse.Namespace):  # noqa: ANN202 (list[Backend], lazy import)
    from cryptoswap_wallet.backends import default_backends, get_backend

    if args.backend == "auto":
        return default_backends()
    return [get_backend(args.backend)]


def _streaming_kwargs(args: argparse.Namespace) -> dict[str, int | None]:
    """Streaming-swap quote kwargs from the parsed args (None when not requested)."""
    return {
        "streaming_interval": getattr(args, "stream_interval", None),
        "streaming_quantity": getattr(args, "stream_quantity", None),
    }


def _select_backend(  # noqa: ANN202 (Backend, lazy import)
    args: argparse.Namespace,
    *,
    from_asset: str,
    to_asset: str,
    amount: int,
    destination: str | None,
    tolerance_bps: int | None = None,
):
    """Pick the backend (lowest price when --backend auto).

    ``tolerance_bps`` is threaded into the selection quotes so a swap the user
    enables by raising it isn't refused here at the default tolerance. The
    backends we don't return are closed before returning (the chosen one is
    closed by the caller's ``with backend.client``); a single explicit backend
    is returned unquoted and closed by the caller.
    """
    from cryptoswap_wallet.backends import best_quote, gather_quotes

    backends = _backends_for(args)
    if len(backends) == 1:
        return backends[0]
    results = gather_quotes(
        backends,
        from_asset,
        to_asset,
        amount,
        destination,
        tolerance_bps=tolerance_bps,
        **_streaming_kwargs(args),
    )
    if not results:
        for unused in backends:
            unused.client.close()
        raise SwapAborted("no swap backend can serve this pair/amount")
    backend, _ = best_quote(results)
    if len(results) > 1:
        print(f"routing via {backend.name} (best of {len(results)})", file=sys.stderr)
    for unused in backends:
        if unused is not backend:
            unused.client.close()
    return backend


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
    from cryptoswap_wallet.pricefeed import (
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
            amount_units / THORCHAIN_UNIT,
            prices[id_from]["usd"],
            prices[id_to]["usd"],
        )
    except (*HTTP_ERRORS, KeyError, ValueError, ZeroDivisionError):
        return None
    quoted = quoted_out_units / THORCHAIN_UNIT
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
    from cryptoswap_wallet.backends import best_quote, gather_quotes

    amount = _base_units(args.amount)
    # Only decrypt the keystore if we actually need to derive the destination.
    if args.dest is None and _derivable_chain(args.to_) in ("BTC", "ETH", "TRON"):
        mnemonic, passphrase = _load_mnemonic(args)
    else:
        mnemonic, passphrase = None, ""
    dest = _resolve_destination(args, mnemonic, passphrase)
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
        for backend, quote in sorted(results, key=lambda p: -p[1].expected_amount_out):
            out = quote.expected_amount_out / THORCHAIN_UNIT
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
    if chain == "BTC":
        return _swap_from_btc(args)
    if chain == "ETH":  # native ETH and ERC-20 tokens (e.g. USDT-ETH)
        return _swap_from_eth(args)
    if chain == "TRON":  # native TRX (TRC-20 tokens not yet a source)
        return _swap_from_tron(args)
    print(f"swap source {args.from_} is not implemented yet", file=sys.stderr)
    return 2


def cmd_send(args: argparse.Namespace) -> int:
    chain = ASSET[args.asset].split(".", 1)[0]
    if chain == "BTC":
        return _send_btc(args)
    if chain == "ETH":  # native ETH and ERC-20 tokens (USDT-ETH / USDC-ETH)
        return _send_eth(args)
    if chain == "TRON":  # native TRX and TRC-20 tokens (USDT-TRON)
        return _send_tron(args)
    print(f"send for {args.asset} is not implemented yet", file=sys.stderr)
    return 2


def _send_eth(args: argparse.Namespace) -> int:
    from cryptoswap_wallet.chains.coins import InsufficientFunds, token_sweep_amount
    from cryptoswap_wallet.chains.eth import NATIVE_SEND_GAS, eth_sweep_amount

    asset = ASSET[args.asset]
    recipient = args.address
    problem = validate_destination_address("ETH", recipient)
    if problem:
        print(f"recipient: {problem}", file=sys.stderr)
        return 2
    is_token = "-" in asset
    sweep = args.amount == "max"
    mnemonic, passphrase = _load_mnemonic(args)
    with _eth_adapter(args, passphrase) as adapter:
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
                amount = eth_sweep_amount(
                    adapter.fetch_balance(from_address),
                    gas=NATIVE_SEND_GAS,
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
        print(f"max fee: {prepared.built.fee / 10**18:.6f} ETH")
        return _confirm_and_execute(prepared, adapter, args)


def _send_tron(args: argparse.Namespace) -> int:
    from cryptoswap_wallet.chains.coins import InsufficientFunds, token_sweep_amount

    asset = ASSET[args.asset]
    recipient = args.address
    problem = validate_destination_address("TRON", recipient)
    if problem:
        print(f"recipient: {problem}", file=sys.stderr)
        return 2
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
        prepared = adapter.build_and_verify_send(
            recipient=recipient, amount=amount, asset=asset, mnemonic=mnemonic
        )
        print(f"send:    {amount / THORCHAIN_UNIT:.8f} {args.asset} to {recipient}")
        return _confirm_and_execute(prepared, adapter, args)


def _send_btc(args: argparse.Namespace) -> int:
    from cryptoswap_wallet.chains.coins import InsufficientFunds, sweep_amount
    from cryptoswap_wallet.chains.scan import scan_account

    mnemonic, passphrase = _load_mnemonic(args)
    recipient = args.address
    sweep = args.amount == "max"
    with _btc_adapter(args, passphrase) as adapter:
        records = scan_account(
            derive_address=lambda p: adapter.derive_address(mnemonic, p),
            probe=adapter.address_info,
            account=BTC_ACCOUNT,
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

        change_address = adapter.derive_address(mnemonic, BTC_CHANGE_PATH)
        fee_rate = adapter.fetch_fee_rate()
        try:
            if sweep:
                total = sum(u.value for u in utxos)
                amount, _ = sweep_amount(total, len(utxos), fee_rate, memo_len=0)
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

        print(f"send:    {amount} sats to {recipient}")
        print(f"btc fee: {prepared.built.fee} sats @ {fee_rate} sat/vB")
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
    print(f"track: cryptoswap-wallet status {result.txid}")
    return 0


def _swap_from_btc(args: argparse.Namespace) -> int:
    from cryptoswap_wallet.chains.coins import InsufficientFunds
    from cryptoswap_wallet.chains.scan import scan_account

    mnemonic, passphrase = _load_mnemonic(args)
    dest = _resolve_destination(args, mnemonic, passphrase)
    if dest is None:
        print("a --dest address is required for this destination", file=sys.stderr)
        return 2

    sweep = args.amount == "max"
    with _btc_adapter(args, passphrase) as adapter:
        records = scan_account(
            derive_address=lambda p: adapter.derive_address(mnemonic, p),
            probe=adapter.address_info,
            account=BTC_ACCOUNT,
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

        change_address = adapter.derive_address(mnemonic, BTC_CHANGE_PATH)
        fee_rate = adapter.fetch_fee_rate()
        if sweep:
            from cryptoswap_wallet.chains.coins import sweep_amount

            total = sum(u.value for u in utxos)
            try:
                amount, _ = sweep_amount(total, len(utxos), fee_rate)
            except InsufficientFunds as exc:
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1
        else:
            amount = _base_units(args.amount)

        request = SwapRequest(
            from_asset="BTC.BTC",
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
                tolerance_bps=args.tolerance_bps,
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        with backend.client as thor:
            try:
                prepared = prepare_swap(
                    thorchain=thor,
                    adapter=adapter,
                    request=request,
                    now=int(time.time()),
                    mnemonic=mnemonic,
                    tolerance_bps=args.tolerance_bps,
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

            out = prepared.quote.expected_amount_out / THORCHAIN_UNIT
            print(f"via:     {backend.name}")
            print(f"send:    {amount} sats to {prepared.quote.inbound_address}")
            print(f"expect:  {out:.8f} {args.to_} -> {dest}")
            print(f"memo:    {prepared.quote.memo}")
            _print_swap_costs(
                prepared.quote,
                args.from_,
                args.to_,
                amount,
                price_check=args.price_check,
            )
            print(f"inbound: {prepared.built.fee} sats on BTC @ {fee_rate} sat/vB")
            return _confirm_and_execute(prepared, adapter, args)


def _swap_from_eth(args: argparse.Namespace) -> int:
    from cryptoswap_wallet.chains.coins import (
        InsufficientFunds,
        token_sweep_amount,
    )
    from cryptoswap_wallet.chains.eth import eth_sweep_amount

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
            "token source — 2 transactions (approve + deposit):",
            "if the deposit fails after the approve, an exact-amount allowance to "
            "the router remains",
        )
    with _eth_adapter(args, passphrase) as adapter:
        from_address = adapter.derive_address(mnemonic)
        nonce = adapter.get_nonce(from_address)
        max_fee_per_gas, max_priority_fee_per_gas = adapter.fetch_fees()
        if sweep and is_token:
            # A token sweep sends the whole balanceOf — gas is paid in ETH, not
            # the token, so the amount is exact.
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
                tolerance_bps=args.tolerance_bps,
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        with backend.client as thor:
            try:
                prepared = prepare_swap(
                    thorchain=thor,
                    adapter=adapter,
                    request=request,
                    now=int(time.time()),
                    mnemonic=mnemonic,
                    tolerance_bps=args.tolerance_bps,
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
            out = prepared.quote.expected_amount_out / THORCHAIN_UNIT
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
            print(f"inbound: {max_fee_eth:.6f} ETH max ({len(prepared.built.txs)} tx)")
            return _confirm_and_execute(prepared, adapter, args)


def _swap_from_tron(args: argparse.Namespace) -> int:
    from cryptoswap_wallet.chains.coins import InsufficientFunds, token_sweep_amount

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
                tolerance_bps=args.tolerance_bps,
            )
            with backend.client as thor:
                prepared = prepare_swap(
                    thorchain=thor,
                    adapter=adapter,
                    request=request,
                    now=int(time.time()),
                    mnemonic=mnemonic,
                    tolerance_bps=args.tolerance_bps,
                    **_streaming_kwargs(args),
                )
        except (SwapAborted, ValueError) as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1

        out = prepared.quote.expected_amount_out / THORCHAIN_UNIT
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


def cmd_add_liquidity(args: argparse.Namespace) -> int:
    from cryptoswap_wallet.liquidity import add_liquidity_memo

    pool = ASSET[args.asset]
    sweep = args.amount == "max"
    amount = None if sweep else _base_units(args.amount)
    return _liquidity(args, memo=add_liquidity_memo(pool), amount=amount, sweep=sweep)


def cmd_withdraw_liquidity(args: argparse.Namespace) -> int:
    from cryptoswap_wallet.liquidity import withdraw_liquidity_memo

    pool = ASSET[args.asset]
    return _liquidity(args, memo=withdraw_liquidity_memo(pool, args.bps), amount=None)


def _liquidity(
    args: argparse.Namespace, *, memo: str, amount: int | None, sweep: bool = False
) -> int:
    _warn(
        "only add liquidity that you can afford to lose, risks include:",
        "experimental feature - bugs may cause lost funds",
        "you're exposed to RUNE/CACAO volatility",
        "volatility may cause arbitrageurs to eat your funds",
        "for small amounts, the networking fees will probably outsize any win",
    )
    asset = ASSET[args.asset]
    chain = asset.split(".", 1)[0]
    if "-" in asset and chain != "ETH":
        # Only ETH-chain ERC-20 LP is wired (via the Maya router). USDT-TRON has
        # no Maya pool; there's nowhere to provide it.
        print(f"token liquidity is only supported for ETH tokens, not {args.asset}")
        return 2
    if chain == "BTC":
        return _liquidity_btc(args, memo=memo, amount=amount, sweep=sweep)
    if chain == "ETH":
        return _liquidity_eth(args, memo=memo, amount=amount, sweep=sweep)
    if chain == "TRON":
        return _liquidity_tron(args, memo=memo, amount=amount, sweep=sweep)
    print(f"liquidity on {chain} is not implemented", file=sys.stderr)
    return 2


def _liquidity_btc(
    args: argparse.Namespace, *, memo: str, amount: int | None, sweep: bool = False
) -> int:
    from cryptoswap_wallet.chains.coins import InsufficientFunds
    from cryptoswap_wallet.chains.scan import scan_account
    from cryptoswap_wallet.swap import prepare_liquidity

    mnemonic, passphrase = _load_mnemonic(args)
    with _btc_adapter(args, passphrase) as adapter, _liquidity_client(args) as thor:
        records = scan_account(
            derive_address=lambda p: adapter.derive_address(mnemonic, p),
            probe=adapter.address_info,
            account=BTC_ACCOUNT,
        )
        utxos = [
            dataclasses.replace(u, path=path)
            for path, address, info in records
            if info.confirmed > 0
            for u in adapter.fetch_utxos(address)
        ]
        if not utxos:
            print(
                "no confirmed BTC (add needs funds; withdraw needs a little BTC "
                "in-wallet for the trigger tx)",
                file=sys.stderr,
            )
            return 1
        change_address = adapter.derive_address(mnemonic, BTC_CHANGE_PATH)
        fee_rate = adapter.fetch_fee_rate()
        if sweep:
            from cryptoswap_wallet.chains.coins import sweep_amount

            total = sum(u.value for u in utxos)
            try:
                amount, _ = sweep_amount(
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
        print(f"send:    {prepared.plan.amount} sats to {vault}")
        print(f"memo:    {memo}")
        print(f"btc fee: {prepared.built.fee} sats @ {fee_rate} sat/vB")
        return _confirm_and_execute(prepared, adapter, args)


def _liquidity_eth(
    args: argparse.Namespace, *, memo: str, amount: int | None, sweep: bool = False
) -> int:
    from cryptoswap_wallet.chains.coins import InsufficientFunds, token_sweep_amount
    from cryptoswap_wallet.chains.eth import eth_sweep_amount
    from cryptoswap_wallet.swap import prepare_liquidity

    asset = ASSET[args.asset]
    # A token *add* (approve + router deposit) is the only token op that needs the
    # router; a token *withdraw* is a native-ETH dust trigger, handled natively.
    token_add = memo.startswith("+") and "-" in asset
    mnemonic, passphrase = _load_mnemonic(args)
    with _eth_adapter(args, passphrase) as adapter, _liquidity_client(args) as thor:
        from_address = adapter.derive_address(mnemonic)
        nonce = adapter.get_nonce(from_address)
        max_fee_per_gas, max_priority_fee_per_gas = adapter.fetch_fees()
        build_extra: dict[str, object] = {}
        decimals = 18
        if token_add:
            token = asset.split("-", 1)[1]
            decimals = adapter.token_decimals(token)
            eth_status = thor.inbound_addresses().get("ETH")
            if not eth_status or not eth_status.router:
                print("no ETH router on this backend — token LP needs it")
                return 2
            build_extra["router"] = eth_status.router
            _warn(
                "token liquidity add — 2 transactions (approve + deposit):",
                "gas is paid in ETH, separate from the tokens deposited",
                "if the deposit fails after approve, a router allowance remains",
            )
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
                    max_fee_per_gas=max_fee_per_gas,
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
                nonce=nonce,
                gas=args.eth_gas,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
                max_fee_wei=ETH_MAX_FEE_WEI,
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
            eth_amt = prepared.plan.amount_wei / 10**18
            print(f"send:    {eth_amt:.8f} ETH to {prepared.plan.inbound_address}")
        print(f"memo:    {memo}")
        print(f"max fee: {prepared.built.fee / 10**18:.6f} ETH")
        return _confirm_and_execute(prepared, adapter, args)


def _liquidity_tron(
    args: argparse.Namespace, *, memo: str, amount: int | None, sweep: bool = False
) -> int:
    from cryptoswap_wallet.swap import prepare_liquidity

    if sweep:
        print("--amount max is not supported for TRON liquidity yet", file=sys.stderr)
        return 2
    mnemonic, passphrase = _load_mnemonic(args)
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
    # A bare tx hash doesn't say which network observed it, and an inbound only
    # exists on the chain it was deposited to (a Maya LP is invisible to
    # thornode). With --backend auto we query every backend and report the one
    # that actually observed it; an unknown hash just yields a "not observed"
    # body on each, so falling through to the last is harmless.
    from cryptoswap_wallet.net import HTTP_ERRORS

    backends = _backends_for(args)
    status: dict[str, object] = {}
    for backend in backends:
        try:
            with backend.client as thor:
                status = thor.tx_status(args.txid)
        except HTTP_ERRORS:
            continue
        observed = status.get("stages", {}).get("inbound_observed", {}).get("started")
        if observed or len(backends) == 1:
            if len(backends) > 1:
                print(f"// observed on {backend.name}", file=sys.stderr)
            print(json.dumps(status, indent=2))
            return 0
    # Not observed on any backend yet (genuinely pending, or unknown hash).
    print(json.dumps(status, indent=2))
    return 0


# --- parser -----------------------------------------------------------------


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
    if amount * THORCHAIN_UNIT < 1:
        raise argparse.ArgumentTypeError(
            f"amount {value!r} is below one base unit (1e-8); too small to send"
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


def _base_units(amount: Decimal) -> int:
    """Scale a human ``--amount`` (whole --from units) to THORChain 1e8 base units.

    Decimal end-to-end: a large amount like ``93393106.59778857`` must not pick
    up a float rounding error and be signed/broadcast one base unit off.
    """
    return int((amount * THORCHAIN_UNIT).to_integral_value(rounding=ROUND_HALF_EVEN))


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
        choices=["thorchain", "maya", "auto"],
        default="auto",
        help="swap backend (auto = lowest price across all)",
    )
    sub.add_argument(
        "--price-check",
        dest="price_check",
        action="store_true",
        default=True,
        help="compare the quote against a public spot price (CoinGecko); default on",
    )
    sub.add_argument(
        "--no-price-check",
        dest="price_check",
        action="store_false",
        help="skip the external spot-price comparison",
    )
    sub.add_argument(
        "--stream-interval",
        type=_nonneg_int,
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
    sub.add_argument(
        "--eth-rpc", help="Ethereum JSON-RPC URL ($CRYPTOSWAP_WALLET_ETH_RPC)"
    )
    sub.add_argument("--eth-gas", type=int, default=60000, help="ETH gas limit")


def _add_liquidity_backend_arg(sub: argparse.ArgumentParser) -> None:
    # No 'auto': LP is not price-routed — it's a choice of which network (and
    # which pairing, RUNE vs Maya's CACAO) to hold the position on.
    sub.add_argument(
        "--backend",
        choices=["thorchain", "maya"],
        default="thorchain",
        help="network to LP on (maya pairs with CACAO; has no TRON pool)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cryptoswap-wallet",
        description="CLI multi-currency wallet with THORChain swaps",
    )
    parser.add_argument(
        "--version", "-V", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--keystore", help="keystore path ($CRYPTOSWAP_WALLET_KEYSTORE)"
    )
    parser.add_argument(
        "--esplora", help="Esplora API base URL ($CRYPTOSWAP_WALLET_ESPLORA)"
    )
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

    s = sub.add_parser("address", help="show derived BTC, ETH, BSC and TRON addresses")
    s.add_argument("--key")
    s.set_defaults(func=cmd_address)

    s = sub.add_parser("balance", help="show balances across supported chains")
    s.add_argument("--key")
    s.add_argument(
        "--eth-rpc", help="Ethereum JSON-RPC URL ($CRYPTOSWAP_WALLET_ETH_RPC)"
    )
    s.add_argument("--tron-api", help="TRON API base URL ($CRYPTOSWAP_WALLET_TRON_API)")
    s.add_argument("--bsc-rpc", help="BSC JSON-RPC URL ($CRYPTOSWAP_WALLET_BSC_RPC)")
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
    s.add_argument(
        "--eth-rpc", help="Ethereum JSON-RPC URL ($CRYPTOSWAP_WALLET_ETH_RPC)"
    )
    s.add_argument(
        "--eth-gas", type=int, default=60000, help="gas limit for ETH deposit"
    )
    s.add_argument(
        "--tolerance-bps",
        type=int,
        default=DEFAULT_TOLERANCE_BPS,
        help="max basis points of price tolerance; raise it for small/high-fee "
        f"swaps THORChain refuses at the default {DEFAULT_TOLERANCE_BPS}. Ignored "
        "when --stream-interval is set (streaming manages slippage itself)",
    )
    s.set_defaults(func=cmd_swap)

    s = sub.add_parser(
        "add-liquidity", help="EXPERIMENTAL: add single-sided liquidity to a pool"
    )
    s.add_argument("--asset", required=True, choices=list(ASSET))
    s.add_argument(
        "--amount",
        type=_amount,
        required=True,
        help="amount of --asset, or 'max' to add the whole balance (BTC/ETH)",
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
        "send", help="send to an external address (no swap); BTC/ETH/TRON"
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
    s.add_argument("--max-fee", type=int, default=50_000, help="max BTC fee in sats")
    s.add_argument(
        "--eth-rpc", help="Ethereum JSON-RPC URL ($CRYPTOSWAP_WALLET_ETH_RPC)"
    )
    s.add_argument("--tron-api", help="TRON API base URL ($CRYPTOSWAP_WALLET_TRON_API)")
    s.set_defaults(func=cmd_send)

    s = sub.add_parser("status", help="track a swap by inbound txid")
    s.add_argument("txid")
    s.add_argument(
        "--backend",
        choices=["thorchain", "maya", "auto"],
        default="auto",
        help="network to query (auto = try all, report where observed)",
    )
    s.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # Shell tab-completion. argcomplete sets _ARGCOMPLETE only when the completion
    # machinery invokes us, so gate the import on it: normal runs pay nothing, and
    # there's no optional-vs-required ambiguity (it's a declared dependency).
    # Enable with: eval "$(register-python-argcomplete cryptoswap-wallet)"
    if "_ARGCOMPLETE" in os.environ:
        import argcomplete

        argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
