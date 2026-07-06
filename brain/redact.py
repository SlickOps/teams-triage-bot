"""Output redaction pass (docs/00-overview.md guardrail #6): regex-scrub
token-shaped strings before anything is user-visible. Belt-and-suspenders --
creds shouldn't be flowing through the model or transcript in the first place,
but a pasted log or an unlucky model completion could still surface one.

Kept conservative on purpose: we'd rather miss an exotic secret shape than
mangle a normal sentence containing e.g. a long filename.

Examples (also exercised by any brain/tests we add later):
    >>> redact("here is my token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQ")
    'here is my token: [redacted]'
    >>> redact("Authorization: Bearer abc123.def456")
    'Authorization: [redacted]'
    >>> redact("aws key AKIAABCDEFGHIJKLMNOP looks live")
    'aws key [redacted] looks live'
    >>> redact("password=hunter2hunter2hunter2hunter2hunter2 in the config")
    'password=[redacted] in the config'
    >>> redact("dev37 test is broken, service checkout-api")
    'dev37 test is broken, service checkout-api'
"""
import re

# JWT-shaped: three base64url segments separated by dots.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")

# "Bearer <token>" auth headers.
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+\b", re.IGNORECASE)

# key=..., password=..., token=..., secret=... assignments (quoted or bare).
_KV_SECRET_RE = re.compile(
    r"\b(key|password|token|secret)\s*=\s*['\"]?[^\s'\"]{6,}['\"]?",
    re.IGNORECASE,
)

# AWS access key IDs.
_AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

# Generic long hex/base64url-ish blob (>=32 chars) -- catches most other secrets
# (API keys, session ids) without touching ordinary words or short identifiers.
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")


def redact(text: str) -> str:
    """Scrub token-shaped strings from `text`, replacing them with '[redacted]'."""
    if not text:
        return text
    out = _JWT_RE.sub("[redacted]", text)
    out = _BEARER_RE.sub("[redacted]", out)
    out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted]", out)
    out = _AWS_KEY_RE.sub("[redacted]", out)
    out = _LONG_TOKEN_RE.sub("[redacted]", out)
    return out
