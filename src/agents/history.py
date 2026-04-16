"""Message history utilities for pydantic-ai agents."""

import json
from dataclasses import replace as dc_replace

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

_DEFAULT_MAX_TOKENS = 20_000
_CHARS_PER_TOKEN = 3
_MAX_TOOL_RESPONSE_CHARS = 300_000


def _estimate_tokens(msg: ModelMessage) -> int:
    """Rough token estimate for a message based on JSON character count."""
    try:
        text = json.dumps(msg, default=str)
    except Exception:
        text = str(msg)
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _cap_tool_responses(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Truncate oversized tool responses to prevent context blowout."""
    result = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            new_parts = []
            for part in msg.parts:
                if (
                    isinstance(part, ToolReturnPart)
                    and isinstance(part.content, str)
                    and len(part.content) > _MAX_TOOL_RESPONSE_CHARS
                ):
                    truncated = part.content[:_MAX_TOOL_RESPONSE_CHARS] + "\n... [truncated]"
                    part = dc_replace(part, content=truncated)
                new_parts.append(part)
            if new_parts != list(msg.parts):
                msg = dc_replace(msg, parts=new_parts)
        result.append(msg)
    return result


def truncate_message_history(
    messages: list[ModelMessage],
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> list[ModelMessage]:
    """Drop old messages when the history exceeds the token budget."""
    total = sum(_estimate_tokens(m) for m in messages)
    if total <= max_tokens:
        return messages

    kept: list[ModelMessage] = []
    budget = max_tokens
    for msg in reversed(messages):
        cost = _estimate_tokens(msg)
        if cost > budget and kept:
            break
        kept.append(msg)
        budget -= cost

    kept.reverse()

    while kept:
        first = kept[0]
        if isinstance(first, ModelRequest):
            has_only_returns = all(isinstance(p, ToolReturnPart) for p in first.parts)
            has_any_return = any(isinstance(p, ToolReturnPart) for p in first.parts)
            if has_only_returns:
                kept.pop(0)
                continue
            if has_any_return:
                stripped = [p for p in first.parts if not isinstance(p, ToolReturnPart)]
                kept[0] = dc_replace(first, parts=stripped)
        elif isinstance(first, ModelResponse):
            kept.pop(0)
            continue
        break

    return kept if kept else messages


def sanitize_orphaned_tool_calls(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Remove tool call/response pairs where not all parallel tool calls were answered."""
    cleaned: list[ModelMessage] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if isinstance(msg, ModelResponse):
            tool_calls = [p for p in msg.parts if isinstance(p, ToolCallPart)]
            if tool_calls:
                pending_ids = {p.tool_call_id for p in tool_calls}
                responded_ids: set[str] = set()

                j = i + 1
                while j < len(messages) and isinstance(messages[j], ModelRequest):
                    req = messages[j]
                    non_return_parts = [
                        p for p in req.parts if not isinstance(p, ToolReturnPart)
                    ]
                    responded_ids |= {
                        p.tool_call_id
                        for p in req.parts
                        if isinstance(p, ToolReturnPart)
                    }
                    if non_return_parts:
                        break
                    j += 1

                if pending_ids - responded_ids:
                    i = j
                    if i < len(messages) and isinstance(messages[i], ModelRequest):
                        req = messages[i]
                        stripped = [
                            p for p in req.parts
                            if not (
                                isinstance(p, ToolReturnPart)
                                and p.tool_call_id in pending_ids
                            )
                        ]
                        if len(stripped) != len(req.parts):
                            if stripped:
                                messages[i] = dc_replace(req, parts=stripped)
                            else:
                                i += 1
                    continue

        cleaned.append(msg)
        i += 1

    result: list[ModelMessage] = []
    for msg in cleaned:
        if isinstance(msg, ModelRequest):
            return_parts = [p for p in msg.parts if isinstance(p, ToolReturnPart)]
            if return_parts:
                prev = result[-1] if result else None
                if isinstance(prev, ModelResponse):
                    prev_call_ids = {
                        p.tool_call_id
                        for p in prev.parts
                        if isinstance(p, ToolCallPart)
                    }
                else:
                    prev_call_ids = set()

                orphaned_ids = {p.tool_call_id for p in return_parts} - prev_call_ids
                if orphaned_ids:
                    stripped = [
                        p
                        for p in msg.parts
                        if not (
                            isinstance(p, ToolReturnPart)
                            and p.tool_call_id in orphaned_ids
                        )
                    ]
                    if stripped:
                        result.append(dc_replace(msg, parts=stripped))
                    continue
        result.append(msg)

    return result if result else messages
