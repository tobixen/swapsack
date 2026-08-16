# swapsack live session — 2026-08-16 (redacted)

Real funds on mainnet. This session exists in the docs because it moved three
paths from "implemented, gated, unit-tested, **never broadcast**" to proven, and
several files claimed otherwise until it did. Where another document says
"proven on mainnet", this is the citation.

**Why this one is redacted and `docs/live-session-2026-07-24.md` is not.** That
run used a throwaway test wallet, so its addresses, amounts and txids are
deliberately in the open. This run used the **owner's personal wallet**.
Publishing it in his own repo would bind those addresses to him by authorship,
which is the thing worth avoiding — the addresses being real is not the problem,
the name-to-address link is. Removed: addresses, transaction ids, block numbers,
clock times, **and amounts** — an exact amount is a search key that finds the
transaction, so rounding it is not enough.

Kept: durations, basis points, protocol parameters and the mechanics. Those are
what the document is *for*, and none of them narrows a search. Section 3 is kept
in full because those two swaps were **rejected at the quote** and never
produced a transaction, so nothing in them is on-chain.

Market context at the time: CACAO ≈ $0.11, ETH ≈ €1600, Ethereum mainnet base
fee well under 0.1 gwei — unusually cheap, so the fee observations below are not
typical.

## Summary

**Proven for the first time:**

| Path | What ran |
|---|---|
| A TRON spend of any kind | first TRX transaction this wallet has broadcast |
| Cosmos `MsgDeposit` on Maya (CACAO) | first CACAO spend of any kind |
| Two-leg symmetric liquidity, end to end | both legs paired into one position |
| ERC-20 router LP add carrying a *symmetric* memo | the asset leg of that add |

**Still unproven after this session** — do not read the above as more than it
says:

- **CACAO/RUNE `MsgSend`** (a plain `send`). Only `MsgDeposit` was exercised.
  The two build different protobuf messages; see the note under *Why the
  rehearsal advice was wrong*.
- **swap-`--from` CACAO.** Both attempts aborted at the *quote*, so nothing was
  ever built or signed. It shares `_prepare_deposit` with the LP leg, so the
  assembly/signing/broadcast machinery is proven — but its own swap-memo and
  destination binding are not.
- **RUNE anything.** THORChain LP is paused and no RUNE swap was made.
- **Arbitrum anything.** No ARB transaction has been signed or broadcast.
- **DASH, ZEC, USDT-TRON, and a plain ETH `send`.** Unchanged.

## 1. `swap --from TRX --to ETH`

The first TRON broadcast: a native TRX deposit to THORChain's TRON inbound
vault carrying a `=:e:<our EVM address>:<limit>` memo, paid out as ETH to the
same wallet.

**End to end: 41 seconds**, deposit confirmation to payout mined.

THORChain's own quote estimated 15 s (3 inbound confirmations ≈ 9 s, zero
outbound delay). That figure is a protocol arithmetic floor — it does not count
observer consensus, TSS signing of the outbound, or destination-block
inclusion. 41 s is the honest number for this shape, and it generalises to
*nothing else*: both slow parts of a THORChain swap (value-scaled inbound
confirmation counting, value-scaled outbound delay) were ~0 for a deposit this
small. A large BTC source is minutes to an hour.

## 2. `add-liquidity --symmetric --asset USDC-ETH --backend maya`

The first two-sided add, and the first CACAO spend. The CACAO amount was
computed from the live pool ratio, as designed.

```
protocol leg  MsgDeposit on MAYA, from <our maya1 address>
              memo +:ETH.USDC-0XA0B8…EB48:<our EVM address>
              network fee 0.2 CACAO (Maya's flat native tx fee)

asset leg     status SUCCESS
              approve + depositWithExpiry via router 0xe3985E6b…B46d
              memo +:ETH.USDC-0XA0B8…EB48:<our maya1 address>
```

Each memo names the **other** leg's address — that cross-reference is the whole
pairing mechanism, and it is what to check first if a future add fails to pair.

The resulting record from `/mayachain/pool/ETH.USDC-…/liquidity_provider/` had
the shape:

```
units                 <non-zero>
asset_address         <our EVM address>
cacao_address         <our maya1 address>
cacao_deposit_value   <the CACAO leg>
asset_deposit_value   <the USDC leg>
cacao_redeem_value    <deposit, less 2.7 bps>
asset_redeem_value    <deposit, plus 2.7 bps>
pending_cacao         0
pending_asset         0
```

`pending_*` at zero **with** non-zero `units` is the proof the legs paired: a
half-add leaves one side pending and `units` at 0. That is the field to check
first when verifying any future symmetric add.

**No entry slip, measured**: +2.7 bps on the USDC side, −2.7 bps on the CACAO
side. That is the one thing symmetric buys over a single-sided add, and it is
now demonstrated rather than asserted.

**Query it by the CACAO address.** The same lookup keyed by the *asset* address
returns a zeros stub — the cause of the `balance` bug in `docs/TODO.md`'s
*Known bugs*, which is why this position does not appear in `swapsack balance`.

## 3. Two quote rejections worth keeping

Both are correct protocol behaviour, both were opaque on screen, and both are
the observed cases behind *Known bugs* entries in `docs/TODO.md`. Neither
produced a transaction — they were refused before anything was built.

`swap --from CACAO --to ETH --amount 10` →
`not enough asset to pay for fees`. 10 CACAO ≈ $1.13; Maya's flat ETH outbound
fee is 0.00075 ETH ≈ €1.22. The swap could not pay for its own delivery. Maya's
floor for CACAO→ETH was 50.04 CACAO. Routing to `ETH-ARB` instead drops the
floor to **4.31 CACAO**, because the ARB outbound fee is 6459 against ETH's
75000 — a 12× difference that makes cheap rehearsals possible.

`swap --from CACAO --to ETH --amount 400` →
`outbound amount does not meet requirements (2323009/2326360)`. The pair is
`emitted/limit` in 1e8. The swap's real cost was **303 bps** against the 300 bps
default tolerance — rejected by three basis points. Confirmed against the live
quote API that `expected_amount_out` is identical at 300/400/500/1000 bps:
tolerance sets only the memo's min-out limit, never the price obtained, so
raising it costs nothing.

## Why the rehearsal advice was wrong

An earlier version of `docs/TODO.md` recommended proving the CACAO path with
"a small plain `send --asset CACAO`". That does not rehearse the symmetric LP
leg. `send` builds a **`MsgSend`**; both `swap --from CACAO` and the LP protocol
leg build a **`MsgDeposit`** — different protobuf messages, and only the latter
two share `cosmos.py::_prepare_deposit` with the path under test. A cheap
`swap --from CACAO --to ETH-ARB` is the correct rehearsal. Recorded because the
wrong version was committed and acted on.

## Four reporting bugs found

Every defect this session surfaced was in *reporting*, not in a money path — the
transactions were all correct and the tool described them wrongly. See
`docs/TODO.md` *Known bugs* for the diagnoses. This is the standing reason to
verify against the chain rather than trusting `swapsack`'s own output.
