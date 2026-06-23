"""
Career Caddy API client.

Thin async HTTP wrapper for the Career Caddy JSON:API. Extracted from the
Career Caddy monorepo (ai/lib/api_tools.py) for standalone use.

Usage:
    from src.client import ApiClient

    api = ApiClient("https://api.careercaddy.online", "jh_xxx")
    result = await api.get("/api/v1/job-posts/")
"""

import json
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, Field, PositiveInt

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class APIResponse(BaseModel):
    success: bool
    data: dict | None = None
    error: str | None = None
    status_code: int | None = None


class JobPostCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    company_id: int = Field(..., gt=0)
    location: str | None = Field(None, max_length=100)
    salary_min: int | None = Field(None, ge=0)
    salary_max: int | None = Field(None, ge=0)
    employment_type: str | None = None
    remote_ok: bool = False
    link: str | None = None
    posted_date: str | None = None
    source: str | None = None


_APPLICATION_SORT_FIELDS = Literal[
    "id", "applied_at", "status", "job_post_id", "company_id", "notes"
]


# ---------------------------------------------------------------------------
# ApiClient — thin async HTTP wrapper
# ---------------------------------------------------------------------------


_TYPE_TO_ROUTE = {
    "job-post": "job-posts",
    "job-application": "job-applications",
    "company": "companies",
    "score": "scores",
    "resume": "resumes",
    "cover-letter": "cover-letters",
    "question": "questions",
    "answer": "answers",
    "summary": "summaries",
    "scrape": "scrapes",
}


def _inject_frontend_urls(data: dict) -> dict:
    """Add _frontend_url and strip API links so the LLM uses frontend paths."""

    def _tag(resource):
        if isinstance(resource, dict) and "type" in resource and "id" in resource:
            route = _TYPE_TO_ROUTE.get(resource["type"])
            if route:
                resource["_frontend_url"] = f"/{route}/{resource['id']}"
            resource.pop("links", None)
            resource.pop("relationships", None)
        return resource

    if isinstance(data.get("data"), list):
        for item in data["data"]:
            _tag(item)
    elif isinstance(data.get("data"), dict):
        _tag(data["data"])
    return data


class ApiClient:
    """HTTP client that forwards a token to the Career Caddy API."""

    def __init__(self, base_url: str, token: str, timeout: int = 120):
        self.base_url = base_url
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-Forwarded-Proto": "https",
        }

    def _ok(self, response: httpx.Response) -> str:
        if response.status_code in (200, 201, 202):
            body = response.json()
            _inject_frontend_urls(body)
            result = APIResponse(success=True, data=body, status_code=response.status_code)
        else:
            text = response.text[:500] if len(response.text) > 500 else response.text
            result = APIResponse(
                success=False,
                error=f"{response.status_code} - {text}",
                status_code=response.status_code,
            )
        return json.dumps(result.model_dump(), indent=2)

    async def get(self, path: str, params: dict | None = None) -> str:
        async with httpx.AsyncClient(follow_redirects=True, timeout=self.timeout) as client:
            resp = await client.get(
                urljoin(self.base_url, path),
                headers=self._headers,
                params=params,
            )
            return self._ok(resp)

    async def post(self, path: str, payload: dict) -> str:
        async with httpx.AsyncClient(follow_redirects=True, timeout=self.timeout) as client:
            resp = await client.post(
                urljoin(self.base_url, path),
                headers=self._headers,
                json=payload,
            )
            return self._ok(resp)

    async def patch(self, path: str, payload: dict) -> str:
        async with httpx.AsyncClient(follow_redirects=True, timeout=self.timeout) as client:
            resp = await client.patch(
                urljoin(self.base_url, path),
                headers=self._headers,
                json=payload,
            )
            return self._ok(resp)


# ---------------------------------------------------------------------------
# Tool implementations — all take an ApiClient as first argument
# ---------------------------------------------------------------------------


