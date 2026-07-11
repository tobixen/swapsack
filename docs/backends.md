# More swap backends — scoping notes

Status: **scoping only, nothing implemented** (probed live 2026-07-11). This
note records what a new backend must provide, which candidates are actually
usable from a keyless CLI, and a recommended order — so the work starts from
decisions, not mid-way through a money path. Style/spirit as `docs/dash.md`.

## Why more backends at all

THORChain + Maya cover the core cross-chain need, and every added backend is a
new money path and maintenance surface. Honest reasons to add one anyway:

1. **Same-chain token↔token swaps are served badly.** USDT-ETH↔USDC-ETH
   routes through *two* cross-chain pool legs plus a flat outbound fee —
   dollars of cost for what a DEX does for ~0.1–0.3 % including gas. Measured
   2026-07-11 for 100 USDT → USDC (same block hour): CoW quoted **99.85 USDC**
   (settlement gas already inside), ParaSwap **99.95** (plus ~$0.10 gas paid
   separately), LiFi/Kyberswap **99.71** (plus ~$0.10 gas).
2. **Assets neither protocol lists** — SOL and DOT exist on Chainflip; nothing
   on THORChain/Maya serves them.
3. **Resilience.** `thornode.thorchain.network` (our default) had a DNS outage
   while this note was written. `--backend auto` across *independent*
   protocols also hedges against one protocol's infra/halt days.

## What a backend must provide (current abstraction)

`backends.Backend` is thornode-shaped: `client.quote_swap()` returns a `Quote`
(inbound vault + `=:`-memo + expected out in 1e8 units), `gather_quotes`/
`best_quote` price-route, and execution is "pay the vault with the memo" via
the per-chain adapters. Anything non-thornode needs: (a) a quote normalized to
expected-out-in-1e8 so `auto` can compare, and (b) its own execution path,
dispatched like the per-chain `_swap_from_*` handlers already are.

## Candidates (all probed live, 2026-07-11)

| Candidate | Keyless? | Model | Fills gap | Execution shape |
|---|---|---|---|---|
| **CoW Protocol** (`api.cow.fi`) | ✅ quote *and* order submission | same-chain intent/solver auction (ETH, ARB, GNO, BASE) | same-chain tokens | one-time ERC-20 approval to the vault relayer, then **sign an EIP-712 order** — no calldata, no per-swap gas tx; solvers settle atomically or the order expires harmlessly |
| **Chainflip** (`chainflip-swap.chainflip.io/v2/quote`) | ✅ quote; executing needs a **deposit channel** opened via a broker (see below) | cross-chain JIT AMM (BTC, ETH, ARB, SOL, DOT; USDC/USDT) | **SOL, DOT**; price-competes with THORChain on BTC/ETH | open channel → get a one-off deposit address → **plain send, no memo** — reuses our existing send builders + verify gates as-is |
| **ParaSwap/Velora** (`api.paraswap.io`) | ✅ (addresses must be lowercase) | same-chain DEX aggregator | same-chain tokens | returns **router calldata** to sign — see the gating problem below |
| **LiFi** (`li.quest`) | ✅ | aggregator-of-aggregators + bridges | same-chain + bridges | returns calldata — same gating problem |
| chainflip-broker.io | ❌ needs a (free) API key | hosted Chainflip broker | — | alternative to running `chainflip-broker-api` |
| 1inch, 0x | ❌ API key walls | same-chain aggregators | — | calldata |
| ChangeNOW / SideShift / LightningEX … | mixed | **instant exchangers — custodial in flight** | huge asset lists | against the project's non-custodial premise; only ever with loud labeling |

### The calldata-gating problem (why CoW over ParaSwap/1inch/LiFi)

This wallet's safety story is the verify gate: before signing, an independent
check binds what the tx *actually does* to what the quote promised. A CoW
order is structured fields (sellToken, buyToken, amounts, receiver, validTo) —
gateable exactly like a `SendPlan`. An aggregator's router calldata is an
opaque blob: verifying "this bytes-string swaps X for ≥Y to me" means either
trusting the API or reimplementing the router's semantics. That's the same
class of risk the gates exist to kill, so intent-style (CoW) fits the project;
calldata-style doesn't, regardless of price.

### Chainflip execution notes (for its phase)

Quoting is keyless REST (probed: 0.1 BTC → 3.57 ETH with itemized ingress/
egress/broker fees and a recommended slippage). Executing requires a deposit
channel from a **broker**: options are (a) the same swap-service the official
frontend uses, (b) self-hosting the open-source `chainflip-broker-api`
against a Chainflip node, or (c) a keyed third-party broker. Decide at
implementation time; (a) needs verifying it's stable/public. The deposit
itself is a *plain* transfer to the channel's address (channels expire, so the
gate must also bind the channel expiry — like quote expiry today). Tracking:
channel/swap status via the same service. Boiler-room risk to document: the
deposit address is single-use; sending after expiry loses funds unless
refunded — Chainflip has refund parameters worth setting.

## Recommendation

1. **Phase A — CoW Protocol** (same-chain ETH tokens): keyless end to end,
   intent model matches our gating philosophy, and the ETH adapter already
   has the ERC-20 plumbing (approval = a `transfer`-style tx; EIP-712 signing
   via eth-account). Wire as a quote source in `auto` for same-chain pairs
   (where THORChain/Maya are at their worst) + an execute path that posts the
   signed order and polls the order uid. No new chain adapters.
2. **Phase B — Chainflip** (cross-chain): brings SOL/DOT and a second
   independent cross-chain venue. Read-only first (quote in `auto`), then the
   broker/channel decision, then execution — which reuses the existing plain-
   send builders and gates.
3. **Not planned**: keyed aggregators (1inch/0x — key friction for a CLI),
   calldata-style keyless ones (ParaSwap/LiFi — gating problem; revisit only
   if CoW's coverage disappoints), instant exchangers (custodial).

## Abstraction changes both phases need

- A `Backend` protocol wider than "thornode client": `name`, capability
  (which (from, to) pairs it can serve), `quote() -> NormalizedQuote`
  (expected out in 1e8, expiry, fee breakdown for the cost display), and an
  executor discriminator the CLI dispatches on (memo-deposit / plain-deposit /
  signed-order).
- `gather_quotes` iterates the wider protocol; `best_quote` unchanged.
- `--backend` grows choices; `auto` stays lowest-price across whatever can
  serve the pair.
- `status` needs per-backend trackers (thorchain txid / CoW order uid /
  Chainflip channel).
