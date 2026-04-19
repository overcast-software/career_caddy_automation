"""Classify and tag emails as job postings using an LLM agent.

Finds unevaluated emails via notmuch, classifies each with the email_classifier
agent, and tags job postings with 'job_post' + 'evaluated'.

Usage:
    uv run caddy-classify                  # single run
    uv run caddy-classify --loop           # loop every 60 minutes
    uv run caddy-classify --loop --interval 30
"""

from lib.observability import configure_logfire
configure_logfire("caddy-classify")

import json
import os
import re
import subprocess
import asyncio
import argparse
import time
import logging
from datetime import datetime, timedelta
from email.header import decode_header
from src.agents.usage_reporter import report_usage
from src.agents.agent_factory import (
    get_model,
    get_model_name,
    get_agent,
    register_defaults,
    resolve_model,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an email classifier. You will be given a single email ID.

Your job:
1. Read the email using read_email(email_id, classify=True, max_content_length=1500).
   classify=True strips tracking URLs / marketing boilerplate — use it every time.
2. Determine if it contains a job posting (job listing, recruiter outreach, job application link, etc.)

Do NOT call tag_email — the caller handles tagging based on your reply.

Reply with exactly one line and nothing else:
  job_post <subject>          (if the email contains a job posting)
  not_job_post <subject>      (if it does not)"""

register_defaults()
_classifier_model = None  # populated in main() after parsing --model
email_agent = None        # ditto


def _build_agent(model_spec: str | None):
    """Create the classifier agent, optionally overriding the model via CLI."""
    global _classifier_model, email_agent
    if model_spec:
        _classifier_model = resolve_model(model_spec)
    else:
        _classifier_model = get_model("email_classifier")
    email_agent = get_agent(
        "email_classifier",
        system_prompt=SYSTEM_PROMPT,
        model=_classifier_model,
    )


# Subjects that reliably identify NON-job emails. Conservative on purpose:
# validated against 512 tagged emails with 0 false negatives. Only short-circuits
# the unambiguous cases; anything else falls through to the LLM classifier.
_NOT_JOB_SUBJECT = re.compile(
    r"(?i)("
    r"\[[a-z0-9/_.-]+\] (run failed|run succeeded|pull request|merged|closed|opened|commit|push)"
    r"|check-suites|workflow run|deployment"
    r"|informed delivery|usps"
    r"|amazon\.com order|order (confirmation|shipped|delivered)|your order|shipped|tracking number"
    r"|welcome to|subscribe|newsletter|digest(?! .*(job|role|position|hire|engineer))"
    r"|reset (your )?password|verify (your )?email|confirm (your )?email|sign in"
    r"|donate|petition|ballot|voter|town hall"
    r"|unsubscribe"
    r"|receipt|invoice|payment (received|processed|failed)"
    r"|happy birthday|anniversar"
    r"|password expir|two-factor|security alert|login attempt"
    r"|members? signup|milestone achieved"
    r"|giving day|fundrais"
    r"|today only|today's (deals|offers)|% off|\bsale\b"
    r"|ski pass|lift ticket|season pass"
    r")"
)


def _decode_subject(s: str) -> str:
    try:
        parts = decode_header(s)
        return "".join(
            p.decode(c or "utf-8", "ignore") if isinstance(p, bytes) else p
            for p, c in parts
        )
    except Exception:
        return s


def fetch_unevaluated_emails(limit: int = 20, days_back: int = 7) -> list[dict]:
    """Fetch unevaluated emails (id + decoded subject) directly from notmuch."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    date_range = f"date:{start_date.strftime('%Y-%m-%d')}..{end_date.strftime('%Y-%m-%d')}"
    query = f"NOT tag:evaluated AND {date_range}"

    result = subprocess.run(
        ["notmuch", "search", "--format=json", f"--limit={limit}", query],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"notmuch failed: {result.stderr}")

    threads = json.loads(result.stdout)
    emails = []
    for thread in threads:
        query_arr = thread.get("query", [])
        if query_arr and query_arr[0]:
            email_id = query_arr[0]
            if email_id.startswith("id:"):
                email_id = email_id[3:]
            emails.append({
                "email_id": email_id,
                "subject": _decode_subject(thread.get("subject", "")),
            })
    return emails


async def classify_email(email_id: str) -> str:
    """Run a fresh isolated agent call for a single email."""
    result = await email_agent.run(f"Classify email id: {email_id}")

    api_token = os.environ.get("CC_API_TOKEN", "")
    if api_token:
        await report_usage(
            api_token=api_token,
            agent_name="email_classifier",
            model_name=get_model_name(_classifier_model),
            usage=result.usage(),
            trigger="classify",
        )

    return result.output


def apply_tags(email_id: str, tags: list[str]) -> None:
    """Run `notmuch tag` directly — we don't trust the agent to do it."""
    tag_args = [f"+{t}" for t in tags]
    subprocess.run(
        ["notmuch", "tag", *tag_args, "--", f"id:{email_id}"],
        check=True, timeout=10,
    )


async def run_once():
    """Classify one batch of unevaluated emails."""
    emails = fetch_unevaluated_emails(limit=20)
    if not emails:
        print("No unevaluated emails found.")
        return

    print(f"Found {len(emails)} unevaluated emails. Classifying...\n")

    tagged = 0
    untagged = 0
    skipped = 0

    for em in emails:
        email_id = em["email_id"]
        subject = em["subject"]

        if _NOT_JOB_SUBJECT.search(subject):
            try:
                apply_tags(email_id, ["evaluated"])
            except Exception as exc:
                logger.warning("tag failed for %s: %s", email_id, exc)
            skipped += 1
            untagged += 1
            print(f"[SKIP] not_job_post {subject}")
            continue

        output = await classify_email(email_id)
        is_job = output.strip().lower().startswith("job_post")
        tags = ["evaluated"] + (["job_post"] if is_job else [])
        try:
            apply_tags(email_id, tags)
        except Exception as exc:
            logger.warning("tag failed for %s: %s", email_id, exc)
        if is_job:
            tagged += 1
        else:
            untagged += 1
        print(f"{'[JOB]' if is_job else '[---]'} {output.strip()}")

    print(f"\nSummary: {tagged} job posts, {untagged} not job posts "
          f"({skipped} pre-filtered, {len(emails) - skipped} via LLM)")


async def main():
    parser = argparse.ArgumentParser(
        description="Classify and tag emails as job postings"
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Run continuously on an interval"
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Minutes between runs when --loop is set (default: 60)"
    )
    parser.add_argument(
        "--model", type=str, default=None,
        help=(
            "Override the classifier model for this run. Accepts "
            "provider-qualified specs like 'openai:gpt-4o-mini', "
            "'anthropic:claude-haiku-4-5-20251001', or 'ollama:qwen3:4b-instruct'. "
            "Default resolves from EMAIL_CLASSIFIER_MODEL / CADDY_DEFAULT_MODEL env vars."
        ),
    )
    args = parser.parse_args()

    _build_agent(args.model)
    logger.info("classifier model: %s", get_model_name(_classifier_model))

    if args.loop:
        print(f"=== Loop mode: running every {args.interval} minutes ===")
        while True:
            try:
                await run_once()
            except Exception as e:
                logger.error(f"Classification run failed: {e}")
            next_run = time.strftime("%H:%M", time.localtime(time.time() + args.interval * 60))
            print(f"\nSleeping {args.interval} minutes (next run ~{next_run})...")
            await asyncio.sleep(args.interval * 60)
    else:
        await run_once()


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
