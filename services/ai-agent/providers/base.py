"""
TradingFirm — the LLM provider interface and its errors (Part 4.1).

`LLMProvider.complete_structured(system, user, schema)` is the plan row's
signature. Everything Phase 4 asks of a model goes through it: one
non-streaming request, a JSON schema in, a validated dict out, a typed error
otherwise. There is no retry anywhere in this layer.

Error messages carry a status code, a key name or an exception *type* —
never a URL, never a body, never the key (G14). Typed errors are raised
`from None`, the alphavantage_client / fred_client rule.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Errors ───────────────────────────────────────────────────────

class LLMError(Exception):
    """Base for everything this layer raises. 4.2 maps these to HTTP."""


class LLMNotConfigured(LLMError):
    """No API key. Raised before a client is built and before any HTTP."""


class LLMAuthFailed(LLMNotConfigured):
    """401 / 402 / 403. A subclass because the outcome is the same as having
    no key: nothing this process does will fix it. 402 (out of credits) lives
    here rather than under LLMRejected because it fails identically on every
    subsequent call, so it earns the same hour of cooldown."""


class LLMCapExceeded(LLMError):
    """The daily call cap. Pre-flight; no request went out."""


class LLMCooledDown(LLMError):
    """A refusal inside the cooldown window. Pre-flight; no request went out.
    The message names the status that started the cooldown."""


class LLMRateLimited(LLMError):
    """This call got the 429. The cooldown is set before it is raised."""


class LLMRejected(LLMError):
    """A 4xx that is ours to fix: 400, 404, 422 and friends. No cooldown —
    retrying is pointless but so is blocking every other call."""


class LLMUnavailable(LLMError):
    """5xx, 408, a transport error, a timeout, or an HTTP 200 whose body
    carries an `error` object instead of choices. No retry, no cooldown."""


class LLMRefused(LLMError):
    """The model refused. The message carries the refusal's length, never
    its text."""


class LLMBadResponse(LLMError):
    """A truncated answer, an unexpected finish_reason, or a body that is not
    JSON / not an object / missing a required key. Never repaired, never
    re-asked."""


# ── Result ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class LLMResult:
    """One completed call. Holds no SDK object: the response is parsed and
    dropped inside the provider (G8).

    `usage` is camelCase like every other payload in this repo, and every key
    is absent-safe — a gateway that reports no cost simply has no `cost`.
    """

    data: dict
    model: str
    finish_reason: Optional[str]
    duration_ms: int
    usage: dict = field(default_factory=dict)
    # Which OpenRouter host served the call (Part 4.4), or None if the
    # gateway did not say. Stored in ai.llm_calls.host.
    host: Optional[str] = None


# ── Request validation (pre-flight step 1) ───────────────────────

# `label` becomes response_format.json_schema.name, which OpenRouter and the
# providers behind it treat as an identifier. Keep it to identifier
# characters so a caller cannot smuggle whitespace or punctuation into it.
LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_request(system: str, user: str, schema: Any, label: str) -> None:
    """Raise ValueError on anything that must not reach the wire.

    Runs at pre-flight step 1, before the configured check, the cooldown
    check and the reservation — so a bad call costs nothing and releases
    nothing.

    The schema rules are load-bearing, not cosmetic: the provider's only
    guarantee about the answer is that every name in `required` is present,
    and OpenRouter warns that `strict: true` is "not guaranteed on every
    endpoint". A schema with no `required` list would make that check check
    nothing.
    """
    if not isinstance(system, str) or not system.strip():
        raise ValueError("system prompt must be a non-empty string")
    if not isinstance(user, str) or not user.strip():
        raise ValueError("user prompt must be a non-empty string")
    if not isinstance(label, str) or not LABEL_RE.match(label):
        raise ValueError("label must match ^[A-Za-z0-9_-]{1,64}$")
    if not isinstance(schema, dict):
        raise ValueError("schema must be a dict")
    if schema.get("type") != "object":
        raise ValueError('schema must have type "object"')
    required = schema.get("required")
    if not isinstance(required, list) or not required:
        raise ValueError("schema must have a non-empty required list")
    if not all(isinstance(name, str) and name.strip() for name in required):
        raise ValueError("schema required entries must be non-empty strings")


# ── Interface ────────────────────────────────────────────────────

class LLMProvider(ABC):
    """One structured completion. No streaming, no tools, no retries."""

    @abstractmethod
    async def complete_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        *,
        label: str = "llm",
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        effort: Optional[str] = None,
        cache_system: bool = False,
    ) -> LLMResult:
        """The three positional arguments are the plan row's. The keyword-only
        four are the extension 4.2 needs: `label` names the call in the log
        and in the schema, the rest send the classifier somewhere cheaper
        without a second provider. `cache_system` (Part 4.4) marks the system
        prompt as a cacheable prefix; off by default, so every earlier caller
        sends exactly what it sent before."""
        raise NotImplementedError
