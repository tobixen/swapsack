"""Dash chain adapter — Phase 1 (hold/balance) + Phase 2 (send/sweep).

Dash is a legacy (pre-segwit) P2PKH chain with no Blockstream Esplora; the
balance / UTXO / broadcast layer speaks to an Insight-API instance, falling
over to a Blockbook one when it is down (configurable — a single community
explorer is a SPOF that can silently under-report funds, see docs/dash.md).
Swaps route through Maya only (no DASH pool on THORChain).

Spending shares the bitcoinlib build/gate/sign path with BTC
(:mod:`swapsack.chains.utxo`): Dash transactions are plain pre-segwit Bitcoin
transactions with different address prefixes, so a ``dash`` network registered
in bitcoinlib (below) is all the signer needs. The legacy P2PKH fee/dust maths
comes from :data:`swapsack.chains.coins.P2PKH`. Insight exposes no usable
``estimatefee``, and Dash fees are ~fixed and low, so ``fetch_fee_rate``
returns a conservative constant instead of a network estimate.

The swap-*from* side (vault deposit + OP_RETURN memo) is wired into the CLI
via ``_swap_from_utxo``.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, TypeVar
from urllib.parse import urlsplit

import niquests
from bitcoinlib.networks import NETWORK_DEFINITIONS

from swapsack.chains.base import AddressInfo, BalanceReport, TxEntry, TxSummary
from swapsack.chains.coins import P2PKH, Utxo, decode_op_return
from swapsack.chains.history import DEFAULT_TX_LIMIT, AddressTxs, collect_pages
from swapsack.chains.p2pkh import derive_p2pkh_address
from swapsack.chains.utxo import UtxoTxBuilder
from swapsack.net import TRANSIENT_ERRORS, HostUnreachable, HttpClient
from swapsack.swap import BroadcastError

_T = TypeVar("_T")

DEFAULT_DASH_API = "https://insight.dash.org/insight-api"
# A Blockbook (Trezor's indexer, a different API from Insight) run by Atomic
# Wallet. The fallback because insight.dash.org answered nothing but 502 on
# 2026-10-29 and no other Insight instance for Dash was left standing; the
# trezor.io Blockbooks did not respond either. A best-effort host of a wallet
# vendor, with no promise behind it — hence second, not first.
DEFAULT_DASH_BLOCKBOOK = "https://dash.atomicwallet.io/api/v2"
# Tried in order, and the one that answers is pinned. As with
# DEFAULT_ESPLORA_NODES, naming one with --dash-api turns the fallback off.
DEFAULT_DASH_NODES = (DEFAULT_DASH_API, DEFAULT_DASH_BLOCKBOOK)
DEFAULT_DERIVATION = "m/44'/5'/0'/0/0"  # receive chain, first index
ACCOUNT = "m/44'/5'/0'"
CHANGE_PATH = "m/44'/5'/0'/1/0"  # internal (change) chain, first index
PREFIX_P2PKH = b"\x4c"  # addresses start with "X"
DUFFS = 100_000_000  # base units per DASH
# Insight caps a /txs window server-side (50 on the reference implementation);
# asking for that much per request keeps the walk to one round trip per page.
INSIGHT_TX_PAGE = 50
# How many times a paged walk is restarted when the address's history moves
# under it (see DashAdapter.address_txs). A busy address may never hold still,
# so this is bounded and the caller is told the answer is incomplete.
INSIGHT_WALK_ATTEMPTS = 3
# Blockbook pages by number and allows up to 1000 per page; this keeps a busy
# address's response a reasonable size.
BLOCKBOOK_TX_PAGE = 100

# Conservative flat fee rate (duffs/vB). Dash Core's min relay is 1 duff/B and
# blocks are far from full; 2 leaves margin without overpaying (a typical
# 1-in-2-out send is ~227 vB ≈ 454 duffs ≈ €0.0002). Insight has no usable
# estimatefee endpoint to ask instead (see docs/dash.md).
DEFAULT_FEE_RATE = 2.0

# bitcoinlib ships no Dash network; register one (idempotent). Only the fields
# the signer touches matter here: the address/WIF prefixes and standard BIP32
# xpub/xprv bytes (the legacy drkp/drkv bytes are deprecated — Trust Wallet et
# al. use the standard ones). Dash has no segwit, hence no bech32 prefix.
_DASH_NETWORK = {
    "description": "Dash Network",
    "currency_name": "dash",
    "currency_name_plural": "dash",
    "currency_symbol": "DASH",
    "currency_code": "DASH",
    "prefix_address": "4C",
    "prefix_address_p2sh": "10",
    "prefix_bech32": "",
    "prefix_wif": "CC",
    "prefixes_wif": [
        ["0488B21E", "xpub", "public", False, "legacy", "p2pkh"],
        ["0488ADE4", "xprv", "private", False, "legacy", "p2pkh"],
    ],
    "bip44_cointype": 5,
    "denominator": 1e-08,
    "dust_amount": P2PKH.dust,
    "fee_default": 1000,
    "fee_min": 226,
    "fee_max": 100000,
    "priority": 5,
}
NETWORK_DEFINITIONS.setdefault("dash", _DASH_NETWORK)


def _appearances(stats: dict, sic: str, corrected: str) -> int:
    """The larger of Insight's two spellings of an appearance counter.

    Insight spells it "txApperances" (sic); newer forks add the corrected
    spelling. Take the max rather than preferring either, because a fork that
    emits *both* may keep only one of them current — and a stale 0 in the
    preferred key would read as "no history", stopping the gap-limit scan early
    and hiding funded addresses.
    """
    return max(int(stats.get(sic, 0) or 0), int(stats.get(corrected, 0) or 0))


def parse_insight_addr(stats: dict) -> AddressInfo:
    appearances = _appearances(stats, "txApperances", "txAppearances")
    unconfirmed = _appearances(
        stats, "unconfirmedTxApperances", "unconfirmedTxAppearances"
    )
    received = stats.get("totalReceivedSat", 0)
    pending = stats.get("unconfirmedBalanceSat", 0)
    return AddressInfo(
        # Any evidence of use keeps the scan going. `pending` is checked for
        # non-zero, not positive: an unconfirmed *spend* is evidence too.
        has_history=appearances > 0 or unconfirmed > 0 or received > 0 or pending != 0,
        confirmed=stats.get("balanceSat", 0),
        pending=pending,
    )


def _duffs(value: object) -> int:
    """A DASH amount (Insight's decimal string or float) as whole duffs.

    Via :class:`~decimal.Decimal`, never binary float: ``0.001495`` is not
    representable in float64, and ``int(0.001495 * 1e8)`` truncates to 149499 —
    a balance off by a base unit for no reason at all.
    """
    if value in (None, ""):
        return 0
    return int((Decimal(str(value)) * DUFFS).to_integral_value(rounding=ROUND_HALF_UP))


def parse_insight_tx(payload: dict) -> TxSummary:
    """Parse one Insight ``/txs`` item into the neutral :class:`TxSummary`.

    Pure (no I/O), like :func:`parse_insight_addr`. Insight is richer than
    Esplora in one respect worth keeping: it names the transaction that spent
    each output (``spentTxId``), which the history listing believes over its own
    local inference.
    """
    confirmed = int(payload.get("confirmations", 0) or 0) > 0
    height = payload.get("blockheight")

    def spend(item: dict) -> TxEntry:
        """An input. A coinbase has no prevout at all — no address, no value."""
        value = item.get("valueSat")
        return TxEntry(
            value=int(value) if value is not None else _duffs(item.get("value")),
            address=item.get("addr"),
            txid=item.get("txid"),
            vout=item.get("vout"),
            sequence=item.get("sequence"),
        )

    def entry(item: dict) -> TxEntry:
        script = item.get("scriptPubKey") or {}
        is_data = script.get("type") == "nulldata"
        data = None
        if is_data and (hex_script := script.get("hex")):
            try:
                data = decode_op_return(bytes.fromhex(hex_script))
            except ValueError:
                data = None  # an OP_RETURN we cannot read; nothing to report
        addresses = script.get("addresses") or []
        return TxEntry(
            value=_duffs(item.get("value")),
            address=addresses[0] if addresses and not is_data else None,
            op_return=is_data,
            op_return_data=data,
            spent_by=item.get("spentTxId") or None,
        )

    return TxSummary(
        txid=payload.get("txid", ""),
        confirmed=confirmed,
        # Insight uses -1 for "not in a block yet"; a negative height is not one.
        block_height=height
        if confirmed and isinstance(height, int) and height >= 0
        else None,
        block_time=payload.get("blocktime") if confirmed else None,
        fee=_duffs(payload.get("fees")),
        # Dash is pre-segwit: size *is* vsize.
        vsize=int(payload.get("size", 0) or 0),
        inputs=tuple(spend(i) for i in payload.get("vin", [])),
        outputs=tuple(entry(o) for o in payload.get("vout", [])),
    )


def is_blockbook(url: str) -> bool:
    """Whether ``url`` is a Blockbook rather than an Insight endpoint.

    Told apart by the path, since every Blockbook serves its API under
    ``/api/v2`` and no Insight does.
    """
    return url.rstrip("/").endswith("/api/v2")


def parse_blockbook_addr(stats: dict) -> AddressInfo:
    """A Blockbook ``/address/<a>`` body as :class:`AddressInfo`.

    Blockbook gives amounts as integer strings in duffs, and leaves out a field
    that is zero — ``unconfirmedBalance`` included.
    """
    confirmed = int(stats.get("balance", 0) or 0)
    pending = int(stats.get("unconfirmedBalance", 0) or 0)
    txs = int(stats.get("txs", 0) or 0) + int(stats.get("unconfirmedTxs", 0) or 0)
    received = int(stats.get("totalReceived", 0) or 0)
    return AddressInfo(
        # As for Insight: an unconfirmed spend (negative pending) is use too.
        has_history=txs > 0 or received > 0 or pending != 0,
        confirmed=confirmed,
        pending=pending,
    )


def parse_blockbook_tx(payload: dict) -> TxSummary:
    """Parse one Blockbook transaction into the neutral :class:`TxSummary`.

    Pure, like :func:`parse_insight_tx`. Blockbook omits zero-valued fields, so
    an input with no ``vout`` spends output 0, and only a coinbase input has no
    ``txid``. ``spentTxId`` is reported on some endpoints and not on others;
    where it is missing the history listing infers the spend itself.
    """
    confirmed = int(payload.get("confirmations", 0) or 0) > 0
    height = payload.get("blockHeight")

    def spend(item: dict) -> TxEntry:
        prevout = item.get("txid")
        addresses = item.get("addresses") or []
        return TxEntry(
            value=int(item.get("value", 0) or 0),
            address=addresses[0] if addresses and item.get("isAddress") else None,
            txid=prevout,
            vout=int(item.get("vout", 0) or 0) if prevout else None,
            sequence=item.get("sequence"),
        )

    def entry(item: dict) -> TxEntry:
        hex_script = item.get("hex") or ""
        is_data = hex_script.startswith("6a")  # OP_RETURN
        data = None
        if is_data:
            try:
                data = decode_op_return(bytes.fromhex(hex_script))
            except ValueError:
                data = None
        addresses = item.get("addresses") or []
        return TxEntry(
            value=int(item.get("value", 0) or 0),
            address=addresses[0] if addresses and item.get("isAddress") else None,
            op_return=is_data,
            op_return_data=data,
            spent_by=item.get("spentTxId") or None,
        )

    return TxSummary(
        txid=payload.get("txid", ""),
        confirmed=confirmed,
        # -1 while in the mempool, where blockTime is the first-seen time.
        block_height=height
        if confirmed and isinstance(height, int) and height >= 0
        else None,
        block_time=payload.get("blockTime") if confirmed else None,
        fee=int(payload.get("fees", 0) or 0),
        vsize=int(payload.get("vsize") or payload.get("size") or 0),
        inputs=tuple(spend(i) for i in payload.get("vin", [])),
        outputs=tuple(entry(o) for o in payload.get("vout", [])),
    )


class EndpointDown(HostUnreachable):
    """An endpoint that answered with a 5xx: broken, not an answer about the address."""


class DashAdapter(HttpClient, UtxoTxBuilder):
    """ChainAdapter for Dash (legacy P2PKH): hold, balance, send, sweep."""

    chain = "DASH"
    asset = "DASH.DASH"
    # The DASH.DASH pool exists only on Maya — and THORChain answers an LP probe
    # for a pool it doesn't run with a 500, not a clean "no position" 404.
    lp_backends = ("maya",)
    # UtxoTxBuilder knobs: legacy transactions with legacy fee/dust sizing.
    witness_type = "legacy"
    script = P2PKH
    default_derivation = DEFAULT_DERIVATION
    account = ACCOUNT
    change_path = CHANGE_PATH
    network = "dash"

    def __init__(
        self,
        api_url: str | Sequence[str] = DEFAULT_DASH_NODES,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(timeout)
        urls = (api_url,) if isinstance(api_url, str) else tuple(api_url)
        if not urls:
            raise ValueError("need at least one endpoint URL")
        self._candidates = tuple(u.rstrip("/") for u in urls)
        self.api_url = self._candidates[0]  # the pinned endpoint
        self.bip39_passphrase = bip39_passphrase

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        # Deliberately NOT bitcoinlib's encoder: this stays the independent,
        # golden-vector-pinned path (test_dash.py cross-checks the registered
        # bitcoinlib network agrees with it before any signing).
        return derive_p2pkh_address(mnemonic, path, PREFIX_P2PKH, self.bip39_passphrase)

    # --- network via Insight or Blockbook; guarded by opt-in live tests ---

    def _on_any(self, op: Callable[[str], _T]) -> _T:
        """Run ``op(endpoint)`` on the pinned endpoint, moving on while it is down.

        Unlike :class:`~swapsack.net.FailoverHttpClient` the endpoints need not
        speak the same API, so the whole operation is retried rather than one
        URL — a history walk restarts from its first page on the next endpoint
        instead of mixing two explorers' cursors. "Down" is unreachable or a
        5xx; any other error status is an answer, and is raised.
        """
        start = self._candidates.index(self.api_url)
        ordered = self._candidates[start:] + self._candidates[:start]
        for index, base in enumerate(ordered):
            try:
                result = op(base)
            except (HostUnreachable, *TRANSIENT_ERRORS) as exc:
                if index + 1 == len(ordered):
                    raise
                # Host, never path: the path is one of the wallet's addresses.
                host, following = (
                    urlsplit(u).netloc for u in ordered[index : index + 2]
                )
                print(
                    f"note: {host} is down ({type(exc).__name__}), trying {following}",
                    file=sys.stderr,
                )
                continue
            self.api_url = base
            return result
        raise AssertionError("unreachable: _candidates is never empty")

    @staticmethod
    def _checked(resp: niquests.Response, base: str) -> niquests.Response:
        """``resp`` if it is an answer; :class:`EndpointDown` if it is a 5xx."""
        status = getattr(resp, "status_code", None)
        if isinstance(status, int) and status >= 500:
            raise EndpointDown(f"{urlsplit(base).netloc}: HTTP {status}")
        resp.raise_for_status()
        return resp

    def _json(self, base: str, suffix: str, **kwargs: object) -> Any:  # noqa: ANN401 (explorer JSON)
        return self._checked(self._get(f"{base}/{suffix}", **kwargs), base).json()

    def address_info(self, address: str) -> AddressInfo:
        def op(base: str) -> AddressInfo:
            if is_blockbook(base):
                return parse_blockbook_addr(self._json(base, f"address/{address}"))
            return parse_insight_addr(self._json(base, f"addr/{address}"))

        return self._on_any(op)

    def fetch_utxos(
        self, address: str, *, include_unconfirmed: bool = False
    ) -> list[Utxo]:
        """This address's spendable outputs; confirmed-only unless opted out.

        Dash implements no mempool replacement (deliberately, for InstantSend),
        so an unconfirmed Dash output cannot be RBF'd out from under a spend of
        it — and with a flat, generous fee rate there is no package-fee maths to
        do either (:meth:`cpfp_deficits` is the shared no-op). It stays opt-in
        all the same: a double-spend race is still lost by whoever the network
        settles against.
        """

        def op(base: str) -> list[Utxo]:
            if is_blockbook(base):
                # Blockbook: duffs as a string, and vout 0 left out.
                rows = self._json(base, f"utxo/{address}")
                return [
                    (x["txid"], int(x.get("vout", 0) or 0), int(x["value"]), x)
                    for x in rows
                ]
            rows = self._json(base, f"addr/{address}/utxo")
            return [(x["txid"], x["vout"], x["satoshis"], x) for x in rows]

        # Fail closed: an output counts as confirmed only if it says so.
        return [
            Utxo(txid=t, vout=n, value=v, address=address, confirmed=confirmed)
            for t, n, v, x in self._on_any(op)
            if (confirmed := x.get("confirmations", 0) > 0) or include_unconfirmed
        ]

    def fetch_balance(self, address: str) -> int:
        return self.address_info(address).confirmed

    def address_txs(self, address: str, *, limit: int = DEFAULT_TX_LIMIT) -> AddressTxs:
        """Every transaction this address takes part in, paged.

        Insight pages ``/addrs/<a>/txs?from=&to=`` by offset (a half-open
        window, capped server-side); Blockbook pages ``/address/<a>?details=txs``
        by page number. Either way the cursor is a position, and a position is
        only as stable as the list under it. Both sort **newest-first** (Insight
        probed against insight.dash.org, 2026-09-01), so a transaction arriving
        mid-walk is *prepended*: every later item shifts down one place, and the
        item at the page boundary is never fetched. That is a skip rather than
        a repeat, so the walker's dedupe cannot see it — and a skipped
        transaction may be the one that spent an output, which would then be
        reported as money you still have. Esplora's cursor is a txid and has no
        such problem; this one has to be defended.

        The reported total (Insight's ``totalItems``, Blockbook's ``txs`` and
        ``unconfirmedTxs``) is the defence: it moves exactly when the list does.
        A walk that sees it change starts over, and one that keeps racing
        returns what it has marked ``truncated`` rather than passing a history
        with a hole in it off as complete. Note this is not the stop condition —
        that stays "a page brought nothing new", because a fork that omits or
        miscounts the total must degrade to the old behaviour rather than end
        the walk early.
        """

        def op(base: str) -> AddressTxs:
            walked = AddressTxs(transactions=[])
            for _ in range(INSIGHT_WALK_ATTEMPTS):
                walked, raced = self._walk_address(base, address, limit)
                if not raced:
                    return walked
            return AddressTxs(transactions=walked.transactions, truncated=True)

        return self._on_any(op)

    def _walk_address(
        self, base: str, address: str, limit: int
    ) -> tuple[AddressTxs, bool]:
        """One pass over the address's pages, and whether the list moved during it.

        A method rather than a closure inside the retry loop, so the per-attempt
        state — the count the first page reported, and whether a later page
        disagreed with it — belongs to the attempt by construction.
        """
        fetch = self._blockbook_page if is_blockbook(base) else self._insight_page
        reported: list[object] = []
        raced = False

        def page(cursor: object | None) -> tuple[list[TxSummary], object | None]:
            nonlocal raced
            txs, total, following = fetch(base, address, cursor)
            if total is not None:
                if not reported:
                    reported.append(total)
                elif total != reported[0]:
                    # This attempt is already going to be thrown away, so end the
                    # walk here (cursor None) rather than paging on through an
                    # address that may have hundreds of pages.
                    raced = True
                    return [], None
            return txs, following

        return collect_pages(page, limit=limit), raced

    def _insight_page(
        self, base: str, address: str, cursor: object | None
    ) -> tuple[list[TxSummary], object | None, object | None]:
        """One Insight window: its transactions, the reported total, the next offset."""
        start = int(cursor or 0)
        body = self._json(
            base,
            f"addrs/{address}/txs",
            params={"from": start, "to": start + INSIGHT_TX_PAGE},
        )
        total = body.get("totalItems")
        items = body.get("items", [])
        return (
            [parse_insight_tx(item) for item in items],
            total if isinstance(total, int) else None,
            start + len(items),
        )

    def _blockbook_page(
        self, base: str, address: str, cursor: object | None
    ) -> tuple[list[TxSummary], object | None, object | None]:
        """One Blockbook page: its transactions, the reported total, the next page."""
        number = int(cursor or 1)
        body = self._json(
            base,
            f"address/{address}",
            params={"details": "txs", "page": number, "pageSize": BLOCKBOOK_TX_PAGE},
        )
        total = (body.get("txs"), body.get("unconfirmedTxs", 0))
        pages = int(body.get("totalPages", 0) or 0)
        return (
            [parse_blockbook_tx(t) for t in body.get("transactions", [])],
            total if total[0] is not None else None,
            # No totalPages: page on, and let the nothing-new stop end it.
            number + 1 if number < pages or not pages else None,
        )

    def fetch_fee_rate(self, target_blocks: int = 2) -> float:  # noqa: ARG002
        """A conservative flat duffs/vB rate (see DEFAULT_FEE_RATE)."""
        return DEFAULT_FEE_RATE

    def wallet_balance(self, mnemonic: str, account: str = ACCOUNT) -> BalanceReport:
        from swapsack.chains.scan import wallet_balance_from_scan

        return wallet_balance_from_scan(
            derive_address=lambda p: self.derive_address(mnemonic, p),
            probe=self.address_info,
            account=account,
            symbol="DASH",
        )

    def broadcast(self, raws: list[str]) -> str:
        """Send each signed transaction, falling over like the reads do.

        Sending one to a second endpoint after the first failed is safe: a
        signed transaction has the same txid wherever it lands. But the first
        may have relayed it before failing (a proxy's 502 after the node took
        it), so a refusal as already known is the broadcast having worked,
        and a failure after a transient one may have sent it all the same —
        said with the txid, since a blind re-run spends other outputs and
        pays twice.
        """
        txid = ""
        for raw in raws:
            txid = self._broadcast_one(raw)
        return txid

    def _broadcast_one(self, raw: str) -> str:
        # Pre-segwit: the txid is the double SHA-256 of the whole raw.
        local = hashlib.sha256(hashlib.sha256(bytes.fromhex(raw)).digest()).digest()
        local_txid = local[::-1].hex()
        uncertain = False

        def op(base: str) -> str:
            nonlocal uncertain
            if is_blockbook(base):
                url, kwargs = f"{base}/sendtx/", {"data": raw}
            else:
                url, kwargs = f"{base}/tx/send", {"json": {"rawtx": raw}}
            try:
                resp = self._post(url, **kwargs)
                status = getattr(resp, "status_code", None)
                if isinstance(status, int) and status >= 400:
                    if "already" in (getattr(resp, "text", "") or "").lower():
                        return local_txid
                resp = self._checked(resp, base)
            except (HostUnreachable, *TRANSIENT_ERRORS):
                uncertain = True
                raise
            body = resp.json()
            sent = body.get("result" if is_blockbook(base) else "txid")
            if not sent:
                raise BroadcastError(f"{urlsplit(base).netloc} refused: {body}")
            return sent

        try:
            return self._on_any(op)
        except Exception as exc:
            if not uncertain:
                raise
            raise BroadcastError(
                f"{exc} — but an endpoint failed in a way that may still have "
                f"relayed it: check `swapsack status {local_txid}` before retrying"
            ) from exc
