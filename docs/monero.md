# Monero (XMR) support — design notes

Status: **not started; plan proposed 2026-10-22** (see "Plan" below, which
supersedes the open decisions D1/D2). Monero does not fit the wallet's
existing model; this note records the analysis and the plan.

THORChain's XMR pool is not live yet. v3.21.0 carries the integration but
"XMR activation remains gated and requires a follow-up v3.21.1 release", then
a Mimir switch for XMR keygen, a churn, and a soft launch with shallow
liquidity (https://blog.thorchain.org/thorchain-protocol-upgrade-v3-21).
Maya has no XMR pool. The three features the owner
asked for — **hold (address)**, **balance**, **send** — are wallet-local and
independent of swapping, so they can be built before the pool exists.

## Why XMR doesn't fit the current model

Every other chain follows one model: **keyless public node, seed stays
encrypted in the keystore, sign locally with a lightweight Python lib**
(bitcoinlib / eth-account / tronpy). Monero breaks all three legs.

1. **Hold / address** — *feasible in pure Python, with a caveat.* Monero
   doesn't use BIP44/secp256k1. It has its own ed25519 dual-key model (a
   secret **spend** key + a secret **view** key, derived as
   `view = keccak256(spend) mod l`), its own block-based base58, and a
   network-byte + dual-pubkey + checksum address layout. The catch: **there is
   no standard for deriving Monero keys from a BIP39 seed.** Feather/Polyseed,
   Cake, Ledger, and MyMonero all differ, so whatever scheme we pick, the
   derived address won't restore in an arbitrary external Monero wallet unless
   we match that wallet's exact convention. (Resolved by *Plan → D2
   dissolves*: the 25-word seed printed from the spend key is what restores.)

2. **Balance** — *not feasible the wallet's current way.* Monero is private:
   you cannot look up a balance by address on any explorer. Computing a balance
   means **scanning the whole chain with the view key**, testing every output
   for ownership, and detecting spends via key images (which needs the spend
   key). That requires either a running `monero-wallet-rpc` + a node, or handing
   the view key to a Light Wallet Server (a privacy leak — and still no send).

3. **Send** — *not feasible in pure Python at all.* Constructing a RingCT
   transaction (decoy selection, ring signatures, bulletproofs, key images) has
   no maintained pure-Python library. Realistically the only path is
   `monero-wallet-rpc`.

`docs/TODO.md` already flagged this instinct: XMR *"needs full nodes (heavy)
and a different custody seam. Future."*

## Open decisions (superseded by *Plan* below; kept for the reasoning)

### D1. Backend for balance + send

| Option | address | balance | send | cost |
|---|---|---|---|---|
| **`monero-wallet-rpc` seam** | pure python | wallet-rpc `get_balance` | wallet-rpc `transfer` | heavy: needs `monerod` (or a trusted remote node) + `monero-wallet-rpc`; keys handed to the wallet-rpc process |
| **Address-only now** | pure python | stub ("needs wallet-rpc") | stub | lightweight; defers the seam |
| **Light Wallet Server** | pure python | LWS API (view key sent to server) | still impossible | half a solution; privacy leak; not recommended |

Only the `monero-wallet-rpc` seam delivers all three features. It is a genuine
fork from the "keyless + local-sign" design: the wallet would derive keys from
the seed, restore them into a `monero-wallet-rpc` instance pointed at a node,
and proxy balance/transfer through it.

Recommended staging if we commit: **(a)** address-only in pure Python now
(correct, lightweight, consistent with the other chains); **(b)** the
wallet-rpc seam for balance/send as a follow-up.

### D2. Key-derivation convention

*Superseded:* the premise below is wrong — see *Plan → D2 dissolves*.

The derived XMR address must match an external wallet to be restorable.

- **Self-consistent (recommended to start):** derive a Monero secret spend key
  from the BIP39 seed entropy, then standard XMR
  (`view = keccak256(spend) mod l`, ed25519 pubkeys, base58 address). Restore
  via `show-seed` → the derived spend key. Document the scheme explicitly. The
  downside is it won't necessarily restore in Feather/Cake.
- **Match an existing wallet (Feather/Polyseed, Cake, …):** reverse-engineer
  and match that wallet's exact BIP39→XMR path so the same seed restores there.
  More research; conventions differ and drift.

## Implementation sketch (refined by *Plan* below)

- New `chains/monero.py` adapter implementing `base.ChainAdapter`:
  - `derive_address(mnemonic, path)` — pure-Python ed25519 + Monero base58.
  - `wallet_balance(mnemonic)` — proxy to `monero-wallet-rpc` (or stub).
  - a `send`/`build_and_verify_send` path mirroring the BTC send seam, with a
    `verify_monero_send` gate (recipient + amount) before any broadcast.
