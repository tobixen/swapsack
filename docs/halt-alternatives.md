This document was AI-generated.  It looks more like "internal thinking" than valuable permanent documentation, so it should most likely be deleted at some point.

# BTC → ETH while THORChain and Maya are both halted

Status: **survey, nothing built.** Live-probed 2026-08-28 ~09:20 UTC. Written
because both of the wallet's cross-chain backends went dark at once, and the
owner asked to widen the search to the two classes `docs/backends.md`
dismissed in a single table row: **custodial instant exchangers** and
**orderbook venues**. Style/spirit as `docs/backends.md` and
`docs/chainflip.md`.

The short version: widening the search does not change the answer. Chainflip —
already the scoped Phase B, non-custodial — beats every custodial exchanger and
every orderbook route *on price*, before the custody argument is even reached.

## 1. The outage, as the chains report it

Both nodes answer and both chains are producing blocks. This is a governance
trading halt, not an infrastructure failure — our own fallback lists cannot
route around it.

| | THORChain | Maya |
|---|---|---|
| node reachable | ✅ `gateway.liquify.com` → `{"ping":"pong"}` | ✅ `mayanode.mayachain.info` → `{"ping":"pong"}` |
| block height | 27,591,949 (advancing) | 18,120,582 (advancing) |
| `inbound_addresses` BTC | `halted: true`, `global_trading_paused: true` | `halted: true`, `chain_trading_paused: true` |
| `inbound_addresses` ETH | `halted: true`, `global_trading_paused: true` | `halted: true`, `chain_trading_paused: true` |
| mimir | `HALTTRADING=1`, `HALTCHURNING=1`, `PAUSELP=1`, `SOLVENCYHALTSOLCHAIN=27591932` | `HALTCHAINGLOBAL=1` |

**Cause and likely duration.** Maya was drained of ~$1.7M on 2026-08-18 by a
single 23-message transaction chaining six bugs across trade accounts, outbound
processing and LP maths, and halted globally. THORChain's own halt is
concurrent; the public reporting does not pin its trigger, and the live mimir is
the only thing here worth trusting. For a duration estimate the precedent is
THORChain's May 2026 GG20/TSS exploit: **five weeks offline**, resuming
2026-06-23. Maya is a THORChain fork sharing the trade-account code that was
exploited, so "days" is optimistic for either.

**Consequence for the wallet:** `swap`, `add-liquidity` and `withdraw-liquidity`
are unavailable for every asset. `balance`, `address`, `send` and `quote --backend
cow` (same-chain ETH tokens) are untouched — they never talk to a vault.

## 2. Reference price at probe time

Everything below is quoted against the same snapshot, so the basis-point
column is comparable within the survey (not across days):

- Kraken `XETHXXBT`: best ask **0.031368** → **31.8796 ETH/BTC**, book spread
  1.0 bps.
- CoinGecko: BTC €68,209 / $79,412, ETH €2,140.19 / $2,491.70 → 31.870 ETH/BTC.

Basis: **0.1 BTC in, ETH out, every venue fee included, source-chain send fee
excluded** (you pay that on every route, including the CEX deposit).

## 3. What 0.1 BTC actually buys, measured

| Venue | Model | ETH out | ETH/BTC | vs Kraken ask | Custody in flight |
|---|---|---:|---:|---:|---|
| **Chainflip** | non-custodial JIT AMM | **3.188354** | **31.8835** | **+1.2 bps** | protocol vault, no operator |
| Kraken `ETH/XBT` | orderbook, taker 0.40 % | ~3.17521 | 31.7521 | −40 bps | full custody + KYC |
| Exolix | custodial exchanger | 3.165442 | 31.6544 | −70 bps | operator holds it |
| ChangeNOW | custodial exchanger | 3.149445 | 31.4944 | −121 bps | operator holds it |
| SideShift.ai | custodial exchanger | 3.126789 | 31.2679 | −191 bps | operator holds it |
| Godex (fixed rate) | custodial exchanger | 3.110961¹ | 31.1096 | −241 bps | operator holds it |

¹ 3.11162265 quoted, less the 0.00066176 ETH withdrawal fee. Part of that gap
is the price-lock premium a fixed rate charges — it is not all margin.

Kraken's line is the *floor* of its cost: 0.40 % is the lowest-volume taker
tier (0.25 % maker), and an ETH withdrawal fee comes on top. A patient maker
order improves it by ~15 bps at the cost of not being filled.

