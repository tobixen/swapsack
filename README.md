# cryptoswap

A python/CLI multi-currency wallet that may do non-custodial cross-chain swaps via [THORChain](https://thorchain.org/).

⚠️ This project is vibed-up, what could possibly go wrong?  **Don't use this wallet for more funds than what you can afford to lose**.  Bugs in the code may easily cause **irreversible loss of funds**.  Even if all the code is perfect, consider that this is a **hot wallet**, an attacker that gains a foothold on the computer running this wallet software may potentially manage to drain the funds in the wallet.

## Status

Working (dry-run by default; `--confirm` to broadcast):

- [x] THORChain client — `cryptoswap.thorchain`
- [x] Pre-broadcast verify gate (BTC + ETH) — `cryptoswap.verify`
- [x] Encrypted keystore (HD seed **and** raw keys) — `cryptoswap.keystore`
- [x] Chain adapters: BTC (bitcoinlib), ETH (eth-account), TRON (addr + balance) — `cryptoswap.chains`
- [x] Swap orchestrator + gap-limit BTC scanning — `cryptoswap.swap`, `cryptoswap.chains.scan`
- [x] Registry-based multi-chain `balance`; `--amount max` sweep (BTC and ETH)
- [x] CLI: `init`, `add-hd`, `add-raw`, `list`, `show-seed`, `address`, `balance`, `quote`, `swap`, `status`, `add-liquidity`, `withdraw-liquidity`

**Swap routes** (source → destination)

| from ↓ \ to → | BTC | ETH | TRX | USDT-TRON | USDT-ETH |
|---|:--:|:--:|:--:|:--:|:--:|
| **BTC** | — | ✅ | ✅ | ✅ | ✅ |
| **ETH** | ✅ | — | ✅ | ✅ | ✅ |
| **USDT-ETH** | ✅ | ✅ | ✅ | ✅ | — |

Sources: **BTC, ETH, and USDT-ETH** (ERC-20 via `approve` + router deposit).
**TRX and USDT-TRON sources are not done yet** — they need native TRON signing
plus a TronGrid API key (`$CRYPTOSWAP_TRON_API`); TRON is currently
destination-only. Destination addresses auto-derive from the seed; pass
`--dest` to override. BTC scanning is BIP84 (Trust Wallet's scheme); compiled
BDK has no Python 3.14 wheel, so BTC uses `bitcoinlib`.

See `docs/TODO.md` for remaining work. Phase 2 (later): semi-automatic "convert
everything above dust since last run".

## Roadmap (asset coverage)

Reach is bounded by THORChain's pools — current chains: BTC, ETH, BSC, AVAX,
BASE, BCH, LTC, DOGE, GAIA (ATOM), SOL, TRON, XRP, THOR (RUNE). **Monero (XMR)
is nearing mainnet** (no live pool yet — verify before relying). Everything
discussed is therefore in scope; nothing currently needs a separate backend.

Status: ✅ done · ◑ destination only · ☐ planned. Listed in recommended order.

| # | Asset | What it is | Family | Status | Notes |
|--:|---|---|---|:--:|---|
| 1 | **BTC** | Bitcoin | UTXO | ✅ | source + destination |
| 2 | **ETH** | Ethereum | EVM | ✅ | source + destination |
| 3 | **USDT-ETH** | Tether (ERC-20) | EVM token | ✅ | source (approve+router) + destination |
| 4 | **TRX** | TRON native | TRON | ◑ | source needs tronpy + a TRON endpoint |
| 5 | **USDT-TRON** | Tether (TRC-20) | TRON token | ◑ | source = TRC-20 transfer+memo (no router) |
| 6 | **BSC / BNB** | BNB Smart Chain | EVM | ☐ | same address/signing as ETH; chainId/RPC/router differ |
| 7 | **AVAX** | Avalanche C-Chain | EVM | ☐ | EVM family config entry |
| 8 | **BASE** | Base (ETH L2) | EVM | ☐ | EVM family config entry |
| 9 | **USDC/USDT on BSC/AVAX/BASE** | stablecoins | EVM token | ☐ | come with the EVM family |
| 10 | **LTC** | Litecoin | UTXO | ☐ | generalize BTC adapter; needs a Litecoin Esplora |
| 11 | **DOGE** | Dogecoin | UTXO | ☐ | UTXO family |
| 12 | **BCH** | Bitcoin Cash | UTXO | ☐ | UTXO family |
| 13 | **RUNE** | THORChain native | Cosmos/THOR | ☐ | dest = `thor1…`; source = `MsgDeposit`; gateway to LP |
| 14 | **(LP)** | liquidity provision | — | ◑ | `add-liquidity`/`withdraw-liquidity` done for BTC & ETH (single-sided, experimental); tokens/TRON pending |
| 15 | **ATOM** | Cosmos Hub (GAIA) | Cosmos | ☐ | new adapter (cosmpy) |
| 16 | **XRP** | XRP Ledger | XRP | ☐ | new adapter (xrpl-py) |
| 17 | **SOL** | Solana | Solana | ☐ | new adapter (ed25519 / solders); THORChain-supported |
| 18 | **XMR (dest)** | Monero, receive-only | — | ☐ | easy: asset entry + external `--dest`, no Monero code. Caveat: ~95-char addr exceeds BTC OP_RETURN, so BTC→XMR needs a THORName; ETH→XMR fits. Pending live pool |
| 19 | **XMR (source)** | Monero, spend | Monero | ☐ | heaviest: full Monero signing stack (`tx_extra` memo) |
| 20 | **TCY** | THORChain reward token | THOR token | ☐ | niche; low priority |

Order rationale: EVM family (6–9) is the most coverage for least risk (reuses ETH
signing, no new deps); UTXO (10–12) next; TRON sources (4–5) are code-ready but
endpoint-blocked; RUNE/LP (13–14) open the native side; ATOM/XRP/SOL/XMR (15–18)
are new signing stacks, XMR heaviest and pool-pending.

## Usage

```sh
uv run cryptoswap init                                   # create encrypted keystore
uv run cryptoswap add-hd --label main                    # import seed (prompted), or:
uv run cryptoswap add-hd --label test --generate         # generate a fresh seed
uv run cryptoswap address                                # BTC / ETH / TRON addresses
uv run cryptoswap balance                                # balances across all chains
uv run cryptoswap quote --from ETH --to USDT-TRON --amount 0.02   # read-only
uv run cryptoswap swap  --from ETH --to BTC --amount max          # DRY RUN (sweep)
uv run cryptoswap swap  --from BTC --to USDT-TRON --amount 0.001 --confirm
```

Defaults are `--from BTC --to ETH`. `--confirm` prints the freshly-quoted swap
and asks before broadcasting (`--yes` skips the prompt for automation).

Config via flags or env: keystore `$CRYPTOSWAP_KEYSTORE`
(`~/.config/cryptoswap/keystore.json`), passphrase `$CRYPTOSWAP_PASSPHRASE`,
Esplora `$CRYPTOSWAP_ESPLORA`, Ethereum RPC `$CRYPTOSWAP_ETH_RPC`, TRON API
`$CRYPTOSWAP_TRON_API`.

## Development

```sh
uv run pytest            # unit tests (auto-syncs; live network tests excluded)
uv run pytest -m network # opt-in: read-only integration tests vs live THORChain
uv run ruff check .
uv run ruff format .
```

The `network` tests are read-only (no funds moved); they guard against THORChain
API drift and stale hard-coded asset strings. Full *broadcast* integration would
use THORChain **stagenet** (the old testnet is deprecated) but needs stagenet
faucet coins + testnet chain params — left as a manual exercise, not CI.

## Refreshing test fixtures

The fixtures in `tests/` are trimmed real responses from the THORChain REST API:

```sh
curl -s "https://thornode.thorchain.liquify.com/thorchain/quote/swap?from_asset=BTC.BTC&to_asset=ETH.ETH&amount=178100"
curl -s "https://thornode.thorchain.liquify.com/thorchain/inbound_addresses"
```
