"""
Career Caddy — Agent Gateway

Two transport modes for exposing agents to an orchestrator:

Approach A — Agent-as-MCP-Tool (default)
    Wrap each specialized agent in a FastMCP tool. The orchestrator calls
    high-level tools (run_email_agent, run_caddy_agent, run_browser_agent)
    and receives finished answers, keeping context small.

    Run: uv run caddy-gateway
         uv run caddy-gateway --port 3003

Approach B — Agent-as-A2A-Server
    Each agent is a full A2A service via agent.to_a2a(). The orchestrator
    contacts them over HTTP/JSON-RPC 2.0.

    Run: uv run caddy-gateway --mode a2a

    Ports:
        email-agent    → :3010
        career-caddy   → :3011
        browser-agent  → :3012

    Requires: pip install fasta2a
"""

from __future__ import annotations

from lib.observability import configure_logfire

import argparse
import asyncio
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
for _noisy in ("httpcore", "httpx", "urllib3", "anyio", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

HOST = "0.0.0.0"
DEFAULT_PORT = 3003


# ---------------------------------------------------------------------------
# Lazy agent imports
# ---------------------------------------------------------------------------


def _load_agents():
    """Import specialized agents. Done lazily so startup is fast."""
    from src.agents.agent_factory import get_agent, register_defaults
    from src.agents.caddy_poster import _CAREER_CADDY_SYSTEM_PROMPT, CareerCaddyResponse
    from src.client.toolset import CareerCaddyDeps

    register_defaults()

    email_agent = get_agent("email_classifier")
    caddy_agent = get_agent(
        "caddy",
        output_type=CareerCaddyResponse,
        system_prompt=_CAREER_CADDY_SYSTEM_PROMPT,
    )
    browser_agent = get_agent("browser_scraper")

    return email_agent, caddy_agent, browser_agent


# ===========================================================================
# APPROACH A — Agent-as-MCP-Tool
# ===========================================================================


def make_agent_mcp_server():
    """
    Return a FastMCP server whose tools each delegate to a specialized agent.

    The orchestrator calls e.g. run_email_agent("find job emails from last week")
    and receives a finished natural-language answer, not raw tool outputs.
    """
    from fastmcp import FastMCP
    from pydantic_ai.usage import UsageLimits

    email_agent, caddy_agent, browser_agent = _load_agents()

    server = FastMCP("career-caddy-agent-gateway")
    _AGENT_LIMITS = UsageLimits(request_limit=20)

    token = os.environ.get("CC_API_TOKEN", "")
    base_url = os.environ.get("CC_API_BASE_URL", "http://localhost:8000")

    @server.tool()
    async def run_email_agent(request: str) -> str:
        """
        Delegate a request to the Email Agent.

        Use for: searching emails, extracting job URLs, classifying messages.
        Include all context in the request string — agent is stateless.
        """
        try:
            result = await email_agent.run(request, usage_limits=_AGENT_LIMITS)
            return result.output
        except Exception as exc:
            logger.exception("email_agent failed")
            return f"Email agent error: {exc}"

    @server.tool()
    async def run_career_caddy_agent(request: str) -> str:
        """
        Delegate a request to the Career Caddy Agent.

        Use for: creating/looking up job posts, company management, checking
        duplicates, recording applications. Embed full job details in request.
        """
        from src.client.toolset import CareerCaddyDeps

        try:
            deps = CareerCaddyDeps(api_token=token, base_url=base_url)
            result = await caddy_agent.run(request, deps=deps, usage_limits=_AGENT_LIMITS)
            output = result.output
            if hasattr(output, "model_dump_json"):
                return output.model_dump_json()
            return str(output)
        except Exception as exc:
            logger.exception("career_caddy_agent failed")
            return f"Career Caddy agent error: {exc}"

    @server.tool()
    async def run_browser_agent(request: str) -> str:
        """
        Delegate a request to the Browser Agent.

        Use for: scraping job post pages, navigating URLs. Always include
        the full URL in the request string.
        """
        try:
            result = await browser_agent.run(request, usage_limits=_AGENT_LIMITS)
            return result.output
        except Exception as exc:
            logger.exception("browser_agent failed")
            return f"Browser agent error: {exc}"

    return server


# ===========================================================================
# APPROACH B — Agent-as-A2A-Server
# ===========================================================================


async def _run_a2a_server(app, host: str, port: int, name: str):
    """Run a single A2A ASGI app with uvicorn."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    logger.info(f"Starting A2A server '{name}' on {host}:{port}")
    await server.serve()


_A2A_AGENTS: dict[str, dict] = {
    "email":   {"port": 3010, "name": "email-agent",         "description": "Searches and analyses job emails via notmuch."},
    "caddy":   {"port": 3011, "name": "career-caddy-agent",  "description": "Manages job posts and companies in the Career Caddy API."},
    "browser": {"port": 3012, "name": "browser-agent",       "description": "Scrapes job post pages via Camoufox browser automation."},
}


def _build_a2a_app(agent_key: str):
    email_agent, caddy_agent, browser_agent = _load_agents()
    agent = {"email": email_agent, "caddy": caddy_agent, "browser": browser_agent}[agent_key]
    meta = _A2A_AGENTS[agent_key]
    return agent.to_a2a(name=meta["name"], description=meta["description"]), meta["port"], meta["name"]


async def run_a2a_mode(host: str, only: str | None = None):
    """
    Start A2A servers. With --only <email|caddy|browser>, runs just that one.
    Otherwise runs all three concurrently in one process.
    """
    try:
        import fasta2a  # noqa: F401
    except ImportError:
        logger.error(
            "fasta2a is not installed.  Run:  pip install fasta2a\n"
            "Alternatively, use the default MCP mode (no --mode flag)."
        )
        sys.exit(1)

    keys = [only] if only else list(_A2A_AGENTS.keys())
    tasks = []
    for key in keys:
        app, port, name = _build_a2a_app(key)
        logger.info(f"  {name}  → http://{host}:{port}")
        tasks.append(_run_a2a_server(app, host, port, name))
    await asyncio.gather(*tasks)


# ===========================================================================
# Entry point
# ===========================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Career Caddy Agent Gateway — exposes agents to an orchestrator.",
    )
    parser.add_argument(
        "--mode", choices=["mcp", "a2a"], default="mcp",
        help="Transport mode: 'mcp' (default) or 'a2a'.",
    )
    parser.add_argument("--host", default=HOST)
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Port (MCP mode only).",
    )
    parser.add_argument(
        "--only", choices=["email", "caddy", "browser"], default=None,
        help="A2A mode only: start a single sub-agent instead of all three.",
    )
    args = parser.parse_args()

    service = f"caddy-gateway-{args.only}" if args.only else ("caddy-gateway-a2a" if args.mode == "a2a" else "caddy-gateway-mcp")
    configure_logfire(service)

    if args.mode == "a2a":
        asyncio.run(run_a2a_mode(args.host, only=args.only))
    else:
        server = make_agent_mcp_server()

        async def _log_tools():
            tools = await server.list_tools()
            logger.info(f"Agent gateway ready — {len(tools)} tools:")
            for t in sorted(tools, key=lambda t: t.name):
                logger.info(f"  {t.name}")

        asyncio.run(_log_tools())
        logger.info(f"Listening on http://{args.host}:{args.port}/mcp")
        server.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
