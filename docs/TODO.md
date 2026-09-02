# TODO

Only open work. Completed items live in `CHANGELOG.md` and the code; caveats that
outlived the work that created them are kept below as open risk.

## Low-hanging fruit (cheapest first)

*Cheapest*, which is not the same as *most valuable* — **priority lives in
*Next up* below**, and this list deliberately does not compete with it. Read it
as the answer to "what is a short session worth of work", ranked by how much
code it needs, with what each one is actually worth stated next to it.

1. **BSC as a swap source — the cheapest feature on the whole list.**
   `chains/bsc.py` already exists and already signs for chain 56; the swap
   entry point is stubbed out by hand because nothing traded BSC, **and that
   halt has since lifted** (2026-09-02). No new adapter, no new chain family,
   no new signer. It is also *less* work than Avalanche was, because BSC is
   already half-wired — checked against the code on 2026-09-02:

   | seam | BSC today |
   |---|---|
   | `_wallet_adapters` | **done** (`cli.py`, `_bsc_adapter` is in the list) |
   | `cmd_address` | **done** (prints a BSC row) |
   | `pricefeed` | **done** (`BNB`, `USDT-BSC`, `USDC-BSC`) |
   | `ASSET` | missing — `BSC.BNB` and the two 18-decimal BEP-20s |
   | `_EVM_ADAPTERS` | missing |
   | `_DESTINATION_DERIVERS` | missing |
   | `--bsc-rpc` | exists on `balance` only; needs adding to `_add_broadcast_args`, `send` and `swap` |
   | `BscAdapter.build_and_verify` | stubbed by hand; unstub it |

   So four seams plus the unstub, not the seven the AVAX wiring needed. The
   one genuine trap is that **BSC's USDT/USDC are 18-decimal**, unlike their
   6-decimal ETH/AVAX namesakes — `BSC_TRACKED_TOKENS` already has it right,
   so do not "harmonise" it.

   **Check the depth before you start, though.** On 2026-09-02 `BSC.USDC` and
   `BSC.USDT` held ~5.4k each — thinner than the `ARB.USDC` pool that *Next
   up* item 2 already calls too thin to be useful at size — against ~1945 BNB
   for `BSC.BNB`. So the native leg may be the only one worth wiring, and that
   is a measurement to redo rather than trust. Full entry: *Swap backends* →
   the BSC bullet.
2. **BASE, the same shape one step further out.** Its halt has lifted too, but
   there is no adapter yet — so it is a `chains/avax.py`-shaped subclass plus a
   `_RULES` line and an `_EVM_CHAINS` entry, on top of the same CLI wiring as
   BSC. **`BASE.ETH` held ~2.6 ETH on 2026-09-02**, which is too thin to swap
   against at any size; do BSC first and reuse whatever that teaches. Same
   bullet's neighbours in *Swap backends*.
3. **ATOM auto-derived instead of `--dest`-only.** One `_DESTINATION_DERIVERS`
   entry and one `RECEIVE_ONLY_CHAINS` entry — the seam exists and is empty.
   See *Next up* item 3's second follow-up for why the warning is the point.
4. **The three address forms the *shape* rules refuse** (uppercase bech32,
   uppercase cashaddr, BCH CashTokens `z…`). A `.lower()` in the right place,
   but see *Other known gaps* — a bare `.lower()` would wrongly start accepting
   mixed case, which BIP-173 forbids.

