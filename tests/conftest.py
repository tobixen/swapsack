"""Shared pytest configuration.

The unit suite runs under ``filterwarnings = ["error"]`` (see pyproject) so
deprecations/misconfig surface as failures. The opt-in ``network`` integration
tests do real HTTP I/O, and niquests/urllib3 keep-alive sockets are released by
the garbage collector *after* the session is already closed. pytest's
unraisable-exception hook then re-raises that ``ResourceWarning`` and attributes
it to whichever ``network`` test happens to be running when the GC fires — which
flaked the "Integration (network)" CI job intermittently (e.g. pinned on
``test_btc_testnet_send_broadcast`` though any network call could be the source).

The socket *is* released; only the teardown timing is nondeterministic. The
per-item ``ignore::ResourceWarning`` filter below covers a GC that fires *during*
a network test, but not one that fires *after the last test* — between teardown
and pytest's unraisable-exception flush at ``pytest_unconfigure`` — which is the
window that intermittently reddened the "Integration (network)" job. So we also
force the reclamation deterministically in each network test's teardown (which
still runs inside that item's filter scope), draining the sockets before they
can leak into the unfiltered session-teardown window.

Both are scoped narrowly to ``network``-marked tests, so a genuine
leaked-resource (or any other unraisable) in the unit suite still fails as before.
"""

from __future__ import annotations

import gc

import pytest


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        if item.get_closest_marker("network"):
            item.add_marker(pytest.mark.filterwarnings("ignore::ResourceWarning"))


@pytest.fixture(autouse=True)
def _drain_keepalive_sockets(request: pytest.FixtureRequest):
    """Force GC of niquests/urllib3 keep-alive sockets in a network test's teardown.

    ``Session.close()`` does not eagerly close pooled keep-alive sockets; the GC
    does. Running ``gc.collect()`` here — still inside the item's
    ``ignore::ResourceWarning`` scope — finalizes them deterministically so none
    survives into the session-teardown window where the warning would be re-raised.
    """
    yield
    if request.node.get_closest_marker("network"):
        gc.collect()
