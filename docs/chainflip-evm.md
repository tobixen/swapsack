# Chainflip vault swaps from an EVM source

Status: **shipped 2026-09-02**, unproven on mainnet in the only way that counts
— no EVM vault swap has been broadcast. Everything short of the broadcast is
covered by opt-in network tests that quote, encode against mainnet, build a real
unsigned transaction from a throwaway key and require the gate to pass — the
standing the Bitcoin path held until 2026-08-28, when it was broadcast for real
(deposit `d7bbc290…`, witnessed as swap 1764999). That one was *refunded* rather
than filled: the deposit sat in the mempool while BTC fell, and by the time two
confirmations landed the encoded floor was above spot. Nothing the gate is
responsible for failed, and fill-or-kill did what it is for — but it is the
sharpest available evidence that the floor is anchored at build time and
enforced much later. An EVM source waits on one confirmation rather than two
Bitcoin blocks, so the same gap exists here and is a good deal narrower.

`docs/chainflip-effort.md` is the Bitcoin story and the reasoning behind vault
swaps in general; this note is only what is *different* on an EVM source, and
the evidence for each claim. Everything below was probed live against
`mainnet-rpc.chainflip.io` on 2026-09-02.

## What changes, and what does not

Unchanged: no broker registers anything on our behalf, the destination is ours
to encode, the floor is ours to compute, the vault is checkable against
`cf_get_vault_addresses`, and the whole intention is readable out of the bytes
we are about to publish.

Changed, in the order it bites:

| | Bitcoin source | EVM source |
|---|---|---|
| transaction | pay the vault, OP_RETURN, change | call the Vault contract |
| parameters | a 48-byte compact payload | ABI arguments + a 96-byte SCALE `cfParameters` |
| amount | the vault output's value | `msg.value`, or an `xSwapToken` argument |
| refund address | the change output | stated explicitly, and gated as ours |
| floor | `min_output_amount` (absolute) | `min_price` (a rate) |
| destination width | fixed 20 bytes → EVM only | length-prefixed → EVM **or Bitcoin** |
| readback RPC | `cf_decode_vault_swap_parameter` | **none** |
| `--amount max` | impossible (needs change) | fine |

The last two are the ones worth pausing on.

**There is no readback.** `cf_decode_vault_swap_parameter` answers
`DispatchError: Decoding Vault Swap only supports Bitcoin and Solana`. On the
Bitcoin side that RPC was never the gate — asking the node that produced the
payload what the payload says proves nothing — but it was a second opinion the
live tests could lean on. Here `verify.decode_evm_vault_call` is the only
reading there is, which is why `tests/test_chainflip_evm.py` keeps two verbatim
mainnet encodings as golden fixtures and pins the local stub against them.

**A Bitcoin payout became reachable.** `bytes dstAddress` carries the address
*string's own ASCII* for a Bitcoin destination — `bc1qw508d6…` goes in as
`0x62633171…`. So an EVM source can pay BTC, and verifying that destination
needs no decoder at all, just a comparison. `can_settle_vault_swap` is where
both halves of that asymmetry live.

## The calldata

Two functions, both confirmed by keccak of their signatures as well as by
observation:

```
xSwapNative(uint32 dstChain, bytes dstAddress, uint32 dstToken, bytes cfParameters)
    -> 0xdd687345
xSwapToken(uint32 dstChain, bytes dstAddress, uint32 dstToken,
           address srcToken, uint256 amount, bytes cfParameters)
    -> 0x04fc7da0
```

`cf_request_swap_parameter_encoding` answers with `{chain, to, calldata, value}`
plus `source_token_address` for the token form. Vault contracts (in
`cf_get_vault_addresses`, and matching what the encoding names):

- Ethereum `0xf5e10380213880111522dd0efd3dbb45b9f62bcc`
- Arbitrum `0x79001a5e762f3befc8e5871b42f6734e00498920`

Chain ids: Ethereum 1, Bitcoin 3, Arbitrum 4, Solana 5. Asset ids: ETH 1,
FLIP 2, USDC 3, BTC 5, ARB-ETH 6, ARB-USDC 7, USDT 8, SOL 9, SOL-USDC 10.

## `cfParameters`, mapped byte by byte

96 bytes for the shape this wallet asks for. Mapped by differential encoding —
vary one RPC field, see which byte moves — the same technique that mapped the
Bitcoin payload:

| offset | field | how it was pinned |
|---|---|---|
| 0 | version = 1 | constant across every probe |
| 1–5 | `retry_duration` u32 LE | 100 → 101 moved byte 1 |
| 5–25 | `refund_address`, 20 bytes | `…dEaD` → `…bEEF` |
| 25–57 | `min_price` u256 LE | 1 → byte 25; 2^128 → byte 41 |
| 57 | `Option refund_ccm_metadata` | setting one grew the blob and shifted 58+ |
| 58 | `Option<u8> max_oracle_price_slippage` | setting 7 → `01 07` at 58 |
| 59 | `Option dca_parameters` | setting one → `01` + two u32 LE |
| 60 | `boost_fee` u8 | 9 → byte 60 |
| 61–93 | broker account id, 32 bytes | changed with the account asked |
| 93–95 | broker commission u16 LE | 7 → byte 93 |
| 95 | affiliate `Vec` length (compact) | 0 in every probe |

