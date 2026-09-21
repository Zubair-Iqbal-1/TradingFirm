"""
TradingFirm — prompt files (Part 4.2).

Prompts live as .md beside this module so they read and review as prose, not
as escaped strings. They ship in the prod image through the Dockerfile's
`COPY . .`.

Read once at first use and held, never read at import: an import-time read
would make a missing or unreadable file a boot failure for the whole service,
including /health, and would fix the contents into the image for the life of
the process even when the dev twin has the tree mounted over /app.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent

# name -> text, filled on first use.
_CACHE: dict[str, str] = {}

HEADLINE_CLASSIFY = "headline_classify"
VERDICT = "verdict"                      # Part 4.4


class PromptMissing(Exception):
    """A prompt file is absent, unreadable or empty. The caller turns this
    into a 500: it is our bug, never the caller's input."""


def load(name: str, *, reload: bool = False) -> str:
    """The text of `prompts/{name}.md`.

    Raises PromptMissing rather than returning an empty system prompt — the
    provider would reject a blank system prompt anyway (validate_request),
    and a silently empty prompt would be a paid call that classifies nothing.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("prompt name must be a non-empty string")
    key = name.strip()
    if not reload and key in _CACHE:
        return _CACHE[key]

    path = PROMPT_DIR / f"{key}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise PromptMissing(f"prompt {key!r} is unreadable ({type(e).__name__})") from None
    if not text.strip():
        raise PromptMissing(f"prompt {key!r} is empty")

    _CACHE[key] = text
    logger.info(f"Loaded prompt {key!r} ({len(text)} chars)")
    return text


def clear_cache(name: Optional[str] = None) -> None:
    """Drop the held text, for tests."""
    if name is None:
        _CACHE.clear()
    else:
        _CACHE.pop(name.strip(), None)
