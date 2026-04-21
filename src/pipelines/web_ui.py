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

from src.agents.agent_factory import get_model, register_defaults
from src.agents.caddy_poster import _CAREER_CADDY_SYSTEM_PROMPT, CareerCaddyResponse
from src.client.toolset import CareerCaddyDeps, CareerCaddyToolset

register_defaults()


def build_agent_direct() -> tuple[Agent, CareerCaddyDeps]:
    """Build an agent that talks to the Career Caddy API directly."""
    model = get_model("caddy")
    agent = Agent(
        model,
        name="career-caddy",
        system_prompt=_CAREER_CADDY_SYSTEM_PROMPT,
        output_type=CareerCaddyResponse,
        deps_type=CareerCaddyDeps,
        toolsets=[CareerCaddyToolset(scope="all")],
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

    agent = Agent(
        model,
        name="career-caddy-mcp",
        system_prompt=_CAREER_CADDY_SYSTEM_PROMPT,
        toolsets=[server],
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

    if args.mcp:
        print(f"Connecting to MCP endpoint: {args.mcp}")
        agent = build_agent_mcp(args.mcp)
        app = agent.to_web()
    else:
        base = os.environ.get("CC_API_BASE_URL", "http://localhost:8000")
        print(f"Connecting to Career Caddy API: {base}")
        agent, deps = build_agent_direct()
        app = agent.to_web(deps=deps)

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
