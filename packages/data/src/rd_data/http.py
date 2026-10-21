"""HTTP access for provider adapters: allow-listed hosts, rate limits, retries, circuit breaker, typed errors.

Every outbound call goes through :class:`HttpFetcher`. It enforces HTTPS to the adapter's allow-listed hosts only
(SSRF guard), takes a shared rate-limit token, retries 429/5xx with full-jitter backoff (honouring ``Retry-After``),
records circuit-breaker outcomes, maps failures to typed errors and logs with secrets redacted.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import orjson

from rd_core.logging import get_logger
from rd_core.redaction import redact_text
from rd_data.errors import InvalidArgs, NotFound, ProviderDown, RateLimited
from rd_data.resilience import (
    RETRYABLE_STATUS,
    CircuitBreaker,
    RateLimit,
    RateLimiter,
    backoff_delays,
    retry_after_seconds,
)

#: Query parameter names (normalised: lower case, ``-`` as ``_``) that carry credentials; never logged or stored.
SECRET_PARAMS = frozenset(
    {
        "api_key",
        "apikey",
        "x_api_key",
        "token",
        "access_token",
        "auth_token",
        "refresh_token",
        "key",
        "access_key",
        "secret",
        "client_secret",
        "secret_key",
        "password",
        "passphrase",
        "signature",
        "sig",
        "auth",
        "api_token",
        "api_secret",
        "session_token",
        "private_key",
        "license_key",
        "licence_key",
        "pwd",
        "pass",
        "passwd",
        "authorization",
        "access_key_id",
        "secret_access_key",
        "app_key",
        "appkey",
        "appid",
        "app_id",
        "privatekey",
        "hmac",
        "awsaccesskeyid",
    }
)
#: Name endings that always mean a credential (``x_auth_token``, ``client_secret``, ``db_password``).
SECRET_SUFFIXES = (
    "_token",
    "_secret",
    "_password",
    "_passphrase",
    "_private_key",
    "_key_id",
    "_signature",
    "_credential",
    "subscription_key",
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
REDACTED_PARAM = "REDACTED"
#: Secrets shorter than this are not scrubbed from bodies (too likely to match ordinary text).
_MIN_SCRUB_LEN = 8


def is_secret_param(name: str) -> bool:
    """Credential parameter names, normalised: camelCase and kebab-case become snake_case (``accessToken``)."""
    norm = _CAMEL.sub("_", name).lower().replace("-", "_")
    return norm in SECRET_PARAMS or norm.endswith(SECRET_SUFFIXES)


def redact_params(params: Mapping[str, Any]) -> dict[str, str]:
    return {k: (REDACTED_PARAM if is_secret_param(k) else str(v)) for k, v in params.items()}


def secret_values(params: Mapping[str, Any]) -> list[str]:
    """The credential values in a request, to be scrubbed from anything stored (some APIs echo them back)."""
    return [str(v) for k, v in params.items() if is_secret_param(k) and len(str(v)) >= _MIN_SCRUB_LEN]


def scrub_body(body: bytes, secrets: Sequence[str]) -> bytes:
    """Replace every occurrence of a request credential in a response body with ``REDACTED``."""
    for secret in secrets:
        body = body.replace(secret.encode(), REDACTED_PARAM.encode())
    return body


def display_url(url: str, params: Mapping[str, Any]) -> str:
    """URL with credentials masked, suitable for logs, evidence and cassettes."""
    query = urlencode(sorted(redact_params(params).items()))
    return redact_text(f"{url}?{query}" if query else url)


@dataclass(frozen=True)
class FetchResult:
    status: int
    url: str  # redacted display URL
    body: bytes
    headers: Mapping[str, str]
    elapsed_s: float

    def json(self) -> Any:  # noqa: ANN401 - provider JSON is untyped until normalised
        try:
            return orjson.loads(self.body)
        except orjson.JSONDecodeError as exc:
            raise ProviderDown(f"invalid JSON from {self.url}: {exc}") from exc

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class HttpFetcher:
    def __init__(
        self,
        *,
        provider: str,
        allowed_hosts: frozenset[str],
        limiter: RateLimiter,
        rate_limit: RateLimit,
        breaker: CircuitBreaker,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "ResearchDesk/0.1 (+support@example.com)",
        timeout_s: float = 30.0,
        max_attempts: int = 4,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        record_ok: bool = True,
        max_body_bytes: int = 64 * 1024 * 1024,
        max_total_wait_s: float = 60.0,
    ) -> None:
        self.provider = provider
        self._hosts = allowed_hosts
        self._limiter = limiter
        self._rate_limit = rate_limit
        self._breaker = breaker
        self._client = httpx.AsyncClient(
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=False,
        )
        self._max_attempts = max_attempts
        self._rng = rng or random.Random()  # noqa: S311 - jitter only
        self._sleep = sleep
        self._log = get_logger("rd_data.http")
        # When False, a 2xx is not yet a success for the circuit breaker: the caller records it once the body has
        # been understood, so a provider answering 200 with garbage still counts as failing (adapters do this).
        self._record_ok = record_ok
        # A response bigger than this is never buffered (SEC company facts, the largest we use, are ~6 MB).
        self._max_body = max_body_bytes
        # Total time one call may spend waiting between retries, Retry-After included.
        self._max_wait = max_total_wait_s

    async def aclose(self) -> None:
        await self._client.aclose()

    async def report_malformed(self, detail: str) -> None:
        """Count a 2xx whose body the caller could not use as a provider failure (circuit breaker)."""
        await self._breaker.record_failure(self.provider, detail)

    async def _client_error(self, status: int, shown: str, body: bytes = b"") -> Exception:
        text = body[:2000].decode("utf-8", errors="replace").lower()
        if status == 400 and ("api_key" in text or "api key" in text):
            status = 401  # e.g. FRED answers an unregistered or revoked key with HTTP 400
        if status == 400 and "does not exist" in text:
            status = 404  # e.g. FRED answers an unknown series with HTTP 400
        if status in (401, 403):
            # A rejected key or plan makes the provider unusable until someone acts: count it, so the status page
            # shows the source as down rather than healthy.
            await self._breaker.record_failure(self.provider, f"HTTP {status} (authorization)")
        else:
            await self._breaker.record_success(self.provider)  # the service answered; the request was wrong
        if status == 404:
            return NotFound(f"{self.provider}: not found ({shown})", provider=self.provider)
        if status in (401, 403):
            return ProviderDown(
                f"{self.provider}: authorization rejected (HTTP {status}); check the API key or plan",
                provider=self.provider,
            )
        return InvalidArgs(f"{self.provider}: request rejected (HTTP {status}) {shown}", provider=self.provider)

    async def _fetch_capped(
        self, url: str, params: Mapping[str, Any], headers: Mapping[str, str] | None
    ) -> tuple[int, dict[str, str], bytes]:
        """One GET, reading at most ``max_body_bytes`` of the body (a larger response is an error, not a buffer)."""
        request = self._client.build_request("GET", url, params=params, headers=dict(headers or {}))
        response = await self._client.send(request, stream=True)
        try:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self._max_body:
                    await self._breaker.record_failure(self.provider, "response too large")
                    raise ProviderDown(
                        f"{self.provider}: response larger than {self._max_body:,} bytes refused",
                        provider=self.provider,
                    )
                chunks.append(chunk)
            return response.status_code, dict(response.headers), b"".join(chunks)
        finally:
            await response.aclose()

    def _check_target(self, url: str) -> None:
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = -1  # unparseable port
        if parts.scheme != "https" or parts.hostname not in self._hosts or port not in (None, 443):
            raise InvalidArgs(
                f"{self.provider}: refusing request to non-allow-listed URL {redact_text(url)}", provider=self.provider
            )

    async def get(
        self, url: str, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None
    ) -> FetchResult:
        params = dict(params or {})
        self._check_target(url)
        shown = display_url(url, params)
        secrets = secret_values(params)
        delays = backoff_delays(self._max_attempts - 1, rng=self._rng)
        last_error = "no attempt made"
        waited = 0.0
        for attempt in range(1, self._max_attempts + 1):
            # Checked before every attempt: once failures (ours or other workers') open the circuit, retries stop.
            await self._breaker.before_call(self.provider)
            await self._limiter.acquire(self.provider, self._rate_limit)
            started = time.perf_counter()
            try:
                status, resp_headers, body = await self._fetch_capped(url, params, headers)
            except httpx.RequestError as exc:
                # Connection failures and bodies that cannot be decoded (a corrupt gzip stream) alike: the
                # provider did not deliver a usable response, which counts against the circuit breaker.
                last_error = f"{type(exc).__name__}"
                self._log.warning(
                    "provider.transport_error", provider=self.provider, url=shown, attempt=attempt, error=last_error
                )
                await self._breaker.record_failure(self.provider, last_error)
                delay = next(delays, None)
                if delay is None or waited + delay > self._max_wait:
                    break
                waited += delay
                await self._sleep(delay)
                continue
            elapsed = time.perf_counter() - started
            self._log.info(
                "provider.response",
                provider=self.provider,
                url=shown,
                status=status,
                attempt=attempt,
                elapsed_ms=round(elapsed * 1000, 1),
            )
            if 200 <= status < 300 and status != 204 and body:
                if self._record_ok:
                    await self._breaker.record_success(self.provider)
                return FetchResult(status, shown, scrub_body(body, secrets), resp_headers, elapsed)
            if status < 400:
                # Redirects are never followed (allow-list) and an empty 2xx carries no data: both mean the
                # provider moved or broke the endpoint, which is an outage from our side, not a bad request.
                last_error = f"HTTP {status} with no usable body"
                await self._breaker.record_failure(self.provider, last_error)
                raise ProviderDown(f"{self.provider}: {last_error} ({shown})", provider=self.provider)
            if status in RETRYABLE_STATUS:
                last_error = f"HTTP {status}"
                if status >= 500:
                    await self._breaker.record_failure(self.provider, last_error)
                hinted = retry_after_seconds(resp_headers.get("retry-after"))
                delay = next(delays, None)
                wait = None if delay is None else max(delay, hinted or 0.0)
                if wait is None or waited + wait > self._max_wait:
                    if status == 429:
                        raise RateLimited(
                            f"{self.provider} rate-limited the request ({shown})",
                            provider=self.provider,
                            retry_after_s=hinted,
                        )
                    break
                waited += wait
                await self._sleep(wait)
                continue
            raise await self._client_error(status, shown, body)
        raise ProviderDown(
            f"{self.provider}: unavailable after {self._max_attempts} attempt(s): {last_error}", provider=self.provider
        )
