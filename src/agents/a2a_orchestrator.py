"""
A2A Orchestrator — pydantic-ai agent that delegates to specialized sub-agents
via the Agent-to-Agent (A2A) protocol over HTTP/JSON-RPC 2.0.

Sub-agent servers (start with: caddy-gateway --mode a2a):
  email-agent    → http://localhost:3010
  career-caddy   → http://localhost:3011
  browser-agent  → http://localhost:3012

The orchestrator keeps costs down by routing to specialized sub-agents
instead of giving one agent all the tools. Each sub-agent is stateless —
all context must be embedded in the request string.

Usage:
    uv run caddy-orchestrator                # interactive REPL
    uv run caddy-orchestrator --web          # web UI on port 8090
    uv run caddy-orchestrator --web --port 9000
"""

from __future__ import annotations

from lib.observability import configure_logfire

configure_logfire("caddy-orchestrator")

import asyncio
import logging
import uuid
from typing import Any

import httpx
from pydantic_ai import Agent

from src.agents.agent_factory import get_model, register_defaults
from src.agents.history import sanitize_orphaned_tool_calls

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s  %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# A2A sub-agent endpoints
# ---------------------------------------------------------------------------

A2A_AGENTS: dict[str, str] = {
    "email": "http://localhost:3010",
    "caddy": "http://localhost:3011",
    "browser": "http://localhost:3012",
}


# ---------------------------------------------------------------------------
# A2A JSON-RPC helpers
# ---------------------------------------------------------------------------


async def _get_agent_card(base_url: str) -> dict[str, Any]:
    """Fetch the A2A agent card from /.well-known/agent-card.json."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{base_url}/.well-known/agent-card.json")
        resp.raise_for_status()
        return resp.json()


async def _send_task(base_url: str, message: str, context_id: str | None = None) -> dict[str, Any]:
    """
    Send a task to an A2A agent and return the completed task dict.

    Uses message/send (JSON-RPC 2.0) and polls tasks/get until the task
    reaches a terminal state (completed / failed / canceled).
    """
    msg: dict[str, Any] = {
        "role": "user",
        "parts": [{"kind": "text", "text": message}],
        "kind": "message",
        "messageId": str(uuid.uuid4()),
    }
    if context_id:
        msg["contextId"] = context_id

    async with httpx.AsyncClient(timeout=120.0) as client:
        send_resp = await client.post(
            base_url,
            json={
                "jsonrpc": "2.0",
                "method": "message/send",
                "params": {"message": msg},
                "id": str(uuid.uuid4()),
            },
        )
        send_resp.raise_for_status()
        send_body = send_resp.json()

        if "error" in send_body:
            raise RuntimeError(f"A2A message/send error: {send_body['error']}")

        task = send_body.get("result", {})
        task_id = task.get("id")
        state = task.get("status", {}).get("state", "unknown")

        # Poll until terminal state with exponential backoff
        poll_delay = 0.5
        while state not in ("completed", "failed", "canceled"):
            await asyncio.sleep(poll_delay)
            poll_delay = min(poll_delay * 1.5, 5.0)

            get_resp = await client.post(
                base_url,
                json={
                    "jsonrpc": "2.0",
                    "method": "tasks/get",
                    "params": {"id": task_id},
                    "id": str(uuid.uuid4()),
                },
            )
            get_resp.raise_for_status()
            get_body = get_resp.json()

            if "error" in get_body:
                raise RuntimeError(f"A2A tasks/get error: {get_body['error']}")

            task = get_body.get("result", {})
            state = task.get("status", {}).get("state", "unknown")
            logger.debug(f"Task {task_id} state: {state}")

        return task


def _extract_output(task: dict[str, Any]) -> str:
    """Pull the text result out of a completed A2A task."""
    state = task.get("status", {}).get("state")
    if state == "failed":
        return f"Task failed: {task.get('status', {}).get('message', 'unknown error')}"

    # Prefer artifacts (the agent's durable output)
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "text":
                return part["text"]
            if part.get("kind") == "data":
                return str(part.get("data", ""))

    # Fall back to messages from the agent turn
    for message in reversed(task.get("history", [])):
        if message.get("role") == "agent":
            for part in message.get("parts", []):
                if part.get("kind") == "text":
                    return part["text"]

    return "(no output)"


# ---------------------------------------------------------------------------
# Orchestrating pydantic-ai agent with A2A tool wrappers
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """
You are a senior Career Caddy orchestrator.  You have three high-level tools
that each delegate to a specialized sub-agent running as an A2A service:

- call_caddy_agent     — manages job posts, companies, and applications in the
                         Career Caddy system (deduplication, creation, updates)
                         It acts as your access to documents around job posts
                         job applications and career history.
                         most of your work is around asking it to perform CRUD
                         operations on your behalf
- call_email_agent     — searches the user's email for job posts and URLs
                         Called only for explicitly requested email related tasks.
                         otherwise assume caddy agent is your source of info.
                         Occasionally you will come here to extract job posts to
                          add to the api.

- call_browser_agent   — scrapes a job post URL and returns structured details
                       - call this agent as a last resort.

Each tool accepts a plain-English request and returns a finished answer.
You do NOT call low-level APIs yourself; delegate all work to the appropriate
agent tool and synthesize the results for the user.

## Typical workflow for processing new job emails

1. call_email_agent — find unprocessed job emails and extract their URLs.
2. call_browser_agent — for each URL, scrape the job post details.
3. call_caddy_agent — save each job (handles company lookup and deduplication).
4. Report a summary: jobs found, jobs added, any errors.

