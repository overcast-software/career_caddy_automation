"""caddy-inbox reports its AI usage — it never did.

`AiUsage` (api `job_hunting/models/ai_usage.py`), the server-side
`estimate_cost` pricing lookup, the `/api/v1/ai-usages/summary` endpoint and
the `/settings/ai-spend` page have existed the whole time. The daemon simply
never fed them:

* ``_run_classify`` and ``_run_inline_post`` ran an agent and dropped
  ``result.usage()`` on the floor.
* ``extract_job_urls`` was called with no ``api_token``, and its own reporting
  (``url_extractor.py:405``) is gated on exactly that.

So while CC-125 re-classified ten stuck emails with ``gpt-4o-mini`` 96 times a
day for seven weeks, the page built to show LLM spend showed a flat line. The
older ``tag_emails.py:144`` pipeline reported correctly, which is what made the
gap invisible — the surface worked, just not for the daemon actually running.

Pinned here:

* each of the three call sites reports, with the agent name and the
  ``inbox_triage`` trigger that lets the summary separate this daemon from
  ``tag_emails`` (which reports the SAME ``email_classifier`` agent name)
* metering is fail-safe in every direction — no token, a broken ``usage``, or
  a raising reporter must all leave triage's own result untouched
* the stand-in below matches the REAL ``AgentRunResult`` shape, checked against
  the installed library rather than asserted from memory (2026-08-25)

No pytest-asyncio in the dev group, so coroutines are driven with
``asyncio.run`` like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import scripts.inbox_triage as it

TOKEN = "jh_testtoken"
EMAIL_ID = "fwd@dougheadley.com"


class _Result:
    """pydantic-ai AgentRunResult stand-in: ``.output`` plus ``.usage``.

    ``usage`` is a PROPERTY, matching ``AgentRunResult`` in pydantic-ai 2.x.

    It was a method here, and that is the whole reason the 2.0.0 upgrade shipped
    broken. ``pyproject.toml`` floors pydantic-ai at ``>=0.0.14``; the lock
    resolved 2.0.0; ``usage`` became a property; and every production call site
    still said ``result.usage()``. This stub agreed with the old contract, so
    the suite stayed green while ``caddy-inbox`` raised
    ``'RunUsage' object is not callable`` on all 20 emails of the 2026-08-23
    sweep and had to be killed by hand.

    A hand-rolled stand-in for a third-party object is a COPY OF ITS CONTRACT,
    and a copy drifts in silence. ``test_stub_matches_the_real_agentrunresult``
    below is the guard: it asserts this shape against the installed library, so
    the next upgrade that moves ``usage`` fails here instead of in Doug's inbox.
    """

    def __init__(self, output, usage=None, usage_raises: bool = False):
        self.output = output
        self._usage = usage or SimpleNamespace(
            request_tokens=120, response_tokens=8, total_tokens=128, requests=1
        )
        self._usage_raises = usage_raises

    @property
    def usage(self):
        if self._usage_raises:
            raise RuntimeError("usage unavailable")
        return self._usage


class _Agent:
    def __init__(self, result):
        self._result = result

    async def run(self, *args, **kwargs):
        return self._result


@pytest.fixture
def reporter(monkeypatch):
    """Capture report_usage kwargs; token present by default."""
    monkeypatch.setenv("CC_API_TOKEN", TOKEN)
    spy = AsyncMock()
    monkeypatch.setattr(it, "report_usage", spy)
    monkeypatch.setattr(it, "get_model", lambda role: f"openai:model-for-{role}")
    return spy


# ---------------------------------------------------------------------------
# The stub is only worth anything if it still resembles the real thing
# ---------------------------------------------------------------------------


def test_stub_matches_the_real_agentrunresult():
    """``usage`` must be a property on BOTH the stub and the installed library.

    This is the test that was missing on 2026-08-23. Everything else in this
    file exercises `_Result`, so all of it passed while production raised
    ``'RunUsage' object is not callable`` on every email — a stub cannot fail
    for a reason that lives in someone else's package.

    ``getattr_static`` deliberately, not ``getattr``: reading ``.usage`` off the
    class normally would return the property OBJECT anyway, but on an instance
    it would EXECUTE it. Asking the class dict keeps this a question about
    shape rather than behaviour.

    If a future pydantic-ai turns ``usage`` back into a method, or renames it,
    this fails with a clear reason — and every ``result.usage`` call site in
    src/ and scripts/ needs the same edit in reverse.
    """
    from pydantic_ai.run import AgentRunResult

    real = inspect.getattr_static(AgentRunResult, "usage")
    assert isinstance(real, property), (
        f"pydantic-ai changed AgentRunResult.usage to {type(real).__name__}; "
        "update every `result.usage` call site in src/ and scripts/, and this stub"
    )
    assert isinstance(inspect.getattr_static(_Result, "usage"), property)


# ---------------------------------------------------------------------------
# The two agent call sites report
# ---------------------------------------------------------------------------


def test_classify_reports_usage(reporter):
    agent = _Agent(_Result("job_post"))
    assert asyncio.run(it._run_classify(agent, EMAIL_ID)) is True
    reporter.assert_awaited_once()
    kw = reporter.await_args.kwargs
    assert kw["agent_name"] == "email_classifier"
    assert kw["model_name"] == "openai:model-for-email_classifier"
    # The dimension that separates this daemon from the tag_emails pipeline,
    # which reports the same agent_name.
    assert kw["trigger"] == "inbox_triage"
    assert kw["api_token"] == TOKEN
    assert kw["usage"].total_tokens == 128


def test_inline_post_reports_usage(reporter):
    payload = SimpleNamespace(title="Staff Engineer", confidence=0.9)
    agent = _Agent(_Result(payload))
    assert asyncio.run(it._run_inline_post(agent, EMAIL_ID)) is payload
    reporter.assert_awaited_once()
    kw = reporter.await_args.kwargs
    assert kw["agent_name"] == "inline_post_extractor"
    assert kw["trigger"] == "inbox_triage"


def test_classify_still_returns_its_answer(reporter):
    # Reporting is additive: the classify verdict is unchanged by metering.
    assert asyncio.run(it._run_classify(_Agent(_Result("not_job")), EMAIL_ID)) is False
    assert asyncio.run(it._run_classify(_Agent(_Result("job_post — a role")), EMAIL_ID)) is True


# ---------------------------------------------------------------------------
# Fail-safe: metering never fails triage
# ---------------------------------------------------------------------------


def test_no_token_skips_reporting_silently(monkeypatch):
    monkeypatch.delenv("CC_API_TOKEN", raising=False)
    spy = AsyncMock()
    monkeypatch.setattr(it, "report_usage", spy)
    assert asyncio.run(it._run_classify(_Agent(_Result("job_post")), EMAIL_ID)) is True
    spy.assert_not_awaited()


def test_broken_usage_call_does_not_break_triage(reporter):
    agent = _Agent(_Result("job_post", usage_raises=True))
    # The verdict survives; nothing is reported.
    assert asyncio.run(it._run_classify(agent, EMAIL_ID)) is True
    reporter.assert_not_awaited()


def test_raising_reporter_does_not_break_triage(monkeypatch):
    """The guarantee must not depend on report_usage keeping its promise.

    `report_usage` documents that it swallows its own errors. If that ever
    changes, a metering failure must still not take the pipeline down — a
    pipeline that dies because the accountant fell over is worse than the bug
    this reporting exists to expose.
    """
    monkeypatch.setenv("CC_API_TOKEN", TOKEN)
    monkeypatch.setattr(it, "get_model", lambda role: "openai:gpt-4o-mini")
    monkeypatch.setattr(it, "report_usage", AsyncMock(side_effect=RuntimeError("api down")))
    assert asyncio.run(it._run_classify(_Agent(_Result("job_post")), EMAIL_ID)) is True


# ---------------------------------------------------------------------------
# The third hole: extract_job_urls was never handed the token it gates on
# ---------------------------------------------------------------------------


def test_extract_job_urls_receives_the_api_token(monkeypatch):
    """url_extractor.py:405 gates its own reporting on a non-empty api_token.

    Calling `extract_job_urls(text)` positionally left it empty, so the most
    expensive call in the sweep reported nothing. Pinned so the kwarg is not
    dropped again.
    """
    monkeypatch.setenv("CC_API_TOKEN", TOKEN)
    monkeypatch.setattr(it, "_load_email_text", lambda _id: "body with a job link")
    spy = AsyncMock(return_value=SimpleNamespace(job_urls=[], reasoning="0 kept"))
    monkeypatch.setattr(it, "extract_job_urls", spy)

    class _Src:
        def __init__(self):
            self.tags: set[str] = set()

        async def add_tags(self, message_id: str, tags: list[str]) -> None:
            self.tags.update(tags)

    from src.email_source import EmailMeta

    meta = EmailMeta(
        id=EMAIL_ID, subject="Fwd: role", tags={"inbox"}, thread_id="T1", recipient="dough"
    )
    api = AsyncMock()
    api.get = AsyncMock(
        return_value='{"success": true, "data": {"data": [{"id": "7", "type": "user"}]}}'
    )
    asyncio.run(
        it._triage_one(
            meta,
            _Src(),
            _Agent(_Result("job_post")),
            _Agent(_Result(SimpleNamespace(title="", confidence=0.0, evidence="thin"))),
            api,
        )
    )
    spy.assert_awaited_once()
    assert spy.await_args.kwargs.get("api_token") == TOKEN
