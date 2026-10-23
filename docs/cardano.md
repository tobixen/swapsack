# Cardano (ADA) — design note

ADA used to be a destination only: a swap could pay an external `--dest`
address, but swapsack had no key for one. This note covers the wallet side,
built in the same three phases as ZEC (see [zcash.md](zcash.md)):

1. **Hold + balance** — keys, addresses, a Koios-backed gap-limit scan. *Done.*
2. **Send + sweep** — CBOR transaction building, fees, min-ADA, signing.
3. **Swap-from** — a Maya deposit carrying its memo in transaction metadata.

Liquidity provision is not planned yet; it would ride the phase-3 deposit
(an `+:ADA.ADA:…` memo) much as DASH/ZEC do.

## Venue

Only Maya has an `ADA.ADA` pool. THORChain has none and Chainflip doesn't
carry Cardano, so `--backend auto` has nothing to compare against, and a Maya
halt strands ADA. The pool is shallow: about 38,600 ADA against 78,400 CACAO
on 2026-10-23, so anything beyond a few hundred euros slips noticeably — the
`Market:` line shows it.

## Keys and addresses

Cardano is not BIP32/secp256k1. Shelley wallets (Yoroi, Eternl, Lace,
Daedalus) use:

- an **Icarus** master key (CIP-3): PBKDF2-HMAC-SHA512 over the mnemonic's
  *entropy*, 4096 rounds, 96 bytes, with the BIP39 passphrase as the
  password (raw UTF-8, as those wallets and pycardano do), then clamped;
- **BIP32-Ed25519 V2** child derivation along **CIP-1852**
  (`m/1852'/1815'/0'/role/index`; role 0 receive, 1 change, 2 stake);
- **base addresses** (`addr1q…`): header `0x01`, then the blake2b-224 hashes of
  the payment key and of the account's single stake key (`…/2/0`).

So the same words and passphrase restored in Yoroi or Eternl show the same
addresses and the same balance. Ledger hardware wallets use a different
master-key derivation, so a seed that lives on a Ledger does *not* map to these
addresses.

The owner chose base over enterprise (`addr1v…`, payment key only) addresses:
they are what every wallet shows and they can be staked. The cost is length:
103 characters pushes a swap memo past the 80-byte OP_RETURN that a BTC, DASH
or ZEC source has to fit it in, so swapping *to your own* ADA from those
sources is refused up front (`_dest_chain_caveats`); from ETH it works.

### Implementation choice

Bespoke, on **PyNaCl** (libsodium) for the Ed25519 scalar and point
operations, rather than `pycardano`. pycardano pulls in 18 dependencies,
including `docker`, `ogmios`, `blockfrost-python` and `websockets`, and its
`__init__` imports every backend eagerly, so there is no light install; it
also broke against `cbor2` 6.x when this was written. It is used only
offline, to generate the golden vectors in `tests/test_cardano.py`; the
`m/1852'/1815'/0'/0/0` address of the standard test mnemonic is also the one
Yoroi/Eternl show for it.

An extended private key is `(kL, kR)` where `kL` *is* the scalar, so signing
cannot go through a standard Ed25519 API: `ExtendedKey.sign` does
r = H(kR‖M), R = r·B, S = r + H(R‖A‖M)·kL by hand. The output is an ordinary
Ed25519 signature, and `sign` verifies it with a stock verifier before
returning, so a key bug cannot reach a broadcast.

## Data source: Koios

[Koios](https://api.koios.rest) is keyless; Blockfrost needs an API key.
`--ada-api` / `$SWAPSACK_ADA_API` names another Koios instance.

- `POST /address_info` answers the scan: an address that ever appeared on
  chain comes back (balance 0 if emptied), a never-used one is absent — so
  presence is the gap-limit history signal, and one call per address is enough.
- Koios wants POST bodies even for reads (GET fails with a read-only-transaction
  error), so queries go through `HttpClient._post_query`, which shares the
  GET retry loop. Broadcasts stay on the plain, never-retried `_post`.
- Koios indexes the ledger only, so there is no pending balance and no
  `--allow-unconfirmed`.

A full scan of an empty account takes about six seconds.

## Units

The adapter speaks **1e-8 ADA** everywhere at its surface — balances, UTXO
values, amounts, fees — because that is what Maya quotes in and what the
shared UTXO flow and verify gate count in. Chain values are lovelace (1e-6),
scaled by `LOVELACE_TO_UNITS` (100) on the way in and back on the wire.

## How Maya reads an ADA deposit (for phase 3)

From Maya's bifrost (`bifrost/pkg/chainclients/cardano/client.go`) and its
`gitlab.com/mayachain/cardano-go` v0.2.8 (`block.go: ExtractMemo`):

- The memo lives in **transaction metadata**. Maya checks its own label 6676
  (`{6676: {"memo": [chunks]}}`) first and falls back to **CIP-20**, label 674
  (`{674: {"msg": [chunks]}}`), in both the Shelley (bare map) and Alonzo
  (tag 259) aux-data shapes. Chunks are concatenated; each metadata string is
  at most 64 bytes.
- Real inbound swaps use both: e.g.
  `6b714fd9e2fba152266987efa0419c0ff77a7d636f8a30fb160f850a33ef38f1` carried
  CIP-20 `{674: {msg: […]}}` in a bare map and was processed as a normal swap.
  swapsack will use CIP-20: the Cardano standard, shown by explorers and wallets.
- The **sender** (refund address) is the address of the *first input's* UTXO.
- The inbound is the first output paying a vault; at most 10 outputs; the memo
  must fit Maya's `MaxMayaMemoSize`. The vault's `dust_threshold` is 1 ADA.

## Transactions

`chains/cardano_tx.py` builds the slice of the ledger format a pure-ADA wallet
needs, on `cbor2`: tag-258 input set, plain `[address, lovelace]` outputs, fee,
TTL and, for a deposit, the CIP-20 memo bound by its hash (body key 7). No
tokens, scripts or certificates.

- **Fee** = `min_fee_a` × size + `min_fee_b` (44 and 155,381 lovelace on
  mainnet), sized on placeholder witnesses and iterated, since the fee's own
  CBOR length counts.
- **Min-ADA**: every output must hold (160 + its serialized size) ×
  `coins_per_utxo_byte`, about 0.97 ADA for a base-address output. A change
  smaller than that cannot exist and is folded into the fee.
- **Tests** pin two transactions: a real Maya deposit from mainnet (its txid is
  the hash of the body as written, its witness verifies, its CIP-20 memo
  decodes), and one built and signed here, which pycardano decodes to the same
  id, accepts, and re-encodes byte for byte.
- `parse` is the gate's neutral reader: it hashes the body bytes as written
  rather than a re-encoding (cbor2 turns tag-258 sets into Python sets, whose
  order is not stable), and refuses aux data the body does not commit to.
