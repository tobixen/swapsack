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


@pytest.mark.network
def test_tron_build_unsigned_transfer_live():
    """Build (no broadcast) a real memo-carrying TRX transfer against the keyless
    public node, using a FRESH random account (the well-known test-vector account
    has a reassigned owner permission on mainnet and would fail signing)."""
    from cryptoswap_wallet.chains.btc import generate_mnemonic

    adapter = TronAdapter()
    fresh = generate_mnemonic()
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
