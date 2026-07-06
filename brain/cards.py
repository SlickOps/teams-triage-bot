"""The Adaptive Card that collects the interview essentials, and the matching
parser for its Action.Submit payload.

Action.Submit (not Action.Execute) is deliberate: Teams delivers an
Action.Submit as a normal `type=="message"` activity with `.value` populated,
which passes the relay's activity-type filter (docs/00-overview.md's edge
relay only forwards `message` activities) completely unchanged. Action.Execute
uses `invoke` activities, which the relay was never built to forward.
"""

_FIELD_IDS = ("environment", "service", "jenkins_job_url", "symptom")


def build_intake_card() -> dict:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": "Let's get this triaged. Fill in what you can -- leave a "
                "field blank only if it genuinely doesn't apply (say why in the "
                "chat and I'll take it from there).",
                "wrap": True,
            },
            {
                "type": "Input.Text",
                "id": "environment",
                "label": "Environment",
                "placeholder": "e.g. dev37",
            },
            {
                "type": "Input.Text",
                "id": "service",
                "label": "Service / component",
                "placeholder": "e.g. checkout-api",
            },
            {
                "type": "Input.Text",
                "id": "jenkins_job_url",
                "label": "Jenkins job URL",
                "placeholder": "link to the job/build involved",
            },
            {
                "type": "Input.Text",
                "id": "symptom",
                "label": "Symptom / error text",
                "placeholder": "what's actually happening -- error text, behavior, etc.",
                "isMultiline": True,
            },
        ],
        "actions": [
            {
                "type": "Action.Submit",
                "title": "Submit",
            }
        ],
    }


def parse_card_submit(value: dict) -> dict:
    """Given `activity.value` from a submitted intake card, return
    {field_name: text} for the non-empty fields among the four essentials.
    Trims whitespace and drops empty strings so callers don't need to."""
    if not isinstance(value, dict):
        return {}
    answers = {}
    for field_id in _FIELD_IDS:
        raw = value.get(field_id)
        if isinstance(raw, str):
            trimmed = raw.strip()
            if trimmed:
                answers[field_id] = trimmed
    return answers
