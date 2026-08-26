"""AUTO-52 — the transport preserves the api's structured error document.

``ApiClient._ok`` used to reduce every non-2xx response to
``error="<status> - <first 500 chars of body>"``. That string was the ONLY
record of the failure, so a caller wanting to know *why* a call failed had no
choice but to substring-match prose.

That is the structural cause of CC-125. The api reports "this link is already a
JobPost" as ``409`` + ``code=duplicate_job_post`` — an expected outcome with a
machine-readable marker — and the pipeline could not tell it apart from a
genuine failure, because the marker had been flattened into text before the
pipeline ever saw the response. An all-duplicate digest therefore never reached
a terminal state and re-ran the LLMs every sweep for seven weeks.

``_ok`` now also populates ``APIResponse.errors`` with the api's error objects,
so ``code`` and ``meta`` are readable as data. These tests drive the REAL
``_ok`` over real ``httpx.Response`` objects rather than hand-written
envelopes — a fixture that approximates the transport cannot catch a transport
that stops matching it.

The hard requirement running through all of it: nothing on the error path may
raise. A body can be a JSON:API document, a bare DRF ``detail``, an HTML proxy
page that never reached Django, or nonsense. Inspecting a failure must not
itself become a new way to fail.
"""

from __future__ import annotations

import json

import httpx

from src.client.api_client import ApiClient, error_codes, has_error_code

# Real NanoID-shaped id — a numeric string would mask an int()-cast regression.
EXISTING_POST_ID = "V30p4hHABQ"

DUPLICATE_CODE = "duplicate_job_post"


def _envelope(status_code: int, body: dict | list | str) -> dict:
    """Run the real ``_ok`` over a real response and parse what it returned."""
    if isinstance(body, str):
        response = httpx.Response(status_code, text=body)
    else:
        response = httpx.Response(status_code, json=body)
    return json.loads(ApiClient("https://example.test", "jh_test")._ok(response))


def _duplicate_body(detail: str = "A job post with this link already exists.") -> dict:
    """The api's real duplicate response — job_hunting/api/views/jobs.py:901-920."""
    return {
        "errors": [
            {
                "status": "409",
                "code": DUPLICATE_CODE,
                "detail": detail,
                "meta": {
                    "job_post_id": EXISTING_POST_ID,
                    "title": "Staff Engineer",
                    "company_name": "Acme",
                    "link": "https://acme.com/jobs/staff-engineer",
                },
            }
        ]
    }


class TestDuplicateConflictIsReadableAsData:
    def test_code_survives_as_a_field(self):
        parsed = _envelope(409, _duplicate_body())
        assert parsed["success"] is False
        assert parsed["status_code"] == 409
        assert error_codes(parsed) == [DUPLICATE_CODE]
        assert has_error_code(parsed, DUPLICATE_CODE) is True

    def test_prose_is_still_there_for_humans_and_llms(self):
        # The string was not removed — it stopped being the only record.
        parsed = _envelope(409, _duplicate_body())
        assert parsed["error"].startswith("409 - ")
        assert DUPLICATE_CODE in parsed["error"]

    def test_meta_carries_the_existing_post_id(self):
        # The 409 identifies the row it collided with. Under the old collapse
        # this was destroyed along with the rest of the document, which is why
        # the pipeline believed the api "withheld the post id" on this path.
        parsed = _envelope(409, _duplicate_body())
        assert parsed["errors"][0]["meta"]["job_post_id"] == EXISTING_POST_ID

    def test_meta_survives_truncation_that_eats_the_string(self):
        """THE point of the change, in one assertion.

        ``error`` is capped at 500 chars. The old substring approach worked
        only because ``code`` happens to sit near the front of the document —
        anything past the cap was simply gone. Here a long ``detail`` pushes
        ``meta`` outside the cap: unreadable in the string, intact as data.
        """
        parsed = _envelope(409, _duplicate_body(detail="x" * 600))
        assert "job_post_id" not in parsed["error"]
        assert parsed["errors"][0]["meta"]["job_post_id"] == EXISTING_POST_ID
        assert has_error_code(parsed, DUPLICATE_CODE) is True

    def test_a_different_409_is_not_a_duplicate(self):
        # The carve-out is the code, never the status. A genuine conflict must
        # stay distinguishable from an "already exists".
        parsed = _envelope(409, {"errors": [{"status": "409", "code": "scrape_in_flight"}]})
        assert has_error_code(parsed, DUPLICATE_CODE) is False
        assert error_codes(parsed) == ["scrape_in_flight"]


