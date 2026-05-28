"""Agent that extracts job-posting links and surrounding context from email.

Given raw email text (headers + body), returns a list of (url, title,
company, description) records. Filters out non-job URLs — unsubscribe,
tracking pixels, CDN assets, social profiles, homepages, etc.

Used by scripts/process_tagged.py to populate Career Caddy with job-posts
that the user can review and optionally run a scrape against.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent

from src.agents.agent_factory import get_model
from src.agents.usage_reporter import report_usage

logger = logging.getLogger(__name__)


# Hosts that always redirect to the real job URL. Unique per recipient/email,
# so two users getting the same role see different URLs and server-side
# dedup on `link` can't see they match.
_TRACKER_HOST_RE = re.compile(
    r"(?i)^("
    r"url\d*\.alerts\.jobot\.com"
    r"|click\.ziprecruiter\.com"
    r"|email\.mg\d*\.ziprecruiter\.com"
    r"|email\.mg\.ziprecruiter\.com"
    r"|url\d*\.mailmunch\.co"
    r"|email\.[a-z0-9-]+\.mailgun\.org"
    r"|links?\.[a-z0-9.-]+\.sendgrid\.net"
    r"|u\d+\.ct\.sendgrid\.net"
    r"|trk\.[a-z0-9.-]+"
    r"|click\.[a-z0-9.-]+"
    r"|t\.[a-z0-9.-]+"
    r")$"
)

# Query params to strip from canonical URLs. Safe to drop — none affect which
# job listing the URL points at.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "trackingId",
    "refId",
    "lipi",
    "eid",
    "midToken",
    "midSig",
    "otpToken",
    "trk",
    "trkEmail",
    "tsid",
    "ssid",
    "fmid",
    "email_source",
    "email_token",
}


_ENCODED_DELIMITERS = ("%22", "%27", "%3e", "%3c")  # " ' > <


def _sanitize_url(url: str) -> str:
    """Strip trailing whitespace and stray HTML-delimiter chars (literal or
    percent-encoded) that leak in when a tracker captures a URL straight
    out of an `href="..."` attribute. Iterates until idempotent so mixed
    cases like `...%22"` or `...%22%22` collapse fully."""
    while True:
        trimmed = url.strip().strip("\"'<>")
        lower = trimmed.lower()
        for enc in _ENCODED_DELIMITERS:
            if lower.endswith(enc):
                trimmed = trimmed[: -len(enc)]
                break
        if trimmed == url:
            return trimmed
        url = trimmed


def _strip_tracking_params(url: str) -> str:
    """Drop known tracking query params from a URL, preserve everything else."""
    url = _sanitize_url(url)
    try:
        p = urlparse(url)
    except ValueError:
        return url
    if not p.query:
        return url
    kept = [
        (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in _TRACKING_PARAMS
    ]
    return urlunparse(p._replace(query=urlencode(kept)))


_DEAD_LINK_MARKERS = re.compile(
    r"(?i)"
    r"wrong link"
    r"|invalid link"
    r"|you have clicked on an invalid"
    r"|this (job|position|posting) (is )?(no longer|has been) (available|removed|filled)"
    r"|job (not found|expired|has been removed)"
    r"|expired job"
    r"|posting (no longer|has been) (available|active)"
    r"|page not found"
    r"|job you.re looking for"
    r"|we.re sorry,? but this job has expired"
)

# Hosts that serve "listing expired" pages at HTTP 200 rather than 404, so
# a bare status check doesn't catch them. For these we do a bounded GET and
# scan for _DEAD_LINK_MARKERS even though they aren't tracker redirects.
# Extend as new aggregators show up.
_DEAD_CHECK_HOST_RE = re.compile(
    r"(?i)^("
    r"(www\.)?hiring\.cafe"
    r"|(www\.)?hiringcafe\.com"
    r")$"
)


async def _peek_for_dead_marker(client: httpx.AsyncClient, url: str) -> bool:
    """Bounded GET that returns True iff the response body contains a dead-
    listing marker. Network errors → False (don't drop on transient issues)."""
    try:
        async with client.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=3.0,
            headers={"User-Agent": "Mozilla/5.0 (CareerCaddyResolver)"},
        ) as r:
            if r.status_code >= 400:
                return False
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) >= 16_384:
                    break
    except (TimeoutError, httpx.HTTPError) as exc:
        logger.debug("dead-marker peek failed for %s: %s", url, exc)
        return False
    return bool(_DEAD_LINK_MARKERS.search(body.decode("utf-8", errors="ignore")))


