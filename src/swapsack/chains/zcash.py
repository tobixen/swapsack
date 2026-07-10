"""Zcash chain adapter — Phase 1 (hold/balance) + Phase 2 (send/sweep).

Zcash's transparent addresses are legacy P2PKH with a two-byte base58 prefix;
derivation shares :mod:`swapsack.chains.p2pkh` with Dash. Everything network
(balance / UTXOs / branch id / broadcast) speaks to a **lightwalletd** gRPC
endpoint (the canonical Zcash light-client infra, several reputable public
operators; configurable — see docs/zcash.md). Swaps route through Maya only
(no ZEC pool on THORChain), and Maya's pool is transparent-only, so shielded
(``zs1…``/``u1…``) funds are out of scope.

The gRPC messages here are tiny, so the wire format is hand-rolled from the
``service.proto`` definitions (reusing the cosmos_tx protobuf primitives)
rather than pulling in protobuf codegen; grpcio handles the transport with
identity (de)serializers.

Spending does NOT go through bitcoinlib (it cannot sign Zcash's tx format):
the bespoke v4/ZIP-243 builder+signer lives in
:mod:`swapsack.chains.zcash_tx`, anchored to a real mainnet transaction in the
tests. Fees follow ZIP-317 (action-based, see ``coins.zip317_fee``), and the
consensus branch id is fetched live per spend — never hardcoded — because a
stale id after a network upgrade would invalidate every signature. The
swap-*from* side (vault deposit + memo) is Phase 3 and not wired yet.
"""

from __future__ import annotations

import dataclasses

import grpc

from swapsack.chains.base import AddressInfo, BalanceReport
from swapsack.chains.coins import (
    InsufficientFunds,
    Utxo,
    select_coins_zip317,
    sweep_amount_zip317,
)
from swapsack.chains.cosmos_tx import (
    _delimited,
    _read_fields,
    _string,
    _uint64,
)
from swapsack.chains.p2pkh import derive_p2pkh_address, derive_p2pkh_key
from swapsack.chains.zcash_tx import (
    TxIn,
    TxOut,
    TxV4,
    address_to_script,
    parse_v4,
    script_to_address,
    serialize_v4,
    sign_transparent,
)
from swapsack.chains.zcash_tx import (
    txid as compute_txid,
)
from swapsack.swap import Prepared
from swapsack.verify import SendPlan, TxOutput, verify_btc_send

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


def encode_utxos_arg(address: str, *, start: int = 1) -> bytes:
    """``GetAddressUtxosArg { repeated string addresses = 1; uint64 startHeight = 2;
    uint32 maxEntries = 3; }`` (maxEntries 0 = no limit, omitted per proto3)."""
    return _string(1, address) + _uint64(2, start)


def decode_utxos_reply(data: bytes, address: str) -> list[Utxo]:
    """Parse a ``GetAddressUtxosReplyList`` into confirmed :class:`Utxo` rows.

    Reply fields: ``txid=1`` (bytes, tx-serialization order), ``index=2``,
    ``script=3``, ``valueZat=4``, ``height=5``, ``address=6``. The txid is
    byte-reversed into the display order the rest of the wallet uses. The
    address index only holds mined outputs, so everything here is confirmed.
    """
    utxos = []
    for entry in _read_fields(data).get(1, []):
        fields = _read_fields(entry)
        utxos.append(
            Utxo(
                txid=fields[1][0][::-1].hex(),
                vout=fields.get(2, [0])[0],
                value=fields[4][0],
                address=address,
            )
        )
    return utxos


def decode_branch_id(data: bytes) -> int:
    """The consensus branch id from a ``LightdInfo`` (field 6, a hex string)."""
    return int(_read_fields(data)[6][0].decode(), 16)


def decode_send_response(data: bytes) -> tuple[int, str]:
    """``SendResponse { int32 errorCode = 1; string errorMessage = 2; }``"""
    fields = _read_fields(data)
    code = fields.get(1, [0])[0]
    message = fields.get(2, [b""])[0].decode(errors="replace")
    return code, message


# A broadcast tx expires (and its funds unlock) if unmined for this many
# blocks (~75s each): long enough for congestion, short enough not to linger.
EXPIRY_DELTA = 40


