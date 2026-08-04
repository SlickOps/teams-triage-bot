"""Offline self-check for the interview-state lifecycle in app.py.handle_envelope.

Companion to selfcheck_phase3.py, split out to keep that file lean. Where the
phase-3 check is content-focused (report models, redaction, injection), this one
is control-flow-focused: it drives the REAL handle_envelope (plus the interview /
investigation steps) against fakes and asserts the lifecycle routing that the
code review flagged as fragile:

  1. A fresh free-text turn sends the intake card and stamps both durable
     activity ids (start + complete).
  2. Redelivery of an already-completed activity is a no-op -- the durable
     marker on the state row survives the in-memory dedup cache being cold
     (the bug where a pod restart re-ran a finished interview).
  3. Redelivery of a started-but-not-completed activity resumes WITHOUT
     re-appending the user turn (transcript double-append bug).
  4. A follow-up on an already-investigated interview routes to run_followup,
     not a fresh interview.
  5. A "new issue"-shaped message after a terminal interview starts fresh.
  6. A stale card submit on a terminal interview does NOT merge its previous-
     incident field values.
  7. "retry" after a failed investigation re-runs it from the stored summary;
     any other message just nudges (no surprise re-run).

app.py imports Bot Framework / Service Bus packages that aren't installed on a
dev box (same constraint as selfcheck_phase3, which only py_compiles app.py). We
stub those container-only modules in sys.modules BEFORE importing app so the
pure-Python lifecycle logic can run here with no network and no Foundry.

Run: `python brain/selfcheck_lifecycle.py`. Exits 0 on success, 1 on failure.
"""
import asyncio
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

_BRAIN_DIR = Path(__file__).parent
sys.path.insert(0, str(_BRAIN_DIR))

# Env that app.py (and the modules it imports) read at import time. Values are
# irrelevant -- this check never opens a socket or calls Foundry.
os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://selfcheck.invalid")
os.environ.setdefault("FOUNDRY_MODEL", "selfcheck-model")
os.environ.setdefault("SERVICEBUS_FULLY_QUALIFIED_NAMESPACE", "selfcheck.invalid")
os.environ.setdefault("BOT_TENANT_ID", "TENANT")
os.environ.setdefault("BOT_APP_ID", "APP")

# What send_text/send_card recorded this scenario: [(kind, text), ...].
_SENT: list[tuple[str, str]] = []


def _install_stubs() -> None:
    """Register fake container-only modules so `import app` succeeds offline."""
    for name in [
        "azure", "azure.identity", "azure.identity.aio", "azure.servicebus",
        "azure.servicebus.aio", "botbuilder", "botbuilder.core",
        "botbuilder.schema", "botframework", "botframework.connector",
        "botframework.connector.auth",
    ]:
        sys.modules[name] = types.ModuleType(name)

    sys.modules["azure.identity.aio"].DefaultAzureCredential = object
    sys.modules["azure.servicebus.aio"].ServiceBusClient = object

    class _TurnContext:
        @staticmethod
        def remove_recipient_mention(activity):
            return getattr(activity, "text", None)

    class _Activity:
        @classmethod
        def deserialize(cls, d):
            o = SimpleNamespace(**d)
            conv = d.get("conversation") or {}
            o.conversation = SimpleNamespace(**conv) if isinstance(conv, dict) else conv
            fp = d.get("from_property") or {}
            o.from_property = SimpleNamespace(**fp) if isinstance(fp, dict) else fp
            return o

    sys.modules["botbuilder.core"].TurnContext = _TurnContext
    sys.modules["botbuilder.schema"].Activity = _Activity
    sys.modules["botframework.connector.auth"].ClaimsIdentity = lambda **kw: SimpleNamespace(**kw)

    # Stub reply (it imports Bot Framework at module top). send_text/send_card
    # just record what would have been sent to Teams.
    reply = types.ModuleType("reply")
    reply.build_adapter = lambda: None

    async def _send_text(_tc, text):
        _SENT.append(("text", text))

    async def _send_card(_tc, _card):
        _SENT.append(("card", "<card>"))

    reply.send_text = _send_text
    reply.send_card = _send_card
    sys.modules["reply"] = reply


_install_stubs()

