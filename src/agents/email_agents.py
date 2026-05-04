"""Agent factories for the email-triage pipeline.

Three agents collaborate in ``scripts/inbox_triage.py`` to move an email
through the pipeline:

    classify  → is this email job-related at all?       (string output)
    refine    → new posting vs. correspondence?         (structured)
    followup  → which application? what status now?     (structured)

The actual pydantic-ai ``Agent`` objects are built through ``agent_factory``
so model resolution, MCP toolsets, and history sanitisation work uniformly.
This module owns the *prompts* and the *structured output schemas*.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from src.agents.agent_factory import get_agent, register_defaults

register_defaults()


# ---------------------------------------------------------------------------
# Stage 1 — classifier: broad "is it job-related?" filter.
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """You are an email classifier. You will be given a single email ID.

Your job:
1. Read the email using read_email(email_id, classify=True, max_content_length=1500).
   classify=True strips tracking URLs / marketing boilerplate — use it every time.
2. Determine if the email is job-related in any way: a new posting, recruiter
   outreach, interview correspondence, rejection, offer, scheduling — anything
   that concerns a job search.

Do NOT call tag_email — the caller handles tagging based on your reply.

Reply with exactly one line and nothing else:
  job_post <subject>          (if the email is job-related)
  not_job_post <subject>      (if it is not)"""


def get_classify_agent(model: str | None = None) -> Agent:
    """Build the stage-1 broad classifier."""
    return get_agent(
        "email_classifier",
        system_prompt=_CLASSIFY_PROMPT,
        model=model,
    )


# ---------------------------------------------------------------------------
# Stage 2 — refiner: new posting vs. in-flight correspondence.
# ---------------------------------------------------------------------------


class RefineResult(BaseModel):
    """Output schema for the stage-2 refiner."""

    kind: Literal["new_post", "follow_up", "direct_solicitation"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(description="One short sentence from the email supporting the choice.")


_REFINE_PROMPT = """You are refining emails that have already been flagged as job-related.

You will be given a single email ID. Read it with
  read_email(email_id, classify=True, max_content_length=2000)

Decide between three categories:

- new_post: the email contains a link to a NEW job posting you could apply to.
  Examples: a listing on LinkedIn/Indeed/company careers page; a job-board
  digest that points at specific openings; a recruiter pasting an ATS link
  to a role they want to submit you to. The defining trait is a SCRAPEABLE
  URL pointing at a single job.

- direct_solicitation: a job-related email that contains the role description
  INLINE (responsibilities, qualifications, location, sometimes comp) but
  NO scrapeable posting URL. Recruiter cold-outreach is the canonical
  example: the recruiter pastes the JD into the body and asks you to reply
  with your resume. No "Apply here" link, or only a generic
  agency/company homepage. The body is rich enough to stand on its own
  as a job post.

- follow_up: correspondence about a role already in progress. Examples:
  "Thanks for applying — we'd like to schedule a phone screen", an interview
  reminder, a take-home assignment email, a rejection, an offer letter, a
  recruiter replying to your prior outreach.

If the email contains BOTH a new posting URL and an inline JD, prefer
new_post (the URL is the more durable identifier). If it contains BOTH a
new posting and follow-up content, prefer new_post (the follow-up will be
re-seen in the reply thread).

Return RefineResult. `evidence` must be a short quoted sentence from the
email body. If you cannot decide confidently, set confidence < 0.6 — the
caller will treat low-confidence outputs as new_post.
"""


def get_refine_agent(model: str | None = None) -> Agent:
    """Build the stage-2 refiner (new_post vs. follow_up)."""
    return get_agent(
        "job_post_refiner",
        system_prompt=_REFINE_PROMPT,
        model=model,
        output_type=RefineResult,
    )


# ---------------------------------------------------------------------------
# Stage 3 — follow-up processor: find the application, pick the new status.
# ---------------------------------------------------------------------------


# Canonical set drawn from src/agents/a2a_orchestrator.py (notes.org:290-292).
# Re-confirm against the server enum before trusting auto-updates in loop mode.
JobApplicationStatus = Literal[
    "Applied",
    "Interview Scheduled",
    "Technical Test",
    "Awaiting Decision",
    "Offer",
    "Accepted",
    "Declined",
    "Rejected",
    "Expired",
    "Archived",
]


class FollowupResult(BaseModel):
    """Output schema for the stage-3 follow-up processor."""

    application_id: int | None = Field(
        description=(
            "ID of the matched job_application, or null if no confident match. "
            "The orchestrator does NOT create new applications; null means skip."
        ),
    )
    new_status: JobApplicationStatus | None = Field(
        description="The inferred status, or null when application_id is null."
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(
        description="One short quoted sentence from the email supporting the status."
    )
    notes: str = Field(description="1-sentence human-readable summary for the application notes.")


_FOLLOWUP_PROMPT = """You are processing a correspondence email about a job the user
has already applied to (or been contacted about). Find the matching
job_application in the Career Caddy API and return the new status implied
by the email.

