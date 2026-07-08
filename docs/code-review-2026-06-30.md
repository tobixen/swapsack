# Code review — 2026-06-30

> **✅ RESOLVED (2026-06-30).** All 13 confirmed findings below have been fixed
> in commit `54f1432`, each with a regression test (suite: 240 passed). Notes:
> - **#1** fixed via the *storage + v1 version-gate* model: keystore bumped to v2;
>   a v1 keystore strips any stored BIP-39 passphrase on load, so existing
>   empty-passphrase wallets keep their addresses while new v2 wallets honour a
>   passphrase through derivation/signing.
> - **#2/#9** amounts are now parsed and scaled as `Decimal`, and amounts below
>   one base unit (1e-8) are rejected at parse time.
> - **#10/#11** the default tolerance constant moved to `thorchain.py` and is
>   threaded into backend selection.
>
> The findings are retained below as the historical record.

**Scope:** full code review of all code (whole tree treated as one changeset, `git diff 4b825dc6…HEAD`).
**Effort:** xhigh — 6 finder angles + cleanup, independent per-location verification.
**Tooling note:** the `sweep` and `synthesize` stages hit the session token limit and were skipped, so findings below are the **verified** set, ranked but unmerged (one duplicate pair merged by hand). 40 candidates verified → 14 kept (one was a duplicate of another), 11 refuted.

> `--comment` was requested but ignored: the target is the local tree, not a GitHub PR.

---

## Confirmed findings

### 1. BIP-39 passphrase is stored but never used in key derivation — funds misdirection
`src/swapsack/cli.py:118` (also `:331`, `:335`, `:339`)

`HdKey.passphrase` is persisted by `add-hd --bip39-passphrase` (cli.py:160) but `_load_mnemonic` returns only `entry.mnemonic.reveal()` and drops the passphrase. All three derivations (BTC `Mnemonic().to_seed`, ETH/TRON `Account.from_mnemonic`) then derive with an **empty** passphrase.

- **Balances:** show 0 / wrong for a passphrase-protected seed.
- **Swap signing & change:** signs from and pays change to addresses that don't belong to the user's real wallet.
- **Auto-derived `--dest`:** `_resolve_destination` derives the payout address from the empty-passphrase seed, so swap output is sent to an address the user cannot spend.

**Severity: critical — irreversible loss of funds.** Either thread the passphrase through every derivation path, or reject passphrase-protected seeds until support is complete.

### 2. Swap/send amounts round-trip through binary `float` — off-by-one base unit
`src/swapsack/cli.py:984` (consumed at `:380, :453, :541, :637, :725, :772`, …)

`_amount()` returns a `float`; every spend path computes `int(round(args.amount * THORCHAIN_UNIT))`. float64 holds only ~15–16 significant decimals, so a large amount like `--amount 93393106.59778857` yields `9339310659778858` base units instead of `9339310659778857`. The verify gate can't catch it because `plan.amount` and the tx output both derive from the same float, so the wrong-by-one amount is signed and broadcast. Money should never pass through float — parse to `Decimal`/integer base units directly.

### 3. `verify_eth_swap` case-folds the destination check instead of using `memo_pays_destination`
`src/swapsack/verify.py:354`

The gate does `plan.destination.lower() not in plan.memo.lower()`, lowercasing **both** sides, whereas the canonical `memo_pays_destination` only case-folds `0x` EVM addresses and does an exact match otherwise. For an ETH→non-EVM swap (TRON base58, BTC/LTC/BCH base58/bech32) a memo carrying a destination differing only by letter-case — i.e. a *different, case-corrupted* address — passes the ETH gate where the canonical helper would reject it. Replace the inline check with a `memo_pays_destination` call. *(This is the merged form of two confirmed candidates at the same location.)*

### 4. Non-sweep BTC swap: `InsufficientFunds` escapes uncaught → raw traceback
`src/swapsack/cli.py:562`

