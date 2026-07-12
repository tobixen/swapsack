# What is this

It's a **Python library** and a **CLI** for holding, sending, receiving and swapping **multiple** cryptocurrencies.

Non-custodial cross-chain swaps are supported via [THORChain](https://thorchain.org/) and [Maya](https://www.mayaprotocol.com/); same-chain ETH-token swaps additionally route through [CoW Protocol](https://cow.fi/)'s keyless intent API.

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
| DASH      |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |  ◑   |  ◑  |
| ZEC       |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |  ◑   |  ◑  |
| CACAO     |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |      |     |
| RUNE      |  ✅  |  ✅ |  ✅ |  ◑   |  ◑   |      |     |

### Features explained

* **Hold** — derive an `address`, hold a balance, receive funds
* **Bal**  — show the `balance` (native, tracked tokens like USDT, and any THORChain/Maya liquidity positions)
* **To**   — use as a `swap` *destination* (for a currency whose address the wallet can't derive yet, give an external one via `--dest`)
* **From** — use as a `swap` *source* (the asset you spend). ◑ = the native swap-from for CACAO/RUNE (a Cosmos `MsgDeposit`, no inbound vault) is implemented + gated + unit-tested but its broadcast is **unproven on mainnet** — there is no Maya/THORChain testnet wired up; the same caveat covers DASH and ZEC (vault + OP_RETURN memo, Maya-only, no testnet; ZEC over its bespoke signer)
* **Send** — `send` to an external address (a plain transfer, no swap). ✅ = implemented and tested; ◑ = USDC-ETH rides the *same* ERC-20 send path as USDT-ETH (only the contract/decimals differ) but isn't separately covered by a test; the native CACAO/RUNE Cosmos `MsgSend` is implemented + unit-tested (protobuf byte-exact vs cosmpy, signature verified) but its broadcast is **unproven on mainnet** — there is no Maya/THORChain testnet wired up; and the DASH legacy send shares the BTC build/gate/sign path and is unit-tested (signatures verified) but its broadcast is likewise **unproven on mainnet** (no Dash testnet — an opt-in mainnet self-sweep test exists, see docs/testnet.md); ZEC rides a bespoke v4/ZIP-243 signer (bitcoinlib can't sign Zcash) whose sighash is verified against a real mainnet transaction's signature, with the same unproven-broadcast caveat
* **Sweep** — `--amount max` sends the maximum amount. ✅ = works: UTXO and token sweeps end at 0 (a token's gas is paid in the native coin); **native account coins (ETH/TRX) intentionally retain a small gas reserve** — the fee is only known at send time, and you *want* some left to move tokens or swap later, so the wallet warns rather than draining you to 0. ◑ = DASH/ZEC sweeps end at 0 like BTC but ride the mainnet-unproven broadcasts above. Blank = not yet (native TRX).
* **Liq**  — `add-liquidity` and `withdraw-liquidity` provide/withdraw *single-sided* liquidity, now including ERC-20 tokens (e.g. USDT-ETH on Maya, via the router). ◑ = DASH/ZEC LP is Maya-only (`--backend maya`, pairs with CACAO) and rides their mainnet-unproven broadcasts. Experimental; see below.

Other features:

* `quote` — read-only price preview for any supported asset
* `status` — track a swap by its inbound txid
* `--backend auto` — compares **THORChain + Maya + CoW** (CoW only quotes same-chain ETH-token pairs) and routes to the best price (`quote`, `swap`). `--backend cow` forces it: a same-chain USDT-ETH/USDC-ETH/ETH swap settles via a signed EIP-712 order (no vault, no memo) instead of THORChain/Maya's two-pool-leg route — see [docs/backends.md](docs/backends.md). `status <order-uid>` tracks a submitted CoW order (auto-detected by its 56-byte uid shape, vs. a chain txid).
* `swap --tolerance-bps N` — raise the slippage/fee tolerance (default 300 = 3%). Small or thinly-traded swaps whose fees exceed the default are *refused* by THORChain; the wallet aborts with a clear message instead of a traceback, and you can opt into a wider tolerance here.
* **cost breakdown** — `quote` and `swap` itemise what you lose: the slip/swap (liquidity) fee, the flat outbound fee, and the quoted total (with `bps`), plus the inbound (source-chain) tx fee shown separately. On THORChain the *liquidity fee is the slippage* — the two are one number, not two.
* **`Market:` block** — by default `quote`/`swap` also compare the quoted output against a public spot price (CoinGecko), surfacing the *total* realised cost including the pool-vs-market spread arbitrageurs capture (which the protocol's own fee fields don't include). Three lines: a source header, the per-asset comparison (`~X DEST at spot → ~N bps total vs market`), and the estimated absolute loss in **EUR**. Best-effort: silently dropped if the feed is unreachable or the asset isn't mapped (the EUR line is dropped if the feed has no EUR price). Disable with `--no-price-check`.
* **Streaming swaps** — `swap`/`quote --stream-interval N [--stream-quantity M]` spreads the trade over blocks (sub-swaps) so each hits the pool smaller, sharply cutting slippage on large or thinly-pooled swaps (e.g. a 0.05 BTC→DASH that's refused at the default tolerance clears at ~20 bps when streamed). `N` = blocks between sub-swaps; `M` = number of sub-swaps (omit to let the network pick). Streaming manages slippage itself, so it overrides `--tolerance-bps` (the memo's limit is set to 0). The tradeoff: the swap settles over more blocks (`quote` prints the estimated duration), during which your funds are in-flight and exposed to price movement. See [docs/streaming.md](docs/streaming.md) for the mechanics and the streaming-vs-tolerance interaction.
* Transaction listings are not supported yet.

**Liquidity (experimental).** `add-liquidity` / `withdraw-liquidity` add or
remove *single-sided* liquidity on a THORChain pool.  By adding liquidity one will earn a share of that pool's swap fees, but it's not without risks.  As of 2026-06-28 THORChain rejects new liquidity for all assets, probably due to a switch to protocol-owned liquidity (POL).  It's still possible to use `add-liquidity --backend maya`.  For bigger amounts, *double-sided* liquidity should be used rather than single-sided liquidity, but this is not supported yet.

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
| AVAX | Avalanche C-Chain | EVM | none | |
| BASE | Base (ETH L2) | EVM | none | |
| ARB | Arbitrum (ETH L2) | EVM | none | Maya-only |
| USDC | USD Coin (ETH/BSC/AVAX/BASE/ARB) | ERC-20 token | partial | ETH done (incl. `send`, via the shared ERC-20 path); AVAX/BASE/ARB need new EVM chain adapters; BSC additionally blocked by the THORChain halt |
| LTC | Litecoin | UTXO | partial | destination only (via `--dest`) |
| DOGE | Dogecoin | UTXO | partial | destination only (via `--dest`) |
| BCH | Bitcoin Cash | UTXO | partial | destination only (via `--dest`) |
| DASH | Dash | UTXO | partial | **Maya-only** (`--backend maya`/`auto`). Every feature is wired: hold, balance, destination, send/sweep, swap-**from** and single-sided LP (Maya, pairs with CACAO) — but all spend paths ship **mainnet-unproven** (no Dash testnet; opt-in mainnet test in docs/testnet.md), hence partial. See [docs/dash.md](docs/dash.md) |
| ZEC | Zcash | UTXO | partial | **Maya-only** (`--backend maya`/`auto`); transparent (`t1…`) addresses only. Every feature is wired: hold, balance, destination, send/sweep, swap-**from** and single-sided LP (Maya, pairs with CACAO) — the spend paths ride a bespoke v4/ZIP-243 signer with ZIP-317 fees (bitcoinlib can't sign Zcash), anchored to a real mainnet tx in the tests but shipping **mainnet-unproven** (no testnet; opt-in test in docs/testnet.md), hence partial. See [docs/zcash.md](docs/zcash.md) |
| RUNE | THORChain native | THORChain | partial | Hold + balance + destination + `send` (`MsgSend`) + swap-**from** (`MsgDeposit`) done — reuses the shared Cosmos-SDK adapter (RUNE is 1e8). Spend paths ship unproven on mainnet (no testnet); see [docs/cacao.md](docs/cacao.md) |
| CACAO | Maya native | Maya | partial | **Maya-only**; 1e10 decimals (not 1e8). Hold + balance + destination + `send` (`MsgSend`) + swap-**from** (`MsgDeposit`, no vault) done; single-sided liquidity n/a for the settlement asset (it's the RUNE-leg of symmetric LP, TODO #4). Spend paths ship unproven on mainnet (no Maya testnet); see [docs/cacao.md](docs/cacao.md) |
| ATOM | Cosmos Hub | Cosmos | none | |
| XRP | XRP Ledger | XRP | none | |
| SOL | Solana | Solana | none | |
| ADA | Cardano | Cardano | none | Maya-only |
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
swapsack balance                             # balances across chains
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
```

Defaults are `--from BTC --to ETH`. `--confirm` prints the freshly-quoted swap
and asks before broadcasting (`--yes` skips the prompt for automation).

Swaps default to a **dry run** (build + verify + print); `--confirm`
is required to broadcast, and `--yes` skips the interactive
confirmation prompt. Destination addresses auto-derive from the seed;
pass `--dest` to override.


Config via flags or env: keystore `$SWAPSACK_KEYSTORE`
(`~/.config/swapsack/keystore.json`), passphrase
`$SWAPSACK_PASSPHRASE`, Esplora `$SWAPSACK_ESPLORA`, Ethereum
RPC `$SWAPSACK_ETH_RPC`, TRON API `$SWAPSACK_TRON_API`, BSC RPC
`$SWAPSACK_BSC_RPC`, Dash Insight API `$SWAPSACK_DASH_API`, Zcash
lightwalletd `$SWAPSACK_ZEC_LWD` (gRPC `host:port`).

**Shell tab-completion** (via argcomplete) — enable for the current shell, e.g. bash:

```sh
eval "$(register-python-argcomplete swapsack)"   # add to ~/.bashrc to persist
```

zsh and fish work too; see the [argcomplete docs](https://github.com/kislyuk/argcomplete#activating-global-completion).

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
- **Chainflip** — the recommended next: a second *independent* non-custodial
  cross-chain venue (keyless quotes probed) that adds **SOL and DOT** and
  price-competes on BTC/ETH. Deposits are plain sends to per-swap deposit
  addresses, so the existing send builders and gates get reused.
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
gmake test          # unit tests (live network tests excluded)
make test-network  # opt-in: read-only integration tests vs live THORChain
make lint          # ruff check + format check
```

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
