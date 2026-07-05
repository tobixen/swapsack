# Two-sided (symmetric) liquidity — design notes

Status: **building blocks done + tested; two-leg CLI orchestration is the
remaining (and riskiest) step.** This note records the mechanics so the
money-sensitive coordination is decided deliberately, not mid-broadcast.

## Why symmetric, and the honest caveats

A symmetric add provides *both* sides of a pool at the current ratio, so it
takes **no entry slip** (unlike a single-sided add, which the pool has to
rebalance). In exchange you must source and hold the protocol asset
(RUNE on THORChain, CACAO on Maya) and you take on a second on-chain tx.

- **Two irreversible txs on two chains that must pair.** New failure mode vs.
  every single-leg path: if one leg lands and the other doesn't, the position
  sits *pending* (or is refunded after a timeout) — lopsided/stuck funds.
- **Unproven on mainnet, doubled.** No THORChain/Maya testnet; two money legs.
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

## The safety protocol for the two-leg CLI (remaining work)

1. Derive both addresses: asset-chain (`X`) and protocol-chain (`P`).
2. Fetch pool depth; `pair_amount` computes the protocol amount from the
   user-supplied asset amount (the chosen "auto-compute from pool ratio" model).
3. Build the **asset leg** (memo `+:X.Y:<P-addr>`) — but do **not** broadcast.
4. Read the asset leg's **observed sender** and build the **protocol leg**
   (memo `+:X.Y:<that sender>`).
5. Run the verify gate on **both** legs. If either fails, abort with **neither**
   broadcast.
6. On `--confirm`: broadcast the protocol leg (native, cheap, fast), then the
   asset leg. If the second fails after the first is out, report **loudly** that
   one leg is live and the position is pending, with the txid and recovery hint.
   Never silently leave a half-add.

## Per-asset caveats for step 4 (the pairing address)

- **Account-model assets (ETH):** the sender is the single derived address —
  unambiguous. Cleanest first target. Maya has `ETH.ETH` OPEN.
- **UTXO assets (BTC):** a multi-input tx has no single "from"; the protocol
  observes (by convention) the **first input's** address. So the protocol-leg
  memo must use the built asset tx's `vin[0]` address, or the add must be
  constrained to spend from a single address. This is an **unverified
  assumption** (no testnet) — get it wrong and the legs don't pair.

## Withdraw

A symmetric position is withdrawn with the ordinary `-:POOL:<bps>` trigger from
either owned address; the protocol returns both sides proportionally. The
existing single-sided withdraw path already builds this memo — symmetric
withdraw mainly needs the trigger to come from an owned address on either side.

## See also

- `docs/cacao.md` — the shared Cosmos adapter (`chains/cosmos.py`) that both legs'
  protocol side reuses.
- `docs/TODO.md` #4 — the original scoping.
