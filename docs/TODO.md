# TODO

Only open work. Completed items live in `CHANGELOG.md` and the code; caveats that
outlived the work that created them are kept below as open risk.

## Next up (priority order)

Owner's requested goal (2026-08-16): **two-sided liquidity for `ETH.USDC` and
`ARB.USDC`.** Both are **Maya** pools paired with **CACAO** — THORChain LP is
globally paused (`PAUSELP=1`, and `PAUSELPDEPOSIT-ETH-USDC-…=1`, checked
2026-08-16), so its `ETH.USDC` pool is not an option and no THORChain/RUNE work
is on this path. Maya is open: `PAUSELP`/`PAUSELPETH`/`PAUSELPARB` are all `0`,
both pools are `Available`, and Maya publishes a router for both `ETH`
(`0xe3985E6b…`) and `ARB` (`0x700E97ef…`). **`ETH.USDC` is delivered and proven
on mainnet** (`docs/live-session-2026-08-16.md`); `ARB.USDC` is implemented but
has never broadcast on Arbitrum, which is item 1.

1. **Prove Arbitrum on mainnet before the first `ARB.USDC` add.**
   **`ETH.USDC` is done** — a real symmetric position was
   added on 2026-08-16, which also made the CACAO `MsgDeposit` and the two-leg
   orchestration mainnet-proven. Evidence:
   `docs/live-session-2026-08-16.md`. What remains for `ARB.USDC` is the one
   surface that is still untouched:

   - **Arbitrum.** The adapter is a `chains/bsc.py`-shaped subclass and its
     read paths are covered by live tests (balance decoding, and the chain id
     asked of the node rather than trusted), but **no ARB transaction has ever
     been signed and broadcast**. Do a minimal `send --asset ETH-ARB` first —
     and note the wallet holds **0 ETH on Arbitrum**, so it needs gas there
     before it can send anything at all. A `swap --from CACAO --to ETH-ARB`
     both funds that gas and rehearses `MsgDeposit`, and its floor is only
     ~4.31 CACAO because Maya's ARB outbound fee is 12× cheaper than ETH's.

   Do **not** substitute `send --asset CACAO` for a `MsgDeposit` rehearsal: it
   builds a `MsgSend`, a different protobuf message that shares none of
   `_prepare_deposit` with the LP leg. An earlier version of this item got that
   wrong; see the live-session note.

   Sourcing CACAO needs no new code — `swap --to CACAO --dest maya1…` works
   today, and at the 2026-08-16 ratio both USDC pools price CACAO at ~$0.113
   (~8.85 CACAO per USDC), so the pair leg is roughly dollar-for-dollar with the
   asset leg.

2. **Size the `ARB.USDC` position against its depth, not your intent.**
   Maya's `ARB.USDC` pool is ~8.9k USDC / ~78.7k CACAO (2026-08-16), against
   ~232k USDC / ~2.05M CACAO for `ETH.USDC`. A position large enough to matter
   makes you a dominant share of a pool that thin — that is where the
   impermanent loss lands, and what makes exiting expensive. Symmetric entry
   avoids *entry* slip; it does nothing about that. Re-measure rather than
   trusting this line; it is the one number here most likely to have moved.

3. **More swap *destinations* via external `--dest` addresses.** ATOM, XRP, ADA
   and ETH-ARB are done; **SOL is the only remaining candidate, and it is
   blocked** — `SOL.SOL` exists on THORChain but is halted (a live
   `BTC->SOL.SOL` quote returns "trading is halted, can't process swap"), the
   same shape as the BSC block below. Revisit when `pools` shows
   `trading_halted: false`, or reach it via Chainflip (see *Swap backends*).
   When it unblocks: SOL addresses are base58 with **no checksum at all** (a
   bare 32-byte ed25519 pubkey), so a `_RULES` shape rule is genuinely all there
   is — add `"SOL"` to `_NO_CHECKSUM` in `addresses.py`, which is what declares
   that as a decision rather than an oversight. A `_RULES` entry alone is
   *rejected* by the invariant test in `test_addresses.py`, deliberately: every
   chain must name a checksum strategy or explicitly say it has none.

   Two follow-ups this work exposed, neither blocking:
   - **ADA is usually unreachable from a UTXO source.** The Shelley *base*
     address wallets hand out is 103 chars, so the memo exceeds the 80-byte
     OP_RETURN — see *N5* below, which this made live. Not a blanket rule: the
     guard measures the actual address, and a shorter Shelley form (enterprise,
     58 chars) fits fine, so do not "simplify" it into a per-chain block. `--dest` now refuses it up front instead of letting
     it read as "no route". Reaching ADA from BTC would need a THORName (a
     registered short alias resolving to the address) — worth considering as a
     general memo-length escape hatch, not just for ADA.
   - **ATOM could be auto-derived rather than requiring `--dest`.** It is the
     same Cosmos derivation as `thor1`/`maya1` with a different HRP, but is
     deliberately `--dest`-only because the wallet cannot *spend* on Cosmos Hub;
     `RECEIVE_ONLY_CHAINS` in `cli.py` is the existing (empty) seam for exactly
     that — derive, but warn loudly that funds land somewhere only another
     wallet can spend. (**ARB no longer belongs here**: it is spendable now, so
     it is simply a `_DESTINATION_DERIVERS` entry with no warning needed.)

