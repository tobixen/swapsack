"""Tests for the Zcash adapter (Phase 1: t-addr derivation + balance).

The derivation is money-sensitive — a wrong ``t1…`` receive address sends funds
to one the wallet cannot spend, and there is no funded-testnet path to catch
it. The golden addresses below were produced from the standard BIP39 test
mnemonic and independently cross-checked: three implementations (bitcoinlib,
eth-account+coincurve, hdwallet) agree on the compressed pubkey at
``m/44'/133'/0'/0/{0,1}``, and hdwallet independently agrees on the two-byte
base58check encoding. See docs/zcash.md.

The lightwalletd wire format (hand-rolled protobuf, like cosmos_tx) is pinned
against byte-literal golden messages assembled from the proto definitions in
zcash/librustzcash `service.proto`.
"""

import pytest

pytest.importorskip("bitcoinlib")
pytest.importorskip("grpc")

from swapsack.chains.p2pkh import p2pkh_address  # noqa: E402
from swapsack.chains.zcash import (  # noqa: E402
    ZecAdapter,
    decode_balance,
    decode_block_id_height,
    encode_address_list,
    encode_block_filter,
)

# Standard BIP39 test mnemonic -> its Zcash t-addrs at m/44'/133'/0'/0/x.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon about"
)
GOLDEN = {
    "m/44'/133'/0'/0/0": "t1XVXWCvpMgBvUaed4XDqWtgQgJSu1Ghz7F",
    "m/44'/133'/0'/0/1": "t1aQ2b1XszNVo15BguYLbQGqETBL9QZA8Jq",
}
# The pubkey behind GOLDEN[.../0/0], for the raw two-byte-prefix encoding test.
GOLDEN_PUBKEY = bytes.fromhex(
    "03db98d8f87716269ed31879aef19bdadbc869a9ea67729e36332d023b916cbcc9"
)

# The golden 0/0 address is NOT fresh on mainnet: other users of the standard
# test mnemonic really used it (two 2018-era Overwinter txs, long emptied) —
# on-chain history never disappears, which makes it a stable live target for
# the used-but-emptied path that keeps the gap-limit scan going.


def test_derive_address_matches_golden_vectors():
    a = ZecAdapter()
    for path, address in GOLDEN.items():
        assert a.derive_address(TEST_MNEMONIC, path) == address


def test_p2pkh_encoding_handles_the_two_byte_prefix():
    assert p2pkh_address(GOLDEN_PUBKEY, b"\x1c\xb8") == GOLDEN["m/44'/133'/0'/0/0"]


def test_bip39_passphrase_changes_the_address():
    plain = ZecAdapter().derive_address(TEST_MNEMONIC)
    other = ZecAdapter(bip39_passphrase="secret").derive_address(TEST_MNEMONIC)
    assert plain != other
    assert other.startswith("t1")


# --- lightwalletd wire format (hand-rolled protobuf) -------------------------


def test_encode_address_list_single_address():
    addr = GOLDEN["m/44'/133'/0'/0/0"]
    # AddressList { repeated string addresses = 1; }
    assert encode_address_list([addr]) == b"\x0a\x23" + addr.encode()


def test_decode_balance_varint():
    # Balance { int64 valueZat = 1; } — 12345 = varint b9 60
    assert decode_balance(b"\x08\xb9\x60") == 12345


def test_decode_balance_empty_message_is_zero():
    # proto3: a zero int64 is omitted from the wire entirely.
    assert decode_balance(b"") == 0


def test_encode_block_filter_nests_the_range():
    # TransparentAddressBlockFilter { string address = 1; BlockRange range = 2; }
    # BlockRange { BlockID start = 1; BlockID end = 2; }
    # BlockID { uint64 height = 1; }
    got = encode_block_filter("t1abc", start=1, end=300)
    start_msg = b"\x08\x01"
    end_msg = b"\x08\xac\x02"  # varint(300) = ac 02
    block_range = b"\x0a\x02" + start_msg + b"\x12\x03" + end_msg
    expected = b"\x0a\x05t1abc" + b"\x12\x09" + block_range
    assert got == expected


