# Asset coverage & implementation order

Every asset discussed, in the order recommended for implementation. Reach is
bounded by **THORChain's pools** — assets not on THORChain can't be swapped by
this tool at all (they'd need a separate swap backend, each its own project).

Status: ✅ done · ◑ partial (destination only) · ☐ planned · ✗ out of scope.

| # | Asset / chain | What it is | Family | THORChain | Status | Notes |
|--:|---|---|---|:--:|:--:|---|
| 1 | **BTC** | Bitcoin | UTXO | yes | ✅ | source + destination |
| 2 | **ETH** | Ethereum (native) | EVM | yes | ✅ | source + destination |
| 3 | **USDT-ETH** | Tether, ERC-20 | EVM token | yes | ✅ | source (approve+router) + destination |
| 4 | **TRX** | TRON native | TRON | yes | ◑ | destination done; source needs tronpy + a TRON endpoint |
| 5 | **USDT-TRON** | Tether, TRC-20 | TRON token | yes | ◑ | destination done; source = TRC-20 transfer+memo (no router) |
| 6 | **BSC / BNB** | BNB Smart Chain | EVM | yes | ☐ | same address/signing as ETH; only chainId/RPC/router differ |
| 7 | **AVAX** | Avalanche C-Chain | EVM | yes | ☐ | EVM family — config entry once adapter is generalized |
| 8 | **BASE** | Base (ETH L2) | EVM | yes | ☐ | EVM family — config entry |
| 9 | **USDC / USDT (BSC/AVAX/BASE)** | stablecoins | EVM token | yes | ☐ | come with the EVM family (source + destination) |
| 10 | **LTC** | Litecoin | UTXO | yes | ☐ | generalize BTC adapter; needs a Litecoin Esplora endpoint |
| 11 | **DOGE** | Dogecoin | UTXO | yes | ☐ | UTXO family |
| 12 | **BCH** | Bitcoin Cash | UTXO | yes | ☐ | UTXO family |
| 13 | **RUNE** | THORChain native | Cosmos/THOR | yes | ☐ | dest = derive `thor1…`; source = `MsgDeposit`; gateway to LP |
| 14 | **(LP)** | liquidity provision | — | yes | ☐ | `+:POOL` / `-:POOL:bps` memos; experimental, tuition-not-yield |
| 15 | **ATOM** | Cosmos Hub | Cosmos | yes | ☐ | new adapter (cosmpy) |
| 16 | **XRP** | XRP Ledger | XRP | yes | ☐ | new adapter (xrpl-py) |
| 17 | **TCY** | THORChain reward token | THOR token | yes | ☐ | niche; low priority |
| — | **SOL** | Solana | — | **no** | ✗ | not on THORChain; needs a separate Solana DEX backend |
| — | **XMR** | Monero | — | **no** | ✗ | not on THORChain; privacy chain; would need BTC↔XMR atomic swaps |

## Why this order

1. **EVM family (6–9)** is the biggest coverage for the least risk: all EVM
   chains share your address and eth-account signing — only chainId/RPC/router
   differ — and it reuses the just-built ETH native + ERC-20 token paths. No new
   dependency, no TronGrid blocker.
2. **UTXO family (10–12)** is the next cheapest: generalize the BTC/bitcoinlib
   adapter; the only friction is finding a reliable Esplora endpoint per coin.
3. **TRON sources (4–5)** are ready code-wise but blocked on a working TRON
   endpoint (keyless TronGrid 429s).
4. **RUNE + LP (13–14)** opens the THORChain-native side and the LP experiment.
5. **ATOM, XRP (15–16)** are genuine new signing stacks — most work per chain.
6. **TCY (17)** is niche.
7. **SOL, XMR** are out of scope for a THORChain-only wallet.
