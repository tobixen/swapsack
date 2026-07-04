# Maya CACAO support — design notes

Status: **destination-only is DONE** (`swap --to CACAO --dest maya1... --backend
maya`/`auto`). The **full wallet side (Hold/Bal/Send/Sweep/From/Liq) is not
started** — unlike the UTXO coins, CACAO is the *native* asset of MayaChain, a
Cosmos-SDK/Tendermint chain, so "full" means a whole new chain family (protobuf
tx signing), not another adapter of an existing one. This note records the
scoping and the **1e10-decimals landmine** discovered while wiring the
destination, so neither is rediscovered mid-money-path. It mirrors
`docs/dash.md` / `docs/zcash.md`.

## TL;DR

- **CACAO is Maya-only and native to it.** Maya runs it as the settlement asset
  (the analogue of RUNE on THORChain). Live `MAYA.CACAO` swaps work (checked
  2026-07-03: a `BTC->MAYA.CACAO` quote returns a memo `=:c:maya1...` paying the
  dest; pool is deep).
- **Destination (`--to CACAO`) is done** — a `MAYA.CACAO` `ASSET` entry, a
  permissive `maya1` bech32 `--dest` rule (charset + length, **not** checksum —
  Maya validates it), a CoinGecko id (`cacao`), **and** the decimals fix below.
- **The full wallet side is a RUNE-class effort**, not a UTXO one — a Cosmos-SDK
  chain adapter with protobuf `MsgSend` / `MsgDeposit` signing. Tractable (no
  exotic-signature blocker like Zcash), but a meaningful chunk + a new
  dependency, and — like every spend path here — it would ship unexercised on
  mainnet (no easy Maya testnet faucet). Roadmap-flagged **niche / low
  priority**.

## The 1e10-decimals landmine (fixed for the destination path)

The whole codebase assumes THORChain's fixed **1e8** base units
(`thorchain.py`: *"All monetary amounts are expressed in THORChain's fixed 1e8
base units"*). **Maya's native CACAO is 1e10 (10 decimals)** — the one asset
that deviates. A naive "add it like ZEC/DASH" destination entry therefore
rendered the output **100x too large**: a 0.05 BTC → CACAO quote printed
`2797163.42 CACAO` instead of the correct `~27983 CACAO`.

Fix (destination path only): `thorchain.asset_unit(asset)` returns the base
units per whole coin, defaulting to `THORCHAIN_UNIT` (1e8) and overriding
`MAYA.CACAO` to 1e10. It is threaded through every place that renders a
*destination-asset* amount:

- `SwapFees.breakdown` (self-determines its unit from `fees.asset`),
- the `quote` output line and the `swap` `expect:` lines in `cli.py`,
- the `Market:` comparison (`_market_comparison`, both legs).

Source-side conversions stay `THORCHAIN_UNIT` because CACAO is destination-only
today. **When CACAO becomes a swap *source*** (Phase 3 below), the source-amount
conversions (`_base_units`, the `send:` lines, sweep maths) must also route
through `asset_unit` — grep for `THORCHAIN_UNIT` and audit each against the
`from` asset.

## Why the full wallet side is a new chain family

CACAO lives *on* MayaChain, a Tendermint/Cosmos-SDK chain (a THORChain fork).
There is no external chain to hold a balance on — the wallet must talk to Maya
itself:

1. **Address derivation.** secp256k1 key → `ripemd160(sha256(pubkey))` → bech32
   with the `maya` HRP. SLIP-44 coin type **931** (shared with THORChain),
   derivation `m/44'/931'/0'/0/x`. Small, self-contained, read-only-testable.
2. **Balance.** Query mayanode (`/cosmos/bank/v1beta1/balances/{addr}`), 1e10
   for CACAO. Read-only.
3. **Send.** A Cosmos-SDK bank `MsgSend`, signed `SIGN_MODE_DIRECT` (protobuf
   `SignDoc`) with secp256k1, then broadcast. **This is a new tx family** (not
   UTXO, not EVM, not TRON) — needs protobuf tx assembly + account-number /
   sequence lookup. A `verify_maya_send` gate (recipient + amount, no memo)
   mirrors the others.
4. **From (swap source) + Liquidity.** A `MsgDeposit` (Maya/THORChain-specific
   Cosmos msg) carrying CACAO + the `=:`/`+:` memo. **This is exactly TODO #4's
   RUNE-leg** — building it for CACAO also unlocks two-sided (symmetric)
   liquidity, whose RUNE/CACAO leg is a `MsgDeposit`.

### Dependency choice (deferred, owner)

Options for the Cosmos signing: `cosmpy` (batteries-included but heavy + its own
protobuf), hand-rolled protobuf against Maya's proto defs (no dep, more code),
or `pycosmos`-style minimal signer. Decide before committing wallet-side code —
same "pick deliberately, not mid-money-path" stance as the DASH data-source
question.

## Testability caveat

Same as DASH/ZEC: the spend side would ship **unexercised on mainnet** (no easy
Maya testnet faucet). Derivation, balance parsing, the `MsgSend`/`MsgDeposit`
assembly, and the verify gate are unit-testable offline; the sign-and-broadcast
loop is not, without funded mainnet CACAO. Plan units + an opt-in mainnet
broadcast test gated on a funded secret, mirroring the Nile TRC-20 loop.

## Recommended phasing

- **Phase 0 — destination (`--to CACAO`). DONE**, including the 1e10 fix.
- **Phase 1 — Hold + Balance (read-only).** maya1 derivation + balance via
  mayanode. Testable without spending.
- **Phase 2 — Send / Sweep. New tx family.** Cosmos `MsgSend` protobuf signer +
  `verify_maya_send`. Opt-in mainnet broadcast test.
- **Phase 3 — From (swap source) + Liq.** `MsgDeposit` with the swap/LP memo;
  audit the source-side `THORCHAIN_UNIT` conversions for CACAO's 1e10 (see the
  landmine section). Unlocks the RUNE-leg for TODO #4 symmetric liquidity.

## See also

- `docs/dash.md`, `docs/zcash.md` — the Maya-only *external* coins (UTXO), which
  share the no-testnet caveat but not the native-chain / decimals issues.
- `docs/TODO.md` — "Two-sided (symmetric) liquidity" (#4) shares the
  `MsgDeposit` signer this would build.
- `README.md` — currency roadmap row for CACAO.
