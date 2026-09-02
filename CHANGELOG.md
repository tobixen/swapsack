# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is derived
automatically from git tags (PEP 440 / SemVer).

## [Unreleased]

### Added

- **`history` and `utxos`.** `swapsack history` lists every transaction that
  touched the wallet — newest first, mempool first — with what it did to your
  balance, the full txid, who else was paid, and the memo that marks a swap
  deposit rather than a plain send. `swapsack utxos` lists the coins behind
  those numbers: every output that ever paid you, the spent ones included, each
  saying either `unspent` or which transaction spent it. `--json` on both, and
  `--unspent` / `--spent` to narrow the second.

  BTC and DASH get the full listing; `utxos --asset ZEC` shows what you still
  hold but cannot show the spent outputs, and says so. The other chains are not
  offered: nothing this wallet talks to can list an ETH or TRON address's
  transactions.

  Both listings cover the addresses the gap-limit scan finds, exactly as
  `balance` does — coins further out than that are missing from them, and
  nothing can warn you about it.

- **`status <txid>` now tracks Chainflip swaps.** A Chainflip vault swap leaves
  no channel or order id behind — only the deposit transaction you broadcast —
  and it is invisible to THORChain and Maya, so `status` used to end on "not
  observed", which reads as *your money went nowhere*. It now asks Chainflip
  first and reports what the protocol says: the state, what went in, what came
  out, and the payout transaction. Amounts in assets this wallet has no key for
  are shown in base units rather than scaled by a guess.

  Chainflip only witnesses a deposit **after it confirms**, though — which is
  not the moment you ask, having just broadcast one. In that window `status`
  now reads the swap out of the deposit's own `OP_RETURN`: which published
  protocol vault was paid and how much, the destination the payload names, and
  the floor it enforces on-chain. Nobody's service has to be up for that, so a
  Chainflip API that is merely slow costs you a payout figure, not the answer.
  A deposit whose payload is well-formed but which pays no address the protocol
  publishes as a vault gets said so loudly: that swap will never happen. And
  the summary printed before you broadcast now points at `swapsack status`
  rather than sending you off to a block explorer.

### Fixed

- **Chainflip vault swaps work again.** The broker account this wallet named
  when asking the Chainflip chain to encode a vault swap started enforcing a
  minimum commission of 5 bps, and since a commission is a skim the verify gate
  refuses, every BTC swap through Chainflip aborted with `DispatchError: Broker
  commission is too low`. The account is now a fallback list, tried in order
  until one encodes at zero commission. What the transaction says is unchanged
  — the swap parameters are byte-identical whichever broker answers — and it
  may pay a different one of the protocol's vaults, which is checked against
  the vault list the protocol publishes, as before.

## [0.2.0] - 2026-08-30

