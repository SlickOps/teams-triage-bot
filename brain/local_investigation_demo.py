"""Run ONE investigation locally against the real Foundry model + local mock MCP
servers, and print the rendered report. This is the loop for iterating on the
investigator prompt / report format WITHOUT deploying to Azure each time.

Setup (one-time):
    python3.12 -m venv .venv            # agent-framework needs Python >= 3.10
    .venv/bin/pip install -r brain/requirements.txt -r mock-mcp/requirements.txt
    az login                            # DefaultAzureCredential uses this for Foundry

Run:
    ./mock-mcp/run_local.sh             # starts jenkins/datadog/argocd on 8801-8803
    export FOUNDRY_PROJECT_ENDPOINT="https://aif-triage-poc-567b31.services.ai.azure.com/"
    export FOUNDRY_MODEL="gpt-5-mini"
    export JENKINS_MCP_URL=http://127.0.0.1:8801/mcp
    export DATADOG_MCP_URL=http://127.0.0.1:8802/mcp
    export ARGOCD_MCP_URL=http://127.0.0.1:8803/mcp
    .venv/bin/python brain/local_investigation_demo.py dev14 frontend "500s after a deploy"

Args: [environment] [service] [symptom]  (all optional; sensible defaults below).

NOTE: the render here mirrors app.py's _render_investigation_markdown -- keep the
two in sync. (app.py can't be imported directly: it reads Bot Framework env vars
at import time that this offline-of-Teams harness has no reason to set.)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from intake import StructuredSummary
from investigation import InvestigationReport, run_investigation


def render(r: InvestigationReport) -> str:
    lines = [f"**Investigation: {r.environment} / {r.service}**"]
    if r.change:
        lines.append(f"**Deploy/change:** {r.change}")
    lines.append(f"**Impact:** {r.impact}")
    lines.append(f"**Hypothesis (unconfirmed):** {r.hypothesis}")
    if r.evidence:
        cites = " · ".join(f"[{e.label}]({e.ref})" if e.ref else e.label for e in r.evidence)
        lines.append(f"**Evidence:** {cites}")
    if r.injection_flagged:
        lines.append("⚠️ A tool log contained a prompt-injection attempt; treated as data, not acted on.")
    if r.tools_unavailable:
        lines.append(f"**Tools unavailable:** {', '.join(r.tools_unavailable)}")
    return "\n\n".join(lines)


async def main() -> None:
    env = sys.argv[1] if len(sys.argv) > 1 else "dev14"
    service = sys.argv[2] if len(sys.argv) > 2 else "frontend"
    symptom = sys.argv[3] if len(sys.argv) > 3 else f"{env} {service} throwing 500s after a deploy"

    summary = StructuredSummary(
        environment=env,
        service=service,
        jenkins_job_url=None,
        symptom=symptom,
        what_we_know=f"Reporter says {service} on {env} broke after a deploy someone else ran.",
        open_questions="No Jenkins job URL available.",
    )
    report = await run_investigation(summary)
    msg = render(report)
    print("\n" + "=" * 64)
    print(msg)
    print("=" * 64)
    print(f"\n[metrics] chars={len(msg)}  evidence={len(report.evidence)}  "
          f"injection_flagged={report.injection_flagged}  tools_unavailable={report.tools_unavailable}")


if __name__ == "__main__":
    asyncio.run(main())
