# TODO

Only open work. Completed items live in `CHANGELOG.md` and the code; caveats that
outlived the work that created them are kept below as open risk.

## Next up (priority order)

Owner's requested order; two-sided liquidity comes *after* these.

1. **More swap *destinations* via external `--dest` addresses.** Remaining
   candidates: ATOM, XRP, SOL (XRP needs care re: destination tag), plus the
   Maya-only ADA/ARB under *Swap backends*.
   A new chain needs two things in `addresses.py`: a shape rule in `_RULES`
   *and*, if its address format is checksummed, a branch in `_checksum_problem`
   — a shape rule alone silently opts the chain out of the checksum guard. ATOM
   is free (Cosmos bech32, same as `thor1`/`maya1`); XRP is base58check with a
   *non-standard alphabet* (`base58.XRP_ALPHABET`); SOL is base58 with **no**
   checksum at all (a bare 32-byte ed25519 pubkey), so for SOL the shape rule is
   genuinely all there is — say so in a comment rather than leaving it looking
   like an omission.

2. **Two-sided (symmetric) liquidity — the two-leg CLI orchestration.**
   A symmetric add is two *linked* deposits: the asset leg (`+:POOL:<thor1addr>`
   to the inbound vault) and a RUNE/CACAO leg (a Cosmos `MsgDeposit` with memo
   `+:POOL:<assetaddr>`), paired by the protocol via the cross-referenced
   addresses within a time window.

   Build on the existing pieces — `thor1` derivation, RUNE balance, `MsgDeposit`
   sign/broadcast for RUNE + CACAO, `symmetric_add_memo`, `pair_amount`,
   `CosmosAdapter.build_and_verify_native_deposit`. What remains is the CLI
   orchestration: prepare-both-then-broadcast, partial-failure handling,
   asset-sender pairing.

   The risk that makes this worth doing carefully: if one leg lands and the other
   does not, the position is lopsided or stuck — material on an experimental,
   loss-prone feature. Note also that one-sided LP already carries ~50% RUNE
   price exposure; symmetric mainly buys *no entry slip* in exchange for sourcing
   and holding RUNE. THORChain LP is currently paused (`PAUSELP`), so symmetric
   works on Maya (asset + CACAO) today, RUNE when THORChain re-enables. See
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
- **Sepolia token send** (USDT/USDC on Sepolia) — the token-swap gate still bakes
  in mainnet `CHAIN_ID`; parameterize it (fold into A2/A3) to testnet-cover the
  ERC-20 send/swap path too.
- **THORChain stagenet swaps** — a real cross-chain swap loop (deposit on one
  testnet, receive on another) needs a stagenet vault + memo, a bigger lift.
- Wire the testnet secrets into the CI **Integration (network)** workflow so the
  broadcast loops run there, not just locally.

## Full RBF support — a `bump` command to unstick a low-fee tx

Every spend now **signals** BIP125 opt-in RBF (`nSequence 0xfffffffd`, set in
`chains/utxo.py`), so a stuck **BTC** transaction *can* be fee-replaced — but
nothing yet *does* the replacing. Build the other half:

- `swapsack bump <txid> [--fee-rate N | --fee-blocks N]` that rebuilds the same
  transaction (same inputs, same recipient/vault output, same OP_RETURN memo —
  all byte-identical) with a higher fee taken out of the change output, re-runs
  the **verify gate** (this is the whole point — never hand-roll a replacement
  outside the gate), signs and rebroadcasts.
- BIP125 rules the replacement must satisfy: pays a higher absolute fee *and* a
  higher feerate than the original; the original's inputs are all still
  available. Reducing only the change output keeps the vault output/memo exact,
  which a THORChain/Maya swap deposit **requires** (the memo carries a min-out
  limit; a changed vault amount would fail or refund).
- Edge: if change was folded into the fee (no change output), there's nothing to
  take the bump from without adding an input — either pull in another confirmed
  UTXO or refuse with a clear message.
