# Code review — 2026-07-08 (follow-up on the 2026-07-07 fixes)

> **✅ RESOLVED (2026-07-08).** A second high-effort review, scoped to the
> commits that fixed [`code-review-2026-07-07.md`](code-review-2026-07-07.md),
> found that three of those fixes introduced correctness regressions and that
> one of them acted on a mistaken premise in the original review. All 9 findings
> are addressed (suite: 396 passed). Commits:
> - **0/1** `ec65036` — RUNE LP on Maya restored
> - **4** `ec65036` — dust=0 withdraw guard reverted
> - **3** `ec65036` — sub-base-unit floor fixed
> - **2/6/5/7** `a136ba8` — local native guard, quote pin, docstring
> - **8/9** `af2d27d` — DERIVABLE_CHAINS dict, keystore warning at CLI boundary

**Scope:** `git diff 1d1eb08..HEAD` — the fix commits from the previous review.
**Effort:** high — 4 finder angles + cleanup, independent per-location
verification. Two live checks against `mayanode.mayachain.info` settled the
findings that turned on real network behaviour.

---

## Regressions the previous round's fixes introduced

### 1. `lp_pools = False` on the shared CosmosAdapter hid RUNE LP positions on Maya
`src/swapsack/chains/cosmos.py` (findings 0/1)

The 2026-07-07 fix for "balance probes pools that can't exist" set
`lp_pools = False` on the shared `CosmosAdapter`, which `ThorAdapter` inherited.
But the premise "a settlement asset has no pool of itself" only holds on the
asset's **home** network: Maya runs a live `THOR.RUNE` pool (verified:
`/mayachain/pools` lists `THOR.RUNE`). So a RUNE LP position on Maya — exactly
what the two-sided-liquidity work enables — silently vanished from `balance`.

**Fix:** moved `lp_pools = False` to `MayaAdapter` only (CACAO is genuinely
pool-less — no `MAYA.CACAO` pool anywhere). `ThorAdapter` keeps the default
`True`, so `THOR.RUNE` is probed again.

### 2. The `dust_threshold <= 0` guard blocked every EVM LP withdraw
`src/swapsack/swap.py` (finding 4)

The 2026-07-07 fix for original finding #5 aborted a liquidity op when
`deposit_amount <= 0`, on the theory that a 0-value deposit is money lost. But
`dust_threshold = 0` is **legitimate** on EVM chains: Maya's
`/mayachain/inbound_addresses` reports `0` for ETH, ARB, KUJI, THOR, XRD, and a
0-value native tx carrying the memo is exactly how those chains trigger a
withdraw. The guard aborted every such withdraw, locking the position.

This also reveals the original finding #5 was **over-stated**: the danger is
confined to UTXO chains (which report a real nonzero dust), and only under a
malformed node response that drops the field — where a below-dust BTC output is
rejected by the network at broadcast, not silently lost.

**Fix:** reverted the guard. A withdraw trigger of 0 is correct on EVM chains.

### 3. Relaxing the amount floor let sub-base-unit amounts round up and send
`src/swapsack/cli.py` (finding 3)

The 2026-07-07 per-asset base-units fix dropped the parse floor to 1e-10 and
guarded only against amounts that round to **zero**. An amount in the
(0.5, 1) base-unit band — e.g. `0.000000006` BTC = 0.6 sat — passed the parse
floor and `ROUND_HALF_EVEN`'d **up** to 1 sat, sending ~1.67× what was typed,
where the old parse floor had rejected it.

**Fix:** `_base_units` now rejects amounts below one whole base unit, checked on
the *unrounded* product (per-asset restore of the old behaviour).

---

## Issues in the new code (not pre-existing)

### 4. The native-source guard did blocking I/O and had an uncaught-HTTP crash mode
`src/swapsack/swap.py` (findings 2, 6)

The new wrong-network guard called `thorchain.inbound_addresses()` on every
native RUNE/CACAO swap — a MsgDeposit that needs no vault data — adding a round
trip and a crash mode (`_swap_from_cosmos` catches `SwapAborted`/`ValueError`,
not HTTP errors; `main()` catches only `SwapAborted`). The CLI already
hard-pins the home backend two layers up, so the guard only ever mattered for a
direct library caller.

**Fix:** replaced the probe with a **local** identity check — the adapter's
`home_path_prefix` vs the client's `path_prefix`, no network call.

### 5. `quote` price-routed native sources; `swap` pinned them — inconsistent guidance
`src/swapsack/cli.py` (finding 5)

`cmd_quote` still quoted a native `--from RUNE/CACAO` across both backends
(Maya quotes `THOR.RUNE` via its pool), while `_swap_from_cosmos` hard-pins the
home backend. So `quote` could show a Maya route the `swap` command refuses.

**Fix:** `cmd_quote` pins native sources to the home backend and refuses an
explicit foreign `--backend`, mirroring the swap path.

### 6. Stale docstring after the guard change (finding 7)
`CosmosAdapter`'s docstring still said `prepare_swap` "skips the inbound-address
check" for native sources; updated to describe the home-network guard.

---

## Cleanups

### 7. `DERIVABLE_CHAINS` was still two parallel lists (finding 8)
The tuple and `_derive_destination_address`'s if-chain were separate; now both
come from one `{chain: deriver}` dict, so drift is impossible.

### 8. Keystore printed to stderr from the library layer (finding 9)
`keystore.py` was the only module outside `cli.py` doing `print`. `Keystore.load`
is now silent and records the stripped labels; a `_load_keystore` CLI helper
renders the warning, and every CLI load site routes through it.

---

## Note

This round is itself a good argument for the review-after-fix loop: three of the
eight 2026-07-07 fixes shipped a regression, two of them money-availability bugs
(hidden LP positions, locked withdraws) that unit tests written from the same
mistaken premises did not catch. The live `mayanode` checks — not the test
suite — are what settled them.
