# What integrating Chainflip actually costs

Status: **both phases shipped** (2026-08-28), so what follows is an estimate
with its own outcome attached rather than a guess. B1 landed inside its
~600-line band. B2 landed too, and the three questions this note left open were
answered from the chain rather than by asking anyone — see *Answers* below.

| | estimated | actual |
|---|---|---|
| B1 (quotes) | ~1 session, ~600 lines | ~1 session, 1,026 insertions incl. tests |
| B2 (execution) | ~2–3 sessions, ~700–900 | 2 commits, ~1,100 insertions incl. tests |

## Answers to the questions this note left open

- **The broker account is inert for what we broadcast.** With a zero commission
  the payload is byte-identical whichever account is named (checked against two
  on 2026-08-28, and against all five in the list below on 2026-08-31); the
  account only selects *which* of the protocol's published vault addresses to
  pay, and the gate confirms that address against `cf_get_vault_addresses`
  either way. No host has to be up for a vault swap to be built — but that is
  not the same as being free of the chain, as the next paragraph says.

  What that answer missed is that the *choice* of account is not free. A broker
  can set a minimum commission it will encode for, and a broker demanding one is
  a broker this wallet cannot use — a commission is a skim the gate refuses. On
  2026-08-31 the single account hardcoded here ("Broker as a Service") began
  enforcing 5 bps, and every vault swap died on `DispatchError: Broker
  commission is too low`. The constant is now a fallback *list*
  (`DEFAULT_BROKER_ACCOUNTS`), tried in order, skipping a broker that refuses to
  encode at zero. The chain publishes no way to read a broker's minimum, so
  asking and reading the rejection is the only mechanism available.

  The pool is thinner than it looks: only a broker with a private Bitcoin
  channel can encode a vault swap at all, and of the 134 accounts
  `cf_all_account_infos` listed as brokers on 2026-08-31, 128 answered
  `NoPrivateChannelExistsForBroker`. Six could encode; five did so at zero
  commission. A network test asserts at least two still do, so the chain wearing
  down is noticed before it is load-bearing — the fallback list buys time, not
  immunity, and if that pool ever empties the choice becomes paying a small
  commission as an explicit, disclosed, gate-checked amount rather than the flat
  "nobody skims" the gate enforces today.
- **`max_oracle_price_slippage` is a `u8` whose 255 is what the protocol
  encodes when it is not asked for.** Its documented unit (basis points) cannot
  reach past 2.55% in a `u8`, so rather than set a number whose meaning is
  unclear, the wallet leaves the protocol default and relies on
  `min_output_amount` — a floor it computes, encodes and gates itself. Recorded
  as a decision, not an oversight.
- **Refunds go to the change output**, which the gate already requires to be an
  address we own. So the refund path is bound by the same check that stops a
  swap paying change to a stranger — which is also why a sweep cannot be a
  vault swap.

The payload layout was mapped by **differential encoding**: vary one RPC
parameter, see which byte moves. That is what made a local decoder possible, and
the local decoder is what makes the gate mean anything — asking the node that
produced the payload what the payload says would prove nothing. Answers "how much work is it?"
against the code as it stands, using the CoW backend (the last one that
shipped) as the empirical yardstick. Supersedes the execution half of
`docs/chainflip.md` — see §2, where its central finding turns out to be wrong
in a way that makes the work *smaller*, not larger.

**Headline: B1 (quote-only) ≈ one session, ~600 lines. B2 (execution) ≈ two to
three sessions, ~800 lines. Together, the same order as the CoW commit** —
1,692 insertions on 2026-07-12, with real bugs trailing into 07-14.

## 1. The yardstick: what CoW actually cost

`ccad12b`, one commit, quote **and** execute:

| File | + |
|---|---:|
| `src/swapsack/cow.py` | 398 |
| `src/swapsack/cli.py` | 250 |
| `src/swapsack/backends.py` | 119 |
| `src/swapsack/chains/eth.py` | 128 |
| `src/swapsack/verify.py` | 92 |
| tests (`test_cow`, `test_cli`, `test_eth`, integration) | 646 |
| docs / CHANGELOG / README | ~82 |

