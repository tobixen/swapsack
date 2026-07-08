# Streaming swaps — notes

Status: **implemented** for `quote` and `swap` (`--stream-interval` /
`--stream-quantity`). This note records how it works and, in particular, the
non-obvious interaction with `--tolerance-bps` that cost some debugging — so it
isn't re-discovered the hard way.

## What it is

A streaming swap splits one trade into several **sub-swaps** spread over blocks.
Each sub-swap hits the pool with a smaller amount, so the price impact (slip) of
each is small; the pool also partially re-fills between them. The net effect is
a much better rate on large or thinly-pooled swaps, at the cost of a longer
settlement (the trade is only complete after all sub-swaps land).

THORChain/Maya express it in the swap **memo's limit field**:

```
=:ASSET:DEST:LIM              # ordinary swap
=:ASSET:DEST:LIM/INTERVAL/QUANTITY   # streaming swap
```

- `INTERVAL` — blocks between sub-swaps (`--stream-interval`, ≥ 1).
- `QUANTITY` — number of sub-swaps (`--stream-quantity`; `0`/omitted = let the
  network pick the count that minimises slip).
- `LIM` — the price limit. For a streaming swap this is set to **0** (no limit):
  streaming manages slippage itself, so a fixed limit is neither needed nor
  wanted (see the gotcha below).

## Wiring in this wallet

The two params are threaded from the CLI (`_streaming_kwargs`) through
`backends.gather_quotes` (so `--backend auto` selects on the same streamed
price) and `swap.prepare_swap` into `ThorchainClient.quote_swap`, which forwards
them to the quote API. The API returns a memo already carrying the
`…/INTERVAL/QUANTITY` suffix, and the wallet broadcasts the quote's memo
verbatim — so nothing extra is needed to *trigger* streaming on-chain.

**Verify gate:** no change was required. The gate binds the tx memo to the
quote's memo exactly and additionally checks our destination is a substring of
it. A streaming suffix comes *after* the destination, so the destination is
still bound and the extra `/INTERVAL/QUANTITY` fields don't trip anything. This
is pinned by tests in `tests/test_verify.py`
(`test_streaming_memo_is_accepted_and_still_binds_destination`, plus rejection
tests for a memo that pays someone else or drops the suffix).

**Display:** when the network actually streams (`streaming_swap_blocks > 0`),
`quote`/`swap` print a `stream:` line with the sub-swap count and estimated
settlement time, so the time-exposure tradeoff is visible next to the cost
breakdown.

## The gotcha: streaming vs. `--tolerance-bps`

**A tolerance limit and streaming do not mix.** If you send an explicit
`tolerance_bps`, the node derives a tight price limit and evaluates the swap
against it *before* streaming's slip reduction is credited — so it reports the
**base (non-streamed) emit** and refuses with
`emit asset X less than price limit Y`, even though the streamed result would
easily clear.

Observed live (0.05 BTC → DASH on Maya):

| request | emit | slip | result |
|---|--:|--:|---|
| plain, `tol=300` | 8.305 DASH-units | 395 bps | refused |
| `stream-interval 1`, `tol=300` | 8.305 (base) | — | **refused** |
| `stream-interval 1`, `tol=None` | 8.965 | 30 bps | **cleared** |

So streaming must send `tolerance_bps=None` (LIM=0) and let streaming manage
slip. The subtle trap: **merely omitting** `tolerance_bps` from the call is not
enough — `ThorchainClient.quote_swap` defaults it to `DEFAULT_TOLERANCE_BPS`
(300), which re-introduces the tight limit. It has to be passed as `None`
*explicitly*. Both `gather_quotes` and `prepare_swap` do this whenever
`streaming_interval` is set, and it is covered by
`test_streaming_drops_tolerance_bps` (backends) and
`test_prepare_streaming_drops_tolerance_limit` (swap).

Consequently the CLI treats `--stream-interval` as overriding `--tolerance-bps`
(documented in both flags' `--help`).

## Tradeoffs / caveats

- **Time exposure.** The swap settles over `streaming_swap_blocks` (≈ the printed
  duration); funds are in-flight and exposed to price movement the whole time.
  Streaming trades *slip* for *duration + volatility risk*.
- **Not always beneficial.** For small/low-slip swaps the network returns
  `max_streaming_quantity: 1` / `streaming_swap_blocks: 0` — i.e. it declines to
  stream, and the wallet shows no `stream:` line. Streaming only helps when slip
  is the dominant cost.
- **Broadcast unproven on mainnet** — like every spending path here, the quote/
  memo/gate path is verified end-to-end but no funds have been streamed for real.

## See also

- `README.md` — "Other features" (streaming bullet) and usage example.
- `CHANGELOG.md` — Unreleased → streaming swaps.
- `src/swapsack/backends.py`, `swap.py` — the tolerance-drop logic.
