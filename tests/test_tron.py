"""Tests for TRON address derivation and base58check encoding."""

import os
import time

import pytest

pytest.importorskip("eth_account")

from swapsack.chains.tron import TronAdapter, base58check_encode  # noqa: E402

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def test_base58check_against_known_tron_address():
    # Canonical hex <-> base58 mapping: the TRON USDT (TRC-20) contract address.
    payload = bytes.fromhex("41a614f803b6fd780986a42c78ec9c7f77e6ded13c")
    assert base58check_encode(payload) == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def test_derive_tron_address_vector():
    addr = TronAdapter().derive_address(MNEMONIC)
    assert addr == "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"
    assert addr.startswith("T")
    assert len(addr) == 34


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def test_fetch_balance_uses_walletgetaccount_api(monkeypatch):
    """Balance must come from the standard /wallet/getaccount full-node API
    (keyless, served by many public nodes) rather than TronGrid's proprietary
    indexed /v1/accounts route."""
    adapter = TronAdapter(api_url="https://tron-rpc.publicnode.com")
    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["json"] = kwargs.get("json")
        return _FakeResponse({"balance": 1234567})

    monkeypatch.setattr(adapter, "_post", fake_post)
    assert adapter.fetch_balance("TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH") == 1234567
    assert calls["url"] == "https://tron-rpc.publicnode.com/wallet/getaccount"
    assert calls["json"] == {
        "address": "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH",
        "visible": True,
    }


def test_fetch_balance_zero_for_account_without_balance_field(monkeypatch):
    """An activated account with no TRX omits the 'balance' field; a fresh
    account returns {}. Both mean zero."""
    adapter = TronAdapter()
    monkeypatch.setattr(adapter, "_post", lambda url, **kw: _FakeResponse({}))
    assert adapter.fetch_balance("TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH") == 0


# --- TRC-20 token balance (USDT-TRON) ---


def test_trc20_fetch_token_balance_query(monkeypatch):
    adapter = TronAdapter(api_url="https://tron-rpc.publicnode.com")
    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["json"] = kwargs.get("json")
        # 0x0f4240 = 1_000_000 = 1.0 USDT (6 decimals)
        return _FakeResponse(
            {
                "result": {"result": True},
                "constant_result": [
                    "00000000000000000000000000000000000000000000000000000000000f4240"
                ],
            }
        )

    monkeypatch.setattr(adapter, "_post", fake_post)
    bal = adapter.fetch_token_balance(
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"
    )
    assert bal == 1_000_000
    assert calls["url"].endswith("/wallet/triggerconstantcontract")
    j = calls["json"]
    assert j["contract_address"] == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    assert j["function_selector"] == "balanceOf(address)"
    assert j["visible"] is True
    # The owner address, 20-byte EVM form, left-padded to 32 bytes.
    assert j["parameter"] == (
        "000000000000000000000000c8599111f29c1e1e061265b4af93ea1f274ad78a"
    )


def test_trc20_fetch_token_balance_zero_for_empty_result(monkeypatch):
    adapter = TronAdapter()
    monkeypatch.setattr(
        adapter, "_post", lambda url, **kw: _FakeResponse({"result": {"result": True}})
    )
    assert (
        adapter.fetch_token_balance(
            "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"
        )
        == 0
    )


def test_tron_token_balances_reports_usdt(monkeypatch):
    adapter = TronAdapter()
    monkeypatch.setattr(
        adapter, "fetch_token_balance", lambda contract, address: 3_000_000
    )
    reports = adapter.token_balances(MNEMONIC)
    assert [r.symbol for r in reports] == ["USDT-TRON"]
    assert reports[0].decimals == 6
    assert reports[0].confirmed == 3_000_000
    assert reports[0].format().startswith("USDT-TRON: 3.0")


# --- spending FROM Tron (1e8 -> sun, build/verify wiring) ---


def test_to_sun_converts_and_rejects_subsun():
    assert TronAdapter.to_sun(150_000_000) == 1_500_000  # 1.5 TRX
    assert TronAdapter.to_sun(100) == 1  # 1 sun
    with pytest.raises(ValueError, match="whole number of sun"):
        TronAdapter.to_sun(150)  # 1.5 sun — sub-sun precision


def _fake_built(to, amount_sun, memo, *, amount_override=None):
    from swapsack.chains.tron import BuiltTronTx

    return BuiltTronTx(
        tx=None,
        priv=None,
        contract_type="TransferContract",
        to_address=to,
        amount_sun=amount_sun if amount_override is None else amount_override,
        memo=memo,
    )


# --- TRC-20 swap source (USDT-TRON) build/verify wiring ---