Note: this section is a hand-rewritten, slimmed-down version. Most people
probably aren't interested in a wall of text; if you want the detailed
AI-generated original, it is the `CHANGELOG.md` at commit
[8cb7b2bc](https://github.com/tobixen/swapsack/blob/8cb7b2bc/CHANGELOG.md).

### Changed

These "features" are indeed breaking changes:

- **Spends now signal opt-in RBF (BIP125).** Every BTC/DASH
  transaction sets `nSequence 0xfffffffd`, which is what lets `bump`
  (below) replace a stuck BTC spend rather than leaving you to wait it
  out.  This is a breaking change if you depend on merchants or other
  people to instantly accept a zero-confirmation transaction - **the
  RBF mechanism can be used to divert the transaction back to your own
  wallet** (this is not implemented in swapsack).  I'll get around to
  making it configurable in a later release.  (Be aware that the RBF
  signal is a courtesy flag - in reality an RBF can often be performed
  without the initial signalling, so don't trust unconfirmed bitcoin
  transactions).

- **More readable `balance` sheet** - with EUR-estimates and total
  value in EUR printed by default, opt-out by `--no-price-check`,
  other units available through `--unit BTC/USD/USDT/USDC/ETH/SATS`.
  `--zeros` to have one line per asset (default is to collapse all
  assets with 0 balance into one line).  This is a breaking change if
  you have scripts scraping the earlier output.

- **Configurable UTXO fee target, with a faster default.** BTC/DASH
  spends previously always targeted 6-block confirmation, which could
  leave a spend stuck for a longer time and surprise an impatient
  user. The default is now next-block-ish (2 blocks), and it's
  configurable: `--fee-blocks N` per run, `$SWAPSACK_FEE_BLOCKS`, or a
  new config file at `~/.config/swapsack/config.toml` (`[fees]
  target_blocks`), in that precedence.  Lower N is faster and
  pricier. Adds the first support for a config file
  (`$SWAPSACK_CONFIG` relocates it).  This is a breaking
  change if you expect the cheapest possible transactions.

### Added

- **`bump <txid>`: unstick a fee-starved BTC transaction (BIP125 replace-by-fee).**
  Fee target via `--fee-rate N` (sats/vB) or `--fee-blocks N`, floored by what
  BIP125 needs to relay at all.  BTC only.

- **`--allow-unconfirmed`: spend money that is still in the mempool.**
  The spend pays its parents' fee shortfall (CPFP).  Confirmed coins
  are still spent first.

- **Chainflip is added as the fourth swap backend**, but only works
  with BTC input for now (use `--backend chainflip` or `--backend
  auto`).  Chainflip is an independent protocol with its own
  validators and pools, so it will continue working when THORChain and
  Maya are both down due to a common bug. **`--amount max` is refused
  for a vault swap** - the Chainflip protocol mandates having a change
  address which it will use for refunds.

- **Support for Arbitrum** — `ETH-ARB` (native ETH on Arbitrum) and
  `USDC-ARB`, Maya-only. Ships **mainnet-unproven**: nothing has been
  broadcast on Arbitrum yet.

- **Two-sided (symmetric) liquidity**: `add-liquidity --symmetric`. Adds both
  sides of a pool at once — the asset leg to the inbound vault, and a matching
  RUNE/CACAO leg as a native `MsgDeposit` — so you enter at the current pool
  ratio and pay **no entry slip**. You give `--amount` for the asset side; the
  RUNE/CACAO amount comes from the live pool ratio and must already be in your
  wallet (`swap --to CACAO --dest maya1…` is how you get it). THORChain has LP
  deposits paused, so this means Maya + CACAO today.

- **USDC as a swap destination on Avalanche and Arbitrum: `USDC-AVAX` and
  `USDC-ARB`**, payable with `--dest` like the other destination-only assets
  (AVAX via THORChain, ARB via Maya). Same dollar, on a chain where moving it
  afterwards costs cents rather than dollars — the swap payout itself is priced
  much the same on all three (the flat outbound fee was ~0.25 USDC on ETH and
  AVAX, ~0.12 on ARB when checked on 2026-08-16), so the saving is in what you
  do with the coins next, not in the swap.

- **Four new swap destinations: `ATOM`, `XRP`, `ADA` and `ETH-ARB`** (Cosmos
  Hub, XRP Ledger, Cardano, and native ETH on Arbitrum), payable with `--dest`
  like LTC/DOGE/BCH. Two caveats:
  - **An XRP payout cannot carry a destination tag.** THORChain accepts only a
    classic `r…` address — X-addresses and `address:tag` are both rejected — so
    `--to XRP` warns you not to use an exchange deposit address that needs a
    tag, since such a deposit is usually unrecoverable.
  - **ADA is usually reachable only from an account-model source** such as
    ETH. The Cardano address a wallet normally hands out is 103 characters,
    which pushes the swap memo past the 80-byte `OP_RETURN` a BTC/DASH/ZEC
    spend has to carry it in. Asking for it from a UTXO source now explains
    that, instead of reporting "no quotes" as if a pool were merely missing.
    The check is per-address, so a shorter Cardano address (an enterprise one
    is 58 characters) still works from BTC.

- **Recipient and `--dest` addresses are now checksum-verified**

- **`status <txid>` now shows information on a BTC transaction**. Previously
  `status` would only return information on swaps, not regular transactions.

- **Transaction fees are also shown in approximate EUR**, so "20000
  sats" or "0.000420 ETH" reads as a cost you can judge before
  confirming: `btc fee: 20000 @ 2.0/vB (~€11.43)`. Amounts under a
  cent show `<€0.01`. The rate comes from the CoinGecko feed. If it is
  unreachable the estimate is simply omitted instead of delaying or
  blocking a spend. Use `--no-price-check` to disable.

- **BIP21 payment URIs** are accepted:
  `swapsack send 'bitcoin:bc1q…?amount=0.01&label=Alice'` and `--dest
  ethereum:0x…` now work. On `send` a URI `amount=` that contradicts
  `--amount` aborts rather than silently preferring one of them. For
  `--dest` only the address is used.

- **CoW Protocol backend (same-chain ETH-token swaps):** `--backend cow`
  (and `auto`) for `quote`/`swap` between USDT-ETH/USDC-ETH/ETH — a keyless
  intent API (`api.cow.fi`) that settles a solver auction instead of routing
  through two THORChain/Maya pool legs, cutting cost sharply for same-chain
  pairs (see `docs/backends.md`).

- **ZEC support** (hold, balance, send, sweep, swap-from, liquidity), through
  a bespoke v4/ZIP-243 signer — bitcoinlib cannot sign Zcash. Ships
  **unproven on mainnet** (there is no Zcash testnet); test with a tiny
  amount first.

- **DASH support** (hold, balance, send, sweep, swap-from, liquidity),
  Maya-routed. Ships **unproven on mainnet** (Dash has no testnet); test
  with a tiny amount first.

- **Tab-completion now works out of the box** — at least on my
  laptop. Installing swapsack now also installs a completion file that
  bash and zsh find on their own; start a new shell and press
  Tab. Doing it by hand still works, and is still needed for fish (see
  README).

### Fixed

- **A dropped reply from a public API no longer aborts a swap.** The
  default Bitcoin endpoint, `blockstream.info`, does not always work
  100% reliable.  An HD account scan makes lots of calls to check the
  balance.  We now have retries in place should a single call fail,
  and mempool.space is utilized as a fallback.  It's also possible to
  name a custom endpoint with `--esplora` / `$SWAPSACK_ESPLORA`.
  Should things still fail, it will be with a clear error message
  rather than a long incomprehensible traceback.

- **Two-sided positions** are now fully supported - and for balance and
  withdrawals it's considered a bug that it didn't work.

- **Working primary THORChain endpoint (the old one is dead):** the previous
  default, `thornode.thorchain.network`, was a Nine Realms host, and Nine Realms
  wound down, so `quote`/`swap --backend thorchain`/`auto` and RUNE
  balance/send stopped working out of the box. The default is now Liquify's
  public gateway. Override with `$SWAPSACK_THORNODE` (a single
  URL) to use your own node.

## [0.1.0] - 2026-07-08

First release
