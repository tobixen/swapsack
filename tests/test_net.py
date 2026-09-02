"""The shared HTTP client's retry policy.

A single slow Esplora reply used to abort a whole swap — after the passphrase
prompt, with a 60-line traceback (blockstream.info, read timeout=20). Reads are
replayable, so they are retried; writes are not.
"""

from __future__ import annotations

import datetime
import email.utils

import niquests
import pytest

from conftest import FakeResponse, FakeSession
from swapsack.net import (
    DEFAULT_RETRIES,
    MAX_RETRY_AFTER,
    FailoverHttpClient,
    HostUnreachable,
    HttpClient,
    RateLimited,
)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Swallow the backoff, and record what it would have slept."""
    slept: list[float] = []
    monkeypatch.setattr("swapsack.net.time.sleep", slept.append)
    return slept


def _client(*outcomes: object, **kwargs: object) -> tuple[HttpClient, FakeSession]:
    client = HttpClient(**kwargs)  # type: ignore[arg-type]
    session = FakeSession(*outcomes)
    client._session = session  # type: ignore[assignment]
    return client, session


def test_get_retries_a_read_timeout_and_succeeds(no_sleep):
    timeout = niquests.exceptions.ReadTimeout("read timed out")
    client, session = _client(timeout, timeout, "body")
    assert client._get("https://blockstream.info/api/address/bc1q") == "body"
    assert len(session.gets) == 3


def test_get_retries_a_connection_error(no_sleep):
    client, session = _client(niquests.exceptions.ConnectionError("no route"))
    assert client._get("https://example.invalid/x") == "ok"
    assert len(session.gets) == 2


def test_get_gives_up_and_raises_the_last_failure(no_sleep):
    fail = niquests.exceptions.ReadTimeout("read timed out")
    client, session = _client(*[fail] * 10)
    with pytest.raises(HostUnreachable):
        client._get("https://blockstream.info/api/address/bc1q")
    # One attempt plus the retries — bounded, not a loop against a dead host.
    assert len(session.gets) == DEFAULT_RETRIES + 1
    assert len(no_sleep) == DEFAULT_RETRIES


def test_backoff_grows_between_attempts(no_sleep):
    fail = niquests.exceptions.ConnectTimeout("connect timed out")
    client, _ = _client(*[fail] * 10)
    with pytest.raises(HostUnreachable):
        client._get("https://example.invalid/x")
    assert no_sleep == sorted(no_sleep) and no_sleep[0] > 0
    assert len(set(no_sleep)) == len(no_sleep)  # exponential, not a fixed sleep


def test_get_does_not_retry_a_non_transport_failure(no_sleep):
    # A TooManyRedirects (or any other RequestException that is not a transport
    # failure) means the peer answered: replaying it would just answer again.
    client, session = _client(niquests.exceptions.TooManyRedirects("loop"))
    with pytest.raises(niquests.exceptions.TooManyRedirects):
        client._get("https://example.invalid/x")
    assert len(session.gets) == 1


def test_post_is_never_retried(no_sleep):
    # A POST is a broadcast or an order. A read timeout there is ambiguous —
    # the peer may well have taken it — so it must not be silently re-sent.
    client, session = _client(niquests.exceptions.ReadTimeout("read timed out"))
    with pytest.raises(niquests.exceptions.ReadTimeout):
        client._post("https://blockstream.info/api/tx", data=b"raw")
    assert len(session.posts) == 1
    assert no_sleep == []


def test_retry_note_names_the_host_but_not_the_path(no_sleep, capsys):
    # The note goes to a terminal the user pastes into issues; an address in a
    # URL path is personal data, the host alone is not.
    client, _ = _client(niquests.exceptions.ReadTimeout("read timed out"))
    client._get("https://blockstream.info/api/address/bc1qsecretaddress")
    err = capsys.readouterr().err
    assert "blockstream.info" in err
    assert "bc1qsecretaddress" not in err


def test_retries_can_be_disabled(no_sleep):
    client, session = _client(
        niquests.exceptions.ReadTimeout("read timed out"), retries=0
    )
    with pytest.raises(HostUnreachable):
        client._get("https://example.invalid/x")
    assert len(session.gets) == 1


def test_giving_up_says_what_a_user_can_act_on(no_sleep):
    # The raw niquests message is "HTTPSConnectionPool(host=..., port=443):
    # Read timed out. (read timeout=20.0)" wrapped in 60 lines of urllib3
    # frames. What a user needs: which host, that it was tried more than once,
    # and that nothing was sent.
    # niquests/urllib3 put the *whole URL* in their own message, so the wrapper
    # must not simply interpolate it: that would re-leak the address it took
    # care not to print.
    fail = niquests.exceptions.ReadTimeout(
        "HTTPSConnectionPool(host='blockstream.info', port=443): Max retries "
        "exceeded with url: /api/address/bc1qsecretaddress (Caused by ...)"
    )
    client, _ = _client(*[fail] * 10)
    with pytest.raises(HostUnreachable) as exc:
        client._get("https://blockstream.info/api/address/bc1qsecretaddress")
    message = str(exc.value)
    assert "blockstream.info" in message
    assert f"{DEFAULT_RETRIES + 1} attempts" in message
    assert "ReadTimeout" in message  # the class name, not its chatty message
    assert "bc1qsecretaddress" not in message  # the path is the user's business
    # The transport failure is kept as the cause, for a --debug traceback.
    assert isinstance(exc.value.__cause__, niquests.exceptions.ReadTimeout)


def test_host_unreachable_is_still_a_request_exception():
    # Every call site in the tree catches HTTP_ERRORS (RequestException); the
    # nicer message must not slip past them.
    assert issubclass(HostUnreachable, niquests.exceptions.RequestException)


def test_the_endpoint_hint_is_included_when_the_client_has_one(no_sleep):
    # Only the caller knows which flag points somewhere else, so it supplies
    # the one line of advice.
    fail = niquests.exceptions.ReadTimeout("read timed out")
    client, _ = _client(*[fail] * 10, hint="try --esplora https://example.org/api")
    with pytest.raises(HostUnreachable, match="--esplora"):
        client._get("https://blockstream.info/api/address/bc1q")


def test_reads_may_time_out_sooner_than_writes(no_sleep):
    # A stalled read never recovers, and these APIs answer in well under a
    # second, so a read gives up early and retries. A POST is a broadcast: it
    # is not retried, so it keeps the patient timeout.
    client, session = _client("ok", "ok", timeout=20.0, read_timeout=8.0)
    client._get("https://blockstream.info/api/blocks/tip/height")
    client._post("https://blockstream.info/api/tx", data=b"raw")
    assert session.timeouts == [8.0, 20.0]


def test_read_timeout_defaults_to_the_general_timeout(no_sleep):
    client, session = _client("ok", timeout=12.0)
    client._get("https://example.org/x")
    assert session.timeouts == [12.0]


# --- failover between interchangeable endpoints --------------------------------

BLOCKSTREAM = "https://blockstream.info/api"
MEMPOOL = "https://mempool.space/api"


def _failover(
    *outcomes: object, **kwargs: object
) -> tuple[FailoverHttpClient, FakeSession]:
    client = FailoverHttpClient([BLOCKSTREAM, MEMPOOL], **kwargs)  # type: ignore[arg-type]
    session = FakeSession(*outcomes)
    client._session = session  # type: ignore[assignment]
    return client, session


def test_a_stalled_endpoint_is_abandoned_for_the_next_one(no_sleep, capsys):
    client, session = _failover(niquests.exceptions.ReadTimeout("stalled"), "body")
    assert client._get_with_fallback("address/bc1q") == "body"
    assert session.gets == [f"{BLOCKSTREAM}/address/bc1q", f"{MEMPOOL}/address/bc1q"]
    # Switching hosts is itself the mitigation, so it happens without a pause.
    assert no_sleep == []


def test_the_switch_is_announced_because_it_is_a_different_operator(no_sleep, capsys):
    # Failing over hands the queried address to somebody else. That is a fact
    # about the user's privacy, not an implementation detail: say it out loud.
    client, _ = _failover(niquests.exceptions.ReadTimeout("stalled"), "body")
    client._get_with_fallback("address/bc1qsecretaddress")
    err = capsys.readouterr().err
    assert "blockstream.info" in err and "mempool.space" in err
    assert "bc1qsecretaddress" not in err


def test_the_endpoint_that_answered_is_pinned(no_sleep):
    client, session = _failover(
        niquests.exceptions.ReadTimeout("stalled"), "body", "body2"
    )
    client._get_with_fallback("address/bc1q")
    assert client.base_url == MEMPOOL
    # The next call starts where the last one succeeded: no re-probing the
    # dead host on every single address of a 60-call scan.
    client._get_with_fallback("fee-estimates")
    assert session.gets[-1] == f"{MEMPOOL}/fee-estimates"


def test_an_http_error_response_is_an_answer_not_an_outage(no_sleep):
    # A 404 or a 500 came *from* the endpoint, and asking a second one would
    # only duplicate the question (and leak the address) — hand it to the
    # caller's own raise_for_status(). A 429 is the exception; see below.
    answer = FakeResponse(500)
    client, session = _failover(answer)
    assert client._get_with_fallback("address/bc1q") is answer
    assert len(session.gets) == 1


def test_every_endpoint_failing_names_them_all(no_sleep):
    fail = niquests.exceptions.ReadTimeout("stalled")
    client, session = _failover(*[fail] * 20, retries=1)
    with pytest.raises(HostUnreachable) as exc:
        client._get_with_fallback("address/bc1q")
    message = str(exc.value)
    assert "blockstream.info" in message and "mempool.space" in message
    # Two endpoints × (1 retry + 1) laps, with a backoff between laps only.
    assert len(session.gets) == 4
    assert len(no_sleep) == 1


def test_a_single_endpoint_still_behaves_like_a_plain_client(no_sleep):
    # No alternatives configured (the user named one with --esplora): same
    # retry-in-place as before, and no invented second operator.
    client = FailoverHttpClient([MEMPOOL])
    session = FakeSession(*[niquests.exceptions.ReadTimeout("stalled")] * 10)
    client._session = session  # type: ignore[assignment]
    with pytest.raises(HostUnreachable):
        client._get_with_fallback("address/bc1q")
    assert len(session.gets) == DEFAULT_RETRIES + 1
    assert {u.split("/api")[0] for u in session.gets} == {"https://mempool.space"}


def test_at_least_one_endpoint_is_required():
    with pytest.raises(ValueError, match="at least one"):
        FailoverHttpClient([])


def test_get_forwards_query_parameters_to_the_session(no_sleep):
    # The retry loop sits between `_get` and the session, and a client that
    # drops `params` on the way through still issues a request that *looks*
    # right: same URL, HTTP 200, an answer the caller then fails to parse.
    # Chainflip's whole quote and CoinGecko's `ids`/`vs_currencies` ride here.
    client, session = _client("body")
    params = {"srcChain": "Bitcoin", "amount": "500000"}
    assert client._get("https://chainflip.io/v2/quote", params=params) == "body"
    assert session.kwargs == [{"params": params}]


def test_a_retried_get_repeats_the_query_parameters(no_sleep):
    timeout = niquests.exceptions.ReadTimeout("read timed out")
    client, session = _client(timeout, "body")
    client._get("https://chainflip.io/v2/quote", params={"amount": "1"})
    assert session.kwargs == [{"params": {"amount": "1"}}] * 2


def test_failover_forwards_query_parameters_to_every_endpoint(no_sleep):
    client = FailoverHttpClient(["https://one.invalid", "https://two.invalid"])
    session = FakeSession(niquests.exceptions.ReadTimeout("stalled"), "body")
    client._session = session
    client._get_with_fallback("api/tx", params={"verbose": "1"})
    assert session.kwargs == [{"params": {"verbose": "1"}}] * 2


# --- rate limiting -------------------------------------------------------------
#
# A deep `history` walk makes hundreds of requests against a public explorer,
# and the explorers throttle it. A 429 is a perfectly good HTTP *response*, so
# it used to sail past the transport-failure retry into raise_for_status() and
# kill the listing partway through — with the queried address in the message.


def test_a_throttled_endpoint_is_abandoned_for_the_next_one(no_sleep):
    # The two default explorers throttle independently, so the fallback that
    # already exists is the right answer to a 429 — and it costs no waiting.
    client, session = _failover(FakeResponse(429), "body")
    assert client._get_with_fallback("address/bc1q") == "body"
    assert session.gets == [f"{BLOCKSTREAM}/address/bc1q", f"{MEMPOOL}/address/bc1q"]
    assert no_sleep == []


def test_a_503_is_treated_like_a_429(no_sleep):
    # "Not now, ask later" — same meaning, same Retry-After, same handling.
    client, session = _failover(FakeResponse(503), "body")
    assert client._get_with_fallback("address/bc1q") == "body"
    assert len(session.gets) == 2


def test_a_throttled_lone_endpoint_is_retried_in_place(no_sleep):
    # Nothing to fail over to (--esplora names one instance): wait and re-ask.
    client, session = _client(FakeResponse(429), FakeResponse(429), "body")
    assert client._get("https://mempool.space/api/address/bc1q") == "body"
    assert len(session.gets) == 3
    assert len(no_sleep) == 2


def test_retry_after_is_honoured(no_sleep):
    # A 429 usually names how long to wait; guessing shorter is what got us
    # throttled in the first place.
    client, _ = _client(FakeResponse(429, Retry_After="7"), "body")
    assert no_sleep == []
    client._get("https://mempool.space/api/address/bc1q")
    assert no_sleep == [7.0]


def test_retry_after_may_be_an_http_date(no_sleep, monkeypatch):
    when = email.utils.format_datetime(
        datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=9)
    )
    client, _ = _client(FakeResponse(429, Retry_After=when), "body")
    client._get("https://mempool.space/api/address/bc1q")
    assert no_sleep and 5 <= no_sleep[0] <= 10


def test_an_absurd_retry_after_is_capped(no_sleep):
    # An explorer that says "come back in an hour" must not hang a CLI run for
    # an hour: cap the wait, spend the remaining attempts, and give up saying so.
    client, _ = _client(*[FakeResponse(429, Retry_After="3600")] * 10)
    with pytest.raises(RateLimited):
        client._get("https://mempool.space/api/address/bc1q")
    assert no_sleep == [MAX_RETRY_AFTER] * DEFAULT_RETRIES


def test_a_retry_after_shorter_than_the_backoff_does_not_shorten_it(no_sleep):
    # Retry-After is a floor on the wait, not a licence to hammer.
    client, _ = _client(*[FakeResponse(429, Retry_After="0")] * 10, backoff=2.0)
    with pytest.raises(RateLimited):
        client._get("https://mempool.space/api/address/bc1q")
    assert no_sleep[0] >= 2.0


def test_every_endpoint_throttling_says_so_without_the_address(no_sleep):
    # What the user saw before: "429 Client Error: Too Many Requests for url:
    # https://blockstream.info/api/address/bc1qht68...", i.e. their own address
    # on the terminal, and no hint that a second explorer exists.
    client, session = _failover(*[FakeResponse(429)] * 20, retries=1)
    with pytest.raises(RateLimited) as exc:
        client._get_with_fallback("address/bc1qsecretaddress")
    message = str(exc.value)
    assert "blockstream.info" in message and "mempool.space" in message
    assert "429" in message
    assert "bc1qsecretaddress" not in message
    assert len(session.gets) == 4  # two endpoints x (1 retry + 1) laps


def test_rate_limited_is_caught_wherever_a_transport_failure_is(no_sleep):
    # Every call site already catches HTTP_ERRORS, and the walkers that degrade
    # to INCOMPLETE catch HostUnreachable specifically.
    assert issubclass(RateLimited, HostUnreachable)
    assert issubclass(RateLimited, niquests.exceptions.RequestException)


def test_the_throttle_note_names_the_host_but_not_the_path(no_sleep, capsys):
    client, _ = _failover(FakeResponse(429), "body")
    client._get_with_fallback("address/bc1qsecretaddress")
    err = capsys.readouterr().err
    assert "blockstream.info" in err and "mempool.space" in err
    assert "429" in err
    assert "bc1qsecretaddress" not in err


def test_a_throttle_outranks_a_timeout_in_the_giveup_message(no_sleep):
    # Mixed failures: one host stalled, the other throttled. The 429 is the
    # actionable half (slow down, or come back later), so it names the error.
    client, _ = _failover(
        niquests.exceptions.ReadTimeout("stalled"), FakeResponse(429), retries=0
    )
    with pytest.raises(RateLimited):
        client._get_with_fallback("address/bc1q")
