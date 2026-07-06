"""Typed intake, adaptive gate, and structured summary models for the Phase 2
interview. These are the Pydantic models MAF fills in via structured output
(`agent.run(..., response_format=InterviewTurn)`).

Optional-with-reason semantics: every intake field is (value, na_reason) rather
than a single optional string. That distinction is the whole point of the
"adaptive gate" from docs/phase-2-llm-interview.md -- a null value means "not
answered yet, keep asking"; a null value WITH an na_reason means "the reporter
gave a justified reason this doesn't apply / can't be obtained (e.g. 'Jenkins
is down')" and the interviewer must stop asking about that field. The gate
(`enough_to_be_useful`) is evaluated over the whole Intake, not by requiring
every field to be non-null -- see SYSTEM_PROMPT in interviewer.py.

Guardrail note (docs/00-overview.md #3, "no autonomous root-cause claims"):
StructuredSummary deliberately has NO root_cause field. Only what the reporter
told us, cited back, plus (optionally) open questions that are still fuzzy.
"""
from pydantic import BaseModel, Field


class IntakeField(BaseModel):
    value: str | None = Field(default=None, description="the answer if provided, else null")
    na_reason: str | None = Field(
        default=None,
        description=(
            "justification if this field is genuinely N/A or unobtainable "
            "(e.g. 'Jenkins is down'); null otherwise"
        ),
    )


class Intake(BaseModel):
    environment: IntakeField  # which environment is broken (e.g. dev37, test)
    service: IntakeField  # which service / component is affected
    jenkins_job_url: IntakeField  # link to the Jenkins job/build involved
    symptom: IntakeField  # concrete symptom or error text


class StructuredSummary(BaseModel):
    environment: str
    service: str
    jenkins_job_url: str | None
    symptom: str
    what_we_know: str  # concise facts gathered from the reporter, cited back
    open_questions: str | None = None  # anything still fuzzy but accepted under the adaptive gate
    # DELIBERATELY no root_cause field -- guardrail: never declare root cause.


class InterviewTurn(BaseModel):
    """The agent's decision for ONE turn of the interview."""

    intake: Intake
    enough_to_be_useful: bool  # the adaptive gate: do we have enough to be useful?
    reply_to_user: str  # if not enough: the next grilling follow-up. if enough: a short closing line.
    summary: StructuredSummary | None = None  # populated iff enough_to_be_useful is True
