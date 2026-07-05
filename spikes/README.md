# spikes/

Throwaway experiments that de-risk a specific assumption before we build on it.
Not production code. Keep them; they document what we proved and when.

## `maf_python_spike.py` — is Microsoft Agent Framework (Python) viable?

Proves the three things the whole stack depends on, against your **real** Foundry
endpoint:

1. **Auth + model** — `FoundryChatClient` reaches your Foundry deployment and completes.
2. **Streaming** — `agent.run(..., stream=True)` streams tokens incrementally (the one
   feature we couldn't confirm from the Python docs).
3. **MCP tool** — an `MCPStreamableHTTPTool` attaches and the agent can enumerate tools.

### Run it

```bash
az login                                            # AzureCliCredential uses this
export FOUNDRY_PROJECT_ENDPOINT="https://<your-foundry-project-endpoint>"
export FOUNDRY_MODEL="<your-model-deployment-name>"
export MCP_URL="https://<reachable-mcp-endpoint>"   # optional; enables check 3

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python maf_python_spike.py
```

### Reading the result

- **All PASS** → MAF-in-Python is locked; proceed to Phase 1.
- **Any FAIL/rough** → swap the brain to **Pydantic AI** now, while no real code exists.
  Topology, MCP tools, and state are framework-independent, so nothing else changes.

> The script was written from current MAF docs but not executed on the authoring
> machine. If an import or attribute name has drifted, the traceback points right at it
> — fix and re-run. That's the spike earning its keep.

### For the fresh session that runs this

Seed it from the repo, e.g.:

> "Read `docs/00-overview.md` and `spikes/README.md`. This is a personal POC for a Teams
> triage bot. Help me run `spikes/maf_python_spike.py` against my Foundry endpoint and
> interpret the result."
