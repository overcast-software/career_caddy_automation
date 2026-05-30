# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

cc_auto is the **operator-side** of the Career Caddy ecosystem — the
HTTP-only toolkit that drives Career Caddy (job-hunt management platform)
from one user's machines (laptop, pibu, home server). It triages email,
classifies and refines messages, follows up on applications, runs the
caddy-web copilot, and orchestrates A2A sub-agents.

cc_auto is being **promoted to a first-class submodule** of the
[Career Caddy parent](https://github.com/overcast-software/career_caddy)
at path `automation/`, alongside `api/`, `frontend/`, `agents/`. See
`notes.org` → `* Promoting cc_auto → automation/ — first-class submodule
[2026-05-30]` for the conversion plan; the parent's [CLAUDE.md] is the
top-level orientation. Until conversion ships, the repo still lives at
`~/Network/syncthing/Projects/career_caddy_automation/`.

**Role split with `agents/` (sibling submodule):**
- `agents/` = **service-side** — Camoufox/Playwright, scrape_graph,
  prod MCP servers (`chat_server.py`, `public_server.py`), pollers
  (hold_poller until queue Phase 4, score_poller until 5b). Runs as
  Docker containers for *all* users.
- `automation/` (this repo) = **operator-side** — email triage,
  caddy-web copilot, A2A orchestrator/gateway, sharpen_profiles, link
  traverser. Runs on *one user's* machines. HTTP-only against the api
  + public MCP.

Test for which side something belongs in: *service for everyone* →
`agents/`; *operator for one user* → `automation/`.

## Dev commands

Dependency manager is `uv`. Python ≥ 3.11.

```bash
uv sync                         # base deps (includes fastmcp, html2text, bs4)
uv sync --extra browser         # + playwright/camoufox
uv sync --extra ollama          # + local Ollama support
uv sync --extra a2a             # + fasta2a + uvicorn (required for caddy-orchestrator/gateway a2a mode)
uv sync --extra all             # everything
```

### CI surface

`make ci` is the single command parent's Dagger pipeline will call;
runs lint + tests fail-fast. Targets:

```bash
make ci         # lint + test (parent's Dagger entry point)
make lint       # ruff check src/ tests/
make fmt        # ruff format src/ tests/  (developer convenience)
make fmt-check  # ruff format --check (advisory; wire into ci if parent asks)
make test       # pytest tests/
```

All targets shell through `uv run --group dev` so ruff + pytest
resolve regardless of the host's default-group config. File-level
inner-loop equivalents:

```bash
uv run --group dev ruff check --fix <file>
uv run --group dev ruff format <file>
uv run --group dev pytest tests/<file>::<test>
```

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
| `caddy-trace-email <id>` | `scripts.trace_email` — one-shot triage of a single email with CADDY_TRIAGE_TRACE + CADDY_DEDUPE_TRACE forced on |
| ~`caddy-login`/`caddy-poller`/`caddy-discover`~ | **removed** — canonical implementations live in the parent's `agents/tools/{manual_login,discover_sites}.py` and `agents/pollers/hold_poller.py`. The local `scripts/hold_poller.py` stopgap is slated for deletion in the submodule conversion. |

Most long-running pipelines support `--loop` and `--interval`.

### `caddy-inbox` triage pipeline

`caddy-inbox` sequences three agents per email — classify (job-related?
yes/no), refine (new posting vs. follow-up correspondence), follow-up
(find matching job_application + set status) — and applies the tags
`evaluated`, `job_post`, `refined`, `follow_up`, `caddy_processed` in
order. Agents live in `src/agents/email_agents.py`; the loop in
`scripts/inbox_triage.py`; the pluggable backend (`src/email_source/`,
selected by `CADDY_EMAIL_BACKEND=notmuch|imap`) keeps classification
agnostic of mail source. **Do not run `caddy-classify` / `caddy-process`
as separate daemons against the same mailbox while `caddy-inbox` is
looping** — they race on the same tags. The IMAP backend is scaffolded
(`src/email_source/imap_source.py`) but raises `NotImplementedError`;
notmuch is the default and only functioning backend today.

## Configuration

`.env` is loaded via `python-dotenv`. Required:

- `CC_API_BASE_URL` — Career Caddy REST endpoint
- `CC_API_TOKEN` — long-lived `jh_*` API key for your user (see api `/admin/api-keys`)
- One LLM provider key (`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`)

Optional:

- `CC_MCP_URL` — switches reads to MCP-SSE mode instead of direct REST
- `MONGODB_URI` — Mongo connection (default `mongodb://localhost:27017/cc_auto`)
- `CADDY_LOG_DIR` — file-log destination (default `$CADDY_HOME/var/logs/`)
- `CADDY_EMAIL_BACKEND` — `notmuch` (default) or `imap`
- `LOGFIRE_READ_TOKEN` — only needed if you want MCP / agents to query logfire reads

### Self-hosting against your own Career Caddy domain

cc_auto talks to Career Caddy entirely over HTTP — there are no Python
imports across the repo boundary. To point this toolkit at your own
Career Caddy instance:

```
CC_API_BASE_URL=https://api.your-domain.com   # REST writes
CC_MCP_URL=https://mcp.your-domain.com/mcp    # MCP reads
CC_API_TOKEN=jh_...                           # API key from /admin/api-keys
```

No code changes. Acceptance test: `uv run caddy-inbox --once --limit 1`
processes one email cleanly against the configured domain.

### Per-agent model overrides

Resolved in `src/agents/agent_factory.py::get_model`. Env names:
`CADDY_MODEL`, `EMAIL_CLASSIFIER_MODEL`, `JOB_EXTRACTOR_MODEL`,
`PIPELINE_MODEL`, `BROWSER_SCRAPER_MODEL`, with `CADDY_DEFAULT_MODEL`
as fallback and `openai:gpt-4o-mini` as the hard default.

## State store: MongoDB

cc_auto's observability state (triage runs, per-email outcomes,
traversal audit, forward audit) lives in **MongoDB** — collections per
concern under db `cc_auto`. Rationale: Metabase has a built-in Mongo
connector (drops the Postgres-sync layer), schema-flexible writes match
how the pipeline evolves, and a sidecar Mongo container ships in the
same `docker-compose.yml` cc_auto provides. See `notes.org` → `***
State store: MongoDB, not SQLite` for the full reasoning.

