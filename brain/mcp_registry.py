"""Extensible MCP tool registry (docs/phase-3-mcp-investigation.md: "Adding a
tool = config, not code").

brain/mcp_tools.json is the source of truth: one entry per MCP server, with a
`${ENV_VAR}` placeholder for its URL. This module's whole job is to turn that
file into a list of ready-to-attach `MCPStreamableHTTPTool` instances without
the investigator agent (investigation.py) knowing anything about where the
list came from. To add a server (e.g. argocd for acceptance #2): append an
entry to mcp_tools.json and set its env var in the deployment -- no code
change here or in investigation.py.

Two deliberate resilience choices, both graceful-degradation
(docs/00-overview.md guardrail #8):
  - An entry whose interpolated URL is empty/unset is SKIPPED, not an error.
    That lets ops list argocd in mcp_tools.json before the argocd MCP server
    is actually deployed/configured, without breaking brain startup.
  - Building the tool list never touches the network (MCPStreamableHTTPTool's
    constructor doesn't connect); only entering the lifecycle manager does.
    So a server that's unreachable at *connect* time is a runtime concern for
    investigation.py's per-call try/except, not this module's.
"""
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path

logger = logging.getLogger("brain.mcp_registry")

_CONFIG_PATH = Path(__file__).parent / "mcp_tools.json"

# Matches ${VAR_NAME} placeholders in the "url" field.
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(url_template: str) -> str:
    """Replace ${VAR} with os.environ.get(VAR, ""). A reference to an unset
    var resolves to the empty string (not a KeyError) -- that's what lets an
    unconfigured server be treated as "not deployed yet" rather than a crash."""

    def _sub(match: re.Match) -> str:
        return os.environ.get(match.group(1), "")

    return _VAR_RE.sub(_sub, url_template)


def _resolve_servers(config_path: Path) -> tuple[list[dict], list[str]]:
    """Load mcp_tools.json and split its entries by whether their URL
    resolves. Returns (resolved, skipped_names): `resolved` are entries with a
    non-empty interpolated URL; `skipped_names` are servers present in config
    but skipped because their URL env var is unset/empty -- callers surface
    those in tools_unavailable so degradation isn't silently under-reported."""
    with open(config_path) as f:
        raw = json.load(f)

    resolved, skipped = [], []
    for entry in raw.get("servers", []):
        name = entry["name"]
        url = _interpolate(entry["url"])
        if not url:
            logger.info(
                "mcp_registry: skipping server %r -- URL env var not set "
                "(server not deployed/configured yet)",
                name,
            )
            skipped.append(name)
            continue
        resolved.append({"name": name, "url": url, "description": entry.get("description", "")})
    return resolved, skipped


def load_server_configs(config_path: Path = _CONFIG_PATH) -> list[dict]:
    """Load mcp_tools.json and return the entries whose URL resolves to a
    non-empty string, with the URL already interpolated. Entries with an
    empty/unset URL are logged and skipped -- see module docstring."""
    return _resolve_servers(config_path)[0]


def build_tools(servers: list[dict]):
    """Build one MCPStreamableHTTPTool per resolved server (from
    _resolve_servers/load_server_configs). Does not connect -- see module
    docstring. Returns [] for an empty list (e.g. local dev with nothing set),
    which is a valid state: the investigator agent still runs, just with no
    tools attached, and its prompt/report degrade via tools_unavailable."""
    # Imported lazily, matching interviewer.py's lazy-import style: modules
    # that only inspect config (e.g. selfcheck_phase3.py) shouldn't need
    # agent-framework installed.
    from agent_framework import MCPStreamableHTTPTool

    tools = []
    for server in servers:
        tools.append(
            MCPStreamableHTTPTool(
                name=server["name"],
                description=server["description"],
                url=server["url"],
                # load_prompts=False per the spike lesson (maf_python_spike.py):
                # MCP *prompts* get converted to OpenAI function schemas too when
                # load_prompts=True (the default), and at least one prompt with an
                # optional string arg produced a schema Azure OpenAI rejected. We
                # also don't want the model treating prompts as agent-callable.
                load_prompts=False,
            )
        )
    return tools


class ToolLifecycle:
    """Async context manager that connects every configured MCP tool on
    __aenter__ and closes all of them on __aexit__, even if one fails to
    connect.

    Why not just `async with tool1, tool2: ...`? The tool list is dynamic
    (config-driven, possibly empty, possibly N servers) -- an AsyncExitStack
    is the standard way to enter a variable number of context managers and
    still guarantee every already-entered one is closed on the way out.

    Graceful degradation: `.unavailable` (by name) collects both servers that
    were skipped at config load (URL env var unset) and servers that failed to
    connect -- either way they're excluded from `.tools` rather than aborting
    the investigation. Surfacing the config-skipped ones too keeps the report
    honest (the model isn't tempted to invent evidence for a tool it never
    had). investigation.py surfaces `.unavailable` in
    InvestigationReport.tools_unavailable.
    """

    def __init__(self, config_path: Path = _CONFIG_PATH):
        self._config_path = config_path
        self.tools: list = []
        self.unavailable: list[str] = []
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> "ToolLifecycle":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        resolved, skipped = _resolve_servers(self._config_path)
        self.unavailable.extend(skipped)
        for tool in build_tools(resolved):
            try:
                await self._stack.enter_async_context(tool)
                self.tools.append(tool)
            except Exception:  # noqa: BLE001 - any connect failure degrades gracefully
                logger.exception("mcp_registry: failed to connect tool %r", getattr(tool, "name", tool))
                self.unavailable.append(getattr(tool, "name", str(tool)))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc, tb)