## Known bugs (found, diagnosed, not yet fixed)

Four surfaced during a real 2026-08-16 session
(`docs/live-session-2026-08-16.md`); **the two `balance` ones are fixed** (see
`CHANGELOG.md`), and the two below remain. Neither risks funds — both are
*reporting* defects over a money path that was correct — but each tells the user
something false about their money, which is why they sit above the feature work
rather than in *Other known gaps*.

(A third, found 2026-08-17 while explaining those: `withdraw-liquidity` could
not exit a symmetric position at all. That one is **fixed** — the trigger now
goes out from the CACAO side. See `docs/liquidity-symmetric.md`.)

### `status` reports a **completed** swap as "not observed"

`cli.py:2466` decides whether a backend saw the tx with:

```python
observed = status.get("stages", {}).get("inbound_observed", {}).get("started")
```

Thornode does not serialise `started` once the stage is done. Verified against
the live API (`/thorchain/tx/status/{hash}`) on 2026-08-16:

| hash | `stages.inbound_observed` |
|---|---|
| unknown | `{"started": false, "final_count": 0, "completed": false}` |
| observed + completed | `{"final_count": 93, "completed": true}` — **no `started`** |

So `.get("started")` is falsy in *both* cases, and the check fails exactly when
the answer is "yes, fully observed". The polarity is the worst available one:
`status` works while a swap is mid-flight and breaks once it succeeds, telling
the user their finished swap vanished.

With the default `--backend auto` it then falls through to the next backend and
prints *that* one's empty body — so a completed THORChain swap is reported using
Maya's answer, and Maya has never heard of the hash (TRON is THORChain-only).
The observed case: a TRX→ETH swap that had completed in 41 seconds still read as
"not observed" minutes later.

**Fix**: thornode returns a `tx` object only for hashes it knows — unknown hash
gives top-level keys `['stages']`, a known one gives
`['out_txs', 'planned_out_txs', 'stages', 'tx']`. Key the "this backend saw it"
decision off that instead of a stage flag whose absence is meaningful. Broadening
the stage test (`started or completed or final_count > 0`) also works but keeps
depending on which flags a given thornode version happens to emit — and Maya, an
older fork, still emits `started`, so the two node families must both be handled.
Worth a regression test built on both real response shapes, captured above.

### A tolerance rejection is only explained when **THORChain** phrases it

`_explain_quote_error` (`swap.py:43`) turns the "your swap costs more than your
slippage tolerance" rejection into an actionable message — send more, stream it,
or raise `--tolerance-bps`. It fires on `if "price limit" in msg`, which is
THORChain's wording. **Maya words the identical condition differently**, so on
`--backend maya` the user gets the raw node error and no guidance:

```
* outbound amount does not meet requirements (2323009/2326360)
```

The two numbers are `emitted/limit` in 1e8 — everything needed to say *how far
short* the user is, which the current message could not say even if it fired.

Observed 2026-08-16: `swap --from CACAO --to ETH --amount 400 --backend maya`
refused at the 300 bps default. The swap's real cost was **303 bps** — rejected
by three basis points, with nothing on screen to suggest that `--tolerance-bps
400` was the entire fix. (Confirmed against the live quote API: identical
`expected_amount_out` at 300/400/500/1000 bps; tolerance only sets the memo's
min-out limit, never the price obtained.)

**Fix**: match Maya's phrasing as well as THORChain's, and parse the
`(emitted/limit)` pair to name the shortfall and the tolerance that would clear
it. Do not match on the numbers alone — an `internal error` line accompanies it,
and the wrapper must not start explaining unrelated failures as slippage.

Second, smaller defect in the same string: the message is hardcoded
`"THORChain rejected the quote"` regardless of backend, so a Maya rejection is
attributed to THORChain. That is what the user sees while explicitly passing
`--backend maya`.

## Symmetric liquidity — the standing risk notes

