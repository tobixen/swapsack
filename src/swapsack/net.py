"""Shared HTTP client used by every chain adapter and the THORChain client.

Centralises the lazy-session + context-manager lifecycle that was previously
copy-pasted four times (A1 in docs/core-review.md). Uses niquests rather than
httpx.

Reads are retried and report themselves in a line a user can act on; writes are
neither — see :meth:`HttpClient._get` and :class:`HostUnreachable`.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Sequence
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

DEFAULT_RETRIES = 2
DEFAULT_BACKOFF = 1.0


class HostUnreachable(niquests.exceptions.RequestException):
    """A host that failed every attempt, said in one line instead of sixty.

    Subclasses ``RequestException`` on purpose: every call site in the tree
    already catches ``HTTP_ERRORS``, and this must not slip past them. The
    original transport error is kept as ``__cause__``.
    """


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
        real answer and is returned as-is; only transport failures move on.
        """
        hosts = [urlsplit(u).netloc for u in urls]
        last: Exception | None = None
        for lap in range(self._retries + 1):
            for index, url in enumerate(urls):
                try:
                    resp = self._http.get(url, timeout=self._read_timeout, **kwargs)
                except TRANSIENT_ERRORS as exc:
                    last = exc
                    self._report(hosts, index, lap, type(exc).__name__)
                else:
                    return resp, index
            if lap < self._retries:
                time.sleep(self._backoff * 2**lap)
        assert last is not None  # urls is non-empty, so the loop ran
        raise HostUnreachable(
            _describe(
                hosts,
                last,
                (self._retries + 1) * len(urls),
                self._read_timeout,
                self._hint,
            )
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
