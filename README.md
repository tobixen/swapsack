# cryptoswap

A python/CLI multi-currency wallet that may do non-custodial cross-chain swaps via [THORChain](https://thorchain.org/).

⚠️ This project is vibed-up ... what could possibly go wrong?

**Don't use this wallet for more funds than what you can afford to lose**.  Bugs in the code may easily cause **irreversible loss of funds**.  Even if all the code is perfect, consider that this is a **hot wallet**, an attacker that gains a foothold on the computer running this wallet software may potentially manage to drain the funds in the wallet.

The rest of this document is partially AI-generated.

## Installation

```
make install
```

This auto-detects `uv`, `pipx`, or `pip` and installs the `cryptoswap-wallet`
binary on your PATH. Then run `cryptoswap-wallet --help`.

## Features

Swaps default to a **dry run** (build + verify + print); `--confirm`
is required to broadcast, and `--yes` skips the interactive
confirmation prompt. Destination addresses auto-derive from the seed;
pass `--dest` to override.

The wallet is still under rapid development as of 2026-06-29.  Missing features and currency support will be prioritized by personal need and by issues/PRs received.  Here is the "current status" of (partially) supported currencies (✅ = working, ◑ = partial, blank = not yet):

| Currency  | Hold | Bal | To  | From | Send | Liq |
|-----------|:----:|:---:|:---:|:----:|:----:|:---:|
| BTC       |  ✅  |  ✅ |  ✅ |  ✅  |  ✅  |  ✅ |
| ETH       |  ✅  |  ✅ |  ✅ |  ✅  |      |  ✅ |
| USDT-ETH  |  ✅  |     |  ✅ |  ✅  |      |     |
| TRX       |  ✅  |  ✅ |  ✅ |  ✅  |      |  ✅ |
| USDT-TRON |  ✅  |     |  ✅ |      |      |     |
| LTC       |      |     |  ✅ |      |      |     |
| DOGE      |      |     |  ✅ |      |      |     |
| BCH       |      |     |  ✅ |      |      |     |

### Features explained

* **Hold** — derive an `address`, hold a balance, receive funds
* **Bal**  — show the `balance` (native + any THORChain/Maya liquidity positions; token balances not shown yet)
* **To**   — use as a `swap` *destination* (for a currency whose address the wallet can't derive yet, give an external one via `--dest`)
* **From** — use as a `swap` *source* (the asset you spend)
* **Send** — `send` to an external address (a plain transfer, no swap)
* **Liq**  — `add-liquidity` and `withdraw-liquidity` can be used to provide/withdraw *single-sided* liquidity (experimental; see below).

Other features:

* `quote` — read-only price preview for any supported asset
* `status` — track a swap by its inbound txid
* `address` — print the derived BTC / ETH / TRON addresses
* `--amount max` — sweep the whole balance minus fees (BTC, ETH source)
* `--backend auto` — compares **THORChain + Maya** and routes to the best price (`quote`, `swap`) for currencies supported by both backends.  (Other backends may be considered in the future)

**Liquidity (experimental).** `add-liquidity` / `withdraw-liquidity` add or
remove *single-sided* liquidity on a THORChain pool.  By adding liquidity one will earn a share of that pool's swap fees, but it's not without risks.  As of 2026-06-28 THORChain rejects new liquidity for all assets, probably due to a switch to protocol-owned liquidity (POL).  It's still possible to use `add-liquidity --backend maya`.  For bigger amounts, *double-sided* liquidity should be used rather than single-sided liquidity, but this is not supported yet.

## Currency roadmap

It's on the roadmap to support the union of the currency sets
supported by the available swapping backends. **Support**:
full = every feature working, partial = some features working, none =
planned. Listed in recommended implementation order; see the
capability grid above for the per-feature detail.

| Currency | What it is | Family | Support | Notes |
|---|---|---|:--:|---|
| BTC | Bitcoin | UTXO | full | |
| ETH | Ethereum | EVM | partial | no `send` yet |
| USDT-ETH | Tether | ERC-20 token | partial | no `send`/`balance`/liquidity yet |
| TRX | TRON | TRON | partial | no `send` yet |
| USDT-TRON | Tether | TRC-20 token | partial | |
| BSC / BNB | BNB Smart Chain | EVM | none | |
| AVAX | Avalanche C-Chain | EVM | none | |
| BASE | Base (ETH L2) | EVM | none | |
| ARB | Arbitrum (ETH L2) | EVM | none | Maya-only |
| USDC | USD Coin (ETH/BSC/AVAX/BASE/ARB) | ERC-20 token | none | |
| LTC | Litecoin | UTXO | partial | destination only (via `--dest`) |
| DOGE | Dogecoin | UTXO | partial | destination only (via `--dest`) |
| BCH | Bitcoin Cash | UTXO | partial | destination only (via `--dest`) |
| DASH | Dash | UTXO | none | Maya-only |
| ZEC | Zcash | UTXO | none | Maya-only |
| RUNE | THORChain native | THORChain | none | |
| CACAO | Maya native | Maya | none | Maya-only |
| ATOM | Cosmos Hub | Cosmos | none | |
| XRP | XRP Ledger | XRP | none | |
| SOL | Solana | Solana | none | |
| ADA | Cardano | Cardano | none | Maya-only |
| XMR | Monero | Monero | none | no live THORChain pool yet |
| TCY | THORChain reward token | THORChain token | none | niche; low priority |
| MAYA | Maya governance token | Maya token | none | Maya-only; niche; low priority |

## Usage

```sh
cryptoswap-wallet init                                # create encrypted keystore
cryptoswap-wallet add-hd --label main                 # import seed (prompted), or:
cryptoswap-wallet add-hd --label test --generate      # generate a fresh seed
cryptoswap-wallet address                             # BTC / ETH / TRON addresses
cryptoswap-wallet balance                             # balances across chains
cryptoswap-wallet quote --from ETH --to USDT-TRON --amount 0.02
cryptoswap-wallet swap  --from ETH --to BTC --amount max          # DRY RUN (sweep)
cryptoswap-wallet swap  --from BTC --to USDT-TRON --amount 0.001 --confirm
cryptoswap-wallet send  bc1q...recipient --amount 0.001                 # DRY RUN
cryptoswap-wallet send  bc1q...recipient --amount max --confirm         # sweep + send
```

Defaults are `--from BTC --to ETH`. `--confirm` prints the freshly-quoted swap
and asks before broadcasting (`--yes` skips the prompt for automation).

Config via flags or env: keystore `$CRYPTOSWAP_WALLET_KEYSTORE`
(`~/.config/cryptoswap-wallet/keystore.json`), passphrase
`$CRYPTOSWAP_WALLET_PASSPHRASE`, Esplora `$CRYPTOSWAP_WALLET_ESPLORA`, Ethereum
RPC `$CRYPTOSWAP_WALLET_ETH_RPC`, TRON API `$CRYPTOSWAP_WALLET_TRON_API`.

## Development

```sh
make dev           # set up the environment (uv)
make test          # unit tests (live network tests excluded)
make test-network  # opt-in: read-only integration tests vs live THORChain
make lint          # ruff check + format check
```

The `network` tests are read-only (no funds moved); they guard against THORChain
API drift and stale hard-coded asset strings.

## Releasing

Versioning is automatic from git tags (hatch-vcs). Pushing a `v*` tag triggers
`.github/workflows/publish.yml`, which runs lint + the **full** test suite
*including* the live integration tests (`pytest -m network`) and only then builds
and publishes to PyPI via trusted publishing — so a THORChain outage blocks a
release. (Configure trusted publishing once at pypi.org.) `pre-commit` runs ruff
on commit and the unit tests on push.

## Refreshing test fixtures

The fixtures in `tests/` are trimmed real responses from the THORChain REST API:

```sh
curl -s "https://thornode.thorchain.liquify.com/thorchain/quote/swap?from_asset=BTC.BTC&to_asset=ETH.ETH&amount=178100"
curl -s "https://thornode.thorchain.liquify.com/thorchain/inbound_addresses"
```
