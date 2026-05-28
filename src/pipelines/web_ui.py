"""Launch pydantic-ai's web UI with Career Caddy tools.

Connects to the Career Caddy API (local or production) and exposes all
tools through pydantic-ai's built-in web interface.

Usage:
    uv run caddy-web                          # local API
    CC_API_BASE_URL=https://api.careercaddy.online uv run caddy-web  # prod

Or connect to the public MCP endpoint instead of direct API:
    uv run caddy-web --mcp https://mcp.careercaddy.online/mcp
"""

import argparse
import os

from lib.observability import configure_logfire

configure_logfire("caddy-web")
from pydantic_ai import Agent

from src.agents.agent_factory import get_model, register_defaults, resolve_model
from src.client.toolset import CareerCaddyDeps, CareerCaddyToolset

register_defaults()


_WEB_UI_SYSTEM_PROMPT = """
You are the user's interactive scrape-quality copilot for Career Caddy.

Your job is to help the user diagnose and improve scraping outcomes. The user
will rely on you to investigate issues end-to-end and, when explicitly asked,
fix the underlying job-post records.

## What you have access to
- Career Caddy API tools (via the CareerCaddy toolset / MCP): scrapes, job-posts,
  companies, job-applications, scrape-profiles, screenshots associated with scrapes.
- Logfire MCP tools (when configured): query traces and exceptions from the
  scraping pipeline (`mcp_servers`, `caddy-url`, `caddy-email`, etc.). Use these
  to correlate a bad scrape with what the pipeline actually did.

## How to investigate a scrape issue
1. Start from whatever the user gives you — a scrape id, a job-post id, a URL,
   a hostname, or a symptom ("LinkedIn scrapes look thin").
2. Pull the relevant records: the scrape row (status, url, job_content length,
   timestamps), the linked job-post if any, and the ScrapeProfile for the host.
3. Look at screenshots associated with the scrape when available — they often
   reveal captchas, login walls, or layout shifts that explain bad extractions.
4. Cross-reference Logfire traces around the scrape's timestamp to see which
   selectors fired, which fell back, and where exceptions occurred.
5. Form a concrete hypothesis: is this a selector regression, a URL-shape issue
   (tracker → homepage), an auth/captcha block, an LLM extraction miss, or a
   ScrapeProfile gap (missing url_rewrite, wrong content selector, etc.)?
6. Report findings clearly: what's wrong, why, and what the fix would be.

## When asked to fix
Only mutate data when the user explicitly asks ("fix it", "update job-post 123",
"patch the profile", etc.). Confirm the proposed change before writing if the
blast radius is non-trivial (changing many records, editing a ScrapeProfile that
affects a whole host). For single-record edits the user has named, just do it.

## Defaults
- Be terse. Lead with the diagnosis, then evidence.
- Prefer `find_job_post_by_link` over `get_job_posts`; never scan by id.
- If a tool returns success=false, stop and report — do not retry blindly.
- Don't speculate about data you haven't actually pulled.
"""


def _logfire_toolset():
    """Logfire MCP server (read-only telemetry queries) if a read token is configured."""
    token = os.environ.get("LOGFIRE_READ_TOKEN")
    if not token:
        return None
    from pydantic_ai.mcp import MCPServerStdio

    return MCPServerStdio(
        command="uvx",
        args=["logfire-mcp@latest", f"--read-token={token}"],
    )


def _selectable_models() -> dict[str, object]:
    """Models offered in the web UI's model picker."""
    return {
        "GPT-4 (OpenAI)": resolve_model("openai:gpt-4"),
        "Claude Sonnet 4.6 (Anthropic)": resolve_model("anthropic:claude-sonnet-4-6"),
        "astral3-tools 12b (Ollama)": resolve_model("ollama:60MPH/astral3-tools:12b"),
    }


def build_agent_direct() -> tuple[Agent, CareerCaddyDeps]:
    """Build an agent that talks to the Career Caddy API directly."""
    model = get_model("caddy")
    toolsets: list = [CareerCaddyToolset(scope="all")]
    logfire_ts = _logfire_toolset()
    if logfire_ts is not None:
        toolsets.append(logfire_ts)
    agent = Agent(
        model,
        name="career-caddy",
        system_prompt=_WEB_UI_SYSTEM_PROMPT,
        deps_type=CareerCaddyDeps,
        toolsets=toolsets,
    )
    deps = CareerCaddyDeps(
        api_token=os.environ["CC_API_TOKEN"],
        base_url=os.environ.get("CC_API_BASE_URL", "http://localhost:8000"),
    )
    return agent, deps


def build_agent_mcp(mcp_url: str) -> Agent:
    """Build an agent that talks to Career Caddy via the public MCP endpoint."""
    from pydantic_ai.mcp import MCPServerStreamableHTTP

    model = get_model("caddy")
    token = os.environ["CC_API_TOKEN"]

    server = MCPServerStreamableHTTP(
        url=mcp_url,
        headers={"Authorization": f"Bearer {token}"},
    )
    toolsets: list = [server]
    logfire_ts = _logfire_toolset()
    if logfire_ts is not None:
        toolsets.append(logfire_ts)

    agent = Agent(
        model,
        name="career-caddy-mcp",
        system_prompt=_WEB_UI_SYSTEM_PROMPT,
        toolsets=toolsets,
    )
    return agent


def main():
    parser = argparse.ArgumentParser(description="Launch Career Caddy web UI (pydantic-ai to_web)")
    parser.add_argument(
        "--mcp",
        type=str,
        default=None,
        help="MCP SSE endpoint URL (e.g. https://mcp.careercaddy.online/mcp). "
        "If omitted, connects directly to CC_API_BASE_URL.",
    )
    parser.add_argument(
        "--port", type=int, default=8888, help="Port for the web UI (default: 8888)"
    )
    args = parser.parse_args()

    import uvicorn

    models = _selectable_models()

    if args.mcp:
        print(f"Connecting to MCP endpoint: {args.mcp}")
        agent = build_agent_mcp(args.mcp)
        app = agent.to_web(models=models)
    else:
        base = os.environ.get("CC_API_BASE_URL", "http://localhost:8000")
        print(f"Connecting to Career Caddy API: {base}")
        agent, deps = build_agent_direct()
        app = agent.to_web(deps=deps, models=models)

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