async def create_company(
    api: ApiClient,
    name: str,
    description: str | None = None,
    website: str | None = None,
    industry: str | None = None,
    size: str | None = None,
    location: str | None = None,
) -> str:
    """Create a new company."""
    from src.client.models import CompanyData as _CompanyData

    try:
        data = _CompanyData(
            name=name,
            description=description,
            website=website,
            industry=industry,
            size=size,
            location=location,
        )
        payload = {
            "data": {
                "type": "company",
                "attributes": data.model_dump(exclude_none=True),
            }
        }
        return await api.post("/api/v1/companies/", payload)
    except ValueError as e:
        return json.dumps(
            APIResponse(success=False, error=f"Validation error: {e}").model_dump(),
            indent=2,
        )


async def find_company_by_name(api: ApiClient, company_name: str) -> str:
    """Find a company by name (case-insensitive search)."""
    result = await api.get("/api/v1/companies/", params={"filter[query]": company_name})
    data = json.loads(result)
    if data.get("success"):
        companies = data.get("data", {}).get("data", [])
        if companies:
            return json.dumps(
                APIResponse(
                    success=True,
                    data={"companies": companies, "count": len(companies)},
                    status_code=200,
                ).model_dump(),
                indent=2,
            )
        return json.dumps(
            APIResponse(
                success=False,
                error=f"No companies found matching '{company_name}'",
                status_code=404,
            ).model_dump(),
            indent=2,
        )
    return result


async def find_user_by_username(api: ApiClient, username: str) -> str:
    """Look up a user by exact `username` via the staff-only users filter.

    Returns the api JSON envelope. On success, the caller can read
    ``data.data`` — a list with the matched user (or empty list when no
    match). Staff-only api endpoint (added in api PR #151); a non-staff
    key gets 403.

    The caller resolves localpart → user id by:
        resp = json.loads(await find_user_by_username(api, "dough"))
        users = (resp.get("data") or {}).get("data") or []
        user_id = int(users[0]["id"]) if users else None

    Username has the same syntactic validation as the catchall validator
    (api `audit_usernames` mgmt cmd); cc_auto trusts the api's filter to
    return an empty list rather than raising on a syntactically valid
    but non-existent username.
    """
    return await api.get("/api/v1/users/", params={"filter[username]": username})


async def search_companies(
    api: ApiClient,
    query: str | None = None,
    page_size: int | None = None,
) -> str:
    """Search companies by name or display_name (case-insensitive OR match)."""
    params = {}
    if query is not None:
        params["filter[query]"] = query
    if page_size is not None:
        params["page[size]"] = page_size
    return await api.get("/api/v1/companies/", params=params)


async def get_companies(api: ApiClient, id: int | None = None) -> str:
    """Fetch companies. Pass id to retrieve a single company; omit for the full list."""
    if id is not None:
        return await api.get(f"/api/v1/companies/{id}/")
    return await api.get("/api/v1/companies/")


# Email-tier sources cc_auto declares as incomplete on create (Posture E).
# These rows ship to the api as title + company + link only, with no real
# description; flagging complete=False up front routes them through the
# existing incomplete-recovery pipeline instead of pretending they're
# fully fleshed-out. The api gates inbound complete=False on the same
# trust threshold, so this is a synced contract — keep the two lists in
# step with api/job_hunting/models/job_post_dedupe.py SOURCE_TRUST.
#
# `email-forward` was added in api PR #149 (2026-06): catchall-forward
# JobPosts carrying provenance via `forwarded_via_address` +
# `discover_for_user_id`. Same trust posture as the other email-tier
# sources: title/description from a forwarded JD is best-effort, not a
# scraped canonical posting.
_EMAIL_TIER_SOURCES = frozenset({"email", "email_direct", "email-forward"})


