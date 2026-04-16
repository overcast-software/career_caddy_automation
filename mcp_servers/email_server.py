#!/usr/bin/env python3
import logfire
import email
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
from fastmcp import FastMCP, Context
import json
import subprocess
import logging
import math
import html2text
from bs4 import BeautifulSoup

logfire.configure(
    service_name="email_mcp_server",
    console=False,
)

logger = logging.getLogger(__name__)
# Configuration
source = "~/.mail"


def format_email_id_for_notmuch(email_id: str) -> str:
    """Return a notmuch query that matches the given message ID exactly.

    The ID value is always double-quoted so that special characters such as
    '@' are treated as literals by notmuch's Xapian query parser rather than
    being interpreted as operators (which causes a parse failure / no match).
    """
    raw = email_id[3:] if email_id.startswith("id:") else email_id
    return f'id:"{raw}"'


class NotmuchSearchResult(BaseModel):
    """Notmuch search result item"""

    thread: str
    timestamp: int
    date_relative: str
    matched: int
    total: int
    authors: str
    subject: str
    query: Optional[List[Optional[str]]] = None
    tags: List[str]

    @property
    def email_id(self) -> str:
        """Extract email ID from query array (first element) and strip id: prefix"""
        if self.query and len(self.query) > 0 and self.query[0]:
            email_id = self.query[0]
            # Strip "id:" prefix if present
            if email_id.startswith("id:"):
                return email_id[3:]
            return email_id
        return ""


class EmailHeader(BaseModel):
    """Simplified email header information for internal use"""

    message_id: Optional[str] = None
    from_address: str
    to_address: str
    subject: str
    date: Optional[datetime] = None
    return_path: Optional[str] = None
    spam_status: Optional[str] = None


class EmailContent(BaseModel):
    """Email content analysis"""

    plain_text: str = ""
    html_content: str = ""
    is_multipart: bool = False


class ParsedEmail(BaseModel):
    """Complete parsed email structure"""

    file_path: str
    headers: EmailHeader
    content: EmailContent
    raw_size: int


class SearchEmailArgs(BaseModel):
    """Arguments for email search operations"""

    query: str = Field(
        default="*",
        description="Notmuch search query. Use '*' for all emails, 'from:sender@example.com' for specific sender, 'subject:keyword' for subject search, etc.",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of results to return (1-100)",
    )
    days_back: int = Field(
        default=7, ge=1, le=365, description="Number of days to search back (1-365)"
    )


class TagEmailArgs(BaseModel):
    """Arguments for tagging operations"""

    email_id: str = Field(description="The email ID to tag (from search results)")
    tags: List[str] = Field(
        min_length=1,
        description="List of tags to add/remove (e.g., ['evaluated', 'job-posting'])",
    )


class ReadEmailArgs(BaseModel):
    """Arguments for reading email content"""

    email_id: str = Field(description="The email ID to read (from search results)")
    max_content_length: int = Field(
        default=4000,
        ge=100,
        le=50000,
        description="Maximum characters for email content (100-50000)",
    )


class SearchByTagArgs(BaseModel):
    """Arguments for searching by tags"""

    tags: List[str] = Field(
        min_length=1, description="List of tags that ALL must be present (AND logic)"
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of results to return (1-100)",
    )
    days_back: int = Field(
        default=7, ge=1, le=365, description="Number of days to search back (1-365)"
    )


