"""Shared Pydantic models for job and company data."""

from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

_ALLOWED_URL_SCHEMES = frozenset({"https", "mailto"})


def _validate_job_url(value: str | None) -> str | None:
    """Reject job URLs that aren't https:// or mailto:.

    A jobPost.url is one of two things: a link to a specific listing
    (https) or a direct-solicitation recruiter address (mailto). Plain
    http and other schemes are rejected at the model boundary so bad
    values can't reach the Career Caddy API.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    scheme = urlparse(value).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"url must use https:// or mailto: (got scheme {scheme!r} in {value!r})"
        )
    return value


class JobPostData(BaseModel):
    """Data model for a job post."""

    title: str = Field(..., min_length=1, max_length=200, description="Job title")
    description: str = Field(..., min_length=10, description="Job description")
    company_name: str = Field(..., min_length=1, description="Company name")
    location: str | None = Field(None, max_length=100, description="Job location")
    salary_min: int | None = Field(None, ge=0, description="Minimum salary")
    salary_max: int | None = Field(None, ge=0, description="Maximum salary")
    employment_type: str | None = Field(
        None, description="Employment type (full-time, part-time, contract, etc.)"
    )
    remote_ok: bool = Field(default=False, description="Whether remote work is allowed")
    link: str | None = Field(None, description="Original job posting link (URL)")
    url: str | None = Field(None, description="Alias for link")

    @model_validator(mode="before")
    @classmethod
    def _sync_url_link(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            if data.get("url") and not data.get("link"):
                data["link"] = data["url"]
        return data

    _validate_link = field_validator("link", "url")(_validate_job_url)

    posted_date: str | None = Field(None, description="When the job was posted (ISO format)")

    # Company details (for creation if needed)
    company_description: str | None = Field(None, description="Company description")
    company_website: str | None = Field(None, description="Company website URL")
    company_industry: str | None = Field(None, max_length=100, description="Company industry")
    company_size: str | None = Field(None, description="Company size")
    company_location: str | None = Field(None, max_length=100, description="Company location")

    def model_post_init(self, __context):
        """Validate salary range if both min and max are provided."""
        if self.salary_min is not None and self.salary_max is not None:
            if self.salary_min > self.salary_max:
                raise ValueError("salary_min cannot be greater than salary_max")


class CompanyData(BaseModel):
    """Data model for a company."""

    name: str = Field(..., min_length=1, max_length=200, description="Company name")
    description: str | None = Field(None, description="Company description")
    website: str | None = Field(None, description="Company website URL")
    industry: str | None = Field(None, max_length=100, description="Company industry")
    size: str | None = Field(None, description="Company size")
    location: str | None = Field(None, max_length=100, description="Company location")
