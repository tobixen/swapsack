# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is derived
automatically from git tags (PEP 440 / SemVer).

## [Unreleased]

### Added

- **`balance` is one aligned sheet, valued and totalled.** Every row lines up,
  each carries what it is worth, and the bottom adds it up — in EUR by default,
  or `--unit BTC/USD/USDT/USDC/ETH/SATS`. Rows print together at the end
  instead of trickling out per chain (which is what made aligning them
  impossible); progress moves to stderr.
  - **Liquidity is totalled separately from spendable funds** — an LP position
    is not liquid and its redeemable figure is gross of exit fees, so it is
    never folded into a single number that reads like cash. A `~` marks an
    amount that includes the RUNE/CACAO side repriced at the pool rate.
  - **A row that cannot be priced is named, not silently counted as zero.**
  - **A chain that fails to answer is named too**, and the total says
    `INCOMPLETE`. Its row stays on the sheet with `?` for an amount: a chain
    that could not be reached is not a chain holding nothing, and a warning on
    stderr is too easy to lose to a redirect.
  - **Rows worth nothing collapse** into one `zero:` line naming them, so a
    chain never just disappears; `--zeros` gives each its own row again.
  - `--no-price-check` (already on the other commands) makes **no** price
    request at all: one lookup would tell a third party every asset this wallet
    holds. A feed that fails costs the value column and nothing else.
  - Liquidity rows now name their pool without its 42-character contract
    suffix (`+LP maya ETH.USDC`), and sit directly under the balance row of the
    token they belong to.

- **Arbitrum is now a chain you can spend from, not just pay to.** `ETH-ARB`
  (native ETH on Arbitrum) and `USDC-ARB` gain hold, balance, `send`/sweep,
  `swap --from`, and liquidity — single- and two-sided. Your Arbitrum address
  *is* your Ethereum address, so `--dest ARB` is now derived automatically
  instead of having to be typed, and `address` lists it.
  - `balance` calls the Arbitrum row **`ETH-ARB`** — the name `--asset` takes,
    so it can't be confused with the Ethereum row above it (same coin, same
    address). Not `ARB`, which is the ARB *token* and not tradeable.
  - **Moving USDC on Arbitrum costs cents.** That was always the point of the
    `USDC-ARB` destination; until now you could only receive it and had to use
    another wallet to do anything with it.
  - **Maya only.** THORChain has no Arbitrum pools, so `add-liquidity` refuses
    `--backend thorchain` for ARB up front rather than failing later.
  - **`USDC-ARB` is Circle's native USDC** (`0xaf88d065…`), not the bridged
    `USDC.e`, which is a different token at a different address.
  - **Mind the depth.** Maya's `ARB.USDC` pool held ~8.9k USDC on 2026-08-16 —
    small enough that a position of any size is a large share of it. The
    `Market:` line shows the swap side of this; for liquidity there is no
    equivalent warning, so check the pool yourself.
  - Like every new chain here, ARB ships **mainnet-unproven**: nothing has been
    broadcast on it yet.

- **Two-sided (symmetric) liquidity: `add-liquidity --symmetric`.** Adds both
  sides of a pool at once — the asset leg to the inbound vault, and a matching
  RUNE/CACAO leg as a native `MsgDeposit` — so you enter at the current pool
  ratio and pay **no entry slip**. You give `--amount` for the asset side; the
  RUNE/CACAO amount comes from the live pool ratio and must already be in your
  wallet (`swap --to CACAO --dest maya1…` is how you get it). THORChain has LP
  deposits paused, so this means Maya + CACAO today.

  What it does *not* buy you: a single-sided add already ends up ~50% exposed
  to RUNE/CACAO once the pool rebalances, so symmetric does not reduce that —
  on a stablecoin pool, half the position is a small-cap protocol token either
  way.

  The money-sensitive parts, all of which the CLI now handles for you:
  - **Both legs are built and verified before either is broadcast**, and
    nothing is sent if either gate objects — the output labels which leg
    complained.
  - **The protocol leg goes first** (native, cheap, fast), so a failure there
    leaves nothing live and the expensive leg is simply never sent. If the
    asset leg then fails, the add is genuinely half-complete and the CLI says
    so loudly with the live txid, rather than reporting a plain failure.
  - **A pool with LP deposits paused is refused up front**, before either leg
    is built — an add against one is refunded minus gas, and here that would be
    gas on two chains.
  - **You are told up front if you do not hold enough RUNE/CACAO** for the
    matching leg, instead of finding out when it bounces.
  - **EVM assets only (Ethereum and Arbitrum), and no `--amount max`.** The
    protocol pairs the legs by the asset leg's observed sender, which only an
    account-model chain has unambiguously; a UTXO source is refused rather than
    guessed at.
  - **Exercised on mainnet**: a real `ETH.USDC` position,
    which entered with no measurable slip (±2.7 bps a side). On **RUNE** the
    protocol leg remains unproven — no THORChain native transaction has ever
    been broadcast.

