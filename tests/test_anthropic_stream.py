from free_llm_proxy.anthropic_translate import AnthropicStreamTranslator


def _names(events):
    return [name for name, _ in events]


def _chunk(content=None, tool_calls=None, finish=None, usage=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        chunk["usage"] = usage
    return chunk


def test_text_only_sequence():
    t = AnthropicStreamTranslator("m", input_tokens=5)
    events = list(t.start())
    events += t.feed(_chunk(content="Hel"))
    events += t.feed(_chunk(content="lo"))
    events += t.feed(_chunk(finish="stop", usage={"completion_tokens": 2}))
    events += t.finish()

    assert _names(events) == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[0][1]["message"]["usage"]["input_tokens"] == 5
    assert events[0][1]["message"]["id"].startswith("msg_")
    assert events[2][1]["delta"] == {"type": "text_delta", "text": "Hel"}
    message_delta = events[-2][1]
    assert message_delta["delta"]["stop_reason"] == "end_turn"
    assert message_delta["usage"]["output_tokens"] == 2


def test_tool_use_sequence():
    t = AnthropicStreamTranslator("m")
    events = list(t.start())
    events += t.feed(
        _chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_w", "arguments": ""},
                }
            ]
        )
    )
    events += t.feed(_chunk(tool_calls=[{"index": 0, "function": {"arguments": '{"x":'}}]))
    events += t.feed(_chunk(tool_calls=[{"index": 0, "function": {"arguments": "1}"}}]))
    events += t.feed(_chunk(finish="tool_calls"))
    events += t.finish()

    assert _names(events) == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    block_start = events[1][1]
    assert block_start["content_block"]["type"] == "tool_use"
    assert block_start["content_block"]["name"] == "get_w"
    assert block_start["content_block"]["id"] == "call_1"
    assert block_start["content_block"]["input"] == {}
    assert events[2][1]["delta"] == {"type": "input_json_delta", "partial_json": '{"x":'}
    assert events[-2][1]["delta"]["stop_reason"] == "tool_use"


def test_text_then_tool_switches_block():
    t = AnthropicStreamTranslator("m")
    events = list(t.start())
    events += t.feed(_chunk(content="hi"))
    events += t.feed(
        _chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "c",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ]
        )
    )
    events += t.feed(_chunk(finish="tool_calls"))
    events += t.finish()

    assert _names(events) == [
        "message_start",
        "content_block_start",  # text, index 0
        "content_block_delta",
        "content_block_stop",  # close text
        "content_block_start",  # tool, index 1
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1][1]["index"] == 0
    assert events[1][1]["content_block"]["type"] == "text"
    assert events[4][1]["index"] == 1
    assert events[4][1]["content_block"]["type"] == "tool_use"


def test_two_tool_calls_distinct_blocks():
    t = AnthropicStreamTranslator("m")
    events = list(t.start())
    events += t.feed(
        _chunk(
            tool_calls=[
                {
                    "index": 0,
                    "id": "a",
                    "type": "function",
                    "function": {"name": "f", "arguments": "{}"},
                }
            ]
        )
    )
    events += t.feed(
        _chunk(
            tool_calls=[
                {
                    "index": 1,
                    "id": "b",
                    "type": "function",
                    "function": {"name": "g", "arguments": "{}"},
                }
            ]
        )
    )
    events += t.feed(_chunk(finish="tool_calls"))
    events += t.finish()

    starts = [data for name, data in events if name == "content_block_start"]
    assert [s["content_block"]["name"] for s in starts] == ["f", "g"]
    assert [s["index"] for s in starts] == [0, 1]


def test_error_event_shape():
    t = AnthropicStreamTranslator("m")
    name, data = t.error_event("overloaded_error", "boom")
    assert name == "error"
    assert data == {"type": "error", "error": {"type": "overloaded_error", "message": "boom"}}
