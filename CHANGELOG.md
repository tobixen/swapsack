# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is derived
automatically from git tags (PEP 440 / SemVer).

## [Unreleased]

### Added

- **CoW Protocol backend (same-chain ETH-token swaps):** `--backend cow`
  (and `auto`) for `quote`/`swap` between USDT-ETH/USDC-ETH/ETH — a keyless
  intent API (`api.cow.fi`) that settles a solver auction instead of routing
  through two THORChain/Maya pool legs, cutting cost sharply for same-chain
  pairs (see `docs/backends.md`). Execution signs a structured EIP-712 order
  (no vault, no memo) rather than paying calldata to a router, so it stays
  gateable exactly like a `SendPlan` — every order field (tokens, amounts,
  receiver, validity, fill-or-kill, balance mode) is bound and checked before
  signing (`verify_cow_order`). Funds the CoW vault relayer's ERC-20 allowance
  first when short (handling USDT's reset-to-zero quirk), and widens the
  `Backend` protocol (`serves()`/`try_quote()`/`executor`) so THORChain, Maya
  and CoW all price-compare under `--backend auto`. `status <order-uid>`
  tracks a submitted order. Live-signature-tested: a throwaway, unfunded key's
  signed order clears every orderbook check up to the balance check.
- **ZEC swap-from + liquidity (Phase 3):** `swap --from ZEC` (Maya-routed,
  vault + OP_RETURN memo, streaming supported) and single-sided
  `add-liquidity`/`withdraw-liquidity --asset ZEC --backend maya` (pairs with
  CACAO; a THORChain LP request is refused up front). The v4 builder carries
  OP_RETURN outputs and the ZIP-317 fee counts the memo's bytes as logical
  actions. Same mainnet-unproven caveat as the ZEC send path.
- **ZEC send + sweep (Phase 2):** `send --asset ZEC` (and `--amount max`)
  spends transparent funds through a **bespoke v4/ZIP-243 signer**
  (`chains/zcash_tx.py`) — bitcoinlib cannot sign Zcash's post-Overwinter
  transaction format. The ZIP-243 sighash implementation is anchored to a
  real mainnet transaction (its embedded signature verifies against our
  digest), the consensus branch id is fetched live from lightwalletd (never
  hardcoded — it would go stale at the next network upgrade), fees follow
  ZIP-317 (action-based), and transactions carry an expiry height (tip + 40)
  so unmined spends release instead of lingering. Ships **unproven on
  mainnet** (no Zcash testnet path) — an opt-in mainnet self-sweep test is
  gated on `SWAPSACK_ZEC_MNEMONIC`; test with a tiny amount first.
  Swap-*from* remains Phase 3. Adds `base58` + `coincurve` as direct
  dependencies (both were already transitive).
- **DASH send + sweep (Phase 2):** `send --asset DASH` (and `--amount max`)
  builds, gates and signs legacy P2PKH transactions through the same
  build/verify/sign path as BTC, broadcasting via the configured Insight API.
  The fee/dust maths is parameterized by script type (legacy 148/34-vB sizing,
  546-duff dust) and the fee rate is a conservative flat 2 duffs/vB. The
  broadcast ships **unproven on mainnet** (Dash has no testnet path) — an
  opt-in mainnet self-sweep test is gated on `SWAPSACK_DASH_MNEMONIC`; test
  with a tiny amount first.
- **DASH swap-from + liquidity (Phase 3):** `swap --from DASH` (Maya-routed,
  vault + OP_RETURN memo, streaming supported) and single-sided
  `add-liquidity`/`withdraw-liquidity --asset DASH --backend maya` (pairs
  with CACAO; a THORChain LP request is refused up front). Same
  mainnet-unproven caveat as the send path.
- **ZEC wallet side, Phase 1 (receive-only):** `address` derives the Zcash
  transparent receive address (standard BIP44, `m/44'/133'/0'/0/0`), `balance`
  gap-limit scans and reports ZEC via a configurable lightwalletd gRPC
  endpoint (`--zec-lwd` / `$SWAPSACK_ZEC_LWD`, default `zec.rocks:443`), and
  `swap --to ZEC` auto-derives the destination from the seed (no `--dest`
  needed), warning loudly that the chain is receive-only. The spend path
  needs a bespoke signer (bitcoinlib cannot sign Zcash's tx format) and is
  deliberately not implemented — see docs/zcash.md. Adds a `grpcio`
  dependency.
- **DASH wallet side, Phase 1 (receive-only):** `address` derives the Dash
  receive address (standard BIP44, `m/44'/5'/0'/0/0`), `balance` gap-limit
  scans and reports DASH via a configurable Insight API (`--dash-api` /
  `$SWAPSACK_DASH_API`, default `insight.dash.org`), and `swap --to DASH`
  auto-derives the destination from the seed (no `--dest` needed), warning
  loudly that the chain is receive-only. The spend path (send/sweep/swap-from)
  is deliberately not implemented yet — see docs/dash.md.

## [0.1.0] - 2026-07-08

First release
