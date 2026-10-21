"""Secret redaction for logs, traces and stored transcripts (PROMPT §1.5).

Two layers: (1) any mapping key whose name suggests a secret is masked; (2) any string value is scanned for
secret-shaped substrings (provider key prefixes, bearer tokens, credentials in URLs, ``api_key=`` query params).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd)?|secret|token|api[_-]?key|apikey|authorization|auth[_-]?header|cookie|session[_-]?id|"
    r"private[_-]?key|signing[_-]?key|dsn|credential)",
    re.IGNORECASE,
)

_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic keys
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),  # generic sk- keys
    re.compile(r"\b(?:pdl|apikey)_[A-Za-z0-9_]{16,}"),  # Paddle-style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"),  # Telegram bot tokens
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{12,}"),  # bearer tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^:/@\s]+):(?P<pw>[^@/\s]+)@")
_QUERY_SECRET = re.compile(
    r"(?P<k>(?:api[_-]?key|apikey|token|access_token|secret|signature|sig|key)=)(?P<v>[^&\s#]+)", re.IGNORECASE
)
_DISCORD_WEBHOOK = re.compile(r"(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+")


def redact_text(text: str) -> str:
    """Mask secret-shaped substrings inside free text."""
    out = text
    for pattern in _VALUE_PATTERNS:
        out = pattern.sub(REDACTED, out)
    out = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{REDACTED}@", out)
    out = _QUERY_SECRET.sub(lambda m: f"{m.group('k')}{REDACTED}", out)
    return _DISCORD_WEBHOOK.sub(lambda m: f"{m.group(1)}{REDACTED}", out)


def is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key))


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
                if isinstance(k, str) and is_sensitive_key(k) and v not in (None, "")
                else redact(v, _depth=_depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, _depth=_depth + 1) for v in value)
    return value
