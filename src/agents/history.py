"""Message history utilities for pydantic-ai agents."""

from dataclasses import replace as dc_replace

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)


def sanitize_orphaned_tool_calls(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Drop tool-call / tool-return pairs that would break provider APIs.

    OpenAI (and others) reject histories where a `tool` message isn't
    preceded by a matching `tool_calls` message, or where a ModelResponse
    emits tool calls that never got answered. Both shapes can appear after
    retries or transient tool failures. This pass:

      1. Drops ModelResponses whose tool calls weren't fully answered, and
         strips the corresponding orphan ToolReturnParts from the next
         ModelRequest.
      2. Final guarantor walk: tracks every tool_call_id emitted by a
         ModelResponse and strips any later ToolReturnPart whose id was
         never emitted.
    """
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

    emitted_ids: set[str] = set()
    guarded: list[ModelMessage] = []
    for msg in cleaned:
        if isinstance(msg, ModelResponse):
            for p in msg.parts:
                if isinstance(p, ToolCallPart):
                    emitted_ids.add(p.tool_call_id)
            guarded.append(msg)
        elif isinstance(msg, ModelRequest):
            new_parts = [
                p for p in msg.parts
                if not (isinstance(p, ToolReturnPart) and p.tool_call_id not in emitted_ids)
            ]
            if not new_parts:
                continue
            if len(new_parts) != len(msg.parts):
                guarded.append(dc_replace(msg, parts=new_parts))
            else:
                guarded.append(msg)
        else:
            guarded.append(msg)

    return guarded if guarded else messages