`add-liquidity --symmetric` is implemented (EVM assets) and proven on mainnet
for `ETH.USDC`; the items above
are what is left. These are the general properties of a symmetric add, which
outlive any particular pool, and none of them are closed by having shipped the
code. A symmetric add is two *linked* deposits: the asset leg
(`+:POOL:<protocol-addr>` to the inbound vault) and a RUNE/CACAO leg (a Cosmos
`MsgDeposit` with memo `+:POOL:<asset-addr>`), paired by the protocol via the
cross-referenced addresses within a time window.

- **The partial-failure hazard is a property of the operation, not a bug the
  code removed.** Both legs are gated before either is broadcast and the
  protocol (cheap, fast) leg goes first, so the *avoidable* half is handled —
  but two irreversible txs on two chains can still end with one landed, and
  then the position is lopsided or stuck. `PartialSymmetricAdd` exists to name
  that outcome, not to prevent it.
- **Symmetric buys less than it looks like it does.** One-sided LP already
  carries ~50% RUNE/CACAO price exposure once the pool rebalances, so symmetric
  does not reduce your exposure to the settlement asset — it only avoids *entry
  slip*, in exchange for sourcing and holding RUNE/CACAO yourself. On a USDC
  pool that means half a nominally dollar-stable position is a small-cap
  protocol token either way; go in knowing that, not expecting stablecoin
  behaviour.
- **The asset leg's pairing address is unambiguous only for account-model
  chains.** ETH and ARB have a single derived sender. A UTXO source does not —
  the protocol observes `vin[0]` by convention, an assumption no testnet can
  verify for us, so `--symmetric` refuses UTXO chains outright. Reaching BTC
  would mean either constraining the add to spend from one address or reading
  `vin[0]` back off the built tx and betting on the convention; neither is
  worth doing without a way to test it.
- THORChain LP is paused (`PAUSELP=1`), so symmetric works on Maya (asset +
  CACAO) today and on RUNE only when THORChain re-enables it. See
  `docs/liquidity-symmetric.md`.

## Integration tests towards testnet / stagenet

- **Verify the funded broadcast loop actually RUNS in CI** (not just skips). A
  fresh signet seed + Sepolia account are set as CI secrets and are being funded
  (signetfaucet queued a payout on 2026-07-02; addresses in `docs/testnet.md`).
  Once the coins land: (a) confirm the balances arrived (`address_info` /
  `fetch_balance`), and (b) confirm the **Integration (network)** job reports
  `test_btc_testnet_send_broadcast` and `test_eth_sepolia_send_broadcast_and_confirm`
  as **PASSED, not skipped** — the tests skip when unfunded, so a green CI alone
  does NOT prove a real testnet tx was broadcast. Inspect the run log for the
  broadcast txids.
- **Sepolia token send** (USDT/USDC on Sepolia) — to testnet-cover the ERC-20
  send/swap path. This was recorded as blocked on "the token-swap gate bakes in
  mainnet `CHAIN_ID`"; it does not. `EthAdapter.__init__` takes `chain_id` and
  threads `self.chain_id` into every build, and `verify_eth_token_swap` /
  `verify_eth_approvals` compare against `built.chain_id`. The module-level
  `CHAIN_ID` survives only as a dataclass default. What is actually missing is
  Sepolia token *contracts* and a funded test account.
- **THORChain stagenet swaps** — a real cross-chain swap loop (deposit on one
  testnet, receive on another) needs a stagenet vault + memo, a bigger lift.
- Wire the testnet secrets into the CI **Integration (network)** workflow so the
  broadcast loops run there, not just locally.

## RBF — what the `bump` command still leaves open

`swapsack bump <txid>` shipped (see `CHANGELOG.md`): it rebuilds a signalling
mempool transaction byte-identically, takes the higher fee out of the change,
re-runs the verify gate, signs and rebroadcasts. These are the gaps it was
knowingly shipped with.

- **A sweep cannot be bumped at all.** `--amount max` leaves no change output,
  so there is nothing to take the bump from without pulling in another
  confirmed UTXO. That is implementable — select an extra input, rebuild with
  it — but it changes *what is being spent*, so it needs its own confirmation
  step rather than happening silently. Today it refuses and says why.
- **Nor can one whose change is under (dust + the BIP125 increment).** Same
  shape of fix. The refusal names the highest rate that would fit, or says
  outright that no rate does.
- **Only the one-recipient-plus-change shape** this wallet builds is
  recognised; anything else is refused rather than guessed at. Fine while the
  wallet is the only thing creating these transactions.
