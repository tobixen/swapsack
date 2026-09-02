"""Shared HTTP client used by every chain adapter and the THORChain client.

Centralises the lazy-session + context-manager lifecycle that was previously
copy-pasted four times (A1 in docs/core-review.md). Uses niquests rather than
httpx.

Reads are retried and report themselves in a line a user can act on; writes are
neither — see :meth:`HttpClient._get` and :class:`HostUnreachable`. A read is
retried on a transport failure *and* on a throttle (429/503), since a deep
listing draws one out of a public explorer sooner or later.
"""

from __future__ import annotations

import email.utils
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

import niquests

# Network + HTTP-status errors worth catching at call sites.
HTTP_ERRORS = (niquests.exceptions.RequestException,)

# Transport failures where no answer was received: the request either never
# arrived or its reply never came back, so replaying a read is safe.
TRANSIENT_ERRORS = (
    niquests.exceptions.ConnectionError,  # also covers ProxyError/SSLError
    niquests.exceptions.Timeout,  # ConnectTimeout + ReadTimeout
)

# Answers that mean "not now, ask later" rather than "no". Both are documented
# to carry ``Retry-After``, and a public explorer hands them out under load —
# a 500 does not qualify: it is the server saying something went wrong, and
# asking again immediately is unlikely to change it.
THROTTLE_STATUSES = frozenset({429, 503})

DEFAULT_RETRIES = 2
DEFAULT_BACKOFF = 1.0

# The longest a ``Retry-After`` may hold up a CLI run. Explorers have been seen
# asking for an hour; waiting that out in the middle of a swap is worse than
# saying so and letting the user come back.
MAX_RETRY_AFTER = 30.0


class HostUnreachable(niquests.exceptions.RequestException):
    """A host that failed every attempt, said in one line instead of sixty.

    Subclasses ``RequestException`` on purpose: every call site in the tree
    already catches ``HTTP_ERRORS``, and this must not slip past them. The
    original transport error is kept as ``__cause__``.
    """


class RateLimited(HostUnreachable):
    """Every endpoint was asked, and every one of them said "not now".

    A subclass of :class:`HostUnreachable` so the call sites that already
    handle a dead host handle a throttled one too; distinct from it so a walk
    that can honestly return a *short* history can tell the two apart (see
    :func:`swapsack.chains.history.collect_pages`).
    """


def _retry_after(resp: object) -> float:
    """Seconds the endpoint asked us to wait, clamped to something sane.

    RFC 9110 allows either a delay in seconds or an HTTP-date; both appear in
    the wild. Anything unparseable is worth nothing more than a shrug — the
    caller falls back to its own backoff.
    """
    header = getattr(resp, "headers", None)
    value = (header or {}).get("Retry-After") if hasattr(header, "get") else None
    if not value:
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 0.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _describe(
    hosts: Sequence[str],
    exc: Exception,
    attempts: int,
    timeout: float,
    hint: str | None,
) -> str:
    """One actionable line: which host, what went wrong, how hard we tried.

    Deliberately *without* the URL path — an Esplora path is one of the
    wallet's own addresses, and this text ends up in terminals, log paste-ins
    and bug reports. That rules out interpolating ``exc``: niquests and urllib3
    put the whole URL in their own message. Only the class name is quoted; the
    original stays reachable as ``__cause__``.
    """
    reason = {
        niquests.exceptions.ReadTimeout: "connected, then never answered",
        niquests.exceptions.ConnectTimeout: "did not accept the connection",
        niquests.exceptions.SSLError: "failed the TLS handshake",
    }.get(type(exc), "could not be reached")
    line = (
        f"{', '.join(dict.fromkeys(hosts))} {reason} — gave up after "
        f"{attempts} attempts ({type(exc).__name__}, {timeout:g}s timeout)"
    )
    return f"{line}\n{hint}" if hint else line


def _describe_throttle(
    hosts: Sequence[str], status: int, attempts: int, hint: str | None
) -> str:
    """The same one line, for endpoints that answered but refused to serve.

    Same reason as :func:`_describe` for not interpolating the response: the
    stock message is "429 Client Error: Too Many Requests for url: .../address/
    bc1q…", which puts one of the wallet's own addresses on the terminal.
    """
    line = (
        f"{', '.join(dict.fromkeys(hosts))} rate-limited the request "
        f"(HTTP {status}) — gave up after {attempts} attempts"
    )
    return f"{line}\n{hint}" if hint else line


