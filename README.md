# Career Caddy Automation

Standalone automation toolkit for [Career Caddy](https://github.com/overcast-software/career_caddy) — scrape job postings, classify emails, and manage applications via the Career Caddy API or MCP endpoint.

## What this does

Career Caddy is a job hunt management platform. This toolkit adds personal automation on top of it:

- **Scrape job URLs** → extract structured data → post to Career Caddy
- **Classify emails** → tag job postings via notmuch → process into Career Caddy
- **Web UI** → interact with Career Caddy tools via pydantic-ai's built-in web interface
- **MCP verification** → connect local agents to `careercaddy.online/mcp`

## Quickstart

```bash
# 1. Clone and install
git clone https://github.com/overcast-software/career_caddy_automation.git
cd career_caddy_automation
pip install uv
uv sync

# 2. Configure
cp .env.example .env
# Edit .env: set CC_API_BASE_URL, CC_API_TOKEN, OPENAI_API_KEY

# 3. Run
uv run caddy-inbox --loop --interval 15  # three-stage email triage (recommended)
uv run caddy-url https://...             # scrape a job URL → Career Caddy
uv run caddy-web                         # web UI with Career Caddy tools
```

## Self-hosting against your own Career Caddy domain

cc_auto talks to Career Caddy entirely over HTTP — there are no Python imports across the repo boundary. To point this toolkit at your own Career Caddy instance, set the env-var trio in `.env`:

```
CC_API_BASE_URL=https://api.your-domain.com    # REST writes
CC_MCP_URL=https://mcp.your-domain.com/mcp     # MCP reads
CC_API_TOKEN=jh_...                            # API key from /admin/api-keys
```

No code changes. Acceptance test:

```bash
uv run caddy-inbox --once --limit 1            # processes one email cleanly
```

## Entry points

| Command | Description |
|---------|-------------|
| `caddy-inbox` | **Recommended.** Three-stage email triage daemon (classify → refine → follow-up). |
| `caddy-url <url>` | Scrape one job URL → extract → post to Career Caddy |
| `caddy-email` | Full pipeline: email search → scrape → post |
| `caddy-web` | Web UI — pydantic-ai `to_web()` with Career Caddy tools |
| `caddy-gateway` | MCP/A2A gateway exposing sub-agents on `:3003` (MCP) or `:3010-3012` (A2A) |
| `caddy-orchestrator` | A2A client/REPL that talks to the gateway |
| `caddy-process` / `caddy-classify` | Legacy email scripts (subsumed by `caddy-inbox` — see warning below) |
| `caddy-score` | Score job posts |
| `caddy-analyze-screenshots` | Debug helper for failed scrapes |

> **Don't run `caddy-classify` and `caddy-process` alongside `caddy-inbox` against the same mailbox** — they race on the same notmuch tags.

Most long-running commands support `--loop` and `--interval`.

The browser-side scripts (`caddy-poller`, `caddy-login`, `caddy-discover`) used to live here; they're now in the [`career_caddy_agents`](https://github.com/overcast-software/career_caddy_agents) submodule of the parent repo. Run them via `make poller-local` (parent) or directly from the agents repo.

## Optional dependencies

```bash
uv sync --extra browser   # camoufox + playwright (browser automation)
uv sync --extra ollama    # local Ollama LLM support
uv sync --extra all       # everything
```

## LLM configuration

Default model: `openai:gpt-4o-mini`. Override per-agent via env vars:

| Env Var | Agent |
|---------|-------|
| `CADDY_MODEL` | Career Caddy CRUD agent |
| `JOB_EXTRACTOR_MODEL` | Job data extractor |
| `EMAIL_CLASSIFIER_MODEL` | Email classifier |
| `BROWSER_SCRAPER_MODEL` | Browser scraper |
| `CADDY_DEFAULT_MODEL` | Fallback for all agents |

For local Ollama: `CADDY_DEFAULT_MODEL=ollama:qwen3:4b-instruct`

## Project structure

```
career_caddy_automation/
├── src/
│   ├── client/             # Reusable Career Caddy API client
│   │   ├── api_client.py   # HTTP wrapper + all CRUD operations
│   │   ├── models.py       # JobPostData, CompanyData
│   │   └── toolset.py      # CareerCaddyToolset for pydantic-ai agents
│   ├── agents/             # Pydantic-AI agents
│   │   ├── agent_factory.py # Agent creation + model routing + Ollama
│   │   ├── job_extractor.py # HTML → structured JobPostData
│   │   ├── caddy_poster.py  # Agent that creates job posts
│   │   ├── history.py       # Message history management
│   │   └── usage_reporter.py # AI usage tracking
│   └── pipelines/          # End-to-end workflows
│       ├── url_to_caddy.py  # Scrape URL → post
│       ├── email_to_caddy.py # Email → scrape → post
│       └── web_ui.py        # pydantic-ai to_web() interface
├── mcp_servers/            # Local MCP servers
│   ├── email_server.py     # notmuch email integration
│   ├── browser_server.py   # Camoufox browser automation (used by html_fetchers + agent factory)
│   └── agents_gateway.py   # A2A / MCP gateway exposing sub-agents
├── lib/browser/            # Browser automation helpers
├── scripts/
│   ├── inbox_triage.py     # Three-stage email triage daemon (caddy-inbox)
│   ├── tag_emails.py       # Legacy classify daemon
│   ├── process_tagged.py   # Legacy follow-up processor
│   ├── score_posts.py      # Job scoring
│   └── analyze_screenshots.py  # Debug helper for failed scrapes
├── pyproject.toml
├── .env.example
└── secrets.yml.example     # Browser login credentials (optional)
```

## Building your own pipeline

The `src/client/` package is the reusable core. Import it to build custom automations:

```python
from src.client import ApiClient, JobPostData, CareerCaddyToolset, CareerCaddyDeps
from pydantic_ai import Agent

# Direct API usage
api = ApiClient("https://careercaddy.online", "jh_xxx")
result = await api.get("/api/v1/job-posts/")

# Or use the pydantic-ai toolset
agent = Agent(
    "openai:gpt-4o-mini",
    toolsets=[CareerCaddyToolset(scope="job_discovery")],
    deps_type=CareerCaddyDeps,
)
result = await agent.run(
    "Find all Python jobs",
    deps=CareerCaddyDeps(api_token="jh_xxx", base_url="https://careercaddy.online"),
)
```
