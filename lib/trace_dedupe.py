"""Per-write JobPost outcome forensics."""

# REMOVE WHEN STABLE — observability scaffolding for inbox debugging.
#
# Wraps the `create_job_post_*` calls cc_auto makes from the email pipeline.
# Each invocation produces a structured logfire event recording:
#
#   url_sent          — raw URL we POSTed (preserved for debugging duplication)
#   url_canonical     — canonical_link the api computed (now surfaced via
#                       JobPostSerializer; lets us spot canonicalization
#                       surprises without grepping the api log)
#   outcome           — created (201) | merged_into_existing (200) |
#                       duplicate_conflict (409 + code=duplicate_job_post) |
#                       failed (other 4xx/5xx) | invalid_response (parse fail)
#   existing_post_id  — id we mapped onto when outcome=merged_into_existing,
#                       or the row we collided with when
#                       outcome=duplicate_conflict (the api returns it in the
#                       409's `errors[0].meta.job_post_id`)
#   merge_diff        — for 200 responses, which of the requested fields
#                       were filled vs. ignored by `merge_empty_fields_from_attrs`
#                       on the api side. This is the killer feature: catches
#                       the "I sent posting_status=closed but it didn't merge"
#                       case at write time, without grepping logs.
#   fingerprint_null_risk — when incoming has company but the existing post's
#                           company_id is still None on the merged response,
#                           flag the precondition for the Microsoft regression
#                           (#22). Observation only — the api-side merge fix
#                           lives on the followups list.
#
# Activation:  `CADDY_DEDUPE_TRACE=1` (off by default; calls are zero-cost
# when the env flag is unset). When active, every write emits a logfire span
# `dedupe.write` with the fields above and prints a one-line summary to the
# logger so the watcher of `caddy-inbox` doesn't have to alt-tab to logfire.

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable

import logfire

from src.client.api_client import error_codes, has_error_code

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return os.environ.get("CADDY_DEDUPE_TRACE", "").strip().lower() in {"1", "true", "yes", "on"}


def _post_resource(parsed: dict) -> dict:
    """JSON:API envelope — outer 'data' wraps the inner resource at .data.data."""
    return (parsed.get("data") or {}).get("data") or {}


def _duplicate_post_id(parsed: dict) -> str | None:
    """The existing post's id out of a ``duplicate_job_post`` 409.

    The api puts it in the error object's ``meta``. This is only readable
    because the transport preserves structured errors (AUTO-52) — under the
    old string collapse the id was destroyed with the rest of the document,
    which is why a duplicate conflict used to trace as an anonymous failure.
    """
    for err in parsed.get("errors") or []:
        if not isinstance(err, dict) or err.get("code") != "duplicate_job_post":
            continue
        post_id = (err.get("meta") or {}).get("job_post_id")
        if post_id is not None:
            return str(post_id)
    return None


def _classify(parsed: dict) -> str:
    if not parsed.get("success"):
        # A duplicate_job_post 409 is the api declining to merge a canonical
        # collision — an expected outcome, not a failure. Tracing it as
        # `failed` is how an all-duplicate digest looked like a broken
        # pipeline for seven weeks (CC-125).
        if parsed.get("status_code") == 409 and has_error_code(parsed, "duplicate_job_post"):
            return "duplicate_conflict"
        return "failed"
    code = parsed.get("status_code")
    if code == 200:
        return "merged_into_existing"
    if code in (201, 202):
        return "created"
    return "ok_other"  # 2xx not yet seen — log loud, don't crash


def _merge_diff(payload: dict, returned_attrs: dict) -> dict:
    """Per requested field, did the api persist the incoming value?

    Sticky-once-closed in particular: posting_status="closed" should always
    win on an existing post. Other fields only fill NULL slots.
    """
    diff: dict[str, dict] = {}
    for k, sent in payload.items():
        if k in {"company_id", "duplicate_of_id"}:
            # ids aren't sent on JobPost create from cc_auto; ignore
            continue
        got = returned_attrs.get(k)
        if got == sent:
            diff[k] = {"sent": sent, "got": got, "kept": True}
        else:
            diff[k] = {"sent": sent, "got": got, "kept": False}
    return diff


