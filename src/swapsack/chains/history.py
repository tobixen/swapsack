"""Wallet transaction and output history for the UTXO chains — pure assembly.

``wallet_history`` takes the used addresses a gap-limit scan found (see
:mod:`swapsack.chains.scan`) plus an injected ``address_txs`` fetch, and folds
them into two views of the same data:

* **transactions** — every transaction touching the wallet, netted: how much of
  it was ours going in, how much came back, who else was paid, and whether it
  carried an OP_RETURN memo (which is what makes it a swap deposit rather than
  a plain send).
* **outputs** — every output *paying us*, spent ones included, each carrying the
  derivation path of the address that owns it and, when spent, the txid that
  spent it.

**Spends are inferred locally, at no extra network cost.** An output paying our
address can only be spent by a transaction that takes an input from that same
address — and such a transaction is, by definition, in that address's history.
So the fetched set already names every spender, and no per-output "outspends"
call is needed. That inference has one precondition: the address history must be
*complete*. When a page limit cuts one short, the fetch says so
(:class:`AddressTxs`) and the result names that address in ``truncated``,
rather than reporting an output as unspent money on evidence it does not have.

The **other** bound on completeness is not checked here and cannot be: this
works from the addresses it is handed, which in practice are the ones
:func:`swapsack.chains.scan.scan_account` found before its gap limit ran out.
An address past that gap — or on a derivation path the wallet does not scan —
is invisible, along with its outputs and their spends, and unlike a truncated
walk nothing detects it. That is the wallet's long-standing address-discovery
contract (``balance`` sees exactly the same set); it is written down here
because these listings read as more absolute than that contract is.

No I/O here, so the netting and the linking are testable offline.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

from swapsack.chains.base import TxSummary
from swapsack.chains.scan import DEFAULT_WORKERS
from swapsack.net import RateLimited


@dataclasses.dataclass(frozen=True)
class AddressTxs:
    """One address's transactions, and whether the source ran out of pages.

    ``truncated`` is the honest half: a paging cap that cut the history short
    means an output with no spender *in this batch* may still have been spent,
    out of sight. The assembly propagates it rather than presenting the gap as
    money.
    """

    transactions: list[TxSummary]
    truncated: bool = False


@dataclasses.dataclass(frozen=True)
class Output:
    """One transaction output paying a wallet address — spent or not.

    This is the "UTXO listing" row. It is deliberately *not*
    :class:`swapsack.chains.coins.Utxo`: that one is spend-planning input (it
    only ever holds selectable, unspent outputs and carries coin-selection
    fields like ``ancestor_deficit``), whereas this is a report row that must
    also be able to describe money that is already gone.
    """

    txid: str
    vout: int
    value: int  # base units
    address: str
    path: str = ""  # derivation path of the owning address ("" if unknown)
    confirmed: bool = False
    block_height: int | None = None
    block_time: int | None = None
    spent_by: str | None = None  # the txid that spent it; None = still unspent

    @property
    def outpoint(self) -> str:
        return f"{self.txid}:{self.vout}"

    @property
    def spent(self) -> bool:
        return self.spent_by is not None


@dataclasses.dataclass(frozen=True)
class WalletTx:
    """One transaction touching the wallet, netted against the wallet's addresses.

    ``received`` and ``sent`` are gross, not offset: a swap deposit spends a
    whole UTXO and hands most of it back as change, and a listing that showed
    only the ``net`` would hide the size of the deposit that actually left.
    """

    txid: str
    confirmed: bool
    block_height: int | None
    block_time: int | None
    fee: int  # base units; what the transaction paid the miner
    received: int  # sum of this tx's outputs paying wallet addresses
    sent: int  # sum of this tx's inputs spending wallet outputs
    has_op_return: bool
    memo: bytes | None  # the OP_RETURN payload, if there is one and it decoded
    counterparties: tuple[str, ...]  # output addresses that are not ours

    @property
    def net(self) -> int:
        """What the wallet gained (positive) or lost (negative), fee included.

        The fee is already accounted for: it is the part of ``sent`` that came
        back neither to us nor to a counterparty.
        """
        return self.received - self.sent

    @property
    def outgoing(self) -> bool:
        return self.sent > 0


@dataclasses.dataclass(frozen=True)
class History:
    """The two views, plus what the assembly could not vouch for."""

    transactions: list[WalletTx]
    outputs: list[Output]
    # Addresses whose history the source cut short. Their unspent outputs are
    # not trustworthy: a spend may simply have fallen off the end.
    truncated: tuple[str, ...] = ()

    @property
    def unspent(self) -> list[Output]:
        return [o for o in self.outputs if not o.spent]

    @property
    def unspent_total(self) -> int:
        return sum(o.value for o in self.unspent)

    @property
    def spent_total(self) -> int:
        return sum(o.value for o in self.outputs if o.spent)


# How deep one address is walked before the listing gives up and says so. A
# wallet address with more history than this is pathological, but an explorer
# that keeps handing out pages must not be allowed to spin forever.
DEFAULT_TX_LIMIT = 500


def collect_pages(
    fetch_page: Callable[[object | None], tuple[list[TxSummary], object | None]],
    *,
    limit: int = DEFAULT_TX_LIMIT,
) -> AddressTxs:
    """Walk one address's paged history into an :class:`AddressTxs`.

    ``fetch_page(cursor)`` is called with ``None`` first and returns
    ``(transactions, next_cursor)``; the cursor is opaque, so Esplora's
    last-seen-txid and Insight's numeric offset share this one loop.

    Two stop conditions, and the second is the important one: a page that brings
    **nothing new** ends the walk. Without it an explorer that keeps replying
    with the same page — a cursor it does not understand, a mirror serving a
    stale index — would be paged forever.

    A third ends it early and says so: a walk over an address with real history
    is hundreds of requests, and every explorer it can reach throttling at once
    (:class:`~swapsack.net.RateLimited`, raised only after the failover and the
    ``Retry-After`` backoff have both been spent) is a *short* history, not a
    missing one. The pages already fetched are returned ``truncated``, which is
    what the listing renders as INCOMPLETE. Nothing else degrades: a host that
    never answered is no history at all, and must keep raising.
    """
    seen: set[str] = set()
    txs: list[TxSummary] = []
    cursor: object | None = None
    while True:
        try:
            page, cursor = fetch_page(cursor)
        except RateLimited:
            return AddressTxs(transactions=txs, truncated=True)
        new = [tx for tx in page if tx.txid not in seen]
        seen.update(tx.txid for tx in new)
        txs.extend(new)
        if len(txs) >= limit:
            return AddressTxs(transactions=txs[:limit], truncated=True)
        if cursor is None or not new:
            return AddressTxs(transactions=txs)


def _sort_key(tx: WalletTx) -> tuple[int, int]:
    """Newest first, with the mempool ahead of every mined block."""
    return (0, 0) if not tx.confirmed else (1, -(tx.block_height or 0))


def wallet_history(
    *,
    records: Sequence[tuple[str, str]],
    address_txs: Callable[[str], AddressTxs],
    max_workers: int = DEFAULT_WORKERS,
) -> History:
    """Assemble a :class:`History` from ``(path, address)`` records.

    ``address_txs(address)`` returns every transaction that address takes part
    in, as inputs or outputs. Addresses are fetched concurrently, since the
    bottleneck is per-address network latency — the same reason
    :func:`swapsack.chains.scan.scan_account` does.
    """
    owned = {address: path for path, address in records}
    addresses = list(owned)
    if not addresses:
        return History(transactions=[], outputs=[])

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fetched = list(pool.map(address_txs, addresses))
    truncated = tuple(
        address
        for address, batch in zip(addresses, fetched, strict=True)
        if batch.truncated
    )
    # A transaction paying two of our addresses comes back from both; keep one.
    txs: dict[str, TxSummary] = {}
    for batch in fetched:
        for tx in batch.transactions:
            txs.setdefault(tx.txid, tx)

    # Every spender of one of our outputs is itself in this set (see the module
    # docstring), so one pass over the inputs names them all.
    spenders = {
        (i.txid, i.vout): tx.txid
        for tx in txs.values()
        for i in tx.inputs
        if i.txid is not None and i.vout is not None
    }

    transactions: list[WalletTx] = []
    outputs: list[Output] = []
    for tx in txs.values():
        memo = next(
            (o.op_return_data for o in tx.outputs if o.op_return and o.op_return_data),
            None,
        )
        counterparties = dict.fromkeys(
            o.address for o in tx.outputs if o.address and o.address not in owned
        )
        transactions.append(
            WalletTx(
                txid=tx.txid,
                confirmed=tx.confirmed,
                block_height=tx.block_height,
                block_time=tx.block_time,
                fee=tx.fee,
                received=sum(o.value for o in tx.outputs if o.address in owned),
                sent=sum(i.value for i in tx.inputs if i.address in owned),
                has_op_return=tx.has_op_return,
                memo=memo,
                counterparties=tuple(counterparties),
            )
        )
        for vout, out in enumerate(tx.outputs):
            if out.address not in owned or out.address is None:
                continue
            outputs.append(
                Output(
                    txid=tx.txid,
                    vout=vout,
                    value=out.value,
                    address=out.address,
                    path=owned[out.address],
                    confirmed=tx.confirmed,
                    block_height=tx.block_height,
                    block_time=tx.block_time,
                    # A source that reports the spend itself (Insight) is
                    # believed over the local inference: it also covers a
                    # spender that never reached this address's history.
                    spent_by=out.spent_by or spenders.get((tx.txid, vout)),
                )
            )

    transactions.sort(key=_sort_key)
    outputs.sort(key=lambda o: (0 if not o.confirmed else 1, -(o.block_height or 0)))
    return History(transactions=transactions, outputs=outputs, truncated=truncated)
