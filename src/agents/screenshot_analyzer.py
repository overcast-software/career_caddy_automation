"""Screenshot analyzer agent — vision-LLM classification of failed scrape screenshots.

Given a screenshot of a failed scrape page and context (URL, failure note,
current scrape profile), produces a structured `ScreenshotAnalysis` describing
the failure mode and any actionable suggestions for the ScrapeProfile.

Scoped deliberately narrow: classify + suggest. Does NOT read or write the
profile itself — that's the orchestration script's job, so write policy
(propose vs auto-apply by field) is enforced outside the model's reach.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, BinaryContent

from src.agents.agent_factory import get_model, resolve_model


class ScreenshotAnalysis(BaseModel):
    failure_mode: Literal[
        "login_wall",
        "account_chooser",
        "captcha",
        "cookie_banner",
        "geo_block",
        "rate_limit",
        "paywall",
        "empty_content",
        "expired_listing",
        "unknown",
    ] = Field(description="Single best label for what the screenshot shows.")
    summary: str = Field(
        description="1-2 sentences describing what is on screen and why the scrape failed."
    )
    suggested_interaction_hint: str | None = Field(
        default=None,
        description=(
            "Free-text instruction referring to what is VISIBLE on screen "
            "(e.g. 'click the \"Accept all\" button', 'click Continue as "
            "<name>'). NEVER invent CSS selectors, class names, or DOM "
            "structure — you cannot see the DOM from a screenshot. Omit if "
            "nothing actionable."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description="Self-assessed confidence in the classification and hint.",
    )


_SYSTEM_PROMPT = """\
You are analyzing a screenshot of a web page that a headless scraper failed
on. Your job is to classify the failure and propose improvements for the
site's ScrapeProfile so future scrapes can handle this case automatically.

Classification rules:
- login_wall: Sign-in form shown to an unauthenticated user.
- account_chooser: "Welcome Back" / "Continue as <name>" rememberme interstitial.
- captcha: Cloudflare / hCaptcha / reCAPTCHA / Turnstile challenge.
- cookie_banner: GDPR/consent banner blocking content below.
- geo_block: "Not available in your region" / IP-restricted page.
- rate_limit: 429 / "Too many requests" / temporary block.
- paywall: Content behind a subscription/trial wall.
- empty_content: Page loaded but main content area is empty.
- expired_listing: The page renders a tombstone message like "Expired Job",
  "This position is no longer available", "Job has been removed/filled",
  "Posting closed". The listing itself is dead upstream — no selector or
  interaction will recover it. Omit all suggested_* selectors/hints in this
  case; the right downstream action is to close the application, not retry.
- unknown: None of the above fit.

Suggestion rules:
- You are looking at PIXELS, not HTML. You cannot see class names, ids, or
  DOM structure. NEVER output CSS selectors or invented markup — a separate
  grounding pass has the real HTML and will turn your hint into a selector.
- suggested_interaction_hint is free-text guidance describing what a human
  would do by looking at the screen. Reference visible text/labels exactly
  as shown (e.g. 'click the "Accept all" button', 'click Continue as Matt').
  NEVER suggest clicking sign-out, cancel, back, close, create an account,
  or sign up.
- Keep summary concise (1-2 sentences).
- confidence=low when the screenshot is unclear or you're guessing.
"""


def build_agent():
    """Build the screenshot analyzer agent. Returns a pydantic-ai Agent."""
    model = resolve_model(get_model("screenshot_analyzer"))
    return Agent(
        model,
        output_type=ScreenshotAnalysis,
        system_prompt=_SYSTEM_PROMPT,
        name="screenshot_analyzer",
    )


async def analyze_screenshot(
    png_bytes: bytes,
    url: str,
    failure_note: str | None,
    current_profile_excerpt: str | None = None,
) -> ScreenshotAnalysis:
    """Run the analyzer on one screenshot with its context."""
    agent = build_agent()
    parts: list = [BinaryContent(data=png_bytes, media_type="image/png")]
    ctx_lines = [f"Scraped URL: {url}"]
    if failure_note:
        ctx_lines.append(f"Failure note from poller: {failure_note}")
    if current_profile_excerpt:
        ctx_lines.append(f"Current profile css_selectors excerpt:\n{current_profile_excerpt}")
    parts.append("\n".join(ctx_lines))
    result = await agent.run(parts)
    return result.output
