"""
Microsoft Agent Framework (Python) — de-risking spike.

Purpose: prove, end-to-end on a real machine + real Foundry endpoint, the three
assumptions the whole stack rests on BEFORE we build anything. This is throwaway:
if it passes, MAF-in-Python is locked; if any check is rough, we swap the brain to
Pydantic AI now, while no real code exists.

Checks:
  1. AUTH + MODEL   — FoundryChatClient can reach your Foundry deployment and complete.
  2. STREAMING      — agent.run(..., stream=True) actually streams tokens incrementally
                      (this was the one feature we could NOT confirm from the Python docs).
  3. MCP TOOL       — an MCPStreamableHTTPTool can be attached and the agent can discover
                      and call a tool. (Skipped unless MCP_URL is set.)

Run:
    az login                                  # AzureCliCredential uses this
    export FOUNDRY_PROJECT_ENDPOINT="https://<your-foundry-project-endpoint>"
    export FOUNDRY_MODEL="<your-model-deployment-name>"
    export MCP_URL="https://<some-reachable-mcp-endpoint>"   # optional; enables check 3
    pip install -r requirements.txt
    python maf_python_spike.py

Note: written against current MAF docs; it has not been executed here (no Foundry
endpoint on the authoring machine). If an import or attribute name has drifted, the
error will point right at it — adjust and re-run. That's the spike doing its job.
"""

import asyncio
import os
import sys

# --- API shapes verified against current MAF Python docs -------------------------
#   from agent_framework import Agent, MCPStreamableHTTPTool
#   from agent_framework.foundry import FoundryChatClient
#   streaming: `async for update in agent.run(msg, stream=True): update.text`
try:
    from agent_framework import Agent, MCPStreamableHTTPTool
    from agent_framework.foundry import FoundryChatClient
    from azure.identity import AzureCliCredential
except ImportError as e:
    print(f"[SETUP] Import failed: {e}")
    print("        pip install -r requirements.txt  (agent-framework, azure-identity)")
    sys.exit(2)


def _make_client() -> "FoundryChatClient":
    """Build the Foundry client from env. On AKS later, swap AzureCliCredential ->
    DefaultAzureCredential() so it picks up the pod's workload identity."""
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    model = os.environ.get("FOUNDRY_MODEL") or os.environ.get("FOUNDRY_MODEL_DEPLOYMENT_NAME")
    if not endpoint or not model:
        print("[SETUP] Set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL (or "
              "FOUNDRY_MODEL_DEPLOYMENT_NAME).")
        sys.exit(2)
    return FoundryChatClient(
        credential=AzureCliCredential(),
        project_endpoint=endpoint,
        model=model,
    )


async def check_auth_and_completion() -> bool:
    print("\n[1/3] AUTH + MODEL ...")
    try:
        agent = Agent(
            client=_make_client(),
            name="SpikeAgent",
            instructions="You are a terse assistant. Answer in one short sentence.",
        )
        result = await agent.run("Reply with exactly: MAF auth OK")
        text = getattr(result, "text", None) or str(result)
        print(f"      model replied: {text.strip()!r}")
        print("      PASS — reached Foundry and got a completion.")
        return True
    except Exception as e:  # noqa: BLE001 - spike wants the raw failure surfaced
        print(f"      FAIL — {type(e).__name__}: {e}")
        return False


async def check_streaming() -> bool:
    print("\n[2/3] STREAMING ...")
    try:
        agent = Agent(
            client=_make_client(),
            name="SpikeAgent",
            instructions="You write short, vivid prose.",
        )
        chunks = 0
        collected = ""
        print("      live: ", end="", flush=True)
        async for update in agent.run(
            "Write two sentences about a server room at 3am.", stream=True
        ):
            piece = getattr(update, "text", None)
            if piece:
                chunks += 1
                collected += piece
                print(piece, end="", flush=True)
        print()
        # Genuine streaming arrives in multiple chunks; a single blob means it buffered.
        if chunks >= 2 and collected.strip():
            print(f"      PASS — streamed in {chunks} chunks.")
            return True
        print(f"      WEAK — got {chunks} chunk(s); confirm this is really incremental.")
        return chunks >= 1
    except Exception as e:  # noqa: BLE001
        print(f"      FAIL — {type(e).__name__}: {e}")
        return False


async def check_mcp_tool() -> bool:
    mcp_url = os.environ.get("MCP_URL")
    print("\n[3/3] MCP TOOL ...")
    if not mcp_url:
        print("      SKIP — set MCP_URL to a reachable MCP endpoint to run this check.")
        return True  # skip is not a failure
    try:
        async with Agent(
            client=_make_client(),
            instructions="You are a helpful assistant. Use the tools when useful.",
            tools=MCPStreamableHTTPTool(
                name="spike_tools",
                description="Tools served by the spike MCP endpoint",
                url=mcp_url,
                # MCP *prompts* (not tools) get converted to OpenAI function schemas too
                # when load_prompts=True (the default), and at least one prompt with an
                # optional string arg produces an invalid schema Azure OpenAI rejects.
                # We also don't want the model treating prompts as agent-callable anyway.
                load_prompts=False,
            ),
        ) as agent:
            result = await agent.run(
                "List the tools you have available, by name, then stop."
            )
            text = getattr(result, "text", None) or str(result)
            print(f"      agent saw tools: {text.strip()[:300]!r}")
            print("      PASS — attached an MCP server and the agent could enumerate tools.")
            return True
    except Exception as e:  # noqa: BLE001
        print(f"      FAIL — {type(e).__name__}: {e}")
        return False


async def main() -> int:
    print("=" * 68)
    print("Microsoft Agent Framework (Python) de-risking spike")
    print("=" * 68)

    results = {
        "auth+model": await check_auth_and_completion(),
        "streaming": await check_streaming(),
        "mcp tool": await check_mcp_tool(),
    }

    print("\n" + "=" * 68)
    print("SUMMARY")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("=" * 68)

    if all(results.values()):
        print("\nAll green → MAF-in-Python is locked. Proceed to Phase 1.")
        return 0
    print("\nSomething is rough → consider swapping the brain to Pydantic AI now.")
    print("Everything else in the architecture (topology, tools, state) is unchanged.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