- **The gate cannot re-check the swap's intent.** It verifies the rebuild did
  not drift from the original's own outputs — same vault, same amount,
  byte-identical memo, change still ours, fee inside `--max-fee` — but the
  quote that authorised the deposit is gone by then, and the destination lives
  inside a memo `bump` does not parse. So a bump inherits the original's
  correctness. Parsing the memo back into a destination check
  (`memo_pays_destination`) would close this for text memos; Chainflip's binary
  payload already has `decode_vault_swap_payload`.
- **No `--force` past the BIP125 relay floor**, and no attempt to check
  Bitcoin's other replacement rules (notably rule 2: the replacement must not
  add new unconfirmed inputs — we never do — and rule 5: at most 100
  descendants evicted). A node rejecting the replacement is the backstop.
- **DASH and ZEC are out**, and stay out. Dash Core implements no mempool
  replacement (deliberate, for InstantSend); Dash's own answer to a stuck tx is
  InstantSend. The ZEC bespoke signer (`chains/zcash_tx.py`) hardcodes
  `sequence 0xffffffff` and Zcash has no standard mempool RBF — leave it unless
  a concrete need appears.

## Unconfirmed spending — what `--allow-unconfirmed` still leaves open

The flag and its child-pays-for-parent fee maths shipped (see `CHANGELOG.md`);
these are the gaps it was knowingly shipped with.

- **The CPFP surcharge looks one hop back.** A parent that is itself spending
  unconfirmed money has its own ancestors' shortfall uncounted, so the package
  can land under the targeted rate even though the wallet believes it paid for
  it. Walking the chain needs the parents' `vin` txids, which `TxSummary`
  currently drops.
- **Nothing counts against Bitcoin's ancestor/descendant limits** (25 txs,
  101 kvB). A deep enough chain gets the child rejected by mempool policy at
  broadcast, with no warning beforehand.
- **Nothing checks whether the parent is ours.** Spending our own unconfirmed
  change is safe; spending an external RBF-signalling parent is not — it can be
  replaced, which invalidates our child (benign: the spend never happens, no
  funds lost). Today the user gets one warning covering both. The wallet could
  tell them apart: a parent whose inputs are all wallet addresses is our own
  change. Needs input addresses from `/tx`, which `TxSummary` already carries.

## Carried over from the early core reviews

The review documents themselves were removed once obsolete (`f90d329`), so these
descriptions are now the only record — the letter/number codes are historical
labels, not lookups.

- **N5** — a swap memo from a UTXO source vs the 80-byte OP_RETURN limit. No
  longer hypothetical: a Cardano base address makes the memo 113 bytes, and the
  backend refuses the quote outright. `_dest_chain_caveats` in `cli.py`
  now refuses any `--dest` whose *shortest possible* memo would not fit, so the
  user gets the reason instead of "no quotes". Still open: an escape hatch that
  makes long destinations reachable from BTC at all (THORName aliases), and the
  original token-destination case (a `0x…` contract-qualified asset from BTC)
  has never been exercised — the check above bounds it, but nothing tests it.
- **A2/A3** — share the EVM key derivation + `to_checksum`/keccak helpers between
  ETH and **TRON**; default `wallet_balance` on an account-model base. Scope
  correction: the *EVM-to-EVM* half of this is already done — `EthAdapter`
  parameterizes `chain_id` and `chains/bsc.py` proves the subclass shape works,
  so BSC/ARB/AVAX do **not** wait on it. What is left is genuinely the TRON
  sharing, which no other item blocks. It is therefore no longer "the
  highest-leverage refactor here"; **A5 is** (see next), because a second
  spendable EVM chain multiplies the `chain == "ETH"` branches.
- **A5** — table-drive the CLI per-chain factories / `_resolve_destination` /
  `cmd_address` / `_swap_from_*`. **Partly done**: adding Arbitrum forced the
  EVM half, so `_EVM_ADAPTERS` now joins `_UTXO_ADAPTERS` and `cmd_send`,
  `cmd_swap` and `_liquidity` dispatch through it instead of branching on
  `chain == "ETH"`. What is left is the account-model stragglers that still
  have their own branches — TRON, MAYA, THOR — plus `cmd_address`, which still
  hand-lists every adapter (and so is the thing that silently forgets a new
  chain).
- **A7** — split `base.ChainAdapter` into `WalletChain` vs `SourceChain` (Tron is
  destination-only). The `swap.SwapSource` protocol already exists from A4.
- **C-list** — one `ThreadPoolExecutor` per scan; `quote` memo row alignment;
  note ETH/TRON balance only inspects index 0.

## Swap backends

