"""Tests for THORChain liquidity-provision memos + symmetric-add maths."""

import pytest

from swapsack.liquidity import (
    add_liquidity_memo,
    pair_amount,
    symmetric_add_memo,
    withdraw_liquidity_memo,
)


def test_add_memo():
    assert add_liquidity_memo("BTC.BTC") == "+:BTC.BTC"


def test_symmetric_add_memo_pairs_the_other_side():
    # Asset leg references the protocol (CACAO/RUNE) address...
    assert symmetric_add_memo("BTC.BTC", "maya1abc") == "+:BTC.BTC:maya1abc"
    # ...and the protocol leg references the asset-chain address.
    assert symmetric_add_memo("BTC.BTC", "bc1qxyz") == "+:BTC.BTC:bc1qxyz"


def test_symmetric_add_memo_requires_paired_address():
    with pytest.raises(ValueError):
        symmetric_add_memo("BTC.BTC", "")


def test_pair_amount_matches_pool_ratio_cacao_1e10():
    # Real Maya BTC.BTC depths: 30.62764092 BTC (1e8) vs 170977282063952174 cacao
    # (1e10). Adding 1 BTC (1e8) -> ~558k CACAO in 1e10 base units.
    protocol = pair_amount(100_000_000, 3_062_764_092, 170_977_282_063_952_174)
    assert protocol == 100_000_000 * 170_977_282_063_952_174 // 3_062_764_092
    assert 5.5e15 < protocol < 5.6e15  # ~558k CACAO * 1e10


def test_pair_amount_rune_1e8():
    # A RUNE pool side is 1e8; e.g. 10 BTC : 5000 RUNE -> add 1 BTC = 500 RUNE.
    assert pair_amount(100_000_000, 1_000_000_000, 500_000_000_000) == 50_000_000_000


def test_pair_amount_rejects_empty_pool_or_nonpositive():
    with pytest.raises(ValueError):
        pair_amount(100_000_000, 0, 1)
    with pytest.raises(ValueError):
        pair_amount(0, 1, 1)


def test_withdraw_memo_full():
    assert withdraw_liquidity_memo("BTC.BTC", 10000) == "-:BTC.BTC:10000"


def test_withdraw_memo_partial():
    assert withdraw_liquidity_memo("ETH.ETH", 2500) == "-:ETH.ETH:2500"


def test_withdraw_memo_rejects_out_of_range():
    with pytest.raises(ValueError):
        withdraw_liquidity_memo("BTC.BTC", 0)
    with pytest.raises(ValueError):
        withdraw_liquidity_memo("BTC.BTC", 10001)
