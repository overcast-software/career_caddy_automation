"""Career Caddy agent — adds job posts and manages applications via the API.

Uses the CareerCaddyToolset to interact with the Career Caddy REST API.
Handles duplicate detection, company creation, and structured responses.
"""

import logging
import os

from pydantic import BaseModel, Field
from pydantic_ai.usage import UsageLimits

from src.agents.agent_factory import get_agent, get_model, get_model_name, register_defaults
from src.agents.usage_reporter import report_usage
from src.client.models import JobPostData
from src.client.toolset import CareerCaddyDeps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CareerCaddyResponse(BaseModel):
    """Structured response from the Career Caddy agent."""

    summary: str = Field(description="Human-readable summary of what was done or found")
    action_taken: str = Field(
        description="Action performed: 'created', 'duplicate', 'found', 'queried', 'error'"
    )
    job_id: str | None = Field(None, description="ID of the job post (if applicable)")
    company_id: str | None = Field(None, description="ID of the company (if applicable)")
    details: dict | None = Field(None, description="Additional data from the API")


_CAREER_CADDY_SYSTEM_PROMPT = """
    You are a helpful agent to facilitate adding job posts and job applications to the career caddy API.

    ## Workflow for adding a job post
    1. **Check for duplicates** — call `find_job_post_by_link` with the job URL.
       - If a result is returned, it is a duplicate: stop and set action_taken='duplicate'.
       - if the data is an empty set, it means there is no job-post for the given url.
       - Do NOT call `get_job_posts` for duplicate checking — it fetches every post and wastes context.
       - Some sites you visit will obfuscate the employer.  Don't put in any company, if it's unclear use the hostname of the url
    2. **Create the job post** — call `create_job_post_with_company_check` with `company_name`.
       It handles company lookup and creation automatically.
       NEVER call `create_job_post` directly — it requires a valid Career Caddy company_id,
       NOT a job board ID or any number you inferred from scraped data.
    3. **Done** — report the result.

    ## Workflow for recording a job application
    1. Find the job post using `find_job_post_by_link` (preferred) or `get_job_posts` as a last resort.
    2. Call `create_job_application` with the `job_post_id` and `status` (default: "applied").
    3. Done — do NOT retry or call any create_job_post tool after this step.

    ## Workflow for updating an existing job application
    1. Find the job post with `find_job_post_by_link` to get the job_post_id.
    2. Call `get_applications_for_job_post(job_post_id)` to get the application IDs.
    3. Call `update_job_application(application_id=<id>, status=<new_status>, ...)`.

    CRITICAL:
    - Every tool returns JSON with a "success" field. If false, stop immediately.
    - If a tool call fails with ANY error, do NOT retry. Stop and set action_taken='error'.
    - NEVER scan for records by incrementing IDs.
    - A 409 duplicate from create_job_post means the post already exists. Take the `existing_id`
      and use it as `job_post_id` in `create_job_application`.
    - `update_job_application` accepts ONLY: application_id, status, notes, applied_at.

    ## Finishing
    Call `final_result` as soon as you have enough data to answer the request.
    You MUST call the `final_result` tool with these fields:
    - summary: plain-English description of what happened
    - action_taken: one of "created", "duplicate", "found", "queried", "error"
    - job_id: ID of the job post, or null
    - company_id: ID of the company, or null
    - details: object with extra API data, or null

    NEVER output plain text or JSON as your final message. ALWAYS end by calling `final_result`.
    """

register_defaults()


async def parse_and_add_job(
    job_content: str, url: str | None = None, scrape_id: str | None = None
) -> dict:
    """Extract structured job data from raw content then add it to the system."""
    from src.agents.job_extractor import extract_job_from_content

    logger.info(
        "parse_and_add_job: extracting scrape_id=%s url=%s content_len=%s",
        scrape_id,
        url,
        len(job_content),
    )
    try:
        job_data = await extract_job_from_content(job_content, url=url)
    except Exception as e:
        logger.error("parse_and_add_job: extraction failed: %s", e)
        return {"success": False, "error": f"Extraction failed: {e}"}

    logger.info(
        "parse_and_add_job: extracted title=%r company=%r, adding to system",
        job_data.title,
        job_data.company_name,
    )
    return await add_job_post(job_data)


async def add_job_post(
    job_data: JobPostData, api_token: str | None = None, pipeline_run_id: str | None = None
) -> dict:
    """Add a job post to Career Caddy via the agent."""
    logger.info(f"Adding job post: {job_data.title} at {job_data.company_name}")

    prompt = f"""
    Add this job post to the system:

    Job Title: {job_data.title}
    Company: {job_data.company_name}
    Description: {job_data.description}
    URL: {job_data.url}
    Location: {job_data.location}
    Remote OK: {job_data.remote_ok}
    Employment Type: {job_data.employment_type}
    Salary Range: {job_data.salary_min} - {job_data.salary_max}
    Posted Date: {job_data.posted_date}

    Company Details (if needed):
    - Description: {job_data.company_description}
    - Website: {job_data.company_website}
    - Industry: {job_data.company_industry}
    - Size: {job_data.company_size}
    - Location: {job_data.company_location}

    Follow the workflow: check if job exists, find/create company, then create job post.
    """

    try:
        model = get_model("caddy")
        agent = get_agent(
            "caddy",
            output_type=CareerCaddyResponse,
            system_prompt=_CAREER_CADDY_SYSTEM_PROMPT,
        )
        token = api_token or os.environ["CC_API_TOKEN"]
        deps = CareerCaddyDeps(
            api_token=token,
            base_url=os.environ.get("CC_API_BASE_URL", "http://localhost:8000"),
        )
        result = await agent.run(prompt, deps=deps, usage_limits=UsageLimits(request_limit=20))

        await report_usage(
            api_token=token,
            agent_name="career_caddy_agent",
            model_name=get_model_name(model),
            usage=result.usage(),
            trigger="pipeline",
            pipeline_run_id=pipeline_run_id,
        )

        response: CareerCaddyResponse = result.output
        return {
            "success": response.action_taken != "error",
            "output": response.summary,
            "action": response.action_taken,
            "job_id": response.job_id,
            "company_id": response.company_id,
            "details": response.details,
            "usage": str(result.usage),
        }
    except Exception as e:
        logger.error(f"Error adding job post: {e}")
        return {"success": False, "error": str(e)}
