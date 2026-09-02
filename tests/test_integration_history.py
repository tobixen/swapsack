"""Opt-in integration tests for the `history`/`utxos` listings, against live
explorers (read-only, no funds moved, no keystore).

These exist because the listings rest on a claim that **no offline fixture can
check**: that a spent output needs no per-output "outspends" query, since an
output paying an address can only be spent by a transaction that also appears
in that address's own history. That is an assertion about how real explorers
paginate and index, not about our code, so the only honest test compares our
answer against the explorer's *own* UTXO endpoint — the one authority
independent of the inference being tested.

**The two halves prove different things**, and it is worth being exact about
which. Esplora never reports who spent an output, so `spent_by` on the BTC side
comes purely from the local inference and the comparison tests it directly.
Insight *does* report `spentTxId`, and `wallet_history` believes a source that
reports a spend over its own inference (`out.spent_by or spenders.get(...)`) —
so the DASH comparison mostly checks Insight's `/txs` against Insight's
`/utxo`, and what it proves for us is that the *paging* reached everything.
That is worth having, because Insight is the source whose numeric offset can
skip a transaction; it is just not a second test of the inference.

Nothing here pins a specific address. A pinned one goes stale (spent down to
nothing, or grown past `--limit`) and then passes vacuously; these sample live
addresses instead and compare against whatever the explorer says right now.
The trade is flakiness for honesty, handled by skipping — never failing — when
an explorer is unreachable, is throttling, or the sample turns up nothing
suitable. Per `.github/workflows/integration.yml` these are a signal, not a
merge gate.

Excluded by default; run with `uv run pytest -m network`.
"""

import time

import niquests
import pytest

# Both adapters pull in bitcoinlib, whose first import emits a SQLAlchemy
# deprecation that `filterwarnings = ["error"]` would turn into a collection
# error. Mirrors the other bitcoinlib-backed test modules.
pytest.importorskip("bitcoinlib")

from swapsack.chains.btc import BtcAdapter  # noqa: E402
from swapsack.chains.dash import INSIGHT_TX_PAGE, DashAdapter  # noqa: E402
from swapsack.chains.history import wallet_history  # noqa: E402
from swapsack.net import HTTP_ERRORS  # noqa: E402

pytestmark = pytest.mark.network

ESPLORA = "https://blockstream.info/api"
INSIGHT = "https://insight.dash.org/insight-api"

# Busy enough to force the walk past its first page, small enough to finish
# well inside the default --limit so the result is not `truncated` (a truncated
# walk is *expected* to disagree with the explorer, so it proves nothing here).
MIN_TXS = 30
MAX_TXS = 400


def _get(url, params=None, tries=4):
    """JSON from a public explorer, or None if it will not answer.

    A deep walk draws rate limiting, and these endpoints go down; neither is a
    defect in this wallet, so both end as a skip rather than a red build.
    """
    for attempt in range(tries):
        try:
            resp = niquests.get(url, params=params, timeout=30)
        except niquests.exceptions.RequestException:
            return None
        if resp.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError:
            return None
    return None


def _skip_unless(value, why):
    if value is None:
        pytest.skip(why)
    return value


def _btc_sample(limit=60):
    """A recent mainnet address with several pages of history, or None."""
    # /blocks/tip/hash answers with a bare hash, not JSON, so it cannot go
    # through _get.
    try:
        resp = niquests.get(f"{ESPLORA}/blocks/tip/hash", timeout=30)
    except niquests.exceptions.RequestException:
        return None
    if resp.status_code != 200:
        return None
    txids = _get(f"{ESPLORA}/block/{resp.text.strip()}/txids")
    if not txids:
        return None
    for txid in txids[1 : limit + 1]:
        tx = _get(f"{ESPLORA}/tx/{txid}")
        if not tx:
            continue
        for out in tx.get("vout", []):
            address = out.get("scriptpubkey_address")
            if not address:
                continue
            stats = _get(f"{ESPLORA}/address/{address}")
            if not stats:
                continue
            # Esplora's first page carries *all* mempool transactions plus 25
            # CONFIRMED ones, so only the confirmed count says whether the
            # cursor will be exercised. Bounding on the sum would admit an
            # address of 20 confirmed + 10 unconfirmed, which fits in page one
            # and would still satisfy a "more than 25 transactions" guard.
            confirmed = stats["chain_stats"]["tx_count"]
            if MIN_TXS <= confirmed <= MAX_TXS:
                return address, confirmed
    return None