def test_decode_block_id_height():
    # BlockID { uint64 height = 1; bytes hash = 2; } — hash is ignored.
    assert decode_block_id_height(b"\x08\xac\x02\x12\x02\xab\xcd") == 300


# --- balance / scan -----------------------------------------------------------


def test_wallet_balance_scans_and_sums(monkeypatch):
    from swapsack.chains.base import AddressInfo

    a = ZecAdapter()
    funded = a.derive_address(TEST_MNEMONIC)  # 0/0

    def fake_info(address):
        if address == funded:
            return AddressInfo(has_history=True, confirmed=250000, pending=0)
        return AddressInfo(has_history=False, confirmed=0, pending=0)

    monkeypatch.setattr(a, "address_info", fake_info)
    report = a.wallet_balance(TEST_MNEMONIC)
    assert report.symbol == "ZEC"
    assert report.decimals == 8
    assert report.confirmed == 250000
    assert report.addresses == (funded,)


def test_broadcast_returns_computed_txid_on_success(monkeypatch):
    a = ZecAdapter()
    sent = {}

    def fake_unary(method, request):
        sent["method"], sent["request"] = method, request
        return b""  # SendResponse { errorCode: 0 } — proto3 omits zeros

    monkeypatch.setattr(a, "_unary", fake_unary)
    tx_id = a.broadcast([REAL_V4_TX.hex()])
    assert tx_id == REAL_TXID  # v4 txid = double-SHA256, byte-reversed
    assert sent["method"] == "SendTransaction"
    assert sent["request"].endswith(REAL_V4_TX)


def test_broadcast_raises_on_node_rejection(monkeypatch):
    from swapsack.chains.cosmos_tx import _string, _uint64

    a = ZecAdapter()
    reply = _uint64(1, 16) + _string(2, "tx-expired")  # SendResponse error
    monkeypatch.setattr(a, "_unary", lambda m, r: reply)
    with pytest.raises(RuntimeError, match="tx-expired"):
        a.broadcast([REAL_V4_TX.hex()])


def test_decode_utxos_reply_reverses_txid():
    from swapsack.chains.cosmos_tx import _delimited, _uint64
    from swapsack.chains.zcash import decode_utxos_reply

    txid_le = bytes.fromhex(REAL_TXID)[::-1]
    entry = (
        _delimited(1, txid_le)  # txid, tx-serialization order
        + _uint64(2, 1)  # index
        + _delimited(3, REAL_PREVOUT_SCRIPT)  # script
        + _uint64(4, 2393379)  # valueZat
        + _uint64(5, 3407167)  # height
    )
    reply_list = _delimited(1, entry)
    (utxo,) = decode_utxos_reply(reply_list, "t1evBud5G5F4HFUPRBpt7sz5s66PeVUYBur")
    assert utxo.txid == REAL_TXID  # display order for the rest of the wallet
    assert utxo.vout == 1
    assert utxo.value == 2393379


def test_build_and_verify_send_full_offline_loop(monkeypatch):
    # scan -> select (ZIP-317) -> build -> gate -> sign -> reparse -> verify,
    # entirely offline (tip + branch id pinned to the real-fixture values).
    from coincurve import PublicKey

    from swapsack.chains.coins import Utxo
    from swapsack.chains.zcash_tx import parse_v4 as zparse
    from swapsack.chains.zcash_tx import sighash_zip243

    a = ZecAdapter()
    monkeypatch.setattr(a, "latest_height", lambda: 3_407_167)
    monkeypatch.setattr(a, "branch_id", lambda: REAL_BRANCH_ID)
    path0, path1 = "m/44'/133'/0'/0/0", "m/44'/133'/0'/0/1"
    addr0, addr1 = GOLDEN[path0], GOLDEN[path1]
    utxos = [
        Utxo(txid="cc" * 32, vout=1, value=150_000, address=addr0, path=path0),
        Utxo(txid="dd" * 32, vout=0, value=90_000, address=addr1, path=path1),
    ]
    prepared = a.build_and_verify_send(
        recipient="t1evBud5G5F4HFUPRBpt7sz5s66PeVUYBur",
        amount=200_000,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=0.0,
        change_address=addr0,
        max_fee=50_000,
    )
    assert prepared.problems == []
    assert prepared.built.fee == 10_000  # 2-in 2-out ZIP-317 grace fee
    assert prepared.built.tx.expiry_height == 3_407_167 + 40
    # value conserved: inputs = recipient + change + fee
    assert sum(o.value for o in prepared.built.outputs) + prepared.built.fee == 240_000

    (raw_hex,) = a.sign(prepared.built)
    reparsed = zparse(bytes.fromhex(raw_hex))
    for i, (script_code, value) in enumerate(prepared.built.spent):
        s = reparsed.inputs[i].script_sig
        der, pub = s[1 : s[0]], s[s[0] + 2 :]
        digest = sighash_zip243(reparsed, i, script_code, value, REAL_BRANCH_ID)
        assert PublicKey(pub).verify(der, digest, hasher=None)


