"""Span-attribute hygiene for the email-triage agents."""

# REMOVE WHEN STABLE — observability scaffolding for inbox debugging.
#
# `instrument_pydantic_ai()` already wraps every agent run in a logfire span,
# but the span name embeds `email_id` in the task-string ("Classify email id:
# 20260430002335.9acdbcd675e8afd8@..."), which makes logfire structured
# queries useless — you can't `WHERE attributes->>'email_id' = X` because the
# id is part of the span name, not an attribute.
#
# This decorator wraps `_run_classify` / `_run_inline_post` (and the
# `extract_job_urls` call) to attach `email_id` (and a `stage` label) as REAL span
# attributes on the outer span we open here. Inner provider spans still carry
# the task-string in the name; the outer span gives logfire a queryable
# pivot.
#
# Once provider spans are queryable on their own (or pydantic-ai exposes a
# hook to add attributes to instrumented spans), this whole module can be
# deleted with no behavioural change — the agent calls would just lose the
# extra wrapper span.

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import logfire

T = TypeVar("T")


def trace_agent_run(
    *,
    stage: str,
    email_id_arg: str = "email_id",
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Wrap an async function whose signature includes `email_id` so the
    inner agent call runs inside a logfire span tagged with structured
    `email_id` and `stage` attributes.

    Args:
        stage: short label — `classify`, `refine`, `followup`, `inline_post`,
               `extract_urls`. Used as the span name and a span attribute.
        email_id_arg: parameter name to pull email_id from when scanning
                      *args / **kwargs. Defaults to `email_id`.

    The decorator is zero-cost when logfire isn't configured — `logfire.span`
    is a no-op in that case, so the only overhead is one async-context-manager
    enter/exit per call.
    """

    def deco(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            email_id = kwargs.get(email_id_arg)
            if email_id is None:
                # Positional fallback: scan args for a string that looks like
                # an email id. The triage call sites all pass email_id as the
                # second positional after the agent, so check args[1] first.
                for candidate in args[1:]:
                    if isinstance(candidate, str):
                        email_id = candidate
                        break
            with logfire.span(
                "triage.agent.{stage}",
                stage=stage,
                email_id=email_id or "",
            ):
                return await fn(*args, **kwargs)

        return wrapper

    return deco
