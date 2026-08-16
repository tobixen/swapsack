# Two-sided (symmetric) liquidity — design notes

Status: **implemented for EVM assets** (Ethereum and Arbitrum) —
`add-liquidity --symmetric`, built on `swap.prepare_symmetric_liquidity` /
`execute_symmetric_liquidity`. The safety protocol below is what the code does,
not a plan, and **mainnet-proven for `ETH.USDC`** (`docs/live-session-2026-08-16.md`)
— ARB is implemented but has never broadcast, and RUNE never has. Remaining: UTXO
sources, blocked on the `vin[0]` pairing assumption — see the per-asset
caveats. This note records the mechanics so the money-sensitive coordination
stays deliberate.

## Why symmetric, and the honest caveats

A symmetric add provides *both* sides of a pool at the current ratio, so it
takes **no entry slip** (unlike a single-sided add, which the pool has to
rebalance). In exchange you must source and hold the protocol asset
(RUNE on THORChain, CACAO on Maya) and you take on a second on-chain tx.

- **Two irreversible txs on two chains that must pair.** New failure mode vs.
  every single-leg path: if one leg lands and the other doesn't, the position
  sits *pending* (or is refunded after a timeout) — lopsided/stuck funds.
- **Two money legs, and no testnet to rehearse them on.** `ETH.USDC` has now
  run for real (`docs/live-session-2026-08-16.md`), so the path is no longer
  unexercised — but that proves *one* pool on *one* pair of chains. ARB and
  RUNE have still never broadcast.
- **THORChain LP is currently paused (`PAUSELP`, checked 2026-07-05).** A RUNE
  symmetric add is refunded today; the existing `lp_deposit_pause_reason` gate
  refuses it. **Maya is OPEN**, so an asset+CACAO add works now.

## Mechanics

For pool `X.Y` (e.g. `BTC.BTC`) on a backend whose protocol asset is `P`
(`THOR.RUNE` / `MAYA.CACAO`), a symmetric add is **two linked deposits**:

| Leg | Where | Memo | Pairs on |
|---|---|---|---|
| asset | `X`'s inbound vault | `+:X.Y:<P-address>` | your protocol address |
| protocol | native `MsgDeposit` on `P`'s chain | `+:X.Y:<X-address>` | your asset-chain address |

The protocol pairs the two by matching each memo's referenced address against
the **other leg's observed sender**, within a time window. So the address you
put in the protocol-leg memo **must equal the asset leg's observed sender** —
this is the crux (see per-asset caveats).

Implemented building blocks (all unit-tested):
- `liquidity.symmetric_add_memo(pool, paired_address)` — builds `+:POOL:addr`
  for either leg.
- `liquidity.pair_amount(asset_amount, balance_asset, balance_protocol)` —
  the protocol-asset amount at the current pool ratio. `asset_amount` and
  `balance_asset` are THORChain 1e8; `balance_protocol` is the protocol asset's
  **native** unit (RUNE 1e8, **CACAO 1e10** — verified against live Maya depths),
  so the result is already native.
- `CosmosAdapter.build_and_verify_native_deposit(memo, amount, …)` — the
  protocol leg: a native `MsgDeposit` carrying `P` with the LP memo, gated
  (`verify_cosmos_deposit`, no swap destination) exactly like a native swap.

## The safety protocol (implemented)

`prepare_symmetric_liquidity` does 0–5, `execute_symmetric_liquidity` does 6.

0. **Check the LP pause first**, before anything is built. This one deserves
   naming: `prepare_liquidity` checks `lp_deposit_pause_reason` on its way to
   the inbound vault, but the protocol leg has no vault and so never passes
   through it — left per-leg, the RUNE/CACAO half would be **ungated**, and a
   paused pool refunds an add minus gas on *both* chains.
1. Derive both addresses: asset-chain (`X`) and protocol-chain (`P`).
2. Fetch pool depth; `pair_amount` computes the protocol amount from the
   user-supplied asset amount (the chosen "auto-compute from pool ratio" model).
   Refuse up front if the wallet does not hold that much `P` — broadcasting a
   leg known to bounce is worse than refusing.
3. Build the **asset leg** (memo `+:X.Y:<P-addr>`) — but do **not** broadcast.
4. Build the **protocol leg** against the asset leg's **observed sender**
   (memo `+:X.Y:<that sender>`). For an account-model chain the sender is the
   single derived address, known before the build; this is why the CLI restricts
   `--symmetric` to those (`_SYMMETRIC_ASSET_CHAINS`).
5. Run the verify gate on **both** legs. If either fails, abort with **neither**
   broadcast — `SymmetricPrepared.problems` labels which leg objected.
6. On `--confirm`: broadcast the protocol leg (native, cheap, fast), then the
   asset leg. A failure on the *first* leaves nothing live and propagates as an
   ordinary `BroadcastError` — the benign case, and the one we can still choose.
   A failure on the *second* raises `PartialSymmetricAdd`, which carries the
   live txid so the CLI can say what is on-chain rather than reporting a bare
   failure. Never silently leave a half-add.

## Per-asset caveats for step 4 (the pairing address)

- **Account-model assets (ETH, ARB):** the sender is the single derived address
  — unambiguous, and the *same* address on both chains. Maya has `ETH.ETH`,
  `ETH.USDC`, `ARB.ETH` and `ARB.USDC` OPEN. `_SYMMETRIC_ASSET_CHAINS` in
  `cli.py` is the list; widening it to a further EVM chain is a one-line change
  once that chain has an adapter.
- **UTXO assets (BTC):** a multi-input tx has no single "from"; the protocol
  observes (by convention) the **first input's** address. So the protocol-leg
  memo would have to use the built asset tx's `vin[0]` address, or the add be
  constrained to spend from a single address. This is an **unverified
  assumption** (no testnet) — get it wrong and the legs don't pair, so the CLI
  refuses `--symmetric` for UTXO chains rather than guessing.

## Withdraw

A symmetric position is withdrawn with the ordinary `-:POOL:<bps>` trigger from
either owned address; the protocol returns both sides proportionally. The
existing single-sided withdraw path already builds this memo — symmetric
withdraw mainly needs the trigger to come from an owned address on either side.

## See also

- `docs/cacao.md` — the shared Cosmos adapter (`chains/cosmos.py`) that the
  protocol leg reuses.
- `docs/TODO.md` *Next up* — the mainnet proving step that is still open.
