"""
TradingFirm — LLM providers (Part 4.1).

One provider in code (decisions.md 2026-09-20, supersedes D16): an
OpenAI-compatible client with a configurable base_url. Switching gateway is
`LLM_BASE_URL`; switching model is `LLM_MODEL`. There is no provider name to
choose, which is why `LLM_PROVIDER` is not a setting.
"""

from config import settings as _settings

from .base import (
    LLMAuthFailed,
    LLMBadResponse,
    LLMCapExceeded,
    LLMCooledDown,
    LLMError,
    LLMNotConfigured,
    LLMProvider,
    LLMRateLimited,
    LLMRefused,
    LLMRejected,
    LLMResult,
    LLMUnavailable,
    validate_request,
)
from .openai_compat_provider import MODEL_REASONING, OpenAICompatProvider, resolve_effort

__all__ = [
    "LLMAuthFailed",
    "LLMBadResponse",
    "LLMCapExceeded",
    "LLMCooledDown",
    "LLMError",
    "LLMNotConfigured",
    "LLMProvider",
    "LLMRateLimited",
    "LLMRefused",
    "LLMRejected",
    "LLMResult",
    "LLMUnavailable",
    "MODEL_REASONING",
    "OpenAICompatProvider",
    "build_provider",
    "resolve_effort",
    "validate_request",
]


def build_provider(settings_obj=None, redis=None, *, http_client=None) -> LLMProvider:
    """The provider this service uses. `redis` may be None (the cap and the
    cooldown fall back to in-process state); `http_client` is the test seam."""
    return OpenAICompatProvider(settings_obj or _settings, redis, http_client=http_client)