import app  # noqa: E402
from intake import StructuredSummary  # noqa: E402
from state_store import InterviewState, _InMemoryStateStore  # noqa: E402

# app configures INFO logging at import; quiet it so the check output is just
# the PASS/FAIL lines (we assert on state, not on log spam).
import logging  # noqa: E402
logging.getLogger("brain").setLevel(logging.WARNING)


# --- fakes for the Foundry-backed collaborators (patched onto the app module) --
async def _fake_run_turn(state):
    # Never "enough": just asks another question, so the interview stays open
    # (reaching completion needs a real model; not what these checks exercise).
    return SimpleNamespace(
        intake=state.intake, reply_to_user="Which service?",
        enough_to_be_useful=False, summary=None,
    )


async def _fake_run_followup(_state, user_text):
    _SENT.append(("followup_called", user_text))
    return "Per the report, the hypothesis was a bad deploy."


async def _fake_run_investigation(summary):
    _SENT.append(("investigation_called", f"{summary.environment}/{summary.service}"))
    from investigation import InvestigationReport
    return InvestigationReport(
        environment=summary.environment, service=summary.service,
        impact="users see 500s", hypothesis="bad deploy", change="deploy #42",
        evidence=[], tools_unavailable=[], injection_flagged=False,
    )


app.run_turn = _fake_run_turn
app.run_followup = _fake_run_followup
app.run_investigation = _fake_run_investigation


class _FakeAdapter:
    async def process_proactive(self, _claims_identity, _activity, _audience, callback):
        await callback(SimpleNamespace())


_ADAPTER = _FakeAdapter()


def _envelope(activity_id, text=None, value=None):
    return {
        "activity": {
            "id": activity_id, "type": "message", "channel_id": "msteams",
            "text": text, "value": value,
            "channel_data": {"tenant": {"id": "TENANT"}},
            "from_property": {"id": "user-1", "aad_object_id": "user-1"},
            "conversation": {"tenant_id": "TENANT"},
        },
        "claims": {"aud": "APP"},
        "audience": "aud",
    }


def _fresh_scenario() -> _InMemoryStateStore:
    """A clean store + cold dedup cache + empty send log for one scenario."""
    _SENT.clear()
    app._seen_activity_ids.clear()
    return _InMemoryStateStore()


def _summary_json(environment="prod", service="api") -> str:
    return StructuredSummary(
        environment=environment, service=service, jenkins_job_url=None,
        symptom="500s", what_we_know="x",
    ).model_dump_json()


# --- checks -------------------------------------------------------------------
async def _check_fresh_interview_stamps_ids() -> None:
    store = _fresh_scenario()
    await app.handle_envelope(_ADAPTER, store, _envelope("a1", text="prod is down"))
    st = await store.get("user-1")
    assert ("card", "<card>") in _SENT, "fresh interview must send the intake card"
    assert st.status == "interviewing"
    assert st.last_started_activity_id == "a1"
    assert st.last_completed_activity_id == "a1", "completed marker must be stamped"
    user_turns = [t for t in st.transcript if t["role"] == "user"]
    assert len(user_turns) == 1, f"expected one user turn, got {user_turns}"


async def _check_durable_dedup_replay() -> None:
    store = _fresh_scenario()
    await app.handle_envelope(_ADAPTER, store, _envelope("a1", text="prod is down"))
    # Simulate a pod restart: the in-memory dedup cache is lost, but the durable
    # marker on the state row must still make a redelivery of a1 a no-op.
    app._seen_activity_ids.clear()
    _SENT.clear()
    await app.handle_envelope(_ADAPTER, store, _envelope("a1", text="prod is down"))
    assert _SENT == [], f"redelivery of a completed activity must not re-send: {_SENT}"


async def _check_resume_no_double_append() -> None:
    store = _fresh_scenario()
    seed = InterviewState(user_id="user-1", status="interviewing", card_sent=True)
    seed.transcript = [{"role": "user", "text": "earlier turn"}]
    seed.last_started_activity_id = "a2"  # started, crashed before completion
    await store.put(seed)
    await app.handle_envelope(_ADAPTER, store, _envelope("a2", text="earlier turn"))
    st = await store.get("user-1")
    user_turns = [t for t in st.transcript if t["role"] == "user"]
    assert len(user_turns) == 1, f"resumed activity must not double-append: {user_turns}"


