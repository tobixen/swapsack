# Monero (XMR) support — design notes

Status: **not started.** Procrastinated on purpose — Monero does not fit the
wallet's existing model and needs a custody/architecture decision before any
code is worth writing. This note records the analysis so the work is
recoverable later.

THORChain's XMR pool is "coming soon" (see the roadmap in `README.md`), so
swaps to/from XMR aren't possible yet regardless. The three features the owner
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
   we match that wallet's exact convention. This is a real decision (see below),
   not a detail.

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

## Open decisions (blocking — owner's call)

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

The derived XMR address must match an external wallet to be restorable.

- **Self-consistent (recommended to start):** derive a Monero secret spend key
  from the BIP39 seed entropy, then standard XMR
  (`view = keccak256(spend) mod l`, ed25519 pubkeys, base58 address). Restore
  via `show-seed` → the derived spend key. Document the scheme explicitly. The
  downside is it won't necessarily restore in Feather/Cake.
- **Match an existing wallet (Feather/Polyseed, Cake, …):** reverse-engineer
  and match that wallet's exact BIP39→XMR path so the same seed restores there.
  More research; conventions differ and drift.

## Implementation sketch (once D1/D2 are decided)

- New `chains/monero.py` adapter implementing `base.ChainAdapter`:
  - `derive_address(mnemonic, path)` — pure-Python ed25519 + Monero base58.
  - `wallet_balance(mnemonic)` — proxy to `monero-wallet-rpc` (or stub).
  - a `send`/`build_and_verify_send` path mirroring the BTC send seam, with a
    `verify_monero_send` gate (recipient + amount) before any broadcast.
- Dependencies: pure-Python keccak + ed25519 (or vet `monero-python`, which
  also wraps `monero-wallet-rpc`); a config seam for the wallet-rpc endpoint
  (env `SWAPSACK_MONERO_RPC`, mirroring the ETH/TRON RPC flags).
- Wire into `cmd_address`, `_wallet_adapters` (balance), and `cmd_send`.
- Tests: address-derivation vectors (pin the chosen convention against a known
  seed→address pair); a mocked wallet-rpc for balance/send; a `verify_monero_send`
  gate test.

## See also

- `docs/TODO.md` — "Swap backends" → BasicSwap (the other privacy/XMR path).
- `README.md` — currency roadmap row for XMR.