If you want cheap **fixes** rather than cheap **features**, the two entries
under *Known bugs* are both smaller than item 1 and both already diagnosed down
to the fix, with the real API responses captured — and each currently tells you
something false about your own money, which is why they sit above the feature
work.

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
   and ETH-ARB are done, and native AVAX arrived with its adapter (auto-derived,
   like ARB, since it *is* the ETH address); **SOL is the only remaining
   candidate, and it is blocked** — `SOL.SOL` exists on THORChain but is
   halted (a live `BTC->SOL.SOL` quote returns "trading is halted, can't
   process swap"). It is now the **only** chain still blocked this way — the
   BSC and BASE halts this used to be compared to have both lifted (see *Swap
   backends*). Revisit when `pools` shows `trading_halted: false`, or reach it
   via Chainflip (see *Swap backends*).
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
- **Signalling cannot be turned off.** `RBF_SEQUENCE = 0xFFFFFFFD`
  (`chains/utxo.py`) is applied unconditionally by both `build_unsigned_swap`
  and `build_replacement`; there is no flag, env var or config key for it. The
  one concrete reason to want one: some merchants and exchanges refuse to
  credit a zero-conf payment that signals RBF, so a spend from this wallet can
  sit uncredited until it confirms, where a non-signalling one would not have.

  Anyone building a `--no-rbf` must not sell it as more than that. Bitcoin Core
  has had `mempoolfullrbf` on by default since 28.0, so on much of the network
  a transaction that does *not* signal is replaceable anyway — the nSequence
  bit is a courtesy marker to whoever is reading it, not protection against
  replacement. A flag that implies otherwise would be worse than no flag.
  It would also have to refuse, or at least warn, on a swap deposit: THORChain,
  Maya and Chainflip all wait for confirmations, so opting out there buys
  nothing and forfeits `bump` as the way to unstick the deposit.

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
  hand-lists every adapter — adding Avalanche meant editing it, its help
  string and `_wallet_adapters` by hand, three places one table would make
  one — (and so is the thing that silently forgets a new
  chain).
- **A7** — split `base.ChainAdapter` into `WalletChain` vs `SourceChain` (Tron is
  destination-only). The `swap.SwapSource` protocol already exists from A4.
- **C-list** — one `ThreadPoolExecutor` per scan; `quote` memo row alignment;
  note ETH/TRON balance only inspects index 0.

## Swap backends

- **Chainflip — B1 (quotes), B2 (execution from BTC) and the mainnet
  broadcast all done 2026-08-28.** The broadcast was
  `d7bbc290bcbdefbc3dd058ab8b0680842a596552051bc9f2ec3b159181214458`, block
  964460: it paid that epoch's vault, carried the 48-byte OP_RETURN, and
  Chainflip witnessed it as swap 1764999 — so the payload, the vault check,
  the encoded destination and the refund binding all held against the real
  protocol, and the local decoder's reading of the payload matches field for
  field what the chain reports back. It did **not** fill, and the reason is
  the interesting part: the deposit went out at 1.13 sats/vB, sat in the
  mempool while BTC fell ~2%, and by the time two confirmations landed the
  encoded floor (78,105 USDT/BTC) was above spot — that floor works back to a
  quote taken around 79,300 USDT/BTC, a level BTC last held about ninety
  minutes before the block, which is the mempool wait showing up as a number.
  Inferred from the price, not from a log. 20 retries, every one `MinPriceViolation`, aborted, 498,294 of 500,000
  sats refunded to the change output. Fill-or-kill did exactly what it is for.
  Cost of the round trip: 1,945 sats — and note both an EGRESS *and* a REFUND
  fee are charged although nothing was ever egressed in the output asset, so a
  refused swap does not cost one fee. What remains:
  **a source other than BTC** — Chainflip is not Bitcoin-only, and neither is
  its vault-swap API: it covers EVM chains and Solana alongside Bitcoin, so
  ETH/ARB (a contract call into the Vault contract) and SOL (a program
  instruction) are reachable brokerlessly too. Neither reuses the UTXO builder
  or the gate that checks its outputs, so each is its own piece of work; a
  source the vault-swap API does not cover would need a deposit channel and a
  broker instead. Today every non-BTC source prices in `auto` and settles
  nowhere, and says so out loud when it was the cheaper route; a
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
- **Chainflip's price floor is set at build time and enforced ~20 minutes
  later** — the lesson of the 2026-08-28 broadcast above, and the one thing
  that stands between a working vault swap and a filled one. Between the quote
  and the witnessing sit the mempool wait, two Bitcoin confirmations, and the
  100-block (~10 min) retry window. `min_output_amount`'s docstring already
  allows for the confirmations; what that run showed is that the **mempool
  wait dominates**, and it is the only term the wallet controls. Three
  candidate fixes, none implemented: (a) refuse to build a vault swap at a fee
  rate that will not confirm in ~2 blocks, or give this path a tighter
  `--fee-blocks` default than a plain `send` — 239 sats of fee saved cost
  1,706 sats of protocol fees and a round trip; (b) when a swap tx is still
  unconfirmed after N blocks, **re-quote and rebuild** rather than CPFP — a
  stale floor cannot be fixed by confirming faster, which is exactly what the
  CPFP rescue did here; (c) expose `retryDurationBlocks` (encoded as 100) so a
  longer window can ride out a dip. Note (a) and (b) pull against `--amount
  max` being impossible here anyway, so a rebuild always has change to work
  with.
- **`status` misreports a refunded Chainflip swap.** Chainflip's `state:
  COMPLETED` means the swap's lifecycle is *over*, not that it succeeded; the
  refund lives in `refundEgress`, which `_print_chainflip_swap` ignores. On the
  aborted swap above it prints `COMPLETED (swap 1764999)` and `out: not paid
  out yet (USDT)` — i.e. it reads as still pending when the money was already
  back in the wallet. It should read `abortedReason` / `refundEgress` and say
  refunded, with the amount and the refund txid. Worst-case reading of the
  current output is a user re-sending a swap that already came back.
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
- **USDC on cheaper chains — done for ARB and AVAX; BASE and BSC remain.**
  This used to say "so do A2/A3 first rather than copy it per chain"; that is
  no longer the trade-off. `EthAdapter` parameterizes `chain_id`,
  `chains/bsc.py` is a ~60-line subclass proving the seam, and the per-chain
  adapter *is* the shared code path. Evidence: `chains/arb.py` (~50 lines of
  configuration) and now `chains/avax.py` (~100, most of it the docstring),
  each of which brought its chain's whole wallet side — hold, balance,
  destination, send/sweep and swap-from — for the native coin *and* its
  tracked tokens.

  What is left on this path is **BASE and BSC**, and both are now purely a
  missing adapter rather than a protocol block (see the halt entry below).
  Also still missing: `AVAX` **LP**, which needs THORChain's global LP pause to
  lift and has no second network to fall back on — Maya has no AVAX pools at
  all. `add-liquidity --asset AVAX` refuses up front and names the mimir key,
  so there is nothing to build until the pause lifts.

  One test gap left behind, worth closing next time this file is open:
  **`tests/test_arb.py` never exercises a token *swap*** (`build_and_verify`
  with a `-`-qualified `from_asset`), which is the only EVM path where the
  tracked-token decimals rescale the amount. `tests/test_avax.py` gained
  exactly that test — copy it across; ARB's spend paths are mainnet-unproven
  too, so the gate is all the evidence there is.

  Two things worth knowing before the next EVM adapter, both learned here:
  - **The per-chain surface is three adapter fields plus one shared constant,
    and all four are silent when wrong**: chain id, token decimals,
    `lp_backends` — and `ETH_MAX_FEE_WEI`, which is not per-chain at all and
    is the entry below. Getting the chain id wrong emits a transaction that is
    *valid on Ethereum mainnet* and pays real ETH to the same recipient.
    Getting `lp_backends` wrong from a copy is the subtler one — ARB is
    Maya-only and AVAX is THORChain-only, the exact inverse — and it fails
    closed while naming the wrong network, which reads as a protocol problem
    rather than a typo.
  - **`ETH_MAX_FEE_WEI` is one fixed wei ceiling across chains whose native
    coin has wildly different value** (`cli.py:72`, applied at four call
    sites; the check is in `verify.py`). A token swap burns
    `APPROVE_GAS` 70000 + `TOKEN_DEPOSIT_GAS` 200000 = 270k gas against
    `10**16` wei, so the gate refuses once `max_fee_per_gas` exceeds ~37 gwei.
    On Ethereum that ceiling is ~€21 and generous; on Avalanche the same
    number is ~€0.06, roughly **330× tighter in value**. It fails *closed*, so
    no funds are at risk — but the message reads as a wallet bug rather than a
    policy, on a path the README advertises. Not urgent: Avalanche's base fee
    measured **0.082 gwei** on 2026-09-02 (`fetch_fees` returns `base*2 + tip`,
    so ~2 gwei), needing a ~220× spike to bite. The fix is to make it an
    `EthAdapter` class attribute overridden per chain and read from the
    adapter at those four call sites, rather than imported as one module
    constant — deliberately deferred because it touches the ETH and ARB money
    paths, not only the new chain.
  - **ARB's raised `native_send_gas` is an L2 thing and must not be copied.**
    Arbitrum needs 30000 because an L2 bills the L1 calldata cost as extra gas
    consumed; Avalanche is an L1 and Ethereum's 21000 is the whole cost. The
    next adapter should ask which of the two it is rather than inherit either
    answer by accident.

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
  - **BASE's halt has lifted — it is now merely unimplemented.** As of
    2026-08-16 `BASE.USDC` and `BASE.ETH` were `Available` but
    `trading_halted: true`; re-checked **2026-09-02**, both are `Available`
    with `trading_halted: false` and `inbound_addresses` reports BASE
    `halted: false`, `chain_trading_paused: false`. So the block is gone and
    what remains is one `_RULES` line, an `_EVM_CHAINS` entry and a
    `chains/avax.py`-shaped adapter. **Mind the depth before bothering**:
    `BASE.ETH` held ~2.6 ETH on 2026-09-02, which is far too thin to swap
    against at any size worth the work; `BASE.USDC` held ~31k. Re-measure.
- **BSC's halt has lifted — the block is gone, the stub is not.**
  (*This is item 1 of* Low-hanging fruit *at the top of this file.*) This entry
  said "blocked, do not implement yet" on the strength of THORChain having BSC
  `chain_trading_paused`/`halted`. Re-checked **2026-09-02** and that is no
  longer true: `inbound_addresses` reports BSC `halted: false`,
  `chain_trading_paused: false`, and `BSC.BNB`, `BSC.USDC`, `BSC.USDT`,
  `BSC.ETH` and `BSC.BUSD` are all `Available` with `trading_halted: false`.
  Maya still has no BSC pools, so it is THORChain-only, like AVAX.

  So the trigger this entry named has fired, and its own prescription now
  applies: `BscAdapter.build_and_verify` raises by design — because there was
  nothing to swap against, **not** because of chain id (`BscAdapter` passes 56
  to `super().__init__`, which is why its inherited *send* paths already sign
  correctly) — so the swap source needs that entry point unstubbed and the CLI
  wiring `chains/avax.py` just went through, not a refactor. That makes it the
  cheapest remaining chain by some distance: the adapter already exists.

  **Measure the depth first.** On 2026-09-02 `BSC.USDC` and `BSC.USDT` held
  ~5.4k each and `BSC.BNB` ~1945 BNB. The stablecoin pools are thinner than
  Maya's `ARB.USDC`, which the *Next up* notes already call too thin to be
  useful at size — so the native `BSC.BNB` pool may be the only one worth
  wiring, and 18-decimal BEP-20 tokens (see `chains/bsc.py`) are the trap
  waiting either way.
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

## Extend `history` / `utxos` to every currency (not just the UTXO chains)

`history` and `utxos` list the UTXO chains only — BTC and DASH in full, ZEC's
unspent set without the spent half. The account-model chains have no listing at
all, and that is a data-source gap rather than a design choice: `chains/eth.py`,
`arb.py`, `avax.py` and `bsc.py` speak plain JSON-RPC to a public node, and
`tron.py` uses
the keyless java-tron HTTP API. **Neither has an address history index**, so
there is no call to enumerate an address's transactions with. Everything else is
already in place: `chains/history.py` is chain-agnostic (it takes `(path,
address)` records plus an `address_txs` fetch and returns `WalletTx`/`Output`
rows), and `report.py` renders whatever it is handed.

So the work is a data source, and picking one is the decision:

- **Etherscan V2** covers Ethereum, Arbitrum and BSC through one multichain
  endpoint, but needs an API key — a new secret to manage, and it binds the
  wallet's addresses to one commercial operator.
- **Blockscout** instances are keyless, but are per-chain URLs, rate-limited,
  and go down; the same class of SPOF as the single Dash Insight explorer that
  `docs/dash.md` already warns about, so it would want the same failover
  treatment `chains/btc.py` gives Esplora.
- **TronGrid**'s `/v1/accounts/{address}/transactions` is keyless but
  rate-limited, and is TRON-only.
- A **self-hosted indexer** (Erigon/Blockscout of one's own) avoids the trust
  and rate-limit problems and has none of the convenience.

Whichever is chosen, keep the shape the UTXO adapters already established: an
`address_txs(address) -> AddressTxs` returning parsed rows plus a `truncated`
flag, so a paging limit is reported as INCOMPLETE rather than passed off as a
complete history. Note the account model has no UTXOs, so `utxos` stays
UTXO-only by nature — only `history` generalizes. ZEC `history` is a separate,
harder problem: lightwalletd returns raw transactions and a post-NU5 (v5) txid
is a ZIP-244 tree hash the wallet does not compute.

Related: the `status <txid>` generalization above needs the same per-chain
transaction reads, so the two are worth doing together.

## Throttling: pacing a deep walk, and the single-endpoint case

A 429 is handled since the `net.py` throttle work: the request fails over to
the other explorer (they throttle independently), an all-endpoints refusal
honours `Retry-After` up to `MAX_RETRY_AFTER`, and `collect_pages` degrades to
a `truncated` — INCOMPLETE — listing rather than dying mid-walk. What is left
is the cheaper half of the problem:

- **Consider a small delay between pages.** Being throttled costs a round trip
  plus a `Retry-After`; a pause between pages costs less than the latency the
  walk already pays. Only worth it if throttling turns out to be routine rather
  than occasional — otherwise it slows every run to protect the rare one.
- **`--esplora` naming one instance turns the failover off**, so a throttle
  there can only be waited out, and the retry budget (3 attempts) is small.
  Whether that deserves a longer budget when there is nowhere to fail over to
  is an open question; naming an endpoint is also a privacy choice, so the
  answer is *not* to quietly re-add a second operator.

## Upstream: the lychee pre-commit hook misreads `rev` under `git push`

`.pre-commit-config.yaml` pins `rev: lychee-v0.24.2` and *also* passes
`LYCHEE_VERSION=0.24.2` as the hook's first argument. The duplication is a
workaround, and the two must be bumped together — including after a
`pre-commit autoupdate`, which will move `rev` and leave the argument behind.

The bug is upstream, in lycheeverse/lychee's `scripts/lychee_pre_commit.sh`. It
works its own version out with:

```sh
tag="$(git describe --tags --exact-match --match 'lychee-*v*' 2>/dev/null || true)"
```

expecting to run inside pre-commit's cached clone of the lychee repository. But
git exports `GIT_DIR` into every hook process, so under `git push` that command
runs against **the repository being pushed** instead. swapsack has no `lychee-*`
tag, the match fails, and the hook exits 100 with

    lychee pre-commit requires 'rev' to be a versioned release tag,
    such as 'lychee-v0.XX.0'

which is misleading: the rev is valid and exists upstream; the probe is looking
at the wrong repository. The symptom is that **every** `git push` from the repo
is refused, while `pre-commit run lychee --hook-stage pre-push --all-files`
passes — a difference that makes it look like a mystery until you notice only
the hook path is affected.

The script's own escape hatch (a `LYCHEE_VERSION=...` first argument, which
skips the probe) is what we use. The proper fix upstream is for the script to
resolve its own directory explicitly rather than trusting the ambient git
environment — e.g. `git --git-dir="$LYCHEE_DIR/.git"`, or clearing `GIT_DIR`,
`GIT_WORK_TREE` and `GIT_INDEX_FILE` before the describe.

**Not yet reported.** Worth filing against lycheeverse/lychee; anyone using that
hook at the `pre-push` stage hits it. Drop the workaround here once it is fixed
and released.

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
- **Broadcast is still unproven on mainnet for DASH, ZEC, ARB, AVAX and
  RUNE.** Two
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
  - **AVAX** — likewise nothing on Avalanche, for native AVAX or its tokens.
    The read paths *are* covered against the live chain (balance decoding, the
    chain id asked of the node, both tracked contracts' `decimals()` read back),
    and every spend path passes its own gate — a real unsigned swap, token
    approve+deposit, native send and token send all reach "verified OK" for
    chain 43114. What is missing is only a broadcast, and the wallet holds 0
    AVAX, so it needs gas there first. `swap --to AVAX` funds that in one
    step: THORChain's AVAX outbound fee was ~0.034 AVAX (~€0.21) on
    2026-09-02, about the same as its ETH fee. That is **not** comparable to
    the ~12×-cheaper ARB figure quoted under *Next up* item 1 — THORChain has
    no ARB pools at all, so that one is Maya's fee schedule, not this one.
    Across protocols ARB is still the cheaper rehearsal; within THORChain,
    AVAX and ETH cost the same.
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
