# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is derived
automatically from git tags (PEP 440 / SemVer).

## [Unreleased]

### Added
- Two-sided (symmetric) liquidity — building blocks: `liquidity.symmetric_add_memo`
  / `liquidity.pair_amount` (protocol amount at the pool ratio; handles CACAO's
  1e10 vs RUNE/asset 1e8) and `CosmosAdapter.build_and_verify_native_deposit`
  (the RUNE/CACAO `MsgDeposit` leg), all unit-tested. The two-leg CLI
  orchestration (prepare-both-then-broadcast + partial-failure handling) is the
  remaining step; THORChain LP is currently paused so the RUNE side is refused
  until re-enabled, while Maya (asset + CACAO) is open. See
  `docs/liquidity-symmetric.md`.
- Shell tab-completion via argcomplete (`register-python-argcomplete cryptoswap-wallet`).
- THORChain REST client, pre-broadcast verify gate, and encrypted keystore
  (HD seeds + raw keys, AES-256-GCM, atomic writes).
- Chain adapters: BTC (bitcoinlib), ETH + ERC-20 (eth-account/eth-abi),
  TRON (address + balance), BSC (address + balance: native BNB and BEP-20
  USDC/USDT at 18 decimals; a thin EVM subclass of the ETH adapter). BSC swaps
  are intentionally not implemented — THORChain has BSC trading halted and Maya
  has no BSC pools.
- Swaps: BTC, ETH (native), TRX (native), USDT-ETH/USDC-ETH (ERC-20) and
  USDT-TRON
  (TRC-20) as sources; BTC,
  ETH, TRX, USDT-TRON, USDT-ETH, USDC-ETH and (external-`--dest`-only) LTC, DOGE,
  BCH, DASH, ZEC, CACAO as
  destinations (DASH, ZEC and CACAO are Maya-only — route via
  `--backend maya`/`auto`; see `docs/dash.md`, `docs/zcash.md`, `docs/cacao.md`).
  CACAO uses 1e10 base units (not the usual 1e8), threaded through the quote/fee/
  market display via `thorchain.asset_unit`.
