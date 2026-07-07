# Code review — 2026-07-07

**Scope:** changes since `8fd9e26` (everything after the 2026-06-30 review fixes: CACAO/RUNE
native support, shared Cosmos adapter, two-sided liquidity building blocks, testnet support).
**Effort:** high — 3 correctness finder angles + 1 cleanup angle, independent per-location
verification.
**Tooling note:** the first run died overnight on expired API credentials and was resumed from
cache; the final synthesize/merge stage hit the session token limit, so the list below is the
**verified** set, deduplicated by hand. 26 candidates verified → 26 confirmed, 0 refuted
(24 distinct locations).

---

## Correctness findings

### 1. v1→v2 keystore migration silently strips a stored BIP-39 passphrase — INTENTIONAL, needs a warning
`src/cryptoswap_wallet/keystore.py:206`

Loading a v1 keystore does `dataclasses.replace(e, passphrase=None)` on HD-key entries, and
`save()` always writes v2, so the next save permanently removes the passphrase from the
encrypted envelope with no message.

**Resolution: the strip is deliberate and stays.** Pre-v2 versions of the wallet *ignored* the
stored passphrase during derivation (finding #1 of the 2026-06-30 review), so any funds ever
received through a v1 keystore live at empty-passphrase addresses; keeping the stored
passphrase would silently *move* the wallet to different (empty) addresses on upgrade. The
only defect is the silence: the migration should print a warning telling the user what was
dropped and why. (Given zero releases and a single known user, arguably the migration is
overkill at all — but it exists, so it should speak up.)

### 2. Native RUNE/CACAO swap deposits on the adapter's own network even when the quote came from the other backend
`src/cryptoswap_wallet/chains/cosmos.py:279`

`CosmosAdapter.build_and_verify` always builds a `MsgDeposit` on the adapter's own chain and
ignores which backend produced the quote, but backend auto-selection can pick the *Maya*
backend for a THOR.RUNE source (Maya has a THOR.RUNE pool). `swap --from RUNE --to DASH`
selects maya (only Maya trades DASH), then broadcasts a **THORChain** MsgDeposit with a
Maya-priced memo `=:DASH.DASH:…` — THORChain has no DASH pool, so the deposit is refunded
minus the native fee (money lost). For pairs both networks serve, the deposit executes on the
wrong network at terms different from the confirmed quote. Neither `prepare_swap`'s
native-source bypass nor `verify_cosmos_deposit` checks that the chosen backend matches the
adapter's network.

**Severity: highest — direct loss of funds.**

### 3. `--stream-interval 0` silently drops all slippage protection
`src/cryptoswap_wallet/cli.py:1547`

`--stream-interval` uses `_nonneg_int` (accepts 0) although the help says ≥1, and every
downstream check is `is not None`. Interval 0 makes prepare_swap/gather_quotes treat the swap
as streaming (tolerance forced off, LIM=0) while the node returns a *non-streaming* memo with
limit 0. The verify gate binds that memo and the swap broadcasts with **zero** min-out
protection — a sandwich can take an arbitrary fraction of the funds.

### 4. Token deposit path hardcodes Ethereum chain id 1; BSC inherits it
`src/cryptoswap_wallet/chains/eth.py:448`, `src/cryptoswap_wallet/chains/bsc.py:34`

`_build_token_deposit` signs with the module constant `CHAIN_ID` (1) while the adapter now
carries `self.chain_id` and the plain-send/native paths honour it. An `EthAdapter` built with
`chain_id=11155111` (Sepolia testnet) produces token swap/LP txs validly signed for
**mainnet**; the verify gate compares against the same constant, so it passes. Related:
`BscAdapter.__init__` never passes `chain_id=56`, so the inherited
`build_and_verify_send`/`_build_and_verify_token_send` sign BSC sends with chainId 1 — the
node rejects them, and the emitted raw tx is a fully valid Ethereum-mainnet transaction paying
the same recipient in ETH.

### 5. Missing `dust_threshold` now defaults to 0 — withdraw-liquidity broadcasts a 0-value deposit
`src/cryptoswap_wallet/thorchain.py:304`

`parse_inbound_addresses` was relaxed from `entry["dust_threshold"]` to
`entry.get("dust_threshold", 0)`, but `prepare_liquidity` uses `status.dust_threshold` as the
deposit amount for withdrawals (`amount=None`) and no zero-amount guard exists downstream.
A degraded thornode entry yields a 0-value deposit that builds, verifies (the gate only checks
the tx matches the 0-amount plan) and broadcasts: on ETH it burns gas on a tx the vault
ignores; on BTC it produces a below-dust output rejected after signing. Previously this was a
loud `KeyError` before any tx was built.

### 6. Missing `inbound_address` defaults to `""` for external-chain sources too
`src/cryptoswap_wallet/thorchain.py:325`

`parse_quote` defaults a missing `inbound_address` to `""` (added for native RUNE/CACAO
quotes, which legitimately have none), but nothing re-checks it for external-chain sources.
A degraded node response omitting the field sends `""` into the adapters:
`to_checksum_address("")`, opaque crashes inside signing/output construction, or worse —
instead of the previous loud `KeyError` / a clean "malformed quote" abort.

### 7. Symmetric LP add memo breaks token-contract extraction in the ETH deposit path
`src/cryptoswap_wallet/chains/eth.py:717`

`build_and_verify_deposit` extracts the token contract as
`memo.split(":", 1)[1].split("-", 1)[1]`, assuming the memo ends right after the contract.
A symmetric add memo from `liquidity.symmetric_add_memo` (`+:ETH.USDT-0X…:<paired_addr>`)
makes `token_contract` include `:<paired_addr>`, so the token LP add crashes (garbage
`eth_call` / `to_checksum_address` ValueError) instead of building the approve+deposit pair.
This blocks exactly the wiring the new two-sided liquidity building blocks exist for.

### 8. CACAO-source quotes are requested for 1/100th of the typed amount
`src/cryptoswap_wallet/cli.py:582`

`cmd_quote` scales `--amount` with the fixed-1e8 `_base_units` for every source asset, but
CACAO's quote API unit is 1e10 (the swap path handles this via `10**adapter.decimals`).
`quote --from CACAO --amount 100` is quoted as 1 CACAO: the output prints "in: 100 CACAO" with
pricing/fees/bps for a 1-CACAO swap (~100× worse-looking), or a spurious "no backend can serve
this swap".

### 9. `quote` subcommand can no longer show a price when fees exceed 300 bps
`src/cryptoswap_wallet/thorchain.py:430`

`ThorchainClient.quote_swap`'s default `tolerance_bps` changed from `None` (no limit sent) to
`DEFAULT_TOLERANCE_BPS=300` (a deliberate 2026-06-30 fix for the *swap* path), but the `quote`
subcommand has no `--tolerance-bps` flag and doesn't thread one into `gather_quotes`. Any
small swap whose fees+slippage exceed 300 bps now gets "no backend can serve this swap"
instead of showing the (bad) price — and the error text points at a flag that only exists on
`swap`. The quote path should be informational: quote without a limit (or with a flag).

### 10. `_amount` rejects valid small CACAO amounts
`src/cryptoswap_wallet/cli.py:1489`

The parse-time minimum uses `THORCHAIN_UNIT` (1e-8), but CACAO's base unit is 1e-10.
`send --asset CACAO --amount 0.000000005` (50 base units, perfectly sendable) is refused at
argument parsing, where the asset isn't known yet.

### 11. `_send_tron` leaks a raw ValueError traceback on sub-precision amounts
`src/cryptoswap_wallet/cli.py:769`

`TronAdapter.to_sun`/`to_token_native` raise `ValueError` for amounts finer than TRX (1e6) or
token precision; `_swap_from_tron` catches `(SwapAborted, ValueError)` but `_send_tron` does
not, so `send TR7… --asset TRX --amount 0.0000001` dies with a traceback instead of the usual
`ABORTED:` message.

---

## Cleanup findings

### 12. Auto-derivable destination chains hardcoded twice, both lists stale
`src/cryptoswap_wallet/cli.py:584` (and `:402`)

`cmd_quote`'s `('BTC','ETH','TRON')` tuple and `_resolve_destination`'s if-chain both encode
"which chains can we derive a --dest for", and both omit MAYA/THOR although the adapters
expose `derive_address` — `swap --from BTC --to CACAO` demands a `--dest` the wallet prints in
`address`. Single source of truth, and add the Cosmos chains.

### 13. Stale CLI help strings
`src/cryptoswap_wallet/cli.py:1630`, `:1705`

`address` help says "BTC, ETH, BSC, TRON and MAYA" but the command also prints THOR; `send`
help says "BTC/ETH/TRON/CACAO" though RUNE send is implemented.

### 14. CHANGELOG and docs reference pre-rename symbol names
`CHANGELOG.md:42`, `docs/cacao.md:74,106,112,124`

`chains/maya_tx.py`, `verify_maya_send`, `verify_maya_deposit` don't exist — the shipped names
are `cosmos_tx.py`, `verify_cosmos_send`, `verify_cosmos_deposit`. Per the changelog policy
(net changes since last release only), collapse the intermediate rename instead of
documenting it.

### 15. `build_and_verify` / `build_and_verify_native_deposit` are ~40-line copy-paste twins
`src/cryptoswap_wallet/chains/cosmos.py:326`

They differ only in memo/destination/expiry/quote; a shared `_prepare_deposit` helper would
collapse them (and five repeated local `import cosmos_tx` statements could be one
module-level import — the module is pure).

### 16. Four `_send_*` handlers re-implement the same send skeleton
`src/cryptoswap_wallet/cli.py:677`

Recipient validation, "max" sweep branching, amount print, `_confirm_and_execute` — copied
with slight variations across `_send_eth`, `_send_tron`, `_send_cosmos`, `_send_btc`
(e.g. recipient check runs before adapter construction in two of them, inside it in another).

### 17. Decimal→base-units scaling written three times
`src/cryptoswap_wallet/cli.py:669`, `:1167`, `:1515`

`_send_cosmos` and `_swap_from_cosmos` inline the same
`int((amount * unit).to_integral_value(ROUND_HALF_EVEN))` expression `_base_units`
implements; generalize `_base_units` with a unit parameter.

### 18. Streaming-overrides-tolerance rule implemented twice
`src/cryptoswap_wallet/backends.py:68`, `src/cryptoswap_wallet/swap.py:189`

The "streaming forces LIM=0 / drops tolerance" policy must stay byte-equivalent in
`gather_quotes` and `prepare_swap` or backend selection quotes a different LIM than the
executed swap.

### 19. Token-LP-add detection string-sniffs the memo instead of receiving the asset
`src/cryptoswap_wallet/chains/eth.py:715`

`build_and_verify_deposit` infers "token add" from `memo.startswith("+") and "-" in memo`,
duplicating the detection cli.py:1336 already did on the asset. Pass an explicit
token/asset parameter. (Closely related to #7 — fixing them together makes sense.)

### 20. Token contract extracted from the asset string twice
`src/cryptoswap_wallet/chains/eth.py:641,643`

`_build_and_verify_token_send` splits `asset` once checksummed and once raw; reuse the first.

### 21. CACAO decimals live in two disconnected places, plus dead constants
`src/cryptoswap_wallet/thorchain.py:63`

`_ASSET_UNITS` hardcodes `MAYA.CACAO=1e10` independently of
`maya.CACAO_DECIMALS`/`MayaAdapter.decimals`, while `maya.CACAO_UNIT` and `thor.RUNE_UNIT`
are never used. Derive the map from the adapter constants; delete the dead ones.

### 22. `parse_spot` is dead production code
`src/cryptoswap_wallet/pricefeed.py:44`

`parse_prices` supersedes it; only tests call it. Delete both.

### 23. `cmd_balance` probes LP positions for pools that cannot exist
`src/cryptoswap_wallet/cli.py:319`

BSC assets (no pools on either network — the adapter documents this) and the settlement
assets CACAO/RUNE (no pool of themselves) are probed on both backends: ~10 guaranteed-404
HTTP round-trips per `balance` run, each with up to a 20 s timeout.

---

## Suggested priority

1. **#2 (wrong-network native deposit)** — direct loss of funds on a shipped path.
2. **#3 (stream-interval 0) and #4 (chain id)** — signed/broadcastable txs with the
   protection gates blind to the problem.
3. **#5, #6 (degraded-node defaults)** — restore loud failure on malformed node responses.
4. **#7/#19 (symmetric LP memo)** — blocks the two-sided liquidity work.
5. **#8–#11** — quoting/UX correctness.
6. **#1** — add the migration warning.
7. **#12–#23** — cleanups, riskiest refactors (#15, #16) last.
