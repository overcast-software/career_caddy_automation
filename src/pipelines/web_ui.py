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
- Enhancer inspection tools (staff-only). These are hidden from non-staff
  sessions by `StaffOnlyToolFilter` on the public MCP server — if your API key
  isn't staff, they simply won't appear in your tools list. Never pretend a
  tool exists when it doesn't; check your tools list first.
  - `inspect_scrape_html(scrape_id, selector?, mode?)` — read a stored scrape's
    HTML.
    - `mode="trim"` (default): BS4-stripped, ~40 KB cap. Drops `<script>`,
      `<style>`, HTML comments, 1x1 tracking pixels, inline event handlers.
      Preserves `aria-*`, `data-testid`, `role`, `class`, `id`.
    - `mode="skeleton"`: tag+class+id tree with body text stripped — first-pass
      orientation on large pages.
    - `mode="selector"` (auto when `selector` is passed): runs the selector via
      BS4 `.select()` and returns per-match `outline` (parent chain),
      `text_snippet`, `attrs`, and `match_count`. Plain CSS3 only —
      Playwright pseudos like `:has-text("...")` raise; use
      `find_selectors_for_text` for text anchoring.
  - `find_selectors_for_text(scrape_id, text, max_results?, case_insensitive?)`
    — ranked stable selectors anchoring `text`. Ranking:
    `data-testid > role > aria-label > stable id > single semantic class >
    multi-class composite > bare tag`. Hashed-looking ids/classes (Tailwind
    JIT, css-in-js) are filtered out. Output is plain CSS3 so it round-trips
    through `inspect_scrape_html(..., mode="selector")` for verification.
  - `test_url_rewrite(url, hostname?)` — dry-run the host's
    `ScrapeProfile.url_rewrites` against `url`. Hostname is derived from `url`
    if not passed (`www.` stripped). Returns `{rewritten, changed,
    matched_rule, rule_count}`. Use BEFORE proposing a new rewrite via
    `update_scrape_profile`.

  Typical 7-call loop when designing or repairing a ScrapeProfile:

  ```
  inspect_scrape_html(id, mode="skeleton")              # orient
  find_selectors_for_text(id, "About the job")          # candidates for ready_selector
  inspect_scrape_html(id, selector=top_candidate)       # verify
  find_selectors_for_text(id, "Apply on company")       # candidates for apply_link
  inspect_scrape_html(id, selector=top_candidate)       # verify
  test_url_rewrite(jp.link)                             # if URL canonicalization is in scope
  update_scrape_profile(profile_id, ...)                # commit
  ```

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
   If the question lands on ScrapeProfile design (selectors, url_rewrites),
   reach for the enhancer inspection tools listed above — `inspect_scrape_html`
   to see the actual HTML, `find_selectors_for_text` to discover stable
   anchors, and `test_url_rewrite` to dry-run rules before writing them.
6. Report findings clearly: what's wrong, why, and what the fix would be.

## When asked to fix
Only mutate data when the user explicitly asks ("fix it", "update job-post 123",
"patch the profile", etc.). Confirm the proposed change before writing if the
blast radius is non-trivial (changing many records, editing a ScrapeProfile that
affects a whole host). For single-record edits the user has named, just do it.
Before writing a new `url_rewrites` rule, dry-run it with `test_url_rewrite`
(when available) so you know exactly what the rewrite would produce. Before
writing a new selector into `ScrapeProfile.css_selectors`, verify it with
`inspect_scrape_html(..., selector=...)` and confirm `match_count == 1`.

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
        # MCP public_server.verify_token() expects Bearer + forwards to
        # api/v1/me/. This is the MCP transport's auth, not the api's;
        # do NOT switch to Api-Key here.
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
