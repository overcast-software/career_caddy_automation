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
from typing import Literal
from urllib.parse import urljoin

from typing import Optional
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


async def create_job_post_minimal(
    api: ApiClient,
    title: str,
    link: str | None = None,
    description: str | None = None,
    source: str = "email",
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
    """
    attrs: dict = {"title": title, "link": link, "source": source}
    if description:
        attrs["description"] = description
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
    url: Optional[str] = None,
    link: Optional[str] = None,
    posted_date: Optional[str] = None,
    company_description: Optional[str] = None,
    company_website: Optional[str] = None,
    company_industry: Optional[str] = None,
    company_size: Optional[str] = None,
    company_location: Optional[str] = None,
    source: str = "chat",
) -> str:
    """Create a job post, creating the company first if it doesn't exist.

    `source` defaults to "email" because cc_auto's primary caller is the
    email-ingest pipeline; rides through to JobPost.source and the
    JobPostDiscovery row the API auto-creates for the caller.
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
) -> str:
    """Create a scrape record."""
    attributes = {"url": url}
    if status:
        attributes["status"] = status
    relationships = {}
    if job_post_id is not None:
        relationships["job-post"] = {"data": {"type": "job-post", "id": str(job_post_id)}}
    if company_id is not None:
        relationships["company"] = {"data": {"type": "company", "id": str(company_id)}}

    payload: dict = {"data": {"type": "scrape", "attributes": attributes}}
    if relationships:
        payload["data"]["relationships"] = relationships

    return await api.post("/api/v1/scrapes/", payload)


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
