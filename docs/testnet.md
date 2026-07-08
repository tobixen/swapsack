# Testnet accounts for the integration broadcast tests

The opt-in `network` broadcast tests in `tests/test_integration_testnet.py`
move real (valueless) testnet coins, so they need funded accounts. The seeds
live **only** in CI secrets (GitHub Actions secrets are write-only — they can't
be read back), so if a seed is lost it is not recoverable from GitHub. To make
re-funding possible without the seed, the **public addresses are documented
here** (addresses are safe to publish; seeds are not and must never be committed).

Both tests derive from a single BIP39 seed (one seed → all chains, as the wallet
does). The same mnemonic is set as both secrets below.

## Addresses to fund

| Chain | Network | Derivation | Address | Secret / env |
|---|---|---|---|---|
| BTC | **signet** | `m/84'/0'/0'/0/0` (P2WPKH) | `tb1qaxgpvty4myyaf7qwz43f9meq5qsuz2dfzhhrdr` | `SWAPSACK_BTC_TESTNET_MNEMONIC` |
| ETH | Sepolia | `m/44'/60'/0'/0/0` | `0xd3074A2Bf86F5Db92C2F096302359CeEFEBC7176` | `SWAPSACK_ETH_SEPOLIA_MNEMONIC` |

The BTC test defaults to **signet** (`blockstream.info/signet/api`). Signet and
testnet3 share the same `tb1…` address format, so the address above is the same
on either; set `SWAPSACK_BTC_TESTNET_NETWORK=testnet` (+ a matching
Esplora) to fall back to testnet3.

The BTC test *sweeps the wallet's UTXOs to itself* and the ETH test *self-sends*
a tiny amount, so both just need a small balance at the address above (enough to
cover fees). Each test **skips** (not fails) when its account is unfunded or too
small, so a dry faucet never turns CI red.

## Faucets

- **BTC signet:** https://signetfaucet.com/ · https://faucet.mutinynet.com/
  (signet coins are stable and the faucet is reliable, unlike testnet3). If you
  switch back to testnet3: https://coinfaucet.eu/en/btc-testnet/ ·
  https://bitcoinfaucet.uo1.net/ (chronically drained).
- **ETH Sepolia:** https://sepoliafaucet.com/ · https://www.alchemy.com/faucets/ethereum-sepolia
  · https://cloud.google.com/application/web3/faucet/ethereum/sepolia

## Setting / rotating the CI secret

```sh
# one seed, both secrets (GitHub Actions):
gh secret set SWAPSACK_BTC_TESTNET_MNEMONIC     # paste the 12 words
gh secret set SWAPSACK_ETH_SEPOLIA_MNEMONIC     # paste the same 12 words
```

Keep a copy of the words in a password manager too — the secret can't be read
back, and this doc deliberately does **not** contain the seed.

## Re-deriving the address from the seed

If you have the mnemonic and want to confirm the address:

```sh
SWAPSACK_BTC_TESTNET_MNEMONIC="word1 … word12" python -c "import os; \
from swapsack.chains.btc import BtcAdapter; \
print(BtcAdapter(network='testnet').derive_address(os.environ['SWAPSACK_BTC_TESTNET_MNEMONIC'], \"m/84'/0'/0'/0/0\"))"

SWAPSACK_ETH_SEPOLIA_MNEMONIC="word1 … word12" python -c "import os; \
from swapsack.chains.eth import EthAdapter; \
print(EthAdapter().derive_address(os.environ['SWAPSACK_ETH_SEPOLIA_MNEMONIC']))"
```

## Network choice

The BTC test defaults to **signet** because testnet3 is being deprecated and its
faucets are chronically drained; signet is stable with a reliable faucet. The
network is env-driven (`SWAPSACK_BTC_TESTNET_NETWORK`, default
`signet`), and the Esplora default follows it
(`https://blockstream.info/<network>/api`), so testnet3/testnet4 remain a
one-env-var switch. bitcoinlib supports `signet`, `testnet` (testnet3) and
`testnet4`. The derived `tb1…` address is identical across them.
