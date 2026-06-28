"""Command-line interface for cryptoswap.

Commands: init / add-hd / add-raw / list / address / balance / quote / swap /
status. Swaps default to a dry run that builds + verifies + prints without
broadcasting; ``--confirm`` is required to actually send funds.

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
from pathlib import Path

from cryptoswap.keystore import HdKey, Keystore
from cryptoswap.net import HTTP_ERRORS
from cryptoswap.swap import SwapAborted, SwapRequest, execute_swap, prepare_swap
from cryptoswap.thorchain import THORCHAIN_UNIT, ThorchainClient

DEFAULT_KEYSTORE = "~/.config/cryptoswap/keystore.json"
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
}


# --- config helpers ---------------------------------------------------------


def _keystore_path(args: argparse.Namespace) -> Path:
    return Path(
        args.keystore or os.environ.get("CRYPTOSWAP_KEYSTORE") or DEFAULT_KEYSTORE
    ).expanduser()


def _passphrase(*, confirm: bool = False) -> str:
    pw = os.environ.get("CRYPTOSWAP_PASSPHRASE")
    if pw:
        return pw
    pw = getpass.getpass("Keystore passphrase: ")
    if confirm and getpass.getpass("Repeat passphrase: ") != pw:
        raise SystemExit("passphrases do not match")
    return pw


def _btc_adapter(args: argparse.Namespace):  # noqa: ANN202 (BtcAdapter, lazy import)
    from cryptoswap.chains.btc import DEFAULT_ESPLORA, BtcAdapter

    url = args.esplora or os.environ.get("CRYPTOSWAP_ESPLORA") or DEFAULT_ESPLORA
    return BtcAdapter(url)


def _eth_adapter(args: argparse.Namespace):  # noqa: ANN202 (EthAdapter, lazy import)
    from cryptoswap.chains.eth import DEFAULT_RPC, EthAdapter

    url = (
        getattr(args, "eth_rpc", None)
        or os.environ.get("CRYPTOSWAP_ETH_RPC")
        or DEFAULT_RPC
    )
    return EthAdapter(url)


def _tron_adapter(args: argparse.Namespace):  # noqa: ANN202 (TronAdapter, lazy import)
    from cryptoswap.chains.tron import DEFAULT_TRON_API, TronAdapter

    url = (
        getattr(args, "tron_api", None)
        or os.environ.get("CRYPTOSWAP_TRON_API")
        or DEFAULT_TRON_API
    )
    return TronAdapter(url)


def _wallet_adapters(args: argparse.Namespace) -> list:  # noqa: ANN201
    """Adapters whose balances `balance` reports — add a chain here and it scales."""
    return [_btc_adapter(args), _eth_adapter(args), _tron_adapter(args)]


def _load_mnemonic(args: argparse.Namespace) -> str:
    keystore = Keystore.load(_keystore_path(args), _passphrase())
    for entry in keystore.entries:
        if isinstance(entry, HdKey) and (args.key is None or entry.label == args.key):
            return entry.mnemonic.reveal()
    raise SystemExit("no matching HD key in keystore")


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
        from cryptoswap.chains.btc import generate_mnemonic

        mnemonic = generate_mnemonic()
    else:
        mnemonic = args.mnemonic or getpass.getpass("BIP39 mnemonic: ")
    keystore.add_hd(args.label, mnemonic, passphrase=args.bip39_passphrase or None)
    keystore.save(path, pw)
    print(f"added HD key {args.label!r}")
    if args.generate:
        from cryptoswap.chains.btc import BtcAdapter

        print(
            "BTC receive address:",
            BtcAdapter().derive_address(mnemonic, BTC_RECEIVE_PATH),
        )
        print(
            "the new seed is stored ENCRYPTED in the keystore; back up the keystore "
            "file + passphrase.\nto reveal the words (do it privately): "
            f"cryptoswap show-seed --key {args.label}"
        )
    return 0


def cmd_show_seed(args: argparse.Namespace) -> int:
    keystore = Keystore.load(_keystore_path(args), _passphrase())
    for entry in keystore.entries:
        if isinstance(entry, HdKey) and (args.key is None or entry.label == args.key):
            print(entry.mnemonic.reveal())
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
    from cryptoswap.chains.btc import BtcAdapter
    from cryptoswap.chains.eth import EthAdapter
    from cryptoswap.chains.tron import TronAdapter

    mnemonic = _load_mnemonic(args)
    print("BTC: ", BtcAdapter().derive_address(mnemonic, BTC_RECEIVE_PATH))
    print("ETH: ", EthAdapter().derive_address(mnemonic))
    print("TRON:", TronAdapter().derive_address(mnemonic))
    return 0


def cmd_balance(args: argparse.Namespace) -> int:
    print(
        "checking balances (the BTC address scan can take ~10s)...",
        file=sys.stderr,
        flush=True,
    )
    mnemonic = _load_mnemonic(args)
    for adapter in _wallet_adapters(args):
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
                print(f"{adapter.chain}: balance unavailable ({exc})", file=sys.stderr)
                continue
            print(report.format())
    return 0


def _derivable_chain(to_: str) -> str:
    """The destination chain we can derive an address for (BTC/ETH/TRON)."""
    return ASSET[to_].split(".", 1)[0]


def _resolve_destination(args: argparse.Namespace, mnemonic: str | None) -> str | None:
    if args.dest:
        return args.dest
    if mnemonic is None:
        return None
    # The destination address depends on the target *chain*, so a token like
    # TRON.USDT lands at the same Tron address as native TRX, ETH.USDT at the
    # ETH address, etc.
    chain = _derivable_chain(args.to_)
    if chain == "ETH":
        from cryptoswap.chains.eth import EthAdapter

        return EthAdapter().derive_address(mnemonic)
    if chain == "BTC":
        from cryptoswap.chains.btc import BtcAdapter

        return BtcAdapter().derive_address(mnemonic, BTC_RECEIVE_PATH)
    if chain == "TRON":
        from cryptoswap.chains.tron import TronAdapter

        return TronAdapter().derive_address(mnemonic)
    return None  # unknown target chain: caller must pass --dest


def cmd_quote(args: argparse.Namespace) -> int:
    if args.amount == "max":
        print("quote needs a numeric amount ('max' is only for swap)", file=sys.stderr)
        return 2
    amount = int(round(args.amount * THORCHAIN_UNIT))
    # Only decrypt the keystore if we actually need to derive the destination.
    mnemonic = (
        _load_mnemonic(args)
        if args.dest is None and _derivable_chain(args.to_) in ("BTC", "ETH", "TRON")
        else None
    )
    dest = _resolve_destination(args, mnemonic)
    with ThorchainClient() as thor:
        quote = thor.quote_swap(ASSET[args.from_], ASSET[args.to_], amount, dest)
    out = quote.expected_amount_out / THORCHAIN_UNIT
    min_in = quote.recommended_min_amount_in / THORCHAIN_UNIT
    print(f"in:     {args.amount} {args.from_}")
    print(f"expect: {out:.8f} {args.to_}")
    print(f"fees:   {quote.fees.total_bps} bps ({quote.fees.slippage_bps} bps slip)")
    print(f"min in: {min_in:.8f} {args.from_}")
    print(f"vault:  {quote.inbound_address}")
    if quote.memo:
        print(f"memo:      {quote.memo}")
    return 0


def cmd_swap(args: argparse.Namespace) -> int:
    chain = ASSET[args.from_].split(".", 1)[0]
    if chain == "BTC":
        return _swap_from_btc(args)
    if chain == "ETH":  # native ETH and ERC-20 tokens (e.g. USDT-ETH)
        return _swap_from_eth(args)
    print(f"swap source {args.from_} is not implemented yet", file=sys.stderr)
    return 2


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
    if time.time() >= prepared.plan.expiry:
        print("ABORTED: quote expired while confirming; re-run.", file=sys.stderr)
        return 1
    result = execute_swap(prepared, adapter, confirm=True)
    print(f"\nBROADCAST txid: {result.txid}")
    print(f"track: cryptoswap status {result.txid}")
    return 0


def _swap_from_btc(args: argparse.Namespace) -> int:
    from cryptoswap.chains.scan import scan_account

    mnemonic = _load_mnemonic(args)
    dest = _resolve_destination(args, mnemonic)
    if dest is None:
        print("a --dest address is required for this destination", file=sys.stderr)
        return 2

    sweep = args.amount == "max"
    with _btc_adapter(args) as adapter, ThorchainClient() as thor:
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
            from cryptoswap.chains.coins import InsufficientFunds, sweep_amount

            total = sum(u.value for u in utxos)
            try:
                amount, _ = sweep_amount(total, len(utxos), fee_rate)
            except InsufficientFunds as exc:
                print(f"ABORTED: {exc}", file=sys.stderr)
                return 1
        else:
            amount = int(round(args.amount * THORCHAIN_UNIT))

        request = SwapRequest(
            from_asset="BTC.BTC",
            to_asset=ASSET[args.to_],
            amount=amount,
            destination=dest,
        )
        try:
            prepared = prepare_swap(
                thorchain=thor,
                adapter=adapter,
                request=request,
                now=int(time.time()),
                mnemonic=mnemonic,
                scanned_utxos=utxos,
                fee_rate=fee_rate,
                change_address=change_address,
                max_fee=args.max_fee,
                sweep=sweep,
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1

        out = prepared.quote.expected_amount_out / THORCHAIN_UNIT
        print(f"send:      {amount} sats to {prepared.quote.inbound_address}")
        print(f"expect:    {out:.8f} {args.to_} -> {dest}")
        print(f"memo:      {prepared.quote.memo}")
        print(f"btc fee:   {prepared.built.fee} sats @ {fee_rate} sat/vB")
        return _confirm_and_execute(prepared, adapter, args)


def _swap_from_eth(args: argparse.Namespace) -> int:
    from cryptoswap.chains.coins import InsufficientFunds
    from cryptoswap.chains.eth import eth_sweep_amount

    mnemonic = _load_mnemonic(args)
    dest = _resolve_destination(args, mnemonic)
    if dest is None:
        print("a --dest address is required for this destination", file=sys.stderr)
        return 2

    from_asset = ASSET[args.from_]
    is_token = "-" in from_asset
    sweep = args.amount == "max"
    if sweep and is_token:
        print("--amount max is not supported for token sources yet", file=sys.stderr)
        return 2
    with _eth_adapter(args) as adapter, ThorchainClient() as thor:
        from_address = adapter.derive_address(mnemonic)
        nonce = adapter.get_nonce(from_address)
        max_fee_per_gas, max_priority_fee_per_gas = adapter.fetch_fees()
        if sweep:
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
            amount = int(round(args.amount * THORCHAIN_UNIT))
        request = SwapRequest(
            from_asset=from_asset,
            to_asset=ASSET[args.to_],
            amount=amount,
            destination=dest,
        )
        try:
            prepared = prepare_swap(
                thorchain=thor,
                adapter=adapter,
                request=request,
                now=int(time.time()),
                mnemonic=mnemonic,
                nonce=nonce,
                gas=args.eth_gas,
                max_fee_per_gas=max_fee_per_gas,
                max_priority_fee_per_gas=max_priority_fee_per_gas,
                max_fee_wei=ETH_MAX_FEE_WEI,
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1

        out = prepared.quote.expected_amount_out / THORCHAIN_UNIT
        amount_in = amount / THORCHAIN_UNIT
        max_fee_eth = prepared.built.fee / 10**18
        vault = prepared.quote.inbound_address
        print(f"send:    {amount_in:.8f} {args.from_} to {vault}")
        print(f"expect:    {out:.8f} {args.to_} -> {dest}")
        print(f"memo:      {prepared.quote.memo}")
        print(f"max fee:   {max_fee_eth:.6f} ETH ({len(prepared.built.txs)} tx)")
        return _confirm_and_execute(prepared, adapter, args)


def cmd_add_liquidity(args: argparse.Namespace) -> int:
    from cryptoswap.liquidity import add_liquidity_memo

    pool = ASSET[args.asset]
    amount = int(round(args.amount * THORCHAIN_UNIT))
    return _liquidity(args, memo=add_liquidity_memo(pool), amount=amount)


def cmd_withdraw_liquidity(args: argparse.Namespace) -> int:
    from cryptoswap.liquidity import withdraw_liquidity_memo

    pool = ASSET[args.asset]
    return _liquidity(args, memo=withdraw_liquidity_memo(pool, args.bps), amount=None)


def _liquidity(args: argparse.Namespace, *, memo: str, amount: int | None) -> int:
    print(
        "EXPERIMENTAL liquidity op. Risk (impermanent loss, RUNE price, protocol) "
        "and fee yield both scale with size; what penalises small positions is the "
        "~fixed round-trip cost (add + withdraw + outbound). Only LP what you can "
        "afford to lose.",
        file=sys.stderr,
    )
    if "-" in ASSET[args.asset]:
        print("liquidity for tokens is not supported yet", file=sys.stderr)
        return 2
    chain = ASSET[args.asset].split(".", 1)[0]
    if chain == "BTC":
        return _liquidity_btc(args, memo=memo, amount=amount)
    if chain == "ETH":
        return _liquidity_eth(args, memo=memo, amount=amount)
    print(f"liquidity on {chain} is not implemented", file=sys.stderr)
    return 2


def _liquidity_btc(args: argparse.Namespace, *, memo: str, amount: int | None) -> int:
    from cryptoswap.chains.scan import scan_account
    from cryptoswap.swap import prepare_liquidity

    mnemonic = _load_mnemonic(args)
    with _btc_adapter(args) as adapter, ThorchainClient() as thor:
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
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        vault = prepared.plan.inbound_address
        print(f"send:    {prepared.plan.amount} sats to {vault}")
        print(f"memo:    {memo}")
        print(f"btc fee: {prepared.built.fee} sats @ {fee_rate} sat/vB")
        return _confirm_and_execute(prepared, adapter, args)


def _liquidity_eth(args: argparse.Namespace, *, memo: str, amount: int | None) -> int:
    from cryptoswap.swap import prepare_liquidity

    mnemonic = _load_mnemonic(args)
    with _eth_adapter(args) as adapter, ThorchainClient() as thor:
        from_address = adapter.derive_address(mnemonic)
        nonce = adapter.get_nonce(from_address)
        max_fee_per_gas, max_priority_fee_per_gas = adapter.fetch_fees()
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
            )
        except SwapAborted as exc:
            print(f"ABORTED: {exc}", file=sys.stderr)
            return 1
        eth_amt = prepared.plan.amount_wei / 10**18
        print(f"send:    {eth_amt:.8f} ETH to {prepared.plan.inbound_address}")
        print(f"memo:    {memo}")
        print(f"max fee: {prepared.built.fee / 10**18:.6f} ETH")
        return _confirm_and_execute(prepared, adapter, args)


def cmd_status(args: argparse.Namespace) -> int:
    with ThorchainClient() as thor:
        print(json.dumps(thor.tx_status(args.txid), indent=2))
    return 0


# --- parser -----------------------------------------------------------------


def _amount(value: str) -> float | str:
    """Parse a swap amount: a number, or the literal 'max' to sweep the balance."""
    return "max" if value.lower() == "max" else float(value)


def _add_swap_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--from", dest="from_", default="BTC", choices=list(ASSET))
    sub.add_argument("--to", dest="to_", default="ETH", choices=list(ASSET))
    sub.add_argument(
        "--amount", type=_amount, required=True, help="amount of --from asset, or 'max'"
    )
    sub.add_argument("--dest", help="destination address (default: derived from seed)")
    sub.add_argument("--key", help="keystore HD key label (default: first)")


def _add_broadcast_args(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--key", help="keystore HD key label (default: first)")
    sub.add_argument("--confirm", action="store_true", help="actually broadcast")
    sub.add_argument(
        "--yes", action="store_true", help="skip the interactive confirm (automation)"
    )
    sub.add_argument("--max-fee", type=int, default=50_000, help="max BTC fee in sats")
    sub.add_argument("--eth-rpc", help="Ethereum JSON-RPC URL ($CRYPTOSWAP_ETH_RPC)")
    sub.add_argument("--eth-gas", type=int, default=60000, help="ETH gas limit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cryptoswap", description="CLI multi-currency wallet with THORChain swaps"
    )
    parser.add_argument("--keystore", help="keystore path ($CRYPTOSWAP_KEYSTORE)")
    parser.add_argument("--esplora", help="Esplora API base URL ($CRYPTOSWAP_ESPLORA)")
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

    s = sub.add_parser("address", help="show derived BTC and ETH addresses")
    s.add_argument("--key")
    s.set_defaults(func=cmd_address)

    s = sub.add_parser("balance", help="show balances across supported chains")
    s.add_argument("--key")
    s.add_argument("--eth-rpc", help="Ethereum JSON-RPC URL ($CRYPTOSWAP_ETH_RPC)")
    s.add_argument("--tron-api", help="TRON API base URL ($CRYPTOSWAP_TRON_API)")
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
    s.add_argument("--eth-rpc", help="Ethereum JSON-RPC URL ($CRYPTOSWAP_ETH_RPC)")
    s.add_argument(
        "--eth-gas", type=int, default=60000, help="gas limit for ETH deposit"
    )
    s.set_defaults(func=cmd_swap)

    s = sub.add_parser(
        "add-liquidity", help="EXPERIMENTAL: add single-sided liquidity to a pool"
    )
    s.add_argument("--asset", required=True, choices=list(ASSET))
    s.add_argument("--amount", type=float, required=True, help="amount of --asset")
    _add_broadcast_args(s)
    s.set_defaults(func=cmd_add_liquidity)

    s = sub.add_parser(
        "withdraw-liquidity", help="EXPERIMENTAL: withdraw liquidity from a pool"
    )
    s.add_argument("--asset", required=True, choices=list(ASSET))
    s.add_argument(
        "--bps", type=int, default=10000, help="basis points to withdraw (1..10000)"
    )
    _add_broadcast_args(s)
    s.set_defaults(func=cmd_withdraw_liquidity)

    s = sub.add_parser("status", help="track a swap by inbound txid")
    s.add_argument("txid")
    s.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