Two follow-ups landed after it, and one of them (`5d5b1f1`, "wait for the CoW
approval to mine before submitting the order") was a genuine money-path bug that
the first pass missed. Budget for that pattern again.

## 2. The finding that changes the plan

`docs/chainflip.md` (2026-07-13) rests on this: a broker registers your
destination address, and the verify gate closes that trust hole by reading the
channel's registered parameters back from the public
`cf_all_open_deposit_channels` / `cf_get_open_deposit_channels`.

**Both halves of that need revising.** Probed today:

- Those two RPCs return **liquidity-provision** channels — a list of
  `(address, asset)` pairs per LP/broker account, 640 KB of them, with **no
  destination address, no expiry and no refund parameters** anywhere in the
  structure. They cannot gate a swap. The readback plan as written does not work.
- It does not need to, because **Chainflip supports vault swaps**, which need no
  broker and no deposit channel at all. This is the route to build.

### Vault swaps, verified end to end against mainnet

The public State Chain RPC (`mainnet-rpc.chainflip.io`, keyless) encodes the
swap parameters for you:

```
cf_request_swap_parameter_encoding(
    <broker account id>,
    {"chain":"Bitcoin","asset":"BTC"},
    {"chain":"Ethereum","asset":"ETH"},
    "0x…dEaD",                                  # destination — OURS, we pass it
    0,                                          # broker commission
    {"chain":"Bitcoin","min_output_amount":"0x29a2241af62c0000",
     "retry_duration":100})
->  {"chain":"Bitcoin",
     "nulldata_payload":"0x0101…dead640000002cf61a24a2290000000000000000ff01000200000000",
     "deposit_address":"bc1p5rrs3gd9tlzucafucuj5jgvaj7rdtgn6je28y44wvvrv4d0vpsdslmnctx"}
```

Three properties matter, and all three were confirmed:

1. **The deposit address is a protocol vault, independently checkable.** That
   exact address is in `cf_get_vault_addresses`' `bitcoin` list. No broker
   holds it, and the gate can confirm membership from a second RPC.
2. **The destination is ours to encode, not a broker's to register.** It is
   visible in the payload (`…000000000000000000000000000000000000dead…`), and
   `cf_decode_vault_swap_parameter(<broker>, {chain, nulldata_payload,
   deposit_address})` round-trips it back in full:

   ```
   destination_asset {"chain":"Ethereum","asset":"ETH"}
   destination_address 0x000000000000000000000000000000000000dead
   broker_commission 0   boost_fee 0   affiliate_fees []
   extra_parameters {min_output_amount 0x29a2241af62c0000, retry_duration 100,
                     max_oracle_price_slippage 255}
   dca_parameters {number_of_chunks 1, chunk_interval 2}
   ```

   That is a *complete* independent readback of everything the OP_RETURN
   commits to — a stronger gate than CoW's EIP-712 fields, and in a different
   league from calldata.
3. **`min_output_amount` is a real on-chain floor inside the payload** (3e18 wei
   encoded little-endian as `2cf61a24a229…`), i.e. the same shape of protection
   as CoW's `buyAmount`, which `--tolerance-bps` maps onto directly.

**And the transaction shape is one the wallet already builds.** Chainflip
requires exactly three outputs, in order: pay the deposit address, the
nulldata OP_RETURN, then change (which doubles as the refund address).
`UtxoTxBuilder.build_unsigned_swap` emits `add_output(amount, vault)`,
`add_output(0, op_return)`, `add_output(change)` — that order, today, for
THORChain. The payload is 48 bytes against an 80-byte `OP_RETURN_MAX_BYTES`.

Two consequences of the same fact:

- **Expiry stops being the foot-gun `docs/chainflip.md` feared.** A vault swap
  is valid for two epoch rotations (~3–6 days), not a single-use channel that
  eats a late send.
- **`--amount max` cannot work.** The change UTXO must exist and clear dust, so
  a Chainflip vault swap can never be a sweep. It has to be refused explicitly.

## 3. B1 — quote-only, in `auto`

**~1 session, ~550–750 lines.** No money path.

- `src/swapsack/chainflip.py`, **~200–260 lines**: an `HttpClient`, the asset map
  (`BTC.BTC` ↔ `{"chain":"Bitcoin","asset":"BTC"}` + decimals), a quote parse
  into a `ChainflipQuote` with `expected_amount_out` in 1e8 and a `SwapFees`,
  plus `ChainflipBackend.serves`/`try_quote` and `default_chainflip_backend()`.
- The one genuinely fiddly part is **fee normalisation**. `SwapFees` is
  destination-denominated; Chainflip's `includedFees` come in **three different
  assets** (INGRESS in BTC, NETWORK in USDC, EGRESS in ETH). CoW solved the
  one-asset version of this by converting at the quote's own price
  (`fee_in_buy = fee_amount * buy_amount // sell_amount`); here the quote's
  `intermediateAmount` (the USDC leg) gives the second rate. Budget an hour and
  a test per fee type — a wrong conversion here silently misreports cost.
- `backends.py`: **~6 lines** in `swap_backends()`. `gather_quotes`,
  `best_quote`, `_select_backend` and `cmd_quote` are already generic — they
  touch only `expected_amount_out`, `fees.total_bps` and `fees.breakdown()`.
- `cli.py`: **~30–50 lines**. `--backend` choices/help, and — the part not to
  skip — a **refusal guard**. All four `_swap_from_*` paths do
  `with backend.client as thor: prepare_swap(thorchain=thor, …)`; a Chainflip
  backend reaching that would blow up mid-swap. One check on the
  `plain-deposit`/`vault-swap` executor, aborting cleanly until B2, is the whole
  fix, and it belongs in the same commit as the quote.
- Tests: `tests/test_chainflip.py` **~250–350 lines** (parse, the three-asset fee
  conversion, `serves` matrix, `try_quote` swallowing HTTP/parse errors as
  `None`), plus a network-marked `test_integration_chainflip.py` ~40 lines. The
  unit suite must stay offline — verify with `unshare -rn`.
- README status table, CHANGELOG, `docs/backends.md`.

Non-issue, contrary to what I wrote in `docs/halt-alternatives.md`: the
403 I hit is a `python-urllib` User-Agent block. **`niquests` — what
`net.py` actually uses — gets a 200** (checked against the project's own venv).
Nothing to do.

## 4. B2 — execution as a vault swap

**~2–3 sessions, ~700–900 lines.** Smaller than `docs/chainflip.md` implies,
because the broker, the channel and the expiry gate all evaporate.

- **A Substrate JSON-RPC helper**, ~120–180 lines with the encode/decode/vault
  calls and their parsing. The wallet has no JSON-RPC client for Substrate; it
  is plain `POST {"jsonrpc":"2.0",...}`, so this is small but new.
- **Binary OP_RETURN — the widest-rippling change.** `build_unsigned_swap` takes
  `memo: str | None` and does `memo.encode()`; the Chainflip payload is binary
  SCALE, which that would mangle. Widening it to accept `bytes` touches
  `chains/utxo.py`, `chains/zcash.py`, `chains/gated.py`, the `SwapPlan.memo`
  comparison in `verify.py`, and their tests — i.e. the **shared money-path
  builder for BTC, DASH and ZEC**. Mechanical, but it is the one change here
  that can break three chains at once, so it wants its own commit and its own
  test run, ahead of the Chainflip work.
- **`verify_chainflip_vault_swap` in `verify.py`**, ~80–120 lines + ~150 of
  tests: deposit address ∈ `cf_get_vault_addresses`; decoded destination and
  destination asset == ours; `min_output_amount` ≥ quoted × (1 − tolerance);
  `broker_commission == 0`, `affiliate_fees == []`, `boost_fee == 0`; then the
  existing `verify_btc_swap` on outputs/fee. `verify.py` stays import-free, so
  the RPC reads happen in the builder and arrive as plan data — exactly how
  `CowOrderPlan` works.
- **CLI path**, ~120–180 lines: a `vault-swap` branch off `_swap_from_utxo`
  (nearer to it than `_swap_via_cow` is to anything), plus the explicit sweep
  refusal.
- **`status`**: `chainflip-swap.chainflip.io/v2/swaps/{id}` is keyless and
  answers `{"message":"resource not found"}` for an unknown id, so the tracker
  exists. Whether the swap id is derivable from our own BTC txid is **unknown**
  — call it an extra evening if it is not.

### What is still open

- **The broker account id parameter.** The encode call requires one; I passed a
  vault-owning account with `broker_commission: 0` and it encoded fine. Confirm
  that a zero commission really means zero, and pick the account deliberately —
  it is a constant in our source either way, not a liveness dependency.
- `max_oracle_price_slippage: 255` came back from a request that never set it.
  Find out whether 255 means "disabled" before relying on `min_output_amount`
  as the only floor.
- ~~Refund semantics on a failed/expired swap~~ — **settled, and it is not
  ours to choose.** The Bitcoin vault-swap spec requires exactly three outputs
  in that order (deposit, nulldata, change) and says the change output's
  address "will be assumed to be refund address", mandatory "because the
  Chainflip protocol needs a refund address in case the swap is refunded". The
  48-byte payload has no field for one, so the sending (input) address cannot
  be nominated instead, and the change output cannot be dropped: that is why
  `--amount max` can never be a vault swap, and it is a protocol rule rather
  than a limitation of this wallet.
  <https://docs.chainflip.io/brokers/vault-swaps-api/bitcoin>
- `cf_swap_rate_v2`/`v3` would be a second, node-native quote source (no
  dependency on the hosted `chainflip-swap` service). Both accept the parameter
  shape but return "Simulated swap failed on the output leg" for a plain
  0.1 BTC → ETH; worth ~30 minutes to get right, and it hardens B1 against the
  hosted service going down — which, given why this survey exists, is not a
  hypothetical.

## 5. Recommendation

**Both phases are done.** What the estimate did not predict, in the order it
was found:

1. The generic `SwapFees.breakdown()` wording ("slip/swap fee") is wrong for
   Chainflip, so the fees object overrides it to name ingress/network/egress.
2. `auto` needed to *say* when a backend it cannot drive won on price, rather
   than quietly routing around a cheaper route the user could take by hand.
3. "Which executors can execute" is not one global set — a signed CoW order
   needs an EVM source and a vault swap is a Bitcoin transaction, so each
   `swap --from` path declares what it can actually drive.
4. The gate wanted its **own plan type**. Reusing `SwapPlan` would have meant a
   binary memo silently skipping `memo_pays_destination`, which is a text
   search; instead that function now *refuses* a binary memo when a destination
   is set, and `ChainflipVaultPlan` carries the binding the vault-swap gate
   does itself.

~~What is left, and deliberately so: **the broadcast is unproven on
mainnet.**~~ **Closed the same evening.** Everything up to the broadcast was
covered by an opt-in network test that quotes, encodes against mainnet, builds
a real unsigned transaction from a throwaway key and requires the gate to pass
— the same shape as CoW's "unfunded order clears every check up to
`InsufficientBalance`". Real BTC then closed the last step: deposit
`d7bbc290bcbdefbc3dd058ab8b0680842a596552051bc9f2ec3b159181214458` (block
964460) was witnessed as swap 1764999, and the payload the local decoder reads
matches field for field what the protocol reports back. It was refunded rather
than filled — the deposit went out at 1.13 sats/vB, sat in the mempool while
BTC fell ~2%, and the floor it had encoded — working back to a quote around
79,300 USDT/BTC, a level last seen about ninety minutes before the block —
could no longer be met, so fill-or-kill returned 498,294 of 500,000 sats to the change
output. Nothing the gate is responsible for failed; see `docs/TODO.md` for the
follow-ups that come out of it.

## Sources

- Live probes 2026-08-28 against `mainnet-rpc.chainflip.io`:
  `rpc_methods` (126 `cf_*`), `cf_request_swap_parameter_encoding`,
  `cf_decode_vault_swap_parameter`, `cf_get_vault_addresses`,
  `cf_all_open_deposit_channels`, `cf_get_open_deposit_channels`,
  `cf_swapping_environment`, `cf_environment`, `cf_swap_rate_v2/v3`; and
  `chainflip-swap.chainflip.io` (`/v2/quote`, `/v2/swaps/{id}`).
- Bitcoin vault swap transaction structure:
  <https://docs.chainflip.io/brokers/vault-swaps-api/bitcoin>
- Encoding reference: <https://docs.chainflip.io/brokers/vault-swaps-api/encoding-reference>
- In-repo yardstick: `git show ccad12b` (the CoW backend commit).