`prepare_swap → build_unsigned_swap → select_coins` raises `InsufficientFunds` (a `RuntimeError`, not `SwapAborted`), but the surrounding `try` at cli.py:575 catches only `SwapAborted`. `swap --from BTC --amount 0.5` with insufficient confirmed UTXOs dies with a Python traceback instead of a clean `ABORTED: have N sats, need …`. The `InsufficientFunds` import/handler only exists inside the `if sweep:` branch.

### 5. Non-sweep BTC add-liquidity: same uncaught `InsufficientFunds`
`src/swapsack/cli.py:847`

`add-liquidity --asset BTC --amount 1.0` with insufficient UTXOs: `prepare_liquidity → build_and_verify_deposit → select_coins` raises `InsufficientFunds`, not caught by the `except SwapAborted` at cli.py:860. Same fix as #4.

### 6. ETH broadcast JSON-RPC rejection is a bare `RuntimeError` — escapes `_confirm_and_execute`
`src/swapsack/cli.py:495` (raised at `chains/eth.py:250`)

`_confirm_and_execute` catches only `(BroadcastError, *HTTP_ERRORS)`, but `EthAdapter._rpc` raises a bare `RuntimeError` on a JSON-RPC `error` body (nonce too low, insufficient gas, intrinsic gas too low) — these come back as HTTP 200. `main()` has no top-level handler, so the CLI crashes instead of printing `BROADCAST FAILED: …`. **For a token swap this happens after the approve tx already broadcast**, leaving a dangling allowance and a confusing crash. Raise/catch `BroadcastError` here.

### 7. `EthAdapter._rpc` returns `payload['result']` with a bare subscript
`src/swapsack/chains/eth.py:251`

A non-conformant node returning `{'jsonrpc':'2.0','id':1}` (no `result`, no `error`) makes `payload.get("error")` falsy, then `payload["result"]` raises `KeyError`. `_rpc` feeds `get_nonce`/`fetch_fees`; the swap path catches only `SwapAborted`/`InsufficientFunds`, so this crashes with a traceback.

### 8. `parse_inbound_addresses` uses bare subscripts on optional fields
`src/swapsack/thorchain.py:249`

`entry["gas_rate"]`, `entry["gas_rate_units"]`, `entry["outbound_fee"]`, `entry["dust_threshold"]` (and `entry["chain"]`) are read with bare subscripts while the flag fields just below use `entry.get(...)`. A partial/degraded thornode response missing any one of these raises `KeyError` mid-swap-prep; the user sees a raw traceback instead of "chain not tradable".

### 9. Positive amount that rounds to 0 base units bypasses the positive-amount guard (plain BTC send)
`src/swapsack/cli.py:453`

`send --asset BTC --amount 0.000000001`: `_amount` accepts it (float > 0), then `int(round(1e-9 * 1e8)) == 0`. `build_and_verify_send` builds a 0-sat recipient output and `verify_btc_send` passes (`recipient_outs[0].value == plan.amount == 0`), so a degenerate 0-value send is signed and broadcast — fee burned, no payout. Unlike swaps, a plain send has no `recommended_min_amount_in` backstop. Guard the post-scaling base-unit amount, not just the float.

### 10. `_select_backend` omits `tolerance_bps` → `--tolerance-bps` silently ineffective on the auto path
`src/swapsack/cli.py:365`

