# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Standalone personal-automation toolkit that sits on top of [Career Caddy](https://github.com/overcast-software/career_caddy) (a job-hunt management platform). It scrapes job postings, classifies emails, and pushes structured data into Career Caddy via its JSON:API or public MCP endpoint.

## Dev commands

Dependency manager is `uv`. Python ≥ 3.11.

```bash
uv sync                         # base deps (includes fastmcp, html2text, bs4)
uv sync --extra browser         # + playwright/camoufox
uv sync --extra ollama          # + local Ollama support
uv sync --extra a2a             # + fasta2a + uvicorn (required for caddy-orchestrator/gateway a2a mode)
uv sync --extra all             # everything
```

Linting/formatting is via **ruff** (configured in `pyproject.toml`, installed in the `dev` dependency group). Run after edits:

```bash
uv run --group dev ruff check --fix .   # autofix lint
uv run --group dev ruff format .        # format
```

No test runner is configured — don't invent test commands.

## Entry points (`[project.scripts]` in `pyproject.toml`)

| Command | Module |
|---|---|
| `caddy-web` | `src.pipelines.web_ui:main` — pydantic-ai `to_web()` UI |
| `caddy-url <url>` | `src.pipelines.url_to_caddy:run` — scrape one URL → post |
| `caddy-email` | `src.pipelines.email_to_caddy:run` — notmuch → scrape → post |
| `caddy-classify` | `scripts.tag_emails:run` — classify/tag emails daemon (stage 1 only) |
| `caddy-inbox` | `scripts.inbox_triage:run` — three-stage triage orchestrator (classify → refine → follow-up); see below |
| `caddy-process` | `scripts.process_tagged:run` |
| `caddy-orchestrator` | `src.agents.a2a_orchestrator:run` — A2A client/REPL (`--web` for UI) |
| `caddy-gateway` | `mcp_servers.agents_gateway:main` — exposes sub-agents as MCP tools (default) or A2A services (`--mode a2a`) |
| ~`caddy-login`/`caddy-poller`/`caddy-discover`~ | **removed** — canonical implementations live in the `career_caddy` parent at `agents/tools/{manual_login,discover_sites}.py` and `agents/pollers/hold_poller.py`. Use `make poller-local` or run them from `agents/`. |

Most long-running pipelines support `--loop` and `--interval`.

### `caddy-inbox` triage pipeline

`caddy-inbox` sequences three agents per email — classify (job-related? yes/no), refine (new posting vs. follow-up correspondence), follow-up (find matching job_application + set status) — and applies the tags `evaluated`, `job_post`, `refined`, `follow_up`, `caddy_processed` in order. Agents live in `src/agents/email_agents.py`; the loop in `scripts/inbox_triage.py`; the pluggable backend (`src/email_source/`, selected by `CADDY_EMAIL_BACKEND=notmuch|imap`) keeps classification agnostic of mail source. **Do not run `caddy-classify` / `caddy-process` as separate daemons against the same mailbox while `caddy-inbox` is looping** — they race on the same tags. The IMAP backend is scaffolded (`src/email_source/imap_source.py`) but raises `NotImplementedError`; notmuch is the default and only functioning backend today.

## Configuration

`.env` is loaded via `python-dotenv`. Required: `CC_API_BASE_URL`, `CC_API_TOKEN`, and one LLM provider key (`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`). `CC_MCP_URL` switches to MCP-SSE mode instead of direct API.

### Self-hosting against your own Career Caddy domain

cc_auto talks to Career Caddy entirely over HTTP — there are no Python imports
across the repo boundary. To point this toolkit at your own Career Caddy
instance, set the env var trio:

```
CC_API_BASE_URL=https://api.your-domain.com   # REST writes
CC_MCP_URL=https://mcp.your-domain.com/mcp    # MCP reads
CC_API_TOKEN=jh_...                           # API key from /admin/api-keys
```

No code changes. Acceptance test: `uv run caddy-inbox --once --limit 1`
processes one email cleanly against the configured domain.

Per-agent model overrides (resolved in `src/agents/agent_factory.py::get_model`): `CADDY_MODEL`, `EMAIL_CLASSIFIER_MODEL`, `JOB_EXTRACTOR_MODEL`, `PIPELINE_MODEL`, `BROWSER_SCRAPER_MODEL`, with `CADDY_DEFAULT_MODEL` as fallback and `openai:gpt-4o-mini` as the hard default.