You have tools for both the active email backend (read_email, list_emails,
search_email, ...) and the Career Caddy application-tracking API
(find_job_post_by_link, get_applications_for_job_post, get_job_applications).

Mapping strategies, in order. Stop at the first confident match:

1. URL in body — if the email quotes a job-post URL, call
   find_job_post_by_link then get_applications_for_job_post.
2. Thread peers — search emails in the same thread (list_emails /
   search_email by subject or message-id) that were previously tagged
   'job_post' or 'follow_up'; if one maps to an application, reuse it.
3. Company + role — call get_job_applications WITH the `company=`
   parameter set to the company name inferred from the sender domain or
   signature (e.g. company="starbucks"). Calling get_job_applications
   without `company=` (or `company_id=`) is FORBIDDEN in this stage —
   listing every application and picking one is a known failure mode.
   Only return a match if exactly one recent application is returned;
   otherwise skip with confidence < 0.6.

Map the email's content to one of these statuses (exact strings):
  Applied, Interview Scheduled, Technical Test, Awaiting Decision,
  Offer, Accepted, Declined, Rejected, Expired, Archived.

DO NOT create a new application. If you cannot find a confident match,
return application_id=null, new_status=null, confidence < 0.6 and explain
in `notes`.

Return FollowupResult. `evidence` must be a short quoted sentence from the
email. `notes` is a 1-line summary the user will see on the application.
"""


def get_followup_agent(model: str | None = None) -> Agent:
    """Build the stage-3 follow-up processor."""
    return get_agent(
        "followup_processor",
        system_prompt=_FOLLOWUP_PROMPT,
        model=model,
        output_type=FollowupResult,
    )


# ---------------------------------------------------------------------------
# Stage 4 — inline-post extractor: pull a JobPost out of a JD that lives
# entirely in the email body (recruiter cold-outreach, no scrapeable URL).
# ---------------------------------------------------------------------------


class InlinePostResult(BaseModel):
    """Structured fields extracted from an inline-JD email."""

    title: str = Field(description="Role title. Empty string when not confidently inferable.")
    company: str | None = Field(
        default=None,
        description=(
            "Employer / agency name. Use the recruiter's company (signature, "
            "sender domain) when no explicit hiring company is given. None "
            "if not confidently inferable."
        ),
    )
    description: str = Field(
        description=(
            "The inline JD verbatim or lightly cleaned (responsibilities, "
            "qualifications, nice-to-haves, location, comp, recruiter contact). "
            "Do NOT invent content. Strip unsubscribe footers and tracking pixels."
        ),
    )
    location: str | None = Field(default=None)
    salary_min: int | None = Field(default=None)
    salary_max: int | None = Field(default=None)
    remote_ok: bool = Field(default=False)
    recruiter_contact: str | None = Field(
        default=None,
        description="Recruiter name + email/phone in one line, for the post's source attribution.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(
        description="One short quoted sentence from the email confirming this is a real role.",
    )


_INLINE_POST_PROMPT = """You are processing an email that contains a job
description INLINE — there is no scrapeable URL, only the role text in the
body. Your job is to extract a faithful, structured JobPost.

Read the email with
  read_email(email_id, classify=True, max_content_length=4000)

Extract the structured fields:

- title: the role title (e.g. "Senior Software Engineer – Full Stack").
  Pull from the email subject or the first heading-like line in the body.
- company: the hiring company if explicit ("we're hiring at Acme…"); else
  the recruiter's agency from the signature; else the sender domain.
  If none of these are confident, return null — DO NOT invent.
- description: the JD text verbatim or lightly cleaned. Keep responsibilities,
  required/nice-to-have qualifications, location, comp range, recruiter
  contact, and any notes about the engagement. STRIP unsubscribe footers,
  tracking-pixel HTML, "click here to update preferences" boilerplate.
- location, salary_min, salary_max, remote_ok: populate when the JD states
  them. salary_min/max are integers (USD/year). remote_ok is true only when
  the JD explicitly says remote/hybrid; otherwise false.
- recruiter_contact: one-line "Name <email>" or "Name <email> | phone" from
  the signature. Used for source attribution at the top of the description.

Return InlinePostResult. `evidence` must be a short quoted sentence from
the email confirming this is a real job (responsibilities/qualifications
language). If the email turns out to be too thin to stand as a JobPost
(generic teaser, "see our careers page"), set confidence < 0.6 and the
caller will skip without creating a post.

DO NOT invent details. Empty/null is always preferable to a guess.
"""


def get_inline_post_agent(model: str | None = None) -> Agent:
    """Build the stage-4 inline-JD extractor."""
    return get_agent(
        "inline_post_extractor",
        system_prompt=_INLINE_POST_PROMPT,
        model=model,
        output_type=InlinePostResult,
    )


# ---------------------------------------------------------------------------
# Backend selection helper.
# ---------------------------------------------------------------------------


def current_backend() -> str:
    """Which email MCP backend this process is configured for."""
    return os.environ.get("CADDY_EMAIL_BACKEND", "notmuch").lower()
