# What is this

It's a **Python library** and a **CLI** for holding, sending, receiving and swapping **multiple** cryptocurrencies.

Non-custodial cross-chain swaps are supported via [THORChain](https://thorchain.org/), [Maya](https://www.mayaprotocol.com/) and [Chainflip](https://chainflip.io/); same-chain ETH-token swaps additionally route through [CoW Protocol](https://cow.fi/)'s keyless intent API.

⚠️ This project is vibed-up ... what could possibly go wrong?

**Don't use this wallet for more funds than what you can afford to lose**.  Bugs in the code may easily cause **irreversible loss of funds**.  Even if all the code is perfect, consider that this is a **hot wallet**, an attacker that gains a foothold on the computer running this wallet software may potentially manage to drain the funds in the wallet.

The rest of this document is partially AI-generated.

## Installation

```
make install
```

This auto-detects `uv`, `pipx`, or `pip` and installs the `swapsack`
binary on your PATH. Then run `swapsack --help`.

## Features

The wallet is still under rapid development as of 2026-07-10.  Missing features and currency support will be prioritized by personal need and by issues/PRs received.  Here is the "current status" of (partially) supported currencies (✅ = working, ◑ = partial, blank = not yet):

<!-- REMEMBER when editing: there is another table further down that also needs to be updated -->

| Currency  | Hold | Bal | To  | From | Send | Sweep | Liq |
|-----------|:----:|:---:|:---:|:----:|:----:|:-----:|:---:|
| BTC       |  ✅  |  ✅ |  ✅ |  ✅  |  ✅  |  ✅  |  ✅ |
| ETH       |  ✅  |  ✅ |  ✅ |  ✅  |  ✅  |  ✅  |  ✅ |
| USDT-ETH  |  ✅  |  ✅ |  ✅ |  ✅  |  ✅  |  ✅  |  ✅ |
| USDC-ETH  |  ✅  |  ✅ |  ✅ |  ✅  |  ◑   |  ✅  |     |
| TRX       |  ✅  |  ✅ |  ✅ |  ✅  |  ✅  |      |  ✅ |
| USDT-TRON |  ✅  |  ✅ |  ✅ |  ✅  |  ✅  |  ✅  |     |
| BNB (BSC) |  ✅  |  ✅ |     |      |      |      |     |
| LTC       |      |     |  ✅ |      |      |      |     |
| DOGE      |      |     |  ✅ |      |      |      |     |
| BCH       |      |     |  ✅ |      |      |      |     |
| ATOM      |      |     |  ✅ |      |      |      |     |
| XRP       |      |     |  ✅ |      |      |      |     |
| ADA       |      |     |  ◑  |      |      |      |     |
| ETH-ARB   |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |  ◑   |  ◑  |
| USDC-ARB  |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |  ◑   |  ◑  |
| USDC-AVAX |      |     |  ✅ |      |      |      |     |
| DASH      |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |  ◑   |  ◑  |
| ZEC       |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |  ◑   |  ◑  |
| CACAO     |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |      |     |
| RUNE      |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |      |     |

### Features explained

* **Hold** — derive an `address`, hold a balance, receive funds
* **Bal**  — show the `balance`: one aligned sheet of native coins, tracked tokens like USDT, and any THORChain/Maya liquidity positions, each row valued and totalled (see below)
* **To**   — use as a `swap` *destination* (for a currency whose address the wallet can't derive yet, give an external one via `--dest`)
* **From** — use as a `swap` *source* (the asset you spend). ◑ = the native swap-from for CACAO/RUNE (a Cosmos `MsgDeposit`, no inbound vault) is implemented + gated + unit-tested, and the underlying `MsgDeposit` build/sign/broadcast is now **mainnet-proven** on Maya (the symmetric LP leg of [docs/live-session-2026-08-16.md](docs/live-session-2026-08-16.md)) — but the swap variant's own memo/destination binding has not itself broadcast, and nothing RUNE has; there is no Maya/THORChain testnet wired up. The same ◑ covers ARB (implemented, never broadcast) and DASH and ZEC (vault + OP_RETURN memo, Maya-only, no testnet; ZEC over its bespoke signer)
* **Send** — `send` to an external address (a plain transfer, no swap). ✅ = implemented and tested; ◑ = USDC-ETH rides the *same* ERC-20 send path as USDT-ETH (only the contract/decimals differ) but isn't separately covered by a test; the native CACAO/RUNE Cosmos `MsgSend` is implemented + unit-tested (protobuf byte-exact vs cosmpy, signature verified) but its broadcast is **unproven on mainnet** — there is no Maya/THORChain testnet wired up; and the DASH legacy send shares the BTC build/gate/sign path and is unit-tested (signatures verified) but its broadcast is likewise **unproven on mainnet** (no Dash testnet — an opt-in mainnet self-sweep test exists, see docs/testnet.md); ZEC rides a bespoke v4/ZIP-243 signer (bitcoinlib can't sign Zcash) whose sighash is verified against a real mainnet transaction's signature, with the same unproven-broadcast caveat
* **Sweep** — `--amount max` sends the maximum amount. ✅ = works: UTXO and token sweeps end at 0 (a token's gas is paid in the native coin); **native account coins (ETH/TRX) intentionally retain a small gas reserve** — the fee is only known at send time, and you *want* some left to move tokens or swap later, so the wallet warns rather than draining you to 0. ◑ = DASH/ZEC sweeps end at 0 like BTC but ride the mainnet-unproven broadcasts above. Blank = not yet (native TRX).
* **Liq**  — `add-liquidity` and `withdraw-liquidity` provide/withdraw liquidity, single-sided by default and *two-sided* with `--symmetric` (EVM assets only, pairing with your own RUNE/CACAO), now including ERC-20 tokens (e.g. USDT-ETH on Maya, via the router). ◑ = DASH/ZEC LP is Maya-only (`--backend maya`, pairs with CACAO) and rides their mainnet-unproven broadcasts; ARB LP is likewise Maya-only and mainnet-unproven. Experimental; see below.

Other features:

* `quote` — read-only price preview for any supported asset
* `status` — track a swap by its inbound txid. For a BTC hash it also prints what the transaction did on-chain (inputs, each output, change, fee in sats and EUR) — so a plain `send`, which no swap vault ever observes, still shows something useful instead of an empty stage list. **Chainflip swaps are tracked too**, keyed by that same deposit txid (a vault swap leaves no channel or order id behind): it reports Chainflip's own state, what went in, what came out and the payout transaction. That lookup runs before the THORChain/Maya one, because a Chainflip deposit is invisible to those and their honest "not observed" reads as *your money went nowhere*. An amount in an asset this wallet has no key for (SOL, DOT, …) is shown in base units and labelled, rather than scaled by guessed decimals.
* `--backend auto` — compares **THORChain + Maya + CoW + Chainflip** (CoW only quotes same-chain ETH-token pairs) and routes to the best price (`quote`, `swap`). `--backend cow` forces it: a same-chain USDT-ETH/USDC-ETH/ETH swap settles via a signed EIP-712 order (no vault, no memo) instead of THORChain/Maya's two-pool-leg route — see [docs/backends.md](docs/backends.md). `status <order-uid>` tracks a submitted CoW order (auto-detected by its 56-byte uid shape, vs. a chain txid).
* `--backend chainflip` — an independent cross-chain protocol, so it keeps working when THORChain and Maya halt together (as they did on 2026-08-18 — see [docs/halt-alternatives.md](docs/halt-alternatives.md)). **Swapping from BTC** settles as a *vault swap*: one ordinary Bitcoin transaction paying a protocol vault, with the destination and an on-chain minimum-output floor encoded in its OP_RETURN — no broker, no deposit channel, and nothing registered on your behalf. The gate decodes those bytes **itself** before signing and checks they pay your address, clear your floor, and carry no broker/boost/affiliate fee; the deposit address must appear in the vault list the chain publishes. Destinations are the EVM assets the gate can re-derive an address for (ETH/ARB native and USDC/USDT); other sources and destinations quote but don't execute there, and `auto` routes around them while *saying so* when they were the cheaper route. `--amount max` can't be a vault swap — Chainflip needs a change output above dust to refund to. See [docs/chainflip-effort.md](docs/chainflip-effort.md).
* `swap --tolerance-bps N` — raise the slippage/fee tolerance (default 300 = 3%). Small or thinly-traded swaps whose fees exceed the default are *refused* by THORChain; the wallet aborts with a clear message instead of a traceback, and you can opt into a wider tolerance here.
* **cost breakdown** — `quote` and `swap` itemise what you lose: the slip/swap (liquidity) fee, the flat outbound fee, and the quoted total (with `bps`), plus the inbound (source-chain) tx fee shown separately. On THORChain the *liquidity fee is the slippage* — the two are one number, not two.
* **`Market:` block** — by default `quote`/`swap` also compare the quoted output against a public spot price (CoinGecko), surfacing the *total* realised cost including the pool-vs-market spread arbitrageurs capture (which the protocol's own fee fields don't include). Three lines: a source header, the per-asset comparison (`~X DEST at spot → ~N bps total vs market`), and the estimated absolute loss in **EUR**. Best-effort: silently dropped if the feed is unreachable or the asset isn't mapped (the EUR line is dropped if the feed has no EUR price). Disable with `--no-price-check`, which is accepted by every command that prices anything (`quote`, `swap`, `send`, `add-liquidity`, `withdraw-liquidity`, `status`, `balance`) and suppresses the request itself, not merely its output — the lookup would otherwise tell a third party that you are about to spend that asset.
* **Streaming swaps** — `swap`/`quote --stream-interval N [--stream-quantity M]` spreads the trade over blocks (sub-swaps) so each hits the pool smaller, sharply cutting slippage on large or thinly-pooled swaps (e.g. a 0.05 BTC→DASH that's refused at the default tolerance clears at ~20 bps when streamed). `N` = blocks between sub-swaps; `M` = number of sub-swaps (omit to let the network pick). Streaming manages slippage itself, so it overrides `--tolerance-bps` (the memo's limit is set to 0). The tradeoff: the swap settles over more blocks (`quote` prints the estimated duration), during which your funds are in-flight and exposed to price movement. See [docs/streaming.md](docs/streaming.md) for the mechanics and the streaming-vs-tolerance interaction.
* Transaction listings are not supported yet.

**The balance sheet.** `balance` prints one aligned table once every chain has
answered (progress goes to stderr, so the table on stdout stays a table):

```
BTC                       0.00000000      €0.00  2 used addresses
  +LP maya BTC.BTC       ~0.00162822    €159.57  deposited ~0.00162545; 0.00001600 via CACAO
ETH                       0.05606159    €173.79  0x…
USDC-ETH                 40.94348000     €35.21

spendable                               €217.60
liquidity                             €10879.47  not spendable; gross of exit fees
total                                 €11097.07
zero: USDT-ETH, ETH-ARB, USDC-ARB, BNB, DASH, RUNE
```

* **Liquidity is totalled apart from spendable funds.** An LP position is not
  liquid and its redeemable figure is gross of exit fees, so it is never folded
  into one number that reads like cash. A `~` marks an amount that includes the
  RUNE/CACAO side repriced at the current pool rate.
* **A row that cannot be priced is named, never counted as zero** — a total that
  quietly omits something is worse than no total.
* **Rows worth nothing collapse** into the trailing `zero:` line, so nothing
  vanishes without being named. `--zeros` gives each its own row.
* `--unit` denominates the value column and the total (`EUR` `USD` `USDT` `USDC`
  `BTC` `ETH` `SATS`; the dollar stablecoins price in USD, as CoinGecko has no
  stablecoin rate). `--no-price-check` makes **no** price request at all — one
  lookup would otherwise tell a third party every asset this wallet holds — and
  prints the amounts alone. A price feed that fails costs you the value column
  and nothing else.

**Liquidity (experimental).** `add-liquidity` / `withdraw-liquidity` add or
remove liquidity on a THORChain pool.  By adding liquidity one will earn a share of that pool's swap fees, but it's not without risks.  As of 2026-06-28 THORChain rejects new liquidity for all assets, probably due to a switch to protocol-owned liquidity (POL).  It's still possible to use `add-liquidity --backend maya`.

For bigger amounts, *double-sided* (symmetric) liquidity is preferable to
single-sided: it enters at the current pool ratio and so takes **no entry
slip**.  `add-liquidity --symmetric` does this, currently for **EVM assets
(Ethereum and Arbitrum)** — see below for what it costs you in exchange.

### Symmetric liquidity (two-sided)

```sh
swapsack add-liquidity --asset USDC-ETH --amount 100 --symmetric --backend maya
```

A symmetric add is **two linked deposits**: the asset leg goes to the inbound
vault with memo `+:POOL:<your maya1/thor1 address>`, and the protocol leg is a
native RUNE/CACAO `MsgDeposit` with memo `+:POOL:<your asset-chain address>`.
The protocol pairs them by matching each memo's referenced address against the
*other* leg's observed sender.  You supply `--amount` for the asset side; the
RUNE/CACAO amount is computed from the live pool ratio and must already be in
your own wallet (`swap --to CACAO --dest maya1…` is how you get it).

What you get and what it costs:

* **No entry slip** — you enter at the pool's current ratio instead of making
  the pool rebalance around a one-sided deposit.
* **It does *not* reduce your RUNE/CACAO exposure.**  A single-sided add ends up
  ~50% exposed to the settlement asset anyway once the pool rebalances.  On a
  stablecoin pool that means half a nominally dollar-stable position is a
  small-cap protocol token either way — go in knowing that.
* **Two irreversible transactions on two chains.**  Both legs are built and
  gated before *either* is broadcast, and the CLI refuses to send anything if
  either gate fails.  The protocol (cheap, fast, native) leg goes first, so a
  failure there leaves nothing live.  If the asset leg then fails, the position
  is genuinely half-added and the CLI says so loudly, with the live txid — wait
  for the protocol to refund the unpaired leg before retrying.
* **EVM assets only** (Ethereum and Arbitrum).  The pairing depends on the asset
  leg having one unambiguous sender, which an account-model chain has and a UTXO
  transaction does not (the `vin[0]` convention is an assumption no testnet
  exists to verify).  `--amount max` is refused for the same reason the amount
  must be definite: the pair leg is derived from it.
* THORChain has LP deposits globally paused (`PAUSELP`), so symmetric works on
  **Maya** (asset + CACAO) today; the CLI aborts with the mimir key if you aim
  it at a paused pool.  On **RUNE** the protocol leg is unproven — no THORChain
  native transaction has ever been broadcast.
* **Exiting works the same way you got in — from the protocol side.**
  `withdraw-liquidity` looks up which kind of position you hold and, for a
  symmetric one, triggers the withdraw with a dust `MsgDeposit` on
  Maya/THORChain instead of a transaction on the asset chain (that is where the
  protocol files the position, so an asset-chain trigger would find nothing).
  Nothing to pass: `swapsack withdraw-liquidity --asset USDC-ETH --backend maya`
  picks the right one.  Both sides come back proportionally — the asset to your
  asset address, the CACAO to your `maya1…`.

See [docs/liquidity-symmetric.md](docs/liquidity-symmetric.md) for the
mechanics and the full safety protocol.

## Currency roadmap

It's on the roadmap to support the union of the currency sets
supported by the available swapping backends. **Support**:
full = every feature working, partial = some features working, none =
planned. Listed in recommended implementation order; see the
capability grid above for the per-feature detail.

<!-- REMEMBER when editing: there is another table further up that also needs to be kept in sync -->

| Currency | What it is | Family | Support | Notes |
|---|---|---|:--:|---|
| BTC | Bitcoin | UTXO | full | |
| ETH | Ethereum | EVM | full | |
| TRX | TRON | TRON | partial | `send` done; sweep pending |
| BSC / BNB | BNB Smart Chain | EVM | partial | Hold + balance work (native BNB and BEP-20 USDC/USDT, 18-decimal). Swaps blocked: BSC trading halted on THORChain (`chain_trading_paused`), and Maya has no BSC pools — nothing to swap against until THORChain re-enables it |
| USDT-ETH | Tether | ERC-20 token | full | `send` + single-sided liquidity (Maya, via router) done |
| USDT-TRON | Tether | TRC-20 token | partial | `send` done |
| USDT-BSC | Tether | BEP-20 token | none | Blocked: halted on THORChain, not on Maya (Maya has no BSC pools) |
| USDT-SOL | Tether | SPL token | none | Not currently available on THORChain/Maya |
| AVAX | Avalanche C-Chain | EVM | partial | `USDC-AVAX` is a destination via `--dest`; native AVAX is not exposed yet. C-Chain (`0x…`, EIP-55 checked) only — an X-/P-Chain `X-avax1…` address is refused, since a payout could never credit it |
| BASE | Base (ETH L2) | EVM | none | Blocked: THORChain's `BASE.USDC` and `BASE.ETH` pools are `Available` but **trading-halted** (checked 2026-08-16), the same shape as the BSC block. Revisit when the halt lifts |
| ARB | Arbitrum (ETH L2) | EVM | partial | **Maya-only**. Every feature is wired: hold, balance, destination (auto-derived — it *is* your ETH address), send/sweep, swap-**from** and liquidity, single- and two-sided. `ETH-ARB` is native ETH on Arbitrum; the ARB *token* pool is `Staged`, not tradeable. `USDC-ARB` is Circle's **native** USDC (`0xaf88d065…`), not the bridged `USDC.e`. Partial because the spend paths ship **mainnet-unproven**. Mind the depth: Maya's `ARB.USDC` pool held ~8.9k USDC on 2026-08-16, so anything above ~€100 slips hard — the `Market:` line shows it |
| USDC | USD Coin (ETH/BSC/AVAX/BASE/ARB) | ERC-20 token | partial | ETH done (incl. `send`, via the shared ERC-20 path). AVAX and ARB are **destinations** (`--dest`) — receiving needs no adapter; *holding or spending* them still does. BASE and BSC are blocked by THORChain halts |
| LTC | Litecoin | UTXO | partial | destination only (via `--dest`) |
| DOGE | Dogecoin | UTXO | partial | destination only (via `--dest`) |
| BCH | Bitcoin Cash | UTXO | partial | destination only (via `--dest`) |
| ADA | Cardano | Cardano | partial | **Maya-only**; destination only (via `--dest`), Shelley `addr1…` bech32 (Byron `Ae2…`/`DdzFF…` not accepted — no verifiable checksum). Usually reachable **only from an account-model source** (ETH): the base address wallets normally hand out is 103 chars, which pushes the swap memo past the 80-byte OP_RETURN a BTC/DASH/ZEC source must fit it in — the CLI checks the actual address and refuses up front, so a shorter Shelley form (an enterprise address is 58 chars) does work from BTC |
| DASH | Dash | UTXO | partial | **Maya-only** (`--backend maya`/`auto`). Every feature is wired: hold, balance, destination, send/sweep, swap-**from** and single-sided LP (Maya, pairs with CACAO) — but all spend paths ship **mainnet-unproven** (no Dash testnet; opt-in mainnet test in docs/testnet.md), hence partial. See [docs/dash.md](docs/dash.md) |
| ZEC | Zcash | UTXO | partial | **Maya-only** (`--backend maya`/`auto`); transparent (`t1…`) addresses only. Every feature is wired: hold, balance, destination, send/sweep, swap-**from** and single-sided LP (Maya, pairs with CACAO) — the spend paths ride a bespoke v4/ZIP-243 signer with ZIP-317 fees (bitcoinlib can't sign Zcash), anchored to a real mainnet tx in the tests but shipping **mainnet-unproven** (no testnet; opt-in test in docs/testnet.md), hence partial. See [docs/zcash.md](docs/zcash.md) |
| RUNE | THORChain native | THORChain | partial | Hold + balance + destination + `send` (`MsgSend`) + swap-**from** (`MsgDeposit`) done — reuses the shared Cosmos-SDK adapter (RUNE is 1e8). Spend paths ship unproven on mainnet (no testnet); see [docs/cacao.md](docs/cacao.md) |
| CACAO | Maya native | Maya | partial | **Maya-only**; 1e10 decimals (not 1e8). Hold + balance + destination + `send` (`MsgSend`) + swap-**from** (`MsgDeposit`, no vault) done; single-sided liquidity n/a for the settlement asset — instead CACAO is the **protocol leg of `add-liquidity --symmetric`**, which is wired and **mainnet-proven** ([docs/live-session-2026-08-16.md](docs/live-session-2026-08-16.md)). The `MsgSend` path behind a plain `send` has still never broadcast; see [docs/cacao.md](docs/cacao.md) |
| ATOM | Cosmos Hub | Cosmos | partial | destination only (via `--dest`, a `cosmos1…` address) |
| XRP | XRP Ledger | XRP | partial | destination only (via `--dest`). Classic `r…` addresses only — THORChain rejects X-addresses and `address:tag`, so a payout **cannot carry a destination tag**; never send to an exchange deposit address that needs one |
| SOL | Solana | Solana | none | Blocked: `SOL.SOL` exists on THORChain but is **halted** (a live quote returns "trading is halted"), so there is nothing to swap against. Chainflip would be the other route — see [docs/chainflip.md](docs/chainflip.md) |
| XMR | Monero | Monero | none | Coming soon to THORChain pool; doesn't fit the current model — see [docs/monero.md](docs/monero.md) |
| TCY | THORChain reward token | THORChain token | none | niche; low priority |
| MAYA | Maya governance token | Maya token | none | Maya-only; niche; low priority |

## Usage

```sh
swapsack --help                              # subcmd --help also works
swapsack init                                # create encrypted keystore
swapsack add-hd --label main                 # import seed (prompted), or:
swapsack add-hd --label test --generate      # generate a fresh seed
swapsack address                             # BTC / ETH / TRON addresses
swapsack balance                             # balances across chains, valued in EUR
swapsack balance --unit BTC                  # …or in BTC / USD / USDT / ETH / SATS
swapsack balance --zeros --no-price-check    # every row, no external price request
swapsack quote --from ETH --to USDT-TRON --amount 0.02
swapsack swap  --from ETH --to BTC --amount max          # DRY RUN (sweep)
swapsack swap  --from BTC --to USDT-TRON --amount 0.001 --confirm
swapsack swap  --from BTC --to DASH --dest X... --stream-interval 1  # streamed, low slip
swapsack swap  --from USDT-ETH --to USDC-ETH --amount 100 --backend cow  # DRY RUN (CoW order)
swapsack send  bc1q...recipient --amount 0.001                 # DRY RUN
swapsack send  bc1q...recipient --amount max --confirm         # sweep + send
swapsack send  0x...recipient --asset ETH --amount 0.01        # native ETH
swapsack send  0x...recipient --asset USDT-ETH --amount max    # sweep tokens
swapsack send  T...recipient --asset USDT-TRON --amount 25     # TRC-20
swapsack send  bitcoin:bc1q...?label=Alice --amount 0.001      # BIP21 URI / QR code
swapsack bump  <txid>                                          # unstick a BTC tx (DRY RUN)
swapsack bump  <txid> --fee-rate 25 --confirm                  # …at an explicit sats/vB
swapsack history                                               # every BTC tx the wallet touched
swapsack history --asset DASH --json                           # …as JSON, for a spreadsheet
swapsack utxos                                                 # every output, spent ones included
swapsack utxos --unspent                                       # …only the coins you still hold
swapsack status <txid>                                         # what a transaction/swap did
```

**Listing what happened (`history`, `utxos`).** `history` prints every
transaction that touched the wallet — newest first, mempool at the top — with
the net effect on your balance, the full txid, who else was paid, and the
`OP_RETURN` memo that marks a swap deposit. `utxos` prints the same data sliced
per output instead: every output that ever paid you, each with its derivation
path and either `unspent` or the txid that spent it. Both take `--json`.

Two limits, both deliberate:

* **UTXO chains only** (`--asset BTC|DASH|ZEC`, default BTC). ETH/ARB/BSC talk
  plain JSON-RPC and TRON the java-tron HTTP API; none of those has an address
  history index, so a listing there would mean depending on an indexer
  (Etherscan, Blockscout, TronGrid) that this wallet does not use.
* **ZEC has no `history`.** lightwalletd's address index returns raw
  transactions rather than txids, and a post-NU5 (v5) txid is a ZIP-244 tree
  hash this wallet does not compute. `utxos --asset ZEC` still lists the
  unspent outputs — it just cannot show the spent ones, and says so.

Spent outputs cost no extra requests: an output paying your address can only be
spent by a transaction that also spends *from* that address, so the address
history already names every spender. That inference needs the history to be
complete, so a walk that hits `--limit` (default 500 transactions per address)
is reported as INCOMPLETE rather than passed off as the whole picture.

One limit is *not* detectable, and is worth knowing: both commands list the
addresses the gap-limit scan finds, exactly as `balance` does — the scan stops
after 20 consecutive unused addresses. Coins on an address beyond that gap, or
on a derivation path this wallet does not scan, are missing from these listings
with no warning, because nothing knows to look for them.

Recipients and `--dest` also accept BIP21-style payment URIs (`bitcoin:…`,
`litecoin:…`, `ethereum:…`, …) as pasted from a wallet or QR code. The scheme
must name the chain being spent — a `litecoin:` URI in a BTC send is refused —
and on `send`, an `amount=` in the URI that contradicts `--amount` aborts rather
than picking one. A `--dest` URI's other parameters are ignored: on a swap the
URI cannot know the output amount, so only the address is taken from it.

Defaults are `--from BTC --to ETH`. `--confirm` prints the freshly-quoted swap
and asks before broadcasting (`--yes` skips the prompt for automation).

Swaps default to a **dry run** (build + verify + print); `--confirm`
is required to broadcast, and `--yes` skips the interactive
confirmation prompt. Destination addresses auto-derive from the seed;
pass `--dest` to override.


Config via flags or env: keystore `$SWAPSACK_KEYSTORE`
(`~/.config/swapsack/keystore.json`), passphrase
`$SWAPSACK_PASSPHRASE`, Esplora `$SWAPSACK_ESPLORA` (default: try
`blockstream.info`, fall back to `mempool.space`; setting this uses
that endpoint alone), Ethereum
RPC `$SWAPSACK_ETH_RPC`, TRON API `$SWAPSACK_TRON_API`, BSC RPC
`$SWAPSACK_BSC_RPC`, Dash Insight API `$SWAPSACK_DASH_API`, Zcash
lightwalletd `$SWAPSACK_ZEC_LWD` (gRPC `host:port`), THORChain REST
`$SWAPSACK_THORNODE`.

**UTXO fee target.** BTC/DASH spends aim for confirmation within N
blocks (lower N = faster & pricier). Default is next-block-ish (2);
override per run with `--fee-blocks N`, or set a personal default via
`$SWAPSACK_FEE_BLOCKS` or a config file at
`~/.config/swapsack/config.toml` (`$SWAPSACK_CONFIG` to relocate):

```toml
[fees]
target_blocks = 4   # cheaper/slower; --fee-blocks and $SWAPSACK_FEE_BLOCKS win
```

Precedence: `--fee-blocks` › `$SWAPSACK_FEE_BLOCKS` › config file › default.

**Spending unconfirmed money.** By default only confirmed UTXOs are
spendable — an output still in the mempool can be replaced or evicted, and
whatever you built on it dies with it (no funds lost; the spend simply never
happens). `--allow-unconfirmed` opts into them anyway, on `send`, `swap` and
the liquidity commands. The spend then pays each unconfirmed parent's fee
shortfall on top of its own fee, so the parent+child *package* reaches the
targeted rate — child-pays-for-parent, which is the point: a fee-stuck
incoming transaction gets dragged into a block by the one you are sending
now. Confirmed coins are still selected first, and the extra fee may need a
higher `--max-fee` before the gate will pass it.

Two things it does not do: it looks one hop back only (a parent that is
itself spending unconfirmed money can leave the package short), and it does
not make a *swap* settle sooner — THORChain, Maya and Chainflip all wait for
their own confirmation count on the deposit regardless. On DASH the flag
spends mempool outputs with no surcharge (no replacement, flat fee rate); on
ZEC it has nothing to act on, and says so.

**Unsticking a transaction you already sent.** Every BTC spend signals BIP125
opt-in replace-by-fee, so `swapsack bump <txid>` can replace one that is sitting
in the mempool with an identical transaction paying more. Identical is meant
literally: the same inputs, the same recipient (or vault) output for the same
amount, the same OP_RETURN memo byte-for-byte — the extra fee comes out of the
change output and nothing else moves, because a THORChain/Maya deposit is
matched against exactly those bytes. The rebuild goes back through the same
verify gate a first-time spend passes, and like every spend it is a dry run
until `--confirm`. The replacement gets a **new txid**; the old one leaves the
mempool.

The fee target is `--fee-rate N` (sats/vB) or `--fee-blocks N` as everywhere
else, floored by what BIP125 requires to relay at all (1 sat/vB of the
transaction's size on top of the old fee). If the transaction spends inputs
whose own parents are still unconfirmed, the bump pays their shortfall too —
raising one transaction's rate is no use while an ancestor holds the package
down.

It is BTC-only and deliberately narrow. It refuses, with the reason, a
transaction that is already confirmed, one that does not signal RBF, one with
inputs this wallet cannot sign, one with no change output to take the bump from
(a `--amount max` sweep), one whose change would drop below dust (it names the
highest rate that *would* fit), and any shape it did not build itself. Dash
implements no mempool replacement at all — deliberately, for InstantSend — and
Zcash's transparent spends do not signal RBF; on both, child-pays-for-parent
(`--allow-unconfirmed`, above) is the only lever. Bumping a *swap* deposit does
not re-quote it: the memo still carries the min-out limit it was quoted at, so a
market that moved on refunds rather than fills.

**Shell tab-completion** (via argcomplete) for commands, subcommands and
options should need no setup at all: the install ships a completion file into
`<prefix>/share/bash-completion/completions/swapsack` (and
`share/zsh/site-functions/_swapsack`), which bash-completion and zsh find on
their own — including from a venv, `pipx` or `uv tool` tree. Start a new shell
after installing and press Tab.

If nothing happens, your setup is one of the cases that needs a nudge:

- **bash without the bash-completion package.** Install it (`pacman -S
  bash-completion`, `apt install bash-completion`) — its dynamic loader is what
  picks the shipped file up.
- **fish**, which has no such lookup:

  ```sh
  register-python-argcomplete --shell fish swapsack > ~/.config/fish/completions/swapsack.fish
  ```

- **anything else** — register it by hand for the current shell:

  ```sh
  eval "$(register-python-argcomplete swapsack)"   # add to ~/.bashrc to persist
  ```

argcomplete's [global completion](https://github.com/kislyuk/argcomplete#activating-global-completion)
(`activate-global-python-argcomplete`, one hook for every argcomplete-enabled
command) also works: `swapsack` carries the `PYTHON_ARGCOMPLETE_OK` marker that
hook requires.

## Related projects

This project started out from a personal need.  When asking Claude Opus to search for existing products, it found nothing.  Later, when searching for the (temporary) name of this package as well as doing research on possible permanent names, different software appeared on the radar.  Here is a comparison:

The CLI / library niche for *non-custodial cross-chain swaps* appears
unoccupied — GUI swap-wallets for phones, web and desktop are plentiful, but the
closest Python packages on PyPI do something else entirely:

- **[`pywallet`](https://github.com/ranaroussi/pywallet)** — a BIP32/HD
  key-and-address *generator* (BTC, ETH, LTC, DASH, DOGE, …). No balances, no
  broadcasting, no network I/O and no swaps; last released 2018. It's a
  key-derivation helper, not a spendable wallet.
- **[`multiwallet`](https://github.com/mflaxman/multiwallet)** — a PyQt5
  **desktop GUI** for *stateless multisig Bitcoin* (airgapped seedpicker +
  PSBT). Bitcoin-only, cold-storage focused, no swaps; last released 2020.

Neither is multi-chain *and* swap-capable from a terminal or as a library, which
is the gap this project fills.

### Name-collision neighbours on GitHub (surveyed 2026-07-08)

Several GitHub projects share a name with this project or live in the same
"crypto swap" search space; none turned out to compete in this niche:

- **[swaponline/MultiCurrencyWallet](https://github.com/swaponline/MultiCurrencyWallet)**
  — the only substantial one (~540 stars, MIT, TypeScript, still active). A
  client-side **web GUI** wallet (BTC, ETH/ERC-20, BSC, Polygon + tokens) with
  a P2P **atomic-swap** exchange and a 0x orderbook, aimed at white-label /
  embedded deployment (WordPress plugin, iframe widgets). Same spirit —
  non-custodial multi-currency wallet with built-in swapping — but a browser
  app rather than a library/CLI, and its swaps need a live counterparty on
  their own orderbook instead of an AMM.
- **[MatthewShelby/swap](https://github.com/MatthewShelby/swap)** — dead and mostly irrelevant.
- **[yoyoemily/crypto-swap](https://github.com/yoyoemily/crypto-swap)** — a
  small Node CLI / LLM-skill wrapper around **LightningEX**
  (`api.lightningex.io`), an instant-exchange service.  0 stars, but the closest in *shape* — swaps driven from a CLI. It holds no
  keys, though: it's an API client for a custodial exchange service, not a
  wallet.
- **ParaSwap-Crypto-Swap** (GitHub org, deliberately not linked) — **SEO spam
  impersonating** the real ParaSwap (whose actual code lives under
  [VeloraDEX](https://github.com/VeloraDEX)): a lone `.github` profile repo
  full of keyword stuffing, with a "GET ParaSwap" button pointing at a
  third-party `github.io` page. Avoid. The *real* ParaSwap/Velora is a
  same-chain EVM DEX aggregator — see below.

### Backend ideas from the survey

Scoped in depth (with live API probes) in [docs/backends.md](docs/backends.md);
the short version:

- **CoW Protocol** — **done** (`--backend cow`/`auto`): same-chain ETH-token
  swaps (where THORChain/Maya are at their worst) via a keyless API and an
  *intent* model (sign a structured EIP-712 order, solvers settle) that fits
  this wallet's verify-gate philosophy — unlike calldata-style aggregators
  (ParaSwap/1inch/0x/LiFi), whose opaque router calldata can't be
  independently gated.
- **Chainflip** — **done for BTC → EVM** (`--backend chainflip`, and in
  `auto`): a second *independent* non-custodial cross-chain venue that
  price-competes on BTC/ETH and keeps working when THORChain and Maya halt
  together. Executes as a **vault swap** — one Bitcoin transaction paying a
  protocol vault with the swap parameters in an OP_RETURN — which is the
  transaction shape the wallet already built for THORChain. Quoting covers more
  pairs than executing; EVM and Tron sources price but don't settle there yet.
  It also reaches **SOL and DOT**, which need wallet keys of their own first.
- **Instant-exchange APIs (LightningEX, ChangeNOW, SideShift, …)** — huge coin
  coverage behind a trivial REST API, but the operator holds your funds
  mid-swap (custodial in flight, occasionally KYC/AML-frozen), which cuts
  against this project's non-custodial premise. Not planned.
- **P2P atomic swaps (à la MultiCurrencyWallet)** — trust-minimized in theory,
  but they require a counterparty/orderbook network; there is no liquidity
  pool to route against.

## Development

```sh
make dev           # set up the environment (uv)
make test          # unit tests (live network tests excluded)
make test-network  # opt-in: read-only integration tests vs live THORChain
make lint          # ruff check + format check
```

The unit suite is **offline by design, and enforced**: `tests/conftest.py`
refuses outbound connections (sockets and grpc channels) in any test not marked
`network`, and names the offending test if one tries. Live I/O belongs behind
`-m network` — a test that quietly makes a real call does not look broken, it
looks flaky, and only when the remote host happens to be down. The guard cannot
see a client that connects from C without a Python socket, so
`unshare -rn -- uv run --no-sync pytest -q` (after a `uv sync`) is the
belt-and-braces check.

Most `network` tests are read-only (no funds moved); they guard against THORChain
API drift and stale hard-coded asset strings, and run in CI (the **Integration
(network)** workflow, on push/PR and a daily schedule) in addition to the
release gate.

One opt-in network test broadcasts a real **TRC-20 transfer on TRON's Nile
testnet** (build → sign → broadcast → confirm → read the memo back on-chain) to
exercise the USDT-TRON deposit mechanics end to end. It is skipped unless a
funded Nile account is provided via env / CI secrets:

```sh
SWAPSACK_NILE_MNEMONIC=...  # Nile account holding the token + some TRX
SWAPSACK_NILE_TOKEN=T...    # a TRC-20 contract (base58) the account holds
SWAPSACK_NILE_RECIPIENT=T...  # optional; defaults to a self-transfer
```

Two more opt-in tests (`tests/test_integration_testnet.py`) prove the **`send`
spending path end to end** on public testnets — build → sign → broadcast →
confirm a real (valueless) transfer, defaulting to a self-send. They skip unless
a funded testnet account our wallet *derives* is provided. The funding
addresses (and faucets) are documented in [docs/testnet.md](docs/testnet.md);
the seeds live only in CI secrets:

```sh
# BTC signet (sweeps the wallet's signet UTXOs to itself; testnet3 is deprecated)
SWAPSACK_BTC_TESTNET_MNEMONIC=...    # a funded account
SWAPSACK_BTC_TESTNET_NETWORK=...     # optional; "signet" (default) / "testnet"
SWAPSACK_BTC_TESTNET_ESPLORA=...     # optional; defaults to blockstream <network>
SWAPSACK_BTC_TESTNET_RECIPIENT=tb1.. # optional; defaults to a self-send

# ETH Sepolia (self-sends 0.001 ETH, chain id 11155111)
SWAPSACK_ETH_SEPOLIA_MNEMONIC=...    # a funded Sepolia account
SWAPSACK_ETH_SEPOLIA_RPC=...         # optional; defaults to a public Sepolia RPC
SWAPSACK_ETH_SEPOLIA_RECIPIENT=0x..  # optional; defaults to a self-send

# DASH — MAINNET (no Dash testnet path; a self-sweep, fee ~450 duffs)
SWAPSACK_DASH_MNEMONIC=...           # a funded mainnet account (keep it tiny)
SWAPSACK_DASH_RECIPIENT=X...         # optional; defaults to a self-send

# ZEC — MAINNET (no Zcash testnet path; a self-sweep, ZIP-317 fee 10000 zat)
SWAPSACK_ZEC_MNEMONIC=...            # a funded mainnet account (keep it tiny)
SWAPSACK_ZEC_RECIPIENT=t1...         # optional; defaults to a self-send
```

## Releasing

Versioning is automatic from git tags (hatch-vcs). Pushing a `v*` tag triggers
`.github/workflows/publish.yml`, which runs lint + the **full** test suite
*including* the live integration tests (`pytest -m network`) and only then builds
and publishes to PyPI via trusted publishing — so a THORChain outage blocks a
release. (Configure trusted publishing once at pypi.org.) `pre-commit` runs ruff
plus a Conventional-Commits message check on commit, and the unit tests plus a
lychee link check on push. Run `make dev` once to install the hooks.

## Refreshing test fixtures

The fixtures in `tests/` are trimmed real responses from the THORChain REST API:

```sh
curl -s "https://thornode.thorchain.network/thorchain/quote/swap?from_asset=BTC.BTC&to_asset=ETH.ETH&amount=178100"
curl -s "https://thornode.thorchain.network/thorchain/inbound_addresses"
```

## Documentation

Deeper topic notes live in [`docs/`](docs/). Most are also linked inline above
where they're relevant; this is the full index:

- [docs/TODO.md](docs/TODO.md) — the running backlog and roadmap (the live source of "what's next")
- [docs/backends.md](docs/backends.md) — swap backends (THORChain, Maya, CoW, and Chainflip)
- [docs/chainflip.md](docs/chainflip.md) — Chainflip execution notes (deposit channels, broker decision)
- [docs/streaming.md](docs/streaming.md) — streaming swaps and the streaming-vs-tolerance interaction
- [docs/liquidity-symmetric.md](docs/liquidity-symmetric.md) — two-sided (symmetric) liquidity mechanics + the two-leg safety protocol
- [docs/dash.md](docs/dash.md) · [docs/zcash.md](docs/zcash.md) — the Maya-only legacy-UTXO chains (their bespoke signing/fee specifics)
- [docs/cacao.md](docs/cacao.md) — Maya CACAO and the shared Cosmos-SDK adapter (also covers RUNE)
- [docs/monero.md](docs/monero.md) — why XMR doesn't fit the model yet, and the open custody choices
- [docs/testnet.md](docs/testnet.md) — funded testnet/mainnet broadcast tests: addresses to fund and faucets
- [docs/live-session-2026-07-24.md](docs/live-session-2026-07-24.md) · [docs/live-session-2026-08-16.md](docs/live-session-2026-08-16.md) — real-funds mainnet run-throughs. These are what "mainnet-proven" above cites; anything not exercised in one of them has never actually broadcast. The 2026-07-24 run used a throwaway wallet, so it keeps its addresses, amounts and txids; the 2026-08-16 run used a personal wallet and is redacted accordingly

(`docs/code-review-*.md` are dated review snapshots, not reference docs.)
