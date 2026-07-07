# Maya CACAO support — design notes

> **Shared with RUNE.** THORChain and MayaChain are the same Cosmos-SDK software,
> so the wallet side described here lives in a shared
> `chains/cosmos.py::CosmosAdapter` (protobuf/signing in `chains/cosmos_tx.py`);
> `maya.py` (CACAO, 1e10) and `thor.py` (RUNE, 1e8) are thin config. Everything
> below applies to RUNE too, with HRP `thor`, chain-id `thorchain-1`, and 1e8
> base units (so RUNE has no decimals landmine).


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
   sequence lookup. A `verify_cosmos_send` gate (recipient + amount, no memo)
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
- **Phase 1 — Hold + Balance (read-only). DONE.** `chains/maya.py`:
  self-contained bech32 + `MayaAdapter.derive_address` (m/44'/931'/0'/0/0, HRP
  `maya`) and `wallet_balance` via mayanode `/cosmos/bank/v1beta1/balances`.
  Wired into `address` and `balance` (and a `--maya-api` override). The
  derivation is cross-checked in `tests/test_maya.py` against a golden vector
  that three independent BIP32 impls (bitcoinlib/eth-account/hdwallet) agree on.
- **Phase 2 — Send. DONE (broadcast unproven on mainnet).** `chains/cosmos_tx.py`
  hand-rolls the Cosmos protobuf (TxBody/AuthInfo/SignDoc/TxRaw + MsgSend) and
  SIGN_MODE_DIRECT signing (sha256(SignDoc) -> 64-byte low-S secp256k1 via
  eth-keys), with **no `grpcio`/`cosmpy` runtime dep**; the wire format is
  validated byte-for-byte against cosmpy in `tests/test_cosmos_tx.py`. The adapter
  fetches account-number/sequence + chain-id, builds+signs, and broadcasts via
  `/cosmos/tx/v1beta1/txs`; a `verify_cosmos_send` gate decodes the *serialized*
  body and binds sender/recipient/denom/amount/no-memo. Wired into `send`
  (`--asset CACAO`). Caveat: no Maya testnet, so broadcast — and the exact
  fee/gas convention (currently empty fee coins + gas 2e6, letting the chain
  charge its fixed native fee) — is unexercised on mainnet. Sweep (`--amount
  max`) is intentionally refused (fixed fee is charged separately).
- **Phase 3 — From (swap source). DONE (broadcast unproven on mainnet).**
  `swap --from CACAO` builds a `types.MsgDeposit` (memo-driven, **no inbound
  vault** — confirmed against a live quote, which returns `inbound_address:
  null`) carrying the CACAO coin at 1e10, signs it (reusing the Phase-2
  machinery) and broadcasts. The `MsgDeposit` wire format is validated by
  decoding a **real on-chain deposit** (`tests/test_cosmos_tx.py`), and a
  `verify_cosmos_deposit` gate binds the coin/amount/memo/signer and that the memo
  pays the destination. `parse_quote` was made tolerant of the fields a
  native-source quote omits (inbound_address/dust/gas), and for a
  `native_source` adapter `prepare_swap` replaces the tradability check with a
  wrong-network guard (the deposit executes on the adapter's own chain, so the
  quoting backend must be the home network). Amount scale (1e10) is
  the same bank denom as `MsgSend`, cross-checked against the quote's
  `recommended_min_amount_in`. Sweep is refused (fixed native fee).
- **Liquidity for CACAO is not a single-sided operation.** CACAO is the
  settlement asset (the base of every pool), so "adding CACAO liquidity" is the
  RUNE-leg of a *symmetric* add — that's TODO #4, not a per-asset LP like BTC.
  The `MsgDeposit` builder here is exactly what that will reuse.

## See also

- `docs/dash.md`, `docs/zcash.md` — the Maya-only *external* coins (UTXO), which
  share the no-testnet caveat but not the native-chain / decimals issues.
- `docs/TODO.md` — "Two-sided (symmetric) liquidity" (#4) shares the
  `MsgDeposit` signer this would build.
- `README.md` — currency roadmap row for CACAO.
