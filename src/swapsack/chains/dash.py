"""Dash chain adapter — Phase 1 (hold/balance) + Phase 2 (send/sweep).

Dash is a legacy (pre-segwit) P2PKH chain with no Blockstream Esplora; the
balance / UTXO / broadcast layer speaks to an Insight-API instance
(configurable — a single community explorer is a SPOF that can silently
under-report funds, see docs/dash.md). Swaps route through Maya only (no DASH
pool on THORChain).

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

from decimal import ROUND_HALF_UP, Decimal

from bitcoinlib.networks import NETWORK_DEFINITIONS

from swapsack.chains.base import AddressInfo, BalanceReport, TxEntry, TxSummary
from swapsack.chains.coins import P2PKH, Utxo, decode_op_return
from swapsack.chains.history import DEFAULT_TX_LIMIT, AddressTxs, collect_pages
from swapsack.chains.p2pkh import derive_p2pkh_address
from swapsack.chains.utxo import UtxoTxBuilder
from swapsack.net import HttpClient

DEFAULT_DASH_API = "https://insight.dash.org/insight-api"
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
        api_url: str = DEFAULT_DASH_API,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        super().__init__(timeout)
        self.api_url = api_url.rstrip("/")
        self.bip39_passphrase = bip39_passphrase

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        # Deliberately NOT bitcoinlib's encoder: this stays the independent,
        # golden-vector-pinned path (test_dash.py cross-checks the registered
        # bitcoinlib network agrees with it before any signing).
        return derive_p2pkh_address(mnemonic, path, PREFIX_P2PKH, self.bip39_passphrase)

    # --- network via Insight; guarded by an opt-in live test (test_dash.py) ---

    def address_info(self, address: str) -> AddressInfo:
        resp = self._get(f"{self.api_url}/addr/{address}")
        resp.raise_for_status()
        return parse_insight_addr(resp.json())

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
        resp = self._get(f"{self.api_url}/addr/{address}/utxo")
        resp.raise_for_status()
        # Fail closed: an output counts as confirmed only if it says so.
        return [
            Utxo(
                txid=x["txid"],
                vout=x["vout"],
                value=x["satoshis"],
                address=address,
                confirmed=confirmed,
            )
            for x in resp.json()
            if (confirmed := x.get("confirmations", 0) > 0) or include_unconfirmed
        ]

    def fetch_balance(self, address: str) -> int:
        return self.address_info(address).confirmed

    def address_txs(self, address: str, *, limit: int = DEFAULT_TX_LIMIT) -> AddressTxs:
        """Every transaction this address takes part in, via Insight's paged
        ``/addrs/<a>/txs?from=&to=`` (a half-open window, capped server-side).

        The cursor is the offset reached so far, and an offset is only as stable
        as the list under it. Insight sorts this endpoint **newest-first**
        (probed against insight.dash.org, 2026-09-01), so a transaction arriving
        mid-walk is *prepended*: every later item shifts down one place, and the
        item at the window boundary is never fetched. That is a skip rather than
        a repeat, so the walker's dedupe cannot see it — and a skipped
        transaction may be the one that spent an output, which would then be
        reported as money you still have. Esplora's cursor is a txid and has no
        such problem; this one has to be defended.

        ``totalItems`` is the defence: it moves exactly when the list does. A
        walk that sees it change starts over, and one that keeps racing returns
        what it has marked ``truncated`` rather than passing a history with a
        hole in it off as complete. Note this is not the stop condition — that
        stays "a page brought nothing new", because a fork that omits or
        miscounts ``totalItems`` must degrade to the old behaviour rather than
        end the walk early.
        """
        walked = AddressTxs(transactions=[])
        for _ in range(INSIGHT_WALK_ATTEMPTS):
            walked, raced = self._walk_address(address, limit)
            if not raced:
                return walked
        return AddressTxs(transactions=walked.transactions, truncated=True)

    def _walk_address(self, address: str, limit: int) -> tuple[AddressTxs, bool]:
        """One pass over the address's pages, and whether the list moved during it.

        A method rather than a closure inside the retry loop, so the per-attempt
        state — the count the first page reported, and whether a later page
        disagreed with it — belongs to the attempt by construction.
        """
        reported: list[int] = []
        raced = False

        def page(cursor: object | None) -> tuple[list[TxSummary], object | None]:
            nonlocal raced
            start = int(cursor or 0)
            resp = self._get(
                f"{self.api_url}/addrs/{address}/txs",
                params={"from": start, "to": start + INSIGHT_TX_PAGE},
            )
            resp.raise_for_status()
            body = resp.json()
            total = body.get("totalItems")
            if isinstance(total, int):
                if not reported:
                    reported.append(total)
                elif total != reported[0]:
                    # This attempt is already going to be thrown away, so end the
                    # walk here (cursor None) rather than paging on through an
                    # address that may have hundreds of pages.
                    raced = True
                    return [], None
            items = body.get("items", [])
            return [parse_insight_tx(item) for item in items], start + len(items)

        return collect_pages(page, limit=limit), raced

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
        txid = ""
        for raw in raws:
            resp = self._post(f"{self.api_url}/tx/send", json={"rawtx": raw})
            resp.raise_for_status()
            txid = resp.json()["txid"]
        return txid
