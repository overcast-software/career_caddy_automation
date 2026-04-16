"""Shared Pydantic models for job and company data."""

from typing import Optional
from pydantic import BaseModel, Field, model_validator


class JobPostData(BaseModel):
    """Data model for a job post."""

    title: str = Field(..., min_length=1, max_length=200, description="Job title")
    description: str = Field(..., min_length=10, description="Job description")
    company_name: str = Field(..., min_length=1, description="Company name")
    location: Optional[str] = Field(None, max_length=100, description="Job location")
    salary_min: Optional[int] = Field(None, ge=0, description="Minimum salary")
    salary_max: Optional[int] = Field(None, ge=0, description="Maximum salary")
    employment_type: Optional[str] = Field(
        None, description="Employment type (full-time, part-time, contract, etc.)"
    )
    remote_ok: bool = Field(default=False, description="Whether remote work is allowed")
    link: str = Field(None, description="Original job posting link (URL)")
    url: Optional[str] = Field(None, description="Alias for link")

    @model_validator(mode="before")
    @classmethod
    def _sync_url_link(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            if data.get("url") and not data.get("link"):
                data["link"] = data["url"]
        return data

    posted_date: Optional[str] = Field(
        None, description="When the job was posted (ISO format)"
    )

    # Company details (for creation if needed)
    company_description: Optional[str] = Field(None, description="Company description")
    company_website: Optional[str] = Field(None, description="Company website URL")
    company_industry: Optional[str] = Field(
        None, max_length=100, description="Company industry"
    )
    company_size: Optional[str] = Field(None, description="Company size")
    company_location: Optional[str] = Field(
        None, max_length=100, description="Company location"
    )

    def model_post_init(self, __context):
        """Validate salary range if both min and max are provided."""
        if self.salary_min is not None and self.salary_max is not None:
            if self.salary_min > self.salary_max:
                raise ValueError("salary_min cannot be greater than salary_max")


class CompanyData(BaseModel):
    """Data model for a company."""

    name: str = Field(..., min_length=1, max_length=200, description="Company name")
    description: Optional[str] = Field(None, description="Company description")
    website: Optional[str] = Field(None, description="Company website URL")
    industry: Optional[str] = Field(
        None, max_length=100, description="Company industry"
    )
    size: Optional[str] = Field(None, description="Company size")
    location: Optional[str] = Field(
        None, max_length=100, description="Company location"
    )