class HttpClient:
    """A lazily-created, reusable HTTP session with a context-manager lifecycle."""

    def __init__(
        self,
        timeout: float = 20.0,
        retries: int = DEFAULT_RETRIES,
        backoff: float = DEFAULT_BACKOFF,
        read_timeout: float | None = None,
        hint: str | None = None,
    ) -> None:
        self._timeout = timeout
        # A stalled read never recovers, so a caller talking to a flaky API may
        # give up on one sooner than on a write and let the retry do the work.
        self._read_timeout = timeout if read_timeout is None else read_timeout
        self._retries = retries
        self._backoff = backoff
        self._hint = hint
        self._session: niquests.Session | None = None

    @property
    def _http(self) -> niquests.Session:
        if self._session is None:
            self._session = niquests.Session()
        return self._session

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, url: str, **kwargs: object) -> niquests.Response:
        """GET ``url``, retrying a transient transport failure with backoff.

        The public APIs this talks to drop requests. Measured on 2026-08-28,
        ``blockstream.info`` black-holed roughly one request in twenty — TCP and
        TLS complete, then nothing comes back — with plain ``curl``, sequential
        or concurrent, over both IPv4 and IPv6. An HD account scan makes dozens
        of calls, so without a retry a stall was near-certain, and it aborted
        the whole swap after the passphrase prompt.

        niquests can retry this itself (``Session(retries=...)``), but it is off
        by default (``Retry(total=0, read=False)``). The loop is here instead so
        the retry can be reported with the host and *not* the URL path, and so
        the give-up carries :class:`HostUnreachable` rather than a bare timeout.

        Its ``Retry`` could also cover a throttle (``status_forcelist=[429]``,
        ``respect_retry_after_header=True``) — but only by *sleeping on the same
        host*: it retries within one connection pool and knows nothing about the
        second explorer, which is the cheaper answer to a 429 and the one this
        wallet has. Its ``MaxRetryError`` also quotes the whole URL, i.e. the
        address. So the status retry lives here beside the failover, not
        underneath it.

        GETs only. A POST here is a broadcast or an order, where a read timeout
        is *ambiguous*: the peer may well have taken it, so re-sending it could
        double-submit. Those are raised as-is, for a human to resolve.

        ``kwargs`` reach ``session.get`` untouched — ``params`` above all, which
        is the *whole request* for a Chainflip quote or a CoinGecko lookup.
        """
        return self._attempt([url], **kwargs)[0]

    def _attempt(
        self, urls: Sequence[str], **kwargs: object
    ) -> tuple[niquests.Response, int]:
        """GET the equivalent ``urls`` in turn until one answers.

        ``urls`` are the *same request* against interchangeable endpoints (one
        entry when there is nothing to fall back to). Each lap tries them all,
        and there are ``retries + 1`` laps, with backoff between laps only:
        moving to another host is itself the mitigation, so it needs no pause,
        while trying the same host again does.

        Returns the response and the index of the URL that produced it, so a
        caller can pin the endpoint that works. An HTTP error *status* is a
        real answer and is returned as-is — except a throttle
        (:data:`THROTTLE_STATUSES`), which is the endpoint asking to be left
        alone for a moment and moves on like a transport failure does. That is
        the whole point of having a second explorer: the two throttle
        independently, so the next one is likely to answer at once, and asking
        it costs nothing where waiting out a ``Retry-After`` costs seconds.

        A throttle only reaches the backoff when *every* endpoint gave one, and
        then ``Retry-After`` (capped at :data:`MAX_RETRY_AFTER`) sets the floor
        for the wait: it is the endpoint telling us what it takes to be served,
        and guessing shorter is what earned the 429.
        """
        hosts = [urlsplit(u).netloc for u in urls]
        last: Exception | None = None
        last_status: int | None = None
        for lap in range(self._retries + 1):
            wait = self._backoff * 2**lap
            for index, url in enumerate(urls):
                try:
                    resp = self._http.get(url, timeout=self._read_timeout, **kwargs)
                except TRANSIENT_ERRORS as exc:
                    last = exc
                    self._report(hosts, index, lap, type(exc).__name__)
                    continue
                status = getattr(resp, "status_code", None)
                if status in THROTTLE_STATUSES:
                    last_status = status
                    wait = max(wait, _retry_after(resp))
                    self._report(hosts, index, lap, f"HTTP {status}")
                    continue
                return resp, index
            if lap < self._retries:
                time.sleep(wait)
        attempts = (self._retries + 1) * len(urls)
        # A throttle outranks a timeout when both happened: it is the half the
        # user can act on (wait, or point --esplora somewhere less busy).
        if last_status is not None:
            raise RateLimited(
                _describe_throttle(hosts, last_status, attempts, self._hint)
            ) from last
        assert last is not None  # urls is non-empty, so the loop ran
        raise HostUnreachable(
            _describe(hosts, last, attempts, self._read_timeout, self._hint)
        ) from last

    def _report(self, hosts: Sequence[str], index: int, lap: int, why: str) -> None:
        """Say what failed and what is being tried instead — host, never path.

        An Esplora path is one of the wallet's own addresses, and this note
        lands in terminals that get pasted into bug reports. The *next* host is
        named because falling over to it hands that address to a different
        operator, which the user is entitled to see.
        """
        following = hosts[index + 1 :] or hosts[:1]
        laps = self._retries + 1
        print(
            f"note: {hosts[index]} {why} (attempt {lap + 1}/{laps}), "
            f"trying {following[0]}",
            file=sys.stderr,
        )

    def _post(self, url: str, **kwargs: object) -> niquests.Response:
        return self._http.post(url, timeout=self._timeout, **kwargs)


class FailoverHttpClient(HttpClient):
    """An :class:`HttpClient` over interchangeable endpoints for the same API.

    The public explorers this wallet depends on are best-effort and go quiet
    without warning, so a second endpoint is the difference between a swap that
    proceeds and one that dies after the passphrase prompt. The endpoint that
    answers is pinned, so a 60-call scan does not re-probe a dead host every
    time. Give it a single candidate — as ``--esplora`` does — and it degrades
    to a plain retrying client: naming an endpoint means naming *the* endpoint,
    not opting into a second operator seeing your addresses.
    """

    def __init__(self, candidates: str | Sequence[str], **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        urls = (candidates,) if isinstance(candidates, str) else tuple(candidates)
        if not urls:
            raise ValueError("need at least one endpoint URL")
        self._candidates = tuple(u.rstrip("/") for u in urls)
        self.base_url = self._candidates[0]

    def _get_with_fallback(self, suffix: str, **kwargs: object) -> niquests.Response:
        """GET ``{endpoint}/{suffix}``, starting from the pinned endpoint."""
        start = (
            self._candidates.index(self.base_url)
            if self.base_url in self._candidates
            else 0
        )
        ordered = self._candidates[start:] + self._candidates[:start]
        resp, index = self._attempt([f"{base}/{suffix}" for base in ordered], **kwargs)
        self.base_url = ordered[index]
        return resp