Collections under db `cc_auto`:
- `triage_runs` — one doc per `run_once()` call
- `triage_emails` — one doc per email processed (refined `outcome` +
  `exception_class` + `network_failure` flags)
- `traversal_runs` — link-traversal audit (Phase C of the Roadmap)
- `forward_audit` — catchall-mail audit (Phase B3 of the Roadmap)
- `skipped_duplicates` — dedupe-skip log

Client lib: **pymongo** (sync). Observability writes are
fire-and-forget operator-side, not in the hot path. Connection +
indexes live in `src/observability/mongo_client.py::get_db()`; domain
APIs (`start_run`, `record_email`, etc.) in
`src/observability/triage_store.py` and siblings.

Legacy: `src/db.py` (orphan SQLite shim from commit e26f787) is slated
for deletion alongside the Mongo introduction.

## Architecture

Three layers, roughly:

1. **Client layer — `src/client/`** is the reusable core.
   - `api_client.py`: plain async `ApiClient` (httpx) with one function per Career Caddy CRUD op (companies, job posts, job applications, etc.).
   - `toolset.py`: `CareerCaddyToolset` wraps the functions in `TOOL_REGISTRY` as a pydantic-ai `FunctionToolset`, with `scope=` filtering (e.g. `"all"`, `"job_discovery"`). Agents receive a `CareerCaddyDeps(api_token, base_url)` dataclass; the toolset builds an `ApiClient` from deps on each call.
   - `models.py`: `JobPostData`, `CompanyData` pydantic models.

