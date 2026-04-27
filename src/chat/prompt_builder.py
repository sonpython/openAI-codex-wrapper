"""
Prompt builder: converts OpenAI message list into a single Codex prompt string.

Format (per PDF skeleton):
    Role:\\n<content>\\n\\nRole:\\n<content>\\n\\nAssistant:\\n

Rules:
  - Image content parts are already rejected upstream (chat_request.py validator).
    If a list of TextContent arrives here, concatenate text fields only.
  - Total length is capped at ``settings.CHAT_MAX_PROMPT_CHARS``; raises
    ``ValueError`` which the route handler converts to a 400.
  - System, user, assistant all get their role capitalised as the header.
"""

from __future__ import annotations

from src.gateway.schemas.chat_request import Message, TextContent
from src.settings import get_settings


def build_prompt(messages: list[Message]) -> str:
    """Convert a list of chat messages into a single text prompt for Codex.

    Args:
        messages: Validated list of Message objects (image parts already rejected).

    Returns:
        Formatted multi-turn prompt string ending with ``Assistant:\\n``.

    Raises:
        ValueError: If the assembled prompt exceeds ``CHAT_MAX_PROMPT_CHARS``.
    """
    parts: list[str] = []
    for msg in messages:
        if isinstance(msg.content, list):
            # Only TextContent parts arrive here (ImageContent rejected by validator).
            text = "".join(part.text for part in msg.content if isinstance(part, TextContent))
        else:
            text = msg.content
        parts.append(f"{msg.role.capitalize()}:\n{text}")

    prompt = "\n\n".join(parts) + "\n\nAssistant:\n"

    max_chars = get_settings().chat_max_prompt_chars
    if len(prompt) > max_chars:
        raise ValueError(
            f"prompt exceeds maximum length of {max_chars} characters " f"(got {len(prompt)})"
        )

    return prompt