- **USDC as a swap destination on Avalanche and Arbitrum: `USDC-AVAX` and
  `USDC-ARB`**, payable with `--dest` like the other destination-only assets
  (AVAX via THORChain, ARB via Maya). Same dollar, on a chain where moving it
  afterwards costs cents rather than dollars — the swap payout itself is priced
  much the same on all three (the flat outbound fee was ~0.25 USDC on ETH and
  AVAX, ~0.12 on ARB when checked on 2026-08-16), so the saving is in what you
  do with the coins next, not in the swap.
  - **Mind Arbitrum's depth.** Maya's `ARB.USDC` pool held ~8.9k USDC, so a
    0.01 BTC swap lost ~12.7% against spot where the same swap to `USDC-ETH`
    lost ~0.35%. The `Market:` line shows this before you confirm — read it.
  - **A payout to a non-mainnet EVM chain now warns which chain it lands on.**
    Every EVM chain shares one address format, so a `0x…` address cannot tell
    you whether it is for Ethereum, Arbitrum or Avalanche. A self-custodial
    address is usually fine on all of them, but an exchange deposit address is
    not, and funds arriving over the wrong chain are a support ticket at best.
    The warning covers the existing `ETH-ARB` destination too.
  - Avalanche addresses are checked as **C-Chain** (`0x…`, EIP-55) only: an
    X-/P-Chain `X-avax1…` address is a valid Avalanche address that a swap
    payout could never credit, so it is refused rather than paid.
  - **Base is not included** — THORChain's `BASE.USDC` pool is trading-halted
    (as is BSC's), the same block that keeps SOL and BSC out.

- **Four new swap destinations: `ATOM`, `XRP`, `ADA` and `ETH-ARB`** (Cosmos
  Hub, XRP Ledger, Cardano, and native ETH on Arbitrum), payable with `--dest`
  like LTC/DOGE/BCH. ATOM and XRP go via THORChain, ADA and ETH-ARB via Maya;
  all four show a market line. Two caveats the CLI now tells you about rather
  than letting you discover them:
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

- **Recipient and `--dest` addresses are now checksum-verified**, not just
  shape-checked. Every chain's address carries a checksum precisely so that a
  single mistyped or dropped character is catchable, and that is the mistake
  worth catching: it is the one that still *looks* like a valid address.
  `send` and `swap --dest` now verify base58check (BTC/LTC/DOGE/DASH/ZEC/TRON),
  bech32 and bech32m (segwit incl. taproot, plus `thor1`/`maya1`), cashaddr
  (BCH) and EIP-55 (ETH), before any keystore or network work happens. Where an
  address carries no checksum to verify — an all-lowercase EVM address, a chain
  with no rule yet — it is still accepted, so nothing valid is newly rejected.
  This also fixes a **one-character typo in a ZEC `t1…` recipient printing a
  `ValueError: Invalid checksum` traceback** from deep inside the signer instead
  of a plain "that is not a valid address".

- **Configurable UTXO fee target, with a faster default.** BTC/DASH spends
  previously always targeted 6-block confirmation, which could leave a spend
  stuck for ~30+ minutes and surprise an impatient user. The default is now
  next-block-ish (2 blocks), and it's configurable: `--fee-blocks N` per run,
  `$SWAPSACK_FEE_BLOCKS`, or a new config file at
  `~/.config/swapsack/config.toml` (`[fees] target_blocks`), in that precedence.
  Lower N is faster and pricier. Adds the first support for a config file
  (`$SWAPSACK_CONFIG` relocates it).
- **Spends now signal opt-in RBF (BIP125).** Every BTC/DASH transaction sets
  `nSequence 0xfffffffd`, so a **BTC** spend that gets stuck in the mempool can
  be fee-replaced rather than only waited out. Nothing bumps yet — the `bump`
  command is still to come (see `docs/TODO.md`) — but signalling has to be in
  place *before* a tx is broadcast for a replacement to be accepted, so it ships
  now. No effect on how a tx is mined today. DASH transactions get the same
  signal from the shared builder, but Dash Core implements no mempool
  replacement (a deliberate choice, for InstantSend), so there it is inert.
- **`status <txid>` now shows what a BTC transaction did on-chain** — inputs,
  each output with its address, the change coming back to you, and the fee in
  both sats and EUR — queried straight from Esplora (no keystore needed). A
  plain `send` is never observed by a THORChain/Maya vault, so previously
  `status` returned only an empty `inbound_observed: false` stage that read like
  a failure; it now explains that (no OP_RETURN memo = not a swap) instead of
  leaving you guessing.
- **Transaction fees are also shown in approximate EUR**, so "20000 sats" or
  "0.000420 ETH" reads as a cost you can judge before confirming: `btc fee:
  20000 @ 2.0/vB (~€11.43)`. Amounts under a cent show `<€0.01`. The rate comes
  from the same keyless CoinGecko feed as the existing market line and is purely
  advisory — if it is unreachable the estimate is simply omitted, never delaying
  or blocking a spend. Because this puts a third-party lookup on paths that
  previously made none, `--no-price-check` is now accepted by `send`,
  `add-liquidity`, `withdraw-liquidity` and `status` as well as `quote`/`swap`,
  and it suppresses the request itself rather than just the printed estimate.
- **BIP21 payment URIs** are accepted wherever an address is: `swapsack send
  'bitcoin:bc1q…?amount=0.01&label=Alice'` and `--dest ethereum:0x…` now work,
  so an address copied from a wallet or scanned from a QR code can be pasted
  as-is instead of being hand-trimmed. The scheme must match the chain being
  spent (a `litecoin:` URI in a BTC send is refused, catching a cross-chain
  paste), and on `send` a URI `amount=` that contradicts `--amount` aborts rather
  than silently preferring one of them. For `--dest` only the address is used —
  a swap's URI cannot state the output amount meaningfully.
- **CoW Protocol backend (same-chain ETH-token swaps):** `--backend cow`
  (and `auto`) for `quote`/`swap` between USDT-ETH/USDC-ETH/ETH — a keyless
  intent API (`api.cow.fi`) that settles a solver auction instead of routing
  through two THORChain/Maya pool legs, cutting cost sharply for same-chain
  pairs (see `docs/backends.md`). Execution signs a structured EIP-712 order
  (no vault, no memo) rather than paying calldata to a router, so it stays
  gateable exactly like a `SendPlan` — every order field (tokens, amounts,
  receiver, validity, fill-or-kill, balance mode) is bound and checked before
  signing (`verify_cow_order`). Funds the CoW vault relayer's ERC-20 allowance
  first when short (handling USDT's reset-to-zero quirk) and waits for the
  approval to mine before submitting the order, and widens the
  `Backend` protocol (`serves()`/`try_quote()`/`executor`) so THORChain, Maya
  and CoW all price-compare under `--backend auto`. `status <order-uid>`
  tracks a submitted order. Live-signature-tested: a throwaway, unfunded key's
  signed order clears every orderbook check up to the balance check.
- **ZEC support (hold, balance, send, sweep, swap-from, liquidity):**
  `address`/`balance` derive the Zcash transparent receive address (standard
  BIP44, `m/44'/133'/0'/0/0`) and gap-limit scan via a configurable
  lightwalletd gRPC endpoint (`--zec-lwd` / `$SWAPSACK_ZEC_LWD`, default
  `zec.rocks:443`). `send`/`swap --from ZEC` (and `--amount max`) spend
  transparent funds through a **bespoke v4/ZIP-243 signer**
  (`chains/zcash_tx.py`) — bitcoinlib cannot sign Zcash's post-Overwinter
  transaction format. The ZIP-243 sighash implementation is anchored to a
  real mainnet transaction (its embedded signature verifies against our
  digest), the consensus branch id is fetched live from lightwalletd (never
  hardcoded — it would go stale at the next network upgrade), fees follow
  ZIP-317 (action-based, counting OP_RETURN memo bytes as logical actions),
  and transactions carry an expiry height (tip + 40) so unmined spends
  release instead of lingering. `swap --from ZEC` is Maya-routed (vault +
  OP_RETURN memo, streaming supported); single-sided
  `add-liquidity`/`withdraw-liquidity --asset ZEC --backend maya` pairs with
  CACAO (a THORChain LP request is refused up front). Ships **unproven on
  mainnet** (no Zcash testnet path) — an opt-in mainnet self-sweep test is
  gated on `SWAPSACK_ZEC_MNEMONIC`; test with a tiny amount first. Adds
  `grpcio`, `base58` and `coincurve` as direct dependencies (the latter two
  were already transitive).
- **DASH support (hold, balance, send, sweep, swap-from, liquidity):**
  `address`/`balance` derive the Dash receive address (standard BIP44,
  `m/44'/5'/0'/0/0`) and gap-limit scan via a configurable Insight API
  (`--dash-api` / `$SWAPSACK_DASH_API`, default `insight.dash.org`).
  `send`/`swap --from DASH` (and `--amount max`) build, gate and sign legacy
  P2PKH transactions through the same build/verify/sign path as BTC,
  broadcasting via the configured Insight API; the fee/dust maths is
  parameterized by script type (legacy 148/34-vB sizing, 546-duff dust) and
  the fee rate is a conservative flat 2 duffs/vB. `swap --from DASH` is
  Maya-routed (vault + OP_RETURN memo, streaming supported); single-sided
  `add-liquidity`/`withdraw-liquidity --asset DASH --backend maya` pairs with
  CACAO (a THORChain LP request is refused up front). Ships **unproven on
  mainnet** (Dash has no testnet path) — an opt-in mainnet self-sweep test is
  gated on `SWAPSACK_DASH_MNEMONIC`; test with a tiny amount first.

- **Tab-completion for commands, subcommands and options now works out of the
  box** — no `eval "$(register-python-argcomplete swapsack)"` line to add to
  `~/.bashrc` first. Installing swapsack now also installs a completion file
  that bash and zsh find on their own; start a new shell and press Tab. Doing
  it by hand still works, and is still needed for fish (see README).

### Fixed

- **`withdraw-liquidity` could not exit a two-sided position** — it sent the
  trigger on the asset chain, where the protocol has no record of such a
  position, so the transaction was spent and nothing came back. It now detects
  which kind of position you hold and, for a two-sided one, triggers from the
  CACAO/RUNE side instead (a dust `MsgDeposit`); both sides are returned
  proportionally. Nothing new to pass — the command picks the right one. If it
  cannot reach the network to find out, it refuses rather than guessing.

- **`balance` hid a two-sided liquidity position completely.** A symmetric add
  is filed by the protocol under your **CACAO/RUNE** address, not the asset
  address — and the asset address answers "no position" rather than an error,
  so the position printed no line at all while single-sided ones printed fine.
  The wallet under-reported real funds (confirmed against a live `ETH.USDC`
  position on 2026-08-16). `balance` now probes each backend's own `maya1…` /
  `thor1…` address alongside the chain's addresses, and de-duplicates in case a
  position answers on both of its keys.

- **Working default THORChain endpoint (the old one is dead):** the previous
  default, `thornode.thorchain.network`, was a Nine Realms host, and Nine Realms
  wound down — it (and `thornode.ninerealms.com`) stopped resolving on
  2026-07-12, so `quote`/`swap --backend thorchain`/`auto` and RUNE
  balance/send no longer worked out of the box. The default is now Liquify's
  public gateway (reachable from a CLI; `thornode.thorswap.net` is behind a
  Cloudflare bot-challenge and unusable here). The dead hosts are **removed**,
  not just deprioritised — `ThorchainClient` still tries its node list in order
  and pins the first that answers, but a permanently-dead entry only cost a
  connection timeout on every call. Override with `$SWAPSACK_THORNODE` (a single
  URL) to use your own node.

## [0.1.0] - 2026-07-08

First release