def _dash_sample(limit=12):
    """A recent Dash address with more than one page of history, or None."""
    blocks = _get(f"{INSIGHT}/blocks", {"limit": 10})
    if not blocks:
        return None
    for meta in blocks.get("blocks", []):
        block = _get(f"{INSIGHT}/block/{meta['hash']}")
        if not block:
            continue
        for txid in block.get("tx", [])[1 : limit + 1]:
            tx = _get(f"{INSIGHT}/tx/{txid}")
            if not tx:
                continue
            for out in tx.get("vout", []):
                for address in (out.get("scriptPubKey") or {}).get("addresses") or []:
                    page = _get(f"{INSIGHT}/addrs/{address}/txs", {"from": 0, "to": 1})
                    if not page:
                        continue
                    count = page.get("totalItems") or 0
                    if INSIGHT_TX_PAGE + 5 <= count <= MAX_TXS:
                        return address, count
    return None


def _walk(adapter, address):
    """The listing's view of one address, as `history`/`utxos` build it."""
    return wallet_history(
        records=[("m/0'/0/0", address)], address_txs=adapter.address_txs
    )


def _assert_unspent_matches(history, utxo_url, label, address, count):
    """Our unspent set must equal the explorer's, allowing for the address moving.

    The walk and this fetch happen at different moments, and the sampler
    deliberately picks busy addresses, so a payment or spend landing in between
    would flip the comparison for reasons that are nothing to do with the
    wallet. On a mismatch the explorer is asked a second time: if its *own*
    answer moved, the address moved under the test and there is nothing to
    conclude. A disagreement that survives a stable second read is real, and
    fails.
    """
    first = _skip_unless(_get(utxo_url), f"{label} would not answer")
    ours = {(o.txid, o.vout) for o in history.unspent}
    theirs = {(u["txid"], u["vout"]) for u in first}
    if ours == theirs:
        return
    again = _skip_unless(_get(utxo_url), f"{label} would not answer")
    if {(u["txid"], u["vout"]) for u in again} != theirs:
        pytest.skip(f"{address} changed while under test; nothing to conclude")
    raise AssertionError(
        f"{address} ({count} confirmed txs): spend inference disagrees with "
        f"{label} — only ours {sorted(ours - theirs)[:5]}, "
        f"only theirs {sorted(theirs - ours)[:5]}"
    )


def _walk_or_skip(adapter, address, label):
    """`_walk`, but a throttling or absent explorer skips instead of erroring.

    The sampling helper `_get` has its own backoff, but the walk goes through
    the adapter's HTTP client, which retries transport failures only and hands
    an HTTP 429 straight back to `raise_for_status`. Without this the file's
    "skip, never fail" promise would hold for the cheap half of the work and
    break on the expensive half — the deep walk, which is precisely where a
    public explorer starts throttling.
    """
    try:
        return _walk(adapter, address)
    except HTTP_ERRORS as exc:
        pytest.skip(f"{label} would not serve the whole walk: {type(exc).__name__}")


# Sampling and walking are the expensive part — dozens of requests against a
# public explorer — so each chain does it once for the whole module rather than
# once per test. That also means every test below examines the same address,
# so a failure in one is directly comparable with a pass in another.
@pytest.fixture(scope="module")
def btc_walk():
    address, count = _skip_unless(
        _btc_sample(), "no suitable BTC address in the recent block"
    )
    with BtcAdapter(ESPLORA) as adapter:
        return address, count, _walk_or_skip(adapter, address, "Esplora")