@dataclasses.dataclass
class ZecBuilt:
    """A built (unsigned) transparent spend, ready for the verify gate + signer."""

    tx: TxV4
    spent: list[tuple[bytes, int]]  # per input: (scriptPubKey, value)
    privkeys: list[bytes]
    outputs: list[TxOutput]  # re-extracted from the serialized bytes (neutral)
    fee: int
    change_address: str
    branch_id: int


class ZecAdapter:
    """ChainAdapter for Zcash (transparent P2PKH): hold, balance, send, sweep."""

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

    # --- Phase 2: send / sweep (bespoke v4/ZIP-243 signer, ZIP-317 fees) -------

    def fetch_utxos(self, address: str) -> list[Utxo]:
        resp = self._unary("GetAddressUtxos", encode_utxos_arg(address))
        return decode_utxos_reply(resp, address)

    def branch_id(self) -> int:
        """The ACTIVE consensus branch id, fetched live (never hardcoded)."""
        return decode_branch_id(self._unary("GetLightdInfo", b""))

    def fetch_fee_rate(self, target_blocks: int = 6) -> float:  # noqa: ARG002
        """Zcash fees are ZIP-317 action-based, not rate-based; 0 = no rate."""
        return 0.0

    def sweep_send_amount(
        self,
        total: int,
        n_inputs: int,
        fee_rate: float,  # noqa: ARG002 (ZIP-317 ignores it)
    ) -> tuple[int, int]:
        return sweep_amount_zip317(total, n_inputs)

    def build_and_verify_send(
        self,
        *,
        recipient: str,
        amount: int,
        now: int,  # noqa: ARG002 (uniform build_and_verify_* signature)
        mnemonic: str,
        scanned_utxos: list[Utxo],
        fee_rate: float,  # noqa: ARG002 (ZIP-317 ignores it)
        change_address: str,
        max_fee: int,
        sweep: bool = False,
    ) -> Prepared:
        """Build + gate a plain transparent send (no memo) to ``recipient``."""
        if sweep:
            chosen = list(scanned_utxos)
            fee = sum(u.value for u in chosen) - amount
            change = 0
            if fee < 0:
                raise InsufficientFunds(f"amount {amount} exceeds balance")
        else:
            sel = select_coins_zip317(scanned_utxos, amount)
            chosen, fee, change = sel.utxos, sel.fee, sel.change

        outputs = [TxOut(amount, address_to_script(recipient))]
        if change > 0:
            outputs.append(TxOut(change, address_to_script(change_address)))
        tx = TxV4(
            inputs=tuple(TxIn(bytes.fromhex(u.txid)[::-1], u.vout) for u in chosen),
            outputs=tuple(outputs),
            expiry_height=self.latest_height() + EXPIRY_DELTA,
        )
        built = ZecBuilt(
            tx=tx,
            spent=[(address_to_script(u.address), u.value) for u in chosen],
            privkeys=[
                derive_p2pkh_key(
                    mnemonic, u.path or DEFAULT_DERIVATION, self.bip39_passphrase
                ).private_byte
                for u in chosen
            ],
            # Neutral extraction: decode what was actually serialized, so a
            # build bug cannot hide from the gate behind its own inputs.
            outputs=[
                TxOutput(address=script_to_address(o.script), value=o.value)
                for o in parse_v4(serialize_v4(tx)).outputs
            ],
            fee=fee,
            change_address=change_address,
            branch_id=self.branch_id(),
        )
        owned = {change_address} | {u.address for u in chosen}
        plan = SendPlan(recipient=recipient, amount=amount)
        problems = verify_btc_send(
            built.outputs,
            fee=built.fee,
            plan=plan,
            owned_addresses=owned,
            max_fee=max_fee,
        )
        return Prepared(quote=None, built=built, plan=plan, problems=problems)

    def sign(self, built: ZecBuilt) -> list[str]:
        signed = sign_transparent(
            built.tx, built.spent, built.privkeys, built.branch_id
        )
        return [serialize_v4(signed).hex()]

    def broadcast(self, raws: list[str]) -> str:
        tx_id = ""
        for raw in raws:
            data = bytes.fromhex(raw)
            resp = self._unary("SendTransaction", _delimited(1, data))
            code, message = decode_send_response(resp)
            if code != 0:
                raise RuntimeError(f"lightwalletd rejected the tx: {code} {message}")
            tx_id = compute_txid(data)
        return tx_id