- Dependencies: pure-Python keccak + ed25519 (or vet `monero-python`, which
  also wraps `monero-wallet-rpc`). wallet-rpc receives the spend key, so it
  is always local, spawned by the CLI; only the node behind it is
  configurable (env `SWAPSACK_MONERO_NODE`, its `--daemon-address`).
- Wire into `cmd_address`, `_wallet_adapters` (balance), and `cmd_send`.
- Tests: address-derivation vectors (pin the chosen convention against a known
  seed→address pair); a mocked wallet-rpc for balance/send; a `verify_monero_send`
  gate test.

## Plan (proposed 2026-10-22)

### Swapping *to* XMR needs no holding

Sending crypto to a remote XMR address via THORChain does not need any of
this: the vault pays the destination directly. That part (phase *Dest* below) is
small, independent of custody, and can be built now. Holding is only needed
for XMR as a swap *source*, for symmetric LP, and for keeping XMR in the
wallet.

### D2 dissolves: the 25-word seed restores anywhere

Monero's legacy 25-word mnemonic is nothing but an encoding of the 32-byte
private spend key (24 words + a checksum word). Whatever scheme derives the
spend key from the BIP39 seed, `show-seed --asset XMR` can print those 25
words, and they restore in monero-wallet-cli, the GUI, Feather and Cake. So
the derivation only needs to be deterministic and documented, not to match
another wallet. Preferred: Trezor's scheme (SLIP-10 ed25519 at
`m/44'/128'/0'`, the derived key keccak-hashed, then reduced mod l; Ledger's
differs), *if* a published seed→address vector can be found to pin it;
otherwise a self-consistent scheme, documented. Whatever the scheme, the
spend key must be reduced mod l, or the 25 words encode a non-canonical key.

Also store a **restore height** (chain height at first derivation) in the
keystore metadata, or every restore scans from 2014. The legacy 25 words
carry no height, so `show-seed` prints it beside them.

### D1: `monero-wallet-rpc`, address-first

Take the recommended staging: pure-Python keys and addresses first, then a
`monero-wallet-rpc` seam for balance and send. wallet-rpc itself always runs
locally (it holds the spend key); the node behind it is the user's choice,
via `SWAPSACK_MONERO_NODE`:

- **own pruned `monerod`** (~100 GB, a day or two of sync): private;
- **remote node**: no setup, but it sees your IP, the blocks you fetch and
  the transactions (with their rings) you relay through it, so it can link
  your IP to a tx and could skew decoy selection. It never sees the view key
  or which ring member is the real spend; wallet-rpc still scans locally,
  slowly.

The wallet file wallet-rpc writes holds the spend key; encrypt it with a
password derived from the keystore passphrase, and keep it beside the
keystore.

### Phases and effort

| phase | what | network | estimate |
|---|---|---|---|
| Dest | XMR as swap **destination**: accept primary (`4…`) and subaddresses (`8…`), reject integrated ones (they also start with `4`: tell them apart by length/network byte) (THORChain refuses them, see https://gitlab.com/thorchain/thornode/-/merge_requests/5111); require a refund address; testable offline against quote fixtures | none | 2–3 h |
| A | keys, primary address, subaddresses, 25-word `show-seed`, restore height; derivation vectors pinned in tests. `monero` on PyPI (1.1.1, last release 2022-09) does seed/keys/addresses in pure Python; vendoring ed25519 + Monero base58 (~200 lines) is the alternative | none | 4–6 h |
| B | wallet-rpc seam: connect or spawn, `generate_from_keys` with restore height, `balance` (locked vs unlocked; received XMR is locked for 10 blocks) | wallet-rpc | 6–8 h |
| C | send: `transfer` with `do_not_relay=true, get_tx_metadata=true`, verify recipient and amount (`verify_monero_send`), then `relay_tx`. Weaker than the BTC gate: it checks wallet-rpc's own report, not a tx we built | wallet-rpc | 4–6 h |
| E | Monero **stagenet** end-to-end (public stagenet nodes + faucet), README/CHANGELOG | stagenet | 3–4 h |
| Src | XMR as swap **source** and LP deposit — blocked until THORChain publishes how an XMR deposit carries its memo (series S21, "memo parsing") | THORChain | ≥4 h, once specified |

Total without *Src*: about 19–27 h, plus setting up and syncing a node.
Order: Dest, A, B, C, E; Src after v3.21.1 is live.

## See also

- `docs/TODO.md` — "Swap backends" → BasicSwap (the other privacy/XMR path).
- `README.md` — currency roadmap row for XMR.