def test_build_and_verify_sweep_spends_everything(monkeypatch):
    from swapsack.chains.coins import Utxo

    a = ZecAdapter()
    monkeypatch.setattr(a, "latest_height", lambda: 3_407_167)
    monkeypatch.setattr(a, "branch_id", lambda: REAL_BRANCH_ID)
    path0 = "m/44'/133'/0'/0/0"
    addr0 = GOLDEN[path0]
    utxos = [Utxo(txid="cc" * 32, vout=1, value=150_000, address=addr0, path=path0)]
    amount, fee = a.sweep_send_amount(150_000, 1, 0.0)
    assert (amount, fee) == (140_000, 10_000)  # 1-in 1-out grace fee
    prepared = a.build_and_verify_send(
        recipient="t1evBud5G5F4HFUPRBpt7sz5s66PeVUYBur",
        amount=amount,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=0.0,
        change_address=addr0,
        max_fee=50_000,
        sweep=True,
    )
    assert prepared.problems == []
    assert len(prepared.built.outputs) == 1  # nothing left behind
    assert prepared.built.outputs[0].value + prepared.built.fee == 150_000


def test_lp_backends_is_maya_only():
    # ZEC.ZEC exists only on Maya; THORChain answers unknown-pool LP probes
    # with a 500, so `balance` must not probe it (same as DASH).
    assert ZecAdapter.lp_backends == ("maya",)


@pytest.mark.network
def test_live_lightwalletd_roundtrip():
    # Read-only guards against lightwalletd API drift, all in one connection:
    # the chain tip is past the 2026-07 height, the golden 0/0 address shows
    # history-but-no-funds (the case that must keep a scan going), and a random
    # never-used address shows neither. The random address is fresh with
    # overwhelming probability (a 160-bit collision would break Zcash itself).
    import secrets

    fresh_addr = p2pkh_address(secrets.token_bytes(33), b"\x1c\xb8")
    with ZecAdapter() as a:
        assert a.latest_height() > 3_400_000
        used = a.address_info(GOLDEN["m/44'/133'/0'/0/0"])
        fresh = a.address_info(fresh_addr)
        # Phase-2 read-only surface: a real branch id, and an emptied address
        # has no UTXOs (its history notwithstanding).
        assert a.branch_id() > 0
        assert a.fetch_utxos(GOLDEN["m/44'/133'/0'/0/0"]) == []
    assert used.has_history
    assert used.confirmed == 0
    assert not fresh.has_history
    assert fresh.confirmed == 0


# --- Phase 2: v4 transparent tx build / ZIP-243 sighash / sign ---------------
#
# The anchor below is a REAL mainnet transaction (txid 0af3caa3…2d6c78, block
# ~3407167, 2026-07-10, fetched via lightwalletd) plus the scriptPubKey/value
# of the output it spends, and the consensus branch id lightwalletd reported
# at that time (0x5437f330). Verifying the tx's own embedded ECDSA signature
# against OUR ZIP-243 digest proves the sighash implementation matches what
# real Zcash wallets sign — not merely our own reading of the spec.

