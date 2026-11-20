"""Untrusted history sanitization, wrapper stripping, secret redaction, and truncation."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

TRUST_BOUNDARY = "untrusted_inert_history"

_WRAPPER_RE = re.compile(
    r"<(system-reminder|local-command-caveat|environment_context|turn_aborted|ide_opened_file|command-name)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)

# Order matters: strip multi-line PEM blocks first, then specific token formats, then generic KEY=value pairs.
_SECRET_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        "[REDACTED]",
    ),
    (
        re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{12,}"),
        "[REDACTED]",
    ),
    (
        re.compile(r"\bgh[po]_[A-Za-z0-9_]{20,}"),
        "[REDACTED]",
    ),
    (
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
        "[REDACTED]",
    ),
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED]",
    ),
    (
        re.compile(r"\bBearer\s+(?!\[REDACTED\])[A-Za-z0-9\-._~+/]+=*"),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD))\s*([:=])\s*(?!Bearer\s+\[REDACTED\]|\[REDACTED\])(\"[^\"]+\"|'[^']+'|[^\s,;]+)"
        ),
        r"\1\2[REDACTED]",
    ),
)


def sanitize_text(value: Any) -> str:
    """Replace Cc/Cs Unicode control characters (except \\n and \\t) with U+FFFD."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return "".join(
        "\ufffd" if ch not in ("\n", "\t") and unicodedata.category(ch) in ("Cc", "Cs") else ch
        for ch in text
    )


def strip_wrappers(text: str) -> str:
    """Remove injected envelope tag blocks while preserving surrounding user prose."""
    stripped = _WRAPPER_RE.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def redact_secrets(text: str) -> tuple[str, int]:
    """Redact PEM private keys, API/auth tokens, and secret assignments, returning (text, count)."""
    cleaned = text
    total = 0
    for pattern, replacement in _SECRET_RULES:
        cleaned, count = pattern.subn(replacement, cleaned)
        total += count
    return cleaned, total


def clean_text(value: Any, *, strip_tags: bool = True) -> tuple[str, int]:
    """Sanitize control characters, optionally strip wrapper tags, and redact secrets."""
    cleaned = sanitize_text(value)
    if strip_tags:
        cleaned = strip_wrappers(cleaned)
    return redact_secrets(cleaned)


def truncate_text(text: str, limit: int, *, single_line: bool = False) -> tuple[str, bool]:
    """Truncate text to at most `limit` characters plus '...' when exceeded."""
    normalized = " ".join(text.split()) if single_line else text
    if limit <= 0:
        return "", bool(normalized)
    if len(normalized) <= limit:
        return normalized, False
    return f"{normalized[:limit].rstrip()}...", True
