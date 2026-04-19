"""Agent factory — create configured pydantic-ai Agent instances.

Includes Ollama support for local LLM use. When pydanticai_ollama is installed
and Ollama is running locally, agents can use local models via env var overrides.

Model resolution order: role-specific env var → CADDY_DEFAULT_MODEL → openai:gpt-4o-mini.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Any, Callable

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponseStreamEvent
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RequestUsage
from pydantic_ai._utils import PeekableAsyncStream

from src.agents.history import sanitize_orphaned_tool_calls
from src.client.toolset import CareerCaddyToolset, CareerCaddyDeps


# ---------------------------------------------------------------------------
# Ollama support — only available when pydanticai_ollama is installed
# ---------------------------------------------------------------------------

try:
    from pydanticai_ollama.models.ollama import OllamaModel, OllamaStreamedResponse
    from pydanticai_ollama.providers.ollama import OllamaProvider
    from pydanticai_ollama.settings.ollama import OllamaModelSettings
    _HAS_OLLAMA = True
except ImportError:
    _HAS_OLLAMA = False
    OllamaModel = None
    OllamaStreamedResponse = None
    OllamaProvider = None
    OllamaModelSettings = None

if _HAS_OLLAMA:
    class ConcreteOllamaProvider(OllamaProvider):
        """Concrete implementation of OllamaProvider with provider_url method."""

        def provider_url(self) -> str:
            return self.base_url

    @dataclass
    class ConcreteOllamaStreamedResponse(OllamaStreamedResponse):
        """Concrete implementation of OllamaStreamedResponse."""

        _model_name: str
        _model_profile: ModelProfile
        _response: PeekableAsyncStream[Any]
        _timestamp: datetime

        @property
        def model_name(self) -> str:
            return self._model_name

        @property
        def provider_name(self) -> str | None:
            return "ollama"

        @property
        def timestamp(self) -> datetime:
            return self._timestamp

        def provider_url(self) -> str:
            return os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")

        async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
            async for chunk in self._response:
                self._usage += RequestUsage(input_tokens=0, output_tokens=1)
                if hasattr(chunk, "message") and chunk.message.content:
                    text_event = self._parts_manager.handle_text_delta(
                        vendor_part_id="content",
                        content=chunk.message.content,
                        thinking_tags=self._model_profile.thinking_tags,
                        ignore_leading_whitespace=self._model_profile.ignore_streamed_leading_whitespace,
                    )
                    if text_event:
                        yield text_event

    class ConcreteOllamaModel(OllamaModel):
        """Concrete OllamaModel that returns ConcreteOllamaStreamedResponse."""

        async def _process_streamed_response(
            self,
            response: AsyncIterator[Any],
            model_request_parameters: ModelRequestParameters,
        ) -> ConcreteOllamaStreamedResponse:
            peekable_response = PeekableAsyncStream(response)
            await peekable_response.peek()

            return ConcreteOllamaStreamedResponse(
                model_request_parameters=model_request_parameters,
                _response=peekable_response,
                _model_name=self._model_name,
                _model_profile=self.profile,
                _timestamp=datetime.now(timezone.utc),
            )

    ollama_settings = OllamaModelSettings(
        temperature=0.1,
        num_predict=1024,
        num_ctx=16384,
        repeat_penalty=1.1,
        num_gpu=1,
        top_k=40,
        top_p=0.9,
    )

    _ollama_base = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
    ollama_provider = ConcreteOllamaProvider(base_url=_ollama_base)

    # Pre-configured Ollama models — use these by assigning to env vars, e.g.:
    #   CADDY_MODEL=ollama:qwen3:4b-instruct
    # Or reference the model objects directly in custom code.
    voytas26_openclaw_oss_model = ConcreteOllamaModel(
        model_name="voytas26/openclaw-oss-20b-deterministic",
        provider=ollama_provider,
    )
    phi3_model = ConcreteOllamaModel(
        model_name="phi3:14b",
        provider=ollama_provider,
    )
    astrail3_model = ConcreteOllamaModel(
        model_name="60MPH/astral3-tools:12b",
        provider=ollama_provider,
    )
    llama3_model = ConcreteOllamaModel(
        model_name="llama3.3",
        provider=ollama_provider,
    )

    # Tool-capable models via Ollama's OpenAI-compatible endpoint (/v1)
    _ollama_openai_provider = OpenAIProvider(
        base_url=f"{_ollama_base}/v1",
        api_key="ollama",
    )
    qwen3_4b_model = OpenAIChatModel(
        "qwen3:4b-instruct",
        provider=_ollama_openai_provider,
    )
    qwen25_coder_7b_model = OpenAIChatModel(
        "qwen2.5-coder:7b",
        provider=_ollama_openai_provider,
    )

    # Browser-optimised: larger context + low temperature for reliable tool sequencing
    browser_ollama_model = OpenAIChatModel(
        "qwen3:4b-instruct",
        provider=_ollama_openai_provider,
        settings={
            "temperature": 0.1,
            "extra_body": {
                "options": {
                    "num_ctx": 16384,
                    "num_predict": 1024,
                    "repeat_penalty": 1.1,
                }
            },
        },
    )


_DEFAULT_MODEL = "openai:gpt-4o-mini"

# ---------------------------------------------------------------------------
# Per-agent model overrides via environment variables.
#   CADDY_MODEL            — career_caddy_agent (main agent + add_job_post)
#   EMAIL_CLASSIFIER_MODEL — email_classifier_agent
#   JOB_EXTRACTOR_MODEL    — job_extractor_agent
#   PIPELINE_MODEL         — pipeline agents (email search)
#   BROWSER_SCRAPER_MODEL  — browser scraper agent
#   CADDY_DEFAULT_MODEL    — fallback for all agents not individually overridden
# ---------------------------------------------------------------------------

_ROLE_ENV_MAP = {
    "caddy": "CADDY_MODEL",
    "chat": "CHAT_MODEL",
    "email_classifier": "EMAIL_CLASSIFIER_MODEL",
    "job_extractor": "JOB_EXTRACTOR_MODEL",
    "pipeline": "PIPELINE_MODEL",
    "browser_scraper": "BROWSER_SCRAPER_MODEL",
}


def get_model(role: str | None = None) -> str:
    """Return the model string for a given agent role."""
    if role and role in _ROLE_ENV_MAP:
        val = os.environ.get(_ROLE_ENV_MAP[role])
        if val:
            return val
    return os.environ.get("CADDY_DEFAULT_MODEL", _DEFAULT_MODEL)


def resolve_model(spec: str):
    """Convert a model spec string to whatever pydantic-ai needs.

    - "ollama:<name>"         → OpenAIChatModel routed to Ollama's /v1 endpoint
                                (tool-calling works via the OpenAI-compat API)
    - any other provider spec ("openai:...", "anthropic:...", etc.) passes
      through as a plain string that pydantic-ai resolves itself.
    """
    if spec.startswith("ollama:"):
        if not _HAS_OLLAMA:
            raise RuntimeError(
                "ollama:* model requested but pydanticai_ollama is not installed "
                "(uv sync --extra ollama)"
            )
        return OpenAIChatModel(spec.split(":", 1)[1], provider=_ollama_openai_provider)
    return spec


def get_model_name(model) -> str:
    """Extract a string model name from a model object or string."""
    if isinstance(model, str):
        return model
    if hasattr(model, "_model_name"):
        provider = getattr(model, "provider_name", None) or "ollama"
        return f"{provider}:{model._model_name}"
    if hasattr(model, "model_name"):
        name = model.model_name
        if callable(name):
            name = name()
        return str(name)
    return str(model)


# Backwards-compatible alias
global_model = get_model("caddy")


# ---------------------------------------------------------------------------
# Agent config + registry
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    """Configuration blueprint for creating a pydantic-ai Agent."""

    role: str
    system_prompt: str = ""
    output_type: type | None = None
    deps_type: type | None = None
    toolset_factories: list[Callable] = field(default_factory=list)
    history_processors: list[Callable] | None = None
    name: str | None = None


_AGENT_REGISTRY: dict[str, AgentConfig] = {}


def register_agent(role: str, config: AgentConfig) -> None:
    """Register (or replace) an agent configuration for a given role."""
    _AGENT_REGISTRY[role] = config


def get_agent_config(role: str) -> AgentConfig | None:
    """Return the registered AgentConfig for a role, or None."""
    return _AGENT_REGISTRY.get(role)


def get_agent(role: str, **overrides) -> Agent:
    """Create a configured Agent instance for the given role."""
    config = _AGENT_REGISTRY.get(role)
    model = overrides.pop("model", None) or get_model(role)

    if config is None:
        return Agent(
            model,
            name=overrides.get("name", role),
            system_prompt=overrides.get("system_prompt", ""),
        )

    system_prompt = overrides.get("system_prompt", config.system_prompt)
    output_type = overrides.get("output_type", config.output_type)
    deps_type = overrides.get("deps_type", config.deps_type)
    name = overrides.get("name", config.name or config.role)
    history_processors = overrides.get("history_processors", config.history_processors)

    toolset_factories = overrides.get("toolset_factories", config.toolset_factories)
    toolsets = [factory() for factory in toolset_factories]

    kwargs: dict[str, Any] = {
        "name": name,
        "system_prompt": system_prompt,
    }
    if toolsets:
        kwargs["toolsets"] = toolsets
    if output_type is not None:
        kwargs["output_type"] = output_type
    if deps_type is not None:
        kwargs["deps_type"] = deps_type
    if history_processors:
        kwargs["history_processors"] = history_processors

    return Agent(model, **kwargs)


# ---------------------------------------------------------------------------
# Default agent registrations
# ---------------------------------------------------------------------------

_defaults_registered = False


def register_defaults() -> None:
    """Register all built-in agent configs. Safe to call multiple times."""
    global _defaults_registered
    if _defaults_registered:
        return
    _defaults_registered = True

    _common_history = [sanitize_orphaned_tool_calls]

    register_agent("caddy", AgentConfig(
        role="caddy",
        system_prompt=(
            "You are a helpful agent to facilitate adding job posts and job "
            "applications to the career caddy API. Follow the standard workflow: "
            "check duplicates → create with company check → report result."
        ),
        output_type=None,
        deps_type=CareerCaddyDeps,
        toolset_factories=[lambda: CareerCaddyToolset(scope="all")],
        history_processors=_common_history,
    ))

    register_agent("job_extractor", AgentConfig(
        role="job_extractor",
        system_prompt=(
            "You are a precise job posting data extractor. Given raw job posting "
            "text or markdown, extract and return structured data."
        ),
        output_type=None,
    ))

    try:
        from pydantic_ai.mcp import MCPServerStdio

        register_agent("email_classifier", AgentConfig(
            role="email_classifier",
            system_prompt=(
                "You are an email classifier. Read the email, determine if it "
                "contains a job posting, and tag accordingly."
            ),
            toolset_factories=[
                lambda: MCPServerStdio("python", args=["mcp_servers/email_server.py"]),
            ],
            history_processors=_common_history,
        ))

        register_agent("pipeline", AgentConfig(
            role="pipeline",
            system_prompt=(
                "Search for emails tagged 'job_post'. For each email found, read it "
                "and extract the job title and one primary job posting URL."
            ),
            toolset_factories=[
                lambda: MCPServerStdio(
                    "python", args=["mcp_servers/email_server.py"], env=os.environ.copy()
                ),
            ],
        ))

        register_agent("browser_scraper", AgentConfig(
            role="browser_scraper",
            system_prompt=(
                "Use the scrape_page tool to retrieve all visible text from the "
                "given URL. Return the raw text."
            ),
            toolset_factories=[
                lambda: MCPServerStdio(
                    "python", args=["mcp_servers/browser_server.py"], env=os.environ.copy()
                ),
            ],
        ))
    except ImportError:
        pass  # fastmcp not installed — MCP-based agents unavailable
