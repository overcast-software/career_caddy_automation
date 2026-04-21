"""Selector grounder — proposes CSS selectors that are verified against real HTML.

Takes the page HTML + the screenshot analyzer's free-text interaction hint
and asks an LLM to produce a selector. The selector is then validated by
running it through BeautifulSoup4 — if it doesn't match at least one element
in the HTML, it's rejected. This prevents the "hallucinated class name"
failure mode that pure-vision selector suggestions suffer from.
"""

from __future__ import annotations

import logging
from typing import Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from src.agents.agent_factory import get_model, resolve_model

logger = logging.getLogger(__name__)

# Keep HTML bounded so we don't blow the context window on large SPAs.
# Cookie banners / login walls / obstacle elements almost always sit near
# the top of the DOM or in fixed-position overlays emitted at the end of
# <body> — we give the LLM both regions.
_HTML_HEAD_CHARS = 40_000
_HTML_TAIL_CHARS = 10_000


class GroundedSelectors(BaseModel):
    obstacle_click_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for the element the interaction hint describes "
            "(e.g. the 'Accept all' cookie button). MUST be taken verbatim "
            "from classes/ids/attributes present in the provided HTML. "
            "Omit if no matching element is visible in the HTML."
        ),
    )
    ready_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for an element that reliably indicates the real "
            "job-content area. MUST exist in the provided HTML. Omit if "
            "you can't identify one confidently."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="Self-assessed confidence based on how well the HTML matched the hint.",
    )
    reasoning: str = Field(
        default="",
        description="One short line: which element you matched and why.",
    )


_SYSTEM_PROMPT = """\
You are building CSS selectors from real HTML. A separate vision pass has
looked at a screenshot of a failed scrape and produced a free-text hint
about what visible obstacle is blocking the page. Your job: find the
actual HTML element that matches that hint and return a CSS selector for it.

HARD RULES:
- Every selector you return MUST be built from classes, ids, or attributes
  that literally appear in the provided HTML. Do NOT invent class names
  like '.cookie-banner-button' unless you can see that exact string in
  the HTML.
- Prefer stable selectors: ids, data-testid attributes, unique class
  combinations, aria-label. AVOID bare tag selectors (button, div, h1)
  and position-based selectors (nth-child).
- If nothing in the HTML matches the hint, return null for that field
  and set confidence=low. Silence is better than a guess.
- For ready_selector: pick an element that marks the real job content,
  not the obstacle. Skip if the HTML doesn't obviously contain the job
  body (e.g. if the screenshot shows only a login wall, there's nothing
  to mark ready).

Selectors will be mechanically validated against the HTML — if no element
matches, the selector will be discarded and you'll have wasted tokens.
"""


def build_agent() -> Agent:
    model = resolve_model(get_model("selector_grounder"))
    return Agent(
        model,
        output_type=GroundedSelectors,
        system_prompt=_SYSTEM_PROMPT,
        name="selector_grounder",
    )


def _trim_html(html: str) -> str:
    """Keep head + tail of the HTML, skipping the middle when too large."""
    if len(html) <= _HTML_HEAD_CHARS + _HTML_TAIL_CHARS:
        return html
    head = html[:_HTML_HEAD_CHARS]
    tail = html[-_HTML_TAIL_CHARS:]
    return f"{head}\n\n<!-- ...{len(html) - _HTML_HEAD_CHARS - _HTML_TAIL_CHARS} chars elided... -->\n\n{tail}"


def _validate_selector(html: str, selector: str | None) -> str | None:
    """Return the selector unchanged if it matches ≥1 element in html, else None."""
    if not selector:
        return None
    try:
        soup = BeautifulSoup(html, "lxml")
        matches = soup.select(selector)
    except Exception as exc:
        logger.info("selector %r invalid: %s", selector, exc)
        return None
    if not matches:
        logger.info("selector %r matched 0 elements — discarding", selector)
        return None
    if len(matches) > 50:
        # Promiscuous — almost certainly not the obstacle.
        logger.info(
            "selector %r matched %d elements (too many) — discarding", selector, len(matches)
        )
        return None
    return selector


async def ground_selectors(
    html: str,
    failure_mode: str,
    summary: str,
    interaction_hint: str | None,
) -> GroundedSelectors:
    """Run the grounder and validate each proposed selector against the HTML."""
    trimmed = _trim_html(html)
    prompt_parts = [
        f"Failure mode (from screenshot): {failure_mode}",
        f"Screenshot summary: {summary}",
    ]
    if interaction_hint:
        prompt_parts.append(f"Interaction hint: {interaction_hint}")
    prompt_parts.append(f"\n--- HTML ({len(html)} chars total) ---\n{trimmed}")
    agent = build_agent()
    result = await agent.run("\n".join(prompt_parts))
    out = result.output
    return out.model_copy(
        update={
            "obstacle_click_selector": _validate_selector(html, out.obstacle_click_selector),
            "ready_selector": _validate_selector(html, out.ready_selector),
        }
    )
