"""Fixture-driven tests for span_validator.filter_span_atomic.

Replays the jp 1724 cross-row hallucination against a ZipRecruiter
digest and confirms the deterministic filter drops the swapped triple
even when both rows share a host. Also covers a multi-paragraph
LinkedIn digest and a single-job control case.
"""
from __future__ import annotations

from collections import namedtuple
from pathlib import Path

from src.agents.span_validator import filter_span_atomic

Link = namedtuple("Link", "url title company")
FIXTURES = Path(__file__).parent / "fixtures" / "emails"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


class TestZipRecruiterCrossRowSwap:
    """jp 1724 reproduction: same-host digest, hallucinated triple."""

    SNBL_URL = "https://www.ziprecruiter.com/km/AAGjSNBL-tracker-token-001"
    FSD_URL = "https://www.ziprecruiter.com/km/BBHkFSDeveloper-token-002"

    def test_drops_cross_row_link(self):
        body = _load("ziprecruiter_km_tracker.txt")
        # The hallucination: title/company from row 2 (FSD), link from
        # row 1 (SNBL). Both rows are on the same host so a host-only
        # check would pass. Per-URL anchoring catches it.
        bad = Link(
            url=self.SNBL_URL,
            title="Junior to Mid Level Full Stack Developer",
            company="Web Connectivity LLC",
        )
        kept = filter_span_atomic([bad], body, email_id="zr-test")
        assert kept == []

    def test_keeps_coherent_snbl(self):
        body = _load("ziprecruiter_km_tracker.txt")
        good = Link(
            url=self.SNBL_URL,
            title="SNBL Bilingual Business Development Manager",
            company="SNBL USA",
        )
        kept = filter_span_atomic([good], body, email_id="zr-test")
        assert [k.url for k in kept] == [self.SNBL_URL]

    def test_keeps_coherent_fsd(self):
        body = _load("ziprecruiter_km_tracker.txt")
        good = Link(
            url=self.FSD_URL,
            title="Junior to Mid Level Full Stack Developer",
            company="Web Connectivity LLC",
        )
        kept = filter_span_atomic([good], body, email_id="zr-test")
        assert [k.url for k in kept] == [self.FSD_URL]


class TestLinkedInMultiJobDigest:
    def test_keeps_all_coherent_links(self):
        body = _load("linkedin_digest_5jobs.txt")
        links = [
            Link(
                "https://www.linkedin.com/jobs/view/100001",
                "Senior Backend Engineer",
                "Acme Inc",
            ),
            Link(
                "https://www.linkedin.com/jobs/view/100002",
                "Frontend Lead",
                "Beta Labs",
            ),
            Link(
                "https://www.linkedin.com/jobs/view/100003",
                "Staff DevOps Engineer",
                "Gamma Cloud",
            ),
            Link(
                "https://www.linkedin.com/jobs/view/100004",
                "Engineering Manager",
                "Delta Systems",
            ),
            Link(
                "https://www.linkedin.com/jobs/view/100005",
                "Site Reliability Engineer",
                "Epsilon Networks",
            ),
        ]
        kept = filter_span_atomic(links, body)
        assert {k.url for k in kept} == {link.url for link in links}

    def test_drops_swapped_link_in_multi_job_digest(self):
        body = _load("linkedin_digest_5jobs.txt")
        # Take row 1's link with row 3's title/company.
        swapped = Link(
            url="https://www.linkedin.com/jobs/view/100001",
            title="Staff DevOps Engineer",
            company="Gamma Cloud",
        )
        kept = filter_span_atomic([swapped], body)
        assert kept == []


class TestSingleJobControl:
    def test_passes_single_job_alert(self):
        body = _load("indeed_alert_singlejob.txt")
        link = Link(
            url="https://www.indeed.com/viewjob?jk=abc123solo",
            title="Backend Engineer",
            company="Solo Co",
        )
        kept = filter_span_atomic([link], body, email_id="indeed-test")
        assert len(kept) == 1


class TestEdgeCases:
    def test_bare_url_no_signal_accepted(self):
        body = _load("linkedin_digest_5jobs.txt")
        bare = Link("https://www.linkedin.com/jobs/view/100001", "", "")
        kept = filter_span_atomic([bare], body)
        assert kept == [bare]

    def test_host_absent_dropped(self):
        body = _load("linkedin_digest_5jobs.txt")
        invented = Link(
            url="https://hallucinated.example.com/jobs/999",
            title="Senior Backend Engineer",
            company="Acme Inc",
        )
        kept = filter_span_atomic([invented], body)
        assert kept == []