With `--backend auto` (default), `_select_backend → gather_quotes` (backends.py:55) omits `tolerance_bps`, so THORChain applies its default tolerance. A small/high-fee swap the user enables by raising `--tolerance-bps` (e.g. 1500) is refused on both backends, `gather_quotes` returns `[]`, and `_select_backend` raises `SwapAborted('no swap backend can serve this pair/amount')` — even though `prepare_swap` would have succeeded with the raised tolerance. Thread `tolerance_bps` into the selection quotes. (Related to #11.)

### 11. `ThorchainClient.quote_swap` default tolerance disagrees with the `ThorchainLike` protocol
`src/swapsack/thorchain.py:375`

`quote_swap` defaults `tolerance_bps` to `None` (param omitted → server default), but the `ThorchainLike` protocol declares the default as `DEFAULT_TOLERANCE_BPS` (300). So `--backend auto` selects on the most-output backend at the server's *implicit* default, then `prepare_swap` re-quotes that backend at 300 bps — the re-quote can be refused ("emit asset less than price limit") even though selection succeeded, or the displayed `quote` differs from what the swap locks in. Make the default match the protocol.

### 12. Backend `ThorchainClient` sessions are never closed → `ResourceWarning` / leaked sockets
`src/swapsack/backends.py:55`

`gather_quotes` calls `backend.client.quote_swap`, lazily opening a niquests `Session` per `ThorchainClient`; neither `gather_quotes`, `_select_backend`, nor `cmd_quote` ever closes them. At interpreter shutdown this emits an unclosed-session `ResourceWarning` — and under the project's `filterwarnings=['error']`, any test on this path becomes a hard failure. In normal use, sockets for unchosen backends leak for the process lifetime. Use a context manager / `.close()`.

### 13. `decode_op_return` indexes `script[1]`/`script[2]` without a length guard (PLAUSIBLE)
`src/swapsack/chains/coins.py:62`

The guard at line 58 checks only emptiness and the first byte, so a length-1 nulldata script (`b"\x6a"`, bare `OP_RETURN`) passes, then `script[1]` raises `IndexError`. `_extract_outputs` runs `decode_op_return` on every nulldata output during verify-gate extraction, so a malformed/library-built script aborts extraction with a traceback rather than a clean reject. Verifier reproduced the `IndexError` directly; rated PLAUSIBLE because reachability with a real on-chain script is unproven.

---

## Refuted candidates (verified as non-issues)

| File:line | Claim | Why refuted |
|---|---|---|
| `chains/tron.py:321` | memo `bytes.fromhex(data).decode()` could raise `UnicodeDecodeError` | `data` only ever comes from tronpy's `builder.memo(memo)`, which round-trips the str we passed; always valid UTF-8 |
| `cli.py:394` | `cmd_quote` omits `tolerance_bps` | `quote` is informational; not a correctness defect (distinct from the swap-path #10) |
| `chains/eth.py:379` | `to_checksum_address(quote.router or "")` returns `0x` silently | guarded upstream; empty router doesn't reach a spend |
| `chains/eth.py:107` | `zip(..., strict=False)` truncates non-20-byte address | input length already validated before this point |
| `chains/base.py:50` | `ChainAdapter.broadcast` Protocol signature mismatches callers | Protocol is unused as a static gate; all concrete adapters agree |
| `chains/tron.py:122` | `_keyless_tron` hardcodes `network='mainnet'` | network field is cosmetic for the keyless path; endpoint URL governs behavior |
| `chains/eth.py:304` | sequential re-derive/refetch could be batched | perf nit, not a defect |
| `chains/eth.py:151` | native vs token fee computed two ways | both correct; verify binds the right surface per type |
| `chains/tron.py:78` | lazy per-call `tronpy.abi` import | style nit |
| `swap.py:255` | `now + 3600` expiry magic constant duplicated | cosmetic duplication |
| `cli.py:474` | duck-typed `getattr(plan,'expiry',None)` | intentional — `SendPlan` has no expiry by design |

---

## Suggested priority

1. **#1 (passphrase) and #2 (float money)** — both can irreversibly misdirect/missize funds. Fix before any real use.
2. **#3 (ETH destination case-fold)** — weakens the safety gate that exists specifically to prevent payout misdirection.
3. **#4–#9** — robustness: uncaught exceptions become raw tracebacks; #9 burns a fee on a no-op send.
4. **#10–#13** — correctness/UX of backend selection and resource hygiene.