async def _check_followup_routes_to_followup() -> None:
    store = _fresh_scenario()
    inv = InterviewState(user_id="user-1", status="investigated")
    inv.intake.environment = app.IntakeField(value="prod")
    inv.structured_summary = _summary_json()
    inv.investigation_report = "{}"
    await store.put(inv)
    await app.handle_envelope(_ADAPTER, store, _envelope("a3", text="what was the hypothesis?"))
    assert any(k == "followup_called" for k, _ in _SENT), "same-context question must route to run_followup"
    st = await store.get("user-1")
    assert st.status == "investigated", "a follow-up must not change interview status"


async def _check_new_incident_resets() -> None:
    store = _fresh_scenario()
    inv = InterviewState(user_id="user-1", status="investigated")
    inv.intake.environment = app.IntakeField(value="prod")
    inv.structured_summary = _summary_json()
    inv.investigation_report = "{}"
    await store.put(inv)
    await app.handle_envelope(_ADAPTER, store, _envelope("a4", text="new issue in staging"))
    st = await store.get("user-1")
    assert ("card", "<card>") in _SENT, "a new incident must start a fresh interview (new card)"
    assert st.status == "interviewing"
    assert st.structured_summary is None, "fresh state must not carry the old summary"


async def _check_stale_card_not_merged() -> None:
    store = _fresh_scenario()
    inv = InterviewState(user_id="user-1", status="investigated")
    inv.investigation_report = "{}"
    await store.put(inv)
    await app.handle_envelope(
        _ADAPTER, store, _envelope("a5", value={"environment": "OLDENV", "service": "OLDSVC"}))
    st = await store.get("user-1")
    assert any(k == "text" and "earlier report" in v for k, v in _SENT), "stale card must get the nudge line"
    assert st.intake.environment.value in (None, ""), "stale card fields must NOT be merged into intake"
    assert st.status == "investigated", "a stale card must not change interview status"


async def _check_retry_reruns_investigation() -> None:
    store = _fresh_scenario()
    comp = InterviewState(user_id="user-1", status="complete")  # investigation had failed
    comp.structured_summary = _summary_json()
    await store.put(comp)
    await app.handle_envelope(_ADAPTER, store, _envelope("a6", text="retry"))
    assert any(k == "investigation_called" for k, _ in _SENT), "'retry' must re-run the investigation"
    st = await store.get("user-1")
    assert st.status == "investigated", "a successful retry must advance status"


async def _check_nudge_on_complete_nonretry() -> None:
    store = _fresh_scenario()
    comp = InterviewState(user_id="user-1", status="complete")
    comp.structured_summary = _summary_json()
    await store.put(comp)
    await app.handle_envelope(_ADAPTER, store, _envelope("a7", text="ok thanks"))
    assert not any(k == "investigation_called" for k, _ in _SENT), "non-retry text must NOT re-run investigation"
    assert any(k == "text" and "couldn't finish" in v for k, v in _SENT), "a non-retry message should nudge"


_FAILURES: list[str] = []


def _check(name: str, coro_fn) -> None:
    try:
        asyncio.run(coro_fn())
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 - surface the raw failure
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        _FAILURES.append(name)


def main() -> int:
    print("Interview-lifecycle offline self-check (no network, no Foundry)\n")
    _check("fresh interview sends card + stamps activity ids", _check_fresh_interview_stamps_ids)
    _check("durable dedup: replay of a completed activity is a no-op", _check_durable_dedup_replay)
    _check("resume: started-but-not-completed activity not double-appended", _check_resume_no_double_append)
    _check("investigated + same-context question routes to follow-up", _check_followup_routes_to_followup)
    _check("investigated + 'new issue' starts a fresh interview", _check_new_incident_resets)
    _check("stale card submit on terminal state is not merged", _check_stale_card_not_merged)
    _check("'retry' re-runs a failed investigation from stored summary", _check_retry_reruns_investigation)
    _check("non-retry message on 'complete' nudges, no re-run", _check_nudge_on_complete_nonretry)

    print()
    if _FAILURES:
        print(f"FAILED: {', '.join(_FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