- MayaChain adapter (`chains/maya.py`): derives the `maya1` address
  (m/44'/931'/0'/0/0, secp256k1, self-contained bech32) and reports the CACAO
  balance from a mayanode REST node — wired into `address` and `balance`, with a
  `--maya-api` override.
- `send --asset CACAO`: a native CACAO transfer as a Cosmos `MsgSend`. The
  protobuf tx assembly + SIGN_MODE_DIRECT signing are hand-rolled in
  `chains/cosmos_tx.py` (no `grpcio`/`cosmpy` runtime dep) and validated
  byte-for-byte against cosmpy in the tests; a `verify_cosmos_send` gate binds the
  serialized recipient/amount/denom before signing. Broadcast is unproven on
  mainnet (no Maya testnet) — see `docs/cacao.md`.
- `swap --from CACAO`: native CACAO as a swap source, built as a Cosmos
  `MsgDeposit` (memo-driven, no inbound vault). The `MsgDeposit` wire format is
  validated by decoding a real on-chain deposit; a `verify_cosmos_deposit` gate
  binds the coin/amount/memo/signer and that the memo pays the destination.
  `parse_quote` now tolerates the fields a native-source quote omits.
  Broadcast unproven on mainnet. (Single-sided liquidity is n/a for the
  settlement asset — that's the RUNE-leg of symmetric LP.)
- RUNE (THORChain native): hold + balance + destination + `send` + swap-**from**,
  mirroring CACAO. Both ride a shared `chains.cosmos.CosmosAdapter` (THORChain
  and Maya are the same Cosmos-SDK software); `maya.py`/`thor.py` are thin
  config. RUNE uses the standard 1e8 base units (no decimals special-casing).
  A native source deposits on its own network via `MsgDeposit`, so it is pinned
  to its home backend (no price routing; a foreign `--backend` is refused, and
  `prepare_swap` double-checks the quoting network).
  Spend paths unproven on mainnet. `--amount max` sweep for BTC/ETH (swap and add-liquidity) and
  for ERC-20/TRC-20 token sources (USDT-ETH, USDC-ETH, USDT-TRON) on swap — the whole
  token balance, exact since the fee is paid in the native coin, not the token.
  The TRX source signs a native TransferContract with
  the memo in tx data (tronpy), via a keyless public node; the USDT-TRON source
  signs a TRC-20 `transfer` to the vault (routerless on TRON — the memo rides in
  the tx data), gated by a dedicated verify pass that decodes the calldata and
  binds the token, recipient, amount and memo.
- Permissive `--dest` sanity check (network/format) to catch gross typos before
  a swap is quoted or broadcast.
- Streaming swaps: `swap`/`quote --stream-interval N [--stream-quantity M]`
  splits the trade into sub-swaps over blocks to cut slippage on large or
  thinly-pooled swaps (threaded through `gather_quotes`/`prepare_swap` into the
  quote; the API returns a `…/interval/quantity` memo the verify gate binds like
  any other). Streaming manages slippage itself so it overrides `--tolerance-bps`
  (memo LIM=0); `quote` prints the sub-swap count and estimated settlement time.
- Itemised cost breakdown on `quote`/`swap` (slip/swap fee, flat outbound fee,
  quoted total in `bps`; the inbound source-chain tx fee is shown separately),
  plus a best-effort `Market:` block comparing the quoted output to a public spot
  price (CoinGecko) to surface the *total* realised cost — protocol fees, slip,
  and the pool-vs-market spread. Three lines: a source header, the per-asset
  comparison in `bps`, and the estimated absolute loss in **EUR**. On by default;
  `--no-price-check` disables it.
- `swap --tolerance-bps` (default 300) to widen the slippage/fee tolerance for
  small or high-fee swaps THORChain refuses at the default. A rejected quote now
  aborts cleanly with an actionable message (no traceback); the common
  `emit ... less than price limit` case explains that fees exceed the tolerance.
  The `quote` subcommand is informational and requests no price limit, so it
  always shows the price — however bad — even when a swap at the default
  tolerance would be refused.
- Registry-based multi-chain `balance` (native coins plus tracked ERC-20/TRC-20
  token balances — USDT-ETH, USDC-ETH and USDT-TRON; now also reports THORChain/Maya
  liquidity positions: total redeemable value in the asset — the RUNE/CACAO side
  of a position is converted at the pool price and folded in, not added raw —
  plus any pending); `quote`, `status`, `address`.
- Experimental `add-liquidity` / `withdraw-liquidity` (BTC, ETH, TRX,
  single-sided), with `--backend {thorchain,maya}` and a pre-flight `PAUSELP`
  check that aborts an add THORChain would only refund. ERC-20 token LP adds
  (e.g. USDT-ETH on Maya) are now supported: an approve + `router.depositWithExpiry`
  pair carrying the `+:POOL` memo (reusing the token-swap deposit builder + gate,
  with no destination to bind), `--amount max` sweeping the whole token balance;
  a token *withdraw* stays a native-ETH dust trigger.
- `send` to an external address (plain transfer, no swap/memo) for **BTC, ETH,
  USDT-ETH, TRX and USDT-TRON** (USDC-ETH rides the same ERC-20 path), with
  `--amount max` to sweep (exact for tokens; ETH leaves a gas reserve; native
  TRX sweep is refused as it can't be exact). Each asset has its own strict
  verify gate binding recipient + amount and rejecting any memo/router/extra
  calldata; the recipient is sanity-checked before building. ERC-20/TRC-20 sends
  are a routerless, approveless `transfer(recipient, amount)`.
- Network-parameterized BTC and ETH adapters (`BtcAdapter(network=...)`,
  `EthAdapter(chain_id=...)`; mainnet stays the default) plus opt-in full-loop
  `send` broadcast tests on BTC testnet3 and ETH Sepolia (skipped unless funded
  testnet accounts are supplied via env) — the first end-to-end proof of the
  spending path.
- Packaging: Hatch + hatch-vcs, `make install`, `--version`, CI, and PyPI
  trusted-publishing gated on the live integration tests.