_REDIRECT_HOP_BUDGET = 10


async def _resolve_one(client: httpx.AsyncClient, url: str) -> str | None:
    """Follow redirects for a tracker URL. Returns canonical URL, or None if
    the tracker is dead. Non-tracker URLs skip the network call entirely.

    Redirects are followed *manually* (not via httpx's follow_redirects=True)
    so we can sanitize the Location header at each hop before httpx parses
    it. The motivating case: SendGrid's redirect target sometimes ends with
    a literal `"` — the closing quote of an HTML `href="..."` that leaked
    into the tracker payload. httpx canonicalizes URLs on parse, so that `"`
    becomes `%22` in the next request, which the destination treats as a
    404. Stripping the bad char between hops keeps the chain alive.

    Uses GET with a bounded body read because some trackers (Jobot/SendGrid)
    serve error pages at HTTP 200 on the tracker domain with "Wrong Link"
    text — HEAD can't see this. We only peek at the first 16KB; that's
    plenty to catch error-page markers near the top of the HTML.
    """
    try:
        host = urlparse(url).netloc
    except ValueError:
        return url
    if not _TRACKER_HOST_RE.match(host):
        canonical = _strip_tracking_params(url)
        if _DEAD_CHECK_HOST_RE.match(host):
            if await _peek_for_dead_marker(client, canonical):
                logger.info("%s serves an expired-listing page — dropping", canonical)
                return None
        return canonical

    current = url
    body = b""
    final_url = ""
    try:
        for _hop in range(_REDIRECT_HOP_BUDGET):
            async with client.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=3.0,
                headers={"User-Agent": "Mozilla/5.0 (CareerCaddyResolver)"},
            ) as r:
                if r.is_redirect:
                    loc = r.headers.get("location", "")
                    if not loc:
                        logger.info("tracker %s: empty Location at hop — dropping", url)
                        return None
                    # Sanitize BEFORE httpx parses: strip leaked HTML href
                    # delimiters from the Location target so the next hop
                    # doesn't fetch a URL-encoded `%22` and 404.
                    loc = _sanitize_url(loc)
                    current = str(httpx.URL(current).join(loc))
                    continue
                if r.status_code >= 400:
                    logger.info(
                        "tracker %s ended at %s with %d — dropping",
                        url,
                        current,
                        r.status_code,
                    )
                    return None
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) >= 16_384:
                        break
                final_url = current
                break
        else:
            logger.info("tracker %s exceeded redirect budget — dropping", url)
            return None
    except (TimeoutError, httpx.HTTPError) as exc:
        logger.debug("tracker resolve failed, keeping raw URL %s: %s", url, exc)
        return _strip_tracking_params(url)

    text = body.decode("utf-8", errors="ignore")
    if _DEAD_LINK_MARKERS.search(text):
        logger.info("tracker %s resolved to error page — dropping", url)
        return None

    # Still sitting on the tracker host after redirects → the tracker served
    # its own error/landing page rather than redirecting us to a job.
    if _TRACKER_HOST_RE.match(urlparse(final_url).netloc):
        logger.info("tracker %s never redirected off tracker domain — dropping", url)
        return None

    return _strip_tracking_params(final_url)


async def canonicalize_urls(links: list[JobLink]) -> list[JobLink]:
    """Resolve tracker redirects, strip tracking params, drop dead trackers,
    deduplicate by canonical URL (keeping the richest description/company)."""
    async with httpx.AsyncClient() as client:
        resolved = await asyncio.gather(
            *(_resolve_one(client, link.url) for link in links),
            return_exceptions=False,
        )

    by_url: dict[str, JobLink] = {}
    for link, canonical in zip(links, resolved, strict=True):
        if canonical is None:
            continue
        link = link.model_copy(update={"url": canonical})
        prev = by_url.get(canonical)
        if prev is None:
            by_url[canonical] = link
            continue
        # Merge: prefer the record with more context.
        best = prev if len(prev.description) >= len(link.description) else link
        if not best.company and link.company:
            best = best.model_copy(update={"company": link.company})
        by_url[canonical] = best

    return list(by_url.values())


class JobLink(BaseModel):
    url: str = Field(description="The job listing URL.")
    title: str = Field(description="Short human-readable role title. Never empty.")

    @field_validator("url", mode="before")
    @classmethod
    def _strip_url_quotes(cls, v: str) -> str:
        return _sanitize_url(v)

    company: str = Field(
        default="",
        description="Employer name. Empty string when not confidently inferable.",
    )
    description: str = Field(
        default="",
        description="Useful role context from the email body. Empty when none.",
    )


