"""Pure translation between Anthropic Messages API and OpenAI Chat Completions.

No I/O, no FastAPI. Everything here is unit-testable in isolation:

- `anthropic_to_openai_request`  — request in  (Anthropic → OpenAI dict)
- `openai_to_anthropic_response` — response out (OpenAI dict → Anthropic dict)
- `map_stop_reason`              — finish_reason → stop_reason
- `estimate_input_tokens`        — heuristic token count for count_tokens
- `AnthropicStreamTranslator`    — OpenAI streaming chunks → Anthropic SSE events

See spec/anthropic.md sections 4-6. MVP scope: text + system + tools (no images,
no thinking blocks, no prompt caching).
"""

import json
import math
import uuid
from typing import Any


class AnthropicError(Exception):
    """An error to render inside Anthropic's top-level error envelope.

    Carries the HTTP status and the Anthropic `error.type`. A FastAPI exception
    handler turns it into `{"type":"error","error":{"type":...,"message":...}}`.
    """

    def __init__(self, status_code: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message

    def body(self) -> dict[str, Any]:
        return {"type": "error", "error": {"type": self.error_type, "message": self.message}}


def _invalid(message: str) -> AnthropicError:
    return AnthropicError(400, "invalid_request_error", message)


# --------------------------------------------------------------------------- #
# Request: Anthropic Messages  →  OpenAI Chat Completions
# --------------------------------------------------------------------------- #


def _system_to_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [
            blk.get("text", "")
            for blk in system
            if isinstance(blk, dict) and blk.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _tool_result_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(blk.get("text", ""))
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(p for p in parts if p)
    return str(content)


def _translate_tool(tool: dict) -> dict:
    fn: dict[str, Any] = {
        "name": tool.get("name"),
        "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
    }
    if tool.get("description"):
        fn["description"] = tool["description"]
    return {"type": "function", "function": fn}


def _translate_tool_choice(tool_choice: Any) -> str | dict | None:
    if not isinstance(tool_choice, dict):
        return None
    t = tool_choice.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "none":
        return "none"
    if t == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return None


def _translate_message(msg: Any) -> list[dict]:
    """One Anthropic message → one or more OpenAI messages.

    A user message carrying `tool_result` blocks expands into standalone
    `role:"tool"` messages (one per block), emitted before the user's own text.
    """
    if not isinstance(msg, dict):
        raise _invalid("Each message must be a JSON object.")
    role = msg.get("role")
    if role not in ("user", "assistant"):
        raise _invalid(f"Unsupported message role: {role!r}.")
    content = msg.get("content")

    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if not isinstance(content, list):
        raise _invalid("Message `content` must be a string or an array of blocks.")

    text_parts: list[str] = []
    tool_calls: list[dict] = []
    tool_messages: list[dict] = []
    for block in content:
        btype = block.get("type") if isinstance(block, dict) else None
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
        elif btype == "tool_result":
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _tool_result_content_to_text(block.get("content")),
                }
            )
        elif btype == "image":
            raise _invalid(
                "Image content blocks are not supported by this proxy (text + tools only)."
            )
        # thinking / redacted_thinking / unknown → dropped

    out: list[dict] = list(tool_messages)
    main: dict[str, Any] = {"role": role}
    has_main = False
    if text_parts:
        main["content"] = "\n".join(p for p in text_parts if p)
        has_main = True
    if tool_calls:
        main["tool_calls"] = tool_calls
        main.setdefault("content", None)  # OpenAI allows null content with tool_calls
        has_main = True
    if has_main:
        out.append(main)
    return out