**Chainflip holds up across sizes** (same probe run, fees included):

| Size | ETH out | ETH/BTC | vs Kraken ask |
|---:|---:|---:|---:|
| 0.005 BTC | 0.159240 | 31.8481 | −9.9 bps |
| 0.01 BTC | 0.318705 | 31.8705 | −2.9 bps |
| 0.05 BTC | 1.593644 | 31.8729 | −2.1 bps |
| 0.1 BTC | 3.188354 | 31.8835 | +1.2 bps |
| 0.5 BTC | 15.939018 | 31.8780 | −0.5 bps |
| 1 BTC | 31.870717 | 31.8707 | −2.8 bps |
| 5 BTC | 159.067618 | 31.8135 | −20.7 bps |

`lowLiquidityWarning: false` throughout; estimated duration ~1014 s (of which
~906 s is waiting for Bitcoin confirmations). Fee itemisation at 0.1 BTC:
INGRESS 239 sats, NETWORK 7.948411 USDC, EGRESS 0.0000223 ETH.

Other destinations from 0.1 BTC, for the assets this wallet already holds:
USDC-ETH **7937.98**, USDT-ETH **7941.16**, ETH-ARB **3.185828**, USDC-ARB
**7935.86**, SOL **74.5938**. (`Tron`/`TRX` and `Polkadot`/`DOT` returned HTTP
400 with the asset names `cf_supported_assets` lists — a naming detail to
resolve at implementation time, not a capability gap.)

## 4. How keyless each one really is

`docs/backends.md` treats "keyless" as a hard requirement for a CLI. Measured:

| Service | Quote | Execute |
|---|---|---|
| Chainflip | ✅ `GET /v2/quote`, no key | ⚠️ deposit channel needs a **broker**; `/v2/swaps/{id}` status is keyless |
| SideShift | ✅ `GET /v2/pair`, `POST /v2/quotes` | ❌ `POST /v2/shifts/variable` demands `affiliateId` — an account |
| ChangeNOW | ✅ v1 `exchange-amount` | ❌ v2 estimate is already `Unauthorized`; creating a tx needs a key |
| Exolix | ✅ `GET /api/v2/rate` | ❌ key |
| Godex | ✅ `POST /api/v1/info` (returns `rate_uuid` + expiry) | ❌ key |
| Trocador, SimpleSwap, StealthEX | ❌ `Missing API key` / `Wrong api key` / `Auth` — keyed even to *quote* | ❌ |
| Kraken | ✅ public ticker/depth | ❌ API key + KYC'd account |

Two implementation notes worth keeping:

