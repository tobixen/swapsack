"""Cardano (ADA): CIP-1852 key derivation, base addresses, Koios balance scan."""

from __future__ import annotations

import dataclasses

import pytest

pytest.importorskip("bitcoinlib")

from nacl.signing import VerifyKey  # noqa: E402

from conftest import FakeSession  # noqa: E402
from swapsack.chains.base import AddressInfo  # noqa: E402
from swapsack.chains.cardano import (  # noqa: E402
    LOVELACE_TO_UNITS,
    AdaAdapter,
    parse_address_info,
)
from swapsack.chains.cardano_keys import base_address, derive, root_key  # noqa: E402

# Standard BIP39 test mnemonic. Golden values from pycardano 0.19.2 (generated
# offline, never imported here); the m/.../0/0 base address is also the one
# Yoroi/Eternl show for this mnemonic.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon about"
)
ROOT_XPRV = (
    "60ce7dbec3616e9fc17e0c32578b3f380337b1b61a1f3cb9651aee30670e6f53"
    "970419a23a2e4e4082d12bf78faa8645dfc882cee2ae7179e2b07fe88098abb2"
)
ROOT_CHAIN_CODE = "072310084784c7308182dbbdb1449b2706586f1ff5cbf13d15e9b6e78c15f067"
GOLDEN = {
    "m/1852'/1815'/0'/0/0": (
        "7ea09a34aebb13c9841c71397b1cabfec5ddf950405293dee496cac2f437480a",
        "addr1qy8ac7qqy0vtulyl7wntmsxc6wex80gvcyjy33qffrhm7sh927ysx5sftuw0dlft"
        "05dz3c7revpf7jx0xnlcjz3g69mq4afdhv",
    ),
    "m/1852'/1815'/0'/0/1": (
        "86e24e36b7f0d1ad2afa266179207eb04f02642898bb58a2b6f80e491b658060",
        "addr1qyz85693g4fr8c55mfyxhae8j2u04pydxrgqr73vmwpx3a8927ysx5sftuw0dlft"
        "05dz3c7revpf7jx0xnlcjz3g69mqu2c0f9",
    ),
    "m/1852'/1815'/0'/1/0": (
        "03bc23b895900a001548d2f6ce9da1dd9a43ae15f4af9ee85a5e937b291b39c0",
        "addr1qykhadtnvjpkxh76xgr0mu4huc9tg800x2sxsqemn9uz8jh927ysx5sftuw0dlft"
        "05dz3c7revpf7jx0xnlcjz3g69mq28kufu",
    ),
}
STAKE_PUB = "012f5dc3115b8a07981e6e50f5a671e2c6fbb26c3ffde1cd1dcaf40a7fe8f160"
# pycardano's ExtendedSigningKey.sign(b"swapsack") with the m/.../0/0 key.
GOLDEN_SIGNATURE = (
    "895e1d8c01276fb8261977bc4adadb8647ddc7098bdbbd95f8ade7a953976ac4"
    "fd1e6b442a21ca8a5edb088536f3f6225da1260d915fa70015d6a6c868e8a30a"
)
# With BIP39 passphrase "TREZOR" (Icarus feeds it to PBKDF2 as the password).
GOLDEN_TREZOR_0 = (
    "addr1qx3mdledzm0zs4d05yc7lntytr7lzhqacrdcewlezgdnwf7ye0j3j44kfknjyy7hz4f"
    "kscee0m6e3ng202hvn3k2rk8q2p4dcs"
)


def test_root_key_matches_icarus_golden_vector():
    root = root_key(TEST_MNEMONIC)
    assert (root.kl + root.kr).hex() == ROOT_XPRV
    assert root.chain_code.hex() == ROOT_CHAIN_CODE


@pytest.mark.parametrize("path", sorted(GOLDEN))
def test_derived_public_keys_match_golden(path):
    assert derive(root_key(TEST_MNEMONIC), path).public_key.hex() == GOLDEN[path][0]


