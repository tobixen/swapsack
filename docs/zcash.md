# Zcash (ZEC) support — design notes

Status: **destination-only is DONE** (`swap --to ZEC --dest t1... --backend
maya`/`auto`). The **full wallet side (Hold/Bal/Send/Sweep/From/Liq) is not
started, and is harder than Dash** — Zcash is not merely a legacy-UTXO chain,
its transaction format is incompatible with the bitcoinlib signer. This note
records the scoping so the risky parts are decided deliberately, not mid-way
through a money path. It mirrors `docs/dash.md`; read that first for the shared
legacy-UTXO issues.

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

- **No data source.** No Blockstream Esplora for Zcash. The balance / UTXO /
  broadcast / fee-rate layer needs a chosen, trusted source (Blockchair, a
  Zcash `lightwalletd`/`zcashd` RPC, or a community explorer). Same "a single
  explorer that is behind can silently *under-report* funds" risk as Dash —
  prefer a configurable endpoint, ideally unioning two sources.
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
- **Phase 1 — Hold + Balance (read-only).** Register Zcash params (t-addr
  version bytes `1CB8` for t1 / `1CBD` for t3; `bip44_cointype = 133`, path
  `m/44'/133'/0'/0/x`) enough to *derive* transparent addresses; a new
  `chains/zcash.py` with `wallet_balance` via the chosen data source +
  `scan_account`. Read-only, testable without spending.
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