async def create_job_post_minimal(
    api: ApiClient,
    title: str,
    link: str | None = None,
    description: str | None = None,
    source: str = "email",
    forwarded_via_address: str | None = None,
    discover_for_user_id: int | None = None,
) -> str:
    """Create a job post with no company relationship.

    Backend is idempotent on `link` — POSTing the same link twice returns
    the existing row (200) instead of a duplicate error. Use this for
    email-discovered postings where we don't have enough info to attach a
    company. Users can attach one later via the UI.

    `source` rides through to JobPost.source AND the JobPostDiscovery
    row the API auto-creates for the caller, so the post can be filtered
    by provenance later. Defaults to "email" because this helper is the
    email-ingest path.

    For the catchall email-forward pipeline (B3), the api accepts two
    extra attributes (api PRs #149, #150):

    - `forwarded_via_address`: the `<localpart>@careercaddy.online`
      address the user forwarded TO. Persists on JobPostDiscovery so
      the UI can show which address surfaced this post.
    - `discover_for_user_id`: the user the discovery row should attach
      to. Required only on staff-authenticated calls that need to
      attribute the post to a *different* user than the caller (the
      catchall poller runs under one staff key but creates posts for
      many users). Omit on normal user-key calls.
    """
    attrs: dict = {"title": title, "link": link, "source": source}
    if description:
        attrs["description"] = description
    if forwarded_via_address is not None:
        attrs["forwarded_via_address"] = forwarded_via_address
    if discover_for_user_id is not None:
        attrs["discover_for_user_id"] = discover_for_user_id
    if source in _EMAIL_TIER_SOURCES:
        attrs["complete"] = False
    payload = {"data": {"type": "job-post", "attributes": attrs}}
    return await api.post("/api/v1/job-posts/", payload)


async def find_job_post_by_link(api: ApiClient, link: str) -> str:
    """Find a job post by its original posting URL."""
    return await api.get("/api/v1/job-posts/", params={"filter[link]": link})


async def create_job_post_with_company_check(
    api: ApiClient,
    title: str,
    company_name: str | None = None,
    description: str | None = None,
    location: str | None = None,
    salary_min: int | None = None,
    salary_max: int | None = None,
    employment_type: str | None = None,
    remote_ok: bool = False,
    url: str | None = None,
    link: str | None = None,
    posted_date: str | None = None,
    company_description: str | None = None,
    company_website: str | None = None,
    company_industry: str | None = None,
    company_size: str | None = None,
    company_location: str | None = None,
    source: str = "chat",
    forwarded_via_address: str | None = None,
    discover_for_user_id: int | None = None,
) -> str:
    """Create a job post, creating the company first if it doesn't exist.

    `source` defaults to "email" because cc_auto's primary caller is the
    email-ingest pipeline; rides through to JobPost.source and the
    JobPostDiscovery row the API auto-creates for the caller.

    `forwarded_via_address` and `discover_for_user_id` are the catchall
    email-forward provenance attributes (api PRs #149/#150). See
    ``create_job_post_minimal`` for the semantics — they're identical
    here.
    """
    job_url = url or link
    if not company_name:
        return json.dumps(
            APIResponse(
                success=False, error="company_name is required to create a job post"
            ).model_dump(),
            indent=2,
        )

    _PLACEHOLDER_NAMES = {"unknown", "n/a", "na", "none", "tbd", "not specified", ""}
    if company_name.strip().lower() in _PLACEHOLDER_NAMES:
        return json.dumps(
            APIResponse(
                success=False,
                error=(
                    f"'{company_name}' is not an acceptable company name. "
                    "Infer the company from: (1) the recruiter's company, "
                    "(2) the email sender domain, (3) the job posting URL domain. "
                    "If you cannot determine the company, ask the user."
                ),
            ).model_dump(),
            indent=2,
        )

    try:
        # No client-side link-dedupe short-circuit. The API's POST handler
        # owns dedupe (link match + fingerprint match) and now also merges
        # incoming fields onto the existing row — bailing here would skip
        # that merge, leaving stub posts (link known, company NULL) stuck
        # in their original state forever. Process_tagged treats 200 as
        # duplicate, so the caller-side bookkeeping is unchanged.
        company_search = json.loads(await find_company_by_name(api, company_name))
        company_id = None
        if company_search.get("success"):
            companies = company_search.get("data", {}).get("companies", [])
            if companies:
                company_id = int(companies[0].get("id"))

        if company_id is None:
            create_result = json.loads(
                await create_company(
                    api,
                    name=company_name,
                    description=company_description,
                    website=company_website,
                    industry=company_industry,
                    size=company_size,
                    location=company_location,
                )
            )
            if create_result.get("success"):
                company_id = int(create_result.get("data", {}).get("data", {}).get("id"))
            else:
                return json.dumps(
                    APIResponse(
                        success=False,
                        error=f"Failed to create company: {create_result.get('error')}",
                    ).model_dump(),
                    indent=2,
                )

        job_data = JobPostCreate(
            title=title,
            description=description,
            company_id=company_id,
            location=location,
            salary_min=salary_min,
            salary_max=salary_max,
            employment_type=employment_type,
            remote_ok=remote_ok,
            link=job_url,
            posted_date=posted_date,
            source=source,
        )
        attributes = job_data.model_dump(exclude={"company_id"}, exclude_none=True)
        # Tag provenance so the backend sankey can attribute this post
        # to the code path that created it (chat agent, email pipeline, ...).
        attributes["source"] = source
        if forwarded_via_address is not None:
            attributes["forwarded_via_address"] = forwarded_via_address
        if discover_for_user_id is not None:
            attributes["discover_for_user_id"] = discover_for_user_id
        if source in _EMAIL_TIER_SOURCES:
            attributes["complete"] = False
        payload = {
            "data": {
                "type": "job-post",
                "attributes": attributes,
                "relationships": {"company": {"data": {"type": "company", "id": str(company_id)}}},
            }
        }
        return await api.post("/api/v1/job-posts/", payload)

    except Exception as e:
        return json.dumps(
            APIResponse(
                success=False,
                error=f"Error creating job post with company check: {e}",
            ).model_dump(),
            indent=2,
        )