class TestSuccessEnvelopeUnchanged:
    def test_success_carries_no_errors(self):
        parsed = _envelope(200, {"data": {"id": EXISTING_POST_ID, "type": "job-post"}})
        assert parsed["success"] is True
        assert parsed["errors"] == []
        assert error_codes(parsed) == []
        assert has_error_code(parsed, DUPLICATE_CODE) is False

    def test_created_still_reports_201(self):
        parsed = _envelope(201, {"data": {"id": EXISTING_POST_ID, "type": "job-post"}})
        assert parsed["success"] is True
        assert parsed["status_code"] == 201

    def test_frontend_url_injection_still_runs(self):
        # Guards the success path against collateral damage from the error-path
        # change: `_ok` still mutates the body before handing it over.
        parsed = _envelope(200, {"data": {"id": EXISTING_POST_ID, "type": "job-post"}})
        assert parsed["data"]["data"]["_frontend_url"] == f"/job-posts/{EXISTING_POST_ID}"


class TestNonJsonApiErrorShapes:
    """Not every failure comes back as a JSON:API document."""

    def test_middleware_403_detail_is_kept(self):
        # ApiKeyPermissionMiddleware short-circuits before any view runs and
        # emits a plain Django JsonResponse. No `code` to recover — but the
        # detail is the only clue you never reached the view at all.
        parsed = _envelope(403, {"errors": [{"detail": "Insufficient API key permissions"}]})
        assert parsed["errors"][0]["detail"] == "Insufficient API key permissions"
        assert parsed["errors"][0]["code"] is None
        assert error_codes(parsed) == []

    def test_bare_drf_detail_is_promoted_to_an_error(self):
        parsed = _envelope(401, {"detail": "Authentication credentials were not provided."})
        assert parsed["errors"][0]["detail"] == "Authentication credentials were not provided."

    def test_bare_error_key_is_promoted_to_an_error(self):
        parsed = _envelope(400, {"error": "bad request"})
        assert parsed["errors"][0]["detail"] == "bad request"

    def test_int_status_does_not_lose_the_code(self):
        # JSON:API says `status` is a string; an int must not take the whole
        # error object down with it.
        parsed = _envelope(409, {"errors": [{"status": 409, "code": DUPLICATE_CODE}]})
        assert has_error_code(parsed, DUPLICATE_CODE) is True
        assert parsed["errors"][0]["status"] == "409"


class TestNothingOnThisPathRaises:
    def test_html_proxy_page(self):
        # A 502 from the edge never reached the api. No JSON, no codes, but the
        # text must survive so the operator can see what answered.
        parsed = _envelope(502, "<html><body>Bad Gateway</body></html>")
        assert parsed["errors"] == []
        assert "Bad Gateway" in parsed["error"]

    def test_empty_body(self):
        parsed = _envelope(500, "")
        assert parsed["errors"] == []
        assert parsed["error"] == "500 - "

    def test_json_body_that_is_not_an_object(self):
        parsed = _envelope(400, ["nope"])
        assert parsed["errors"] == []

    def test_errors_key_holding_junk(self):
        parsed = _envelope(400, {"errors": ["a string", 7, None]})
        assert parsed["errors"] == []

    def test_malformed_error_object_degrades_to_detail(self):
        # `source` must be an object; a string is invalid. Keep the evidence
        # rather than dropping the entry or blowing up.
        parsed = _envelope(400, {"errors": [{"code": "weird", "source": "not-a-dict"}]})
        assert len(parsed["errors"]) == 1
        assert "not-a-dict" in parsed["errors"][0]["detail"]

    def test_unknown_json_api_keys_are_ignored(self):
        parsed = _envelope(
            409, {"errors": [{"id": "e1", "links": {"about": "/x"}, "code": DUPLICATE_CODE}]}
        )
        assert has_error_code(parsed, DUPLICATE_CODE) is True


class TestHelpersOnHostileInput:
    """``error_codes`` reads whatever a caller hands it, including garbage."""

    def test_missing_errors_key(self):
        assert error_codes({"success": False, "status_code": 500}) == []

    def test_errors_is_none(self):
        assert error_codes({"errors": None}) == []

    def test_non_string_code_is_skipped(self):
        assert error_codes({"errors": [{"code": 409}]}) == []

    def test_multiple_codes_are_all_returned(self):
        parsed = {"errors": [{"code": "a"}, {"code": None}, {"code": "b"}]}
        assert error_codes(parsed) == ["a", "b"]
        assert has_error_code(parsed, "b") is True
        assert has_error_code(parsed, "c") is False
