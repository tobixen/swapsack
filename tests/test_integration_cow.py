"""Opt-in integration tests against the live CoW Protocol API.

Excluded by default; run with `uv run pytest -m network`. These catch API
drift (field renames, EIP-712 domain/type changes) that unit tests against a
recorded fixture cannot, and — the important one — prove the wallet can sign
a real, keyless CoW order end to end without ever holding a live private key:
a throwaway, unfunded key's signed order is submitted and must clear every
orderbook check (signature, EIP-712 domain, decoding) up to the balance
check, which is the one thing that requires actual funds.
"""

import time

import pytest
from eth_account import Account

from swapsack.cli import ASSET
from swapsack.cow import (
    COW_ASSETS,
    CowClient,
    CowError,
    build_order,
    parse_cow_quote,
    sign_order,
)

pytestmark = pytest.mark.network

USDT = COW_ASSETS[ASSET["USDT-ETH"]][0]
USDC = COW_ASSETS[ASSET["USDC-ETH"]][0]
DEST = "0x40A50cf069e992AA4536211B23F286eF88752187"


def test_usdt_to_usdc_quote_live():
    with CowClient() as client:
        payload = client.quote(
            USDT, USDC, 100_000_000, from_address=DEST, receiver=DEST
        )
    quote = parse_cow_quote(payload, to_asset=ASSET["USDC-ETH"], buy_decimals=6)
    assert quote.expected_amount_out > 0
    assert quote.sell_amount_total == 100_000_000
    assert quote.valid_to > time.time()


def test_dust_amount_raises_live():
    # 1 wei of USDT can't cover the fee -> the API errors instead of quoting a
    # nonsensical trade. Guards our error-body parsing (errorType/description).
    with pytest.raises(CowError, match="SellAmountDoesNotCoverFee"):
        with CowClient() as client:
            client.quote(USDT, USDC, 1, from_address=DEST, receiver=DEST)


def test_signed_order_from_unfunded_key_clears_every_check_but_balance_live():
    # Proves the EIP-712 domain/types/signing match the live orderbook's
    # expectations: an unfunded key's order is rejected ONLY for insufficient
    # balance, meaning the signature, decoding and every order field passed.
    acct = Account.create()
    with CowClient() as client:
        payload = client.quote(
            USDT, USDC, 100_000_000, from_address=acct.address, receiver=acct.address
        )
        quote = parse_cow_quote(payload, to_asset=ASSET["USDC-ETH"], buy_decimals=6)
        order = build_order(quote)
        signature = sign_order(order, acct.key)
        with pytest.raises(CowError, match="InsufficientBalance"):
            client.submit_order(
                order,
                signature=signature,
                from_address=acct.address,
                quote_id=quote.quote_id,
            )
