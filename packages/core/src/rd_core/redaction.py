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
# user:password@ — the user part may be empty (redis://:password@host).
# The password may itself contain "@" or "/": it runs to the last "@" before the host.
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^:/@\s]*):(?P<pw>\S+)@(?=[^@\s]*(?:\s|$))"
)
# Key-only URL credentials such as Sentry DSNs (https://<hex key>@host/project).
_URL_KEY_ONLY = re.compile(r"(?P<scheme>https?://)[0-9a-f]{32}@")
_QUERY_SECRET = re.compile(
    r"(?<![A-Za-z0-9])(?P<k>(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|client[_-]?secret|"
    r"secret|password|signature|sig|key)=)(?P<v>[^&\s#]+)",
    re.IGNORECASE,
)
# "Authorization: Basic abc", "x-api-key: abc", '"password": "abc"', "AWS_SECRET_ACCESS_KEY=abc" in free text.
_AUTH_HEADER = re.compile(
    r"(?i)(?P<k>\b(?:proxy-)?authorization\b[\"']?\s*[:=]\s*[\"']?)(?:(?P<scheme>basic|bearer|token|digest)\s+)?[^\s\"',;]+"
)
_KEY_VALUE = re.compile(
    r"(?i)(?P<k>(?P<q>[\"']?)\b(?:x-api-key|api[_-]?key|apikey|aws_secret_access_key|client[_-]?secret|"
    r"secret[_-]?key|password|passwd|passphrase|pass|pgpassword|secret|token|[a-z0-9]+_(?:password|passwd|auth|"
    r"token|secret))\b(?P=q)\s*[:=]\s*)(?P<v>\"[^\"]*\"|'[^']*'|[^\s\"',;&]+)"
)
_COOKIE_HEADER = re.compile(r"(?im)(?P<k>\b(?:set-)?cookie\s*:\s*)[^\r\n]+")
_DISCORD_WEBHOOK = re.compile(r"(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+")


def _mask_key_value(m: re.Match[str]) -> str:
    value = m.group("v")
    quote = value[0] if value[:1] in ("'", '"') else ""
    return f"{m.group('k')}{quote}{REDACTED}{quote}"


def redact_text(text: str) -> str:
    """Mask secret-shaped substrings inside free text."""
    out = text
    for pattern in _VALUE_PATTERNS:
        out = pattern.sub(REDACTED, out)
    out = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{REDACTED}@", out)
    out = _URL_KEY_ONLY.sub(lambda m: f"{m.group('scheme')}{REDACTED}@", out)
    out = _QUERY_SECRET.sub(lambda m: f"{m.group('k')}{REDACTED}", out)
    out = _AUTH_HEADER.sub(
        lambda m: f"{m.group('k')}{(m.group('scheme') + ' ') if m.group('scheme') else ''}{REDACTED}", out
    )
    out = _KEY_VALUE.sub(_mask_key_value, out)
    out = _COOKIE_HEADER.sub(lambda m: f"{m.group('k')}{REDACTED}", out)
    return _DISCORD_WEBHOOK.sub(lambda m: f"{m.group(1)}{REDACTED}", out)


def _key_words(key: str) -> list[str]:
    return [w.lower() for w in _WORD_SPLIT.split(key) if w]


def is_sensitive_key(key: str) -> bool:
    words = _key_words(key)
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