async def trace_write(
    *,
    url: str,
    payload: dict,
    company_name: str | None,
    do_post: Callable[[], Awaitable[str]],
) -> str:
    """Wrap a single create_job_post_* call site.

    Caller passes a zero-arg callable that performs the actual HTTP POST
    (we don't take the api method directly because cc_auto has two separate
    creators with different signatures). We invoke it, parse the response,
    classify the outcome, and emit one logfire span.

    The raw response string is returned unchanged — this is observation
    only, not a behaviour change.
    """
    if not is_enabled():
        return await do_post()

    with logfire.span(
        "dedupe.write",
        url_sent=url,
        company_name_sent=company_name or "",
    ) as span:
        raw = await do_post()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            span.set_attribute("outcome", "invalid_response")
            span.set_attribute("response_head", raw[:200])
            logger.warning("dedupe.write %s: invalid response", url)
            return raw

        outcome = _classify(parsed)
        resource = _post_resource(parsed)
        attrs = resource.get("attributes") or {}
        post_id = resource.get("id")
        canonical = attrs.get("canonical_link")
        relationships = resource.get("relationships") or {}
        existing_company_rel = (
            relationships.get("company", {}).get("data")
            if isinstance(relationships, dict)
            else None
        )
        existing_company_id = (
            existing_company_rel.get("id") if isinstance(existing_company_rel, dict) else None
        )

        span.set_attribute("outcome", outcome)
        span.set_attribute("post_id", post_id or "")
        span.set_attribute("url_canonical", canonical or "")
        span.set_attribute("status_code", parsed.get("status_code") or 0)

        if outcome == "merged_into_existing":
            diff = _merge_diff(payload, attrs)
            span.set_attribute("merge_diff", diff)
            ignored = [k for k, v in diff.items() if not v["kept"]]
            if ignored:
                logger.info(
                    "dedupe.write %s: merged into post %s; api ignored %s "
                    "(merge_empty_fields_from_attrs only fills NULL slots, "
                    "except sticky-once-closed for posting_status)",
                    url,
                    post_id,
                    ignored,
                )
            else:
                logger.info(
                    "dedupe.write %s: merged into post %s; all sent fields kept",
                    url,
                    post_id,
                )

            # Microsoft-regression precondition: incoming has company,
            # existing post still has no company_id even after merge. Means
            # the api's merge path didn't reach company_id (likely because
            # merge_empty_fields_from_attrs takes attrs only — company_id
            # lives in relationships).
            if company_name and existing_company_id is None:
                span.set_attribute("fingerprint_null_risk", True)
                logger.warning(
                    "dedupe.write %s: stage5.fingerprint_null_risk — incoming "
                    "company=%r but existing post %s still has company_id=NULL "
                    "after merge. See followups todo: api/ JobPost dedupe — "
                    "merge company_id into NULL-fingerprint stubs.",
                    url,
                    company_name,
                    post_id,
                )
        elif outcome == "created":
            logger.info(
                "dedupe.write %s: created post %s (canonical=%s)",
                url,
                post_id,
                canonical,
            )
        elif outcome == "duplicate_conflict":
            existing_id = _duplicate_post_id(parsed)
            span.set_attribute("existing_post_id", existing_id or "")
            logger.info(
                "dedupe.write %s: duplicate — api declined to merge into post %s "
                "(canonical collision against a complete, higher-trust row)",
                url,
                existing_id or "?",
            )
        elif outcome == "failed":
            span.set_attribute("error", parsed.get("error") or "")
            span.set_attribute("error_codes", error_codes(parsed))
            logger.warning(
                "dedupe.write %s: failed status=%s codes=%s err=%s",
                url,
                parsed.get("status_code"),
                error_codes(parsed) or "—",
                parsed.get("error"),
            )

        return raw
