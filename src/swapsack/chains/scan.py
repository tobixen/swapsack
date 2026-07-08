"""Gap-limit HD address scanning, independent of any wallet library.

``derive_address`` and ``probe`` are injected, so the gap logic stays pure and
unit-testable. ``probe(address)`` returns any object exposing a ``has_history``
attribute (e.g. ``BtcAdapter.address_info``); records that have history are
returned with their derivation path. Addresses within a window are probed
concurrently, since the bottleneck is per-address network latency.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol


class _HasHistory(Protocol):
    has_history: bool


DEFAULT_GAP_LIMIT = 20
DEFAULT_WINDOW = 10
DEFAULT_WORKERS = 5


def scan_account[T: _HasHistory](
    *,
    derive_address: Callable[[str], str],
    probe: Callable[[str], T],
    account: str,
    gap_limit: int = DEFAULT_GAP_LIMIT,
    branches: tuple[int, ...] = (0, 1),
    window: int = DEFAULT_WINDOW,
    max_workers: int = DEFAULT_WORKERS,
) -> list[tuple[str, str, T]]:
    """Scan an account's branches; return ``(path, address, info)`` per used address.

    Gap counting uses ``info.has_history`` so used-but-empty addresses keep the
    scan going. Stops after ``gap_limit`` consecutive unused addresses.
    """
    found: list[tuple[str, str, T]] = []
    for branch in branches:
        gap = 0
        index = 0
        while gap < gap_limit:
            paths = [f"{account}/{branch}/{index + i}" for i in range(window)]
            addresses = [derive_address(p) for p in paths]
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                infos = list(pool.map(probe, addresses))
            for path, address, info in zip(paths, addresses, infos, strict=True):
                if info.has_history:
                    gap = 0
                    found.append((path, address, info))
                else:
                    gap += 1
            index += window
    return found