USDT_TRON_ASSET = "TRON.USDT-TR7NHQJEKQXGTCI8Q8ZY4PL8OTSZGJLJ6T"
USDT_TRON_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def test_token_contract_and_decimals_lookup():
    contract, decimals = TronAdapter.token_contract_and_decimals(USDT_TRON_ASSET)
    assert contract == USDT_TRON_CONTRACT
    assert decimals == 6
    with pytest.raises(ValueError, match="supported TRON token"):
        TronAdapter.token_contract_and_decimals("TRON.NOPE-TXXXX")


def test_to_token_native_converts_and_rejects_subunit():
    # 20 USDT (6 decimals) in THORChain 1e8 units -> 20_000_000 native.
    assert TronAdapter.to_token_native(2_000_000_000, 6) == 20_000_000
    assert TronAdapter.to_token_native(100, 6) == 1  # one base unit
    with pytest.raises(ValueError, match="base unit"):
        TronAdapter.to_token_native(150, 6)  # sub-unit dust (1e8 not divisible)


def _trc20_calldata(to_base58: str, amount: int) -> str:
    from tronpy.abi import trx_abi

    return (
        "a9059cbb" + trx_abi.encode(["address", "uint256"], [to_base58, amount]).hex()
    )


def _fake_token_built(to_vault, amount, memo, *, contract=USDT_TRON_CONTRACT):
    from swapsack.chains.tron import BuiltTronTx

    return BuiltTronTx(
        tx=None,
        priv=None,
        contract_type="TriggerSmartContract",
        to_address=contract,
        amount_sun=0,
        memo=memo,
        call_data=_trc20_calldata(to_vault, amount),
    )


def _token_request_and_quote():
    from types import SimpleNamespace

    from swapsack.swap import SwapRequest

    vault = "TWhCKmPTJL8k9ugzoQStN68KcAWUSzWWas"
    dest = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    memo = f"=:b:{dest}:30344"
    quote = SimpleNamespace(inbound_address=vault, memo=memo, expiry=2000)
    request = SwapRequest(
        from_asset=USDT_TRON_ASSET,
        to_asset="BTC.BTC",
        amount=2_000_000_000,
        destination=dest,
    )
    return vault, memo, quote, request


def test_tron_token_build_and_verify_clean(monkeypatch):
    adapter = TronAdapter()
    vault, memo, quote, request = _token_request_and_quote()
    monkeypatch.setattr(
        adapter,
        "build_unsigned_trc20_transfer",
        lambda *, mnemonic, token, to, amount, memo, **kw: _fake_token_built(
            to, amount, memo
        ),
    )
    prepared = adapter.build_and_verify(
        quote=quote, request=request, now=1000, mnemonic="x"
    )
    assert prepared.problems == []
    assert prepared.safe
    assert prepared.plan.token == USDT_TRON_CONTRACT
    assert prepared.plan.inbound_address == vault
    assert prepared.plan.amount == 20_000_000  # 20 USDT, 6 decimals


def test_tron_token_gate_flags_tampered_recipient(monkeypatch):
    adapter = TronAdapter()
    _vault, memo, quote, request = _token_request_and_quote()
    # Build pays a DIFFERENT recipient than the quoted vault -> must be unsafe.
    monkeypatch.setattr(
        adapter,
        "build_unsigned_trc20_transfer",
        lambda *, mnemonic, token, to, amount, memo, **kw: _fake_token_built(
            "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH",
            amount,
            memo,  # valid, but not the vault
        ),
    )
    prepared = adapter.build_and_verify(
        quote=quote, request=request, now=1000, mnemonic="x"
    )
    assert not prepared.safe


def test_tron_build_and_verify_deposit_wiring(monkeypatch):
    adapter = TronAdapter()
    monkeypatch.setattr(
        adapter,
        "build_unsigned_transfer",
        lambda *, mnemonic, to, amount_sun, memo, **kw: _fake_built(
            to, amount_sun, memo
        ),
    )
    prepared = adapter.build_and_verify_deposit(
        vault="TVaultAddr", memo="+:TRON.TRX", amount=150_000_000, now=0, mnemonic="x"
    )
    assert prepared.problems == []
    assert prepared.plan.amount_sun == 1_500_000
    assert prepared.plan.inbound_address == "TVaultAddr"


def test_tron_deposit_gate_flags_tampered_amount(monkeypatch):
    adapter = TronAdapter()
    monkeypatch.setattr(
        adapter,
        "build_unsigned_transfer",
        lambda *, mnemonic, to, amount_sun, memo, **kw: _fake_built(
            to, amount_sun, memo, amount_override=amount_sun + 1
        ),
    )
    prepared = adapter.build_and_verify_deposit(
        vault="TVaultAddr", memo="+:TRON.TRX", amount=150_000_000, now=0, mnemonic="x"
    )
    assert not prepared.safe


# --- plain external send (no swap / memo) ---

SEND_RECIPIENT = "TUEZSdKsoDHQMeZwihtdoBiN46zxhGWYdH"


