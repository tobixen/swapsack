# Project status — snapshot 2026-07-05

A point-in-time summary of where the wallet stands, tying together the
per-topic notes. The authoritative per-feature grid lives in `README.md`; this
doc adds the cross-cutting caveats and what's in flight. Dates are absolute.

## Currency support (see the README grid for per-feature detail)

| Currency | State | Notes |
|---|---|---|
| BTC | full | reference implementation (UTXO) |
| ETH + USDT/USDC-ETH | full | EVM; ERC-20 via the shared path |
| TRX / USDT-TRON | full / partial | native send done; TRX sweep pending |
| BSC / BNB | hold+bal | swaps blocked (THORChain halt, no Maya pool) |
| LTC / DOGE / BCH | destination-only | `--dest`, via THORChain |
| DASH / ZEC | destination-only | **Maya-only**; wallet side is a legacy-UTXO effort — see `dash.md`, `zcash.md` |
| **CACAO** (Maya native) | **hold + bal + to + send + from** | Cosmos-SDK; 1e10; see `cacao.md` |
| **RUNE** (THORChain native) | **hold + bal + to + send + from** | Cosmos-SDK; 1e8; shares the CACAO adapter |
| Two-sided liquidity | building blocks done | tested core; CLI orchestration pending — see `liquidity-symmetric.md` |
| XMR | not started | doesn't fit the model — see `monero.md` |

## What changed this session (2026-07-03 → 07-05)

Eight commits, **currently unpushed** (8 ahead of `origin/main`):

1. **ZEC as a swap destination** (Maya-only) — `--dest t1…`, live-verified.
2. **CI flakiness fix** — drained keep-alive sockets in network-test teardown
   (the "Integration (network)" job was a false red; tests passed).
3. **CACAO full wallet side** in phases: destination (+ the **1e10-decimals**
   fix via `thorchain.asset_unit`), then a MayaChain Cosmos adapter for
   hold/balance, then `send` (`MsgSend`), then swap-**from** (`MsgDeposit`).
4. **RUNE** (THORChain native) — same capabilities, delivered by **refactoring**
   the Maya adapter into a shared `chains/cosmos.py::CosmosAdapter`
   (`maya_tx.py` → `cosmos_tx.py`; `maya.py`/`thor.py` are thin config).
5. **Two-sided liquidity** — tested building blocks (memos, pool-ratio pairing,
   the RUNE/CACAO `MsgDeposit` leg).

## Cross-cutting caveats (read before using with real funds)

- **The Cosmos spend paths ship unproven on mainnet.** CACAO/RUNE `send` and
  swap-`from` are unit-tested to the hilt — the protobuf is byte-exact vs cosmpy,
  signatures verify, the verify gates bind recipient/amount/memo — **but there
  is no Maya/THORChain testnet, so the actual broadcast and the exact fee/gas
  convention are unexercised.** Same "ships unproven" caveat as the BTC/ETH/TRON
  spend paths. **Test with a tiny amount first.** This is why `send`/`from` are
  marked ◑ (partial) in the grid, not ✅.
- **Address derivation is the one thing that is cross-checked hard.** The
  `maya1`/`thor1` derivation is pinned to golden vectors that four independent
  implementations agree on (bitcoinlib, eth-account, hdwallet, cosmpy) — a wrong
  receive address would lose funds silently, so it is not left to trust.
- **THORChain LP is paused** (`PAUSELP`, checked 2026-07-05): single-sided and
  RUNE-leg symmetric adds are refused/refunded until re-enabled. Maya LP is open.
- **Hot-wallet risk** stands (see README): don't hold more than you can lose.

## In flight / next

- **Two-sided liquidity CLI** — the two-leg orchestration (prepare both legs +
  gate both before broadcasting either; broadcast protocol-then-asset with loud
  partial-failure handling; derive the asset-leg observed sender for pairing).
  Recommended first target: **ETH + CACAO on Maya** (single-address pairing is
  unambiguous; `ETH.ETH` pool is open). See `liquidity-symmetric.md`.
- **Verify the funded testnet broadcast loop actually runs in CI** (BTC signet /
  ETH Sepolia) — the BTC signet funding address was still empty at last check.
  See `TODO.md` and `testnet.md`.

## Doc map

- `cacao.md` — Maya CACAO + the shared Cosmos adapter (also covers RUNE).
- `liquidity-symmetric.md` — two-sided LP mechanics + safety protocol.
- `dash.md` / `zcash.md` — Maya-only legacy-UTXO chains (destination-only today).
- `monero.md` — the other "doesn't fit the model yet" chain.
- `streaming.md` — streaming swaps. `testnet.md` — funded broadcast tests.
- `TODO.md` — the running backlog. `README.md` — the capability grid + roadmap.
