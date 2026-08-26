"""AUTO-52 — the dedupe tracer stops calling a duplicate a failure.

``lib/trace_dedupe`` is the forensics layer over every JobPost write. It
classified any non-success envelope as ``failed`` and logged it at WARNING,
because the transport gave it nothing finer to go on. So during CC-125 the one
tool built to explain what the write path was doing reported thousands of
routine duplicates as errors — the signal that should have exposed the loop was
the signal drowning it out.

With ``code`` readable as data (see ``test_api_client_structured_errors``), a
``duplicate_job_post`` 409 now gets its own outcome, and the tracer can name the
row that was collided with: the api puts the existing post's id in the error's
``meta``, which the old string collapse destroyed.
"""

from __future__ import annotations

import json

import httpx

from lib.trace_dedupe import _classify, _duplicate_post_id
from src.client.api_client import ApiClient

EXISTING_POST_ID = "V30p4hHABQ"


def _parsed(status_code: int, body: dict | str) -> dict:
    """Drive the real ``_ok`` so the tracer is tested against the real shape."""
    if isinstance(body, str):
        response = httpx.Response(status_code, text=body)
    else:
        response = httpx.Response(status_code, json=body)
    return json.loads(ApiClient("https://example.test", "jh_test")._ok(response))


def _duplicate_409() -> dict:
    return _parsed(
        409,
        {
            "errors": [
                {
                    "status": "409",
                    "code": "duplicate_job_post",
                    "detail": "A job post with this link already exists.",
                    "meta": {"job_post_id": EXISTING_POST_ID, "title": "Staff Engineer"},
                }
            ]
        },
    )


class TestClassify:
    def test_duplicate_409_is_not_failed(self):
        assert _classify(_duplicate_409()) == "duplicate_conflict"

    def test_other_409_is_still_failed(self):
        parsed = _parsed(409, {"errors": [{"status": "409", "code": "scrape_in_flight"}]})
        assert _classify(parsed) == "failed"

    def test_codeless_409_is_still_failed(self):
        parsed = _parsed(409, {"errors": [{"detail": "conflict"}]})
        assert _classify(parsed) == "failed"

    def test_server_error_is_failed(self):
        assert _classify(_parsed(500, "boom")) == "failed"

    def test_merge_path_unchanged(self):
        assert _classify(_parsed(200, {"data": {"id": EXISTING_POST_ID}})) == "merged_into_existing"

    def test_create_unchanged(self):
        assert _classify(_parsed(201, {"data": {"id": EXISTING_POST_ID}})) == "created"

    def test_unexpected_2xx_still_falls_through_loudly(self):
        """The ``ok_other`` fallback is unreachable through ``_ok`` today.

        ``_ok`` counts only 200/201/202 as success, so no response it builds can
        arrive here marked successful with some other status. Asserted against a
        hand-built envelope on purpose — this branch exists for a future ``_ok``
        that admits another 2xx, and it should keep classifying rather than
        crash when that day comes.
        """
        assert _classify({"success": True, "status_code": 207}) == "ok_other"


class TestDuplicatePostId:
    def test_reads_the_collided_row_out_of_meta(self):
        assert _duplicate_post_id(_duplicate_409()) == EXISTING_POST_ID

    def test_none_when_the_409_is_not_a_duplicate(self):
        parsed = _parsed(409, {"errors": [{"code": "scrape_in_flight", "meta": {"x": 1}}]})
        assert _duplicate_post_id(parsed) is None

    def test_none_when_meta_is_absent(self):
        parsed = _parsed(409, {"errors": [{"code": "duplicate_job_post"}]})
        assert _duplicate_post_id(parsed) is None

    def test_none_on_a_success_envelope(self):
        assert _duplicate_post_id(_parsed(200, {"data": {}})) is None

    def test_id_is_stringified_never_int_cast(self):
        # Job-hunting PKs are NanoID strings (CC-77/CC-79). A numeric-looking id
        # must come back as a string, not silently become an int somewhere.
        parsed = _parsed(
            409,
            {"errors": [{"code": "duplicate_job_post", "meta": {"job_post_id": 42}}]},
        )
        assert _duplicate_post_id(parsed) == "42"
