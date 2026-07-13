# Code review — 2026-07-13 (everything since v0.1.0)

**Scope:** `git diff v0.1.0..HEAD` — DASH/ZEC phases 1–3, the btc.py →
utxo.py/p2pkh.py refactor, the CoW Protocol backend, the THORChain
node-fallback fix, CI bumps.
**Effort:** high — 8 finder angles bundled into 4 parallel agents, findings
verified individually against the current source. One finder (removed-behavior
+ cross-file tracer) died mid-run on a session limit; its core checks were
redone inline (btc refactor extraction, thorchain fallback coverage, callers
of the changed dispatch) but less exhaustively than the other angles.

Verified clean along the way: `_get_with_fallback` covers every GET in
`ThorchainClient` (nothing still calls `self._get` with a pinned URL), and the
UTXO builder extraction preserves BTC fee/dust/sighash behavior.

**Difficulty legend** (for delegating fixes):
- **easy** — localized, mechanical, existing tests point the way; safe for a
  smaller model with the usual test-first discipline.
- **medium** — needs a design decision or touches money-safety code; review
  the diff carefully.
- **hard** — cross-cutting or needs live/API verification.

---

## Correctness

### 1. The CoW verify gate binds the sell amount to the API's own response — **easy**, highest value
`src/swapsack/cli.py:1369` (and the approval amount at `:1383`)

