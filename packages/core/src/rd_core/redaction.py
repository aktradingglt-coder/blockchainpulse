"""Secret redaction for logs, traces and stored transcripts (PROMPT §1.5).

Two layers: (1) any mapping key whose name suggests a secret is masked; (2) any string value is scanned for
secret-shaped substrings (provider key prefixes, bearer tokens, credentials in URLs, ``api_key=`` query params).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Key names are split into words (snake, kebab, dotted and camelCase) and matched word-wise, so "input_tokens",
# "passenger" or "secretary" stay visible while "refreshToken", "X-Api-Key" or "db_password" are masked.
_SENSITIVE_WORDS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "pwd",
        "pass",
        "secret",
        "secrets",
        "token",
        "apikey",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "dsn",
        "sessionid",
        "jwt",
        "bearer",
        "auth",
        "pgpassword",
    }
)
_SENSITIVE_PAIRS = frozenset(
    {
        ("api", "key"),
        ("private", "key"),
        ("signing", "key"),
        ("secret", "key"),
        ("access", "key"),
        ("session", "id"),
        ("auth", "header"),
        ("webhook", "url"),
        ("client", "secret"),
    }
)
# A sensitive word qualified by one of these names metadata, not a credential: token_symbol, token_unlocks,
# auth_provider, dsn_host, password_length, secret_count.
_NON_SECRET_QUALIFIERS = frozenset(
    {
        "symbol",
        "symbols",
        "unlock",
        "unlocks",
        "supply",
        "price",
        "prices",
        "address",
        "contract",
        "name",
        "names",
        "decimals",
        "count",
        "budget",
        "usage",
        "limit",
        "limits",
        "type",
        "provider",
        "method",
        "scheme",
        "host",
        "port",
        "status",
        "required",
        "enabled",
        "length",
        "rate",
        "expires",
        "expiry",
        "ttl",
        "rotated",
        "version",
        "standard",
    }
)
_WORD_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")

_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic keys
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),  # generic sk- keys
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),  # Stripe-style keys
    re.compile(r"\b(?:pdl|apikey)_[A-Za-z0-9_]{16,}"),  # Paddle-style keys
    re.compile(r"\bre_[A-Za-z0-9_]{20,}"),  # Resend keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key IDs
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWTs
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"),  # Telegram bot tokens (also inside /bot<token>/ URLs)
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{12,}"),  # bearer tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)
#: Longer strings are truncated before scanning: bounds CPU per log line or span attribute (no regex DoS).
MAX_SCAN_CHARS = 8192
# Every pattern below is linear-time: unbounded runs only follow a literal anchor, and runs before a required
# literal are bounded ({0,N}) so a long attacker-controlled string cannot trigger quadratic backtracking.

# user:password@ — the user part may be empty (redis://:password@host); the password may contain "@", "/" or "$"
# and runs to the last "@" before the host.
_URL_CREDENTIALS = re.compile(
    r"(?<![A-Za-z0-9+.\-])(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]{0,31}://)(?P<user>[^:/@\s]{0,256}):"
    r"(?P<pw>[^\s]{1,512}?)@(?P<host>[^@\s]{1,512})(?=\s|$)"
)
# Key-only URL credentials such as Sentry DSNs (https://<hex key>@host/project).
_URL_KEY_ONLY = re.compile(r"(?P<scheme>https?://)[0-9a-f]{32}@")
# Ambiguous names (token, key, sig, signature, pass) are masked only when the value looks like a credential, so
# market data such as "token=ETH", "key=AAPL" or "sig=0.05" survives; unambiguous names are always masked.
_UNAMBIGUOUS_NAMES = (
    r"x-api-key|api[_-]?key|apikey|aws_secret_access_key|client[_-]?secret|secret[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|password|passwd|passphrase|pgpassword|secret|[a-z0-9]{1,40}[_-](?:password|passwd|auth|token|secret|api[_-]?key)"
)
_AMBIGUOUS_NAMES = r"token|key|sig|signature|pass"
_KEY_VALUE = re.compile(
    rf"(?i)(?P<k>(?P<q>[\"']?)(?<![A-Za-z0-9_\-])(?P<name>{_UNAMBIGUOUS_NAMES}|{_AMBIGUOUS_NAMES})(?P=q)\s{{0,8}}"
    r"[:=]\s{0,8})(?P<v>\"[^\"\n]{0,2048}\"|'[^'\n]{0,2048}'|[^\s\"',;&#]{1,2048})"
)
# "Authorization: Basic abc" (keeps the scheme word, masks the credential).
_AUTH_HEADER = re.compile(
    r"(?i)(?P<k>\b(?:proxy-)?authorization\b[\"']?\s{0,8}[:=]\s{0,8}[\"']?)"
    r"(?:(?P<scheme>basic|bearer|token|digest)\s{1,8})?[^\s\"',;]{1,2048}"
)
_COOKIE_HEADER = re.compile(r"(?im)(?P<k>\b(?:set-)?cookie\s{0,8}:\s{0,8})[^\r\n]{1,8192}")
_DISCORD_WEBHOOK = re.compile(r"(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d{1,32}/)[\w-]{1,256}")
_PLAIN_LITERAL = re.compile(r"(?i)^(?:-?\d+(?:\.\d+)?%?|true|false|null|none)$")
_AMBIGUOUS = frozenset({"token", "key", "sig", "signature", "pass"})


def _looks_like_credential(value: str) -> bool:
    """For ambiguous names: long or mixed-character values are credentials; tickers and numbers are not."""
    if _PLAIN_LITERAL.match(value):
        return False
    return len(value) >= 12 or (len(value) >= 6 and any(c.isdigit() for c in value) and any(c.isalpha() for c in value))


def _mask_key_value(m: re.Match[str]) -> str:
    raw = m.group("v")
    quote = raw[0] if raw[:1] in ("'", '"') else ""
    value = raw[1:-1] if quote else raw
    name = m.group("name").lower()
    if _PLAIN_LITERAL.match(value) or (name in _AMBIGUOUS and not _looks_like_credential(value)):
        return m.group(0)
    return f"{m.group('k')}{quote}{REDACTED}{quote}"


_PORT_AND_PATH = re.compile(r"\d{1,5}(?:/\S*)?")


def _mask_url_credentials(m: re.Match[str]) -> str:
    # "https://example.com:8443/path@v2" is host:port/path with an "@" in the path, not user:password@host.
    if "." in m.group("user") and _PORT_AND_PATH.fullmatch(m.group("pw")):
        return m.group(0)
    return f"{m.group('scheme')}{m.group('user')}:{REDACTED}@{m.group('host')}"


def redact_text(text: str) -> str:
    """Mask secret-shaped substrings inside free text. Linear-time; input is capped at ``MAX_SCAN_CHARS``."""
    out = text
    if len(out) > MAX_SCAN_CHARS:
        out = f"{out[:MAX_SCAN_CHARS]}…[TRUNCATED {len(text) - MAX_SCAN_CHARS} chars]"
    for pattern in _VALUE_PATTERNS:
        out = pattern.sub(REDACTED, out)
    out = _URL_CREDENTIALS.sub(_mask_url_credentials, out)
    out = _URL_KEY_ONLY.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", out)
    out = _COOKIE_HEADER.sub(lambda m: f"{m.group('k')}{REDACTED}", out)
    out = _AUTH_HEADER.sub(
        lambda m: f"{m.group('k')}{(m.group('scheme') + ' ') if m.group('scheme') else ''}{REDACTED}", out
    )
    out = _KEY_VALUE.sub(_mask_key_value, out)
    return _DISCORD_WEBHOOK.sub(lambda m: f"{m.group(1)}{REDACTED}", out)


def _key_words(key: str) -> list[str]:
    return [w.lower() for w in _WORD_SPLIT.split(key) if w]


def is_sensitive_key(key: str) -> bool:
    words = _key_words(key)
    if len(words) > 1 and any(w in _NON_SECRET_QUALIFIERS for w in words):
        return False
    if any(w in _SENSITIVE_WORDS for w in words):
        return True
    return any(pair in _SENSITIVE_PAIRS for pair in itertools.pairwise(words))


def _is_secret_like(value: Any) -> bool:  # noqa: ANN401 - inspects arbitrary values
    """Numbers, booleans and empty values under a sensitive-looking key (e.g. token_count) are not secrets."""
    if value is None or isinstance(value, (bool, int, float)):
        return False
    return not (isinstance(value, str) and value == "")


def redact(value: Any, *, _depth: int = 0) -> Any:  # noqa: ANN401 - structurally generic by design
    """Recursively redact mappings, sequences and strings. Non-container scalars pass through."""
    if _depth > 32:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            k: (
                REDACTED
                if isinstance(k, str) and is_sensitive_key(k) and _is_secret_like(v)
                else redact(v, _depth=_depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, _depth=_depth + 1) for v in value)
    return value
