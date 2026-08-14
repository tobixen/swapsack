# Chainflip backend — feasibility assessment

Status: **scoping (B-phase), not built.** Live-probed 2026-07-13 against
mainnet. This note supersedes the "broker decision is open" caveat in
`docs/backends.md`'s Chainflip section: the key blocker — how to trust a
broker with your destination address — turns out to be resolvable, because a
channel's registered parameters are readable from a public keyless RPC. Style/
spirit as `docs/dash.md` and `docs/backends.md`.

## The three things a backend needs, checked against reality

### 1. Quote — keyless, works today

`chainflip-swap.chainflip.io/v2/quote` returns a live quote with no key. Probed
2026-07-13, 0.1 BTC → ETH:

```
GET https://chainflip-swap.chainflip.io/v2/quote
    ?srcChain=Bitcoin&srcAsset=BTC&destChain=Ethereum&destAsset=ETH&amount=10000000
-> egressAmount 3531454161178790746 (~3.53 ETH), itemized includedFees
   (INGRESS/NETWORK/EGRESS), poolInfo route, recommendedSlippageTolerancePercent
   1.75, plus a boostQuote variant.
```

Normalizing this into a `CowQuote`-style object with `expected_amount_out` in
1e8 units drops straight into `gather_quotes`/`best_quote` as a read-only quote
source — essentially no money-path risk. This is the cheap first step, exactly
mirroring how CoW was phased.

### 2. Assets it actually unlocks

`cf_supported_assets` on the public State Chain RPC (`mainnet-rpc.chainflip.io`,
keyless) returned 2026-07-13:

| Chain | Assets |
|---|---|
| Ethereum | ETH, FLIP, USDC, USDT, WBTC |
| Polkadot | DOT |
| Bitcoin | BTC |
| Arbitrum | ETH, USDC, USDT |
| Solana | **SOL**, USDC, USDT |
| Assethub | DOT, USDT, USDC |
| Tron | **TRX**, USDT |

Genuine gap-fillers vs THORChain/Maya: **SOL and DOT** (as the earlier scoping
claimed), plus a second independent venue and price competition for
BTC/ETH/USDC/USDT.

### 3. Execution — the real blocker, more tractable than feared

Depositing is a plain send to a per-swap channel address (reuses existing send
builders + gates). The catch: *opening* that channel needs a **broker** that
holds a State Chain key and signs the channel-open extrinsic. From the SDK the
broker URL is optional and **defaults to Chainflip's own hosted broker**, so
from our side it is keyless. Funds are never in broker custody — they go into
the protocol vault. The broker's only power is registering *what destination
the swap pays out to*.

## Why the gating objection is now resolvable (the key finding)

The earlier scoping flagged that a broker submits your destination address, so
you'd have to trust it. But `cf_all_open_deposit_channels` /
`cf_get_open_deposit_channels` are **public, keyless RPCs that return open
channels' registered parameters** (live records confirmed 2026-07-13). So the
verify gate can do exactly what it does for CoW: after the broker returns a
deposit address, independently query the State Chain to confirm the channel's
registered destination address == ours, plus expiry and refund params — *before*
releasing any funds to the deposit address. That closes the "trust the broker"
hole and makes Chainflip fit the verify-gate philosophy the same way CoW's
EIP-712 order fields do. This is the fact that moves the recommendation from
"blocked on a trust decision" to "buildable."

## Honest downsides / arguments against

- **Destination-only vs swap-from is very asymmetric.** Swapping *to* SOL/DOT is
  cheap — an `ASSET` entry + a `--dest` validation rule, like the DASH/ZEC
  destination-only work. Swapping *from* SOL or DOT needs full wallet-side
  signers for Solana/Polkadot — a large effort on the scale of DASH/ZEC Phase
  2/3, and neither chain is held today. So "adds SOL/DOT" realistically means
  **receive-side SOL/DOT** unless you commit to those signers.
- **Channel expiry is a new foot-gun.** Deposit addresses are single-use and
  expire by block; a send after expiry can lose funds absent refund params. The
  gate must bind channel expiry (like it binds quote expiry today) — non-trivial
  and worth a boiler-room warning in docs.
- **New event/RPC-scanning surface.** Verifying the channel means parsing State
  Chain records (SCALE-encoded — `state_getMetadata` returns raw SCALE); more
  moving parts than CoW's clean REST orderbook.
- **Default-broker liveness dependency.** Relying on Chainflip's hosted broker
  is a liveness dependency (the on-chain readback removes the *trust*
  dependency, not the *availability* one). Self-hosting `chainflip-broker-api`
  needs a funded/registered State Chain account — not keyless.

## Recommended phasing (mirrors CoW)

1. **B1 (small, do first):** wire the keyless quote into `auto` /
   `--backend chainflip` as a read-only quote source. Immediate value: price
   competition + resilience hedge, zero money-path risk.
2. **B2:** channel-open via the default broker + a **State Chain verify gate**
   (confirm destination / expiry / refund from `cf_*_open_deposit_channels`
   before funding), then execute as a plain gated send. Start with a pair the
   wallet already holds both sides of, or destination-only SOL/DOT.
3. **B3 (optional, large):** SOL/DOT send-side signers if swap-*from* those
   assets is wanted — a separate, roadmap-scale effort.

## Sources

- Chainflip SDK: <https://docs.chainflip.io/brokers/how-to-use-chainflip-sdk>
- Broker API: <https://docs.chainflip.io/brokers/broker-api>
- SwapKit broker/channel (third-party hosted broker example): the deep link read
  on 2026-07-13 has since 404'd, so this points at the section instead —
  <https://docs.swapkit.dev/swapkit-api>
- Live probes (2026-07-13): `chainflip-swap.chainflip.io/v2/quote`,
  `mainnet-rpc.chainflip.io` (`cf_supported_assets`, `cf_environment`,
  `cf_all_open_deposit_channels`, `cf_get_open_deposit_channels`).
