"""Tests for ETH address derivation (destination for BTC->ETH swaps)."""

import pytest

pytest.importorskip("bitcoinlib")

from cryptoswap_wallet.chains.eth import EthAdapter, to_checksum_address  # noqa: E402

MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def test_derive_eth_address_matches_vector():
    # m/44'/60'/0'/0/0 for the canonical test mnemonic
    assert EthAdapter().derive_address(MNEMONIC) == (
        "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    )


def test_eip55_checksum():
    raw = bytes.fromhex("5aaeb6053f3e94c9b9a09f33669435e7ef1beaed")
    assert to_checksum_address(raw) == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


VAULT = "0x85034887f6656d610c38ef1710208495791fb146"
BTC_MEMO = "=:BTC.BTC:bc1qexampledest:123"


def _build(nonce=0):
    return EthAdapter().build_unsigned_swap(
        mnemonic=MNEMONIC,
        vault_address=VAULT,
        amount=100000,  # 1e8 units -> 1e15 wei
        memo=BTC_MEMO,
        nonce=nonce,
        gas=60000,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
    )


def test_build_eth_swap_tx_fields():
    built = _build(nonce=3)
    assert built.value == 100000 * 10**10
    assert built.data == "0x" + BTC_MEMO.encode().hex()
    assert built.chain_id == 1
    assert built.to.lower() == VAULT
    assert built.tx["nonce"] == 3
    assert built.fee == 60000 * 20_000_000_000


def test_eth_sign_produces_typed_raw():
    raws = EthAdapter().sign(_build())
    assert len(raws) == 1
    assert raws[0].startswith("0x02")  # EIP-1559 typed transaction


def test_eth_sweep_amount_leaves_gas_reserve():
    from cryptoswap_wallet.chains.eth import eth_sweep_amount

    amount = eth_sweep_amount(10**18, gas=60000, max_fee_per_gas=20_000_000_000)
    expected = (10**18 - 60000 * 20_000_000_000) // 10**10
    assert amount == expected


def test_eth_sweep_amount_insufficient():
    import pytest

    from cryptoswap_wallet.chains.coins import InsufficientFunds
    from cryptoswap_wallet.chains.eth import eth_sweep_amount

    with pytest.raises(InsufficientFunds):
        eth_sweep_amount(1000, gas=60000, max_fee_per_gas=20_000_000_000)


def test_erc20_fetch_token_balance_encodes_and_decodes(monkeypatch):
    adapter = EthAdapter()
    captured = {}

    def fake_rpc(method, params):
        captured["method"] = method
        captured["params"] = params
        return "0x" + (1_234_567).to_bytes(32, "big").hex()

    monkeypatch.setattr(adapter, "_rpc", fake_rpc)
    token = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    owner = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"
    assert adapter.fetch_token_balance(token, owner) == 1_234_567
    assert captured["method"] == "eth_call"
    call = captured["params"][0]
    assert call["to"].lower() == token.lower()
    # balanceOf(address) selector + the 20-byte owner left-padded to 32 bytes.
    assert call["data"] == "0x70a08231" + "0" * 24 + owner[2:].lower()


def test_eth_token_balances_report_tracked_tokens(monkeypatch):
    adapter = EthAdapter()
    monkeypatch.setattr(
        adapter, "fetch_token_balance", lambda token, address: 2_500_000
    )
    reports = adapter.token_balances(MNEMONIC)
    assert [r.symbol for r in reports] == ["USDT-ETH", "USDC-ETH"]
    assert all(r.decimals == 6 and r.confirmed == 2_500_000 for r in reports)
    assert reports[0].format().startswith("USDT-ETH: 2.50")
    assert reports[1].format().startswith("USDC-ETH: 2.50")


def test_eth_build_and_verify_clean():
    from cryptoswap_wallet.swap import SwapRequest
    from cryptoswap_wallet.thorchain import Quote, SwapFees

    a = EthAdapter()
    dest = "bc1qexampledest"
    quote = Quote(
        inbound_address=VAULT,
        expected_amount_out=170000,
        memo=f"=:b:{dest}",
        fees=SwapFees("BTC.BTC", 1058, 0, 500, 1558, 20, 50),
        recommended_min_amount_in=1000,
        expiry=9_999_999_999,
        dust_threshold=1000,
        recommended_gas_rate=15,
        gas_rate_units="gwei",
        router=None,
        max_streaming_quantity=1,
        streaming_swap_blocks=1,
        total_swap_seconds=30,
        raw={},
    )
    request = SwapRequest(
        from_asset="ETH.ETH", to_asset="BTC.BTC", amount=100000, destination=dest
    )
    prepared = a.build_and_verify(
        quote=quote,
        request=request,
        now=0,
        mnemonic=MNEMONIC,
        nonce=0,
        gas=60000,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**17,
    )
    assert prepared.problems == []


def _eth_token_quote(memo, *, expiry=9_999_999_999):
    from cryptoswap_wallet.thorchain import Quote, SwapFees

    return Quote(
        inbound_address="0xe3536ba9559966c357f551ceccccf38b533aa171",
        expected_amount_out=24556,
        memo=memo,
        fees=SwapFees("BTC.BTC", 1058, 0, 500, 1558, 20, 50),
        recommended_min_amount_in=1,
        expiry=expiry,
        dust_threshold=0,
        recommended_gas_rate=15,
        gas_rate_units="gwei",
        router="0xD37BbE5744D730a1d98d8DC97c42F0Ca46aD7146",
        max_streaming_quantity=1,
        streaming_swap_blocks=1,
        total_swap_seconds=30,
        raw={},
    )


USDT_ASSET = "ETH.USDT-0xdAC17F958D2ee523a2206206994597C13D831ec7"


def _build_usdt(dest="bc1qexampledest", amount=500_000_000):
    from cryptoswap_wallet.swap import SwapRequest

    request = SwapRequest(
        from_asset=USDT_ASSET, to_asset="BTC.BTC", amount=amount, destination=dest
    )
    return EthAdapter().build_token_swap(
        mnemonic=MNEMONIC,
        request=request,
        quote=_eth_token_quote(f"=:b:{dest}"),
        nonce=7,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        decimals=6,
    )


def test_eth_token_build_amounts_and_nonces():
    built = _build_usdt()
    assert built.native_amount == 5_000_000  # 5e8 thorchain units -> 5 USDT (6 dec)
    assert built.approve_tx["nonce"] == 7
    assert built.deposit_tx["nonce"] == 8
    assert built.approve_tx["to"].lower().endswith("831ec7")  # token contract
    assert built.deposit_tx["to"].lower().endswith("ad7146")  # router
    assert len(built.txs) == 2


# --- plain external send (no swap / memo / router) ---------------------------

SEND_RECIPIENT = "0x1111111111111111111111111111111111111111"
_MAX_FEE_WEI = 10**16


def _send_kwargs(**over):
    kw = dict(
        recipient=SEND_RECIPIENT,
        amount=100_000,  # 1e8 units -> 0.001 ETH
        asset="ETH.ETH",
        mnemonic=MNEMONIC,
        nonce=3,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=_MAX_FEE_WEI,
    )
    kw.update(over)
    return kw


def test_eth_native_send_clean():
    prepared = EthAdapter().build_and_verify_send(**_send_kwargs())
    assert prepared.problems == []
    built = prepared.built
    assert built.to.lower() == SEND_RECIPIENT
    assert built.value == 100_000 * 10**10  # 0.001 ETH in wei
    assert built.data == "0x"  # a plain send carries NO calldata
    assert built.tx["nonce"] == 3
    assert built.gas == 21000


def test_eth_token_send_transfers_to_recipient():
    from cryptoswap_wallet.chains.eth import TRANSFER_SELECTOR, _decode_call

    prepared = EthAdapter().build_and_verify_send(
        **_send_kwargs(asset=USDT_ASSET, amount=250_000_000)  # 2.5 USDT
    )
    assert prepared.problems == []
    built = prepared.built
    assert built.value == 0
    assert built.to.lower().endswith("831ec7")  # tx targets the token contract
    recipient, amount = _decode_call(
        built.data, TRANSFER_SELECTOR, ["address", "uint256"]
    )
    assert recipient.lower() == SEND_RECIPIENT
    assert amount == 2_500_000  # 2.5 USDT at 6 decimals


def test_eth_native_send_fee_ceiling_blocks():
    prepared = EthAdapter().build_and_verify_send(**_send_kwargs(max_fee_wei=1))
    assert any("fee" in p for p in prepared.problems)


def test_eth_chain_id_is_configurable_for_testnet():
    # A Sepolia adapter must sign for chain id 11155111, and the gate must accept
    # it (plan.chain_id follows the adapter, not a hardcoded mainnet 1).
    sepolia = EthAdapter(chain_id=11155111)
    prepared = sepolia.build_and_verify_send(**_send_kwargs())
    assert prepared.problems == []
    assert prepared.built.chain_id == 11155111
    assert prepared.built.tx["chainId"] == 11155111
    # Default stays mainnet.
    assert EthAdapter().chain_id == 1


def test_eth_token_verify_clean():
    from cryptoswap_wallet.chains.eth import verify_eth_token_swap

    built = _build_usdt()
    problems = verify_eth_token_swap(
        built=built, destination="bc1qexampledest", now=0, max_fee_wei=10**18
    )
    assert problems == []


def test_eth_token_verify_rejects_wrong_destination():
    from cryptoswap_wallet.chains.eth import verify_eth_token_swap

    built = _build_usdt()
    problems = verify_eth_token_swap(
        built=built, destination="bc1qsomeoneelse", now=0, max_fee_wei=10**18
    )
    assert any("destination" in p.lower() for p in problems)


def test_eth_token_sign_produces_two_raws():
    raws = EthAdapter().sign(_build_usdt())
    assert len(raws) == 2
    assert all(r.startswith("0x02") for r in raws)


def test_eth_token_build_from_uppercase_0x_asset():
    # ASSET uses THORChain's uppercase "0X..." contract form — must not crash (T0).
    from cryptoswap_wallet.swap import SwapRequest

    asset = "ETH.USDT-0XDAC17F958D2EE523A2206206994597C13D831EC7"
    req = SwapRequest(
        from_asset=asset, to_asset="BTC.BTC", amount=500_000_000, destination="bc1qx"
    )
    built = EthAdapter().build_token_swap(
        mnemonic=MNEMONIC,
        request=req,
        quote=_eth_token_quote("=:b:bc1qx"),
        nonce=1,
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        decimals=6,
    )
    assert built.token.lower().endswith("831ec7")


def test_eth_token_verify_rejects_wrong_amount():
    from cryptoswap_wallet.chains.eth import encode_deposit, verify_eth_token_swap

    built = _build_usdt()
    built.deposit_tx["data"] = encode_deposit(
        built.vault, built.token, built.native_amount + 1, built.memo, built.expiry
    )
    problems = verify_eth_token_swap(
        built=built, destination="bc1qexampledest", now=0, max_fee_wei=10**18
    )
    assert any("amount" in p.lower() for p in problems)


def test_eth_token_verify_rejects_swapped_vault_token():
    from cryptoswap_wallet.chains.eth import encode_deposit, verify_eth_token_swap

    built = _build_usdt()
    # vault and token slots swapped — substring checks would have missed this.
    built.deposit_tx["data"] = encode_deposit(
        built.token, built.vault, built.native_amount, built.memo, built.expiry
    )
    problems = verify_eth_token_swap(
        built=built, destination="bc1qexampledest", now=0, max_fee_wei=10**18
    )
    assert problems


# --- ERC-20 liquidity add (approve + router.depositWithExpiry, "+:POOL" memo) ---

LP_ROUTER = "0xe3985E6b61b814F7Cdb188766562ba71b446B46d"
LP_VAULT = "0x6a16f961e24e6e90bd9f950f768dc42a7f305664"


def _lp_deposit_kwargs(**over):
    kw = dict(
        vault=LP_VAULT,
        memo=f"+:{USDT_ASSET}",
        amount=2_500_000_000,  # 25 USDT in THORChain 1e8 units
        now=1000,
        mnemonic=MNEMONIC,
        nonce=4,
        gas=60000,  # ignored on the token path (uses APPROVE_GAS/TOKEN_DEPOSIT_GAS)
        max_fee_per_gas=20_000_000_000,
        max_priority_fee_per_gas=1_000_000_000,
        max_fee_wei=10**16,
        router=LP_ROUTER,
    )
    kw.update(over)
    return kw


def test_eth_token_lp_add_builds_and_verifies():
    from cryptoswap_wallet.chains.eth import DEPOSIT_SELECTOR, _decode_call

    prepared = EthAdapter().build_and_verify_deposit(**_lp_deposit_kwargs())
    assert prepared.problems == []
    built = prepared.built
    assert built.native_amount == 25_000_000  # 25 USDT (6 dec)
    assert built.approve_tx["nonce"] == 4
    assert built.deposit_tx["nonce"] == 5
    assert built.router.lower() == LP_ROUTER.lower()
    assert built.vault.lower() == LP_VAULT.lower()
    # The deposit calldata binds vault/token/amount/memo positionally.
    d_vault, d_token, d_amount, d_memo, _exp = _decode_call(
        built.deposit_tx["data"],
        DEPOSIT_SELECTOR,
        ["address", "address", "uint256", "string", "uint256"],
    )
    assert d_vault.lower() == LP_VAULT.lower()
    assert d_amount == 25_000_000
    assert d_memo == f"+:{USDT_ASSET}"


def test_eth_token_lp_add_requires_router():
    from cryptoswap_wallet.swap import SwapAborted

    with pytest.raises(SwapAborted, match="router"):
        EthAdapter().build_and_verify_deposit(**_lp_deposit_kwargs(router=None))


def test_eth_token_lp_withdraw_is_native_dust_not_a_token_deposit():
    # A withdraw ("-:POOL:bps") — even of a token pool — is a dust native-ETH
    # trigger, so it must take the native path (one tx, memo as calldata), not
    # build an approve+deposit pair.
    memo = f"-:{USDT_ASSET}:5000"
    prepared = EthAdapter().build_and_verify_deposit(
        **_lp_deposit_kwargs(memo=memo, amount=1000)
    )
    assert prepared.problems == []
    built = prepared.built
    assert not hasattr(built, "approve_tx")  # native EthBuiltSwap, not token pair
    assert built.data == "0x" + memo.encode().hex()


@pytest.mark.network
def test_eth_token_balance_live():
    """Live ERC-20 balanceOf against the public RPC — guards the call encoding
    and decoding against drift. Asserts shape, not an exact (mutable) balance."""
    reports = EthAdapter().token_balances(MNEMONIC)
    assert [r.symbol for r in reports] == ["USDT-ETH", "USDC-ETH"]
    assert all(r.decimals == 6 and r.confirmed >= 0 for r in reports)


# --- JSON-RPC error handling (broadcast + malformed responses) ---


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_eth_broadcast_wraps_rpc_error(monkeypatch):
    # A JSON-RPC rejection comes back HTTP 200 with an `error` body, which _rpc
    # raises as a bare RuntimeError. broadcast() must wrap it in BroadcastError
    # so the CLI's _confirm_and_execute handler catches it (no raw traceback).
    from cryptoswap_wallet.swap import BroadcastError

    adapter = EthAdapter()

    def boom(method, params):
        raise RuntimeError("RPC eth_sendRawTransaction: nonce too low")

    monkeypatch.setattr(adapter, "_rpc", boom)
    with pytest.raises(BroadcastError):
        adapter.broadcast(["0xdeadbeef"])


def test_eth_rpc_missing_result_is_clean_error(monkeypatch):
    # A non-conformant node may answer with neither `result` nor `error`. _rpc
    # must raise a descriptive RuntimeError, not a bare KeyError on payload["result"].
    adapter = EthAdapter()
    monkeypatch.setattr(
        adapter, "_post", lambda *a, **k: _FakeResp({"jsonrpc": "2.0", "id": 1})
    )
    with pytest.raises(RuntimeError) as excinfo:
        adapter._rpc("eth_getBalance", ["0x0", "latest"])
    assert "result" in str(excinfo.value).lower()


# --- BIP-39 passphrase derivation (finding #1) ---


def test_eth_derivation_honors_bip39_passphrase():
    base = EthAdapter().derive_address(MNEMONIC)
    withpw = EthAdapter(bip39_passphrase="extra-word").derive_address(MNEMONIC)
    assert withpw != base  # a passphrase derives a different wallet
    # An empty passphrase MUST equal the no-passphrase derivation, so a v1
    # wallet (passphrase stripped to "") keeps deriving its existing addresses.
    assert EthAdapter(bip39_passphrase="").derive_address(MNEMONIC) == base
