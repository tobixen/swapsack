# cryptoswap

A python/CLI multi-currency wallet that may do non-custodial cross-chain swaps via [THORChain](https://thorchain.org/).

⚠️ This project is vibed-up ... what could possibly go wrong?

**Don't use this wallet for more funds than what you can afford to lose**.  Bugs in the code may easily cause **irreversible loss of funds**.  Even if all the code is perfect, consider that this is a **hot wallet**, an attacker that gains a foothold on the computer running this wallet software may potentially manage to drain the funds in the wallet.

The rest of this document is AI-generated.

## Installation

```
make install
```

This auto-detects `uv`, `pipx`, or `pip` and installs the `cryptoswap-wallet`
binary on your PATH. Then run `cryptoswap-wallet --help`.

## Features

Swaps default to a **dry run** (build + verify + print); `--confirm` is required
to broadcast. Destination addresses auto-derive from the seed; pass `--dest` to
override.

| Feature | Available for | Notes |
|---|---|---|
| `balance` | BTC, ETH, TRX | native balances (token balances not shown yet) |
| `address` | BTC, ETH, TRON | derived from the seed |
| `quote` | any supported asset | read-only price preview |
| `swap` (source) | BTC, ETH, USDT-ETH | the asset you spend |
| `swap` (destination) | BTC, ETH, TRX, USDT-ETH, USDT-TRON | where funds land |
| `send` (to external address) | — | **planned** (next) |
| `add-liquidity` / `withdraw-liquidity` | BTC, ETH | single-sided, **experimental** |
| `status` | all | track a swap by its inbound txid |
| `--amount max` | BTC, ETH (source) | sweep the whole balance minus fees |

## Currency support

Reach is bounded by THORChain's pools. Support: **full** = source + destination,
**partial** = destination/balance only, **none** = planned. Listed in
recommended implementation order.

| Currency | Support | Notes |
|---|:--:|---|
| BTC | full | UTXO; source + destination + LP |
| ETH | full | EVM; source + destination + LP |
| USDT-ETH | full | ERC-20; source (approve+router) + destination |
| TRX | partial | destination + balance; source needs tronpy + a TRON endpoint |
| USDT-TRON | partial | destination only; source = TRC-20 transfer + memo |
| BSC / BNB | none | EVM family (next): same address/signing as ETH |
| AVAX | none | EVM family |
| BASE | none | EVM family |
| USDC (ETH/BSC/AVAX/BASE) | none | EVM family stablecoins |
| LTC | none | UTXO family (generalize the BTC adapter) |
| DOGE | none | UTXO family |
| BCH | none | UTXO family |
| RUNE | none | THORChain-native; gateway to LP |
| ATOM | none | Cosmos (new adapter) |
| XRP | none | XRP Ledger (new adapter) |
| SOL | none | Solana (new adapter); THORChain-supported |
| XMR | none | Monero, nearing THORChain mainnet; receive-only is cheap once live |
| TCY | none | THORChain reward token; niche |

EVM is the recommended next family (most coverage, least risk); then UTXO; TRON
sources are code-ready but need a working endpoint. See `docs/TODO.md` for detail.

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