- The ZEC bespoke signer (`chains/zcash_tx.py`) still hardcodes
  `sequence 0xffffffff`; Zcash has no standard mempool RBF, so leave it unless a
  concrete need appears — but note it here so it isn't forgotten.
- DASH inherits the signal from the shared builder but cannot use it: Dash Core
  implements no mempool replacement (deliberate, for InstantSend). So `bump`
  should be BTC-only and say so, rather than building a replacement no Dash node
  will accept. Dash's own answer to a stuck tx is InstantSend, not RBF.
- Pairs naturally with the CPFP work below (the other way to rescue a stuck tx —
  child-pays-for-parent — for when *we* don't control the parent).

## Spend unconfirmed inbound via CPFP (`--allow-unconfirmed`)

Currently `fetch_utxos` is confirmed-only and the fee model is a flat
`fee_rate`, so a swap can't be funded from an inbound tx still in the mempool.

Add an opt-in `--allow-unconfirmed` that:

- includes unconfirmed UTXOs as spendable, and
- does proper **child-pays-for-parent** fee selection: detect the parent's fee
  deficit and overpay on the swap (child) tx so the parent+child *package*
  reaches the target feerate.

Notes / caveats (see the chat that prompted this):

- THORChain still only acts on **confirmed** deposits (value-scaled
  confirmation count), so CPFP speeds up reaching that point but does not skip
  it. Main benefit is when the inbound is fee-stuck.
- Only safe when we control the parent. An external RBF-signalling parent can
  be replaced, which invalidates our deposit tx (benign failure: the swap just
  never happens, no funds lost) — warn the user.
- Mind Bitcoin mempool ancestor/descendant limits.

## Carried over from the early core reviews

The review documents themselves were removed once obsolete (`f90d329`), so these
descriptions are now the only record — the letter/number codes are historical
labels, not lookups.

- **N5** — BTC→token-destination memo vs the 80-byte OP_RETURN limit.
  Becomes live once USDT destinations from BTC are exercised.
- **A2/A3** — share the EVM key derivation + `to_checksum`/keccak helpers between
  ETH and TRON; default `wallet_balance` on an account-model base. Several other
  items below want this first (BSC swaps, USDC on cheaper chains, Sepolia token
  tests), so it is the highest-leverage refactor here.
- **A5** — table-drive the CLI per-chain factories / `_resolve_destination` /
  `cmd_address` / `_swap_from_*`.
- **A7** — split `base.ChainAdapter` into `WalletChain` vs `SourceChain` (Tron is
  destination-only). The `swap.SwapSource` protocol already exists from A4.
- **C-list** — one `ThreadPoolExecutor` per scan; `quote` memo row alignment;
  note ETH/TRON balance only inspects index 0.

## Swap backends

- **Chainflip** — the remaining non-thornode backend from the
  `docs/backends.md` scoping: a second independent cross-chain venue; adds
  SOL/DOT. Deposits are plain sends to a per-swap channel, so the existing
  send builders/gates get reused — but executing needs a broker/deposit-
  channel decision first (see `docs/backends.md`'s Chainflip execution notes,
  and `docs/chainflip.md` for the feasibility assessment). Calldata-style
  aggregators (ParaSwap/1inch/0x/LiFi) and custodial instant exchangers: not
  planned (gating problem / custody).
- **Maya-only assets still to expose**: ADA (Cardano) and ARB (Arbitrum) —
  destination-only is just an `ASSET` entry + a `--dest` rule. CACAO is exposed
  as a destination, but its **full wallet side** is a Cosmos-SDK chain effort
  (protobuf `MsgSend`/`MsgDeposit`) that overlaps *Next up* item 2's RUNE leg.
  (CACAO needs `thorchain.asset_unit` to stay 1e10, not 1e8 — see
  `docs/cacao.md`.)
- **USDC on cheaper chains**: THORChain also pools USDC on AVAX/BASE and Maya on
  ARB — all far cheaper to use than ETH mainnet (where the only current pool is).
  Each
  needs a new EVM chain adapter (RPC, chain-id, native coin, dest validation),
  so do A2/A3 (generalize `EthAdapter` into a shared EVM code path) rather than
  copy it per chain.
- **BSC swaps are blocked — do not implement yet.** Hold + Balance work
  (`chains/bsc.py`), but THORChain has BSC `chain_trading_paused`/`halted` (a
  live `BTC->BSC.BNB` quote returns "trading is halted, can't process swap") and
  Maya has no BSC pools, so To/From/Sweep/Liq are unusable and untestable.
  `BscAdapter.build_and_verify` raises by design (the inherited builders bake in
  ETH's chain id 1, wrong for BSC's 56). Revisit when `inbound_addresses` shows
  BSC `chain_trading_paused: false`; a swap source will also need the EVM chain
  id parameterized (currently the module-level `CHAIN_ID` in `eth.py`) — fold
  into A2/A3.
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

- **Broadcast is still unproven on mainnet for DASH, ZEC and TRON.** The
  2026-07-24 run-through (`docs/live-session-2026-07-24.md`) spent real funds and
  proved the BTC send + BTC/ETH/ERC-20 swap paths (THORChain, Maya and CoW), but
  DASH and ZEC ship mainnet-unproven — their opt-in broadcast loops are gated on
  a funded `SWAPSACK_DASH_MNEMONIC` / `SWAPSACK_ZEC_MNEMONIC` (seeds in
  `docs/testnet.md`) — and no TRX/USDT-TRON transaction has ever been broadcast.
  A plain ETH `send` (as opposed to a swap) has also not been exercised live.
  Both DASH and ZEC are otherwise feature-complete (hold/balance/send/sweep/
  swap-from/LP); proving the broadcasts is all that remains. See `docs/dash.md`,
  `docs/zcash.md`.
- **Nothing stops a unit test from reaching the network.** The default suite is
  meant to be fully offline (live I/O belongs behind `-m network`), but that is
  convention, not enforcement — and a test that leaks a real call does not look
  broken, it looks *flaky*, and only when the remote host happens to be down.
  One had been doing exactly that since v0.1.0:
  `test_wallet_balance_scans_and_sums` patched `address_info` but not
  `latest_height`, so `ZecAdapter.wallet_balance` hit `zec.rocks:443` on every
  run; it surfaced only when a full run took 87s instead of 17s and went red.
  Add an autouse fixture in `tests/conftest.py` that blocks socket connections
  for any item *without* the `network` marker — the file already has both seams
  (an autouse fixture keyed on `request.node.get_closest_marker("network")`, and
  `pytest_collection_modifyitems`). Patch `socket.socket.connect`/`create_connection`
  to raise, and make the message name the offending test and say to add the
  marker or mock the call. Note gRPC (ZEC's lightwalletd) may bypass Python-level
  sockets via the C core, so verify the guard actually catches that path rather
  than assuming — until then `unshare -rn -- uv run --no-sync pytest -q` (after a
  `uv sync`, which itself needs the network) is the check that definitely works.
- **BIP49/44 scanning**: real wiring scans BIP84 only (Trust Wallet's scheme).
  `scan_account` is generic enough to add `m/49'`/`m/44'` accounts + script
  types when needed.
- **ETH gas estimation**: ETH source uses a fixed `--eth-gas` (default 60000);
  could call `eth_estimateGas` against the quote's vault/memo instead.
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
  this.)
- **USDT-ETH source niceties**: `--amount max` (needs token balance), real
  `eth_estimateGas` instead of fixed approve/deposit gas, and the USDT
  "reset allowance to 0 before re-approving" edge case for repeat swaps.
- **Phase 2 — semi-automatic convert**: human-in-the-loop "convert everything
  above dust since last run" command (accumulate small inbounds, stream large
  swaps, idempotent on processed txids).
