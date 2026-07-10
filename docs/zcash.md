# Zcash (ZEC) support — design notes

Status: **Phase 0 (destination) and Phase 1 (Hold + Balance, receive-only) are
DONE** (2026-07-10): `swap --to ZEC` auto-derives the `t1…` destination, and
`address`/`balance` cover ZEC via **lightwalletd** (gRPC, default `zec.rocks`,
override `--zec-lwd` / `$SWAPSACK_ZEC_LWD`). The **spend side (Send/Sweep/
From/Liq) is not started, and is harder than Dash** — Zcash is not merely a
legacy-UTXO chain, its transaction format is incompatible with the bitcoinlib
signer, so `broadcast` refuses loudly. This note records the scoping so the
risky parts are decided deliberately, not mid-way through a money path. It
mirrors `docs/dash.md`; read that first for the shared legacy-UTXO issues.

## TL;DR

- **Swaps are feasible — via Maya only.** Zcash is **not on THORChain**. Maya
  runs a live `ZEC.ZEC` pool (checked 2026-07-03: `Available`, depth ~3,623
  ZEC). Every Zcash swap therefore routes through the Maya backend
  (`--backend maya`, or `auto`).
- **Destination (`--to ZEC`) is done** — it mirrors LTC/DOGE/BCH/DASH: a
  `ZEC.ZEC` `ASSET` entry, a permissive `--dest` sanity rule (transparent
  `t1`/`t3` base58, charset + length, **not** checksum — Maya validates the
  checksum), and a CoinGecko id (`zcash`) for the market line. Maya's pool is
  transparent-only, so **only t-addresses** are accepted; shielded `zs1…`
  (Sapling) and unified `u1…` addresses are intentionally rejected.
- **The full wallet side is a bigger, riskier job than Dash** for two reasons:
  the same missing-data-source / no-testnet problem Dash has, **plus** a
  transaction-format problem Dash does not — see below. Not recommended without
  a deliberate decision on a signer and a data source.

## Why Zcash is *not* "just another legacy UTXO coin" (the real blocker)

Dash is plain legacy pay-to-pubkey-hash: once its network params are registered
in bitcoinlib, bitcoinlib can build **and sign** a valid Dash transaction. Zcash
cannot be signed by bitcoinlib at all, even for **transparent-only** (t-addr →
t-addr) spends:

- Since **Overwinter/Sapling** (2018) and **NU5** (2022), Zcash transactions use
  a distinct format — an `fOverwintered` header flag, an `nVersionGroupId`, an
  `nExpiryHeight`, a `nConsensusBranchId`, and (Sapling+) a `valueBalance` and
  shielded bundles. Transaction versions are **v4** (Sapling, ZIP-243 sighash)
  and **v5** (NU5, ZIP-225 sighash), not Bitcoin's v1/v2.
- The **signature hash is not Bitcoin's**. ZIP-143/243/225 bind the sighash to
  the consensus branch ID of the active network upgrade, with a
  BIP143-like-but-different preimage. bitcoinlib produces a Bitcoin sighash, so
  any signature it emits is **rejected by Zcash consensus**.

So the spending side needs a **bespoke Zcash transparent-tx builder + signer**
(implementing the current consensus branch's sighash), or a different library
than bitcoinlib. This is the money-sensitive core; getting the branch ID or
sighash wrong yields a tx that is either rejected or — worse — malleable.

## The other blockers (shared with Dash)

- **Data source: decided (owner, 2026-07-10) — lightwalletd.** No Blockstream
  Esplora for Zcash, and the alternatives probed poorly (Blockbook's `zecN.
  trezor.io` is Cloudflare-blocked for non-browsers, Blockchair keyless
  transiently blacklists IPs, the community explorers expose no address API).
  lightwalletd is the canonical Zcash light-client infra with several
  reputable public operators (default `zec.rocks`, configurable) and covers
  the whole roadmap: `GetTaddressBalance`/`GetTaddressTxids` (Phase 1, done),
  `GetAddressUtxos` + `SendTransaction` (Phase 2). It's gRPC: the transport
  uses grpcio, but the messages are tiny and hand-rolled on the cosmos_tx
  protobuf primitives — no codegen/protobuf dependency. The "single source
  that is behind can silently *under-report* funds" caveat still applies;
  unioning a second source stays a TODO for the spend side.
- **Fees.** Zcash uses **ZIP-317** (conventional fee ≈ `5000 * max(2,
  n_logical_actions)` zatoshis; for a small transparent spend this is ~10,000
  zat = 0.0001 ZEC). For a **swap**, prefer Maya's quote
  `recommended_gas_rate` / `gas_rate_units`.
- **Testability.** No easy funded-testnet faucet + broadcast path comparable to
  BTC signet / ETH Sepolia. The spend side would ship **unexercised on
  mainnet** — the exact "irreversible loss of funds" the README warns about.
  Coin-selection/fee maths and the verify gate are unit-testable offline; the
  UTXO-scan-and-broadcast loop is not, without funded mainnet ZEC.

## Recommended phasing

- **Phase 0 — destination (`--to ZEC`). DONE.** `ZEC: "ZEC.ZEC"` in the CLI
  asset map, a `t[13]…` `--dest` rule in `addresses.py`, and `ZEC: "zcash"` in
  `pricefeed.py`. Unit-tested (address sanity + CoinGecko id).
- **Phase 1 — Hold + Balance (read-only). DONE.** `chains/zcash.py` derives
  `t1…` at `m/44'/133'/0'/0/x` via the shared `chains/p2pkh.py` (no bitcoinlib
  network registration — BIP32 derivation is network-independent, and the
  two-byte `1CB8` prefix wouldn't fit bitcoinlib's one-byte `prefix_address`
  anyway; golden vectors cross-checked against three independent
  implementations), and `wallet_balance` gap-limit scans via lightwalletd
  (`GetTaddressTxids` answers "ever used?" so used-but-emptied addresses keep
  the scan going; `GetTaddressBalance` prices the hits; no per-address mempool
  view, so pending is always 0). Wired into `cmd_address`, `balance` and
  destination auto-derivation (with a loud receive-only warning). The standard
  test mnemonic's 0/0 address has real 2018-era on-chain history, giving the
  scan an opt-in live guard (`pytest -m network`).
- **Phase 2 — Send / Sweep. Hard.** Needs the bespoke transparent-tx signer
  above (ZIP-243/225 sighash, consensus branch ID), generalized legacy (P2PKH)
  fee maths in `coins.py`, and a `verify_zcash_send` gate mirroring
  `verify_btc_send`. Add an opt-in mainnet broadcast test.
- **Phase 3 — From (swap source) + Liq.** Reuse the Phase-2 deposit path with a
  Maya vault + memo, against the Maya client; single-sided LP pairs with CACAO.

## See also

- `docs/dash.md` — the sibling Maya-only legacy-UTXO chain; shares the
  data-source and testnet issues (but not the tx-format one).
- `docs/monero.md` — the other "doesn't fit the model yet" chain.
- `README.md` — currency roadmap row for ZEC.
