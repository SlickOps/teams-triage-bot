"""Interview lifecycle helpers: when free-text is a *new* incident vs a
continuation of the current one (retry investigation / follow-up Q&A).

POC approach: cheap heuristics only -- no extra model call. A free-text turn
counts as a new incident when:

  1. The user uses an explicit "new issue"-style phrase, OR
  2. The text names a different environment than the one already on the
     active interview / completed summary.

Otherwise the turn continues the current lifecycle state (see handle_envelope
in app.py). Keep this module free of network I/O so selfcheck can cover it.
"""
from __future__ import annotations

import re

from intake import StructuredSummary
from state_store import InterviewState

# Explicit new-incident language. Keep conservative: we prefer continuing
# (retry / follow-up) over accidentally wiping a good interview.
_NEW_ISSUE_RE = re.compile(
    r"\b("
    r"new\s+(issue|problem|incident|report|bug|ticket)|"
    r"another\s+(issue|problem|incident|bug|report)|"
    r"different\s+(issue|problem|incident|env(?:ironment)?|service)|"
    r"start\s+over|"
    r"fresh\s+(issue|report|incident)|"
    r"unrelated\s+(issue|problem|incident)?|"
    r"something\s+else\b|"
    r"separate\s+(issue|problem|incident)"
    r")\b",
    re.IGNORECASE,
)

# Explicit "retry the investigation" language. Only consulted when an interview
# is "complete" but its investigation failed (status stayed "complete", no
# report) -- we retry ONLY when asked, never automatically, so a normal message
# in that state doesn't surprise the reporter with a fresh (costly) tool run.
_RETRY_RE = re.compile(
    r"\b("
    r"retry|"
    r"re-?run|"
    r"try\s+again|"
    r"investigate\s+again|"
    r"run\s+it\s+again|"
    r"take\s+another\s+(look|run|crack|shot)"
    r")\b",
    re.IGNORECASE,
)

# Environment-like tokens that show up in this org's triage chat.
# Not exhaustive; env mismatch only fires when BOTH sides have a token.
_ENV_TOKEN_RE = re.compile(
    r"\b("
    r"prod(?:uction)?|"
    r"staging|stage|"
    r"uat|"
    r"qa\d*|"
    r"test\d*|"
    r"dev\d+"
    r")\b",
    re.IGNORECASE,
)


def _norm_env(token: str) -> str:
    t = token.strip().lower()
    if t in {"production", "prod"}:
        return "prod"
    if t in {"staging", "stage"}:
        return "staging"
    return t


def known_environment(state: InterviewState) -> str | None:
    """Best-effort environment already established for this interview."""
    if state.structured_summary:
        try:
            summary = StructuredSummary.model_validate_json(state.structured_summary)
            if summary.environment and summary.environment.strip():
                return summary.environment.strip()
        except Exception:  # noqa: BLE001 - corrupt blob: fall through to intake
            pass
    value = state.intake.environment.value
    if value and value.strip():
        return value.strip()
    return None


def environment_tokens(text: str) -> set[str]:
    return {_norm_env(m.group(0)) for m in _ENV_TOKEN_RE.finditer(text or "")}


def is_retry_request(text: str) -> bool:
    """True if free-text explicitly asks to re-run a failed investigation."""
    return bool(text and _RETRY_RE.search(text))


def is_new_incident(text: str, state: InterviewState) -> bool:
    """Return True if free-text should open a brand-new InterviewState."""
    if not text or not text.strip():
        return False
    if _NEW_ISSUE_RE.search(text):
        return True

    known = known_environment(state)
    if not known:
        return False

    known_norm = _norm_env(known)
    # Also accept the full known string as an env token if it looks like one.
    known_tokens = environment_tokens(known) or {known_norm}
    mentioned = environment_tokens(text)
    if not mentioned:
        return False
    # New incident only when the user names at least one env and *none* of
    # those match the known environment (e.g. known=dev14, text mentions prod).
    return mentioned.isdisjoint(known_tokens)
