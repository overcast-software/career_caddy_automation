"""Forward-path triage — a light pass over the emails Doug forwards to
``forwarding@careercaddy.online``.

Doug triages job mail himself in Thunderbird and forwards the keepers to the
forward recipient; the forward IS the "evaluate this" signal. So this is a
light pass, not a classifier gauntlet — two deterministic paths per forward:

    stage 1 (classify)  →  one cheap "is this a job?" check; tag `evaluated`
                           (a resume checkpoint) and `job_post` when it is.
    stage E (extract)   →  render the body (html-only forwards included),
                           extract links, create a JobPost per link, and hand
                           known-good links to the poller as `hold` scrapes
                           carrying `job_post_id` so the runner AUGMENTS the
                           just-created post (never mints a second one).
    stage I (inline)    →  fallback when a forward has no link: pull a
                           link-less JobPost out of a JD pasted inline.

`caddy_processed` is written on EVERY terminal path so the
``NOT tag:caddy_processed`` selector never re-runs the LLMs on the backlog.
All tag reads/writes are MESSAGE-granular (``meta.id``), never thread —
a forward sharing a thread with an already-processed original is judged on
its own state (AUTO-32). Refine + follow-up handling are deliberately gone.

Backend is chosen by ``CADDY_EMAIL_BACKEND`` (``notmuch`` default, ``imap``
when implemented).

**Do not run this alongside** ``caddy-classify`` / ``caddy-process`` against
the same mailbox — they mutate the same tags and will race.

Usage:
    uv run caddy-inbox                       # loop every 15 minutes
    uv run caddy-inbox --once --limit 5
    uv run caddy-inbox --backend notmuch
"""

from lib.observability import configure_logfire

configure_logfire("caddy-inbox")

try:
    import logfire