`CowOrderPlan.sell_amount` is set to `quote.sell_amount_total` — a value from
the quote response — and the ERC-20 approval amount is taken from the same
place. But the order being verified is *also* built from that response, so the
gate compares the API against itself. The design intent is documented in two
places and this code violates both: `verify.py`'s `CowOrderPlan` docstring
("``sell_amount`` is the user's requested amount … so a quote response lying
about any of them is caught here") and `cow.py`'s `sell_amount_total`
docstring ("asserted by the verify gate against the user's own amount").

**Failure:** a compromised/buggy orderbook inflates `sellAmount + feeAmount`
for a 100 USDT swap; plan, order and approval all inherit the inflated number,
`verify_cow_order` sees no mismatch, and with `--confirm --yes` the wallet
approves and signs an order selling far more than requested. Only the human
reading the printed `sell:` line stands in the way.

**Fix:** use the locally computed `sell_amount` from `cli.py:1347` for both
`plan.sell_amount` and the approval amount. Two lines. Write the test first: a
quote fixture whose `sellAmount + feeAmount` exceeds the requested amount must
make the gate emit a problem.

### 2. ZEC cannot pay `t3` addresses, and crashes with a traceback trying — stopgap **easy**, real fix **medium**
`src/swapsack/chains/zcash_tx.py:50` / `src/swapsack/addresses.py:30`

The ZEC recipient regex accepts `^t[13]…` (t1 P2PKH *and* t3 P2SH), but
`address_to_script` only builds P2PKH and raises `ZcashTxError` for t3.
`_send_utxo` (`cli.py:1030`) catches only `InsufficientFunds`, so
`swapsack send ZEC t3… 0.1` passes CLI validation and then dies with a raw
`ValueError` traceback deep in the builder. Paying any exchange or multisig
t3 address is impossible.

**Stopgap (easy):** catch `ZcashTxError` in `_send_utxo` (or validate t1-only
at the CLI for ZEC *sends* while still allowing t3 as a swap `--dest`) so the
user gets a clean `ABORTED:` line.
**Real fix (medium):** teach `address_to_script`/`script_to_address` P2SH
(`a9 14 <hash160> 87`), so t3 recipients actually work. Both functions must
learn it together or the neutral-extraction outputs decode to `address=None`
and the gate mis-reports. Needs tests against known t3 vectors.

### 3. `--backend cow` with a non-ETH source crashes with AttributeError — **easy**
`src/swapsack/cli.py:648` (`_select_backend`), crash surfaces at `:1139`

A single explicit backend is returned unquoted with **no `serves()` check**,
and only `_swap_from_eth` dispatches on `backend.executor`. The UTXO, TRON and
cosmos swap paths pass `backend.client` straight into `prepare_swap` as a
thornode. `swap --from BTC --to ETH --backend cow` therefore calls
`quote_swap` on a `CowClient` → AttributeError traceback.

**Fix:** in `_select_backend`, when a single explicit backend is chosen,
refuse it with `SwapAborted` if `not backend.serves(from_asset, to_asset)`.
(The thornode backends' `serves()` is permissive, so this only bites cow.)
Test: `--from BTC --backend cow` exits 1 with a clean message.

### 4. CoW order submitted while the approval tx is still unmined — **medium**
`src/swapsack/cli.py:1438`

When the vault-relayer allowance is short, the approval tx(s) are broadcast
and `submit_order` is called immediately after — same second, allowance still
0 on-chain. The orderbook validates balance/allowance at placement (the API
has `InsufficientAllowance`/`InsufficientBalance` rejections; `cow.py`'s own
docstring notes validation runs up to the balance check). First-time CoW users
with `--confirm` get: gas spent, a dangling exact-amount allowance, `ORDER
SUBMIT FAILED`, exit 1, no swap.

**Fix:** after broadcasting approvals, poll for the receipt (bounded, e.g.
60–120 s) before submitting; on timeout, print the order details and tell the
user to re-run (the allowance will then already be in place). Medium because
it adds a polling loop + UX for the timeout path, and the failure mode should
be verified against the real orderbook once.

### 5. A malformed 200 from the CoW API crashes `quote` and `swap --backend auto` — **easy**
`src/swapsack/cow.py:148` (`parse_cow_quote`), escape route via `:392` and
`backends.py:172`

`parse_cow_quote` indexes `payload["quote"]`, `quote["sellAmount"]`,
`payload["expiration"]` unguarded. `try_quote` catches only `CowError` and
`HTTP_ERRORS`; `gather_quotes` catches nothing. A 200 body missing those keys
(degraded API, proxy error page as JSON) raises KeyError out of every quote
path involving an ETH token pair.

**Fix:** widen `try_quote`'s except to `(CowError, KeyError, ValueError,
TypeError, *HTTP_ERRORS)` — or have `parse_cow_quote` re-raise structural
surprises as `CowError` (better: the CLI's `_swap_via_cow` then reports them
cleanly too). Test with a `{"ok": true}` payload.

### 6. `ChainAdapter.broadcast` protocol signature is wrong — **easy**
`src/swapsack/chains/base.py:63`

The protocol declares `broadcast(self, raw_hex: str)`; every implementation
(btc, dash, zcash, eth, cosmos, tron — and the `swap.py:143` protocol) takes
`raws: list[str]`. `runtime_checkable` ignores signatures, so nothing fails
today, but code written against the protocol passes a single hex string that
an adapter would iterate character by character.

**Fix:** change the annotation to `raws: list[str]`. One line.

### 7. Dash unconfirmed-count fallback misses the corrected spelling — **easy**
`src/swapsack/chains/dash.py:75`

Line 74 accepts both `txApperances` (sic) and `txAppearances`; line 75 reads
only `unconfirmedTxApperances`. Against an Insight fork using the corrected
spelling, an address whose only activity is an unconfirmed incoming tx shows
`has_history=False`, so the gap-limit scan can stop early and under-report
the wallet (and omit later addresses' UTXOs from sends/sweeps).

**Fix:** mirror line 74's double-`get`. Consider also counting
`unconfirmedBalanceSat != 0` toward `has_history` for belt-and-braces. One
line + a test fixture with the corrected spelling.

---

## Cleanups / structure

### 8. ZecAdapter duplicates the money-safety gate wrappers — **medium**
`src/swapsack/chains/zcash.py:361-472` vs `src/swapsack/chains/utxo.py:178-292`

`build_and_verify` / `build_and_verify_deposit` / `build_and_verify_send` are
near-verbatim copies of `UtxoTxBuilder`'s: same owned-address set, same
`SwapPlan`/`SendPlan` construction, same `expiry=now + 3600` LP fallback —
only the inner build call differs. Any gate-wiring fix now has six sites in
two modules; missing the zcash copies silently weakens the ZEC gate.

**Fix:** make the tx build an overridable hook under `UtxoTxBuilder`'s shared
wrappers and delete the zcash copies. Medium: it's a refactor of gated
money paths — lean on the existing zcash/dash/btc test suites.

### 9. `COW_ASSETS` restates eth.py's token registry — **easy**
`src/swapsack/cow.py:64` vs `src/swapsack/chains/eth.py:61`

The USDT/USDC contract addresses + decimals are hand-copied from
`TRACKED_TOKENS`, whose neighboring comment says "so the contract address is
listed once". Drift means CoW silently stops serving an asset or — worse —
quotes/approves against a stale contract or wrong decimals.

**Fix:** derive the ERC-20 entries of `COW_ASSETS` from `TRACKED_TOKENS`
(the key is mechanical: `f"ETH.{sym}-0X{contract[2:].upper()}"`), keep the
`ETH.ETH` sentinel entry by hand. Test: assert the derived keys match
`cli.ASSET` values.

### 10. Chain dispatch is scattered across 5+ sites; `lp_backends` only honored for DASH/ZEC — **medium**
`src/swapsack/cli.py:817` (`cmd_swap`), `:843` (`cmd_send`), `:1647`
(`_liquidity`), plus the cli-local `*_ACCOUNT`/`*_CHANGE_PATH` constants
(`:49-55`) restating the adapters' own `ACCOUNT`s

Adding DASH/ZEC extended three parallel if/elif chains and duplicated the
derivation paths into cli.py. Concretely wrong already: `_lp_backend_refused`
is invoked only inside the DASH and ZEC branches of `_liquidity`, so an
`lp_backends` restriction on any other adapter would be silently ignored; and
a cli.py path constant drifting from the adapter's `ACCOUNT` would scan/change
on the wrong derivation path.

**Fix:** one registry `{chain: (adapter_factory, account, change_path)}` —
account/change-path belong on the adapter class (it already carries
`default_derivation`) — and hoist the `_lp_backend_refused` check out of the
per-chain branches. Medium: mechanical but wide.

---

## Below the cap (all real, all small)

- **easy** `cli.py:602` — `_cow_tolerance` is a byte-copy of `_tolerance`
  differing only in the default; fold into `_tolerance(args, default=…)`.
- **easy** `chains/coins.py:39-41` — `P2WPKH_INPUT_VB` / `P2WPKH_OUTPUT_VB` /
  `DUST_P2WPKH` "compat aliases" have zero references in src or tests; delete.
- **easy** `wallet_balance` is copy-pasted across `btc.py:86`, `dash.py:143`,
  `zcash.py:256` (scan + sum + `BalanceReport`); extract a shared helper
  parameterized by symbol/pending.
- **easy (docs)** — stale docs contradict shipped behavior and must be folded
  before tagging a release:
  - `chains/dash.py:17` docstring still says swap-from "is not wired into the
    CLI";
  - `cli.py` `ASSET` comments still call ZEC "receive-only" and list DASH
    under "Destination-only";
  - `CHANGELOG.md` Unreleased simultaneously claims DASH/ZEC are receive-only
    (Phase 1 entries: "the spend path … is deliberately not implemented") and
    fully spendable (the Phase 2/3 entries above them), and the ZEC Phase 2
    entry ends "Swap-*from* remains Phase 3" while the entry above ships it.
    Per the changelog policy (net changes since last release), fold the
    per-phase entries into one final entry per chain.
- **medium** `cli.py:1345-1359` — `_swap_via_cow` re-implements `try_quote`'s
  scale/quote/parse sequence because `try_quote` swallows errors as `None`;
  a raising `quote_pair()` helper in cow.py that `try_quote` wraps would
  leave one copy. (Do together with finding 5 — same code.)

---

## Suggested order for the easy batch

Test-first on each (confirm the new test fails before the fix):

1. Finding 1 — CoW gate binding (two lines, biggest safety win)
2. Finding 6 — `broadcast` protocol annotation (one line)
3. Finding 7 — Dash spelling fallback (one line)
4. Finding 5 — CoW malformed-200 handling
5. Finding 3 — `serves()` check on explicit backend
6. Finding 2 stopgap — clean abort on ZEC t3
7. Finding 9 — derive `COW_ASSETS` from `TRACKED_TOKENS`
8. The below-the-cap items (aliases, `_cow_tolerance`, `wallet_balance`,
   docs/CHANGELOG fold)

Leave findings 4, 8, 10, the t3 real fix, and the `quote_pair` refactor for a
larger-model session.