- **`chainflip-swap.chainflip.io` 403s a default `python-urllib` User-Agent**
  and serves the same request fine under `curl/8.5.0`. This turned out **not**
  to affect us: `net.py` uses niquests, which sends `niquests/3.20.0` and gets a
  200 (checked 2026-08-28 against the project's own venv). Noted only so the
  403 is not rediscovered and mistaken for a block on the wallet.
- Chainflip's channel-open route is **not** on the swapping-service host —
  `/v2/swaps` is `Cannot POST`, and every guessed channel path 404s. That
  confirms `docs/chainflip.md`'s finding unchanged: opening a channel goes
  through a broker (the SDK's default hosted one, `chainflip-broker.io` with a
  free key, or self-hosted `chainflip-broker-api`), while the State Chain RPC
  `mainnet-rpc.chainflip.io` answers `cf_supported_assets` keyless and is what
  the verify gate would read the channel's registered destination back from.

## 5. Orderbook venues, considered as the owner asked

- **Centralised orderbooks (Kraken, Bitstamp, Coinbase, Binance).** Genuinely
  the deepest books — Kraken's `ETH/XBT` spread is 1.0 bps, an order of
  magnitude tighter than any AMM. That advantage is then eaten by a 40 bps taker
  fee, and the route costs KYC, an API key with withdrawal rights, and a window
  where an exchange holds the coins and can freeze the account. For *this pair*
  it is strictly worse than Chainflip on every axis. Where a CEX genuinely wins
  is elsewhere: fiat on/off ramps, limit orders, and assets no DEX lists.
- **Komodo DeFi Framework (ex-AtomicDEX/BarterDEX)** is the only real
  *non-custodial* orderbook for native BTC↔ETH: a P2P order book settled by
  HTLC atomic swaps, driven by a local `kdf` daemon over JSON-RPC — which would
  suit a Python CLI well. **Not probed live**, and two objections are structural:
  the daemon needs the seed imported, i.e. a *second* copy of the key material
  outside our keystore, which is exactly what `keystore.py` exists to avoid; and
  BTC/ETH book depth there is thin and unmeasured. Worth a live probe only if
  Chainflip is rejected.
- **Bisq** is not a route for this. Bisq 2 is fiat-for-BTC; Bisq 1's altcoin
  trades were deprecated, and settlement was manual anyway.
- On-chain orderbook DEXes (dYdX, Serum-likes, Vertex) do not custody native
  BTC — reaching them means bridging first, so the bridge is the real question.

## 6. Honest arguments against doing anything about this

1. **This is an outage, not a permanent state.** THORChain's last halt lasted
   five weeks. Cutting a *new money path* into a wallet whose own README says
   bugs "may easily cause irreversible loss of funds", under the time pressure
   of wanting a swap today, is the worst possible condition for that work. If
   the BTC must become ETH this week, do it by hand (§7); if it need not, wait.
2. **The custodial branch is refuted by its own numbers.** Handing coins to an
   anonymous operator with no recourse would at least be a *trade* if it bought
   something. It costs 70–241 bps more than a non-custodial route that is live
   right now. There is no version of this where a custodial exchanger backend is
   the right call for BTC→ETH; "loud labelling" would not fix a route that is
   simply worse.
3. **A custodial backend changes what swapsack is,** permanently, and adds a
   support surface — API keys, affiliate IDs, per-IP `createShift` permissions,
   KYC-triggered freezes — for the worst-priced route on the list.
4. **Chainflip's own gaps are unchanged by the urgency.** Deposit channels are
   single-use and expire; a late send can lose funds without refund parameters.
   That foot-gun wants a gate designed calmly, which is an argument for building
   it *after* the panic, not during.

## 7. What to actually do

**Now, by hand — no code, no custody.** Use Chainflip's own frontend
(`swap.chainflip.io`): give it the ETH destination, it returns a single-use BTC
deposit address, and you `swapsack send` to it. Two things make this honest
rather than a shortcut:

- It is the same protocol we would build against, so it is a stopgap that does
  not compromise the project's premise.
- **The verify gate can be performed manually.** Before funding, query
  `mainnet-rpc.chainflip.io` for `cf_get_open_deposit_channels` and confirm the
  channel's registered destination is *your* ETH address, plus its expiry.
  That is exactly what B2's gate would automate — do it with `curl` this once.

Mind the expiry, and prefer one send over the channel's lifetime.

**Then, in code — the phasing does not change, only its priority.** This outage
is the concrete case for `docs/chainflip.md`'s **B1**: wire the keyless quote in
as a read-only source for `auto`/`--backend chainflip`. It is small, carries zero
money-path risk, and would have made `quote` still answer today instead of
reporting that both backends are halted. **B2** (channel-open + State Chain
verify gate) then makes the execution path real. Nothing in this survey argues
for reopening the custodial or calldata-aggregator decisions.

## Sources

- Live probes 2026-08-28: `gateway.liquify.com/chain/thorchain_api`
  (`/thorchain/ping`, `/inbound_addresses`, `/mimir`, `/lastblock`),
  `mayanode.mayachain.info` (same routes), `chainflip-swap.chainflip.io/v2/quote`,
  `mainnet-rpc.chainflip.io` (`cf_supported_assets`), `api.kraken.com`
  (`Ticker`, `Depth`, `AssetPairs`), `api.coingecko.com`, `sideshift.ai/api/v2`,
  `exolix.com/api/v2`, `api.changenow.io/v1`, `api.godex.io/api/v1`,
  `api.trocador.app`, `api.simpleswap.io`, `api.stealthex.io`.
- Maya exploit, 2026-08-18: <https://crypto.news/maya-protocol-suffers-1-7-million-exploit-halts-network/>,
  <https://defimon.xyz/blog/maya-protocol-hack-august-2026>
- THORChain's May 2026 halt and five-week recovery:
  <https://www.coindesk.com/tech/2026/05/15/thorchain-halts-trading-after-usd10-million-cross-chain-exploit-rune-token-drops-12>,
  <https://www.kucoin.com/news/flash/thorchain-resumes-trading-after-5-week-pause-following-10-7m-exploit>
- `HALTTRADING` semantics: <https://dev.thorchain.org/concepts/network-halts.html>