async def search_job_posts(
    api: ApiClient,
    query: str | None = None,
    title: str | None = None,
    company: str | None = None,
    company_id: int | None = None,
    sort: str | None = None,
    page_size: int | None = None,
) -> str:
    """Search job posts by keyword, title, company name, or company ID."""
    params = {}
    if query is not None:
        params["filter[query]"] = query
    if title is not None:
        params["filter[title]"] = title
    if company is not None:
        params["filter[company]"] = company
    if company_id is not None:
        params["filter[company_id]"] = company_id
    if sort is not None:
        params["sort"] = sort
    if page_size is not None:
        params["page[size]"] = page_size
    return await api.get("/api/v1/job-posts/", params=params)


# ---------------------------------------------------------------------------
# Pre-create near-dupe check (operator-side, REST-only)
# ---------------------------------------------------------------------------

# Confidence ranking used to pick the strongest candidate (high is most
# certain). Mirrors the api's by-id /duplicate-candidates/ vocabulary.
_DUP_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}

# Title-similarity guards. The api owns the authoritative title_similarity
# (its content-fingerprint logic, which we can't import across the HTTP
# boundary); these reproduce its INTENT conservatively: the shorter
# normalized title must be a prefix/suffix of the longer AND substantial
# enough that the overlap isn't a one-word collision ("Engineer" sitting
# inside "Engineering Manager"). Deliberately strict — a missed near-dupe
# (still created + reviewable) beats a false-positive that could later be
# auto-skipped.
_DUP_TITLE_MIN_LEN = 8
_DUP_TITLE_MIN_RATIO = 0.6


@dataclass(frozen=True)
class DuplicateCandidate:
    """One suspected-duplicate JobPost surfaced by the pre-create check.

    Shape mirrors the api's by-id ``/duplicate-candidates/`` payload and
    the public-MCP ``find_duplicate_candidates`` composite so cc_auto and
    the api stay in lockstep on what "duplicate" means.
    """

    id: int
    title: str
    company_name: str | None
    confidence: str  # "high" | "medium" | "low"
    match_signals: list[str]
    frontend_url: str | None = None


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _normalize_title(title: str | None) -> str:
    return " ".join((title or "").lower().split())


def _title_relation(incoming: str, existing: str) -> str | None:
    """Classify two titles → ``"title_exact"`` / ``"title_similarity"`` / None.

    Conservative prefix/suffix heuristic — see the module guards above.
    """
    a, b = _normalize_title(incoming), _normalize_title(existing)
    if not a or not b:
        return None
    if a == b:
        return "title_exact"
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < _DUP_TITLE_MIN_LEN:
        return None
    if len(shorter) / len(longer) < _DUP_TITLE_MIN_RATIO:
        return None
    if longer.startswith(shorter) or longer.endswith(shorter):
        return "title_similarity"
    return None