class MailReader:
    """Mail reader service for analyzing emails"""

    def __init__(
        self,
        mail_directory: str = "~/.mail",
        default_limit: int = 50,
        default_days_back: int = 7,
    ):
        self.mail_directory = Path(mail_directory).expanduser()
        self.default_limit = default_limit
        self.default_days_back = default_days_back

    def search_email(
        self, query: str, limit: int = None, days_back: int = None
    ) -> List[str]:
        """Search for emails and return email IDs"""
        if limit is None:
            limit = self.default_limit
        if days_back is None:
            days_back = self.default_days_back

        try:
            date_range = _build_date_range_query(days_back)
            full_query = date_range if query == "*" else f"({query}) AND {date_range}"
            results = _run_notmuch_search(full_query, limit)
            return [sr.email_id for sr in results if sr.email_id]
        except Exception as e:
            logfire.warning(f"Error searching emails: {e}")
            return []

    def _extract_content_from_message(self, msg, plain_text="", html_content=""):
        """Recursively extract text and HTML content from a message, including nested/forwarded messages"""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()

                # Handle nested message/rfc822 (forwarded emails)
                if content_type == "message/rfc822":
                    # Get the nested message
                    payload = part.get_payload()
                    if payload and isinstance(payload, list) and len(payload) > 0:
                        nested_msg = payload[0]
                        # Recursively extract content from nested message
                        plain_text, html_content = self._extract_content_from_message(
                            nested_msg, plain_text, html_content
                        )
                elif content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        try:
                            plain_text += (
                                payload.decode("utf-8", errors="ignore") + "\n"
                            )
                        except Exception as e:
                            logfire.warning(f"Error decoding plain text: {e}")
                elif content_type == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        try:
                            html_content += (
                                payload.decode("utf-8", errors="ignore") + "\n"
                            )
                        except Exception as e:
                            logfire.warning(f"Error decoding HTML content: {e}")
        else:
            # Single part message
            content_type = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                try:
                    decoded_content = payload.decode("utf-8", errors="ignore")
                    if content_type == "text/html":
                        html_content += decoded_content + "\n"
                    else:
                        plain_text += decoded_content + "\n"
                except Exception as e:
                    logfire.warning(f"Error decoding single part content: {e}")

        return plain_text, html_content

    def parse_email(self, email_id: str) -> Optional[ParsedEmail]:
        """Parse a single email using notmuch show --format=raw + Python's email package"""
        try:
            # Use notmuch show --format=raw to get raw email content
            result = subprocess.run(
                [
                    "notmuch",
                    "show",
                    "--format=raw",
                    format_email_id_for_notmuch(email_id),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                logfire.warning(
                    f"notmuch show --format=raw failed for id {email_id}: {result.stderr}"
                )
                return None

            # Parse the raw email content with Python's email package
            msg = email.message_from_string(result.stdout)
            return self._build_parsed_email(msg, email_id, raw_size=len(result.stdout))

        except Exception as e:
            logfire.warning(f"Error parsing email with id {email_id}: {e}")
            return None

    def parse_email_from_file(self, filepath: str) -> Optional[ParsedEmail]:
        """Parse an email file directly by filepath using Python's email package"""
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                raw = f.read()
            msg = email.message_from_string(raw)
            return self._build_parsed_email(msg, filepath, raw_size=len(raw))
        except FileNotFoundError:
            logfire.warning(f"Email file not found: {filepath}")
            return None
        except Exception as e:
            logfire.warning(f"Error parsing email file {filepath}: {e}")
            return None

    def parse_notmuch_search_results(
        self, json_output: str
    ) -> List[NotmuchSearchResult]:
        """Parse notmuch search JSON output into Pydantic models"""
        try:
            raw_results = json.loads(json_output) if json_output.strip() else []
            search_results = []

            for result_data in raw_results:
                try:
                    search_result = NotmuchSearchResult(**result_data)
                    search_results.append(search_result)
                except Exception as e:
                    logfire.warning(f"Failed to parse search result: {e}")
                    # Continue with other results
                    continue

            return search_results
        except json.JSONDecodeError as e:
            logfire.warning(f"Failed to parse notmuch search JSON: {e}")
            return []

    def _build_parsed_email(self, msg, file_path: str, raw_size: int) -> "ParsedEmail":
        """Build a ParsedEmail from an already-parsed email.Message object."""
        headers = EmailHeader(
            message_id=msg.get("Message-ID"),
            from_address=msg.get("From", ""),
            to_address=msg.get("To", ""),
            subject=msg.get("Subject", ""),
            return_path=msg.get("Return-Path"),
            spam_status=msg.get("X-Spam-Status"),
        )
        date_str = msg.get("Date")
        if date_str:
            try:
                headers.date = email.utils.parsedate_to_datetime(date_str)
            except Exception:
                pass
        plain_text, html_content = self._extract_content_from_message(msg)
        content = EmailContent(
            plain_text=plain_text.strip(),
            html_content=html_content.strip(),
            is_multipart=msg.is_multipart(),
        )
        return ParsedEmail(file_path=file_path, headers=headers, content=content, raw_size=raw_size)


# Initialize mail reader with defaults
mail_reader = MailReader(source, default_limit=50, default_days_back=7)

# MCP Server setup using FastMCP
server = FastMCP("email-server")


# ---------------------------------------------------------------------------
# Private module-level helpers (shared by all search tools)
# ---------------------------------------------------------------------------


def _build_date_range_query(days_back: float) -> str:
    """Return a notmuch date range clause for the last N days."""
    days = max(1, math.ceil(days_back))
    end = datetime.now()
    start = end - timedelta(days=days)
    return f"date:{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}"


def _run_notmuch_search(query: str, limit: int) -> List[NotmuchSearchResult]:
    """Run notmuch search and return parsed results.

    Raises RuntimeError if notmuch exits non-zero.
    """
    result = subprocess.run(
        ["notmuch", "search", "--format=json", f"--limit={limit}", query],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"notmuch search failed: {result.stderr.strip()}")
    return mail_reader.parse_notmuch_search_results(result.stdout)


def _format_search_results(
    results: List[NotmuchSearchResult],
    query: str,
    title: str,
    empty_msg: str = "*No emails found.*",
    footer: str = "*Use `read_email(email_id)` to get full content of specific emails.*\n",
) -> str:
    """Format a list of NotmuchSearchResult objects as a markdown string."""
    out = f"# {title}\n\n**Query:** `{query}`\n**Total Found:** {len(results)}\n\n"
    if not results:
        return out + empty_msg + "\n" + footer
    out += "---\n\n"
    for i, sr in enumerate(results, 1):
        out += f"## {i}. {sr.subject}\n\n"
        out += f"- **Email ID:** `{sr.email_id}`\n"
        out += f"- **From:** {sr.authors}\n"
        out += f"- **Date:** {sr.date_relative}\n"
        out += f"- **Tags:** {', '.join(sr.tags) if sr.tags else 'none'}\n"
        out += f"- **Thread:** `{sr.thread}`\n"
        out += f"- **Matched/Total:** {sr.matched}/{sr.total}\n\n---\n\n"
    return out + footer


def _parse_email_with_content(
    email_id: str, max_content_length: int = 4000
) -> Optional[Dict[str, Any]]:
    """Helper function to parse an email and convert all content to markdown.

    Args:
        email_id: The email ID to parse
        max_content_length: Maximum length for markdown content (default: 4000 chars)

    Returns a dict with markdown content and headers, or None if parsing fails.
    """
    parsed_email = mail_reader.parse_email(email_id)
    if not parsed_email:
        return None

    # Prefer HTML content if available, otherwise use plain text
    markdown_content = ""
    content_source = "none"

    if parsed_email.content.html_content:
        # Convert HTML to markdown using html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = False
        h.body_width = 0
        h.unicode_snob = True
        h.ignore_emphasis = False

        try:
            # First clean up the HTML with BeautifulSoup
            soup = BeautifulSoup(parsed_email.content.html_content, "html.parser")

            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()

            # Convert to markdown
            markdown_content = h.handle(str(soup))
            content_source = "html"
        except Exception as e:
            logfire.warning(f"Error converting HTML to markdown: {e}")
            # Fallback to plain text
            markdown_content = parsed_email.content.plain_text
            content_source = "plain_text_fallback"
    elif parsed_email.content.plain_text:
        # Use plain text directly as markdown
        markdown_content = parsed_email.content.plain_text
        content_source = "plain_text"

    original_length = len(markdown_content)

    # Log content sizes
    logfire.info(
        f"Email {email_id} content",
        email_id=email_id,
        content_source=content_source,
        original_length=original_length,
    )

    # Truncate if needed
    truncated = False
    if original_length > max_content_length:
        markdown_content = (
            markdown_content[:max_content_length]
            + f"\n\n[... truncated {original_length - max_content_length} characters]"
        )
        truncated = True
        logfire.info(
            f"Truncated content for email {email_id} from {original_length} to {max_content_length} chars"
        )

    return {
        "content": {
            "markdown": markdown_content,
            "content_source": content_source,
            "truncated": truncated,
            "original_length": original_length,
        },
        "headers": {
            "from": parsed_email.headers.from_address,
            "to": parsed_email.headers.to_address,
            "subject": parsed_email.headers.subject,
            "date": (
                parsed_email.headers.date.isoformat()
                if parsed_email.headers.date
                else None
            ),
            "message_id": parsed_email.headers.message_id,
        },
    }


@server.tool()
async def list_emails(query: str = "*", limit: int = 50, days_back: float = 7) -> str:
    """List emails matching a search query with metadata only.

    **Purpose**: Get a quick overview of emails without loading full content. This is
    the primary tool for discovering emails in the mailbox.

    **When to use**:
    - Starting point for any email workflow
    - Getting a list of recent emails
    - Finding emails by sender, subject, or other criteria

    **Workflow**:
    1. Use this to get email_id values
    2. Use read_email(email_id) to get full content of interesting emails
    3. Use tag_email() to organize or mark emails

    **Query syntax** (notmuch format):
    - "*" = all emails
    - "from:sender@example.com" = emails from specific sender
    - "subject:invoice" = emails with "invoice" in subject
    - "to:me@example.com" = emails sent to you
    - Combine with AND/OR: "from:boss@company.com AND subject:urgent"

    Args:
        limit: Maximum number of results (default: 50)
        query: Notmuch search query (default: "*" for all emails)
        days_back: How many days back to search (default: 7)

    Returns:
        JSON with email metadata including email_id, subject, authors, tags, dates.
        Does NOT include full email content - use read_email() for that.
    """
    date_range = _build_date_range_query(days_back)
    full_query = date_range if query == "*" else f"({query}) AND {date_range}"
    logfire.info(f"notmuch search {full_query!r} limit={limit}")
    try:
        results = _run_notmuch_search(full_query, limit)
        logfire.info(f"list_emails: {len(results)} results")
        return _format_search_results(results, full_query, "Email Search Results",
                                      empty_msg="*No emails found matching the query.*")
    except RuntimeError as e:
        return f"notmuch command failed: {e}"
    except Exception as e:
        return f"Error running notmuch command: {str(e)}"


@server.tool()
async def read_email(
    ctx: Context, email_id: str, max_content_length: int = 4000
) -> str:
    """Read the full content of a specific email by email ID.

    **Purpose**: Retrieve complete email content including body text and headers.
    HTML emails are automatically converted to clean markdown format for better
    readability and analysis.

    **When to use**:
    - After finding interesting emails with list_emails() or search_email()
    - When you need to analyze email content (extract URLs, job postings, etc.)
    - Before making decisions about tagging or categorizing emails

    **Content handling**:
    - HTML emails → converted to markdown (preserves links, formatting)
    - Plain text emails → returned as-is
    - Nested/forwarded emails → content extracted recursively
    - Long emails → automatically truncated with notice

    **Workflow example**:
    1. search_unevaluated_emails() → get email_id
    2. read_email(email_id) → analyze content
    3. tag_email(email_id, ["evaluated", "job-posting"]) → categorize

    Args:
        email_id: The email ID from list_emails/search_email (e.g., "abc123@example.com")
        max_content_length: Maximum chars for content (default: 4000, prevents token overflow)

    Returns:
        JSON with:
        - content.markdown: Email body in markdown format
        - content.content_source: "html", "plain_text", or "plain_text_fallback"
        - content.truncated: Boolean indicating if content was cut off
        - headers: from, to, subject, date, message_id
    """
    try:
        parsed_data = _parse_email_with_content(
            email_id, max_content_length=max_content_length
        )
        if not parsed_data:
            return f"Error: Could not parse email with ID {email_id}"

        combined_result = {
            "email_id": email_id,
            "success": True,
            "content": parsed_data["content"],
            "headers": parsed_data["headers"],
        }

        # Log the final JSON size
        result_json = json.dumps(combined_result, indent=2, default=str)
        logfire.info(
            f"read_email result",
            email_id=email_id,
            result_length=len(result_json),
            content_source=parsed_data["content"]["content_source"],
            truncated=parsed_data["content"]["truncated"],
        )

        return result_json

    except Exception as e:
        return f"Error reading email: {str(e)}"


@server.tool()
async def tag_email(ctx: Context, email_id: str, tags: List[str]) -> str:
    """Add one or more tags to an email for organization and tracking.

    **Purpose**: Organize, categorize, and track email processing status using tags.
    Tags are persistent and can be used for filtering and workflow management.

    **When to use**:
    - Mark emails as processed: ["evaluated"]
    - Categorize content: ["job-posting", "newsletter", "receipt"]
    - Set priority: ["important", "urgent", "low-priority"]
    - Track workflow: ["todo", "waiting-response", "archived"]
    - Flag for follow-up: ["needs-reply", "action-required"]

    **Common tag patterns**:
    - "evaluated" = email has been reviewed/processed
    - "job-posting" = contains job opportunity
    - "important" = high priority
    - "todo" = requires action
    - "archived" = processed and filed away

    **Workflow example**:
    1. read_email(email_id) → analyze content
    2. Determine category/status
    3. tag_email(email_id, ["evaluated", "job-posting"]) → mark it
    4. Later: search_by_tag(["job-posting"]) → find all job emails

    Args:
        email_id: The email ID to tag (from list_emails/search_email)
        tags: List of tags to add (e.g., ["important", "todo", "evaluated"])

    Returns:
        JSON with success status, email_id, and tags_added list

    Note: Tags are additive - existing tags are preserved. Use untag_email() to remove.
    """
    try:
        # Format tags with + prefix for adding
        tag_args = [f"+{tag}" for tag in tags]

        result = subprocess.run(
            ["notmuch", "tag"]
            + tag_args
            + ["--", format_email_id_for_notmuch(email_id)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            return json.dumps(
                {
                    "success": True,
                    "email_id": email_id,
                    "tags_added": tags,
                    "message": f"Successfully added tags: {', '.join(tags)}",
                },
                indent=2,
            )
        else:
            return json.dumps(
                {"success": False, "error": f"notmuch tag failed: {result.stderr}"},
                indent=2,
            )

    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"Error tagging email: {str(e)}"}, indent=2
        )


@server.tool()
async def untag_email(ctx: Context, email_id: str, tags: List[str]) -> str:
    """Remove one or more tags from an email.

    **Purpose**: Remove tags that are no longer relevant or were added by mistake.
    Useful for correcting categorization or updating email status.

    **When to use**:
    - Correct mis-categorization: remove wrong tags
    - Update status: remove "unread" after reading
    - Clean up: remove "todo" after completing action
    - Reset processing: remove "evaluated" to reprocess

    **Common use cases**:
    - Remove "unread" after processing
    - Remove "todo" when task is complete
    - Remove "spam" if email was misclassified
    - Remove "evaluated" to trigger reprocessing

    Args:
        email_id: The email ID to untag (from list_emails/search_email)
        tags: List of tags to remove (e.g., ["spam", "unread", "todo"])

    Returns:
        JSON with success status, email_id, and tags_removed list

    Note: Only removes specified tags - other tags remain unchanged.
    """
    try:
        # Format tags with - prefix for removing
        tag_args = [f"-{tag}" for tag in tags]

        result = subprocess.run(
            ["notmuch", "tag"]
            + tag_args
            + ["--", format_email_id_for_notmuch(email_id)],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            return json.dumps(
                {
                    "success": True,
                    "email_id": email_id,
                    "tags_removed": tags,
                    "message": f"Successfully removed tags: {', '.join(tags)}",
                },
                indent=2,
            )
        else:
            return json.dumps(
                {"success": False, "error": f"notmuch tag failed: {result.stderr}"},
                indent=2,
            )

    except Exception as e:
        return json.dumps(
            {"success": False, "error": f"Error untagging email: {str(e)}"}, indent=2
        )


@server.tool()
async def search_by_tag(
    ctx: Context, tags: List[str], limit: int = 20, days_back: float = 7
) -> str:
    """Find emails that have ALL of the specified tags (AND logic).

    **Purpose**: Filter emails by tags to find specific categories or workflow states.
    This is the primary tool for retrieving previously categorized emails.

    **When to use**:
    - Find all job postings: ["job-posting"]
    - Find urgent unread emails: ["important", "unread"]
    - Find emails needing action: ["todo", "evaluated"]
    - Review processed emails: ["evaluated", "archived"]
    - Find specific categories: ["receipt", "invoice"]

    **Logic**: ALL tags must be present (AND operation)
    - search_by_tag(["important", "unread"]) → emails with BOTH tags
    - To find emails with ANY tag, call this multiple times

    **Workflow examples**:
    1. Find job opportunities: search_by_tag(["job-posting", "evaluated"])
    2. Find pending tasks: search_by_tag(["todo"])
    3. Review important emails: search_by_tag(["important"])

    Args:
        tags: List of tags that ALL must be present (e.g., ["important", "unread"])
        limit: Maximum number of results (default: 20)
        days_back: Number of days to search back (default: 7)

    Returns:
        JSON with email metadata (email_id, subject, authors, tags, dates).
        Does NOT include full content - use read_email(email_id) for that.

    Note: Returns metadata only. Use read_email() to get full content of specific emails.
    """
    try:
        date_range = _build_date_range_query(days_back)
        tag_query = " AND ".join(f"tag:{tag}" for tag in tags)
        full_query = f"({tag_query}) AND {date_range}"
        results = _run_notmuch_search(full_query, limit)
        return _format_search_results(results, full_query,
                                      f"Emails with Tags: {', '.join(tags)}",
                                      empty_msg="*No emails found with the specified tags.*")
    except RuntimeError as e:
        return json.dumps({"success": False, "error": str(e)}, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": f"Error searching by tag: {str(e)}"}, indent=2)


@server.tool()
async def search_without_tag(
    ctx: Context, tags: List[str], limit: int = 20, days_back: float = 1
) -> str:
    """Find emails that do NOT have any of the specified tags (NOT logic).

    **Purpose**: Find emails that haven't been categorized or processed yet.
    Useful for discovering new emails or finding emails that need attention.

    **When to use**:
    - Find unprocessed emails: search_without_tag(["evaluated"])
    - Find emails not marked as spam: search_without_tag(["spam", "deleted"])
    - Find emails without category: search_without_tag(["job-posting", "newsletter"])
    - Find emails needing review: search_without_tag(["archived"])

    **Logic**: Emails must NOT have ANY of the specified tags (NOT operation)
    - search_without_tag(["spam", "deleted"]) → emails without spam OR deleted tags
    - Opposite of search_by_tag()

    **Common workflows**:
    1. Find new emails: search_without_tag(["evaluated"])
    2. Find unarchived: search_without_tag(["archived"])
    3. Find non-spam: search_without_tag(["spam"])

    Args:
        tags: List of tags that must NOT be present (e.g., ["spam", "deleted"])
        limit: Maximum number of results (default: 20)
        days_back: Number of days to search back (default: 1, recent emails only)

    Returns:
        JSON with email metadata (email_id, subject, authors, tags, dates).
        Does NOT include full content - use read_email(email_id) for that.

    Note: Default days_back=1 focuses on recent emails. Increase for broader search.
    """
    try:
        date_range = _build_date_range_query(days_back)
        tag_query = " AND ".join(f"NOT tag:{tag}" for tag in tags)
        full_query = f"({tag_query}) AND {date_range}"
        results = _run_notmuch_search(full_query, limit)
        return _format_search_results(results, full_query,
                                      f"Emails WITHOUT Tags: {', '.join(tags)}",
                                      empty_msg="*No emails found without the specified tags.*")
    except RuntimeError as e:
        return json.dumps({"success": False, "error": str(e)}, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": f"Error searching without tag: {str(e)}"}, indent=2)


@server.tool()
async def search_unevaluated_emails(
    ctx: Context, query: str = "*", limit: int = 20, days_back: float = 7
) -> str:
    """Find emails that haven't been processed yet (missing 'evaluated' tag).

    **Purpose**: This is the PRIMARY ENTRY POINT for email processing workflows.
    Use this to discover new emails that need review, classification, or action.

    **When to use**:
    - Starting any email processing workflow
    - Finding new emails to analyze
    - Discovering unprocessed job postings
    - Identifying emails needing categorization
    - Daily email review routine

    **Why this tool exists**:
    Convenience wrapper around search_without_tag(["evaluated"]) with better defaults
    for typical email processing workflows. Saves you from manually excluding evaluated emails.

    **Standard workflow**:
    1. search_unevaluated_emails() → get list of unprocessed emails
    2. For each email_id:
       a. read_email(email_id) → get full content
       b. Analyze content (extract job info, categorize, etc.)
       c. tag_email(email_id, ["evaluated", "category"]) → mark as processed
    3. Repeat until all emails are evaluated

    **Query examples**:
    - "*" (default) = all unevaluated emails
    - "from:recruiter@company.com" = unevaluated emails from specific sender
    - "subject:job" = unevaluated emails with "job" in subject

    Args:
        query: Notmuch search query (default: "*" for all unevaluated emails)
        limit: Maximum number of results (default: 20)
        days_back: Number of days to search back (default: 7)

    Returns:
        JSON with email metadata (email_id, subject, authors, tags, dates).
        Does NOT include full content - use read_email(email_id) for that.

    Note: After processing, ALWAYS tag emails with "evaluated" to prevent reprocessing.
    """
    try:
        date_range = _build_date_range_query(days_back)
        full_query = (f"NOT tag:evaluated AND {date_range}" if query == "*"
                      else f"({query}) AND NOT tag:evaluated AND {date_range}")
        results = _run_notmuch_search(full_query, limit)
        return _format_search_results(
            results, full_query, "Unevaluated Emails",
            empty_msg="*No unevaluated emails found. All emails have been processed!*",
            footer='*These emails have NOT been evaluated yet. Use `read_email(email_id)` to get full content, then tag with `tag_email(email_id, ["evaluated", ...])`*\n',
        )
    except RuntimeError as e:
        return f"notmuch search failed: {e}"
    except Exception as e:
        return f"Error searching unevaluated emails: {str(e)}"


@server.tool()
async def search_email(
    ctx: Context, query: str = "*", limit: int = 20, days_back: float = 7
) -> str:
    """Search for emails using flexible notmuch query syntax.

    **Purpose**: General-purpose email search with full notmuch query support.
    Use this when you need more control than the specialized search tools provide.

    **When to use**:
    - Complex searches combining multiple criteria
    - Finding emails by specific sender, subject, or content
    - Date-based searches beyond simple days_back
    - Advanced filtering with AND/OR/NOT logic

    **CRITICAL PARAMETER RULES**:
    1. query: MUST be a valid notmuch query string (see examples below)
    2. limit: MUST be an integer between 1-100 (default: 20)
    3. days_back: MUST be a positive number >= 1 (default: 7)
       - Fractional days are rounded UP to nearest integer
       - "today" or "last few hours" = days_back=1
       - "last week" = days_back=7
       - "last month" = days_back=30

    **Query syntax** (notmuch search format):
    IMPORTANT: The query parameter is a STRING, not a dict/object!

    Valid query examples:
    - "*" = all emails (default)
    - "from:sender@example.com" = emails from specific sender
    - "to:recipient@example.com" = emails to specific recipient
    - "subject:keyword" = emails with keyword in subject
    - "body:text" = emails containing text in body
    - "tag:important" = emails with specific tag
    - "date:2024-01-01..2024-12-31" = date range
    - "from:boss@company.com AND subject:urgent" = combine criteria
    - "subject:meeting NOT from:spam@example.com" = exclude criteria

    **Common query patterns**:
    - Job emails: "subject:job OR subject:opportunity OR subject:position"
    - From recruiter: "from:recruiter@company.com"
    - Urgent unread: "tag:important AND tag:unread"
    - Recent invoices: "subject:invoice"

    **WRONG - DO NOT DO THIS**:
    ❌ query={"from": "sender@example.com"}  # query is NOT a dict!
    ❌ query=["from:sender"]  # query is NOT a list!
    ❌ limit="20"  # limit must be integer, not string!
    ❌ days_back="7"  # days_back must be number, not string!

    **CORRECT - DO THIS**:
    ✅ query="from:sender@example.com", limit=20, days_back=7
    ✅ query="subject:job OR subject:opportunity", limit=50, days_back=14
    ✅ query="*", limit=10, days_back=1

    **Comparison with other tools**:
    - list_emails() = simpler, good for basic queries
    - search_by_tag() = filter by tags only
    - search_unevaluated_emails() = specifically for unprocessed emails
    - search_email() = most flexible, full query power

    Args:
        query: Notmuch search query STRING (default: "*" for all emails)
               Examples: "from:user@example.com", "subject:meeting", "*"
        limit: Maximum results INTEGER, 1-100 (default: 20)
        days_back: Days to search back NUMBER, >= 1 (default: 7)
                   Examples: 1 (today), 7 (week), 30 (month)

    Returns:
        JSON with email metadata (email_id, subject, authors, tags, dates).
        Does NOT include full content - use read_email(email_id) for that.

    Note: For simple "find unevaluated emails" use search_unevaluated_emails() instead.
    """
    try:
        date_range = _build_date_range_query(days_back)
        full_query = date_range if query == "*" else f"({query}) AND {date_range}"
        results = _run_notmuch_search(full_query, limit)
        return _format_search_results(results, full_query, "Email Search Results",
                                      empty_msg="*No emails found matching the query.*")
    except RuntimeError as e:
        return f"notmuch search failed: {e}"
    except Exception as e:
        return f"Error searching emails: {str(e)}"


@server.tool()
async def reindex_email() -> str:
    """Reindex the mail directory to discover newly arrived emails.

    **Purpose**: Update the notmuch database to include emails that have arrived
    since the last indexing. This makes new emails searchable and accessible.

    **When to use**:
    - At the start of an email processing session
    - When you know new emails have arrived but they're not showing up
    - After external mail sync (mbsync, offlineimap, etc.)
    - Before running search_unevaluated_emails() to ensure you see all new emails
    - Periodically during long-running email processing workflows

    **What it does**:
    - Scans mail directory for new email files
    - Adds new emails to notmuch database
    - Updates existing email tags/metadata
    - Removes deleted emails from database

    **Typical workflow**:
    1. reindex_email() → pick up new emails
    2. search_unevaluated_emails() → find unprocessed emails
    3. Process emails...
    4. (Optional) reindex_email() again if more emails arrived

    **Performance note**:
    - Usually completes in seconds
    - May take longer with large mailboxes
    - Safe to call frequently
    - Timeout set to 120 seconds

    Returns:
        JSON with:
        - success: Boolean indicating if reindex completed
        - output: Notmuch output showing what was indexed
        - error: Error message if reindex failed

    Note: This does NOT fetch new emails from server - it only indexes emails
    already present in the mail directory. Use external tools (mbsync, etc.) to
    fetch emails first.
    """
    try:
        result = subprocess.run(
            ["notmuch", "new"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode == 0:
            return json.dumps(
                {
                    "success": True,
                    "message": "Reindex complete",
                    "output": result.stdout.strip(),
                },
                indent=2,
            )
        else:
            return json.dumps(
                {
                    "success": False,
                    "error": result.stderr.strip() or result.stdout.strip(),
                },
                indent=2,
            )

    except subprocess.TimeoutExpired:
        return json.dumps(
            {
                "success": False,
                "error": "notmuch new timed out after 120 seconds",
            },
            indent=2,
        )
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": f"Error running notmuch new: {str(e)}",
            },
            indent=2,
        )


if __name__ == "__main__":
    server.run()