def test_stake_key_matches_golden():
    stake = derive(root_key(TEST_MNEMONIC), "m/1852'/1815'/0'/2/0")
    assert stake.public_key.hex() == STAKE_PUB


@pytest.mark.parametrize("path", sorted(GOLDEN))
def test_adapter_derives_golden_base_addresses(path):
    assert AdaAdapter().derive_address(TEST_MNEMONIC, path) == GOLDEN[path][1]


def test_base_address_from_raw_keys():
    payment = bytes.fromhex(GOLDEN["m/1852'/1815'/0'/0/0"][0])
    assert (
        base_address(payment, bytes.fromhex(STAKE_PUB))
        == (GOLDEN["m/1852'/1815'/0'/0/0"][1])
    )


def test_bip39_passphrase_changes_the_address():
    adapter = AdaAdapter(bip39_passphrase="TREZOR")
    assert adapter.derive_address(TEST_MNEMONIC) == GOLDEN_TREZOR_0


def test_extended_key_signature_matches_golden_and_verifies():
    key = derive(root_key(TEST_MNEMONIC), "m/1852'/1815'/0'/0/0")
    signature = key.sign(b"swapsack")
    assert signature.hex() == GOLDEN_SIGNATURE
    # A plain Ed25519 verifier accepts it: what a Cardano node checks.
    VerifyKey(key.public_key).verify(b"swapsack", signature)


def test_path_must_start_at_the_root():
    with pytest.raises(ValueError, match="path"):
        derive(root_key(TEST_MNEMONIC), "1852'/1815'")


class FakeJson:
    """A Koios answer: a 200 carrying ``body``."""

    status_code = 200

    def __init__(self, body: object) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self._body


# --- Koios address_info parsing (units are 1e-8 ADA, Maya's) ----------------


def test_lovelace_scale_to_maya_units():
    assert LOVELACE_TO_UNITS == 100


def test_parse_address_info_absent_address_has_no_history():
    assert parse_address_info([]) == AddressInfo(
        has_history=False, confirmed=0, pending=0
    )


def test_parse_address_info_used_but_empty_counts_as_history():
    row = {"address": "addr1x", "balance": "0", "utxo_set": []}
    assert parse_address_info([row]) == AddressInfo(
        has_history=True, confirmed=0, pending=0
    )


def test_parse_address_info_scales_lovelace_to_maya_units():
    row = {"address": "addr1x", "balance": "2457959347", "utxo_set": []}
    assert parse_address_info([row]).confirmed == 2457959347 * 100


def test_wallet_balance_scans_and_sums(monkeypatch):
    adapter = AdaAdapter()
    funded = {GOLDEN["m/1852'/1815'/0'/0/0"][1]: 5_000_000 * 100}

    def fake_info(address):
        if address in funded:
            return AddressInfo(has_history=True, confirmed=funded[address], pending=0)
        return AddressInfo(has_history=False, confirmed=0, pending=0)

    monkeypatch.setattr(adapter, "address_info", fake_info)
    report = adapter.wallet_balance(TEST_MNEMONIC)
    assert report.symbol == "ADA"
    assert report.decimals == 8
    assert report.confirmed == 5_000_000 * 100
    assert report.addresses == (GOLDEN["m/1852'/1815'/0'/0/0"][1],)


def test_address_info_posts_to_koios():
    adapter = AdaAdapter("https://koios.example.com/api/v1")
    session = FakeSession(
        FakeJson([{"address": "addr1x", "balance": "7", "utxo_set": []}])
    )
    adapter._session = session  # type: ignore[assignment]
    assert adapter.address_info("addr1x").confirmed == 700
    assert session.posts == ["https://koios.example.com/api/v1/address_info"]
    assert session.kwargs == [{"json": {"_addresses": ["addr1x"]}}]


@pytest.mark.network
def test_live_koios_sees_golden_address_history():
    with AdaAdapter() as adapter:
        assert adapter.address_info(GOLDEN["m/1852'/1815'/0'/0/0"][1]).has_history