def test_tron_native_send_clean(monkeypatch):
    adapter = TronAdapter()
    monkeypatch.setattr(
        adapter,
        "build_unsigned_transfer",
        lambda *, mnemonic, to, amount_sun, memo, **kw: _fake_built(
            to, amount_sun, memo
        ),
    )
    prepared = adapter.build_and_verify_send(
        recipient=SEND_RECIPIENT, amount=100_000_000, asset="TRON.TRX", mnemonic="x"
    )
    assert prepared.problems == []
    assert prepared.plan.recipient == SEND_RECIPIENT
    assert prepared.plan.amount_sun == 1_000_000  # 1 TRX


def test_tron_native_send_gate_flags_memo(monkeypatch):
    adapter = TronAdapter()
    monkeypatch.setattr(
        adapter,
        "build_unsigned_transfer",
        lambda *, mnemonic, to, amount_sun, memo, **kw: _fake_built(
            to,
            amount_sun,
            "=:sneaky",  # a send must carry no memo
        ),
    )
    prepared = adapter.build_and_verify_send(
        recipient=SEND_RECIPIENT, amount=100_000_000, asset="TRON.TRX", mnemonic="x"
    )
    assert any("memo" in p for p in prepared.problems)


def test_tron_token_send_clean(monkeypatch):
    adapter = TronAdapter()
    monkeypatch.setattr(
        adapter,
        "build_unsigned_trc20_transfer",
        lambda *, mnemonic, token, to, amount, memo, **kw: _fake_token_built(
            to, amount, memo
        ),
    )
    prepared = adapter.build_and_verify_send(
        recipient=SEND_RECIPIENT,
        amount=2_000_000_000,
        asset=USDT_TRON_ASSET,
        mnemonic="x",
    )
    assert prepared.problems == []
    assert prepared.plan.recipient == SEND_RECIPIENT
    assert prepared.plan.amount == 20_000_000  # 20 USDT (6 dec)


def test_tron_token_send_gate_flags_tampered_recipient(monkeypatch):
    adapter = TronAdapter()
    # The built transfer pays a DIFFERENT recipient than intended.
    monkeypatch.setattr(
        adapter,
        "build_unsigned_trc20_transfer",
        lambda *, mnemonic, token, to, amount, memo, **kw: _fake_token_built(
            "TWhCKmPTJL8k9ugzoQStN68KcAWUSzWWas", amount, memo
        ),
    )
    prepared = adapter.build_and_verify_send(
        recipient=SEND_RECIPIENT,
        amount=2_000_000_000,
        asset=USDT_TRON_ASSET,
        mnemonic="x",
    )
    assert any("recipient" in p for p in prepared.problems)


def test_broadcast_translates_tronpy_error_with_headroom_hint():
    from tronpy.exceptions import ValidationError

    from swapsack.swap import BroadcastError

    class _FakeTx:
        txid = "deadbeef"

        def broadcast(self):
            raise ValidationError(
                "Contract validate error : Validate TransferContract error, "
                "balance is not sufficient."
            )

    adapter = TronAdapter()
    with pytest.raises(BroadcastError) as exc:
        adapter.broadcast([_FakeTx()])
    # The opaque node error is wrapped and given an actionable hint.
    assert "headroom" in str(exc.value).lower()


# --- Phase 2 scaffold: TRC-20 transfer (USDT-TRON deposit) mechanics ---


def test_decode_trc20_transfer_calldata():
    from swapsack.chains.tron import decode_trc20_transfer

    # transfer(TR7NH..., 1_000_000): selector + padded 20-byte addr + uint256
    calldata = (
        "a9059cbb"
        "000000000000000000000000a614f803b6fd780986a42c78ec9c7f77e6ded13c"
        + format(1_000_000, "064x")
    )
    to, amount = decode_trc20_transfer(calldata)
    assert to == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    assert amount == 1_000_000


def test_decode_trc20_transfer_rejects_wrong_selector():
    from swapsack.chains.tron import decode_trc20_transfer

    with pytest.raises(ValueError, match="selector"):
        decode_trc20_transfer("deadbeef" + "0" * 128)