async def _resolve_company_id(api: ApiClient, company_name: str) -> int | None:
    """Resolve a company name → id via ``find_company_by_name``; None on miss."""
    try:
        resp = json.loads(await find_company_by_name(api, company_name))
    except Exception:
        return None
    if not resp.get("success"):
        return None
    companies = (resp.get("data") or {}).get("companies") or []
    if not companies:
        return None
    return _coerce_int(companies[0].get("id"))


async def find_duplicate_candidates(
    api: ApiClient,
    *,
    title: str,
    company: str | None = None,
    link: str | None = None,
    max_results: int = 10,
) -> list[DuplicateCandidate]:
    """Pre-create near-dupe check — does an incoming posting already exist?

    The operator-side, REST-only twin of the public-MCP
    ``find_duplicate_candidates`` composite (which has no api endpoint of
    its own — it is itself built from ``find_job_post_by_link`` +
    ``find_company_by_name`` + ``search_job_posts`` + a local title
    compare). Reproducing that strategy here lets cc_auto's catchall poller
    run the check over plain HTTP REST without the MCP transport, while
    staying in lockstep with the api's documented composite.

    Strategy (kept aligned with the MCP composite):

      - ``link`` exact match  → confidence ``"high"``,  signal ``"link"``.
      - ``company`` resolved  → list its posts, compare titles locally:
          * normalized-equal title → ``"high"``,   signal ``"title_exact"``.
          * prefix/suffix overlap  → ``"medium"``, signal ``"title_similarity"``.

    This is the EXTRA net for *non-canonical* near-dupes — the same role
    re-listed from a different source URL — that the api's POST-time
    canonical dedupe (``canonical_link`` + ``fingerprint``) does not catch.
    It never replaces that dedupe: the caller still POSTs, the api still
    returns created/deduped.

    Fail-safe by contract: any miss / parse error / api exception returns
    ``[]`` (mirrors ``fetch_profile_readiness`` returning ``None``), so a
    lookup hiccup degrades to "looks unique" and the caller fails OPEN —
    it still creates the post. ``title`` alone (no link, no company) is too
    low-signal and returns ``[]``.
    """
    try:
        candidates: dict[int, DuplicateCandidate] = {}

        # 1) Exact-link match (high). The api POST dedupes this too, but
        #    surfacing it makes the pre-create decision observable.
        if link:
            try:
                resp = json.loads(await find_job_post_by_link(api, link))
            except Exception:
                resp = {}
            if resp.get("success"):
                for row in (resp.get("data") or {}).get("data") or []:
                    cid = _coerce_int(row.get("id"))
                    if cid is None:
                        continue
                    attrs = row.get("attributes") or {}
                    candidates[cid] = DuplicateCandidate(
                        id=cid,
                        title=attrs.get("title") or "",
                        company_name=attrs.get("company_name"),
                        confidence="high",
                        match_signals=["link"],
                        frontend_url=row.get("_frontend_url"),
                    )

        # 2) Same-company title comparison (high on exact, medium on drift).
        if company:
            company_id = await _resolve_company_id(api, company)
            if company_id is not None:
                try:
                    resp = json.loads(
                        await search_job_posts(
                            api, company_id=company_id, page_size=max_results * 5
                        )
                    )
                except Exception:
                    resp = {}
                if resp.get("success"):
                    for row in (resp.get("data") or {}).get("data") or []:
                        cid = _coerce_int(row.get("id"))
                        if cid is None:
                            continue
                        attrs = row.get("attributes") or {}
                        relation = _title_relation(title, attrs.get("title") or "")
                        if relation is None:
                            continue
                        confidence = "high" if relation == "title_exact" else "medium"
                        existing = candidates.get(cid)
                        if existing is None:
                            candidates[cid] = DuplicateCandidate(
                                id=cid,
                                title=attrs.get("title") or "",
                                company_name=attrs.get("company_name") or company,
                                confidence=confidence,
                                match_signals=[relation],
                                frontend_url=row.get("_frontend_url"),
                            )
                        else:
                            # Same row matched link + title: union signals,
                            # keep the strongest confidence.
                            signals = list(dict.fromkeys([*existing.match_signals, relation]))
                            best = existing.confidence
                            if _DUP_CONFIDENCE_RANK.get(confidence, 0) > _DUP_CONFIDENCE_RANK.get(
                                best, 0
                            ):
                                best = confidence
                            candidates[cid] = replace(
                                existing, confidence=best, match_signals=signals
                            )

        return sorted(
            candidates.values(),
            key=lambda c: _DUP_CONFIDENCE_RANK.get(c.confidence, 0),
            reverse=True,
        )[:max_results]
    except Exception:
        return []