# --- send / sweep / deposit ----------------------------------------------------

from swapsack.chains.cardano_tx import (  # noqa: E402
    ProtocolParams,
    min_fee,
    parse,
)
from swapsack.chains.coins import InsufficientFunds, Utxo  # noqa: E402
from swapsack.swap import SwapAborted  # noqa: E402

PARAMS = ProtocolParams(min_fee_a=44, min_fee_b=155381, coins_per_utxo_byte=4310)
VAULT = "addr1vyqnx906342cukj8ukfvps5vr24emnam7nrz0hr9cjc807say5nlw"
RECIPIENT = GOLDEN["m/1852'/1815'/0'/0/1"][1]
OURS = GOLDEN["m/1852'/1815'/0'/0/0"][1]
CHANGE = GOLDEN["m/1852'/1815'/0'/1/0"][1]
ADA = 100_000_000  # one ADA in the adapter's 1e-8 units


def _utxo(ada: float, index: int = 0, path: str = "m/1852'/1815'/0'/0/0") -> Utxo:
    return Utxo(
        txid=f"{index:064x}",
        vout=index,
        value=int(ada * ADA),
        address=OURS if path.endswith("/0/0") else CHANGE,
        path=path,
    )


@pytest.fixture
def adapter(monkeypatch):
    a = AdaAdapter("https://koios.example.com/api/v1")
    monkeypatch.setattr(a, "protocol_params", lambda: PARAMS)
    monkeypatch.setattr(a, "tip_slot", lambda: 200_000_000)
    return a


def _build(adapter, utxos, amount, *, memo=None, sweep=False, to=RECIPIENT):
    return adapter.build_unsigned_swap(
        mnemonic=TEST_MNEMONIC,
        utxos=utxos,
        vault_address=to,
        amount=amount,
        memo=memo,
        change_address=CHANGE,
        sweep=sweep,
    )


def test_fetch_utxos_scales_and_skips_token_bearing_outputs(monkeypatch):
    adapter = AdaAdapter("https://koios.example.com/api/v1")
    plain = {"tx_hash": "aa" * 32, "tx_index": 1, "value": "3000000", "asset_list": []}
    tokens = {
        "tx_hash": "bb" * 32,
        "tx_index": 0,
        "value": "1500000",
        "asset_list": [{"policy_id": "cc" * 28, "quantity": "1"}],
    }
    adapter._session = FakeSession(  # type: ignore[assignment]
        FakeJson([{"address": OURS, "balance": "4500000", "utxo_set": [plain, tokens]}])
    )
    [utxo] = adapter.fetch_utxos(OURS)
    assert (utxo.txid, utxo.vout, utxo.value, utxo.address) == (
        "aa" * 32,
        1,
        3_000_000 * 100,
        OURS,
    )


def test_send_pays_recipient_returns_change_and_pays_the_minimum_fee(adapter):
    built = _build(adapter, [_utxo(10)], 3 * ADA)
    raw = bytes.fromhex(adapter.sign(built)[0])
    parsed = parse(raw)
    assert parsed.outputs[0] == (RECIPIENT, 3_000_000)
    assert parsed.outputs[1][0] == CHANGE
    assert parsed.fee == min_fee(len(raw), PARAMS)
    assert built.fee == parsed.fee * 100
    assert parsed.outputs[1][1] == 10_000_000 - 3_000_000 - parsed.fee
    [(vkey, signature)] = parsed.witnesses
    VerifyKey(vkey).verify(parsed.txid, signature)


def test_neutral_outputs_are_read_back_from_the_serialized_bytes(adapter):
    built = _build(adapter, [_utxo(10)], 3 * ADA)
    assert built.outputs[0].address == RECIPIENT
    assert built.outputs[0].value == 3 * ADA
    assert built.outputs[1].address == CHANGE
    assert all(o.op_return_data is None for o in built.outputs)


