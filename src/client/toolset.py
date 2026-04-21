"""
Pydantic-AI toolset for Career Caddy agents.

Wraps src/client/api_client.py functions as pydantic-ai tools so agents
can call them with automatic ApiClient construction from deps.
"""

import functools
import inspect
from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from src.client import api_client
from src.client.api_client import ApiClient

# ---------------------------------------------------------------------------
# Deps
# ---------------------------------------------------------------------------


@dataclass
class CareerCaddyDeps:
    """Dependencies for Career Caddy toolsets. Passed to Agent.run(deps=...)."""

    api_token: str
    base_url: str = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Tool registry — maps tool names to api_client functions
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Any] = {
    # Companies
    "create_company": api_client.create_company,
    "find_company_by_name": api_client.find_company_by_name,
    "search_companies": api_client.search_companies,
    "get_companies": api_client.get_companies,
    # Job posts
    "create_job_post_with_company_check": api_client.create_job_post_with_company_check,
    "create_job_post_minimal": api_client.create_job_post_minimal,
    "find_job_post_by_link": api_client.find_job_post_by_link,
    "search_job_posts": api_client.search_job_posts,
    "get_job_posts": api_client.get_job_posts,
    "update_job_post": api_client.update_job_post,
    # Job applications
    "create_job_application": api_client.create_job_application,
    "get_job_applications": api_client.get_job_applications,
    "get_applications_for_job_post": api_client.get_applications_for_job_post,
    "update_job_application": api_client.update_job_application,
    # Career data
    "get_career_data": api_client.get_career_data,
    # Resumes
    "get_resumes": api_client.get_resumes,
    # Questions & Answers
    "get_questions": api_client.get_questions,
    "get_answers": api_client.get_answers,
    # Scrapes
    "create_scrape": api_client.create_scrape,
    "get_scrapes": api_client.get_scrapes,
    "update_scrape": api_client.update_scrape,
    # Scores
    "score_job_post": api_client.score_job_post,
    "get_scores": api_client.get_scores,
}


# ---------------------------------------------------------------------------
# Named scopes — subsets of tools for different agent roles
# ---------------------------------------------------------------------------

SCOPES: dict[str, set[str]] = {
    "all": set(TOOL_REGISTRY.keys()),
    "job_discovery": {
        "find_company_by_name",
        "search_companies",
        "get_companies",
        "create_company",
        "create_job_post_with_company_check",
        "create_job_post_minimal",
        "find_job_post_by_link",
        "search_job_posts",
    },
    "application_tracking": {
        "create_job_application",
        "get_job_applications",
        "get_applications_for_job_post",
        "update_job_application",
        "update_job_post",
        "find_job_post_by_link",
    },
    "scrape_management": {
        "create_scrape",
        "get_scrapes",
        "update_scrape",
    },
}


# ---------------------------------------------------------------------------
# Wrapper builder
# ---------------------------------------------------------------------------


def _make_tool_wrapper(fn):
    """Build a RunContext-aware wrapper for an api_client function."""
    sig = inspect.signature(fn)
    original_params = list(sig.parameters.values())
    tool_params = original_params[1:]  # drop api: ApiClient

    @functools.wraps(fn)
    async def wrapper(ctx: RunContext[CareerCaddyDeps], **kwargs):
        api = ApiClient(ctx.deps.base_url, ctx.deps.api_token)
        return await fn(api, **kwargs)

    ctx_param = inspect.Parameter(
        "ctx",
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        annotation=RunContext[CareerCaddyDeps],
    )
    wrapper.__signature__ = sig.replace(parameters=[ctx_param] + tool_params)

    annotations = {"ctx": RunContext[CareerCaddyDeps]}
    for p in tool_params:
        if p.annotation != inspect.Parameter.empty:
            annotations[p.name] = p.annotation
    wrapper.__annotations__ = annotations

    return wrapper


# ---------------------------------------------------------------------------
# CareerCaddyToolset
# ---------------------------------------------------------------------------


def CareerCaddyToolset(
    scope: str | list[str] = "all",
    *,
    id: str | None = "career-caddy",
) -> FunctionToolset[CareerCaddyDeps]:
    """Build a scoped FunctionToolset from api_client functions.

    Args:
        scope: A named scope (e.g. "job_discovery") or a list of tool names.
        id: Toolset ID for pydantic-ai (must be unique per agent).

    Returns:
        A FunctionToolset ready to pass to Agent(toolsets=[...]).
    """
    if isinstance(scope, str):
        tool_names = SCOPES[scope]
    else:
        tool_names = set(scope)

    toolset: FunctionToolset[CareerCaddyDeps] = FunctionToolset(id=id)

    for name in sorted(tool_names):
        fn = TOOL_REGISTRY[name]
        toolset.add_function(
            _make_tool_wrapper(fn),
            takes_ctx=True,
            name=name,
        )

    return toolset
