"""Tests for TRON address derivation and base58check encoding."""

import pytest

pytest.importorskip("eth_account")

from cryptoswap_wallet.chains.tron import TronAdapter, base58check_encode  # noqa: E402

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
    from cryptoswap_wallet.chains.tron import BuiltTronTx

    return BuiltTronTx(
        tx=None,
        priv=None,
        contract_type="TransferContract",
        to_address=to,
        amount_sun=amount_sun if amount_override is None else amount_override,
        memo=memo,
    )


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


def test_broadcast_translates_tronpy_error_with_headroom_hint():
    from tronpy.exceptions import ValidationError

    from cryptoswap_wallet.swap import BroadcastError

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
    from cryptoswap_wallet.chains.tron import decode_trc20_transfer

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
    from cryptoswap_wallet.chains.tron import decode_trc20_transfer

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

    from cryptoswap_wallet.chains.tron import decode_trc20_transfer

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


@pytest.mark.network
def test_tron_build_unsigned_transfer_live():
    """Build (no broadcast) a real memo-carrying TRX transfer against the keyless
    public node, using a FRESH random account (the well-known test-vector account
    has a reassigned owner permission on mainnet and would fail signing)."""
    from cryptoswap_wallet.chains.btc import generate_mnemonic

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
