# Core review — follow-up (latest changes)

*Date: 2026-06-28. Reviewer: Claude Opus 4.8 via Claude Code, on behalf of tobixen.
Scope: the five commits `830820a..f3731a8` since the first review (`66c8c6d`).
State: `pytest` 94 passed (was 88), `ruff check` clean, `httpx` fully removed.*

Follows up on `docs/core-review.md`. Short version: **the fixes are real and
correctly done, with tests.** Three of them carried extra scope or subtlety worth
recording. Nothing here blocks; N1–N3 are the only items I'd actually act on.

---

## Verification of the previous findings

| ID | Finding | Status | Notes |
|----|---------|--------|-------|
| **H1** | Non-atomic keystore write | ✅ Fixed, tested | `_atomic_write` (keystore.py:191) — mkstemp in same dir, `chmod 0600`, `flush`+`fsync`, `os.replace`, temp cleanup on `BaseException`. Correct. Test asserts no temp left behind. |
| **M1** | Gate didn't confirm memo pays *you* | ✅ Fixed, tested | `plan.destination` added to both `SwapPlan`/`EthSwapPlan`; both gates assert it appears in the memo. Reject+accept tests for BTC and ETH. See N4 for a nuance. |
| **A1** | HTTP boilerplate ×4 | ✅ Fixed | `net.HttpClient` base class; BTC/ETH/TRON/Thorchain all inherit. ~80 lines of dup gone. But it also swapped httpx→niquests — see N2. |
| **M2** | Fee fallback chose cheapest | ✅ Fixed | `max(estimates.values())` (btc.py:249). Correct direction now. |
| **L1** | UTXOs failed open | ✅ Fixed | `confirmed` defaults to `False` (btc.py:220). Fails closed. |
| **A4** | `prepare_*_swap` duplicated | ✅ Fixed | One `prepare_swap` + adapter `build_and_verify`; swap.py 283→135 lines, the two ad-hoc protocols collapsed to one `SwapSource`. See N3. |

Deferred items (M3, L2, A2, A3, A5, A7) are tracked in `docs/TODO.md` — fine to
leave; not re-raised here.

This is a clean, well-tested round. The net diff *removed* ~550 lines while adding
coverage. Good.

---

## New observations from these changes

### N1 — niquests Session shared across the concurrent scan — ✅ resolved

`scan_account` probes a window of addresses via `ThreadPoolExecutor`
(`scan.py:48`), all calling `BtcAdapter.address_info` → the **one** lazily-created
`niquests.Session` on the adapter (`net.py:24`). The concern was that
requests-style `Session` objects historically are *not* thread-safe.

Resolved by research (2026-06-28): niquests **documents its `Session` as
thread-safe** (built on urllib3-future, which lists thread-safety as a feature),
so the shared session under the concurrent scan is fine. No change needed.

### N2 — niquests is a deliberate cross-project standard; footprint is the tradeoff to own

A1 was "stop copy-pasting the client lifecycle" — `net.py` does that. The
underlying httpx→niquests swap is independent scope, and per the maintainer it's
a **deliberate cross-project default** (see `~/caldav/docs/source/http-libraries.rst`
and the 2026-06-28 research): httpx is stagnant (no release since Nov 2024, hence
the httpxyz/httpx2 forks), requests is feature-frozen, niquests is the chosen
go-to. That rationale is sound; record it in `net.py`'s docstring so the choice
isn't mistaken for accident.

Two things still worth owning here, *specific to this being a hot wallet*:

- **Largest transitive footprint of the options**: niquests pulls
  `urllib3-future`, `jh2`, `qh3`, `wassima`, plus `urllib3`/`charset-normalizer`
  — for a tool whose only HTTP need is a handful of sync REST GET/POSTs that gain
  nothing from HTTP/2/3, multiplexing or async. For a wallet, minimising attack
  surface is a first-class concern; this is the one project where "fewer,
  more-audited deps" (requests, or stdlib) is a real counter-argument. Keeping
  niquests for cross-project consistency is still defensible — just a conscious
  trade, not a free win.
- **Behavioural surface changed**: broadcast went from httpx `content=raw_hex` to
  niquests `data=raw_hex` (btc.py:252), and `ThorchainClient` lost `base_url=` so
  every call now prepends `self.base_url` + passes `headers=` manually
  (thorchain.py). All correct on read, but the **BTC broadcast body** is on the
  untested live path — confirm against a real Esplora node before trusting it.

### N3 — The unified orchestrator traded static typing for runtime checks

`prepare_swap(**build_kwargs: object)` and `SwapSource.build_and_verify(**kwargs:
object)` are untyped passthroughs, and `Prepared.built`/`.plan` are now `object`
(were `BuiltSwapLike | EthBuiltLike`). A misspelled or missing chain-specific
kwarg (`change_address`, `nonce`, `max_fee_per_gas`, …) is now a **runtime
`TypeError` at swap time** rather than something a type-checker would catch.

Mitigated by dry-run-by-default and the test suite, so a bad call can't silently
broadcast. But the safety net moved from static → runtime on the money path.
Cheap improvement: a typed per-chain build-params dataclass passed as one arg,
keeping `build_and_verify` signatures explicit while still funnelling through one
`prepare_swap`. Low priority; flagging the tradeoff, not demanding a revert.

### N4 — M1 check is case-insensitive: right for ETH hex, slightly loose for TRON base58

Both gates do `plan.destination.lower() not in plan.memo.lower()`
(verify.py:107, 162). Lower-casing both sides can never cause a **false
rejection**, so it's safe. But base58 (Tron) addresses are case-sensitive, so for
Tron destinations this is marginally weaker than an exact, case-sensitive
membership test (it would take an astronomically unlikely lowercase collision to
matter). For hex/EIP-55 (ETH) case-insensitive is exactly right.

Two cheap hardenings, since this is *the* anti-loss check:
1. Use a case-sensitive `in` for base58-address chains (case-insensitive only
   where the address encoding is case-insensitive).
2. Sanity-check against a **live** quote that THORChain embeds the destination
   *verbatim and untruncated* in the memo for every destination type — the gate's
   correctness depends on that being true.

### N5 — Watch memo length vs the 80-byte OP_RETURN as USDT destinations land (WIP)

For token destinations the asset string in the memo is long
(`ETH.USDT-0XDAC17…EC7`, `TRON.USDT-TR7…`), inflating memo size. The BTC gate
already rejects memos over 80 bytes, so an over-long memo **fails closed** (the
swap aborts, no loss) — but it means some `BTC → *.USDT` swaps may be blocked by
OP_RETURN size. Not a bug; a limitation to keep in mind when wiring USDT
destinations from BTC. (USDT is WIP, so out of scope for scoring.)

---

## Bottom line

The fixes land correctly and are tested; the refactor is a net simplification.
The only things I'd genuinely follow up on:

1. **N1** — confirm niquests Session thread-safety under the scan (or per-thread sessions).
2. **N2** — record *why* niquests, and verify the BTC broadcast `data=` body live.
3. **N4** — case-sensitive memo check for base58 chains + one live-memo sanity check.

N3 and N5 are notes, not asks.
