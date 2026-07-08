"""Tests for gap-limit HD address scanning (pure, injected probe)."""

import dataclasses

from swapsack.chains.scan import scan_account

ACCOUNT = "m/84'/0'/0'"


@dataclasses.dataclass
class Info:
    has_history: bool
    confirmed: int = 0


def derive(path: str) -> str:
    return f"addr::{path}"


def make_probe(activity: dict[str, int]):
    def probe(address: str) -> Info:
        if address in activity:
            return Info(has_history=True, confirmed=activity[address])
        return Info(has_history=False)

    return probe


def test_scan_finds_history_and_records_path():
    addr0 = derive(f"{ACCOUNT}/0/0")
    found = scan_account(
        derive_address=derive,
        probe=make_probe({addr0: 5000}),
        account=ACCOUNT,
        gap_limit=3,
        branches=(0,),
    )
    assert len(found) == 1
    path, address, info = found[0]
    assert path == f"{ACCOUNT}/0/0"
    assert address == addr0
    assert info.confirmed == 5000


def test_scan_respects_gap_limit_when_empty():
    found = scan_account(
        derive_address=derive,
        probe=make_probe({}),
        account=ACCOUNT,
        gap_limit=2,
        branches=(0,),
    )
    assert found == []


def test_scan_continues_past_used_but_empty_address():
    a0 = derive(f"{ACCOUNT}/0/0")  # used, now empty (history, zero balance)
    a1 = derive(f"{ACCOUNT}/0/1")  # holds funds
    found = scan_account(
        derive_address=derive,
        probe=make_probe({a0: 0, a1: 7000}),
        account=ACCOUNT,
        gap_limit=2,
        branches=(0,),
    )
    assert {info.confirmed for _, _, info in found} == {0, 7000}


def test_scan_covers_receive_and_change_branches():
    recv = derive(f"{ACCOUNT}/0/0")
    chng = derive(f"{ACCOUNT}/1/0")
    found = scan_account(
        derive_address=derive,
        probe=make_probe({recv: 1000, chng: 2000}),
        account=ACCOUNT,
        gap_limit=2,
        branches=(0, 1),
    )
    assert {info.confirmed for _, _, info in found} == {1000, 2000}