2. **Agents — `src/agents/`** are pydantic-ai `Agent`s built through a small registry pattern in `agent_factory.py`:
   - `register_defaults()` populates `_AGENT_REGISTRY` with roles: `caddy`, `job_extractor`, `email_classifier`, `pipeline`, `browser_scraper`. MCP-backed ones (email_classifier, pipeline, browser_scraper) are only registered if `pydantic_ai.mcp` (i.e. fastmcp) imports succeed.
   - `get_agent(role)` picks the model via the env-var map, assembles toolsets from `toolset_factories`, and applies the common history processor (`sanitize_orphaned_tool_calls` from `history.py`) which strips tool-call/return shape violations that provider APIs reject.
   - Ollama integration: if `pydanticai_ollama` is installed, pre-built model instances (`qwen3_4b_model`, `browser_ollama_model`, etc.) target Ollama's OpenAI-compat `/v1` endpoint via `OpenAIChatModel`. The `OllamaModel` / `OllamaProvider` from `pydanticai-ollama` are also instantiable directly (the older `Concrete*` shims were dropped in commit b0b3f96 once 0.1.4 exposed the required abstract methods).

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

Shared helpers for `mcp_servers/browser_server.py` and `src/agents/html_fetchers.py` (used by `analyze_screenshots`). `secrets.yml` (gitignored; see `secrets.yml.example`) holds login credentials for browser automation. The legacy `caddy-login`/`caddy-poller`/`caddy-discover` entry points are gone — canonical copies live in the parent's `agents/` submodule.

### Observability — `src/observability/` + `lib/observability.py`

- `src/observability/` (NEW per the Roadmap) — Mongo-backed persistence layer. `mongo_client.py` (single cached `pymongo.Database`), `triage_store.py` (run + email collections), `traversal_store.py`, `forward_audit.py`. Domain-API style — call sites in `scripts/inbox_triage.py` etc. import from `src.observability`, not from raw pymongo.
- `lib/observability.py` — stateless logfire / file-log configuration (`configure_logfire(name)` adds a rotating file handler under `$CADDY_LOG_DIR`).
- `lib/trace_*.py` — forensic per-email tracers (`trace_inbox.py`, `trace_dedupe.py`, `trace_observability.py`) gated by env flags. Decorators only; no state.

## Gitflow

- `feature/* → main`, no `develop`.
- Commit specific files by name; never `add -A` / `add .`.
- Commit messages: short, present-tense, scoped — `fix(inbox_triage):`, `feat(span_validator):`, `chore(api_client):`. **No `Co-Authored-By` footer**.
- Lint + test before push: `make ci`.
- Push: `git push origin feature/<concern>:feature/<concern>` (explicit refspec, once remote lands during submodule conversion).
- Never commit `notes.org`, `.env*`, `secrets.yml` (gitignored — leave them that way; don't `git rm --cached`).

## Conventions worth knowing

- The `src`, `lib`, `mcp_servers`, and `scripts` packages are all top-level wheel packages (see `[tool.hatch.build.targets.wheel]`). Imports use absolute paths like `from src.client.toolset import ...` and `from mcp_servers...` — don't refactor them into a single parent package without updating the wheel config.
- Agents are *created*, not reused — call `get_agent(role)` when you need one, rather than holding a module-level instance. Model selection happens at creation time based on current env vars.
- When adding a new Career Caddy API call: add the function in `api_client.py`, register it in `TOOL_REGISTRY` in `toolset.py`, and (if scope-limited) add it to the relevant scope set.
- Config + state are **self-contained**: `.env`, `secrets.yml`, `config/`, `var/` all live at the repo root anchored by `$CADDY_HOME` (defaults to the directory containing `pyproject.toml`). No `~/.config/career_caddy/` or `~/.local/share/career_caddy/` paths.