class ExtractedUrls(BaseModel):
    job_urls: list[JobLink] = Field(default_factory=list)
    reasoning: str = Field(
        default="",
        description="One-line note on what was kept and what was filtered.",
    )


_SYSTEM_PROMPT = """\
You extract job-posting links from an email and capture whatever useful
context the email provides about each one. Return structured JobLink records.

URL FILTER — keep things that point at the way to apply to a SPECIFIC
role. Usually that's a job-listing URL; for direct-solicitation emails
(a recruiter writing "send your resume to hiring@acme.com") the apply
target IS the email address itself, so the address becomes the URL.
Allowed schemes: http, https, mailto.

KEEP:
  - /jobs/<id>, /job/<slug>, /careers/<role>, lever.co/<co>/<uuid>,
    greenhouse.io/<co>/jobs/<id>, workday <url>/job/<id>, etc.
  - Direct job-board listing URLs (LinkedIn /jobs/view/<id>, Indeed
    /viewjob?jk=<id>, Glassdoor /job-listing/<slug>)
  - ATS apply links when they are the only link to the role
  - mailto:<address> when the email is a direct solicitation and the
    recruiter asks the candidate to reply / send a resume to that
    address. In that case the address IS the apply link — emit it as
    the literal `mailto:foo@bar.com` form (no bare address, no separate
    web URL invented for the role).

REJECT:
  - Homepages, /careers root, /jobs search pages with no id
  - Unsubscribe, preferences, privacy, terms, help, profile, settings
  - Tracking pixels, open-tracking, utm-only wrappers around known bad targets
  - CDN assets: .png .jpg .svg .gif .ico .css .js, logo/*, images/*
  - Social PROFILE links (twitter/linkedin profiles, company pages that
    don't link to a role)
  - App-store links, calendar invites, zoom/meet links

When in doubt, reject. Empty list is a valid answer.

PER-FIELD GUIDANCE:

title — never empty. Priority:
  1. Anchor text of the link
  2. Email Subject header (the email text you receive includes headers)
  3. Recruiter's wording around the link
  4. URL's last path segment cleaned up (`senior-backend-engineer` →
     `Senior Backend Engineer`)

company — best-effort, "" if not confident. Priority:
  1. Recruiter's employer / signature block
  2. Explicit mention ("we're hiring at Acme…")
  3. Sender email domain (skip marketing subdomains: mail., notifications.)
  4. Job URL domain when it's an employer site (jobs.stripe.com → Stripe).
     NOT for job boards: linkedin.com, indeed.com, lever.co, greenhouse.io,
     workday.com, ashbyhq.com, wellfound.com — leave "" for these.

description — useful role info from the email body: summary blurb, comp
range, location, tech stack, interview process, recruiter's name/contact.
Verbatim or light paraphrase. Do NOT invent content. Do NOT copy
boilerplate signatures, unsubscribe text, or link dumps. Empty string when
the email only has a title + URL (job-board digests).

If multiple URLs share the same role context (recruiter email with one
job link plus auxiliary links like company homepage / LinkedIn profile),
apply the description to the PRIMARY job URL only — auxiliary links get
an empty description.

Populate `reasoning` with one short line: how many kept, how many dropped,
most common drop reason.
"""


def build_url_extractor_agent() -> Agent:
    return Agent(
        get_model("job_extractor"),
        name="url-extractor",
        system_prompt=_SYSTEM_PROMPT,
        output_type=ExtractedUrls,
    )


async def extract_job_urls(
    email_text: str,
    api_token: str = "",
    pipeline_run_id: str | None = None,
) -> ExtractedUrls:
    """Run the extractor on an email body. Returns ExtractedUrls (may be empty).

    Post-processes the LLM output by resolving tracker redirects, stripping
    tracking query params, and deduplicating on the canonical URL.
    """
    agent = build_url_extractor_agent()
    result = await agent.run(email_text)
    if api_token:
        await report_usage(
            api_token=api_token,
            agent_name="url_extractor",
            model_name=get_model("job_extractor"),
            usage=result.usage(),
            trigger="inbox_triage",
            pipeline_run_id=pipeline_run_id,
        )
    extracted = result.output
    before = len(extracted.job_urls)
    extracted.job_urls = await canonicalize_urls(extracted.job_urls)
    after = len(extracted.job_urls)
    if after != before:
        extracted.reasoning = (
            f"{extracted.reasoning} [canonicalized: {before}→{after}, {before - after} dedup/dead]"
        ).strip()
    return extracted