## Critical: each sub-agent is stateless

Every tool call runs in isolation — the sub-agent cannot see your conversation
history.  Embed ALL relevant context in the request string you pass to it.

Bad:  call_caddy_agent("save the job")
Good: call_caddy_agent("Save: title='Senior SWE', company='Acme', url='https://...'")

## When you receive a result from a sub-agent

Return it DIRECTLY to the user.  Do NOT call the same tool again.  Do NOT ask
the user to "try again".  If the result contains a list, emails, jobs, or data
the user asked for — present it immediately.

If a tool returns an error (any message containing "error", "failed", or
"connection"), report it to the user and STOP — do not retry.  Retrying a
failed connection will never succeed.

Some sites you visit will obfuscate the employer.
Don't put in any company, if it's unclear after browsing - use the hostname of the url.
for instance, Toptal doesn't display the company and you should put the company as toptal.
This isn't ideal, but it's the best option if all other options are exhausted.

## Job application statuses

Use exactly these canonical values when creating/updating applications:

- Unvetted — Not Started
- Vetted Good — In Progress
- Applied — In Progress
- Contact — In Progress
- Interview Scheduled — In Progress
- Interviewed — In Progress
- Technical Test — In Progress
- Awaiting Decision — In Progress
- Offer — Completed
- Accepted — Completed
- Declined — No-Go
- Vetted Bad — No-Go
- Rejected — No-Go
- Expired — No-Go
- Archived — Archived

Synonym mapping (normalize before calling call_caddy_agent):

- "applied", "application" -> Applied
- "interview scheduled", "phone screen scheduled" -> Interview Scheduled
- "tech test", "technical assessment", "take-home" -> Technical Test
- "awaiting decision", "pending decision" -> Awaiting Decision
- "offer extended" -> Offer
- "offer accepted" -> Accepted
- "declined", "withdrew", "withdrawn" -> Declined
- "rejected", "not selected", "no-go" -> Rejected
- "expired", "posting closed", "closed" -> Expired

Always emit one of the canonical statuses above in your responses.
"""

register_defaults()

a2a_orchestrator = Agent(
    get_model("caddy"),
    name="a2a_orchestrator",
    system_prompt=SYSTEM_PROMPT,
    history_processors=[sanitize_orphaned_tool_calls],
)


@a2a_orchestrator.tool_plain
async def call_email_agent(request: str) -> str:
    """
    Delegate a request to the Email Agent via A2A.

    Use for: searching emails, extracting job URLs, classifying messages.
    Always include all necessary context in the request string.
    """
    try:
        task = await _send_task(A2A_AGENTS["email"], request)
        return _extract_output(task)
    except Exception as exc:
        logger.exception("email A2A call failed")
        return f"Email agent error: {exc}"


@a2a_orchestrator.tool_plain
async def call_caddy_agent(request: str) -> str:
    """
    Delegate a request to the Career Caddy Agent via A2A.

    Use for: creating job posts, checking duplicates, managing companies and
    applications.  Embed full job details (title, company, URL, description,
    location) in the request — the agent has no memory of prior calls.
    """
    try:
        task = await _send_task(A2A_AGENTS["caddy"], request)
        return _extract_output(task)
    except Exception as exc:
        logger.exception("caddy A2A call failed")
        return f"Career Caddy agent error: {exc}"


@a2a_orchestrator.tool_plain
async def call_browser_agent(request: str) -> str:
    """
    Delegate a request to the Browser Agent via A2A.

    Use for: scraping job post pages, navigating URLs, extracting structured
    data from websites.  Always include the full URL in the request.
    """
    try:
        task = await _send_task(A2A_AGENTS["browser"], request)
        return _extract_output(task)
    except Exception as exc:
        logger.exception("browser A2A call failed")
        return f"Browser agent error: {exc}"


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

app = a2a_orchestrator.to_web(
    models=["openai:gpt-4o-mini"],
    instructions="Career Caddy A2A Orchestrator — delegates to email, caddy, and browser sub-agents via A2A.",
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


async def discover_agents() -> None:
    """Print the agent card for each registered A2A sub-agent."""
    for name, url in A2A_AGENTS.items():
        try:
            card = await _get_agent_card(url)
            print(f"\n[{name}] {url}")
            print(f"  name:        {card.get('name')}")
            print(f"  description: {card.get('description')}")
            print(f"  version:     {card.get('version')}")
            skills = card.get("skills", [])
            if skills:
                print(f"  skills:      {[s.get('name') for s in skills]}")
        except Exception as exc:
            print(f"\n[{name}] {url}  — unreachable: {exc}")


# ---------------------------------------------------------------------------
# Standalone REPL + Web entry point
# ---------------------------------------------------------------------------


async def _repl():
    print("Career Caddy A2A Orchestrator")
    print("Discovering sub-agents...")
    await discover_agents()
    print("\nType 'quit' to exit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input or user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        try:
            result = await a2a_orchestrator.run(user_input)
            print(f"\nCaddy: {result.output}\n")
        except Exception as exc:
            logger.exception("Orchestrator run failed")
            print(f"\nError: {exc}\n")


def run():
    import sys

    if "--web" in sys.argv:
        import uvicorn

        port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8090
        print(f"A2A Orchestrator web UI: http://127.0.0.1:{port}")
        uvicorn.run(app, host="127.0.0.1", port=port)
    else:
        asyncio.run(_repl())


if __name__ == "__main__":
    run()
