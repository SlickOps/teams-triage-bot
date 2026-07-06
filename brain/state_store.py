"""Interview state persistence (docs/phase-2-llm-interview.md: "Interview state
in Cosmos/Table: TTL, resumable, one active interview per user.")

Table Storage layout: PartitionKey is fixed ("interview") so all interview rows
land in one partition (fine at this POC's scale); RowKey is the sanitized user
id. Keying by user id is what gives us "one active interview per user" for
free -- there is at most one row per user, so a new interview simply overwrites
the old one. The whole InterviewState is serialized as JSON into a single
string property ("data") rather than modeled as separate table columns: the
transcript is a nested list and Table Storage entities are flat, and the
whole blob is tiny (well under the ~1MB entity limit) so there's no reason to
fight the flat schema.

Graceful degradation (docs/00-overview.md guardrail #8): if STATE_STORAGE_ACCOUNT
isn't set, fall back to an in-process dict with the same async interface. This
keeps local runs (and any infra misconfiguration) from hard-crashing the brain,
at the cost of losing interview state on every pod restart -- acceptable for a
POC fallback, not for the real path.
"""
import logging
import os
import re
import time

from pydantic import BaseModel, Field

from intake import Intake, IntakeField

logger = logging.getLogger("brain.state_store")

_PARTITION_KEY = "interview"
_TABLE_NAME = os.environ.get("INTERVIEW_TABLE_NAME", "interviews")
_STORAGE_ACCOUNT = os.environ.get("STATE_STORAGE_ACCOUNT")

TTL_SECONDS = 24 * 60 * 60  # 24h default TTL for an in-flight or completed interview

# Table Storage keys can't contain '/', '\\', '#', '?', or control characters.
_ROWKEY_UNSAFE_RE = re.compile(r"[/\\#?\x00-\x1f\x7f]")


def _sanitize_row_key(user_id: str) -> str:
    return _ROWKEY_UNSAFE_RE.sub("_", user_id)


def _empty_intake() -> Intake:
    return Intake(
        environment=IntakeField(),
        service=IntakeField(),
        jenkins_job_url=IntakeField(),
        symptom=IntakeField(),
    )


class InterviewState(BaseModel):
    user_id: str
    status: str = "interviewing"  # "interviewing" | "complete"
    transcript: list[dict] = Field(default_factory=list)  # [{"role": "user"|"bot", "text": str}, ...]
    intake: Intake = Field(default_factory=_empty_intake)
    card_sent: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0


class StateStore:
    """Async interview-state store backed by Azure Table Storage, with a lazy
    TTL (checked on read; best-effort delete of expired rows) rather than a
    server-side TTL feature (Table Storage has none)."""

    def __init__(self):
        self._table_client = None  # lazily created; see _client()

    async def _client(self):
        if self._table_client is None:
            # Imported lazily so importing this module doesn't require the
            # azure-data-tables package unless the Table-backed path is used
            # (mirrors the lazy-import style already used in reply.py).
            from azure.data.tables.aio import TableServiceClient
            from azure.identity.aio import DefaultAzureCredential

            endpoint = f"https://{_STORAGE_ACCOUNT}.table.core.windows.net"
            service_client = TableServiceClient(endpoint=endpoint, credential=DefaultAzureCredential())
            try:
                await service_client.create_table(_TABLE_NAME)
            except Exception:
                # Table already exists (or a transient race creating it) -- either
                # way, proceed; a real failure will surface on the next call.
                logger.debug("create_table(%s) did not create a new table (likely exists)", _TABLE_NAME)
            self._table_client = service_client.get_table_client(_TABLE_NAME)
        return self._table_client

    async def get(self, user_id: str) -> InterviewState | None:
        client = await self._client()
        row_key = _sanitize_row_key(user_id)
        try:
            entity = await client.get_entity(partition_key=_PARTITION_KEY, row_key=row_key)
        except Exception:
            return None
        state = InterviewState.model_validate_json(entity["data"])
        if state.expires_at and state.expires_at < time.time():
            logger.info("interview state for %s expired; treating as absent", user_id)
            try:
                await client.delete_entity(partition_key=_PARTITION_KEY, row_key=row_key)
            except Exception:
                logger.exception("failed to delete expired interview state for %s", user_id)
            return None
        return state

    async def put(self, state: InterviewState) -> None:
        client = await self._client()
        now = time.time()
        state.updated_at = now
        if not state.created_at:
            state.created_at = now
        state.expires_at = now + TTL_SECONDS
        entity = {
            "PartitionKey": _PARTITION_KEY,
            "RowKey": _sanitize_row_key(state.user_id),
            "data": state.model_dump_json(),
        }
        await client.upsert_entity(entity)

    async def delete(self, user_id: str) -> None:
        client = await self._client()
        try:
            await client.delete_entity(partition_key=_PARTITION_KEY, row_key=_sanitize_row_key(user_id))
        except Exception:
            logger.debug("delete of interview state for %s: nothing to delete", user_id)


class _InMemoryStateStore:
    """Fallback used when STATE_STORAGE_ACCOUNT is unset. Same async interface
    as StateStore, but non-persistent (lost on process restart) and scoped to
    this pod only -- fine for local dev, not a substitute for Table Storage in
    a real deployment."""

    def __init__(self):
        self._rows: dict[str, InterviewState] = {}

    async def get(self, user_id: str) -> InterviewState | None:
        state = self._rows.get(user_id)
        if state is None:
            return None
        if state.expires_at and state.expires_at < time.time():
            self._rows.pop(user_id, None)
            return None
        return state

    async def put(self, state: InterviewState) -> None:
        now = time.time()
        state.updated_at = now
        if not state.created_at:
            state.created_at = now
        state.expires_at = now + TTL_SECONDS
        self._rows[state.user_id] = state

    async def delete(self, user_id: str) -> None:
        self._rows.pop(user_id, None)


def build_state_store():
    """Return the configured StateStore, or the in-memory fallback if
    STATE_STORAGE_ACCOUNT is unset."""
    if not _STORAGE_ACCOUNT:
        logger.warning(
            "STATE_STORAGE_ACCOUNT is unset -- falling back to a non-persistent "
            "in-process interview state store. Interview state will NOT survive "
            "a pod restart."
        )
        return _InMemoryStateStore()
    return StateStore()