async def get_job_posts(
    api: ApiClient,
    id: int | None = None,
    sort: str | None = None,
    order: str | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> str:
    """Fetch job posts. Pass id for a single post; omit for a paginated list."""
    if id is not None:
        return await api.get(f"/api/v1/job-posts/{id}/")
    params = {}
    if sort is not None:
        params["sort"] = sort
    if order is not None:
        params["order"] = order
    if page is not None:
        params["page"] = page
    if per_page is not None:
        params["per_page"] = per_page
    return await api.get("/api/v1/job-posts/", params=params)


async def update_job_post(
    api: ApiClient,
    job_post_id: PositiveInt,
    title: str | None = None,
    description: str | None = None,
    location: str | None = None,
    salary_min: int | None = None,
    salary_max: int | None = None,
    employment_type: str | None = None,
    remote_ok: bool | None = None,
    link: str | None = None,
    posted_date: str | None = None,
    company_id: int | None = None,
) -> str:
    """Update an existing job post's attributes or company relationship."""
    attributes = {}
    if title is not None:
        attributes["title"] = title
    if description is not None:
        attributes["description"] = description
    if location is not None:
        attributes["location"] = location
    if salary_min is not None:
        attributes["salary_min"] = salary_min
    if salary_max is not None:
        attributes["salary_max"] = salary_max
    if employment_type is not None:
        attributes["employment_type"] = employment_type
    if remote_ok is not None:
        attributes["remote_ok"] = remote_ok
    if link is not None:
        attributes["link"] = link
    if posted_date is not None:
        attributes["posted_date"] = posted_date

    if not attributes and company_id is None:
        return json.dumps(
            APIResponse(success=False, error="No fields provided to update").model_dump(),
            indent=2,
        )

    payload: dict = {
        "data": {
            "type": "job-post",
            "id": str(job_post_id),
            "attributes": attributes,
        }
    }
    if company_id is not None:
        payload["data"]["relationships"] = {
            "company": {"data": {"type": "company", "id": str(company_id)}}
        }
    return await api.patch(f"/api/v1/job-posts/{job_post_id}/", payload)


async def create_job_application(
    api: ApiClient,
    job_post_id: PositiveInt,
    status: str = "applied",
    notes: str | None = None,
    applied_at: str | None = None,
) -> str:
    """Create a new job application linked to an existing job post."""
    attributes: dict = {"status": status}
    if notes is not None:
        attributes["notes"] = notes
    if applied_at is not None:
        attributes["applied_at"] = applied_at

    payload = {
        "data": {
            "type": "job-application",
            "attributes": attributes,
            "relationships": {"job-post": {"data": {"type": "job-post", "id": str(job_post_id)}}},
        }
    }
    return await api.post("/api/v1/job-applications/", payload)


async def get_job_applications(
    api: ApiClient,
    id: PositiveInt | None = None,
    company: str | None = None,
    company_id: PositiveInt | None = None,
    status: str | None = None,
    query: str | None = None,
    sort: _APPLICATION_SORT_FIELDS | None = None,
    order: Literal["asc", "desc"] | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> str:
    """Fetch job applications. Pass id for a single application; omit for a list.

    Filters (all optional, combinable):
      company    — case-insensitive substring of company name (e.g. "starbucks")
      company_id — exact company FK (use when you already resolved it)
      status     — case-insensitive substring of status (e.g. "rejected")
      query      — broad text match across title/company/status/notes
    """
    if id is not None:
        return await api.get(f"/api/v1/job-applications/{id}/")
    params: dict = {}
    if company is not None:
        params["filter[company]"] = company
    if company_id is not None:
        params["filter[company_id]"] = company_id
    if status is not None:
        params["filter[status]"] = status
    if query is not None:
        params["filter[query]"] = query
    if sort is not None:
        params["sort"] = sort
    if order is not None:
        params["order"] = order
    if page is not None:
        params["page"] = page
    if per_page is not None:
        params["per_page"] = per_page
    return await api.get("/api/v1/job-applications/", params=params)


async def get_applications_for_job_post(api: ApiClient, job_post_id: PositiveInt) -> str:
    """Fetch all job applications linked to a specific job post."""
    return await api.get(f"/api/v1/job-posts/{job_post_id}/job-applications/")


async def update_job_application(
    api: ApiClient,
    application_id: PositiveInt,
    status: str | None = None,
    notes: str | None = None,
    applied_at: str | None = None,
    company_id: int | None = None,
) -> str:
    """Update a job application's status, notes, or company association."""
    attributes = {}
    if status is not None:
        attributes["status"] = status
    if notes is not None:
        attributes["notes"] = notes
    if applied_at is not None:
        attributes["applied_at"] = applied_at

    if not attributes and company_id is None:
        return json.dumps(
            APIResponse(success=False, error="No fields provided to update").model_dump(),
            indent=2,
        )

    payload: dict = {
        "data": {
            "type": "job-application",
            "id": str(application_id),
            "attributes": attributes,
        }
    }
    if company_id is not None:
        payload["data"]["relationships"] = {
            "company": {"data": {"type": "company", "id": str(company_id)}}
        }
    return await api.patch(f"/api/v1/job-applications/{application_id}/", payload)


async def get_career_data(api: ApiClient) -> str:
    """Fetch the user's personal career profile."""
    return await api.get("/api/v1/career-data/")


async def get_resumes(
    api: ApiClient,
    id: int | None = None,
    favorite: bool | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> str:
    """Fetch resumes. Pass id for a single resume; omit for a paginated list."""
    if id is not None:
        return await api.get(f"/api/v1/resumes/{id}/")
    params = {}
    if favorite is not None:
        params["favorite"] = str(favorite).lower()
    if page is not None:
        params["page"] = page
    if per_page is not None:
        params["per_page"] = per_page
    return await api.get("/api/v1/resumes/", params=params)


async def create_scrape(
    api: ApiClient,
    url: str,
    job_post_id: int | None = None,
    company_id: int | None = None,
    status: str | None = None,
    attended: bool = False,
) -> str:
    """Create a scrape record.

    ``attended=True`` marks the scrape so ONLY an attended runner
    (``make runner ARGS="--attended"``, warm cookies/login) claims it via
    the api's partitioned claim-next; default runners claim only
    ``attended=False`` holds. Used to route known-good auto-scrapes to the
    operator's attended session. The api reads the ``attended`` attribute
    snake_case (it does not dasherize JSON:API attribute keys). When
    ``False`` (the default) the attribute is omitted, so existing call
    sites send a byte-identical payload.
    """
    attributes: dict = {"url": url}
    if status:
        attributes["status"] = status
    if attended:
        attributes["attended"] = True
    relationships = {}
    if job_post_id is not None:
        relationships["job-post"] = {"data": {"type": "job-post", "id": str(job_post_id)}}
    if company_id is not None:
        relationships["company"] = {"data": {"type": "company", "id": str(company_id)}}

    payload: dict = {"data": {"type": "scrape", "attributes": attributes}}
    if relationships:
        payload["data"]["relationships"] = relationships

    return await api.post("/api/v1/scrapes/", payload)


async def fetch_profile_readiness(api: ApiClient, hostname: str) -> tuple[bool, str | None] | None:
    """Read a domain's scrape-readiness signal from the api.

    Hits the staff-only ScrapeProfile filter endpoint
    ``GET /api/v1/scrape-profiles/?filter[hostname]=<hostname>`` (api PR
    #185) and returns ``(is_known_good, tier)`` for the first matched
    profile, or ``None`` when there's no profile for the host *or* the
    fetch fails for any reason.

    ``is_known_good`` is read snake_case — the api does not dasherize
    JSON:API attribute keys. ``tier`` comes from the nested
    ``readiness.tier`` and may be ``None`` even on a hit.

    Fail-safe by contract: any miss / parse error / non-success envelope
    returns ``None`` so callers treat "unknown" exactly like "not
    known-good". cc_auto's API key is staff-scoped, so the filter is
    accessible (a non-staff key would get 403 → ``None`` here too).
    """
    try:
        raw = await api.get("/api/v1/scrape-profiles/", params={"filter[hostname]": hostname})
        resp = json.loads(raw)
    except Exception:
        return None
    if not resp.get("success"):
        return None
    profiles = (resp.get("data") or {}).get("data") or []
    if not profiles:
        return None
    attrs = profiles[0].get("attributes") or {}
    is_known_good = bool(attrs.get("is_known_good"))
    readiness = attrs.get("readiness")
    tier = readiness.get("tier") if isinstance(readiness, dict) else None
    return is_known_good, tier


async def get_scrapes(
    api: ApiClient,
    id: int | None = None,
    sort: str | None = None,
    page: int | None = None,
    per_page: int | None = None,
    status: str | None = None,
    job_post_id: int | None = None,
) -> str:
    """Fetch scrape records."""
    if id is not None:
        return await api.get(f"/api/v1/scrapes/{id}/")
    params = {}
    if sort is not None:
        params["sort"] = sort
    if page is not None:
        params["page"] = page
    if per_page is not None:
        params["per_page"] = per_page
    if status is not None:
        params["filter[status]"] = status
    if job_post_id is not None:
        params["filter[job_post_id]"] = job_post_id
    return await api.get("/api/v1/scrapes/", params=params)


async def update_scrape(
    api: ApiClient,
    scrape_id: PositiveInt,
    status: str | None = None,
    job_content: str | None = None,
    url: str | None = None,
) -> str:
    """Update a scrape record's status, content, or URL."""
    attributes = {}
    if status is not None:
        attributes["status"] = status
    if job_content is not None:
        attributes["job_content"] = job_content
    if url is not None:
        attributes["url"] = url

    if not attributes:
        return json.dumps(
            APIResponse(success=False, error="No fields provided to update").model_dump(),
            indent=2,
        )

    payload = {
        "data": {
            "type": "scrape",
            "id": str(scrape_id),
            "attributes": attributes,
        }
    }
    return await api.patch(f"/api/v1/scrapes/{scrape_id}/", payload)


async def get_questions(
    api: ApiClient,
    id: int | None = None,
    company_id: int | None = None,
    job_post_id: int | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> str:
    """Fetch interview questions."""
    if id is not None:
        return await api.get(f"/api/v1/questions/{id}/")
    params = {}
    if company_id is not None:
        params["filter[company_id]"] = company_id
    if job_post_id is not None:
        params["filter[job_post_id]"] = job_post_id
    if page is not None:
        params["page"] = page
    if per_page is not None:
        params["per_page"] = per_page
    return await api.get("/api/v1/questions/", params=params)


async def get_answers(
    api: ApiClient,
    id: int | None = None,
    question_id: int | None = None,
    favorite: bool | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> str:
    """Fetch answers to interview questions."""
    if id is not None:
        return await api.get(f"/api/v1/answers/{id}/")
    params = {}
    if question_id is not None:
        params["filter[question_id]"] = question_id
    if favorite is not None:
        params["favorite"] = str(favorite).lower()
    if page is not None:
        params["page"] = page
    if per_page is not None:
        params["per_page"] = per_page
    return await api.get("/api/v1/answers/", params=params)


async def score_job_post(api: ApiClient, job_post_id: PositiveInt) -> str:
    """Score a job post against the user's career data."""
    payload = {
        "data": {
            "type": "score",
            "attributes": {},
            "relationships": {"job-post": {"data": {"type": "job-post", "id": str(job_post_id)}}},
        }
    }
    return await api.post("/api/v1/scores/", payload)


async def get_scores(
    api: ApiClient,
    id: int | None = None,
    job_post_id: int | None = None,
    page: int | None = None,
    per_page: int | None = None,
) -> str:
    """Fetch scores."""
    if id is not None:
        return await api.get(f"/api/v1/scores/{id}/")
    params = {}
    if job_post_id is not None:
        params["filter[job_post_id]"] = job_post_id
    if page is not None:
        params["page"] = page
    if per_page is not None:
        params["per_page"] = per_page
    return await api.get("/api/v1/scores/", params=params)
