"""Zcash chain adapter — Phase 1: t-addr derivation + balance (receive-only).

Zcash's transparent addresses are legacy P2PKH with a two-byte base58 prefix;
derivation shares :mod:`swapsack.chains.p2pkh` with Dash. Balances come from a
**lightwalletd** gRPC endpoint (the canonical Zcash light-client infra, several
reputable public operators; configurable — see docs/zcash.md). Swaps route
through Maya only (no ZEC pool on THORChain), and Maya's pool is
transparent-only, so shielded (``zs1…``/``u1…``) funds are out of scope.

The gRPC messages here are tiny, so the wire format is hand-rolled from the
``service.proto`` definitions (reusing the cosmos_tx protobuf primitives)
rather than pulling in protobuf codegen; grpcio handles the transport with
identity (de)serializers.

The spend side (send/sweep/swap-from) is deliberately NOT implemented — Zcash's
tx format (ZIP-243/225 sighash) cannot be signed by bitcoinlib, see
docs/zcash.md Phase 2 — so ``broadcast`` refuses loudly rather than ever
pretending to work. Funds received here are spendable by importing the seed
into another Zcash wallet (standard BIP44, ``m/44'/133'/0'/0/x``).
"""

from __future__ import annotations

import grpc

from swapsack.chains.base import AddressInfo, BalanceReport
from swapsack.chains.cosmos_tx import (
    _delimited,
    _read_fields,
    _string,
    _uint64,
)
from swapsack.chains.p2pkh import derive_p2pkh_address

DEFAULT_ZEC_LWD = "zec.rocks:443"
DEFAULT_DERIVATION = "m/44'/133'/0'/0/0"
ACCOUNT = "m/44'/133'/0'"
PREFIX_P2PKH = b"\x1c\xb8"  # transparent P2PKH, addresses start with "t1"

_RPC = "/cash.z.wallet.sdk.rpc.CompactTxStreamer/"


# --- lightwalletd wire format (service.proto, hand-rolled like cosmos_tx) ----


def encode_address_list(addresses: list[str]) -> bytes:
    """``AddressList { repeated string addresses = 1; }``"""
    return b"".join(_string(1, address) for address in addresses)


def decode_balance(data: bytes) -> int:
    """``Balance { int64 valueZat = 1; }`` (proto3: zero is absent)."""
    return _read_fields(data).get(1, [0])[0]


def decode_block_id_height(data: bytes) -> int:
    """The height from a ``BlockID { uint64 height = 1; bytes hash = 2; }``."""
    return _read_fields(data).get(1, [0])[0]


def encode_block_filter(address: str, *, start: int, end: int) -> bytes:
    """``TransparentAddressBlockFilter { string address = 1; BlockRange range = 2; }``

    with ``BlockRange { BlockID start = 1; BlockID end = 2; }`` and heights in
    ``BlockID.height`` (field 1).
    """
    block_range = _delimited(1, _uint64(1, start)) + _delimited(2, _uint64(1, end))
    return _string(1, address) + _delimited(2, block_range)


class ZecAdapter:
    """ChainAdapter for Zcash (transparent P2PKH), Phase 1: address + balance."""

    chain = "ZEC"
    asset = "ZEC.ZEC"
    # The ZEC.ZEC pool exists only on Maya — and THORChain answers an LP probe
    # for a pool it doesn't run with a 500, not a clean "no position" 404.
    lp_backends = ("maya",)

    def __init__(
        self,
        lwd_url: str = DEFAULT_ZEC_LWD,
        timeout: float = 20.0,
        bip39_passphrase: str = "",
    ) -> None:
        self.lwd_url = lwd_url
        self._timeout = timeout
        self.bip39_passphrase = bip39_passphrase
        self._channel: grpc.Channel | None = None
        self._tip: int | None = None  # chain tip, cached for one scan's probes

    # --- lifecycle (mirrors net.HttpClient so `balance` can `with adapter:`) --

    @property
    def _grpc(self) -> grpc.Channel:
        if self._channel is None:
            self._channel = grpc.secure_channel(
                self.lwd_url, grpc.ssl_channel_credentials()
            )
        return self._channel

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    def __enter__(self) -> ZecAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- derivation ------------------------------------------------------------

    def derive_address(self, mnemonic: str, path: str = DEFAULT_DERIVATION) -> str:
        return derive_p2pkh_address(mnemonic, path, PREFIX_P2PKH, self.bip39_passphrase)

    # --- network via lightwalletd; guarded by an opt-in live test --------------

    def _unary(self, method: str, request: bytes) -> bytes:
        try:
            return self._grpc.unary_unary(_RPC + method)(request, timeout=self._timeout)
        except grpc.RpcError as exc:  # keep grpc types out of callers (cmd_balance)
            raise RuntimeError(
                f"lightwalletd {method}: {exc.code().name}: {exc.details()}"
            ) from exc

    def latest_height(self) -> int:
        return decode_block_id_height(self._unary("GetLatestBlock", b""))

    def _has_history(self, address: str, tip: int) -> bool:
        """Whether any tx ever touched ``address`` (streaming, cancelled at one).

        A used-but-emptied address must keep the gap-limit scan going, and the
        balance alone cannot tell it from a fresh one — so probe the txid index.
        """
        stream = self._grpc.unary_stream(_RPC + "GetTaddressTxids")(
            encode_block_filter(address, start=1, end=tip), timeout=self._timeout
        )
        try:
            first = next(iter(stream), None)
        except grpc.RpcError as exc:
            raise RuntimeError(
                f"lightwalletd GetTaddressTxids: {exc.code().name}: {exc.details()}"
            ) from exc
        finally:
            stream.cancel()  # one hit answers the question; drop the rest
        return first is not None

    def address_info(self, address: str) -> AddressInfo:
        if self._tip is None:
            self._tip = self.latest_height()
        if not self._has_history(address, self._tip):
            return AddressInfo(has_history=False, confirmed=0, pending=0)
        balance = decode_balance(
            self._unary("GetTaddressBalance", encode_address_list([address]))
        )
        # lightwalletd exposes no per-address mempool delta; pending stays 0.
        return AddressInfo(has_history=True, confirmed=balance, pending=0)

    def wallet_balance(self, mnemonic: str, account: str = ACCOUNT) -> BalanceReport:
        from swapsack.chains.scan import scan_account

        self._tip = self.latest_height()  # one tip for the whole scan
        records = scan_account(
            derive_address=lambda p: self.derive_address(mnemonic, p),
            probe=self.address_info,
            account=account,
        )
        return BalanceReport(
            symbol="ZEC",
            confirmed=sum(info.confirmed for _, _, info in records),
            decimals=8,
            pending=0,
            note=f"({len(records)} used addresses)",
            addresses=tuple(address for _, address, _ in records),
        )

    def broadcast(self, raws: list[str]) -> str:
        raise NotImplementedError(
            "the ZEC spend path is not implemented (receive/balance only; "
            "bitcoinlib cannot sign Zcash's tx format) — see docs/zcash.md Phase 2"
        )