- **Chainflip — B1 (quotes) and B2 (execution from BTC) both done
  2026-08-28.** What remains: a **mainnet broadcast** (everything short of it is
  covered by an opt-in network test that builds and gates a real unsigned tx);
  **a source other than BTC** — Chainflip is not Bitcoin-only, and neither is
  its vault-swap API: it covers EVM chains and Solana alongside Bitcoin, so
  ETH/ARB (a contract call into the Vault contract) and SOL (a program
  instruction) are reachable brokerlessly too. Neither reuses the UTXO builder
  or the gate that checks its outputs, so each is its own piece of work; a
  source the vault-swap API does not cover would need a deposit channel and a
  broker instead. Today every non-BTC source prices in `auto` and settles
  nowhere, and says so out loud when it was the cheaper route. A
  **`status` tracker** — `chainflip-swap.chainflip.io/v2/swaps/{id}` is keyless,
  but whether the id is derivable from our own BTC txid is untested; a
  **base58check decoder** so a Tron destination can be gated (Solana also needs
  the 60-byte payload variant); and `cf_swap_rate_v2/v3` as a node-native quote
  source, which would drop the hosted service as a dependency.
  Superseded detail, kept for the reasoning:
  `--backend chainflip`/`auto` price it; `swap` refuses to route there, and
  `auto` notes out loud when it was the cheaper route. Calldata-style
  aggregators (ParaSwap/1inch/0x/LiFi) and custodial instant exchangers: still
  not planned (gating problem / custody — and, measured 2026-08-28, the
  custodial ones lose on price too).
  **B2 is a vault swap, not a deposit channel**: `cf_request_swap_parameter_
  encoding` (keyless) returns an OP_RETURN payload carrying our own destination
  and an on-chain `min_output_amount` floor, paid to a vault that
  `cf_get_vault_addresses` confirms, and `cf_decode_vault_swap_parameter` reads
  every field back for the gate. No broker, no channel expiry. The order of
  work, from `docs/chainflip-effort.md`: **first** widen
  `build_unsigned_swap`'s `memo` from `str` to `bytes` as its own commit (the
  payload is binary SCALE; this touches the shared BTC/DASH/ZEC money path and
  can break three chains at once), **then** the gate and the CLI path. Note
  `--amount max` can never work here — Chainflip requires a non-zero,
  above-dust change output.
  **Priority raised 2026-08-28**: THORChain and Maya were halted simultaneously
  (Maya's $1.7M exploit on 08-18), leaving BTC→ETH with no route through this
  wallet at all. B1 — the keyless quote as a read-only source in `auto` — is
  small, carries no money-path risk, and is what would have let `quote` still
  answer. `docs/halt-alternatives.md` has the outage record, the live price
  comparison against custodial exchangers and CEX orderbooks (Chainflip wins by
  70–241 bps and ~40 bps respectively), and the manual stopgap.
  **`docs/chainflip-effort.md` (2026-08-28) sizes the work**: B1 ~1 session
  (~600 lines), B2 ~2–3 sessions (~800), together the order of the CoW commit.
  It also supersedes this bullet's broker premise — Chainflip **vault swaps**
  need no broker and no deposit channel, the destination is ours to encode and
  to read back, and the tx shape (pay vault / OP_RETURN / change) is the one
  `UtxoTxBuilder` already emits. Two corrections that came out of sizing it: the
  `python-urllib` 403 is a non-issue (niquests gets a 200), and
  `cf_*_open_deposit_channels` return **liquidity-provision** channels with no
  destination or expiry in them — `docs/chainflip.md`'s readback plan does not
  work as written.
- **Maya-only assets**: ADA and ETH-ARB are now exposed as destinations. Note
  what *isn't* there — the ARB **token** pool (`ARB.ARB`) is `Staged`, not
  tradeable, so "ARB" as a destination means native ETH on Arbitrum.
  CACAO's **full wallet side is done** — hold, balance, `send` (`MsgSend`) and
  swap-**from** (`MsgDeposit`) all ship via `chains/cosmos.py` +
  `chains/maya.py`. The `MsgDeposit` build/sign/broadcast is mainnet-proven
  (`docs/live-session-2026-08-16.md`); `MsgSend` has still never broadcast. (An earlier version of this line, and
  the status header of `docs/cacao.md`, called it "not started"; both were
  stale. `docs/cacao.md`'s own phasing section was correct.) So *Next up* item
  1's CACAO leg needs no new chain work. (CACAO needs `thorchain.asset_unit` to
  stay 1e10, not 1e8 — see `docs/cacao.md`.)
- **USDC on cheaper chains — the *destination* half is done** (`USDC-AVAX` via
  THORChain, `USDC-ARB` via Maya). What remains is **holding, spending or
  sourcing** them, which needs a per-chain EVM adapter (RPC, chain id, native
  coin, tracked tokens). This used to say "so do A2/A3 first rather than copy
  it per chain" — that is no longer the trade-off: `EthAdapter` already
  parameterizes `chain_id` and `chains/bsc.py` is a ~60-line subclass proving
  the seam, so the per-chain adapter *is* the shared code path. **ARB is now
  done** (`chains/arb.py`, ~50 lines of configuration, which is the evidence
  for that claim). Native AVAX remains unexposed: it needs an `ASSET` line plus
  the same adapter treatment, and `USDC-AVAX` LP needs THORChain's LP pause to
  lift, which it has not.

  Three findings from doing it, worth having before the adapter work:
  - **The premise "far cheaper than ETH mainnet" did not survive measurement.**
    On 2026-08-16 the flat outbound fee was ~0.25 USDC to ETH, ~0.25 to AVAX
    and ~0.12 to ARB, and total cost vs spot on a 0.001 BTC swap was 68/73/139
    bps respectively — i.e. the *swap* is not the expensive part. The real
    saving is what you do with the USDC afterwards (a transfer on ARB/AVAX
    costs cents), which only matters once we can spend there — the adapter
    work, not this. Do not re-justify the adapter with the swap-cost argument.
  - **Maya's `ARB.USDC` pool is thin** — ~8.9k USDC of depth, so a 0.01 BTC
    swap lost ~1269 bps against spot vs ~35 bps for the same swap to
    `USDC-ETH`. Nothing to fix in the code (the `Market:` line surfaces it),
    but it caps how useful `USDC-ARB` is at size, and is worth re-measuring
    rather than assumed stable. Re-measured 2026-08-16: ~8.9k USDC / ~78.7k
    CACAO, unchanged. It bounds the *LP* ambition too, not just swaps — see
    *Next up* item 2.
  - **BASE is blocked, not merely unimplemented**: `BASE.USDC` *and* `BASE.ETH`
    are `Available` but `trading_halted: true` on THORChain (checked
    2026-08-16), same shape as the BSC and SOL blocks. One `_RULES` line and an
    `_EVM_CHAINS` entry is still all it needs whenever the halt lifts.
- **BSC swaps are blocked — do not implement yet.** Hold + Balance work
  (`chains/bsc.py`), but THORChain has BSC `chain_trading_paused`/`halted` (a
  live `BTC->BSC.BNB` quote returns "trading is halted, can't process swap") and
  Maya has no BSC pools, so To/From/Sweep/Liq are unusable and untestable.
  `BscAdapter.build_and_verify` raises by design — because there is nothing to
  swap against, **not** because of chain id. (This entry used to claim "the
  inherited builders bake in ETH's chain id 1"; they do not. `BscAdapter` passes
  56 to `super().__init__`, which is exactly why its inherited *send* paths sign
  correctly.) Revisit when `inbound_addresses` shows BSC
  `chain_trading_paused: false`; at that point the swap source needs only the
  entry point unstubbed, not a refactor.
- **BasicSwap backend** (trustless P2P / privacy / XMR): orchestrate its daemon
  via API; needs full nodes (heavy) and a different custody seam. Future.
- **Monero (XMR) hold/balance/send**: blocked on a custody/architecture
  decision — see `docs/monero.md` for the analysis and the open choices.

## Extend `status <txid>` to every chain (not just BTC)

`status` prints an on-chain transaction summary (inputs, outputs, change, fee in
sats + EUR, and whether it carries a swap memo) — but **only for BTC**, via
`BtcAdapter.fetch_tx` / `parse_tx_summary` (`chains/btc.py`). A plain `send` is
never observed by a swap vault, so this on-chain view is the *only* useful thing
`status` can say about a non-swap tx, and it's missing for every other chain.

To generalize:
- DASH/ZEC are the cheapest wins — DASH has the same Esplora-ish Insight `/tx`
  shape (a `parse_tx_summary`-alike over Insight's fields); ZEC needs the tx read
  back from lightwalletd (transparent in/out only).
- ETH/BSC: an `eth_getTransactionByHash` + receipt summary (to/from/value, gas
  used × price = fee) — different shape (account model, no UTXO change output),
  so `TxSummary` needs a chain-agnostic form or a per-model variant.
- TRON: `gettransactionbyid` + `gettransactioninfobyid` for the energy/bandwidth
  fee.
- CACAO/RUNE: the Cosmos tx query (`/cosmos/tx/v1beta1/txs/{hash}`), decoding the
  `MsgSend`/`MsgDeposit` and the fee.
- Route in `cli._print_onchain_tx` by the same chain-dispatch the rest of the CLI
  uses (`_UTXO_ADAPTERS` + the account-model adapters) instead of hardcoding BTC.

Consider whether the README feature matrix should gain a **Track** (or **Status**)
column for this — it's a distinct capability from Send/Sweep, currently ✅ for BTC
and blank everywhere else. (Remember the duplicated matrix further down the
README — both copies would need the column.)

## Other known gaps

- **The address checksum guard can now over-reject, which is the worse
  failure.** `validate_destination_address` verifies base58check / bech32 /
  bech32m / cashaddr / EIP-55 as well as the shape. It is fail-open where no
  checksum exists (an all-lowercase EVM address, a chain with no shape rule, a
  chain listed in `_NO_CHECKSUM`), but if a chain has a *second* legitimate
  address form the dispatch does not know about, a valid address is now refused
  — and a blocked spend you meant to make is worse than a missed typo. Known
  deliberate exclusions: Cardano Byron (`Ae2…`/`DdzFF…`, base58-over-CBOR with
  a CRC32, unverifiable here) and XRP X-addresses (rejected by THORChain
  anyway). Adding a chain means naming its strategy — a `_SEGWIT_HRP` /
  `_PLAIN_BECH32_HRP` / `_EVM_CHAINS` / `_BASE58CHECK_ALPHABET` entry, or
  `_NO_CHECKSUM` — and the invariant test fails if a `_RULES` entry names none.
- **Three legitimate address forms are refused by the *shape* rules** (not the
  checksum layer; these predate it and are unchanged by it):
  **uppercase bech32** (`BC1QW5…` is valid per BIP-173 and is what BIP-21 QR
  codes emit), **uppercase cashaddr** (`BITCOINCASH:QPM2…`, the spec's other
  canonical form), and **BCH CashTokens-aware addresses** (`bitcoincash:z…`,
  live since 2023, which receive plain BCH fine). `_B32` is `[a-z0-9]` and the
  cashaddr rule is `[qp]`, so all three miss. Fixing the first two is roughly a
  `.lower()` before the bech32/cashaddr rules — but do it properly: BIP-173
  requires all-one-case, so mixed case must still be *rejected*, which a bare
  `.lower()` would start accepting.
- **An XRP payout can never carry a destination tag.** THORChain rejects both
  spellings that could express one: an X-address (the XRPL's own tag encoding)
  and an `address:tag` suffix each come back "unable to parse address". So
  `--to XRP` can only pay a bare classic `r…` address, and an exchange deposit
  address that requires a tag **must not be used** — such a deposit is usually
  unrecoverable, and nothing downstream of us can fix it. `--dest` warns and the
  `XRP` rule in `addresses.py` refuses X-addresses, which is as far as this side
  can go; the limitation itself is THORChain's and is not ours to close. Recorded
  here so it is not rediscovered as a bug. If THORChain ever adds tag support the
  memo format will change, and that is the trigger to revisit.
- **Broadcast is still unproven on mainnet for DASH, ZEC, ARB and RUNE.** Two
  run-throughs have spent real funds: `docs/live-session-2026-07-24.md` proved
  the BTC send + BTC/ETH/ERC-20 swap paths (THORChain, Maya and CoW), and
  `docs/live-session-2026-08-16.md` proved a **TRX** swap-from, the Cosmos
  `MsgDeposit` on Maya (CACAO), and the two-leg symmetric add. What is left:
  - **DASH and ZEC** — opt-in broadcast loops gated on a funded
    `SWAPSACK_DASH_MNEMONIC` / `SWAPSACK_ZEC_MNEMONIC` (seeds in
    `docs/testnet.md`). Otherwise feature-complete (hold/balance/send/sweep/
    swap-from/LP); proving the broadcasts is all that remains. See
    `docs/dash.md`, `docs/zcash.md`.
  - **ARB** — nothing has been broadcast on Arbitrum at all (*Next up* item 1).
  - **RUNE** — no THORChain native tx; its LP is paused and no RUNE swap has
    been made. CACAO's proof does **not** transfer: same code, different chain,
    chain-id and fee.
  - **`MsgSend` (CACAO/RUNE `send`)** — only `MsgDeposit` has broadcast. These
    are different protobuf messages; one proves nothing about the other.
  - **swap-`--from` CACAO** — both live attempts aborted at the quote, so
    nothing was built. It shares `_prepare_deposit` with the proven LP leg, but
    its own swap-memo/destination binding has not run.
  - **USDT-TRON**, and a plain ETH `send` (as opposed to a swap).
- **The offline guard covers Python sockets and grpc — not every possible
  transport.** `tests/conftest.py` now refuses `socket.connect`/`connect_ex`/
  `create_connection` and the grpc channel factories for any test without the
  `network` marker (`tests/test_offline_guard.py` asserts both the block and
  the lift). Two things it does *not* close:
  - Any **other C-level client** that connects without going through a Python
    socket object is unguarded, for the same reason grpc needed its own seam:
    grpcio's C core connects straight past a patched `socket.socket.connect`
    (verified, not assumed — a real channel to `zec.rocks:443` came up with the
    patch in place). So `unshare -rn -- uv run --no-sync pytest -q` (after a
    `uv sync`, which itself needs the network) remains the definitive check —
    it is the kernel saying no rather than us. Worth considering for CI, where
    the sync and the run are separate steps anyway.
  - The guard is **per-test**, so a module doing I/O at *import* time still
    escapes it (collection happens before fixtures). Nothing does today.
  Five tests were leaking when the guard first ran: `send`/`status` price their
  fee lines via CoinGecko, and `status` also queried Esplora. `_eur_price` is
  `@functools.cache`d, which made it worse than a plain leak — the first test to
  warm the cache spared the rest, so *which* test went out depended on ordering.
  The `fake_feed` fixture in `test_cli.py` is autouse for that reason.
- **BIP49/44 scanning**: real wiring scans BIP84 only (Trust Wallet's scheme).
  `scan_account` is generic enough to add `m/49'`/`m/44'` accounts + script
  types when needed.
- **EVM gas estimation**: every EVM path uses fixed gas constants (`--eth-gas`
  default 60000, `APPROVE_GAS` 70000, `TOKEN_DEPOSIT_GAS` 200000); could call
  `eth_estimateGas` against the quote's vault/memo instead.

  This was previously flagged as a *blocker* for Arbitrum, on the reasoning that
  L2s charge the L1 calldata cost through an inflated gas limit and so would
  need real estimation. **Measured, and the premise was wrong at this scale.**
  Asking Arbitrum's `NodeInterface` precompile (`gasEstimateL1Component`) on
  2026-08-16, the L1 surcharge was **101 gas at 0 bytes of calldata rising to
  172 at 108 bytes** — negligible against a 60k/200k budget, so ARB inherits the
  *swap and deposit* constants unchanged.

  **The native-send budget was the exception, and it was got wrong once.** The
  same measurement put a plain native transfer at 21,345 against
  `NATIVE_SEND_GAS = 21000`, which is Ethereum's exact floor with no slack — so
  the shared constant was not merely tight on Arbitrum, it was *below* the
  floor, and an `ETH-ARB` send would have run out of gas, reverted and burned
  the whole limit delivering nothing. `NATIVE_SEND_GAS` is now a per-adapter
  `native_send_gas`, which ARB raises to 30000. A gas limit is refunded when
  unused, so the headroom is free. Watch for this when adding any further L2:
  the 60k/200k budgets absorb the surcharge, the 21000 one cannot.

  What would change the rest: the surcharge scales with the L1 base fee, which
  was ~0.8 gwei when measured. It would take roughly a 250x L1 spike to eat the
  60k budget's headroom. Re-measure with the same precompile before assuming
  otherwise — and note this says nothing about chains with a *different* L2 fee
  model.
- **Cache LP provider addresses (balance-report speed-up)**: reporting added
  liquidity queries the backend's `pool/{POOL}/liquidity_provider/{ADDRESS}`
  endpoint. ETH/TRON have a single derived address; BTC's LP is keyed by the
  deposit tx's VIN0, which isn't predictable, so the report has to query every
  *used* address the account scan already enumerates (× each backend). To skip
  those per-address LP calls, cache the provider address learned when *we* build
  an `add-liquidity` tx — read VIN0 back from the final built/signed tx (don't
  predict it from coin-selection order: bitcoinlib may BIP-69-reorder inputs).
  Deferred because it's only a BTC concern and a cache must **extend** coverage,
  never shrink it: a lost/stale cache (seed restored elsewhere, LP added by
  another tool) would silently under-report funds — the worst failure for a
  wallet. So treat it as a hint unioned with the full scan, or as an opt-in fast
  path with the scan as the default source of truth. (See the chat that prompted
  this.) That warning has already come true once — a symmetric position is keyed
  by the `maya1…`/`thor1…` address and was invisible until `_report_liquidity`
  started probing it, so any cache must keep that probe rather than replace it.
- **USDT-ETH source niceties**: `--amount max` (needs token balance), real
  `eth_estimateGas` instead of fixed approve/deposit gas, and the USDT
  "reset allowance to 0 before re-approving" edge case for repeat swaps.
- **Phase 2 — semi-automatic convert**: human-in-the-loop "convert everything
  above dust since last run" command (accumulate small inbounds, stream large
  swaps, idempotent on processed txids).