except ImportError:  # logfire is optional — heartbeat/flush degrade to no-ops
    logfire = None  # type: ignore[assignment]

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from src.agents.email_agents import (
    InlinePostResult,
    get_classify_agent,
    get_inline_post_agent,
)
from src.agents.span_validator import filter_span_atomic
from src.agents.url_extractor import extract_job_urls
from src.client.api_client import (
    ApiClient,
    create_job_post_minimal,
    create_job_post_with_company_check,
    create_scrape,
    fetch_profile_readiness,
    get_scrapes,
)
from src.email_source import EmailMeta, EmailSource, make_source
from src.email_source.html_render import html_to_markdown
from src.email_source.mime import extract_bodies
from src.observability import (
    classify_exception,
    finish_run,
    record_email,
    start_run,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


CONFIDENCE_FLOOR = 0.6


@dataclass
class TriageOutcome:
    """What ``_triage_one`` reports back to the caller for observability.

    The previous shape (single string) was good enough for the in-memory
    counter, but Phase A's Mongo writer also wants the per-email tags-added
    diff so it lands in ``triage_emails``. Keeping both fields on one
    dataclass keeps the call site readable.

    ``introspection`` (AUTO-33) is the optional extraction-diagnostic
    sub-document built at stage E (see ``_build_introspection``); ``None``
    for emails that exit before extraction or when the build fails.
    """

    outcome: str
    tags_added: list[str] = field(default_factory=list)
    introspection: dict[str, Any] | None = None


def _api_client() -> ApiClient:
    return ApiClient(
        os.environ.get("CC_API_BASE_URL", "http://localhost:8000"),
        os.environ["CC_API_TOKEN"],
    )


async def _run_classify(agent, email_id: str) -> bool:
    """Return True iff the email is job-related."""
    result = await agent.run(f"Classify email id: {email_id}")
    text = (result.output or "").strip().lower()
    return text.startswith("job_post")


async def _run_inline_post(agent, email_id: str) -> InlinePostResult:
    result = await agent.run(f"Extract inline JobPost from email id: {email_id}")
    return result.output


def _load_email_text(email_id: str) -> str:
    """Rendered body text of a forward via ``notmuch show --format=raw``.

    Thunderbird forwards are frequently ``text/html``-only; the old
    ``--format=text`` path returned notmuch's ``Non-text part: text/html``
    placeholder (≈0 URLs) and every such forward dead-ended at
    ``new_no_urls``. Here we pull the RAW message, extract its plain + html
    bodies (recursing into forward-as-attachment nesting), and prefer plain
    text — falling back to html rendered to markdown so the URL extractor
    sees real links. ``--part=N`` was rejected: it needs a json round-trip
    to resolve the html part index and is brittle.
    """
    result = subprocess.run(
        ["notmuch", "show", "--format=raw", f"id:{email_id}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"notmuch show failed for {email_id}: {result.stderr.strip()}")
    plain, html = extract_bodies(result.stdout)
    return plain if plain.strip() else html_to_markdown(html)


# Matches http(s):// and mailto: URLs in the loaded body text. The negated
# class stops at whitespace and the HTML delimiters that leak in from
# ``href="..."`` attributes so a count isn't inflated by a trailing
# quote/bracket.
_BODY_URL_RE = re.compile(r"""(?:https?://|mailto:)[^\s"'<>)\]]+""")

# The literal placeholder ``notmuch show --format=text`` emits in place of a
# body when the only part is ``text/html`` (e.g. a Thunderbird forward). Its
# presence with zero URLs is the html-only signature that silently starves the
# URL extractor and produces ``new_no_urls``.
_NONTEXT_MARKER = "Non-text part:"


def _count_body_urls(text: str) -> int:
    """Count http(s)/mailto URLs in the loaded body text (regex, no network)."""
    return len(_BODY_URL_RE.findall(text))


def _build_introspection(body_text: str, extracted: Any) -> dict[str, Any] | None:
    """Extraction-diagnostic sub-document for the per-email Mongo record (AUTO-33).

    Bakes the exact signals that made ``new_no_urls`` invisible in Mongo into
    the ``triage_emails`` doc so the outcome self-explains from a query:

    * ``body_chars`` — length of the loaded body text (570 for an html-only
      forward vs ~36k for a multipart original — the tell at a glance).
    * ``body_url_count`` — raw http(s)/mailto URLs the body held.
    * ``body_nontext_only`` — the html-only signature: body carries the
      ``Non-text part:`` placeholder AND held zero URLs.
    * ``extract_kept`` — how many URLs the stage-E extractor kept.
    * ``extract_reasoning`` — the extractor's "N kept, M dropped" line.

    Pure observability — fully fail-safe. ANY error returns ``None`` so the
    email's real outcome and Mongo record are never endangered.
    """
    try:
        url_count = _count_body_urls(body_text)
        intro: dict[str, Any] = {
            "body_chars": len(body_text),
            "body_url_count": url_count,
            "body_nontext_only": _NONTEXT_MARKER in body_text and url_count == 0,
        }
        if extracted is not None:
            intro["extract_kept"] = len(extracted.job_urls)
            intro["extract_reasoning"] = extracted.reasoning
        return intro
    except Exception:
        logger.debug("introspection build failed for stage-E email (non-fatal)", exc_info=True)
        return None


def _auto_scrape_known_good_enabled() -> bool:
    """Opt-in gate for known-good free-tier auto-enrichment (default OFF).

    Mirrors ``process_tagged._auto_scrape_enabled`` but reads the
    ``CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD`` flag — the same env the deleted
    ``email_catchall`` poller used. AUTO-26 removed that file in the
    IMAP→notmuch consolidation; AUTO-29 re-ports the behavior into the live
    notmuch triage path. Off unless explicitly enabled.
    """
    return os.environ.get("CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _enrich_known_good(api: ApiClient, post_id, url: str) -> str | None:
    """Free-tier auto-enrichment for a JobPost on a known-good domain.

    Doug's Phase 3 ("morning descriptions, only when free"): if the post's
    host is known-good, its api-side extraction is the $0 deterministic
    Tier-0 CSS pass (never an LLM), so a hold scrape fills the description
    without spending tokens. ``auto_score=False`` guarantees scoring tokens
    are never spent either.

    Fully fail-safe: any error — including a readiness miss — returns a
    benign value and never propagates, so JobPost creation is unaffected.
    Dedupe-aware: skips when a scrape already exists for the post (ports the
    ``process_tagged._ensure_hold_scrape`` pattern, adding ``auto_score``).

    Returns ``"created"`` when a hold scrape was queued, ``"exists"`` when one
    was already present, ``"skip"`` when the host isn't known-good, or
    ``None`` on any error.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return None
        readiness = await fetch_profile_readiness(api, host)
        if readiness is None:
            return "skip"
        is_known_good, tier = readiness
        if not (is_known_good or str(tier) == "0"):
            return "skip"

        # Dedupe-aware: skip create if any scrape already exists for the post.
        try:
            existing_raw = await get_scrapes(api, job_post_id=post_id, per_page=1)
            existing = json.loads(existing_raw)
            if existing.get("success"):
                rows = (existing.get("data") or {}).get("data") or []
                if rows:
                    return "exists"
        except Exception as exc:
            logger.warning("  known-good scrape lookup failed for jp %s: %s", post_id, exc)

        raw = await create_scrape(
            api, url=url, job_post_id=post_id, status="hold", auto_score=False
        )
        resp = json.loads(raw)
        if resp.get("success"):
            return "created"
        logger.warning(
            "  known-good scrape create failed for jp %s: %s", post_id, resp.get("error")
        )
        return None
    except Exception as exc:
        logger.warning("  known-good enrichment raised for %s: %s", url, exc)
        return None


async def _create_posts_from_urls(
    api: ApiClient,
    urls,
    created_acc: list[dict] | None = None,
    auto_scrape_known_good: bool | None = None,
) -> dict:
    """Create a JobPost per extracted URL.

    ``auto_scrape_known_good`` gates free-tier auto-enrichment (AUTO-29):
    each successful post on a *known-good* domain also gets a dedupe-guarded
    ``hold`` scrape carrying ``job_post_id`` (so the runner AUGMENTS that
    post) with ``auto_score=False`` (see ``_enrich_known_good``) so
    ``/job-posts`` shows descriptions by morning without spending tokens.
    The forward path passes ``True``; ``None`` (the default for other callers
    and tests) falls back to the ``CADDY_FORWARD_AUTO_SCRAPE_KNOWN_GOOD`` env
    gate so that contract is untouched. The enrichment is fully fail-safe, so
    it can never break JobPost creation.

    Outcome per URL is read from the api response:
      201 + new resource          → fresh create
      200 + existing post resource → api dedupe hit (link or fingerprint).
                                     `merge_empty_fields_from_attrs` ran on
                                     the existing post; response carries the
                                     post we mapped onto, including the
                                     api-computed `canonical_link`.
      4xx / non-success            → failed.
    """
    created: list[str] = []
    duplicates: list[str] = []
    failed: list[str] = []
    scrapes_queued = 0
    enrich = (
        auto_scrape_known_good
        if auto_scrape_known_good is not None
        else _auto_scrape_known_good_enabled()
    )
    for link in urls:
        desc = link.description or None
        try:
            if link.company:
                raw = await create_job_post_with_company_check(
                    api,
                    title=link.title,
                    company_name=link.company,
                    link=link.url,
                    description=desc,
                    source="email",
                )
            else:
                raw = await create_job_post_minimal(
                    api,
                    title=link.title,
                    link=link.url,
                    description=desc,
                )
        except Exception as exc:
            logger.warning("  job-post raised for %s: %s", link.url, exc)
            failed.append(link.url)
            continue

        try:
            resp = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("  unparseable response for %s: %s", link.url, exc)
            failed.append(link.url)
            continue

        if not resp.get("success"):
            logger.warning("  job-post failed for %s: %s", link.url, resp.get("error"))
            failed.append(link.url)
            continue

        # JSON:API envelope: outer "data" wraps the inner resource at .data.data
        post_resource = (resp.get("data") or {}).get("data") or {}
        post_id = post_resource.get("id")
        attrs = post_resource.get("attributes") or {}
        canonical = attrs.get("canonical_link")
        status_code = resp.get("status_code")

        if status_code == 200:
            duplicates.append(link.url)
            logger.info(
                "  job-post dup: %s  id=%s  canonical=%s  (%s)",
                link.title,
                post_id,
                canonical,
                link.url,
            )
        else:
            # 201 (fresh create) — or any other 2xx the api evolves to use.
            created.append(link.url)
            logger.info(
                "  job-post: %s @ %s  id=%s  canonical=%s  (%s)",
                link.title,
                link.company or "—",
                post_id,
                canonical,
                link.url,
            )
            if created_acc is not None and post_id is not None:
                created_acc.append(
                    {
                        "id": post_id,
                        "title": link.title or "(untitled)",
                        "company": link.company or "—",
                        "link": canonical or link.url,
                        "source": "email_url",
                    }
                )

        # AUTO-29: free-tier auto-enrichment for known-good domains. Opt-in
        # (flag default OFF), fail-safe, dedupe-aware, never scores. No-op for
        # any post whose host isn't known-good. Runs for both fresh creates
        # and dedupe hits — the dedupe guard inside skips posts already
        # carrying a scrape.
        if enrich and post_id is not None:
            outcome = await _enrich_known_good(api, post_id, link.url)
            if outcome == "created":
                scrapes_queued += 1
                logger.info("  known-good: hold scrape queued for jp %s (%s)", post_id, link.url)
            elif outcome == "exists":
                logger.info("  known-good: scrape already present for jp %s", post_id)
    return {
        "created": created,
        "duplicates": duplicates,
        "failed": failed,
        "scrapes_queued": scrapes_queued,
    }


async def _create_inline_job_post(
    api: ApiClient,
    res: InlinePostResult,
    created_acc: list[dict] | None = None,
) -> str | None:
    """POST a JobPost from an inline-JD email. Returns "created", "duplicate",
    or None on failure. link is null; source is "email_direct"."""
    description = res.description
    if res.recruiter_contact:
        description = f"Source: direct email from {res.recruiter_contact}\n\n{description}"
    try:
        if res.company:
            raw = await create_job_post_with_company_check(
                api,
                title=res.title,
                company_name=res.company,
                description=description,
                location=res.location,
                salary_min=res.salary_min,
                salary_max=res.salary_max,
                remote_ok=res.remote_ok,
                source="email_direct",
            )
        else:
            raw = await create_job_post_minimal(
                api,
                title=res.title,
                description=description,
                source="email_direct",
            )
        resp = json.loads(raw)
    except Exception as exc:
        logger.warning("  inline job-post raised: %s", exc)
        return None

    if (resp.get("data") or {}).get("duplicate"):
        return "duplicate"
    if resp.get("status_code") in (200, 409):
        return "duplicate"
    if not resp.get("success"):
        logger.warning("  inline job-post failed: %s", resp.get("error"))
        return None
    if created_acc is not None:
        post_resource = (resp.get("data") or {}).get("data") or {}
        post_id = post_resource.get("id")
        if post_id is not None:
            created_acc.append(
                {
                    "id": post_id,
                    "title": res.title or "(untitled)",
                    "company": res.company or "—",
                    "link": None,
                    "source": "email_direct",
                }
            )
    return "created"


async def _triage_one(
    meta: EmailMeta,
    source: EmailSource,
    classify_agent,
    inline_post_agent,
    api: ApiClient,
    created_acc: list[dict] | None = None,
) -> TriageOutcome:
    """Drive one forwarded email through the light forward-path flow. Returns
    the outcome bucket + tags-added diff (+ extraction introspection) for the
    summary counter and the Mongo per-email record.

    Two paths only — see the module docstring. All tag reads/writes are
    MESSAGE-granular (``meta.id``), never thread (AUTO-32), and
    ``caddy_processed`` is written on EVERY terminal path so the
    ``NOT tag:caddy_processed`` selector never re-runs the LLMs on the
    backlog.

    Logs a per-email outcome line in `finally` so every email — even the ones
    that fall through to "already_done" — produces one line mapping
    email_id → outcome → tags-added.
    """
    email_id = meta.id
    initial_tags = set(meta.tags)
    tags: set[str] = set(initial_tags)
    final_outcome = "already_done"
    introspection: dict[str, Any] | None = None

    def _result() -> TriageOutcome:
        return TriageOutcome(
            outcome=final_outcome,
            tags_added=sorted(tags - initial_tags),
            introspection=introspection,
        )

    try:
        # Stage 1 — classify (only if not yet evaluated). `evaluated` is a
        # cheap resume checkpoint: an already-classified forward skips the LLM
        # call and resumes at extraction.
        if "evaluated" not in tags:
            is_job = await _run_classify(classify_agent, email_id)
            new_tags = ["evaluated"] + (["job_post"] if is_job else [])
            await source.add_tags(meta.id, new_tags)
            tags.update(new_tags)
            logger.info("[%s] %s  %s", "JOB" if is_job else "---", email_id, meta.subject)
            if not is_job:
                await source.add_tags(meta.id, ["caddy_processed"])
                tags.add("caddy_processed")
                final_outcome = "not_job"
                return _result()

        # Stage E — extract links first. Render the body (html-only forwards
        # included), pull URLs, and create a JobPost per link. Known-good links
        # also get a `hold` scrape carrying `job_post_id` so the runner enriches
        # THIS post rather than minting a second one (the augmentation contract).
        try:
            text = _load_email_text(email_id)
        except RuntimeError as exc:
            logger.warning("  load_email_text failed for %s: %s", email_id, exc)
            final_outcome = "load_failed"
            return _result()
        extracted = await extract_job_urls(text)
        # CC-111: deterministic cross-row guard. On multi-job digests
        # (ZipRecruiter /km/ trackers, LinkedIn /jobs/view/ ids) the LLM
        # extractor occasionally pairs one row's apply link with another
        # row's title/company. filter_span_atomic re-anchors each JobLink
        # against the body and drops any whose URL doesn't co-occur with its
        # title/company in the same row (mirrors process_tagged.py). Runs
        # BEFORE _build_introspection so the Mongo record reflects what was
        # kept, not what the LLM first emitted.
        before_span = len(extracted.job_urls)
        extracted.job_urls = filter_span_atomic(extracted.job_urls, text, email_id=email_id)
        if len(extracted.job_urls) != before_span:
            logger.info(
                "  span_validator dropped %d/%d url(s) (cross-row hallucination guard)",
                before_span - len(extracted.job_urls),
                before_span,
            )
        # AUTO-33: bake the body/URL/extract diagnostics into the per-email
        # record so the outcome (especially new_no_urls) explains itself from
        # Mongo. Fail-safe: a build error yields None and never touches the
        # real outcome.
        introspection = _build_introspection(text, extracted)
        if extracted.job_urls:
            url_outcome = await _create_posts_from_urls(
                api, extracted.job_urls, created_acc, auto_scrape_known_good=True
            )
            if not url_outcome["failed"]:
                await source.add_tags(meta.id, ["caddy_processed"])
                tags.add("caddy_processed")
            logger.info(
                "  extract: created=%d duplicates=%d failed=%d scrapes_queued=%d",
                len(url_outcome["created"]),
                len(url_outcome["duplicates"]),
                len(url_outcome["failed"]),
                url_outcome.get("scrapes_queued", 0),
            )
            if url_outcome["failed"]:
                final_outcome = "new_failed"
                return _result()
            if url_outcome["created"]:
                final_outcome = "new_created"
                return _result()
            final_outcome = "new_duplicate"
            return _result()

        # Stage I — inline fallback (zero links only). A manually-forwarded
        # recruiter email with the JD pasted inline still yields a link-less
        # JobPost (link=NULL, source="email_direct"). Whatever the result,
        # mark the forward processed so it stops re-matching.
        res = await _run_inline_post(inline_post_agent, email_id)
        if res.title and res.confidence >= CONFIDENCE_FLOOR:
            inline_outcome = await _create_inline_job_post(api, res, created_acc)
            await source.add_tags(meta.id, ["caddy_processed"])
            tags.add("caddy_processed")
            if inline_outcome is None:
                final_outcome = "inline_failed"
                return _result()
            logger.info(
                "  inline-post %s: %s @ %s  conf=%.2f",
                inline_outcome,
                res.title,
                res.company or "—",
                res.confidence,
            )
            final_outcome = f"inline_{inline_outcome}"
            return _result()

        # Inline too thin to stand as a post — still mark processed.
        logger.info(
            "  inline-post low confidence for %s (conf=%.2f, title=%r): %s",
            email_id,
            res.confidence,
            res.title,
            res.evidence[:120],
        )
        await source.add_tags(meta.id, ["caddy_processed"])
        tags.add("caddy_processed")
        final_outcome = "new_no_urls"
        return _result()
    finally:
        added = sorted(tags - initial_tags)
        diff = ",".join(added) if added else "—"
        logger.info(
            "  → %-18s %s  added=[%s]  %s",
            final_outcome,
            email_id,
            diff,
            (meta.subject or "(no subject)")[:70],
        )


async def run_once(limit: int, backend: str | None, days_back: int) -> None:
    source = make_source(backend)
    pending = await source.list_pending(limit=limit, days_back=days_back)
    if not pending:
        logger.info("No pending emails.")
        return

    classify_agent = get_classify_agent()
    inline_post_agent = get_inline_post_agent()
    api = _api_client()

    # Phase A1: open a Mongo run doc so every email this pass lands with
    # a foreign-key into one row in `triage_runs`. start_run returns None
    # on Mongo outage; downstream record_email/finish_run tolerate that.
    run_id = start_run(backend)

    counters: dict[str, int] = {}
    created_acc: list[dict] = []
    for meta in pending:
        outcome_bucket = "already_done"
        tags_added: list[str] = []
        exception_class: str | None = None
        network_failure = False
        introspection: dict[str, Any] | None = None
        try:
            triage = await _triage_one(
                meta,
                source,
                classify_agent,
                inline_post_agent,
                api,
                created_acc=created_acc,
            )
            outcome_bucket = triage.outcome
            tags_added = triage.tags_added
            introspection = triage.introspection
        except Exception as exc:
            logger.exception("Triage raised for %s: %s", meta.id, exc)
            outcome_bucket, network_failure = classify_exception(exc)
            exception_class = type(exc).__name__
        finally:
            record_email(
                run_id,
                meta.id,
                meta.subject,
                outcome_bucket,
                tags_added,
                exception_class=exception_class,
                network_failure=network_failure,
                introspection=introspection,
            )
        counters[outcome_bucket] = counters.get(outcome_bucket, 0) + 1

    finish_run(run_id, total_emails=len(pending), counters=counters)

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counters.items()))
    logger.info("Done: %s", summary)
    if created_acc:
        logger.info("Created %d JobPost(s) this pass:", len(created_acc))
        for post in created_acc:
            logger.info(
                "  jp #%s [%s]  %s @ %s  %s",
                post["id"],
                post["source"],
                post["title"][:60],
                post["company"],
                post["link"] or "(no link)",
            )


# State queries used by --status. Same date scope as list_pending so
# counts line up with what the daemon would see on a normal pass. The
# forward-only flow leaves just three live tags — `evaluated` (resume
# checkpoint), `job_post`, and `caddy_processed` (single terminal tag).
STATE_QUERIES: dict[str, str] = {
    "unevaluated": "not tag:evaluated",
    "evaluated_not_job": "tag:evaluated and not tag:job_post",
    "job_post_unprocessed": "tag:job_post and not tag:caddy_processed",
    "caddy_processed": "tag:caddy_processed",
}


async def print_status(
    backend: str | None, days_back: int, show: str | None, show_limit: int
) -> None:
    """Tag-state breakdown of the mailbox so the user can see where
    pending work is stuck without watching live logs. `--show <state>`
    dumps the matching email subjects/ids."""
    source = make_source(backend)
    if not hasattr(source, "count_by_query"):
        raise RuntimeError(
            f"--status not supported for backend {type(source).__name__}; "
            "only NotmuchSource implements count_by_query so far."
        )

    if show is not None:
        if show not in STATE_QUERIES:
            valid = ", ".join(STATE_QUERIES.keys())
            raise SystemExit(f"--show: unknown state {show!r}. Valid: {valid}")
        metas = await source.list_by_query(
            STATE_QUERIES[show], limit=show_limit, days_back=days_back
        )
        logger.info("=== %s (showing %d, last %d days) ===", show, len(metas), days_back)
        for m in metas:
            tag_str = ",".join(sorted(m.tags)) or "(none)"
            logger.info("  %s  [%s]  %s", m.id, tag_str, (m.subject or "")[:80])
        return

    logger.info("=== Pipeline state (last %d days) ===", days_back)
    width = max(len(k) for k in STATE_QUERIES)
    for state, query in STATE_QUERIES.items():
        n = await source.count_by_query(query, days_back=days_back)
        logger.info("  %-*s : %4d", width, state, n)
    logger.info("(use --show <state> to list matching emails)")


class _SignalExit(Exception):
    """Raised to unwind the poll loop on SIGTERM/SIGINT so the stop is
    logged + flushed (ERROR), never silent.

    AUTO #17: the triage daemon went silent for ~24h looking exactly like
    an un-trapped signal / host-sleep — a clean stop with no error and no
    heartbeat. Converting the signal into an exception lets us record and
    flush before the process dies.
    """

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"signal {signum}")


_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _heartbeat(backend: str | None) -> None:
    """Emit a logfire-visible heartbeat each loop cycle.

    Without this, a daemon that is up but seeing zero pending mail emits
    no logfire records per cycle — indistinguishable from a dead one. The
    heartbeat makes "alive but idle" visible so a silence points upstream
    (mail sync / notmuch) rather than at cc_auto. Best-effort: a logfire
    outage must never break the loop.
    """
    try:
        if logfire is not None:
            logfire.info(
                "caddy-inbox heartbeat",
                backend=backend or os.environ.get("CADDY_EMAIL_BACKEND", "notmuch"),
            )
    except Exception:
        logger.debug("heartbeat logfire.info failed (non-fatal)", exc_info=True)


def _force_flush() -> None:
    """Flush buffered logfire records before exit so a stop/crash isn't
    lost in the export buffer. Best-effort."""
    try:
        if logfire is not None:
            logfire.force_flush()
    except Exception:
        logger.debug("logfire.force_flush failed (non-fatal)", exc_info=True)


def _handle_stop_signal(signum: int, _frame: object = None) -> None:
    """Signal handler: log ERROR + flush logfire, then raise ``_SignalExit``
    to unwind the loop.

    The loud+flush work lives here (not only in ``_run_loop``) so a real
    signal is recorded even if the raised exception unwinds outside the
    loop's frame under asyncio.
    """
    try:
        name = signal.Signals(signum).name
    except Exception:
        name = str(signum)
    logger.error("caddy-inbox received %s — flushing logfire and shutting down.", name)
    _force_flush()
    raise _SignalExit(signum)


def _install_signal_handlers() -> None:
    """Trap SIGTERM/SIGINT so a daemon stop is loud + flushed, not silent.

    Best-effort: ``signal.signal`` raises if not on the main thread (e.g.
    under pytest, or when embedded), so we swallow that — the loop still
    runs, it just won't intercept signals in that context.
    """
    for sig in _STOP_SIGNALS:
        try:
            signal.signal(sig, _handle_stop_signal)
        except (ValueError, OSError):
            pass


async def _run_loop(limit: int, backend: str | None, days_back: int, interval: int) -> None:
    """Continuous triage poll loop with heartbeat + loud-on-exit.

    Closes the AUTO #17 observability gap:

    * ``_heartbeat`` emits a logfire record every cycle (alive-vs-idle).
    * an unexpected unwind logs CRITICAL, a signal-driven stop logs ERROR
      (in ``_handle_stop_signal``), and both ``force_flush`` logfire in
      ``finally`` — a stop can no longer be silent.

    Per-cycle ``run_once`` failures stay swallowed-and-continued (one bad
    pass must not kill the daemon, matching run_once's per-email isolation);
    only an escape from the loop itself is loud.
    """
    _install_signal_handlers()
    logger.info("Loop mode: every %d min.", interval)
    try:
        while True:
            _heartbeat(backend)
            try:
                await run_once(limit, backend, days_back)
            except Exception:
                logger.exception("run_once crashed — continuing.")
            await asyncio.sleep(interval * 60)
    except _SignalExit:
        # _handle_stop_signal already logged ERROR + flushed; just unwind.
        raise
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.error("caddy-inbox loop interrupted — shutting down.")
        raise
    except BaseException:
        logger.critical(
            "caddy-inbox loop exited unexpectedly — the poll loop should run "
            "forever; treat this as a crash, not a clean stop.",
            exc_info=True,
        )
        raise
    finally:
        _force_flush()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the forward-path email triage pipeline (caddy-inbox)."
    )
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass and exit (default when --loop is absent).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=15,
        help="Minutes between runs when --loop is set (default: 15).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max emails processed per pass (default: 20).",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=14,
        help="notmuch date window (default: 14).",
    )
    parser.add_argument(
        "--backend",
        choices=["notmuch", "imap"],
        default=None,
        help="Override CADDY_EMAIL_BACKEND for this run.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help=(
            "Print a tag-state breakdown of the mailbox and exit. Use to "
            "find emails stuck mid-pipeline (e.g., 'evaluated_not_job' = "
            "candidates the classifier rejected; 'refined_follow_up' = "
            "follow-ups not yet matched to an application)."
        ),
    )
    parser.add_argument(
        "--show",
        type=str,
        default=None,
        metavar="STATE",
        help=(
            "With --status: list the matching email ids/subjects for the "
            "named state (one of: " + ", ".join(STATE_QUERIES.keys()) + ")."
        ),
    )
    parser.add_argument(
        "--show-limit",
        type=int,
        default=20,
        help="Max emails listed by --show (default: 20).",
    )
    args = parser.parse_args()

    if args.status or args.show is not None:
        await print_status(args.backend, args.days_back, args.show, args.show_limit)
        return

    if args.loop:
        await _run_loop(args.limit, args.backend, args.days_back, args.interval)
    else:
        await run_once(args.limit, args.backend, args.days_back)


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("caddy-inbox interrupted — exiting.")
    except _SignalExit as exc:
        logger.info("caddy-inbox stopped on signal %s — exiting.", exc.signum)


if __name__ == "__main__":
    run()