@pytest.mark.network
def test_tron_build_unsigned_trc20_transfer_live_nile():
    """Build (no broadcast) a memo-carrying TRC-20 transfer on the Nile TESTNET,
    decoupled from THORChain. Exercises the Phase 2 USDT-TRON deposit mechanics
    safely: TriggerSmartContract construction, calldata encoding, the memo in the
    tx data field, and local signing. A FRESH account is used (the test-vector
    account has a reassigned owner permission that fails signing); nothing is
    broadcast."""
    from eth_account import Account

    from swapsack.chains.tron import decode_trc20_transfer

    Account.enable_unaudited_hdwallet_features()
    _, fresh = Account.create_with_mnemonic()
    token = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"  # a Nile address; not broadcast
    recipient = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    memo = f"=:TRON.USDT:{recipient}"
    with TronAdapter(api_url="https://nile.trongrid.io") as adapter:
        built = adapter.build_unsigned_trc20_transfer(
            mnemonic=fresh, token=token, to=recipient, amount=1_000_000, memo=memo
        )
        assert built.contract_type == "TriggerSmartContract"
        assert built.to_address == token  # the TriggerSmartContract targets the token
        decoded_to, decoded_amount = decode_trc20_transfer(built.call_data)
        assert decoded_to == recipient
        assert decoded_amount == 1_000_000
        assert built.memo == memo
        assert adapter.sign(built)  # local signing must succeed for a fresh account


# Env that funds the full broadcast loop below; see the test docstring.
NILE_MNEMONIC = os.environ.get("SWAPSACK_NILE_MNEMONIC")
NILE_TOKEN = os.environ.get("SWAPSACK_NILE_TOKEN")


@pytest.mark.network
@pytest.mark.skipif(
    not (NILE_MNEMONIC and NILE_TOKEN),
    reason="set SWAPSACK_NILE_MNEMONIC + _TOKEN (a funded Nile account "
    "holding the TRC-20) to run the full broadcast loop",
)
def test_tron_trc20_broadcast_and_confirm_nile():
    """FULL LOOP on the Nile TESTNET: build -> sign -> broadcast -> confirm a real
    memo-carrying TRC-20 transfer, then read the memo back on-chain. Defaults to a
    self-transfer of 1 base unit, so it only spends TRX for energy/bandwidth.

    Gated on a funded Nile account provided via env (so CI runs it with the
    SWAPSACK_NILE_* secrets, and it skips for everyone else):
      SWAPSACK_NILE_MNEMONIC   seed of a Nile account holding the token + TRX
      SWAPSACK_NILE_TOKEN      a TRC-20 contract (base58) the account holds
      SWAPSACK_NILE_RECIPIENT  optional; defaults to a self-transfer
    """
    with TronAdapter(api_url="https://nile.trongrid.io") as adapter:
        sender = adapter.derive_address(NILE_MNEMONIC)
        # `or sender`, not a .get() default: CI injects an *unset* optional secret
        # as an empty string, which would otherwise become an invalid recipient.
        recipient = os.environ.get("SWAPSACK_NILE_RECIPIENT") or sender
        memo = f"=:TRON.USDT:{recipient}"
        built = adapter.build_unsigned_trc20_transfer(
            mnemonic=NILE_MNEMONIC, token=NILE_TOKEN, to=recipient, amount=1, memo=memo
        )
        txid = adapter.broadcast(adapter.sign(built))
        assert txid

        info: dict = {}
        for _ in range(20):  # Nile blocks ~3s; wait up to ~60s for inclusion
            info = adapter.get_transaction_info(txid)
            if info.get("blockNumber"):
                break
            time.sleep(3)
        assert info.get("blockNumber"), f"tx {txid} not confirmed in time"
        # SUCCESS (or absent for a trivially-successful call), never a revert.
        assert info.get("receipt", {}).get("result", "SUCCESS") == "SUCCESS"

        # The memo must survive on-chain in the tx data field (the whole point —
        # THORChain reads the swap memo from there, TRON having no router).
        raw = adapter._post(
            f"{adapter.api_url}/wallet/gettransactionbyid", json={"value": txid}
        ).json()
        assert bytes.fromhex(raw["raw_data"]["data"]).decode() == memo


@pytest.mark.network
def test_tron_build_unsigned_transfer_live():
    """Build (no broadcast) a real memo-carrying TRX transfer against the keyless
    public node, using a FRESH random account (the well-known test-vector account
    has a reassigned owner permission on mainnet and would fail signing)."""
    from swapsack.chains.btc import generate_mnemonic

    fresh = generate_mnemonic()
    with TronAdapter() as adapter:
        built = adapter.build_unsigned_transfer(
            mnemonic=fresh,
            to="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            amount_sun=1_000_000,
            memo="+:TRON.TRX",
        )
        assert built.contract_type == "TransferContract"
        assert built.to_address == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        assert built.amount_sun == 1_000_000
        assert built.memo == "+:TRON.TRX"
        # Signing is local and must succeed for a fresh (own-permission) account.
        assert adapter.sign(built)


@pytest.mark.network
def test_tron_token_balance_live():
    """Live TRC-20 balanceOf against the keyless public node — guards the
    triggerconstantcontract call/encoding against drift. Asserts shape, not an
    exact (mutable) balance."""
    reports = TronAdapter().token_balances(MNEMONIC)
    assert [r.symbol for r in reports] == ["USDT-TRON"]
    assert reports[0].decimals == 6
    assert reports[0].confirmed >= 0