## Architecture

Three layers, roughly:

1. **Client layer — `src/client/`** is the reusable core.
   - `api_client.py`: plain async `ApiClient` (httpx) with one function per Career Caddy CRUD op (companies, job posts, job applications, etc.).
   - `toolset.py`: `CareerCaddyToolset` wraps the functions in `TOOL_REGISTRY` as a pydantic-ai `FunctionToolset`, with `scope=` filtering (e.g. `"all"`, `"job_discovery"`). Agents receive a `CareerCaddyDeps(api_token, base_url)` dataclass; the toolset builds an `ApiClient` from deps on each call.
   - `models.py`: `JobPostData`, `CompanyData` pydantic models.

2. **Agents — `src/agents/`** are pydantic-ai `Agent`s built through a small registry pattern in `agent_factory.py`:
   - `register_defaults()` populates `_AGENT_REGISTRY` with roles: `caddy`, `job_extractor`, `email_classifier`, `pipeline`, `browser_scraper`. MCP-backed ones (email_classifier, pipeline, browser_scraper) are only registered if `pydantic_ai.mcp` (i.e. fastmcp) imports succeed.
   - `get_agent(role)` picks the model via the env-var map, assembles toolsets from `toolset_factories`, and applies the common history processor (`sanitize_orphaned_tool_calls` from `history.py`) which strips tool-call/return shape violations that provider APIs reject.
   - Ollama integration: if `pydanticai_ollama` is installed, the factory defines `ConcreteOllama{Provider,Model,StreamedResponse}` subclasses plus pre-built model instances (`qwen3_4b_model`, `browser_ollama_model`, etc.). Tool-capable Ollama calls go through Ollama's OpenAI-compat `/v1` endpoint, not the native one.

3. **Pipelines — `src/pipelines/`** are the end-to-end flows: `url_to_caddy`, `email_to_caddy`, `web_ui`. They wire agents + toolsets together and own the CLI surface.

### Multi-agent / A2A mode

`caddy-orchestrator` (client) + `caddy-gateway --mode a2a` (server) implement an Agent-to-Agent pattern over HTTP/JSON-RPC 2.0:
- Gateway exposes each specialised agent as a standalone A2A service on fixed ports — email `3010`, caddy `3011`, browser `3012`.
- Orchestrator discovers via `/.well-known/agent-card.json`, sends `message/send`, polls `tasks/get` until terminal.
- Sub-agents are stateless; the orchestrator must embed all needed context in each request string. This is the explicit cost/context-control strategy — don't give one agent every toolset.
- Default gateway mode (no `--mode a2a`) instead wraps the same sub-agents as FastMCP tools on a single port (`3003`).

### MCP servers — `mcp_servers/`

- `email_server.py`: notmuch-backed email tools (requires `notmuch` CLI + `NOTMUCH_MAILDIR`).
- `browser_server.py`: Camoufox/Playwright page scraping.
- `agents_gateway.py`: the gateway described above.
These are spawned as `MCPServerStdio` subprocesses by the agent factory; they're not imported.

### Browser helpers — `lib/browser/`

Shared helpers for `mcp_servers/browser_server.py` and `src/agents/html_fetchers.py` (used by `analyze_screenshots`). `secrets.yml` (gitignored; see `secrets.yml.example`) holds login credentials for browser automation. The `caddy-login`/`caddy-poller`/`caddy-discover` entrypoints that used to live here are gone — use the canonical copies in the `career_caddy` parent's `agents/` submodule.

## Conventions worth knowing

- The `src`, `lib`, `mcp_servers`, and `scripts` packages are all top-level wheel packages (see `[tool.hatch.build.targets.wheel]`). Imports use absolute paths like `from src.client.toolset import ...` and `from mcp_servers...` — don't refactor them into a single parent package without updating the wheel config.
- Agents are *created*, not reused — call `get_agent(role)` when you need one, rather than holding a module-level instance. Model selection happens at creation time based on current env vars.
- When adding a new Career Caddy API call: add the function in `api_client.py`, register it in `TOOL_REGISTRY` in `toolset.py`, and (if scope-limited) add it to the relevant scope set.
