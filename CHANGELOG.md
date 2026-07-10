# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is derived
automatically from git tags (PEP 440 / SemVer).

## [Unreleased]

### Added

- **ZEC wallet side, Phase 1 (receive-only):** `address` derives the Zcash
  transparent receive address (standard BIP44, `m/44'/133'/0'/0/0`), `balance`
  gap-limit scans and reports ZEC via a configurable lightwalletd gRPC
  endpoint (`--zec-lwd` / `$SWAPSACK_ZEC_LWD`, default `zec.rocks:443`), and
  `swap --to ZEC` auto-derives the destination from the seed (no `--dest`
  needed), warning loudly that the chain is receive-only. The spend path
  needs a bespoke signer (bitcoinlib cannot sign Zcash's tx format) and is
  deliberately not implemented — see docs/zcash.md. Adds a `grpcio`
  dependency.
- **DASH wallet side, Phase 1 (receive-only):** `address` derives the Dash
  receive address (standard BIP44, `m/44'/5'/0'/0/0`), `balance` gap-limit
  scans and reports DASH via a configurable Insight API (`--dash-api` /
  `$SWAPSACK_DASH_API`, default `insight.dash.org`), and `swap --to DASH`
  auto-derives the destination from the seed (no `--dest` needed), warning
  loudly that the chain is receive-only. The spend path (send/sweep/swap-from)
  is deliberately not implemented yet — see docs/dash.md.

## [0.1.0] - 2026-07-08

First release