All three Options are `None` in what we ask for, and any `Some` lengthens the
blob past 96 — so the length check alone rules out every shape whose offsets
would differ. The decoder refuses anything else rather than guessing.

## `min_price`: the part to get right

Chainflip states a price as `output / input` in the two assets' **own base
units**, scaled by `2**128`. Evidence, rather than assertion:

- `cf_pool_price` for ETH/USDC returned `price = 0xa43bd7c00f31fec48dc7eab00`;
  divided by `2**128` that is `2.3899e-9` USDC-base-units per wei, i.e. ~$2390
  per ETH — which matched a live `/v2/quote` of the same pair
  (`2.394867372e-9`) to three figures, and its own `tick` of `-198531`
  reproduces the same number as `1.0001**tick`.
- `cf_pool_price_v2`'s `sell`/`buy` are the **square roots** of that (they equal
  v1's `sqrt_price` field). Copying those would be wrong by a square.
- Round trip: a `min_price` handed to `cf_request_swap_parameter_encoding` comes
  back out of the encoded blob bit-identical. Both golden fixtures assert it.

What is *not* settled from the chain is which two amounts the protocol compares.
The reading taken here is the swap's own ends — the deposit less the ingress
fee going in, the output before the egress fee coming out — so
`chainflip.min_price` puts both flat fees back:

```
price = ceil((floor + egress fee) * 2**128 / (deposit − ingress fee))
```

That is deliberately the strict reading. If the protocol instead compares the
gross deposit against the delivered output, this number is higher than that
reading needs, which costs a refund on a swap that drifted. The other rounding
is the one that quietly delivers less than promised, and there is no undoing it.
Rounding is up, for the same reason.

**Consequence worth knowing:** a tolerance tight enough to matter is now tight
against a floor that is a bit stricter than the printed one. The printed floor
(`min_output_amount`) is what the user is promised; the encoded price is at
least that.

## Gas

`eth_estimateGas` against mainnet for `xSwapNative`: **32,212** on Ethereum,
**32,754** on Arbitrum (which bills the L1 calldata cost as extra gas consumed).
`chains/eth.py` budgets 120,000 native / 250,000 token — generous, because an
unused limit is refunded while running out burns the whole limit having
delivered nothing. The token figure also has to cover the `transferFrom` the
Vault does on the way in. `--eth-gas` does **not** size the transaction here:
that knob sizes a memo deposit. It is still read on a **sweep**, as a floor —
`--amount max` has to decide what to hold back before a backend is chosen, so it
reserves whichever is larger, `--eth-gas` or the vault swap's own budget. Get
that wrong the other way and the transaction cannot pay for itself: the first
cut of this reserved the deposit's 60,000 and the node refused every sweep.

## What is still open

- **The mainnet broadcast.** Unproven, as above.
- **`min_price`'s exact comparands**, per the section above. Reading the
  swapping pallet, or watching one real swap refund and one fill, would settle
  it and could relax the floor slightly.
- **A Solana source**, which is a program instruction and another key entirely,
  and **Tron/Solana destinations**, which need base58check and a 32-byte
  address the gate can reproduce. Both are listed and quotable today and settle
  nowhere.
- **An allowance left behind.** A token source is two transactions, and if the
  Vault call fails after the approve, an exact-amount allowance to the Vault
  remains. The same is true of the THORChain token path, and the CLI warns
  about it the same way.

## Sources

- Live probes 2026-09-02 against `mainnet-rpc.chainflip.io`:
  `cf_request_swap_parameter_encoding` (Ethereum and Arbitrum sources, native
  and token, against Bitcoin/Ethereum/Arbitrum/Solana destinations),
  `cf_decode_vault_swap_parameter`, `cf_get_vault_addresses`, `cf_pool_price`,
  `cf_pool_price_v2`, `cf_oracle_prices`; and `chainflip-swap.chainflip.io`
  `/v2/quote`. Gas via `eth_estimateGas` on `ethereum-rpc.publicnode.com` and
  `arbitrum-one-rpc.publicnode.com`.
- <https://docs.chainflip.io/brokers/vault-swaps-api/evm>
- <https://docs.chainflip.io/brokers/vault-swaps-api/encoding-reference>
- In-repo: `docs/chainflip-effort.md` (the Bitcoin path and the vault-swap
  rationale), `tests/test_chainflip_evm.py` (golden fixtures),
  `tests/test_integration_chainflip.py` (the live end-to-end shape).