def test_send_passes_the_verify_gate(adapter):
    prepared = adapter.build_and_verify_send(
        recipient=RECIPIENT,
        amount=3 * ADA,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=[_utxo(10)],
        fee_rate=0.0,
        change_address=CHANGE,
        max_fee=adapter.default_max_fee,
    )
    assert prepared.problems == []


def test_change_below_min_ada_is_folded_into_the_fee(adapter):
    # 3.5 ADA in, 3 ADA out: ~0.33 ADA left after the fee, under the ~0.97 ADA
    # an output must hold, so it cannot be returned and goes to the fee.
    built = _build(adapter, [_utxo(3.5)], 3 * ADA)
    parsed = parse(bytes.fromhex(adapter.sign(built)[0]))
    assert parsed.outputs == ((RECIPIENT, 3_000_000),)
    assert parsed.fee == 500_000


def test_more_inputs_are_taken_rather_than_folding_change(adapter):
    built = _build(adapter, [_utxo(3.5, 0), _utxo(5, 1)], 3 * ADA)
    parsed = parse(bytes.fromhex(adapter.sign(built)[0]))
    assert len(parsed.outputs) == 2  # change kept: 5 ADA covered it alone
    assert len(parsed.inputs) == 1


def test_amount_finer_than_a_lovelace_is_refused(adapter):
    with pytest.raises(SwapAborted, match="lovelace"):
        _build(adapter, [_utxo(10)], 3 * ADA + 1)


