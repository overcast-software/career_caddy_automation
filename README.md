# Career Caddy Automation

Standalone automation toolkit for [Career Caddy](https://github.com/overcast-software/career_caddy) — scrape job postings, classify emails, and manage applications via the Career Caddy API or MCP endpoint.

## What this does

Career Caddy is a job hunt management platform. This toolkit adds personal automation on top of it:

- **Scrape job URLs** → extract structured data → post to Career Caddy
- **Classify emails** → tag job postings via notmuch → process into Career Caddy
- **Web UI** → interact with Career Caddy tools via pydantic-ai's built-in web interface
- **MCP verification** → connect local agents to `mcp.careercaddy.online`

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
uv run caddy-web                    # web UI with Career Caddy tools
uv run caddy-url https://...        # scrape a job URL → Career Caddy
uv run caddy-email                  # email pipeline (requires notmuch)
uv run caddy-classify               # classify emails (requires notmuch)
```

## Connection modes

### Direct API (default)
Set `CC_API_BASE_URL` and `CC_API_TOKEN` in `.env`. The toolkit makes HTTP requests directly to the Career Caddy JSON:API endpoints.

```
CC_API_BASE_URL=https://api.careercaddy.online
CC_API_TOKEN=jh_xxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### MCP SSE (public endpoint)
Connect to `mcp.careercaddy.online` for tool access via the MCP protocol:

```bash
uv run caddy-web --mcp https://mcp.careercaddy.online/mcp
```

## Entry points

| Command | Description |
|---------|-------------|
| `caddy-web` | Web UI — pydantic-ai `to_web()` with Career Caddy tools |
| `caddy-url <url>` | Scrape one job URL → extract → post to Career Caddy |
| `caddy-email` | Full pipeline: email search → scrape → post |
| `caddy-classify` | Classify and tag emails (notmuch) |

All commands support `--loop` and `--interval` for continuous operation.

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
├── mcp_servers/            # Local MCP servers (optional)
│   ├── email_server.py     # notmuch email integration
│   └── browser_server.py   # Camoufox browser automation
├── lib/browser/            # Browser automation helpers
├── scripts/
│   └── tag_emails.py       # Email classification daemon
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
api = ApiClient("https://api.careercaddy.online", "jh_xxx")
result = await api.get("/api/v1/job-posts/")

# Or use the pydantic-ai toolset
agent = Agent(
    "openai:gpt-4o-mini",
    toolsets=[CareerCaddyToolset(scope="job_discovery")],
    deps_type=CareerCaddyDeps,
)
result = await agent.run(
    "Find all Python jobs",
    deps=CareerCaddyDeps(api_token="jh_xxx", base_url="https://api.careercaddy.online"),
)
```
