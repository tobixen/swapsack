"""Shared pytest configuration.

The unit suite runs under ``filterwarnings = ["error"]`` (see pyproject) so
deprecations/misconfig surface as failures. The opt-in ``network`` integration
tests do real HTTP I/O, and niquests/urllib3 keep-alive sockets are released by
the garbage collector *after* the session is already closed. pytest's
unraisable-exception hook then re-raises that ``ResourceWarning`` and attributes
it to whichever ``network`` test happens to be running when the GC fires — which
flaked the "Integration (network)" CI job intermittently (e.g. pinned on
``test_btc_testnet_send_broadcast`` though any network call could be the source).

The socket *is* released; only the teardown timing is nondeterministic, so this
is teardown noise rather than a leak we can fix in our code. Tolerate it — but
narrowly: ignore only ``ResourceWarning`` and only for ``network``-marked tests,
so a genuine leaked-resource (or any other unraisable) in the unit suite still
fails as before.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        if item.get_closest_marker("network"):
            item.add_marker(pytest.mark.filterwarnings("ignore::ResourceWarning"))
