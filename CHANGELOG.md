# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is derived
automatically from git tags (PEP 440 / SemVer).

## [Unreleased]

### Added

- **`status <txid>` now shows what a BTC transaction did on-chain** — inputs,
  each output with its address, the change coming back to you, and the fee in
  both sats and EUR — queried straight from Esplora (no keystore needed). A
  plain `send` is never observed by a THORChain/Maya vault, so previously
  `status` returned only an empty `inbound_observed: false` stage that read like
  a failure; it now explains that (no OP_RETURN memo = not a swap) instead of
  leaving you guessing.
- **Transaction fees are also shown in approximate EUR**, so "20000 sats" or
  "0.000420 ETH" reads as a cost you can judge before confirming: `btc fee:
  20000 @ 2.0/vB (~€11.43)`. Amounts under a cent show `<€0.01`. The rate comes
  from the same keyless CoinGecko feed as the existing market line and is purely
  advisory — if it is unreachable the estimate is simply omitted, never delaying
  or blocking a spend. Because this puts a third-party lookup on paths that
  previously made none, `--no-price-check` is now accepted by `send`,
  `add-liquidity`, `withdraw-liquidity` and `status` as well as `quote`/`swap`,
  and it suppresses the request itself rather than just the printed estimate.
- **BIP21 payment URIs** are accepted wherever an address is: `swapsack send
  'bitcoin:bc1q…?amount=0.01&label=Alice'` and `--dest ethereum:0x…` now work,
  so an address copied from a wallet or scanned from a QR code can be pasted
  as-is instead of being hand-trimmed. The scheme must match the chain being
  spent (a `litecoin:` URI in a BTC send is refused, catching a cross-chain
  paste), and a URI `amount=` that contradicts `--amount` aborts rather than
  silently preferring one of them.
- **CoW Protocol backend (same-chain ETH-token swaps):** `--backend cow`
  (and `auto`) for `quote`/`swap` between USDT-ETH/USDC-ETH/ETH — a keyless
  intent API (`api.cow.fi`) that settles a solver auction instead of routing
  through two THORChain/Maya pool legs, cutting cost sharply for same-chain
  pairs (see `docs/backends.md`). Execution signs a structured EIP-712 order
  (no vault, no memo) rather than paying calldata to a router, so it stays
  gateable exactly like a `SendPlan` — every order field (tokens, amounts,
  receiver, validity, fill-or-kill, balance mode) is bound and checked before
  signing (`verify_cow_order`). Funds the CoW vault relayer's ERC-20 allowance
  first when short (handling USDT's reset-to-zero quirk) and waits for the
  approval to mine before submitting the order, and widens the
  `Backend` protocol (`serves()`/`try_quote()`/`executor`) so THORChain, Maya
  and CoW all price-compare under `--backend auto`. `status <order-uid>`
  tracks a submitted order. Live-signature-tested: a throwaway, unfunded key's
  signed order clears every orderbook check up to the balance check.
- **ZEC support (hold, balance, send, sweep, swap-from, liquidity):**
  `address`/`balance` derive the Zcash transparent receive address (standard
  BIP44, `m/44'/133'/0'/0/0`) and gap-limit scan via a configurable
  lightwalletd gRPC endpoint (`--zec-lwd` / `$SWAPSACK_ZEC_LWD`, default
  `zec.rocks:443`). `send`/`swap --from ZEC` (and `--amount max`) spend
  transparent funds through a **bespoke v4/ZIP-243 signer**
  (`chains/zcash_tx.py`) — bitcoinlib cannot sign Zcash's post-Overwinter
  transaction format. The ZIP-243 sighash implementation is anchored to a
  real mainnet transaction (its embedded signature verifies against our
  digest), the consensus branch id is fetched live from lightwalletd (never
  hardcoded — it would go stale at the next network upgrade), fees follow
  ZIP-317 (action-based, counting OP_RETURN memo bytes as logical actions),
  and transactions carry an expiry height (tip + 40) so unmined spends
  release instead of lingering. `swap --from ZEC` is Maya-routed (vault +
  OP_RETURN memo, streaming supported); single-sided
  `add-liquidity`/`withdraw-liquidity --asset ZEC --backend maya` pairs with
  CACAO (a THORChain LP request is refused up front). Ships **unproven on
  mainnet** (no Zcash testnet path) — an opt-in mainnet self-sweep test is
  gated on `SWAPSACK_ZEC_MNEMONIC`; test with a tiny amount first. Adds
  `grpcio`, `base58` and `coincurve` as direct dependencies (the latter two
  were already transitive).
- **DASH support (hold, balance, send, sweep, swap-from, liquidity):**
  `address`/`balance` derive the Dash receive address (standard BIP44,
  `m/44'/5'/0'/0/0`) and gap-limit scan via a configurable Insight API
  (`--dash-api` / `$SWAPSACK_DASH_API`, default `insight.dash.org`).
  `send`/`swap --from DASH` (and `--amount max`) build, gate and sign legacy
  P2PKH transactions through the same build/verify/sign path as BTC,
  broadcasting via the configured Insight API; the fee/dust maths is
  parameterized by script type (legacy 148/34-vB sizing, 546-duff dust) and
  the fee rate is a conservative flat 2 duffs/vB. `swap --from DASH` is
  Maya-routed (vault + OP_RETURN memo, streaming supported); single-sided
  `add-liquidity`/`withdraw-liquidity --asset DASH --backend maya` pairs with
  CACAO (a THORChain LP request is refused up front). Ships **unproven on
  mainnet** (Dash has no testnet path) — an opt-in mainnet self-sweep test is
  gated on `SWAPSACK_DASH_MNEMONIC`; test with a tiny amount first.

### Fixed

- **THORChain node outage resilience:** `ThorchainClient` (and the `thorchain`
  swap backend) now tries an ordered list of nodes rather than a single
  hardcoded default, falling through on connection failure and pinning the
  first that answers. Prompted by `thornode.thorchain.network` going dark to
  DNS on 2026-07-12; the default list now also includes Liquify's public
  gateway. Override with `$SWAPSACK_THORNODE` as before (a single URL).

## [0.1.0] - 2026-07-08

First release