def test_output_below_min_ada_is_refused(adapter):
    with pytest.raises(SwapAborted, match="at least"):
        _build(adapter, [_utxo(10)], ADA // 2)


def test_insufficient_funds(adapter):
    with pytest.raises(InsufficientFunds):
        _build(adapter, [_utxo(2)], 3 * ADA)


def test_inputs_on_several_addresses_get_one_witness_each(adapter):
    utxos = [_utxo(2, 0), _utxo(2, 1, path="m/1852'/1815'/0'/1/0")]
    built = _build(adapter, utxos, 3 * ADA)
    parsed = parse(bytes.fromhex(adapter.sign(built)[0]))
    assert len(parsed.inputs) == 2
    assert len(parsed.witnesses) == 2
    for vkey, signature in parsed.witnesses:
        VerifyKey(vkey).verify(parsed.txid, signature)


def test_a_utxo_whose_path_does_not_own_its_address_is_refused(adapter):
    wrong = dataclasses.replace(_utxo(10), path="m/1852'/1815'/0'/0/7")
    with pytest.raises(SwapAborted, match="payment key"):
        _build(adapter, [wrong], 3 * ADA)


def test_sweep_spends_everything_into_one_output(adapter):
    utxos = [_utxo(2, 0), _utxo(3, 1)]
    amount, fee = adapter.sweep_send_amount(5 * ADA, len(utxos), 0.0)
    assert amount + fee == 5 * ADA
    built = _build(adapter, utxos, amount, sweep=True)
    raw = bytes.fromhex(adapter.sign(built)[0])
    parsed = parse(raw)
    assert parsed.outputs == ((RECIPIENT, amount // 100),)
    # Sized for the worst case before the recipient was known; never short.
    assert min_fee(len(raw), PARAMS) <= parsed.fee <= min_fee(len(raw), PARAMS) + 5000


def test_sweep_that_cannot_pay_its_fee_is_refused(adapter):
    with pytest.raises(InsufficientFunds):
        _build(adapter, [_utxo(2)], 2 * ADA, sweep=True)


def test_deposit_carries_the_memo_as_cip20_metadata(adapter):
    memo = "=:ETH.ETH:0x" + "ab" * 20 + ":0/1/0"
    built = _build(adapter, [_utxo(10)], 5 * ADA, memo=memo, to=VAULT)
    parsed = parse(bytes.fromhex(adapter.sign(built)[0]))
    assert parsed.memo == memo.encode()
    assert parsed.outputs[0] == (VAULT, 5_000_000)
    assert [o.op_return_data for o in built.outputs if o.address is None] == [
        memo.encode()
    ]


def test_a_memo_longer_than_an_op_return_passes_the_ada_gate(adapter):
    # Cardano metadata has no 80-byte cap; Maya takes far longer memos.
    memo = "=:BTC.BTC:bc1p" + "q" * 100 + ":0/1/0"
    prepared = adapter.build_and_verify_deposit(
        vault=VAULT,
        memo=memo,
        amount=5 * ADA,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=[_utxo(10)],
        fee_rate=0.0,
        change_address=CHANGE,
        max_fee=adapter.default_max_fee,
    )
    assert prepared.problems == []


def test_protocol_params_come_from_the_current_epoch(monkeypatch):
    adapter = AdaAdapter("https://koios.example.com/api/v1")
    session = FakeSession(
        FakeJson([{"epoch_no": 663, "abs_slot": 201_219_907}]),
        FakeJson(
            [
                {
                    "epoch_no": 663,
                    "min_fee_a": 44,
                    "min_fee_b": 155381,
                    "coins_per_utxo_size": "4310",
                    "max_tx_size": 16384,
                }
            ]
        ),
    )
    adapter._session = session  # type: ignore[assignment]
    assert adapter.protocol_params() == PARAMS
    assert session.gets == [
        "https://koios.example.com/api/v1/tip",
        "https://koios.example.com/api/v1/epoch_params",
    ]
    assert session.kwargs[1] == {"params": {"_epoch_no": 663}}


def test_broadcast_submits_the_cbor_and_returns_its_txid(adapter):
    built = _build(adapter, [_utxo(10)], 3 * ADA)
    [raw] = adapter.sign(built)
    txid = parse(bytes.fromhex(raw)).txid.hex()
    session = FakeSession(FakeSubmit(202, f'"{txid}"'))
    adapter._session = session  # type: ignore[assignment]
    assert adapter.broadcast([raw]) == txid
    assert session.posts == ["https://koios.example.com/api/v1/submittx"]
    assert session.kwargs == [
        {
            "data": bytes.fromhex(raw),
            "headers": {"Content-Type": "application/cbor"},
        }
    ]


# --- review fixes ----------------------------------------------------------------


def _deposit(adapter, utxos, *, memo="+:ADA.ADA:maya1xyz", amount=5 * ADA):
    return adapter.build_and_verify_deposit(
        vault=VAULT,
        memo=memo,
        amount=amount,
        now=0,
        mnemonic=TEST_MNEMONIC,
        scanned_utxos=utxos,
        fee_rate=0.0,
        change_address=CHANGE,
        max_fee=adapter.default_max_fee,
    )


def test_lp_deposit_spends_from_and_returns_change_to_the_receive_address(adapter):
    # Maya files the position under (and authorises its withdraw from) the
    # first input's address, and inputs are sorted by txid on the wire: only
    # one address may fund an LP deposit, or add and withdraw can diverge.
    utxos = [_utxo(50, 0, path="m/1852'/1815'/0'/1/0"), _utxo(10, 1)]
    prepared = _deposit(adapter, utxos)
    assert prepared.problems == []
    assert {u.address for u in prepared.built.inputs} == {OURS}
    assert [o.address for o in prepared.built.outputs if o.address] == [VAULT, OURS]


def test_lp_deposit_with_nothing_on_the_receive_address_is_refused(adapter):
    with pytest.raises(InsufficientFunds, match=OURS):
        _deposit(adapter, [_utxo(50, 0, path="m/1852'/1815'/0'/1/0")])


def test_folded_change_is_named_on_the_fee(adapter):
    assert _build(adapter, [_utxo(10)], 3 * ADA).fee_note == ""
    note = _build(adapter, [_utxo(3.5)], 3 * ADA).fee_note
    assert "change" in note and "min" in note


def test_the_gate_fee_is_read_from_the_serialized_body(adapter):
    built = _build(adapter, [_utxo(3.5)], 3 * ADA)
    raw = bytes.fromhex(adapter.sign(built)[0])
    assert built.fee == parse(raw).fee * 100


def test_sign_refuses_when_the_outputs_differ_from_what_the_gate_passed(adapter):
    built = _build(adapter, [_utxo(10)], 3 * ADA)
    built.outputs[0] = dataclasses.replace(built.outputs[0], value=1)
    with pytest.raises(RuntimeError, match="read-back"):
        adapter.sign(built)


def test_an_ada_memo_over_maya_s_cap_is_refused_by_the_gate(adapter):
    prepared = _deposit(adapter, [_utxo(10)], memo="x" * 251)
    assert any("memo limit of 250" in p for p in prepared.problems)


def test_op_return_chains_keep_the_80_byte_cap_and_old_max_fee():
    from swapsack.chains.btc import BtcAdapter
    from swapsack.chains.dash import DashAdapter
    from swapsack.chains.zcash import ZecAdapter

    for cls in (BtcAdapter, DashAdapter, ZecAdapter):
        assert (cls.memo_max_bytes, cls.default_max_fee) == (80, 50_000)


def test_fetch_utxos_skips_outputs_carrying_a_reference_script():
    # Conway charges per byte of reference script in the inputs; the fee
    # maths here does not, so such an output would make the tx underpay.
    adapter = AdaAdapter("https://koios.example.com/api/v1")
    script = {
        "tx_hash": "aa" * 32,
        "tx_index": 0,
        "value": "3000000",
        "asset_list": [],
        "reference_script": {"hash": "cc" * 28},
    }
    adapter._session = FakeSession(  # type: ignore[assignment]
        FakeJson([{"address": OURS, "balance": "3000000", "utxo_set": [script]}])
    )
    assert adapter.fetch_utxos(OURS) == []


def test_a_tx_over_the_max_size_is_refused(adapter, monkeypatch):
    small = dataclasses.replace(PARAMS, max_tx_size=200)
    monkeypatch.setattr(adapter, "protocol_params", lambda: small)
    with pytest.raises(SwapAborted, match="bytes"):
        _build(adapter, [_utxo(10)], 3 * ADA)


def test_protocol_params_fall_back_to_the_last_epoch_koios_has():
    # Right after an epoch boundary Koios can know the tip's epoch before it
    # has that epoch's parameters row.
    adapter = AdaAdapter("https://koios.example.com/api/v1")
    row = {
        "epoch_no": 663,
        "min_fee_a": 44,
        "min_fee_b": 155381,
        "coins_per_utxo_size": "4310",
        "max_tx_size": 16384,
    }
    session = FakeSession(
        FakeJson([{"epoch_no": 664, "abs_slot": 1}]), FakeJson([]), FakeJson([row])
    )
    adapter._session = session  # type: ignore[assignment]
    assert adapter.protocol_params() == PARAMS
    assert [k.get("params") for k in session.kwargs[1:]] == [
        {"_epoch_no": 664},
        {"_epoch_no": 663},
    ]


class FakeSubmit(FakeJson):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(None)
        self.status_code = status
        self.text = text


def _signed(adapter):
    [raw] = adapter.sign(_build(adapter, [_utxo(10)], 3 * ADA))
    return raw, parse(bytes.fromhex(raw)).txid.hex()


def test_broadcast_accepts_a_plain_text_txid(adapter):
    raw, txid = _signed(adapter)
    adapter._session = FakeSession(FakeSubmit(202, f'"{txid}"\n'))  # type: ignore[assignment]
    assert adapter.broadcast([raw]) == txid


def test_broadcast_rejection_says_nothing_was_sent(adapter):
    raw, _ = _signed(adapter)
    adapter._session = FakeSession(FakeSubmit(400, "BadInputsUTxO"))  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="rejected.*BadInputsUTxO"):
        adapter.broadcast([raw])


def test_an_odd_answer_after_submit_warns_the_tx_may_be_live(adapter):
    raw, txid = _signed(adapter)
    adapter._session = FakeSession(FakeSubmit(202, "ok"))  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match=f"may have been broadcast.*{txid}"):
        adapter.broadcast([raw])