def anthropic_to_openai_request(body: Any) -> dict:
    """Translate an Anthropic Messages request body into an OpenAI request dict.

    The `model` and `stream` fields are intentionally NOT carried over — model
    selection is the proxy's job and streaming is decided by the endpoint.
    Raises `AnthropicError` (400) for malformed input.
    """
    if not isinstance(body, dict):
        raise _invalid("Request body must be a JSON object.")

    messages_in = body.get("messages")
    if not isinstance(messages_in, list) or not messages_in:
        raise _invalid("`messages` must be a non-empty array.")
    if body.get("max_tokens") is None:
        raise _invalid("`max_tokens` is required.")

    openai_messages: list[dict] = []
    system_text = _system_to_text(body.get("system"))
    if system_text:
        openai_messages.append({"role": "system", "content": system_text})
    for msg in messages_in:
        openai_messages.extend(_translate_message(msg))

    out: dict[str, Any] = {"messages": openai_messages, "max_tokens": body["max_tokens"]}

    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("stop_sequences") is not None:
        out["stop"] = body["stop_sequences"]
    # top_k and metadata are intentionally dropped (no OpenAI Chat equivalent).

    if body.get("tools"):
        out["tools"] = [_translate_tool(t) for t in body["tools"]]
    tool_choice = _translate_tool_choice(body.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice
    tc = body.get("tool_choice")
    if isinstance(tc, dict) and tc.get("disable_parallel_tool_use"):
        out["parallel_tool_calls"] = False

    return out


# --------------------------------------------------------------------------- #
# Response: OpenAI Chat Completions  →  Anthropic Messages
# --------------------------------------------------------------------------- #

_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def map_stop_reason(finish_reason: str | None) -> str:
    if not finish_reason:
        return "end_turn"
    return _STOP_REASON.get(finish_reason, "end_turn")


def _message_to_content_blocks(message: dict) -> list[dict]:
    blocks: list[dict] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                text = part.get("text", "")
                if text:
                    blocks.append({"type": "text", "text": text})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id") or f"toolu_{uuid.uuid4().hex}",
                "name": fn.get("name"),
                "input": args,
            }
        )
    return blocks


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def openai_to_anthropic_response(resp: dict, model_id: str) -> dict:
    choice = (resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = resp.get("usage") or {}
    return {
        "id": new_message_id(),
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": _message_to_content_blocks(message),
        "stop_reason": map_stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


# --------------------------------------------------------------------------- #
# count_tokens — deliberately approximate (no per-model tokenizer)
# --------------------------------------------------------------------------- #


def estimate_input_tokens(body: dict, divisor: float = 4.0) -> int:
    parts: list[str] = [_system_to_text(body.get("system"))]
    for msg in body.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get("type")
                if btype == "text":
                    parts.append(blk.get("text", ""))
                elif btype == "tool_use":
                    parts.append(json.dumps(blk.get("input") or {}, ensure_ascii=False))
                elif btype == "tool_result":
                    parts.append(_tool_result_content_to_text(blk.get("content")))
    for tool in body.get("tools") or []:
        parts.append(json.dumps(tool, ensure_ascii=False))
    text = "".join(p for p in parts if p)
    if not text:
        return 0
    if divisor <= 0:
        divisor = 4.0
    return max(1, math.ceil(len(text) / divisor))


# --------------------------------------------------------------------------- #
# Streaming: OpenAI chunks  →  Anthropic SSE events
# --------------------------------------------------------------------------- #

# An event is a (name, data) pair; the endpoint serializes it as
#   event: <name>\n data: <json>\n\n
SSEEvent = tuple[str, dict[str, Any]]


class AnthropicStreamTranslator:
    """Stateful translator: feed OpenAI chunk dicts, get Anthropic SSE events.

    Drives the event sequence message_start → (content_block_start/delta/stop)* →
    message_delta → message_stop, opening a new content block whenever the kind
    of output switches between text and a tool call (spec/anthropic.md §6.3).
    """

    def __init__(self, model_id: str, input_tokens: int = 0) -> None:
        self.model_id = model_id
        self.input_tokens = input_tokens
        self.message_id = new_message_id()
        self._index = -1
        self._open_type: str | None = None  # "text" | "tool_use" | None
        self._tool_block_index: dict[int, int] = {}  # OpenAI tool index → block index
        self._finish_reason: str | None = None
        self._output_tokens = 0

    def start(self) -> list[SSEEvent]:
        return [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model_id,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
                    },
                },
            )
        ]

    def feed(self, chunk: dict) -> list[SSEEvent]:
        events: list[SSEEvent] = []
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}

        text = delta.get("content")
        if text:
            events += self._ensure_text_block()
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            )

        for tc in delta.get("tool_calls") or []:
            events += self._feed_tool_call(tc)

        if choice.get("finish_reason") is not None:
            self._finish_reason = choice["finish_reason"]
        usage = chunk.get("usage")
        if usage and usage.get("completion_tokens") is not None:
            self._output_tokens = usage["completion_tokens"]
        return events

    def finish(self) -> list[SSEEvent]:
        events = self._close_open_block()
        events.append(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": map_stop_reason(self._finish_reason),
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": self._output_tokens},
                },
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        return events

    @staticmethod
    def error_event(error_type: str, message: str) -> SSEEvent:
        return ("error", {"type": "error", "error": {"type": error_type, "message": message}})

    # -- internals ---------------------------------------------------------- #

    def _close_open_block(self) -> list[SSEEvent]:
        if self._open_type is None:
            return []
        idx = self._index
        self._open_type = None
        return [("content_block_stop", {"type": "content_block_stop", "index": idx})]

    def _ensure_text_block(self) -> list[SSEEvent]:
        if self._open_type == "text":
            return []
        events = self._close_open_block()
        self._index += 1
        self._open_type = "text"
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self._index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
        return events

    def _feed_tool_call(self, tc: dict) -> list[SSEEvent]:
        events: list[SSEEvent] = []
        oai_index = tc.get("index", 0)
        fn = tc.get("function") or {}
        if oai_index not in self._tool_block_index:
            events += self._close_open_block()
            self._index += 1
            self._open_type = "tool_use"
            self._tool_block_index[oai_index] = self._index
            events.append(
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self._index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc.get("id") or f"toolu_{uuid.uuid4().hex}",
                            "name": fn.get("name") or "",
                            "input": {},
                        },
                    },
                )
            )
        arguments = fn.get("arguments")
        if arguments:
            events.append(
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._tool_block_index[oai_index],
                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                    },
                )
            )
        return events