REAL_V4_TX = bytes.fromhex(
    "0400008085202f8901505a941c5934a1ddf5337fc11e8ab0df72ea258c091343d873"
    "92dc18c4035386010000006b483045022100f64ddb84275501a6f517d5dac338ffed"
    "e986fb5e8ce8f2d88aef2ad5802da70002203c3d590766cf485a8dea7374a4aa1bc2"
    "6c85f08c130c6abc8eb75a0c3738609a01210294e95e927508f6a6cde79a068358b9"
    "0b10a1c6bf9d3a61f0e711c945efa3bd59ffffffff0240420f00000000001976a914"
    "e6d7c5deebc177fafd724e65bd1fa2c826245aef88ace4131500000000001976a914"
    "3baf2c65ae0c9171d40b988df1459ebee092224b88ac000000000000000000000000"
    "00000000000000"
)
REAL_PREVOUT_SCRIPT = bytes.fromhex(
    "76a9143baf2c65ae0c9171d40b988df1459ebee092224b88ac"
)
REAL_PREVOUT_VALUE = 2393379
REAL_BRANCH_ID = 0x5437F330
REAL_TXID = "0af3caa3893272b4f0f6365508fbde4012d76359784fc42248d8944c352d6c78"


def test_real_mainnet_tx_roundtrips_byte_identically():
    from swapsack.chains.zcash_tx import parse_v4, serialize_v4, txid

    tx = parse_v4(REAL_V4_TX)
    assert serialize_v4(tx) == REAL_V4_TX
    assert txid(REAL_V4_TX) == REAL_TXID
    assert len(tx.inputs) == 1 and len(tx.outputs) == 2


def test_zip243_sighash_verifies_a_real_mainnet_signature():
    from coincurve import PublicKey

    from swapsack.chains.zcash_tx import parse_v4, sighash_zip243

    tx = parse_v4(REAL_V4_TX)
    script_sig = tx.inputs[0].script_sig
    sig_len = script_sig[0]
    der, hashtype = script_sig[1:sig_len], script_sig[sig_len]
    pubkey = script_sig[sig_len + 2 :]
    assert hashtype == 0x01  # SIGHASH_ALL
    digest = sighash_zip243(
        tx, 0, REAL_PREVOUT_SCRIPT, REAL_PREVOUT_VALUE, REAL_BRANCH_ID
    )
    assert PublicKey(pubkey).verify(der, digest, hasher=None)
    # The digest is branch-bound: a wrong/stale branch id must NOT verify.
    wrong = sighash_zip243(tx, 0, REAL_PREVOUT_SCRIPT, REAL_PREVOUT_VALUE, 0xC2D6D0B4)
    assert not PublicKey(pubkey).verify(der, wrong, hasher=None)


def test_address_script_roundtrip_and_prefix_check():
    from swapsack.chains.zcash_tx import (
        ZcashTxError,
        address_to_script,
        script_to_address,
    )

    addr = GOLDEN["m/44'/133'/0'/0/0"]
    script = address_to_script(addr)
    assert script[:3] == b"\x76\xa9\x14" and len(script) == 25
    assert script_to_address(script) == addr
    # The real tx's first output pays the t1evBud… address seen on-chain.
    assert (
        script_to_address(
            bytes.fromhex("76a914e6d7c5deebc177fafd724e65bd1fa2c826245aef88ac")
        )
        == "t1evBud5G5F4HFUPRBpt7sz5s66PeVUYBur"
    )
    with pytest.raises(ZcashTxError):
        address_to_script("XoJA8qE3N2Y3jMLEtZ3vcN42qseZ8LvFf5")  # a DASH address