@pytest.fixture(scope="module")
def dash_walk():
    address, count = _skip_unless(
        _dash_sample(), "no suitable DASH address in recent blocks"
    )
    with DashAdapter(INSIGHT) as adapter:
        return address, count, _walk_or_skip(adapter, address, "Insight")


def test_btc_unspent_set_matches_esplora_live(btc_walk):
    """The whole correctness claim, checked against Esplora's own answer.

    Every output the walk calls unspent must be one Esplora also lists as an
    unspent output, and vice versa. A disagreement in either direction is a
    wrong number about money: an extra output means reporting spent coins as
    spendable, a missing one means hiding funds the wallet holds.
    """
    address, count, history = btc_walk
    assert not history.truncated, f"{address} outgrew --limit; sample bounds are wrong"
    _assert_unspent_matches(
        history, f"{ESPLORA}/address/{address}/utxo", "Esplora", address, count
    )


def test_btc_walk_really_pages_live(btc_walk):
    """Guards the test above from passing vacuously.

    Esplora returns 25 *confirmed* transactions per page, so an address under
    that never exercises the cursor at all — and the paging is where a walk
    silently loses a spend. `count` is the confirmed count for exactly this
    reason; the mempool ones all arrive on page one and prove nothing.

    The completeness check is `>=`, not `==`: a transaction arriving between
    the sample and the walk legitimately raises the total, while a walk that
    stopped early lands below it, which is the defect being guarded against.
    """
    address, count, history = btc_walk
    assert count > 25, "sampled an address that fits in one page"
    assert len(history.transactions) >= count, (
        f"{address}: walked {len(history.transactions)} of at least {count} "
        "transactions — the walk stopped early, which reads as an unspent output"
    )


def test_btc_every_listed_output_is_ours_live(btc_walk):
    """A listing must never attribute a stranger's output to the wallet."""
    address, _count, history = btc_walk
    assert history.outputs, "an address with history produced no outputs"
    assert {o.address for o in history.outputs} == {address}
    assert all(o.path == "m/0'/0/0" for o in history.outputs)


def test_dash_unspent_set_matches_insight_live(dash_walk):
    """The same comparison on the other data source — but proving paging.

    Insight reports `spentTxId` itself and `wallet_history` prefers a
    source-reported spend to its own inference, so this is not a second test of
    the inference (see the module docstring). What it does test is that the
    walk reached everything: Insight pages by numeric offset over a
    newest-first list, which is the arrangement where a transaction arriving
    mid-walk shifts the window and a spend can be skipped — the race
    `DashAdapter.address_txs` watches `totalItems` for.
    """
    address, count, history = dash_walk
    if history.truncated:
        pytest.skip(f"{address} raced or outgrew --limit; nothing to compare against")
    _assert_unspent_matches(
        history, f"{INSIGHT}/addr/{address}/utxo", "Insight", address, count
    )


def test_dash_walk_really_pages_live(dash_walk):
    """As for BTC: an address inside one Insight window proves nothing."""
    address, count, history = dash_walk
    if history.truncated:
        pytest.skip(f"{address} raced or outgrew --limit")
    assert count > INSIGHT_TX_PAGE, "sampled an address that fits in one page"
    # `>=` for the same reason as the BTC guard: arrivals only add.
    assert len(history.transactions) >= count


def test_an_oversized_history_declares_itself_truncated_live(btc_walk):
    """The honest half: past `--limit` the listing must say it is INCOMPLETE
    rather than present a partial history as the whole one.

    Checked with a deliberately tiny limit so it needs no enormous address —
    the flag is what matters, not the size that triggers it.
    """
    address, _count, _history = btc_walk
    with BtcAdapter(ESPLORA) as adapter:
        history = wallet_history(
            records=[("m/0'/0/0", address)],
            address_txs=lambda a: adapter.address_txs(a, limit=5),
        )
    assert history.truncated == (address,)
    assert len(history.transactions) == 5
