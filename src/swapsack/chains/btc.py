"""Bitcoin chain adapter, backed by bitcoinlib for HD keys, signing and OP_RETURN.

The build/gate/sign machinery lives in :mod:`swapsack.chains.utxo` (shared with
the legacy-P2PKH chains); this module adds the BTC specifics: bech32 (P2WPKH)
derivation and the Esplora-shaped UTXO / balance / fee / broadcast layer.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from bitcoinlib.mnemonic import Mnemonic

from swapsack.chains.base import AddressInfo, BalanceReport, TxEntry, TxSummary
from swapsack.chains.coins import Utxo, cpfp_deficit, decode_op_return
from swapsack.chains.history import DEFAULT_TX_LIMIT, AddressTxs, collect_pages
from swapsack.chains.utxo import UtxoTxBuilder
from swapsack.net import FailoverHttpClient

DEFAULT_ESPLORA = "https://blockstream.info/api"
# The public Esplora is best-effort and drops a few percent of requests (see
# HttpClient._get). Reads retry, but a user hitting it repeatedly wants to know
# the endpoint is swappable — any Esplora-compatible instance will do.
DEFAULT_ESPLORA_MIRROR = "https://mempool.space/api"
# Tried in order, and the one that answers is pinned. Both are public,
# best-effort explorers run by different operators; measured 2026-08-28,
# blockstream.info black-holed ~1 request in 20 while mempool.space answered
# every one. Two is enough to survive that; naming one with --esplora turns
# the fallback off, since an endpoint you chose is not one to be second-guessed.
DEFAULT_ESPLORA_NODES = (DEFAULT_ESPLORA, DEFAULT_ESPLORA_MIRROR)
ESPLORA_HINT = (
    "hint: these public endpoints are best-effort. Any Esplora-compatible one "
    "works: --esplora https://your-instance/api (or $SWAPSACK_ESPLORA)"
)
# A stalled Esplora read never recovers and a healthy one answers in under a
# second, so reads give up well before a write does and let the retry run.
ESPLORA_READ_TIMEOUT = 8.0
DEFAULT_DERIVATION = "m/84'/0'/0'/0/0"  # receive chain, first index
ACCOUNT = "m/84'/0'/0'"
CHANGE_PATH = "m/84'/0'/0'/1/0"  # internal (change) chain, first index


def generate_mnemonic(strength: int = 128) -> str:
    """Generate a fresh BIP39 mnemonic (128 bits of entropy = 12 words)."""
    return Mnemonic().generate(strength)


def parse_address_info(stats: dict) -> AddressInfo:
    """Parse a single Esplora ``/address`` response."""
    chain = stats.get("chain_stats", {})
    mem = stats.get("mempool_stats", {})
    confirmed = chain.get("funded_txo_sum", 0) - chain.get("spent_txo_sum", 0)
    pending = mem.get("funded_txo_sum", 0) - mem.get("spent_txo_sum", 0)
    has_history = chain.get("tx_count", 0) > 0 or mem.get("tx_count", 0) > 0
    return AddressInfo(has_history=has_history, confirmed=confirmed, pending=pending)


def parse_tx_summary(payload: dict) -> TxSummary:
    """Parse an Esplora ``/tx/<txid>`` response into a :class:`TxSummary`.

    Pure (no I/O) so it can be unit-tested against a recorded response, like
    ``parse_address_info``.
    """
    status = payload.get("status", {})
    # Esplora reports weight; vsize is weight/4 rounded up.
    weight = payload.get("weight")
    vsize = -(-weight // 4) if weight else payload.get("size", 0)

    def entry(item: dict) -> TxEntry:
        is_data = item.get("scriptpubkey_type") == "op_return"
        data = None
        if is_data and (script := item.get("scriptpubkey")):
            try:
                data = decode_op_return(bytes.fromhex(script))
            except ValueError:
                data = None  # an OP_RETURN we cannot read; nothing to rebuild from
        return TxEntry(
            value=item.get("value", 0),
            address=item.get("scriptpubkey_address"),
            op_return=is_data,
            op_return_data=data,
        )

    def spend(item: dict) -> TxEntry:
        """An input: the prevout being spent, plus the outpoint naming it."""
        return dataclasses.replace(
            entry(item.get("prevout") or {}),
            txid=item.get("txid"),
            vout=item.get("vout"),
            sequence=item.get("sequence"),
        )

    return TxSummary(
        txid=payload.get("txid", ""),
        confirmed=bool(status.get("confirmed")),
        block_height=status.get("block_height"),
        block_time=status.get("block_time"),
        fee=payload.get("fee", 0),
        vsize=vsize,
        inputs=tuple(spend(i) for i in payload.get("vin", [])),
        outputs=tuple(entry(o) for o in payload.get("vout", [])),
    )


class BtcAdapter(FailoverHttpClient, UtxoTxBuilder):
    """ChainAdapter for Bitcoin (native segwit / P2WPKH)."""

    chain = "BTC"
    asset = "BTC.BTC"
    # UtxoTxBuilder knobs (P2WPKH sizing is its default script)
    witness_type = "segwit"
    default_derivation = DEFAULT_DERIVATION
    account = ACCOUNT
    change_path = CHANGE_PATH

    def __init__(
        self,
        esplora_url: str | Sequence[str] = DEFAULT_ESPLORA_NODES,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
        network: str = "bitcoin",
        read_timeout: float = ESPLORA_READ_TIMEOUT,
    ) -> None:
        super().__init__(
            esplora_url,
            timeout=timeout,
            read_timeout=read_timeout,
            hint=ESPLORA_HINT,
        )
        self.bip39_passphrase = bip39_passphrase
        # bitcoinlib network name: "bitcoin" (mainnet) or "testnet"/"signet".
        # Set alongside a matching testnet Esplora URL to spend on a testnet.
        self.network = network

    @property
    def esplora_url(self) -> str:
        """The endpoint currently pinned — what a broadcast will be sent to."""
        return self.base_url

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        return self._hdkey(mnemonic, path).address(
            script_type="p2wpkh", encoding="bech32"
        )

    # --- network via Esplora; covered by manual/integration testing, not units ---

    def address_info(self, address: str) -> AddressInfo:
        """History + confirmed/pending balance from a single /address call."""
        resp = self._get_with_fallback(f"address/{address}")
        resp.raise_for_status()
        return parse_address_info(resp.json())

    def fetch_utxos(
        self, address: str, *, include_unconfirmed: bool = False
    ) -> list[Utxo]:
        """This address's spendable outputs; confirmed-only unless opted out.

        ``include_unconfirmed`` (the CLI's ``--allow-unconfirmed``) also returns
        mempool outputs, marked ``confirmed=False`` so the fee maths can price
        their parents — see :meth:`cpfp_deficits`.
        """
        resp = self._get_with_fallback(f"address/{address}/utxo")
        resp.raise_for_status()
        # Fail closed: an output counts as confirmed only if it says so (L1).
        return [
            Utxo(
                txid=x["txid"],
                vout=x["vout"],
                value=x["value"],
                address=address,
                confirmed=confirmed,
            )
            for x in resp.json()
            if (confirmed := x.get("status", {}).get("confirmed", False))
            or include_unconfirmed
        ]

    def cpfp_deficits(self, utxos: list[Utxo], fee_rate: float) -> list[Utxo]:
        """Fill in ``ancestor_deficit`` for the unconfirmed inputs among ``utxos``.

        One ``/tx`` call per distinct mempool parent, priced against the same
        ``fee_rate`` the child will be built at. Depth 1 only: if the parent is
        itself spending unconfirmed money, its own ancestors' shortfall is not
        counted, so the package can still land under target. Bitcoin's
        ancestor/descendant limits (25 txs, 101 kvB) bound such chains anyway;
        a deep one is a sign to wait rather than to build on.
        """
        deficits: dict[str, int] = {}
        for utxo in utxos:
            if utxo.confirmed or utxo.txid in deficits:
                continue
            parent = self.fetch_tx(utxo.txid)
            if parent is None:
                raise RuntimeError(
                    f"cannot price the unconfirmed parent {utxo.txid}: this chain "
                    "has never seen it — refusing to guess its fee"
                )
            deficits[utxo.txid] = (
                0
                if parent.confirmed  # mined between the scan and now: nothing to lift
                else cpfp_deficit(parent.fee, parent.vsize, fee_rate)
            )
        return [
            dataclasses.replace(u, ancestor_deficit=deficits[u.txid])
            if u.txid in deficits
            else u
            for u in utxos
        ]

    def fetch_balance(self, address: str) -> int:
        return self.address_info(address).confirmed

    def address_txs(self, address: str, *, limit: int = DEFAULT_TX_LIMIT) -> AddressTxs:
        """Every transaction this address takes part in, newest first.

        Esplora answers ``/address/<a>/txs`` with the mempool transactions plus
        the first page of confirmed ones (25 on both default instances), and
        ``/address/<a>/txs/chain/<txid>`` continues from a **confirmed** txid.
        Paging from a mempool txid would restart the walk, so the cursor is
        always the last confirmed transaction of the page.

        The page size is not assumed: the walk ends when a page brings nothing
        new (see :func:`~swapsack.chains.history.collect_pages`), which costs
        one extra request per address and survives an instance that pages
        differently. Guessing the size instead would stop the walk early — and
        an unseen spend reads as money still sitting there.
        """

        def page(cursor: object | None) -> tuple[list[TxSummary], object | None]:
            suffix = f"address/{address}/txs"
            if cursor:
                suffix = f"{suffix}/chain/{cursor}"
            resp = self._get_with_fallback(suffix)
            resp.raise_for_status()
            txs = [parse_tx_summary(item) for item in resp.json()]
            confirmed = [tx.txid for tx in txs if tx.confirmed]
            return txs, (confirmed[-1] if confirmed else None)

        return collect_pages(page, limit=limit)

    def fetch_tx(self, txid: str) -> TxSummary | None:
        """What a broadcast tx did, or None if this chain has never seen it."""
        resp = self._get_with_fallback(f"tx/{txid}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return parse_tx_summary(resp.json())

    def wallet_balance(self, mnemonic: str, account: str = ACCOUNT) -> BalanceReport:
        from swapsack.chains.scan import wallet_balance_from_scan

        return wallet_balance_from_scan(
            derive_address=lambda p: self.derive_address(mnemonic, p),
            probe=self.address_info,
            account=account,
            symbol="BTC",
        )

    def fetch_fee_rate(self, target_blocks: int = 2) -> float:
        resp = self._get_with_fallback("fee-estimates")
        resp.raise_for_status()
        estimates = resp.json()
        # Fall back to the *highest* known rate, never the cheapest/slowest (M2).
        return float(estimates.get(str(target_blocks)) or max(estimates.values()))

    def broadcast(self, raws: list[str]) -> str:
        txid = ""
        for raw in raws:
            resp = self._post(f"{self.esplora_url}/tx", data=raw)
            resp.raise_for_status()
            txid = resp.text.strip()
        return txid