def test_build_sign_own_tx_and_verify_roundtrip():
    # Build a 2-in 2-out spend from the test mnemonic's own addresses, sign it,
    # re-parse the raw bytes and check both signatures against freshly computed
    # digests — the full offline loop our send path performs.
    from coincurve import PublicKey

    from swapsack.chains.p2pkh import derive_p2pkh_key
    from swapsack.chains.zcash_tx import (
        TxIn,
        TxOut,
        TxV4,
        address_to_script,
        parse_v4,
        serialize_v4,
        sighash_zip243,
        sign_transparent,
    )

    paths = ["m/44'/133'/0'/0/0", "m/44'/133'/0'/0/1"]
    keys = [derive_p2pkh_key(TEST_MNEMONIC, p) for p in paths]
    scripts = [address_to_script(GOLDEN[p]) for p in paths]
    tx = TxV4(
        inputs=(TxIn(b"\xaa" * 32, 1), TxIn(b"\xbb" * 32, 0)),
        outputs=(
            TxOut(150_000, address_to_script("t1evBud5G5F4HFUPRBpt7sz5s66PeVUYBur")),
            TxOut(139_000, scripts[0]),  # change back to us
        ),
        expiry_height=3_500_000,
    )
    spent = [(scripts[0], 200_000), (scripts[1], 100_000)]
    signed = sign_transparent(tx, spent, [k.private_byte for k in keys], REAL_BRANCH_ID)
    reparsed = parse_v4(serialize_v4(signed))
    for i, (script_code, value) in enumerate(spent):
        s = reparsed.inputs[i].script_sig
        der, pub = s[1 : s[0]], s[s[0] + 2 :]
        digest = sighash_zip243(reparsed, i, script_code, value, REAL_BRANCH_ID)
        assert PublicKey(pub).verify(der, digest, hasher=None)


def test_sign_refuses_non_p2pkh_input():
    from swapsack.chains.p2pkh import derive_p2pkh_key
    from swapsack.chains.zcash_tx import (
        TxIn,
        TxOut,
        TxV4,
        ZcashTxError,
        sign_transparent,
    )

    key = derive_p2pkh_key(TEST_MNEMONIC, "m/44'/133'/0'/0/0")
    tx = TxV4(inputs=(TxIn(b"\xaa" * 32, 0),), outputs=(TxOut(1000, b"\x51"),))
    with pytest.raises(ZcashTxError, match="non-P2PKH"):
        sign_transparent(tx, [(b"\x51", 2000)], [key.private_byte], REAL_BRANCH_ID)


# --- Phase 3: swap-from / LP deposits (vault + OP_RETURN memo) ----------------


def test_built_deposit_carries_memo_and_signs(monkeypatch):
    # The swap-from/LP shape: vault output + OP_RETURN memo + change, priced
    # with the memo's ZIP-317 actions, gated by verify_btc_swap, signed and
    # re-verified from the serialized bytes.
    from coincurve import PublicKey

    from swapsack.chains.coins import Utxo
    from swapsack.chains.zcash_tx import parse_v4 as zparse
    from swapsack.chains.zcash_tx import sighash_zip243

    a = ZecAdapter()
    monkeypatch.setattr(a, "latest_height", lambda: 3_407_167)
    monkeypatch.setattr(a, "branch_id", lambda: REAL_BRANCH_ID)
    path0 = "m/44'/133'/0'/0/0"
    addr0 = GOLDEN[path0]
    utxos = [Utxo(txid="cc" * 32, vout=1, value=500_000, address=addr0, path=path0)]
    memo = "=:BTC.BTC:bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    vault = "t1evBud5G5F4HFUPRBpt7sz5s66PeVUYBur"
    prepared = a.build_and_verify_deposit(
        vault=vault,
        memo=memo,
        amount=200_000,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=0.0,
        change_address=addr0,
        max_fee=50_000,
    )
    assert prepared.problems == []
    op_returns = [o for o in prepared.built.outputs if o.op_return_data is not None]
    assert [o.op_return_data for o in op_returns] == [memo.encode()]
    # 2 standard outputs (68 B) + 51-byte memo (~63 B) = 131 B -> 4 actions.
    assert prepared.built.fee == 20_000

    (raw_hex,) = a.sign(prepared.built)
    assert memo.encode().hex() in raw_hex  # the raw v4 tx carries the memo
    reparsed = zparse(bytes.fromhex(raw_hex))
    s = reparsed.inputs[0].script_sig
    der, pub = s[1 : s[0]], s[s[0] + 2 :]
    digest = sighash_zip243(
        reparsed, 0, prepared.built.spent[0][0], 500_000, REAL_BRANCH_ID
    )
    assert PublicKey(pub).verify(der, digest, hasher=None)
